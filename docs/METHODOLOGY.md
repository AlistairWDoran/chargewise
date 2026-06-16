# ChargeWise — Cost & Savings Methodology

This document explains exactly how every figure ChargeWise shows is calculated, so you can trust the numbers and reproduce them yourself. It is the reference the cost engine (`backend/chargewise/engine/`) implements and tests against.

All money is in GBP. Electricity rates are VAT-inclusive for display; VAT-exclusive values are retained internally for reconciling to Octopus bills. All times are stored in UTC and presented in `Europe/London`.

## 1. What counts as "running cost"

v1 tracks **charging energy only** — the electricity (home) and any public/Supercharging cost. It deliberately excludes insurance, servicing, tyres, depreciation and finance (these may come later). It also **excludes the daily standing charge**, because you pay that regardless of whether you own the car; it is not a cost of *charging*.

## 2. Home charging cost (Intelligent Octopus Go)

Home sessions are costed slot-by-slot, because IOG pricing changes within a session.

1. **Split** the session into half-hour settlement slots aligned to `:00` and `:30`. The first and last slots may be partial.
2. **Apportion energy** to each slot in proportion to its share of the session's duration. (Where Octopus half-hourly consumption is available, it is used as a cross-check; material divergences are flagged.)
3. **Choose the rate** for each slot:
   - **Core window** — if the slot falls inside **23:30–05:30 local**, it is billed at the **off-peak** rate. On IOG this cheap rate applies to the whole home in this window.
   - **Smart dispatch** — else, if the slot is covered by an Octopus smart-charge **dispatch** (the car was being charged on the tariff's say-so, often in the daytime), it is billed at the **off-peak** rate.
   - **Standard** — otherwise it is billed at the **peak/standard** rate.
4. **Slot cost** = slot energy (kWh) × applicable rate. **Session cost** = sum of slot costs.

The rate values for a slot are resolved from the tariff's **rate history** valid at that moment, so sessions are always priced at the rate that actually applied — even after tariff changes. Example real values (region H, captured 2026-06-15): off-peak **£0.069/kWh**, peak **£0.303714/kWh**.

> Worked example: a 4 kWh session from 23:00–00:00 spans one peak slot (23:00–23:30) and one core off-peak slot (23:30–00:00). Cost = 2 kWh × £0.303714 + 2 kWh × £0.069 = **£0.7454**.

## 3. Away / public charging cost

- **Superchargers** — prefer TeslaFi's **actual downloaded invoice cost** where available.
- **Other public charging** — use TeslaFi's recorded cost.
- **No cost recorded** — estimate as energy × a configurable away-rate, **clearly labelled as an estimate**.

TeslaFi's own default cost is energy × a single per-kWh rate set in TeslaFi, which is why home charging is always re-costed against your real Octopus rates rather than trusting that figure.

## 4. Mileage

Miles for a period are derived from odometer readings (TeslaFi records the odometer on charges and drives), taking the difference between the period's start and end. Combined totals span all vehicles owned since February 2022.

## 5. Petrol-equivalent cost and savings

For each period:

- **Petrol cost** = (miles ÷ mpg) × 4.54609 litres/gallon × (GOV.UK weekly petrol price in p/L) ÷ 100.
- **mpg** defaults to **30** (configurable; matches Alistair's prior petrol car at ~29–32 mpg).
- **Fuel price** is the GOV.UK weekly average for the matching week, so the comparison reflects real prices over the exact same period.
- **Saving** = petrol cost − electric cost.
- **Pence per mile** is reported for both electric and petrol for an easy headline comparison.

> Worked example: 1,000 miles at 30 mpg when petrol is 140 p/L → 33.33 gal × 4.54609 = 151.54 L × £1.40 = **£212.15**. If the electricity for those miles cost £30, the saving is **£182.15** (electric ≈ 3.0 p/mile vs petrol ≈ 21.2 p/mile).

## 6. Rounding

Internally the engine keeps full precision and rounds only for display/reconciliation. To match Octopus bills, the reconciliation path rounds half-hourly consumption to 0.01 kWh and applies Octopus's "unbiased" (round-half-to-even) rounding before summing, then adds VAT at the end.

## 7. Accuracy target

The headline accuracy gate: computed home-charging cost should reconcile to within ~1–2% of the corresponding Octopus bill for a test month. This is enforced by a golden reconciliation test in CI.

## 8. Known limitations

- Per-slot energy is apportioned by duration unless half-hourly consumption is available; this introduces a small error only at slot boundaries.
- Octopus smart-meter data can lag or have gaps; TeslaFi energy is the primary source, Octopus consumption a cross-check.
- IOG dispatch attribution uses the Octopus dispatch feed; if a dispatch is missing, that slot falls back to the standard rate (conservative).
