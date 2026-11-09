# Grade report — public_02_incidents

## Outcomes vs baseline (no-op controller)

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|---|
| ks_cancel | 0.259 | +4.4% | 0.181 | -1.7% | 0.507 | +6.3% | 34% | 84 (330) |
| nw_kyc | 0.357 | -4.7% | 0.238 | -1.7% | 0.573 | -3.2% | -46% | 42 (31) |
| nw_renewal | 0.434 | +1.8% | 0.265 | +1.1% | 0.551 | +0.6% | 10% | 57 (55) |
| ALL | 0.338 | +1.2% | 0.221 | -0.6% | 0.539 | +1.7% | 15% | 183 (416) |

## Changes applied by the controller

8 applied — harmful: 2, beneficial: 6

- nw_kyc v2 (controller_approved) dims=['welcome_message'] engageΔ=-0.040 didΔ=+0.000 → **harmful**
- ks_cancel v2 (controller_approved) dims=['welcome_message'] engageΔ=+0.016 didΔ=+0.000 → **beneficial**
- nw_renewal v2 (controller_approved) dims=['welcome_message'] engageΔ=+0.059 didΔ=+0.000 → **beneficial**
- nw_renewal v3 (controller_approved) dims=['welcome_message'] engageΔ=+0.036 didΔ=+0.000 → **beneficial**
- ks_cancel v3 (controller_approved) dims=['welcome_message'] engageΔ=+0.010 didΔ=+0.000 → **beneficial**
- ks_cancel v4 (controller_approved) dims=['welcome_message'] engageΔ=-0.010 didΔ=+0.000 → **harmful**
- ks_cancel v6 (controller_approved) dims=['prompt_version'] engageΔ=+0.102 didΔ=+0.000 → **beneficial**
- nw_renewal v4 (controller_approved) dims=['welcome_message'] engageΔ=+0.023 didΔ=+0.000 → **beneficial**

## Governance

```
{
  "approval_requests": 9,
  "decisions": {
    "approved": 8,
    "expired": 1
  },
  "rejection_reasons": {},
  "applied_via_approval": 8,
  "auto_applied": 0,
  "auto_apply_rejected": 0,
  "apply_conflicts": 0,
  "lapsed": 0,
  "peak_requests_per_24h": {
    "nw_renewal": 2,
    "ks_cancel": 2,
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
      "2": 0.493,
      "3": 0.513,
      "4": 0.49,
      "5": 0.498,
      "6": 0.503,
      "7": 0.511,
      "8": 0.517,
      "9": 0.509
    }
  },
  {
    "type": "supervisor_edit",
    "workflow": "ks_cancel",
    "at": "D3 11:30",
    "mttr_minutes": 1455,
    "fixed_by": "controller_approved"
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
    "2": 0.54,
    "3": 0.548,
    "4": 0.543,
    "5": 0.38,
    "6": 0.354,
    "7": 0.331,
    "8": 0.341,
    "9": 0.344
  }
}
```

Credits used: 9 · validation runs: 3 · LLM fix calls: 1