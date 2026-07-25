"""Безопасная согласованная резервная копия SQLite с ротацией."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import os
import sqlite3
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Резервная копия базы Консилиума")
    parser.add_argument("--source", type=Path, default=settings.database_path)
    parser.add_argument("--destination", type=Path, default=Path("/var/backups/consilium"))
    parser.add_argument("--keep", type=int, default=14)
    args = parser.parse_args()

    source = args.source.resolve()
    destination = args.destination.resolve()
    if not source.is_file():
        print(f"База не найдена: {source}", file=sys.stderr)
        return 1
    if args.keep < 1 or args.keep > 365:
        print("--keep должен быть от 1 до 365", file=sys.stderr)
        return 1

    destination.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    final_path = destination / f"consilium-{timestamp}.db.gz"

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

        checksum = sha256(final_path)
        final_path.with_suffix(final_path.suffix + ".sha256").write_text(
            f"{checksum}  {final_path.name}\n", encoding="ascii"
        )

        copies = sorted(destination.glob("consilium-*.db.gz"), reverse=True)
        for old_copy in copies[args.keep:]:
            old_copy.unlink(missing_ok=True)
            old_copy.with_suffix(old_copy.suffix + ".sha256").unlink(missing_ok=True)
        print(f"Резервная копия создана: {final_path}")
        return 0
    except (OSError, sqlite3.Error) as exc:
        if temp_name:
            try:
                Path(temp_name).unlink(missing_ok=True)
            except OSError:
                pass
        try:
            final_path.unlink(missing_ok=True)
        except OSError:
            pass
        print(f"Не удалось создать резервную копию: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
