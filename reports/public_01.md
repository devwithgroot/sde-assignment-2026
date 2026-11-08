# Grade report — public_01_dev

## Outcomes vs baseline (no-op controller)

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|---|
| ks_cancel | 0.342 | +2.7% | 0.244 | +2.2% | 0.491 | -0.0% | -16% | 64 (47) |
| nw_kyc | 0.367 | +0.9% | 0.237 | -2.2% | 0.610 | +3.3% | 16% | 15 (21) |
| nw_renewal | 0.424 | +1.0% | 0.263 | -0.1% | 0.556 | +2.0% | 12% | 57 (42) |
| ALL | 0.374 | +1.6% | 0.249 | +0.5% | 0.536 | +1.3% | 2% | 136 (110) |

## Changes applied by the controller

4 applied — beneficial: 3, harmful: 1

- nw_kyc v2 (controller_approved) dims=['welcome_message'] engageΔ=+0.039 didΔ=+0.000 → **beneficial**
- ks_cancel v2 (controller_approved) dims=['welcome_message'] engageΔ=-0.023 didΔ=+0.000 → **harmful**
- nw_renewal v2 (controller_approved) dims=['welcome_message'] engageΔ=+0.066 didΔ=+0.000 → **beneficial**
- nw_renewal v3 (controller_approved) dims=['welcome_message'] engageΔ=+0.014 didΔ=+0.000 → **beneficial**

## Governance

```
{
  "approval_requests": 4,
  "decisions": {
    "approved": 4,
    "expired": 0
  },
  "rejection_reasons": {},
  "applied_via_approval": 4,
  "auto_applied": 0,
  "auto_apply_rejected": 0,
  "apply_conflicts": 0,
  "lapsed": 0,
  "peak_requests_per_24h": {
    "nw_renewal": 1,
    "ks_cancel": 1,
    "nw_kyc": 1
  }
}
```

## Incidents

```
[]
```

## Decaying variants — share of in-population calls still on the variant, by day after onset

```
{
  "nw_renewal:welcome_message=W3": {
    "3": 0.557,
    "4": 0.559,
    "5": 0.559,
    "6": 0.558,
    "7": 0.541
  }
}
```

Credits used: 0 · validation runs: 0 · LLM fix calls: 0