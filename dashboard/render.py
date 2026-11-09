"""HTML rendering for the read-only dashboard."""

from __future__ import annotations

from html import escape
from typing import Any

from dashboard.queries import DashboardSnapshot


def _fmt_rate(value: Any) -> str:
    if value is None:
        return "n/a"
    try:
        return f"{float(value):.4f}"
    except (TypeError, ValueError):
        return "n/a"


def _fmt_int(value: Any) -> str:
    if value is None:
        return "n/a"
    return str(value)


def _rows(headers: list[str], body_rows: list[list[str]]) -> str:
    head = "".join(f"<th>{escape(h)}</th>" for h in headers)
    if not body_rows:
        return f"<table><thead><tr>{head}</tr></thead><tbody><tr><td colspan='{len(headers)}' class='empty'>No data</td></tr></tbody></table>"
    lines = []
    for row in body_rows:
        lines.append("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>")
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(lines)}</tbody></table>"


def render_html(snap: DashboardSnapshot) -> str:
    status = "ok" if snap.ok and not snap.errors else "error"
    sim = snap.simulator or {}
    sim_block = (
        f"<p>scenario=<code>{escape(str(sim.get('scenario', 'n/a')))}</code> "
        f"minute=<code>{escape(str(sim.get('sim_minute', 'n/a')))}</code> "
        f"finished=<code>{escape(str(sim.get('finished', 'n/a')))}</code> "
        f"credits=<code>{escape(str(sim.get('credits_remaining', 'n/a')))}</code></p>"
        if sim
        else "<p class='warn'>Simulator not contacted (optional). Pass <code>--sim-url</code> to show live clock.</p>"
    )

    errors = "".join(f"<li>{escape(e)}</li>" for e in snap.errors) or "<li class='ok'>None</li>"
    warnings = "".join(f"<li>{escape(w)}</li>" for w in snap.warnings) or "<li class='ok'>None</li>"

    count_rows = [[escape(k), _fmt_int(v)] for k, v in sorted(snap.counts.items())]
    kpi_rows = [
        [
            escape(str(r.get("workflow_id"))),
            _fmt_int(r.get("dials")),
            _fmt_int(r.get("connected")),
            _fmt_rate(r.get("pickup")),
            _fmt_rate(r.get("engagement")),
            _fmt_rate(r.get("conclusion_rate")),
            _fmt_int(r.get("uploaded_leads")),
            _fmt_int(r.get("concluded_leads")),
        ]
        for r in snap.workflow_kpis
    ]
    caller_rows = [
        [
            escape(str(r.get("workflow_id"))),
            escape(str(r.get("caller_id"))),
            _fmt_int(r.get("dials")),
            _fmt_rate(r.get("pickup")),
            _fmt_rate(r.get("engagement")),
        ]
        for r in snap.caller_kpis
    ]
    signal_rows = [
        [
            escape(str(r.get("workflow_id"))),
            escape(str(r.get("signal_type"))),
            escape(str(r.get("severity"))),
            escape(str(r.get("caller_id") or "")),
            escape(str(r.get("message"))),
            _fmt_rate(r.get("baseline_value")),
            _fmt_rate(r.get("recent_value")),
        ]
        for r in snap.signals
    ]
    cohort_rows = [
        [escape(str(r.get("workflow_id"))), escape(str(r.get("cohort_id"))), _fmt_int(r.get("leads"))]
        for r in snap.cohort_summary
    ]
    config_rows = [
        [
            _fmt_int(r.get("event_id")),
            escape(str(r.get("workflow_id"))),
            _fmt_int(r.get("version")),
            escape(str(r.get("actor"))),
            escape(str(r.get("changed_dims_json"))),
            escape(str(r.get("ts") or "")),
        ]
        for r in snap.config_changes
    ]
    approval_rows = [
        [
            escape(str(r.get("approval_id"))),
            escape(str(r.get("workflow_id"))),
            escape(str(r.get("status"))),
            escape(str(r.get("reasons_json"))),
            escape(str(r.get("ts") or "")),
        ]
        for r in snap.approval_decisions
    ]
    issue_rows = [
        [
            _fmt_int(r.get("chunk")),
            _fmt_int(r.get("line_no")),
            escape(str(r.get("reason"))),
            escape(str(r.get("raw_line"))[:120]),
        ]
        for r in snap.ingest_issues
    ]
    unimplemented_rows = [
        [escape(u["key"]), escape(u["detail"]), "<span class='badge'>not implemented</span>"]
        for u in snap.unimplemented
    ]
    checkpoint_rows = [
        [escape(str(r.get("kind"))), _fmt_int(r.get("last_chunk")), _fmt_int(r.get("last_event_id"))]
        for r in snap.checkpoints
    ]

    experiment_rows = [
        [
            escape(str(r.get("experiment_id"))),
            escape(str(r.get("workflow_id"))),
            escape(str(r.get("cohort_id"))),
            escape(str(r.get("dimension"))),
            escape(str(r.get("status"))),
            escape(str(r.get("decision") or "")),
            escape(str(r.get("control_variant"))),
            escape(str(r.get("winner_variant") or "-")),
            escape(str(r.get("promotion_status") or "-")),
            _fmt_int(r.get("min_sample_per_arm")),
            escape(str(r.get("decision_reason") or "")[:160]),
        ]
        for r in snap.experiments
    ]
    arm_rows = [
        [
            escape(str(r.get("experiment_id"))),
            escape(str(r.get("variant"))),
            "yes" if r.get("is_control") else "no",
            _fmt_int(r.get("sample_n")),
            "yes" if r.get("sample_ok") else "no",
            _fmt_rate(r.get("primary_rate")),
            _fmt_rate(r.get("guardrail_rate")),
            escape(str(r.get("verdict") or "-")),
            _fmt_rate(r.get("effect_size")),
            "n/a" if r.get("p_value") is None else f"{float(r['p_value']):.4g}",
            "n/a" if r.get("adjusted_alpha") is None else f"{float(r['adjusted_alpha']):.4g}",
            escape(str(r.get("reason") or "")[:120]),
        ]
        for r in snap.experiment_arms
    ]
    if snap.experiments_available:
        if snap.experiments:
            experiments_block = f"""
      <p class="note">Experiment analysis is observational. Promotion status reflects Phase 6 governance when run.
        Status <code>running</code> or decision <code>underpowered</code> means insufficient samples — not a fabricated winner.</p>
      <h3>Experiment status</h3>
      {_rows(["id", "workflow", "cohort", "dim", "status", "decision", "control", "winner", "promotion", "min_n", "reason"], experiment_rows)}
      <h3>Variant arms</h3>
      {_rows(["experiment", "variant", "control", "n", "sample_ok", "primary", "guardrail", "verdict", "effect", "p", "alpha'", "reason"], arm_rows)}
"""
        else:
            experiments_block = """
      <p class="warn">Experiment tables are present, but no experiment rows exist yet. Insufficient data — nothing to show.</p>
"""
    else:
        experiments_block = """
      <p class="warn">Experiments unavailable. Phase 5 schema/evaluation has not been run against this database.</p>
"""

    incident_rows = [
        [
            escape(str(r.get("incident_id"))),
            escape(str(r.get("workflow_id"))),
            escape(str(r.get("signal_type"))),
            escape(str(r.get("severity"))),
            escape(str(r.get("status"))),
            escape(str(r.get("related_experiment_id") or "-")),
            escape(str(r.get("related_proposal_id") or "-")),
            escape(str(r.get("resolution") or "")[:120]),
            escape(str(r.get("message") or "")[:120]),
        ]
        for r in snap.incidents
    ]
    if snap.incidents_available:
        incidents_block = (
            f"""
      <p class="note">Incidents come from population detection signals. This dashboard never resolves them.</p>
      {_rows(["id", "workflow", "type", "severity", "status", "experiment", "proposal", "resolution", "message"], incident_rows)}
"""
            if snap.incidents
            else """<p class="warn">No incident rows. Empty means no signal-backed incidents yet — not a fabricated clear state.</p>"""
        )
    else:
        incidents_block = """<p class="warn">Incidents unavailable. Phase 6 schema has not been applied to this database.</p>"""

    proposal_rows = [
        [
            escape(str(r.get("proposal_id"))),
            escape(str(r.get("workflow_id"))),
            escape(str(r.get("status"))),
            escape(str(r.get("source"))),
            escape(str(r.get("route") or "-")),
            escape(",".join(r.get("changed_dims") or []) if isinstance(r.get("changed_dims"), list) else str(r.get("changed_dims") or "")),
            _fmt_rate(r.get("impact_score")),
            _fmt_int(r.get("affected_leads")),
            escape(str(r.get("approval_id") or "-")),
            escape(str(r.get("change_summary") or "")[:100]),
            escape(str((r.get("dry_run") or {}).get("limitation") if isinstance(r.get("dry_run"), dict) else "")[:80]),
        ]
        for r in snap.proposals
    ]
    if snap.proposals_available:
        proposals_block = (
            f"""
      <p class="note">Proposals include rationale, diffs, and dry-run estimates. Dashboard cannot submit or apply them.</p>
      {_rows(["id", "workflow", "status", "source", "route", "dims", "impact", "affected", "approval", "summary", "dry_run_note"], proposal_rows)}
"""
            if snap.proposals
            else """<p class="warn">No proposals stored yet.</p>"""
        )
    else:
        proposals_block = """<p class="warn">Proposals unavailable.</p>"""

    approval_rows = [
        [
            escape(str(r.get("approval_id"))),
            escape(str(r.get("proposal_id"))),
            escape(str(r.get("workflow_id"))),
            escape(str(r.get("status"))),
            _fmt_int(r.get("base_version")),
            escape(str(r.get("expires_at") or "-")),
            escape(str(r.get("apply_by") or "-")),
            escape(str(r.get("decided_at") or "-")),
            _fmt_int(r.get("applied_version")),
            escape(str(r.get("reasons") or "")[:120]),
        ]
        for r in snap.controller_approvals
    ]
    if snap.approvals_available:
        approvals_block = (
            f"""
      <p class="note">Controller-side approval tracking. Simulator event approvals are listed separately below.</p>
      {_rows(["approval", "proposal", "workflow", "status", "base", "expires", "apply_by", "decided", "applied_v", "reasons"], approval_rows)}
"""
            if snap.controller_approvals
            else """<p class="warn">No controller approvals tracked yet.</p>"""
        )
    else:
        approvals_block = """<p class="warn">Controller approvals unavailable.</p>"""

    audit_rows = [
        [
            escape(str(r.get("decision_id"))),
            escape(str(r.get("kind"))),
            escape(str(r.get("workflow_id") or "-")),
            escape(str(r.get("summary") or "")[:140]),
            escape(str(r.get("related_proposal_id") or "-")),
            escape(str(r.get("related_approval_id") or "-")),
            escape(str(r.get("wall_ts") or "")),
        ]
        for r in snap.audit_decisions
    ]
    if snap.audit_available:
        audit_block = (
            f"""
      <p class="note">Audit of controller decisions. Read-only; no actions from this UI.</p>
      {_rows(["id", "kind", "workflow", "summary", "proposal", "approval", "wall_ts"], audit_rows)}
"""
            if snap.audit_decisions
            else """<p class="warn">No audit rows yet.</p>"""
        )
    else:
        audit_block = """<p class="warn">Audit store unavailable.</p>"""

    llm_rows = [
        [
            escape(str(r.get("attempt_id"))),
            escape(str(r.get("incident_id"))),
            escape(str(r.get("workflow_id"))),
            escape(str(r.get("status"))),
            escape(str(r.get("selected_prompt_version") or "-")),
            escape(str(r.get("selected_validation_run_id") or "-")),
            escape(str(r.get("related_proposal_id") or "-")),
            escape(str(r.get("error_code") or "")),
            escape(str(r.get("problem") or "")[:100]),
        ]
        for r in snap.llm_attempts
    ]
    if snap.llm_attempts_available:
        llm_block = (
            f"""
      <p class="note">LLM suggestions are untrusted candidates. Selection uses validation thresholds, not the LLM.</p>
      {_rows(["attempt", "incident", "workflow", "status", "prompt", "validation", "proposal", "error", "problem"], llm_rows)}
"""
            if snap.llm_attempts
            else """<p class="warn">No LLM suggestion attempts stored yet.</p>"""
        )
    else:
        llm_block = """<p class="warn">LLM suggestion table unavailable.</p>"""

    validation_rows = [
        [
            escape(str(r.get("record_id"))),
            escape(str(r.get("attempt_id"))),
            escape(str(r.get("incident_id"))),
            escape(str(r.get("prompt_version"))),
            _fmt_int(r.get("n")),
            escape(str(r.get("validation_run_id") or "-")),
            _fmt_rate(r.get("pass_rate")),
            "yes" if r.get("meets_threshold") else "no",
            escape(str(r.get("status"))),
            escape(str(r.get("error_code") or "")),
        ]
        for r in snap.validation_records
    ]
    if snap.validation_available:
        validation_block = (
            f"""
      <p class="note">Validation measures safety only. Failed/invalid runs never become approved changes by themselves.</p>
      {_rows(["record", "attempt", "incident", "prompt", "n", "run_id", "pass_rate", "meets_min", "status", "error"], validation_rows)}
"""
            if snap.validation_records
            else """<p class="warn">No validation records stored yet.</p>"""
        )
    else:
        validation_block = """<p class="warn">Validation records unavailable.</p>"""

    sched_rows = [
        [
            escape(str(r.get("workflow_id"))),
            escape(str(r.get("tenant_id"))),
            _fmt_rate(r.get("weight")),
            _fmt_int(r.get("wait_ticks")),
            _fmt_int(r.get("actions_taken")),
            escape(str(r.get("last_action_kind") or "-")),
            _fmt_int(r.get("last_served_tick")),
        ]
        for r in snap.scheduler_state
    ]
    defer_rows = [
        [
            escape(str(r.get("defer_id"))),
            escape(str(r.get("workflow_id"))),
            escape(str(r.get("action_kind"))),
            _fmt_int(r.get("priority")),
            escape(str(r.get("reason") or "")[:140]),
            escape(str(r.get("status"))),
        ]
        for r in snap.deferred_actions
    ]
    alert_rows = [
        [
            escape(str(r.get("kind"))),
            escape(str(r.get("severity"))),
            escape(str(r.get("workflow_id") or "-")),
            escape(str(r.get("message") or "")[:160]),
            escape(str(r.get("created_at") or "")),
        ]
        for r in snap.operational_alerts
    ]
    counter_rows = [[escape(k), _fmt_rate(v) if isinstance(v, float) and v != int(v) else _fmt_int(v)] for k, v in sorted(snap.observability_counters.items())]
    if snap.fairness_available:
        cb = snap.credit_budget or {}
        fairness_block = f"""
      <p class="note">Fair scheduler + credit reserves. Dashboard is read-only and cannot spend credits or submit proposals.</p>
      <h3>Credit budget</h3>
      <p>sim_credits=<code>{escape(str(cb.get('last_sim_credits', 'n/a')))}</code>
         emergency_reserve=<code>{escape(str(cb.get('emergency_reserve', 'n/a')))}</code>
         experiment_spent=<code>{escape(str(cb.get('experiment_spent', 'n/a')))}</code>
         emergency_spent=<code>{escape(str(cb.get('emergency_spent', 'n/a')))}</code>
         experiment_cap=<code>{escape(str(cb.get('experiment_budget_cap', 'n/a')))}</code></p>
      <h3>Scheduler state</h3>
      {_rows(["workflow", "tenant", "weight", "wait", "actions", "last_kind", "last_tick"], sched_rows)}
      <h3>Deferred actions</h3>
      {_rows(["id", "workflow", "kind", "priority", "reason", "status"], defer_rows) if defer_rows else "<p class='warn'>No deferred actions.</p>"}
      <h3>Operational alerts</h3>
      {_rows(["kind", "severity", "workflow", "message", "created"], alert_rows) if alert_rows else "<p class='warn'>No alerts.</p>"}
      <h3>Counters</h3>
      {_rows(["name", "value"], counter_rows) if counter_rows else "<p class='warn'>No counters yet.</p>"}
"""
    else:
        fairness_block = """<p class="warn">Fairness/observability tables unavailable. Run Phase 8 controller once.</p>"""

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Controller monitor</title>
  <style>
    :root {{
      --bg: #f6f4ef;
      --ink: #1c1b19;
      --muted: #5c574f;
      --card: #fffdf8;
      --line: #d9d2c5;
      --ok: #1f6b4a;
      --warn: #8a5a00;
      --bad: #8b1e1e;
      --accent: #245b6b;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font: 15px/1.45 "Segoe UI", system-ui, sans-serif;
      color: var(--ink);
      background:
        radial-gradient(circle at top left, #efe7d8 0, transparent 40%),
        linear-gradient(180deg, #f8f5ee, var(--bg));
    }}
    header {{
      padding: 1.5rem 1.25rem 1rem;
      border-bottom: 1px solid var(--line);
      background: rgba(255,253,248,0.9);
    }}
    h1 {{ margin: 0 0 0.25rem; font-size: 1.6rem; letter-spacing: -0.02em; }}
    h2 {{ margin: 0 0 0.75rem; font-size: 1.05rem; }}
    .meta {{ color: var(--muted); }}
    main {{
      display: grid;
      gap: 1rem;
      padding: 1rem 1.25rem 2rem;
      max-width: 1200px;
    }}
    section {{
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 10px;
      padding: 1rem;
    }}
    .status-ok {{ color: var(--ok); font-weight: 600; }}
    .status-error {{ color: var(--bad); font-weight: 600; }}
    .warn {{ color: var(--warn); }}
    .ok {{ color: var(--ok); }}
    .badge {{
      display: inline-block;
      padding: 0.1rem 0.45rem;
      border-radius: 999px;
      background: #efe2c8;
      color: #6a4b10;
      font-size: 0.75rem;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 0.92rem;
    }}
    th, td {{
      text-align: left;
      padding: 0.4rem 0.45rem;
      border-bottom: 1px solid var(--line);
      vertical-align: top;
    }}
    th {{ color: var(--muted); font-weight: 600; }}
    .empty {{ color: var(--muted); font-style: italic; }}
    code {{ background: #efeae0; padding: 0.05rem 0.3rem; border-radius: 4px; }}
    ul {{ margin: 0.25rem 0 0; padding-left: 1.2rem; }}
    .note {{ color: var(--muted); font-size: 0.9rem; }}
  </style>
</head>
<body>
  <header>
    <h1>Controller monitor</h1>
    <p class="meta">Read-only view of controller SQLite. Never submits approvals or changes config.</p>
    <p>DB <code>{escape(snap.db_path)}</code> · schema v{_fmt_int(snap.schema_version)} ·
      status <span class="status-{status}">{status}</span> ·
      projection cursor <code>{_fmt_int(snap.projection_cursor)}</code></p>
  </header>
  <main>
    <section>
      <h2>Simulator</h2>
      {sim_block}
    </section>
    <section>
      <h2>Errors</h2>
      <ul>{errors}</ul>
      <h2>Warnings</h2>
      <ul>{warnings}</ul>
    </section>
    <section>
      <h2>Controller counts</h2>
      {_rows(["table / metric", "count"], count_rows)}
      <h2>Ingest checkpoints</h2>
      {_rows(["kind", "last_chunk", "last_event_id"], checkpoint_rows)}
    </section>
    <section>
      <h2>Workflow KPIs</h2>
      <p class="note">Pickup, engagement, and conclusion stay separate. <code>n/a</code> means no denominator, not zero.</p>
      {_rows(["workflow", "dials", "connected", "pickup", "engagement", "conclusion", "uploaded", "concluded"], kpi_rows)}
    </section>
    <section>
      <h2>Caller ID pickup</h2>
      {_rows(["workflow", "caller_id", "dials", "pickup", "engagement"], caller_rows)}
    </section>
    <section>
      <h2>Detected issues</h2>
      <p class="note">Population signals only. Empty can mean healthy history, or that an incident has not happened yet in simulated time.</p>
      {_rows(["workflow", "type", "severity", "caller_id", "message", "baseline", "recent"], signal_rows)}
    </section>
    <section>
      <h2>Cohorts</h2>
      {_rows(["workflow", "cohort_id", "leads"], cohort_rows)}
    </section>
    <section>
      <h2>Config changes seen in events</h2>
      <p class="note">From simulator <code>config_changed</code> events. Controller-authored proposals are not implemented yet.</p>
      {_rows(["event_id", "workflow", "version", "actor", "dims", "ts"], config_rows)}
    </section>
    <section>
      <h2>Approval decisions seen in events</h2>
      <p class="note">From simulator <code>approval_decided</code> events, not a controller proposal store.</p>
      {_rows(["approval_id", "workflow", "status", "reasons", "ts"], approval_rows)}
    </section>
    <section>
      <h2>Ingest issues</h2>
      {_rows(["chunk", "line", "reason", "raw"], issue_rows)}
    </section>
    <section>
      <h2>Experiments</h2>
      {experiments_block}
    </section>
    <section>
      <h2>Incidents</h2>
      {incidents_block}
    </section>
    <section>
      <h2>Proposals</h2>
      {proposals_block}
    </section>
    <section>
      <h2>Controller approvals</h2>
      {approvals_block}
    </section>
    <section>
      <h2>Audit decisions</h2>
      {audit_block}
    </section>
    <section>
      <h2>LLM suggestion attempts</h2>
      {llm_block}
    </section>
    <section>
      <h2>Validation runs</h2>
      {validation_block}
    </section>
    <section>
      <h2>Fairness &amp; observability</h2>
      {fairness_block}
    </section>
    <section>
      <h2>Not implemented yet</h2>
      {_rows(["area", "detail", "status"], unimplemented_rows)}
      <p class="note">Phase 9 grading/submission surfaces remain unavailable.</p>
    </section>
  </main>
</body>
</html>"""
