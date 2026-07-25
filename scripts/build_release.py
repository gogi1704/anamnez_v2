"""Создаёт проверенный серверный архив без локальных секретов и данных."""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
INCLUDED = [
    "backend",
    "static",
    "scripts",
    "deploy",
    "tests",
    "docs",
    "index.html",
    "run.py",
    "requirements.txt",
    "README.md",
    ".env.production.example",
]


def archive_filter(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
    parts = Path(info.name).parts
    if "__pycache__" in parts or info.name.endswith((".pyc", ".pyo")):
        return None
    return info


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description="Сборка release-архива Консилиума")
    parser.add_argument("--skip-tests", action="store_true")
    args = parser.parse_args()

    missing = [name for name in INCLUDED if not (PROJECT_DIR / name).exists()]
    if missing:
        print("Не найдены обязательные пути: " + ", ".join(missing), file=sys.stderr)
        return 1

    if not args.skip_tests:
        result = subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
            cwd=PROJECT_DIR,
            check=False,
        )
        if result.returncode:
            print("Архив не создан: тесты завершились с ошибкой", file=sys.stderr)
            return result.returncode

    dist_dir = PROJECT_DIR / "dist"
    dist_dir.mkdir(exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive = dist_dir / f"consilium-release-{timestamp}.tar.gz"
    with tarfile.open(archive, "w:gz", compresslevel=6) as package:
        for name in INCLUDED:
            package.add(PROJECT_DIR / name, arcname=name, filter=archive_filter)

    checksum = file_sha256(archive)
    checksum_path = archive.with_suffix(archive.suffix + ".sha256")
    checksum_path.write_text(f"{checksum}  {archive.name}\n", encoding="ascii")
    print(f"Release-архив: {archive}")
    print(f"SHA-256: {checksum_path}")
    print("Локальные .env, базы, журналы и виртуальные окружения в архив не входят.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
