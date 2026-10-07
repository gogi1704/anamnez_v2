"""Безопасная согласованная резервная копия SQLite с ротацией."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from backend.config import settings  # noqa: E402


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def encrypt_with_gpg(source: Path, recipient: str) -> Path:
    """Encrypt `source` for `recipient`'s GPG public key, return the new path.

    The backup host only ever needs the *public* key to run this — the
    matching private key should live offline, away from this server, so a
    compromised backup host can't decrypt its own backups.  Any failure here
    must stop the backup, never fall back to shipping it unencrypted.
    """
    encrypted_path = source.with_suffix(source.suffix + ".gpg")
    try:
        subprocess.run(
            [
                "gpg", "--batch", "--yes", "--trust-model", "always",
                "--recipient", recipient,
                "--output", str(encrypted_path),
                "--encrypt", str(source),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("gpg не найден в PATH — установите gnupg") from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"gpg завершился с ошибкой: {exc.stderr.strip()}") from exc
    return encrypted_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Резервная копия базы Консилиума")
    parser.add_argument("--source", type=Path, default=settings.database_path)
    parser.add_argument("--destination", type=Path, default=Path("/var/backups/consilium"))
    parser.add_argument("--keep", type=int, default=14)
    parser.add_argument(
        "--label", default="consilium",
        help="Safe filename prefix, for example consilium or analytics",
    )
    parser.add_argument(
        "--gpg-recipient",
        default=os.getenv("BACKUP_GPG_RECIPIENT", ""),
        help="GPG key id/email/fingerprint to encrypt the backup for (or BACKUP_GPG_RECIPIENT)",
    )
    parser.add_argument(
        "--allow-plaintext", action="store_true",
        help="Explicit emergency opt-out; without it a missing GPG recipient stops the backup",
    )
    args = parser.parse_args()

    source = args.source.resolve()
    destination = args.destination.resolve()
    label = str(args.label or "").strip().lower()
    recipient = str(args.gpg_recipient or "").strip()
    if not source.is_file():
        print(f"База не найдена: {source}", file=sys.stderr)
        return 1
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,39}", label):
        print("--label должен содержать только a-z, 0-9, _ или -", file=sys.stderr)
        return 1
    if args.keep < 1 or args.keep > 365:
        print("--keep должен быть от 1 до 365", file=sys.stderr)
        return 1
    if not recipient and not args.allow_plaintext:
        print(
            "Шифрование обязательно: задайте --gpg-recipient или "
            "BACKUP_GPG_RECIPIENT. Для осознанного аварийного исключения есть "
            "--allow-plaintext.",
            file=sys.stderr,
        )
        return 1

    destination.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    final_path = destination / f"{label}-{timestamp}.db.gz"

    temp_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            prefix=".consilium-", suffix=".db", dir=destination, delete=False
        ) as handle:
            temp_name = handle.name
        temp_db = Path(temp_name)
        with closing(sqlite3.connect(source)) as source_conn:
            with closing(sqlite3.connect(temp_db)) as backup_conn:
                source_conn.backup(backup_conn)
                check = backup_conn.execute("PRAGMA quick_check").fetchone()[0]
                if check != "ok":
                    raise sqlite3.DatabaseError(f"quick_check: {check}")
        with temp_db.open("rb") as raw, gzip.open(final_path, "wb", compresslevel=6) as packed:
            while chunk := raw.read(1024 * 1024):
                packed.write(chunk)
        temp_db.unlink(missing_ok=True)

        if recipient:
            encrypted_path = encrypt_with_gpg(final_path, recipient)
            final_path.unlink(missing_ok=True)
            final_path = encrypted_path
        else:
            print(
                "ВНИМАНИЕ: использован явный --allow-plaintext; бэкап не зашифрован",
                file=sys.stderr,
            )

        checksum = sha256(final_path)
        final_path.with_suffix(final_path.suffix + ".sha256").write_text(
            f"{checksum}  {final_path.name}\n", encoding="ascii"
        )

        copies = sorted(
            (
                *destination.glob(f"{label}-*.db.gz"),
                *destination.glob(f"{label}-*.db.gz.gpg"),
            ),
            key=lambda p: p.name,
            reverse=True,
        )
        for old_copy in copies[args.keep:]:
            old_copy.unlink(missing_ok=True)
            old_copy.with_suffix(old_copy.suffix + ".sha256").unlink(missing_ok=True)
        print(f"Резервная копия создана: {final_path}")
        return 0
    except (OSError, sqlite3.Error, RuntimeError) as exc:
        if temp_name:
            try:
                Path(temp_name).unlink(missing_ok=True)
            except OSError:
                pass
        for stray in (
            final_path,
            final_path.with_suffix(final_path.suffix + ".gpg"),
            final_path.with_suffix(final_path.suffix + ".sha256"),
            final_path.with_suffix(final_path.suffix + ".gpg.sha256"),
        ):
            try:
                stray.unlink(missing_ok=True)
            except OSError:
                pass
        print(f"Не удалось создать резервную копию: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
