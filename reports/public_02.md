# Grade report — public_02_incidents

## Outcomes vs baseline (no-op controller)

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|---|
| ks_cancel | 0.245 | -1.1% | 0.182 | -0.9% | 0.478 | +0.3% | 8% | 334 (330) |
| nw_kyc | 0.358 | -4.4% | 0.239 | -1.4% | 0.582 | -1.7% | -46% | 25 (31) |
| nw_renewal | 0.424 | -0.6% | 0.262 | +0.1% | 0.545 | -0.5% | 2% | 56 (55) |
| ALL | 0.329 | -1.7% | 0.221 | -0.6% | 0.527 | -0.5% | -1% | 415 (416) |

## Changes applied by the controller

4 applied — harmful: 1, beneficial: 3

- nw_kyc v2 (controller_approved) dims=['welcome_message'] engageΔ=-0.040 didΔ=+0.000 → **harmful**
- ks_cancel v2 (controller_approved) dims=['welcome_message'] engageΔ=+0.016 didΔ=+0.000 → **beneficial**
- nw_renewal v2 (controller_approved) dims=['welcome_message'] engageΔ=+0.059 didΔ=+0.000 → **beneficial**
- nw_renewal v3 (controller_approved) dims=['welcome_message'] engageΔ=+0.036 didΔ=+0.000 → **beneficial**

## Governance

```
{
  "approval_requests": 10,
  "decisions": {
    "approved": 10,
    "expired": 0
  },
  "rejection_reasons": {},
  "applied_via_approval": 4,
  "auto_applied": 0,
  "auto_apply_rejected": 0,
  "apply_conflicts": 0,
  "lapsed": 6,
  "peak_requests_per_24h": {
    "nw_renewal": 2,
    "ks_cancel": 1,
    "nw_kyc": 1
  }
}
```

## Incidents

```
[
  {
    "type": "did_flagged",
    "workflow": "ks_cancel",
    "at": "D2 10:00",
    "flagged_did_share_by_day": {
      "2": 0.497,
      "3": 0.494,
      "4": 0.5,
      "5": 0.504,
      "6": 0.517,
      "7": 0.496,
      "8": 0.494,
      "9": 0.494
    }
  },
  {
    "type": "supervisor_edit",
    "workflow": "ks_cancel",
    "at": "D3 11:30",
    "mttr_minutes": null,
    "fixed_by": null
  },
  {
    "type": "supervisor_edit",
    "workflow": "nw_renewal",
    "at": "D5 15:00"
  }
]
```

## Decaying variants — share of in-population calls still on the variant, by day after onset

```
{
  "nw_renewal:welcome_message=W3": {
    "2": 0.535,
    "3": 0.561,
    "4": 0.544,
    "5": 0.551,
    "6": 0.554,
    "7": 0.554,
    "8": 0.548,
    "9": 0.561
  }
}
```

Credits used: 54 · validation runs: 18 · LLM fix calls: 6