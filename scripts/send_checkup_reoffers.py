"""Queue tomorrow's eligible check-up reminders (safe to run repeatedly)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend import analytics, database as db  # noqa: E402
from backend.checkup_reoffers import send_due  # noqa: E402


if __name__ == "__main__":
    db.init_db()
    analytics.init_db()
    print(json.dumps(send_due(), ensure_ascii=False))
