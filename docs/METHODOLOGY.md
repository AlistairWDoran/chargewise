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

The rate values for a slot are resolved from the tariff's **rate history** valid at that moment, so sessions are priced at the rate that applied at the time — even across tariff changes. One known approximation applies within long-running tariff agreements: see §8. Example real values (region H, captured 2026-06-15): off-peak **£0.069/kWh**, peak **£0.303714/kWh**. (Octopus's API returns unit rates in **pence**; the engine converts to pounds on ingestion — this is regression-tested.)

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

The headline accuracy gate: computed home-charging cost should reconcile to within ~1–2% of the corresponding Octopus bill for a test month. This will be enforced by a golden reconciliation test in CI; **as of July 2026 that test has not yet been built** and remains the next accuracy milestone. Until it lands, the figures should be read alongside the limitations in §8 — the dispatch-feed limitation errs conservative (home costs overstated, savings understated); the rate-era collapse can err in either direction.

## 8. Known limitations

### Methodology limitations

- Per-slot energy is apportioned by duration unless half-hourly consumption is available; this introduces a small error only at slot boundaries.
- Octopus smart-meter data can lag or have gaps; TeslaFi energy is the primary source, Octopus consumption a cross-check.
- IOG dispatch attribution uses the Octopus dispatch feed; if a dispatch is missing, that slot falls back to the standard rate (conservative).
- **The dispatch feed only covers a recent window.** Octopus exposes only the most recent smart-charge dispatches, so *historical* daytime smart charges cannot be attributed to a dispatch and are billed at the peak rate instead of off-peak. Directionally this **overstates home costs and therefore understates the saving** — the error is conservative, never flattering.
- **Rate eras are collapsed within each tariff agreement.** `derive_iog_rate_periods` reduces each tariff agreement to a single off-peak/peak pair, taking the minimum observed rate as off-peak and the maximum as peak. Where rates changed *within* an agreement — notably the 2022–24 variable era — the applied rates are approximate: peak slots are priced at the highest rate of the era (overstating their cost), while off-peak slots are priced at the lowest (understating theirs where the off-peak rate rose within an agreement). **The net direction of this approximation is not guaranteed.** Per-era rate refinement is a planned improvement.

### Data limitations (not methodology)

- **TeslaFi gap, January to mid-May 2026.** The TeslaFi subscription lapsed for this period, so no charging sessions were recorded and none appear in the totals. This is a gap in the source data, not an error in the calculation, and it is unrecoverable from TeslaFi.
