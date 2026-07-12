# RESOLVED (12 Jul 2026) — no reply ever received; all questions answered empirically

TeslaFi support never replied to this email. Every open question below was settled by probing the API directly during the v1 build. The findings, which supersede the questions in the draft:

- **Endpoint:** `history.php?command=charges&dateFrom&dateTo` works and returns full history back to Feb 2022.
- **Timestamps:** `date` is UTC.
- **Date range:** `dateTo` is inclusive and capped at now; omitting both dates returns the full history.
- **Ordering:** results arrive **non-chronologically** — always sort client-side.
- **Partial responses:** under throttling, responses can be **truncated** — checksum `len(results)` against the payload `count` and retry on mismatch.
- **Rate limits:** roughly 30 rapid calls, then HTTP 429 — back off.
- **Other commands:** `command=chargeSummary` does not exist; `command=drives` returns 0 results (possibly token permissions).
- **Multi-vehicle:** the `vin` field on each record splits vehicles.
- **Fields:** `chargerKWH` = energy at the wall; `superCost`/`travelCost` carry away-charging costs.
- **Data gap:** the Jan–mid-May 2026 gap is a TeslaFi **subscription lapse**, not an API issue, and is unrecoverable from TeslaFi.

**Implementation:** `backend/chargewise/ingest/teslafi_history.py` (sorts, checksums, retries and backs off per the above).

The draft below is retained for the record only — do not send it.

---

# Draft email to TeslaFi support

**To:** TeslaFi support (via Help menu / https://support.teslafi.com/?show_helpdesk_form=true)
**From:** Alistair Doran (alistairdoran@hotmail.com)
**Subject:** Questions on the beta Charges/Drives history API (`history.php`) for a personal integration

---

Hello TeslaFi team,

Thank you for the data-logging service — it's excellent. I'm building a small personal, open-source tool that calculates the true cost of running my Tesla by combining my TeslaFi charge data with my Octopus Energy (Intelligent Octopus Go) tariff rates. I'd like to use the beta history API (`history.php?command=charges` and `command=drives`, with a Bearer token and `dateFrom`/`dateTo`) rather than manual CSV exports.

Before I build against it, could you help with a few specifics that aren't covered in the in-app notes?

1. **Field reference.** Is there a documented list of the JSON fields returned by `command=charges` and `command=drives`? In particular I'd like to confirm the field names/units for: charge start and end time, location type (home / travel / Supercharger), address or lat-long, kWh added, kWh used, charge cost and currency, battery start/end %, odometer, and charger voltage/amperage/power.

2. **Timestamps and timezone.** Are the times in the response in UTC or in my account's local time, and what format? (This matters for matching charges to half-hourly electricity rates across BST/GMT.)

3. **Multiple vehicles.** I have logged more than one Tesla over time on this account. Does `command=charges` return charges for all vehicles ever logged, or only the currently selected vehicle? Is there a parameter to select a vehicle, and is each charge tagged with a VIN or vehicle ID so I can attribute it correctly?

4. **History depth and pagination.** Using `dateFrom`/`dateTo`, will a single request return all matching records for a wide range (e.g. a whole year), or is there a maximum record count / pagination? What date-range chunk size do you recommend for pulling my full history (since February 2022)?

5. **Rate limits.** Are the `history.php` data endpoints subject to the monthly 500 command / 50 wake request limits, or are those limits only for vehicle commands/wakes? What polling frequency would you consider reasonable for ongoing, incremental data pulls (e.g. once or twice a day)?

6. **Supercharger / actual costs.** For Supercharger sessions where TeslaFi has downloaded the Tesla invoice, does the charges feed return the *actual* invoiced cost, or only the estimated cost based on my per-kWh setting? Is there a field that distinguishes estimated from actual cost?

7. **Beta stability.** As the endpoints are marked beta and "subject to change", is there a way to be notified of changes (a version field, changelog, or mailing list) so my integration doesn't silently break?

8. **Acceptable use.** Finally, is programmatic read-only access to my own data via this API, for a personal open-source project, within your acceptable-use terms?

Thank you very much for your help — and for building a genuinely great product.

Best regards,
Alistair Doran

---

*Note: do not paste your API token into this email. TeslaFi can identify your account from your login; the token should be kept private (and regenerated in Settings → API if it has been shared anywhere).*
