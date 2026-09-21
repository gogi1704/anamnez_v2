"""Normalize historical tube identifiers to digits-only values.

Run without arguments for a preview.  Use ``--apply`` only after taking a
database backup.  Related cached valuations are removed so the regular
backfill can calculate them again using the corrected identifier.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend import database as db  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="write changes to the database")
    args = parser.parse_args()

    db.init_db()
    with db.connection() as conn:
        tables = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        rows = conn.execute(
            """SELECT chel_id, tube_number FROM user_profile
            WHERE TRIM(COALESCE(tube_number,'')) <> ''
            ORDER BY chel_id"""
        ).fetchall()
        changes = []
        for row in rows:
            original = str(row["tube_number"] or "")
            normalized = db.normalize_tube_number(original)
            if normalized != original:
                changes.append({
                    "chel_id": str(row["chel_id"]),
                    "from": original,
                    "to": normalized,
                    "action": "normalized" if normalized else "cleared",
                })

        if args.apply and changes:
            now = db.utc_now()
            conn.execute("BEGIN IMMEDIATE")
            for item in changes:
                chel_id = item["chel_id"]
                original = item["from"]
                normalized = item["to"]
                conn.execute(
                    "UPDATE user_profile SET tube_number=?, updated_at=? WHERE chel_id=?",
                    (normalized, now, chel_id),
                )
                if "lab_result_value_estimates" in tables:
                    conn.execute(
                        "DELETE FROM lab_result_value_estimates WHERE chel_id=?",
                        (chel_id,),
                    )
                if "lab_result_subscriptions" in tables:
                    if normalized:
                        conn.execute(
                            """UPDATE OR IGNORE lab_result_subscriptions
                            SET med_id=?, updated_at=?
                            WHERE chel_id=? AND med_id=?""",
                            (normalized, now, chel_id, original),
                        )
                    conn.execute(
                        "DELETE FROM lab_result_subscriptions WHERE chel_id=? AND med_id=?",
                        (chel_id, original),
                    )
                if "lab_interpretations" in tables:
                    if normalized:
                        conn.execute(
                            """UPDATE OR IGNORE lab_interpretations SET med_id=?
                            WHERE chel_id=? AND med_id=?""",
                            (normalized, chel_id, original),
                        )
                    conn.execute(
                        "DELETE FROM lab_interpretations WHERE chel_id=? AND med_id=?",
                        (chel_id, original),
                    )
            conn.commit()

    summary = {
        "status": "applied" if args.apply else "preview",
        "profiles_scanned": len(rows),
        "changed": len(changes),
        "normalized": sum(item["action"] == "normalized" for item in changes),
        "cleared": sum(item["action"] == "cleared" for item in changes),
        "changes": changes,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
