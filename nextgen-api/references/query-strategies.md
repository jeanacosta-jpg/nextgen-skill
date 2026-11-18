# Query strategies

The three basic shapes and the variants the production pipeline (`glue/extractor.py`)
has already solved. Before inventing a strategy, look here for the one that fits.

---

## Basic shapes

| Shape | When | Calls | Function |
|---|---|---|---|
| **List** | path without `:param`, no window trap | 1 + pages | `ng.get_all(path, params=…)` |
| **Monthly list** | path with a 7-day window | months × (1 + pages) | `extract.fetch_monthly(...)` |
| **Fan-out** | path with `:personId` / `:encounterId` | **1+ per parent** | `extract.fan_out(ng, parents, template)` |

Estimate for `--explain`: one year by month = 12 paged queries; a fan-out over 20,000
persons = 20,000 calls (×2 with `include_deleted`). With 10 workers and ~0.3 s per call,
20,000 calls ≈ 10 min if nothing fails. Warn the user above ~50 calls.
`ngcat chain <path>` shows the dependency levels that multiply the cost.

---

## S1. Full load split by month

```python
rows = extract.fetch_monthly(ng, "appointments", "appointmentDate", "2024-01-01", until,
                             base_filter=extra_flt, extra_params={"$expand": "Location"})
```

- `month_ranges()` keeps partial months at both ends (03/15 → starts on the 15th).
- `F.between` closes at `23:59:59`. That's correct for **business dates**
  (appointmentDate, transactionDate). For **millisecond timestamps** (modifyTimestamp)
  use `F.half_open` or `extract.deleted_windows()` (exclusive bound).
- 1 s pause between months (`sleep_s`); the pipeline uses 2 s for appointments.

## S2. Incremental with bookmark

```python
since = extract.resolve_since("my_table")            # None -> full load
rows = ng.get_all(path, params=params(F.since(since))) if since else extract.fetch_monthly(...)
extract.write(rows, "my_table")
extract.write_bookmark("my_table", extract.max_timestamp(rows))   # only if the load was complete
```

- `resolve_since` subtracts a **1 h overlap** (records written while the previous run ran).
- **Don't advance the bookmark** after a `--limit` or with a closed window (`--until`):
  the next run would skip records.
- If the endpoint hides deleted rows: add pass **B** from
  [deleted-and-lifecycle.md](deleted-and-lifecycle.md) (by `modifyTimestamp`).
- `modifyTimestamp` in `$filter` also lifts the 7-day window on `/appointments`
  (measured 2026-08-21: 1867 identical rows with and without an `appointmentDate`
  range). The pipeline keeps the range as a safety net: it costs the same.

## S3. Future horizon (appointments)

Appointments live in the future. A range ending "today" loses everything booked from
tomorrow on. Pipeline: `appointmentDate le today + APPT_FORWARD_DAYS` (365).

```python
horizon = (datetime.now(timezone.utc) + timedelta(days=365)).strftime("%Y-%m-%d")
```

## S4. Fallback when the incremental returns 0 (transactions)

`extract_transactions_incremental`: if `modifyTimestamp ge since` returns 0 rows, it
retries with `transactionDate` from `since − 7 days` to today. Useful pattern when a
modification field is unreliable on a given endpoint.

## S5. Parent discovery through a cross-person index

Per-person endpoints don't accept a date filter → the incremental logic is applied when
**choosing whom to walk**, and each person's full history is fetched.

| To extract | Index that discovers persons | Field |
|---|---|---|
| charges, charge_icd_codes | `persons/chart/charges` | `modifyTimestamp` |
| encounters, patient_procedures, encounter_insurances | `persons/charts/encounters` | `modifyTimestamp` |
| persons, coverages, clinical tables, person_chart | `persons` | `modifyTimestamp` |

```python
pids = extract.discover_person_ids(ng, since="2026-08-01")          # /persons
idx  = ng.get_all("persons/chart/charges", params=params(F.since(since)))
pids = list({extract.safe_id(r.get("personId")) for r in idx} - {None})
```

Without `since`, all three indexes have a **7-day window**: full load = month by month
from 2024-01-01 (32+ windows, ~131k persons in production → doesn't fit a 60-min job).
In Glue, without a bookmark a bounded window is used (`CHART_FALLBACK_DAYS = 7`);
the total crawl only with `--full_reload`.

**Historical anti-pattern**: deriving persons from appointments (it missed anyone with
procedures/coverages but no appointment in the window). Only `insurances` still works that way.

## S6. Multi-level fan-out

`fan_out` accepts bare ids (→ `personId`) or **dicts**, whose fields fill the template
(`str.format`). Chain levels without new code:

```python
encs = extract.fan_out(ng, pids, "persons/{personId}/chart/encounters")
ins  = extract.fan_out(ng, encs, "persons/{personId}/chart/encounters/{id}/insurances")
```

- 404 = "this parent has no records": zero rows, not an error.
- It now pages each parent until a short page (it used to stop at the first 100).
- `mode="singleton"` for objects (`persons/{personId}/chart`).
- `drop_tenant=True` by default; `flatten=True` flattens one level.
- A failure in one parent is logged and doesn't kill the batch (`ng.map` + `_safe`).
- (person, encounter) pairs in Glue: from the `persons/charts/encounters` index, using
  `id` or `encounterId`.

## S7. List + detail (fields the list omits)

`/persons/{id}/insurances` doesn't return `policyNumber`;
`/persons/{id}/insurances/{personPayerId}` does. `_fetch_coverages` pattern: list → GET
detail per element → `merged = {**list, **detail}` (detail wins, list-only fields like
`relationship` are kept). Cost: 1 + N calls per person.

When a field "doesn't show up", compare `ngcat show <list>` with `ngcat show <detail>`:
details usually have many more fields (encounters 26 → 63, diagnoses 32 → 47).

## S8. Melting repeated columns (charge ICDs)

A charge carries `icdCodeId`, `icdCodeId2` … `icdCodeId12`. `_fetch_charge_icds` emits
one row per non-empty (charge, position) (`is_blank_id` discards `""` and `"0"`) with
`icdPosition`, `icdField`, `diagnosisCodeId`. PK `(charge_id, icd_position)`.

## S9. Resolving a catalog from referenced ids (batched OR)

Instead of downloading all of `/master/diagnoses`, resolve only the ids in use:

```python
ids = sorted({extract.safe_id(c) for c in code_ids if not extract.is_blank_id(c)})
for batch in (ids[i:i + 10] for i in range(0, len(ids), 10)):
    # By hand and quoted, like the pipeline: F.any_of in auto mode would leave a numeric
    # id unquoted, and kind="raw" never quotes.
    flt = " or ".join(f"diagnosisCodeId eq '{c}'" for c in batch)
    rows += ng.get_all("master/diagnoses", params={"$filter": flt})
```

Batches of 10 (`--diag_batch`): a huge OR blows up the URL. Parallelize batches with
`ng.map`. Deduplicate by the catalog key.

## S10. Always-full tables (`ALWAYS_FULL`)

`practices`, `payers` (no filter), `resources` (400 on `modifyTimestamp`), `events`
(no timestamp). They fetch the whole catalog every time; in Glue the bookmark only
decides whether it's due today (`full_load_due`: runs if the bookmark is from an
earlier day, UTC). They're small: **always with the deleted pass**.

## S11. GUID filter with probing and fallback

`appointments_by_location.py`: `detect_guid_dialect` once → server-side filter with the
right dialect; if `"none"`, fetch the range and filter client-side comparing lowercase.
Several locations: `F.any_of(..., kind="guid", dialect=d)` (parenthesized OR; there's no `in`).

## S12. Fetching one specific record

`ng.get_one("appointments/{id}")` (404 → `None`). Useful to reconcile ids suspected of
deletion (details do return deleted rows) — `appointment_show.py`.

---

## Choosing a strategy: guiding questions

1. What's the **universe**? (all appointments in a range, those modified since X, those
   of some persons…) → decides the shape and the window field.
2. Is the request's field a **business date** or a **modification date**? Don't mix
   them: "January appointments" ≠ "appointments changed in January".
3. Is there a **window trap**? → always an explicit range.
4. Are there **hidden deleted rows** and do they want them / is it incremental? → second pass.
5. How many **calls**? → say it in `--explain` and bound with `since`/`--limit`.
6. Is some field only in the **detail**? → S7, and multiply the calls.
