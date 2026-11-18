# Recipes

Snippets ready for an `extract_*()` function in a script under the repo's `scripts/`.
All of them assume:

```python
from nextgen import NextGenClient, extract
from nextgen.odata import F, detect_guid_dialect, params, where

ng = NextGenClient.from_env()
```

None of them prints rows: in the script, pass them to `report()` (counts) and to
`extract.write()`.

---

### 1. Full master, with deleted rows

```python
locs = extract.prune_all(extract.with_deleted(ng, "master/locations"))
deleted = sum(1 for r in locs if extract.parse_bool(r.get("isDeleted")))
```

### 2. Appointments for a range and some locations, with deleted rows

```python
dialect = detect_guid_dialect(ng, "appointments", "locationId", locs[0])
base = F.any_of("locationId", locs, kind="guid", dialect=dialect) if dialect != "none" else ""
rows = extract.fetch_monthly(ng, "appointments", "appointmentDate", "2025-01-01", "2025-12-31",
                             base_filter=base, include_deleted=True,
                             extra_params={"$expand": "Location"})
if dialect == "none":
    wanted = {l.lower() for l in locs}
    rows = [r for r in rows if str(r.get("locationId", "")).lower() in wanted]
```

### 3. Appointments changed or deleted since the last bookmark

Already exists: `scripts/appointments_modified_on_day.py --bookmark`. The core:

```python
since = extract.resolve_since("appointments_modified")
horizon = (datetime.now(timezone.utc) + timedelta(days=365)).strftime("%Y-%m-%d")
active = ng.get_all("appointments", params={"$filter": F.all_of(
    F.between("appointmentDate", "2024-01-01", horizon), F.since(since))})
deleted = extract.fetch_deleted(ng, "appointments", since=since)
rows, n_del = extract.merge_by_id(active, deleted or [])
```

### 4. Only states: cancelled, no-show, rescheduled

```python
cancel = extract.fetch_monthly(ng, "appointments", "appointmentDate", start, end,
                               base_filter="isCancelled eq true")
noshow = extract.fetch_monthly(ng, "appointments", "appointmentDate", start, end,
                               base_filter="isNoShow eq true")
# or a single load classified client-side:
for r in rows:
    state = ("deleted" if extract.parse_bool(r.get("isDeleted")) else
             "cancelled" if extract.parse_bool(r.get("isCancelled")) else
             "no-show" if extract.parse_bool(r.get("isNoShow")) else
             "rescheduled" if extract.parse_bool(r.get("isRescheduled")) else
             "kept" if extract.parse_bool(r.get("isKept")) else "pending")
```

One load + client-side classification costs fewer calls than N filtered loads when they
want several states.

### 5. Incremental per-person fan-out

```python
pids = extract.discover_person_ids(ng, since="2026-08-01T00:00:00")
dx   = extract.fan_out(ng, pids, "persons/{personId}/chart/diagnoses")
```

### 6. Fan-out with deleted rows (procedures)

```python
idx  = ng.get_all("persons/charts/encounters", params=params(F.since(since)))
pids = list({extract.safe_id(r.get("personId")) for r in idx} - {None})
procs = extract.fan_out(ng, pids, "persons/{personId}/chart/procedures", include_deleted=True)
```

### 7. Current diagnoses + deleted diagnoses (dedicated endpoint)

```python
dx      = extract.fan_out(ng, pids, "persons/{personId}/chart/diagnoses")
dx_del  = extract.fan_out(ng, pids, "persons/{personId}/chart/deleted-diagnoses",
                          label="deleted-diagnoses")
for r in dx_del:
    r["_deleted"] = True
rows, _ = extract.merge_by_id(dx, dx_del)
```

Doubles the fan-out. Verify the endpoint first with `--limit` (the pipeline doesn't use it).

### 8. Merged persons (the "deletion" of persons)

```python
merges = extract.fetch_monthly(ng, "persons/merged", "modifyTimestamp", "2024-01-01")
remap = {extract.safe_id(m["dropPersonId"]): extract.safe_id(m["keepPersonId"]) for m in merges}
```

`persons/merged` has a 7-day window: always with a range.

### 9. Two levels: insurances per encounter

```python
encs = extract.fan_out(ng, pids, "persons/{personId}/chart/encounters")
ins  = extract.fan_out(ng, encs, "persons/{personId}/chart/encounters/{id}/insurances")
```

### 10. List + detail (policyNumber)

```python
from nextgen.client import unwrap

def coverages(pid):
    r = ng.get(f"persons/{pid}/insurances", params={"$top": 100}, pass_status=(404,))
    out = []
    for cov in ([] if r.status_code == 404 else unwrap(r.json())):
        ppid = cov.get("personPayerId")
        det = ng.get_one(f"persons/{pid}/insurances/{ppid}") if ppid else None
        out.append(extract.prune({**cov, **(det or {})}, drop_tenant=True))   # detail wins
    return ng.tag(out, person_id=pid)

rows = ng.map(coverages, pids, label="coverages")     # ng.map already parallelizes: don't nest fan_out
```

### 11. Reconcile ids suspected of deletion (endpoint without isDeleted)

```python
# suspects: [{"personId": ..., "id": ...}] that were in the warehouse and no longer show up
def still_there(s):
    row = ng.get_one(f"persons/{extract.safe_id(s['personId'])}/chart/encounters/{s['id']}")
    return [] if row else [{**s, "_gone": True}]

gone = ng.map(still_there, suspects, label="reconcile")
```

One call per id: limit it to the ids that disappeared from a full reload.

### 12. Probe before a serious load

```bash
ngcat lifecycle chart/medications
python scripts/probe_lifecycle_flags.py --paths "persons/{personId}/chart/medications" --person-ids @sample.txt --explain
```

---

## Script skeleton with deleted-row support

Copy the structure of `scripts/appointments_by_location.py` and add:

```python
p.add_argument("--no-deleted", action="store_true",
               help="skip the isDeleted eq true pass (deletions won't arrive)")

def explain(...):
    ...
    if not args.no_deleted:
        print("  + deleted pass (isDeleted eq true), joined by id")
        for desc, flt in extract.deleted_windows(since=since):     # if incremental
            print(f"    [{desc}] $filter={F.all_of(flt, extract.DELETED_CLAUSE)}")

def report(rows, ...):
    n_del = sum(1 for r in rows if extract.parse_bool(r.get("isDeleted")))
    print(f"    active {len(rows) - n_del:>8}   deleted {n_del:>8}")
```
