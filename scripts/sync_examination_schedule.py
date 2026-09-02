"""Run one examination schedule synchronization for deployment diagnostics."""

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend import database as db  # noqa: E402
from backend.examination_schedule import (  # noqa: E402
    ExaminationScheduleUnavailable,
    sync_now,
)


def main() -> int:
    db.init_db()
    try:
        result = sync_now()
    except ExaminationScheduleUnavailable as exc:
        print(json.dumps({"status": "error", "detail": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("status") == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
