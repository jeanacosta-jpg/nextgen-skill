# Deleted rows and record lifecycle

The problem: **every NextGen list endpoint that carries `isDeleted` hides the rows where
it is `true`**. No warning, no error, just fewer rows. It hurts twice:

1. **Deletions never reach** the warehouse: a deleted appointment stays "alive" forever.
2. **Masters lose rows that facts point to**: measured 2026-08-20,
   `appointments.resource_ids` pointed to **65 resources** and
   `transactions.transaction_code_id` to **31 transaction codes** that the default view
   did not return → dangling keys.

Source: `glue/extractor.py` (docstring "Deleted rows", `INCLUDE_DELETED`,
`_pages_with_deleted`, `_appointments_deleted`) and `nextgen/catalog.py`
(`HIDES_DELETED` and friends).

`ngcat <cmd>` = `python <skill-dir>/scripts/ngcat.py <cmd>`. Paths like
`scripts/probe_lifecycle_flags.py` refer to the **nextgen-platform repo**.

---

## 1. What each endpoint does

`ngcat lifecycle [fragment]` prints it grouped. Summary:

### Measured: HIDE deleted rows (`hides`)

Measured 2026-08-20, "default view / explicit `isDeleted eq true`":

| Endpoint | Default / explicit | Pipeline table |
|---|---|---|
| `/appointments` | 0 / 868 | appointments |
| `/resources` | 0 / 65 | resources |
| `/master/transaction-codes` | 0 / 31 | transaction_codes |
| `/providers` | 0 / 16 | providers |
| `/master/payers` | 0 / 7 | payers |
| `/master/locations` | 0 / 4 | locations |
| `/master/events` | 0 / 2 | events |
| `/master/procedures` | 0 / 1 | service_items |
| `/persons/:personId/chart/procedures` | 0 of 104 active; 15 when asked for | patient_procedures |

Another test on 2026-08-20: of 1024 appointments for one day, 0 came with
`isDeleted=true`, and 2 ids deleted that day only showed up when requested by id.

### Not measured but with `isDeleted` (`probable`, ~109 GETs)

Examples: `/appointments/waitlist-items`, `/financial/batches`, `/master/diagnoses`,
`/users`, almost all of `master/*`, and per-person `chart/alerts`, `chart/medications`,
`chart/immunizations`, `chart/lab/orders`, `chart/problems`, `chart/clinical-notes`,
`persons/:personId/appointments`, `resources/:resourceId/appointments`…

**Treat them as `hides`** and confirm before a serious load (from the repo):

```bash
python scripts/probe_lifecycle_flags.py --paths master/diagnoses --explain
python scripts/probe_lifecycle_flags.py --paths master/diagnoses
python scripts/probe_lifecycle_flags.py --paths "persons/{personId}/chart/medications" --person-ids @sample.txt
```

Verdicts (printed in Spanish by the script): `OCULTA` (hides), `MUESTRA` (shows),
`MIXTO` (mixed), `NO FILTRABLE (HTTP 400)` (not filterable), `NO CONCLUYENTE`
(inconclusive: 0 deleted in the window → widen `--from/--to`). Once you measure one,
**record it in `nextgen/catalog.py`** (`HIDES_DELETED` / `SHOWS_DELETED`) and rebuild
this skill's index.

### No `isDeleted` (`none`): the deletion just disappears

`/financial/transactions`, `/persons`, `/persons/chart/charges`,
`/persons/charts/encounters`, and per-person `chart/charges`, `chart/encounters`,
`chart/assessments`, `chart/care-plan-assessments`, `chart/history-of-present-illness`,
`chart/medical-history`, `chart/physical-exam`, `chart/sticky-notes`.

No filter brings them back. Options, best to worst:

1. **Dedicated endpoint** if there is one (next section).
2. **Reconcile by id**: ids you have in the warehouse that no longer show up → `GET` the
   detail; a 404 confirms the deletion. Expensive (one call per id): do it on the
   suspicious set, not on everything.
3. **Compare universes**: for a parent (person) reloaded in full, whatever was there
   before and isn't now was deleted. That's what the pipeline's "full history per
   person" fan-out enables, but only if downstream does a *replace* per person
   (today it merges by PK and **keeps** what disappeared).

### Dedicated endpoint for deletions (`via`)

| Data | Deletions in | Notes |
|---|---|---|
| `chart/diagnoses` | `persons/:personId/chart/deleted-diagnoses` | same fields as diagnoses (id, diagnosisCodeId, icdCode, encounterId, modifyTimestamp…). **The pipeline doesn't use it yet.** |
| `chart/allergies` | `persons/:personId/chart/deleted-allergies` | same |
| `persons` | `persons/merged` | `dropPersonId` (disappears) → `keepPersonId` (survives). Has a **7-day window**: filter by `createTimestamp`/`modifyTimestamp`. |

A "deleted" person in NextGen is almost always a **merge**: its records move to
`keepPersonId`. To reconcile persons and everything per-person, load `persons/merged`
month by month and remap `dropPersonId → keepPersonId`.

### Return deletions without being asked (`shows`)

`/persons/:personId/insurances`: returns deactivated coverages; the state is in
**`isActive`** (and `expirationDate` / `policyExpirationDate`). No second pass.

### Detail by id (`detail`)

`GET /appointments/{appointmentId}` returns the appointment **even if deleted**, with
`isDeleted=true` (measured). For other details this is assumed by analogy.
`scripts/appointment_show.py <id>` is the viewer (`--mask` for sharing).

---

## 2. How to request deleted rows

### Rule 0: don't pin `isDeleted` in the base filter

`isDeleted eq false` is **redundant** (the endpoint already hides them) and
**contradicts** the second pass (`isDeleted eq false and isDeleted eq true` → 0). The
Glue extractor had exactly this bug in `locations`. `extract.with_deleted()` rejects it
with `ValueError`. (Old examples in `README.md`/`nextgen/__init__.py` with
`where("isDeleted=false")` are harmless without a second pass, but don't copy them.)

### Case A — "everything in the range, including deleted"

Same filter + `isDeleted eq true`, join by `id`:

```python
rows = extract.with_deleted(ng, "master/locations", params=params("isSchedulable eq true"))
rows = extract.fetch_monthly(ng, "appointments", "appointmentDate", "2025-01-01", "2025-12-31",
                             include_deleted=True, extra_params={"$expand": "Location"})
```

Cost: ×2 calls. Use it in full loads and for masters (which must **always** include
deleted rows, or facts end up with dangling keys).

### Case B — "what changed/was deleted since X" (incremental)

The deleted pass goes **only by `modifyTimestamp`**, never by the business date:
deleting a record stamps `modifyTimestamp`. If you reuse it with `appointmentDate`,
deleting today an appointment dated outside the range **never arrives**. Measured
2026-09-25: `modifyTimestamp ge … and isDeleted eq true` without `appointmentDate`
returned the same 91 rows as with the range, with appointment dates from 2026-07 to 2027-08.

```python
active = ng.get_all("appointments", params={"$filter": F.all_of(
    F.between("appointmentDate", "2024-01-01", horizon),   # safety net
    F.since(since))})
deleted = extract.fetch_deleted(ng, "appointments", since=since)   # None = all windows failed
rows, n_deleted = extract.merge_by_id(active, deleted or [])
```

- Full load of deletions (no `since`): `fetch_deleted(ng, path, start="2024-01-01")`
  splits by month with `F.half_open` (**exclusive** upper bound: timestamps carry
  milliseconds and `le …T23:59:59` loses the last second).
- `deleted_windows()` returns the plan without network → use it in `--explain`.
- Reference implementation: `scripts/appointments_modified_on_day.py`.

### Case C — per-person

```python
procs = extract.fan_out(ng, pids, "persons/{personId}/chart/procedures", include_deleted=True)
```

One extra call **per person** (there's no cross-person procedures index). In Glue it
has its own switch: `--include_deleted_per_person false`. Enable it only on
`hides`/`probable` paths.

### Join by id

`merge_by_id(active, extra)`: adds from `extra` only the ids not in `active`. If an id
comes in both, the active one wins (it was read mid-change). Downstream, within a batch
`transform.dedup()` sorts by `modify_timestamp` descending and keeps the most recent
version per PK; whether the deletion reaches the warehouse depends on the Load layer
upserting by PK with that row (`is_deleted=true`). Downstream must filter
`is_deleted = false` for "current".

### Deleted-pass failures

`with_deleted` / `fan_out` log the failure and return the active rows; `fetch_deleted`
returns `None` if **all** windows fail. Distinguish "0 deletions" from "deletions
unknown" and say so in `report()`. In Glue: `DELETED_RECOVERED` / `DELETED_FAILED` in
the summary, and "0 deleted in the whole run" is a **WARNING** (measured: there are hundreds).

---

## 3. States that are NOT deletions

They come in the default view; filter them with a normal `$filter` or client-side.
`ngcat show <path>` lists them under `LIFECYCLE:`.

| Field | Where | Meaning / caveat |
|---|---|---|
| `isCancelled` | appointments (+ detail with `cancelReasonId`) | cancelled, **not** deleted |
| `isNoShow` | appointments | patient didn't come |
| `isKept` | appointments | appointment was kept |
| `isRescheduled` + `rescheduledAppointmentId` | appointments | rescheduled: the new appointment is another row; link through `rescheduledAppointmentId` (+ `rescheduleReasonId` in detail) |
| `appointmentConfirmed`, `workflowStatus`, `workflowRoom` | appointments | visit workflow |
| `isActive` | `persons/:id/insurances`, recall-plans, obstetrics/problems, vaccines | inactive ≠ deleted |
| `status` / `statusId` / `statusDescription` | charges, encounters, procedures, orders, waitlist… | endpoint-specific values; look at the distribution before filtering |
| `deleteReason`, `inactiveReason` | `chart/medications` | reason for removal/inactivation |
| `cancelReason`, `cancelledDate` | diagnostic/lab/immunization orders | cancelled order |
| `expirationDate`, `policyExpirationDate`, `payerExpirationDate` | masters, coverages | validity |
| `resolvedDate` | diagnoses, allergies | resolved problem (still in the chart) |

Before filtering by state, **ask what the request means**: "cancelled appointments"
may be `isCancelled eq true`, or also no-shows and reschedules; "active appointments"
may exclude deleted (default) but also cancelled.

```python
# 2025 cancelled appointments, including those later deleted
rows = extract.fetch_monthly(ng, "appointments", "appointmentDate", "2025-01-01", "2025-12-31",
                             base_filter="isCancelled eq true", include_deleted=True)
# no-shows for a set of locations
flt = F.all_of("isNoShow eq true", F.any_of("locationId", locs, kind="guid", dialect=dialect))
```

Booleans may arrive as `true`/`"TRUE"`/`1` depending on the source (API, CSV, Oracle):
use `extract.parse_bool()` client-side; `transform` casts `is_*` to boolean.

---

## 4. Checklist for any extraction

- [ ] `lifecycle <path>`: `hides`/`probable`? → do they want deletions, or is it incremental?
- [ ] base filter **without** `isDeleted`
- [ ] full → Case A · incremental → Case B (by `modifyTimestamp`, half-open)
- [ ] masters referenced by facts → **always** with deleted rows
- [ ] `--explain` shows both passes and their `$filter`
- [ ] `report()` counts active / deleted / states, and distinguishes "0" from "failed"
- [ ] `none`: tell the user — those deletions can't be fetched with a filter
