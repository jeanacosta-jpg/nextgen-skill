# Routes and route dependencies

The API has **547 paths** and **99 distinct path parameters**. Almost every interesting
route hangs off a `:personId`, and many off an `:encounterId` below it. To call a route
with parameters you **first have to get those ids from another route**. That chain is
the "dependency", and it determines the cost of the extraction.

`ngcat <cmd>` = `python <skill-dir>/scripts/ngcat.py <cmd>`.

```bash
ngcat chain <path>        # ordered call plan to reach a route
ngcat params [name]       # who produces each :param
ngcat children <path>     # what hangs off a route
ngcat tree [prefix] --depth N
```

## 1. Root: authentication

Everything depends on the session, which in turn depends on a practice:

```
POST nge-oauth/token                         -> access_token
GET  /master/practices      (Bearer only)    -> practiceId, enterpriseId
PUT  /users/me/login-defaults                -> x-ng-sessionid header
                                                (without it: 401 on everything)
```

`/master/practices` and `/master/payers` are the only routes that work **without a
session**. `NextGenClient.from_env()` does all three steps.

## 2. Top-level tree

```
/appointments      (+17)  appointments, slots, waitlist   7d window looking FORWARD
/audit             (+22)  audit events
/document-batches  (+6)
/favorites         (+6)
/financial         (+9)   transactions, guarantors        7d window looking BACK
/master            (+121) masters: locations, payers, procedures, transaction-codes, events, diagnoses...
/persons           (+469) persons and the WHOLE clinical chart  (:personId in 459 routes)
/providers         (+31)
/resources         (+5)   rejects modifyTimestamp -> always full load
/users             (+43)  /users/me/login-defaults lives here
```

Below `/persons/:personId/chart/encounters/:encounterId` there are **140 subroutes**
(diagnoses, procedures, vitals, allergies, insurances, care-plan…).

## 3. Who produces each parameter

Two kinds of producer, and `ngcat` tells you which is which:

- **parent list**: the path right before the `:param` is a GET listing those objects
  (`persons/:personId/chart/encounters` → its `id` is the `:encounterId`). It's the
  natural relationship and the most reliable.
- **field**: a list returns a field with the same name (`appointments.personId`).
  It's a **name-based heuristic**: confirm with `show` that the id means the same thing
  (e.g. a charge's `procedureId` is not necessarily the `:procedureId` of
  `encounters/:encounterId/procedures`).

| Parameter | Routes | Main producer | Notes |
|---|---|---|---|
| `:personId` | 459 | `/persons` (`personId`) | **7d window**; discover with a date via `persons`, `persons/chart/charges`, `persons/charts/encounters`, `appointments` (`extract.discover_person_ids`) |
| `:encounterId` | 162 | `/persons/:personId/chart/encounters` (`id`) | also the cross-person index `/persons/charts/encounters` (7d) |
| `:orderId` | 52 | `/persons/:personId/chart/lab/orders` (`id`) | immunization/diagnostic orders have their own parent |
| `:userId` | 42 | `/users` (`id`) | |
| `:providerId` | 31 | `/providers` (`id`) | hides deleted rows |
| `:eventId` | 23 | `/master/events` (`id`) | `audit/events/:eventId` is a DIFFERENT event (audit) |
| `:personPayerId` | 17 | `/persons/:personId/insurances` (`personPayerId`) | returns deletions (`isActive`) |
| `:medicationId` | 17 | `/persons/:personId/chart/medications` | |
| `:documentId` | 11 | `/persons/:personId/chart/documents` (`id`) | |
| `:appointmentId` | 10 | `/appointments` (`id`) | the detail does return deleted rows |
| `:diagnosisId` | 10 | `/persons/:personId/chart/encounters/:encounterId/diagnoses` | |
| `:practiceId` | 7 | `/master/practices` (`id`) | Bearer only |
| `:resourceId` | 3 | `/resources` (`id`) | |

Full, up-to-date list: `ngcat params`. Those shown as **external** (`:vitalsId`,
`:postpartumCheckId`…) have no list that produces them: they come from a previous
detail call, another system, or the user.

## 4. Typical chains

```
master/*, providers, resources, users      0 levels   direct list (ng.get_all)
appointments, financial/transactions       0 levels   MONTHLY list (7d window)
persons/:personId                          1 level    persons index -> detail (singleton)
persons/:personId/chart/<x>                1 level    discover persons -> fan_out
persons/:personId/insurances/:personPayerId  2        insurances -> detail
persons/:personId/chart/encounters/:encounterId/<x>   2 levels
persons/:personId/chart/lab/orders/:orderId/tests/:testId   3 levels
```

Real example (`ngcat chain persons/:personId/chart/encounters/:encounterId/insurances`):

```
0. AUTHENTICATION (token -> practices -> login-defaults)
1. :personId    <- GET /persons                              ! 7d window, deletions in /persons/merged
2. :encounterId <- GET /persons/:personId/chart/encounters   ! no isDeleted
3. GET /persons/:personId/chart/encounters/:encounterId/insurances
COST: persons x encounters calls
```

In code, `extract.fan_out` chains levels by passing **dicts** as parents:

```python
pids = extract.discover_person_ids(ng, since="2026-01-01")
encs = extract.fan_out(ng, pids, "persons/{personId}/chart/encounters")
# each encounter carries personId and id -> second level with no new code
ins = extract.fan_out(ng, encs, "persons/{personId}/chart/encounters/{id}/insurances")
```

The template is filled with the **parent dict's keys** (`str.format`). If a parent is
missing a key, that row is skipped with a warning. Check with `show` that the parent
list carries that field; `ng.tag` also adds `_personId`.

## 5. Cost: estimate before launching

| Shape | Calls |
|---|---|
| list | pages of 100 (`$top`) |
| monthly list | months × pages (1 year of appointments ≈ 12 ranges) |
| 1-level fan-out | number of persons (20,000 persons = 20,000 calls) |
| 2-level fan-out | persons × encounters per person |
| per-person `include_deleted` | **× 2** |

That's why the mandatory flow is `--explain` (no network, prints the plan) →
`--limit 50` → full load launched by a person, not by Claude.

## 6. Data dependencies between tables (pipeline)

Besides path parameters, there are **discovery** dependencies:

```
persons (modifyTimestamp index) ──┬─> persons/{id}                      (persons)
                                  ├─> chart/* (9 clinical tables)       (bookmark person_charts_ng)
                                  └─> insurances + detail               (coverages)
persons/charts/encounters ────────┬─> chart/encounters, chart/procedures
                                  └─> encounters/{enc}/insurances
persons/chart/charges ────────────┬─> chart/charges
                                  └─> melt icdCodeId..icdCodeId12 ──> master/diagnoses (batches of 10)
appointments ─────────────────────> persons from appointments ──> insurances
master/* (locations, resources, transaction-codes, events...) ──> keys in appointments/charges
```

A master extracted **without its deleted pass** leaves dangling keys in the tables that
reference it (an appointment with a `resourceId` that doesn't exist in `resources`).
Details of the 26 tables: [pipeline-map.md](pipeline-map.md).
