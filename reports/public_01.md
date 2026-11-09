# Grade report — public_01_dev

## Outcomes vs baseline (no-op controller)

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|---|
| ks_cancel | 0.338 | +1.5% | 0.242 | +1.6% | 0.486 | -1.0% | -15% | 53 (47) |
| nw_kyc | 0.376 | +3.5% | 0.241 | -0.2% | 0.614 | +3.9% | 16% | 18 (21) |
| nw_renewal | 0.430 | +2.4% | 0.261 | -0.9% | 0.564 | +3.4% | 18% | 31 (42) |
| ALL | 0.377 | +2.2% | 0.249 | +0.3% | 0.538 | +1.6% | 4% | 102 (110) |

## Changes applied by the controller

4 applied — beneficial: 2, harmful: 2

- nw_kyc v2 (controller_approved) dims=['welcome_message'] engageΔ=+0.039 didΔ=+0.000 → **beneficial**
- ks_cancel v2 (controller_approved) dims=['welcome_message'] engageΔ=-0.023 didΔ=+0.000 → **harmful**
- nw_renewal v2 (controller_approved) dims=['welcome_message'] engageΔ=+0.066 didΔ=+0.000 → **beneficial**
- nw_renewal v3 (controller_approved) dims=['welcome_message'] engageΔ=-0.036 didΔ=+0.000 → **harmful**

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
    "nw_renewal": 2,
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
    "3": 0.0,
    "4": 0.0,
    "5": 0.0,
    "6": 0.0,
    "7": 0.0
  }
}
```

Credits used: 0 · validation runs: 0 · LLM fix calls: 0