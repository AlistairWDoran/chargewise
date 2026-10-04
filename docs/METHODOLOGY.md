# ChargeWise — Cost & Savings Methodology

This document explains exactly how every figure ChargeWise shows is calculated, so you can trust the numbers and reproduce them yourself. It is the reference the cost engine (`backend/chargewise/engine/`) implements and tests against.

All money is in GBP. Sessions are priced with the VAT-inclusive unit rates Octopus publishes; the bill-reconciliation path (§6) derives the VAT-exclusive rate from them. One open question about VAT on the current tariff is recorded in §8. All times are stored in UTC and presented in `Europe/London`.

## 1. What counts as "running cost"

v1 tracks **charging energy only** — the electricity (home) and any public/Supercharging cost. It deliberately excludes insurance, servicing, tyres, depreciation and finance (these may come later). It also **excludes the daily standing charge**, because you pay that regardless of whether you own the car; it is not a cost of *charging*.

## 2. Home charging cost (Intelligent Octopus Go)

Home sessions are costed slot by slot, because the price changes within a session.

1. **Split** the session into half-hour settlement slots aligned to `:00` and `:30`. The first and last slots may be partial.
2. **Apportion energy** to each slot in proportion to its share of the session's duration. The energy is TeslaFi's figure for the session (energy drawn from the wall); the pipeline does not use half-hourly meter readings.
3. **Find the rate** for each slot. Three questions are answered in order, each for the instant the slot starts:
   1. **Which tariff agreement was in force?** Octopus's account record lists every tariff agreement the account has held, with its start and end. Zero-length agreements (left behind by tariff switches) are ignored.
   2. **What did that agreement's tariff charge at that moment?** The tariff's published unit rates are fetched for the agreement's dates and **clipped to them**, so a rate can never apply outside the agreement it came from. They are reduced to *rate periods* — a start, an end, an off-peak rate and a standard rate — with a new period wherever either rate changes (see "From published rates to rate periods" below).
   3. **Off-peak or standard?**
      - **Core window** — if the slot starts inside **23:30–05:30 local**, it is billed at the **off-peak** rate. On IOG the cheap rate applies to the whole home in this window.
      - **Smart dispatch** — else, if a stored Octopus smart-charge **dispatch** covers that instant (the car was being charged on the tariff's say-so, often in the daytime), it is billed at the **off-peak** rate. Every dispatch ChargeWise has ever seen is stored and used, because Octopus only serves recent ones. The dispatch's recorded location is not consulted.
      - **Standard** — otherwise it is billed at the **standard (peak)** rate.
4. **Slot cost** = slot energy (kWh) × applicable rate. **Session cost** = sum of slot costs.

### From published rates to rate periods

Octopus publishes rates in three shapes, and each is read by *when* a record applies, never by comparing one value with another:

- **Time-of-day tariff** (standard unit rates containing records shorter than 24 hours — IOG publishes one off-peak and one standard record per day): a record that starts inside 23:30–05:30 local sets the off-peak rate; any other record sets the standard rate. Each holds until the next record of the same kind.
- **Flat tariff** (every record runs for at least 24 hours, or is open-ended): off-peak and standard are both the record's value.
- **Day/night tariff** (nothing published under standard unit rates; day and night rates have separate histories): the night rate is the off-peak rate and the day rate is the standard rate. Only spans where both are published are covered.

For an agreement that is still open, already-published future rates are included, so a dated price change is picked up before it takes effect.

The periods from all agreements are then put in date order. ChargeWise does not guess when this goes wrong; the run stops and the failure is reported (`/api/status`, `last_error`):

- two periods overlap, so an instant would have two prices — `OctopusRatesOverlap`;
- an agreement has no usable published rates — `OctopusRatesUnavailable`, naming the tariff;
- no period covers a slot (a gap in the published rates) — "No rate period covers …".

Octopus's API returns unit rates in **pence**; they are converted to pounds once, on ingestion, and this is regression-tested. Example values (one region, June 2026): off-peak **£0.069/kWh**, standard **£0.303714/kWh**.

> Worked example: a 4 kWh session from 23:00–00:00 spans one standard-rate slot (23:00–23:30) and one core off-peak slot (23:30–00:00). Cost = 2 kWh × £0.303714 + 2 kWh × £0.069 = **£0.7454**.

## 3. Away / public charging cost

- **Superchargers** — prefer TeslaFi's **actual downloaded invoice cost** where available.
- **Other public charging** — use TeslaFi's recorded cost.
- **No cost recorded** — estimate as energy × a configurable away-rate, **clearly labelled as an estimate**.

TeslaFi's own default cost is energy × a single per-kWh rate set in TeslaFi, which is why home charging is always re-costed against your real Octopus rates rather than trusting that figure.

## 4. Mileage

Miles are derived from the odometer reading TeslaFi records on each charge. Each session is given the miles driven since the vehicle's previous recorded charge (this odometer reading minus the last one), so a period's mileage is the sum over its sessions. A vehicle's first session has no earlier reading and carries no miles. Combined totals span all vehicles owned since February 2022.

A vehicle is identified by its VIN. Its display name (the `VEHICLE_MAP` setting) is only a label: a vehicle stored with its VIN is found by that VIN whatever it is called, so renaming it, or running without the map, does not split its history or store its charges twice. The exception is a vehicle stored without a VIN (from a CSV import, say): if it later arrives through TeslaFi under a different name it is not recognised, and becomes a second vehicle.

## 5. Petrol-equivalent cost and savings

For each period:

- **Petrol cost** = (miles ÷ mpg) × 4.54609 litres/gallon × (GOV.UK weekly petrol price in p/L) ÷ 100.
- **mpg** defaults to **30** (configurable: set it to the petrol car you are comparing against).
- **Fuel price** is the GOV.UK weekly average for the matching week, so the comparison reflects real prices over the exact same period.
- **Saving** = petrol cost − electric cost.
- **Pence per mile** is reported for both electric and petrol for an easy headline comparison.

> Worked example: 1,000 miles at 30 mpg when petrol is 140 p/L → 33.33 gal × 4.54609 = 151.54 L × £1.40 = **£212.15**. If the electricity for those miles cost £30, the saving is **£182.15** (electric ≈ 3.0 p/mile vs petrol ≈ 21.2 p/mile).

## 6. Rounding

Internally the engine keeps full precision and rounds only for display/reconciliation. To match Octopus bills, the reconciliation path rounds half-hourly consumption to 0.01 kWh and applies Octopus's "unbiased" (round-half-to-even) rounding before summing, then adds VAT at the end.

## 7. Accuracy: what has been verified, and what has not

**Verified (4 October 2026, by agents separate from the authors):**

- **The rates.** For every half-hour from the first recorded charge in February 2022 to 4 October 2026 (over 81,000 instants), the rate the code selects was compared with Octopus's published record for the tariff agreement in force at that instant: 0 mismatches. Boundary, clock-change and 300,000 random-instant probes: 0 mismatches. The check can be re-run with `scripts/check-rates-against-octopus.py`, which uses Octopus's public rate endpoints.
- **The arithmetic.** All 4,485 home sessions in the live database, re-priced by the code, matched an independent slot-by-slot re-price to within £0.0001.
- **Stability.** A 60-day replay of the daily job caused no drift in miles, sessions or saving. After the first live run of the fixed code, no previously stored row had changed in any column.
- **The published figures.** Every field of the API's lifetime summary and status matched the live database.

**Not verified:**

- **Nothing has been reconciled against a real Octopus bill.** The checks above show that sessions are priced at the rates Octopus *publishes*. They do not show that those rates, applied to TeslaFi's energy figures, reproduce what Octopus *charged*. That needs a bill.
- The accuracy gate for this is the golden reconciliation test (`backend/tests/test_golden_reconciliation.py`, run as its own CI job): the computed cost for one bill month must come within **±2%** of the bill, with 1–2% the target. The test is built and passes on synthetic data, but its two real-bill fixtures are placeholders and are **skipped**. A real-bill fixture holds a household's consumption, so it is git-ignored and kept locally: the real reconciliation runs on the machine that holds the file, and CI runs the synthetic fixtures. It needs two numbers from one bill: the kWh billed, and the energy cost excluding the standing charge.

A caution from this project's own history: the figures published in July 2026 were described as verified, but those checks compared the arithmetic with itself. The rate being applied was wrong for four years of charging (an early, short-lived tariff's flat rate was used in place of the IOG rates), and no check that stayed inside the system could have seen it.

## 8. Known limitations

### Methodology limitations

- **Home cost is an upper bound, because dispatch history is missing.** ChargeWise stores smart-charge dispatches only from October 2026, and Octopus no longer serves the earlier ones. Daytime home charging before then cannot be matched to a dispatch and is priced at the standard rate; wherever it was in fact smart-dispatched, the true cost is lower. Energy inside 23:30–05:30 is unaffected. The error can only overstate cost and understate the saving. The same applies from now on to any dispatch that is missed, and whether one run a day catches every dispatch before Octopus stops serving it is not yet confirmed.
- **VAT on the current tariff is unresolved.** The day/night tariff now in force publishes identical inc-VAT and ex-VAT prices, and its opening prices × 1.05 equal the previous tariff's inc-VAT rates. ChargeWise prices with the published inc-VAT value, so if the bill adds 5% VAT on top, costs since the move to that tariff are understated by 5%. The reconciliation path (§6) also assumes ex-VAT = inc-VAT ÷ 1.05, which these published figures do not satisfy. To be settled against a bill.
- **The off-peak window is fixed at 23:30–05:30 local** for every tariff. For a day/night tariff this assumes the night rate applies in exactly that window; the published-rate check in §7 makes the same assumption, so only a bill can confirm it.
- **Per-slot energy is apportioned by duration**, as if the car drew power evenly from the start of the session to its end (TeslaFi's total session time, which includes any pauses in charging). Where a session straddles a rate boundary and the car in fact drew most of its energy on one side of it, the split between the two rates is wrong to that extent.
- **Not reconciled against a bill** — see §7.

### Data limitations (not methodology)

- **A gap in the charge history.** For a period of several months TeslaFi recorded no charging sessions, and none can be recovered. Mileage is measured between recorded charges (§4), so the first session after the gap carries all the miles driven during it, with no charging cost behind them. **The saving is therefore overstated** by the cost of the unrecorded electricity for those miles. Whether to estimate that electricity or to exclude those miles is **undecided**.
- **Energy comes from TeslaFi, not the meter.** Home sessions are priced on TeslaFi's wall-energy figure. Octopus half-hourly meter consumption is used only by the whole-house mode of the bill reconciliation test, not by the daily pipeline.
