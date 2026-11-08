"""Print a few proposal/audit rows for SUBMISSION evidence."""
from __future__ import annotations

import json
import sqlite3
import sys


def dump(path: str) -> None:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    print("===", path, "===")
    print("proposals", con.execute("select count(*) c from proposals").fetchone()["c"])
    print(
        "applied approvals",
        con.execute("select count(*) from controller_approvals where status='applied'").fetchone()[0],
    )
    rows = con.execute(
        "select proposal_id, workflow_id, status, source, changed_dims_json, impact_score, rationale "
        "from proposals order by created_at limit 8"
    ).fetchall()
    for r in rows:
        print(json.dumps(dict(r), default=str))
    cols = [d[1] for d in con.execute("pragma table_info(audit_decisions)").fetchall()]
    print("audit columns", cols)
    sel = "decision_id, workflow_id" if "decision_id" in cols else "*"
    if "kind" in cols:
        sel += ", kind"
    if "summary" in cols:
        sel += ", summary"
    elif "message" in cols:
        sel += ", message"
    aud = con.execute(f"select {sel} from audit_decisions order by rowid desc limit 5").fetchall()
    for a in aud:
        print("audit", json.dumps(dict(a), default=str))
    con.close()


if __name__ == "__main__":
    for p in sys.argv[1:]:
        dump(p)
