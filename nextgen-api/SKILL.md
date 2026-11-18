---
name: nextgen-api
description: >-
  Expert knowledge of the NextGen Enterprise API: its 547 routes, which filters and
  fields each one accepts, the DEPENDENCIES between routes (which endpoint produces each
  :personId, :encounterId, :orderId... and in what order to call them), its silent
  traps, and the nextgen/ library in the nextgen-platform repo. Use it when someone asks
  for NextGen data ("I need the appointments/charges/encounters/diagnoses for..."),
  asks "which endpoint gives X?", "how do I reach this route?", "where do I get the
  encounterId?", "what hangs off /persons/{id}/chart?", wants to filter with
  $filter/OData, fetch deleted records (isDeleted, cancelled, merged), run full or
  incremental loads, fan out per person, estimate how many calls an extraction will
  cost, or troubleshoot 401/400 errors, empty responses or extractions returning fewer
  rows than expected. Works offline and without credentials; inside the
  nextgen-platform repo it also writes scripts that reuse its library.
argument-hint: "[data request, route or problem]"
---

# NextGen Enterprise API

Request: **$ARGUMENTS**

Almost every error in this API is **silent**: nothing fails, you just get fewer rows.
This skill is the map of the routes, how they depend on each other, and those traps.

## Tool: `ngcat.py` (routes, offline, no credentials)

It lives at `scripts/ngcat.py` **inside this skill's directory** (the "Base directory"
shown when the skill loads; a user install is usually `~/.claude/skills/nextgen-api/`).
Standard library only, answers in milliseconds:

```bash
NGCAT="python <skill-dir>/scripts/ngcat.py"
$NGCAT search <term> [GET]       # find an endpoint
$NGCAT show <path>               # filters, fields+type, TRAPS, DELETED, what it depends on
$NGCAT fields <field>            # which endpoints return that field
$NGCAT chain <path>              # ordered call plan to reach the route + cost
$NGCAT params [name]             # who produces each :param
$NGCAT children <path>           # subroutes; tree [prefix] --depth N for the tree
$NGCAT traps | lifecycle [path]  # 7-day windows / how deleted rows are handled
```

Paths work in any form (`persons/{personId}/chart`, `/persons/:personId/chart`).
**Don't guess paths or fields: check them here.** Never read `reference/swagger.json`
(6.7 MB) or `data/endpoints.json` by hand: it burns the context.

## Am I inside the nextgen-platform repo?

Check whether `nextgen/client.py` exists in the working directory.

- **Yes** → reuse the library (table below), write to `scripts/` following
  `scripts/appointments_by_location.py`, and follow the repo's `CLAUDE.md`.
  `python -m nextgen.catalog` is the source of truth; `ngcat` adds `chain`, `params`,
  `children` and `tree`.
- **No** → answer with `ngcat` and the references (endpoint, filters, chain, cost,
  traps). For runnable code, say it should be done inside the nextgen-platform repo;
  **don't reimplement the HTTP client** elsewhere.

## Non-negotiable rules

1. **PHI.** NextGen data is PHI. Never show patient rows or read `output/`,
   `bookmarks/` or `.env`; show counts and column names. Before processing PHI with
   Claude Code or any other integration, the service and mode must be approved in
   `vipmed-technology/approved-services`; if that can't be confirmed, VIPMedicalGroup
   Cybersecurity decides.
2. **`--explain` → `--limit 50` → full load**, and a person launches the full load.
3. **Reuse `nextgen/`**: client, filters, paging, fan-out, deleted rows, bookmarks.
4. **Never `isDeleted eq false`** in the base filter.

## The 6 traps (95% of errors)

| # | Trap | Symptom | Defense |
|---|---|---|---|
| 1 | **Two-step** auth: token + `PUT /users/me/login-defaults` → `X-NG-SessionId` | 401 on everything | `NextGenClient.from_env()` |
| 2 | **7-day window** on 15 endpoints if `$filter` doesn't name their date (`appointments` looks forward; persons, charges, encounters, transactions indexes look back) | fewer rows, no error | `fetch_monthly()` / explicit range |
| 3 | **Lists that hide `isDeleted=true`** (`appointments` 0/868, `resources` 0/65…) | deletions never arrive; dangling keys | `with_deleted()`, `fetch_deleted()`, `fan_out(include_deleted=True)` |
| 4 | **No `$select`** | huge payload | `extract.prune()` |
| 5 | Non-uniform **GUID dialect** (`guid'…'` vs `'…'`) | 400 or 0 rows | `odata.detect_guid_dialect()` + client-side filter |
| 6 | Envelope `items` in **lowercase** (swagger says `Items`) | empty list | `ng.get_all()` / `unwrap()` |

Details: [references/api-fundamentals.md](references/api-fundamentals.md).

## Routes and dependencies (the essentials)

```
auth: token -> /master/practices (Bearer only) -> PUT /users/me/login-defaults
/master/*, /providers, /resources, /users ......... masters: direct list
/appointments, /financial/transactions ............ MONTHLY list (7d window)
/persons ........................................... person index (7d) -> :personId
/persons/:personId/chart/* ......................... per-person fan-out (459 routes)
/persons/:personId/chart/encounters/:encounterId/* . 2 levels (140 subroutes)
```

- `:personId` ← `/persons`, or with a date from `persons/chart/charges`,
  `persons/charts/encounters`, `appointments` (`extract.discover_person_ids`).
- `:encounterId` ← `/persons/:personId/chart/encounters` (`id`).
- A **"parent list"** producer = the path right before the `:param` (reliable).
  A **"field"** producer = match by field name (heuristic: confirm it with `show`).
- **Cost** = product of the levels: 20,000 persons × N encounters. Always state it in the plan.

For any route with a `:param`, run `chain <path>` before designing anything.
Full map, producer table and pipeline table dependencies:
[references/route-dependencies.md](references/route-dependencies.md).

## Workflow for a data request

```
1. UNDERSTAND entity · filters · period · include deleted/cancelled?
2. LOCATE     search / show / fields
3. DEPEND     chain <path>: what must be called first and how many calls it costs
4. TRAPS      show flags 7d TRAP and DELETED [hides|probable|via|none|shows|detail]
5. SHAPE      list | monthly list | fan-out (+ deleted pass if applicable)
6. REUSE      is there a script in scripts/ that covers it with one more parameter?
7. WRITE      scripts/<name>.py (argparse, --explain, --limit, report() with counts)
8. VALIDATE   --explain (offline), then hand over the --limit 50 and full-load commands
```

Ask before writing code when the answer changes the result: filter by `locationId` or
fetch everything?, include deleted?, does "cancelled" mean `isCancelled` or also
`isNoShow`/`isRescheduled`?, what range?

```
Does the path have a :param?
 ├─ yes → FAN-OUT: chain <path> says where the parents come from
 └─ no  → 7d TRAP?  yes → MONTHLY LIST (fetch_monthly)   no → LIST (ng.get_all)
Then: DELETED hides/probable and they want deletions (or it's incremental)? → deleted pass
```

Full strategies: [references/query-strategies.md](references/query-strategies.md).

## Deleted rows

| Class | Meaning | How to fetch deletions |
|---|---|---|
| `hides` | measured: the default view hides `isDeleted=true` | 2nd pass `isDeleted eq true`, join by `id` |
| `probable` | has `isDeleted`, not measured | treat as `hides`; measure with the repo's `scripts/probe_lifecycle_flags.py` |
| `via` | no `isDeleted`, dedicated endpoint | `chart/deleted-diagnoses`, `chart/deleted-allergies`, `persons/merged` |
| `none` | no `isDeleted` | only reconcile by id (detail → 404) |
| `shows` | returns deletions unasked | filter `isActive` client-side |
| `detail` | GET by id | returns the record even if deleted |

```python
rows = extract.with_deleted(ng, "master/locations")                          # all + deleted
rows = extract.fetch_monthly(ng, "appointments", "appointmentDate",
                             "2025-01-01", "2025-12-31", include_deleted=True)
deleted = extract.fetch_deleted(ng, "appointments", since="2026-08-20T00:00:00")  # incremental
rows, n = extract.merge_by_id(active, deleted or [])
```

In incrementals the deleted pass goes **only by `modifyTimestamp`** with an exclusive
upper bound (`F.half_open`). `isCancelled`, `isNoShow`, `isActive`, `status`… are
**not** deletions. Details: [references/deleted-and-lifecycle.md](references/deleted-and-lifecycle.md).

## What already exists in `nextgen/` (don't rewrite it)

| You need | Use |
|---|---|
| authenticate | `NextGenClient.from_env()` |
| paged list / object | `ng.get_all(path, params=…, max_items=…)` / `ng.get_one(path)` |
| `$filter` | `odata.where("field>=value", "field in a,b")`, `F.between`, `F.since`, `F.half_open`, `F.all_of`; `odata.params(filter, expand=…)` |
| monthly load | `extract.fetch_monthly(ng, path, date_field, from, to, include_deleted=…)` |
| deleted rows | `extract.with_deleted`, `fetch_deleted`, `deleted_windows`, `merge_by_id` |
| persons / fan-out | `extract.discover_person_ids(ng, since=…)`, `extract.fan_out(ng, parents, template)` |
| GUID dialect | `odata.detect_guid_dialect(ng, path, field, guid)` |
| trim / normalize | `extract.prune`, `safe_id`, `is_blank_id`, `parse_ts`, `parse_bool` |
| incremental | `resolve_since(table)`, `write_bookmark`, `max_timestamp` |
| save | `extract.write(rows, table, formats=("parquet","csv"))` |

Signatures, module dependencies and packages: [references/library.md](references/library.md).
Recipes: [references/recipes.md](references/recipes.md).

## Quick diagnosis

| Symptom | Most likely cause |
|---|---|
| 401 on everything | auth without `login-defaults` |
| 400 filtering by GUID | dialect → `detect_guid_dialect` |
| 400 with `modifyTimestamp` | `/resources`, `/master/events`, per-person details don't accept it |
| fewer rows, no error | 7d window (`traps`), hidden deleted rows (`lifecycle`) or incomplete parents in a fan-out (`chain`) |
| 0 deleted rows recovered | `isDeleted eq false` in the base filter, or the pass uses the business date |
| dangling keys | master extracted without its deleted pass |
| `12345.0` in the URL | missing `safe_id()` |
| "al padre le falta el campo X" (parent missing field X) | the fan-out template uses a key the parent list doesn't return (`show`) |

Full tree: [references/troubleshooting.md](references/troubleshooting.md).
Glue pipeline (26 tables): [references/pipeline-map.md](references/pipeline-map.md).

## Maintaining this skill

`data/endpoints.json` is a snapshot of the repo's catalog (date in `ngcat stats`).
When the swagger changes or something new is measured in `nextgen/catalog.py`
(`HIDES_DELETED`, `SHOWS_DELETED`, `NO_DELETED_FLAG`, `DELETED_VIA`, `_EXTRA_TRAPS`),
rebuild it: `python scripts/build_index.py --repo <path-to-nextgen-platform>`.
The `deleted.why` / `extra_trap` texts inside the index come from the repo and are in
Spanish; `ngcat` prints everything else in English.
