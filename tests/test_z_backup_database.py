import gzip
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from scripts import backup_database


class BackupDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.source = self.root / "source.db"
        self.destination = self.root / "backups"
        with closing(sqlite3.connect(self.source)) as connection:
            connection.execute("CREATE TABLE sample (value TEXT NOT NULL)")
            connection.execute("INSERT INTO sample VALUES ('secret medical data')")
            connection.commit()

    def tearDown(self):
        self.temp_dir.cleanup()

    def run_backup(self, *extra: str) -> int:
        argv = [
            "backup_database.py", "--source", str(self.source),
            "--destination", str(self.destination), "--label", "analytics",
            *extra,
        ]
        with mock.patch("sys.argv", argv), mock.patch.dict(
            "os.environ", {"BACKUP_GPG_RECIPIENT": ""}, clear=False,
        ):
            return backup_database.main()

    def test_missing_recipient_fails_closed_without_plaintext(self):
        self.assertEqual(self.run_backup(), 1)
        self.assertFalse(list(self.destination.glob("*")) if self.destination.exists() else [])

    def test_plaintext_requires_explicit_emergency_flag(self):
        self.assertEqual(self.run_backup("--allow-plaintext"), 0)
        copies = list(self.destination.glob("analytics-*.db.gz"))
        self.assertEqual(len(copies), 1)
        with gzip.open(copies[0], "rb") as source:
            self.assertTrue(source.read(16).startswith(b"SQLite format 3"))

    def test_encrypted_backup_removes_plain_archive_and_keeps_checksum(self):
        def fake_encrypt(source: Path, recipient: str) -> Path:
            self.assertEqual(recipient, "TEST-FINGERPRINT")
            encrypted = source.with_suffix(source.suffix + ".gpg")
            shutil.copyfile(source, encrypted)
            return encrypted

        with mock.patch.object(backup_database, "encrypt_with_gpg", side_effect=fake_encrypt):
            self.assertEqual(self.run_backup("--gpg-recipient", "TEST-FINGERPRINT"), 0)

        encrypted = list(self.destination.glob("analytics-*.db.gz.gpg"))
        self.assertEqual(len(encrypted), 1)
        self.assertFalse(list(self.destination.glob("analytics-*.db.gz")))
        self.assertTrue(encrypted[0].with_suffix(".gpg.sha256").is_file())


if __name__ == "__main__":
    unittest.main()
