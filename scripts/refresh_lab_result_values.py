"""Backfill cached price estimates for users with linked result tubes."""

from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend import database as db  # noqa: E402
from backend.lab_result_valuation import calculate_and_store, document_fingerprint  # noqa: E402
from backend.lab_results import LabResultsUnavailable, lookup_many_lab_results  # noqa: E402


def main() -> int:
    db.init_db()
    users = db.list_linked_user_tubes()
    try:
        results = lookup_many_lab_results([item["med_id"] for item in users])
    except LabResultsUnavailable as exc:
        print(json.dumps({"status": "error", "detail": str(exc)}, ensure_ascii=False))
        return 1
    summary = {"status": "ok", "users": len(users), "ready": 0, "skipped": 0, "failed": 0}
    for item in users:
        result = results.get(item["med_id"])
        if not result or result.status != "found":
            summary["skipped"] += 1
            continue
        documents = result.to_dict().get("documents") or []
        fingerprint = document_fingerprint(documents)
        cached = db.get_lab_result_value_estimate(item["chel_id"])
        if cached and cached.get("document_fingerprint") == fingerprint and cached.get("status") in {
            "ready", "partial", "unrecognized",
        }:
            summary["skipped"] += 1
            continue
        try:
            calculate_and_store(item["chel_id"], item["med_id"], documents)
            summary["ready"] += 1
        except Exception:
            summary["failed"] += 1
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if not summary["failed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
