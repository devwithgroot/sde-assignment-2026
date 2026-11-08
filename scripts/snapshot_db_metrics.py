import json
import sqlite3
import sys

path = sys.argv[1]
con = sqlite3.connect(path)
con.row_factory = sqlite3.Row
out = {
    "events": con.execute("select count(*) c from events").fetchone()["c"],
    "calls": con.execute("select count(*) c from calls").fetchone()["c"],
    "leads": con.execute("select count(*) c from leads").fetchone()["c"],
    "proposals": con.execute("select count(*) c from proposals").fetchone()["c"],
    "approvals": con.execute("select count(*) c from controller_approvals").fetchone()["c"],
    "applied": con.execute(
        "select count(*) c from controller_approvals where status='applied'"
    ).fetchone()["c"],
}
c = con.execute(
    "select value from observability_counters where name='controller.cycles'"
).fetchone()
out["cycles"] = float(c["value"]) if c else None
cred = con.execute("select * from credit_budget_state where id=1").fetchone()
out["credits"] = dict(cred) if cred else None
print(json.dumps(out))
con.close()
