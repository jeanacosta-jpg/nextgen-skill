# NextGen Enterprise API fundamentals

Everything here is verified against `nextgen/client.py`, `nextgen/odata.py`,
`glue/extractor.py` (months in production) and the `reference/swagger.json` collection
(queried through `ngcat` / `nextgen.catalog`, never read directly).

`ngcat <cmd>` = `python <skill-dir>/scripts/ngcat.py <cmd>`.

```
BASE_URL  https://nativeapi.nextgen.com/nge/prod/nge-api/api
AUTH_URL  https://nativeapi.nextgen.com/nge/prod/nge-oauth/token
Catalog   742 requests · 547 paths · GET 466 · 238 accept $filter · 19 folders
          (Chart 412, Master 121, Persons 56, Provider 32, Users 32, Audit 22,
           Appointments 19, Tasking 12, Financial 9, ...)
```

---

## 1. Authentication (two steps) and session

```
Step 1  POST AUTH_URL   (form-urlencoded)
        grant_type=client_credentials, client_id, client_secret, site_id
        -> access_token

Step 2  PUT  {BASE_URL}/users/me/login-defaults
        Authorization: Bearer <token>
        {"enterpriseId": "...", "practiceId": "..."}
        -> RESPONSE HEADER  x-ng-sessionid

Every call after that:
        Authorization:  Bearer <token>
        X-NG-SessionId: <sid>
```

- **Chicken and egg**: step 2 needs `practiceId`, which comes from `/master/practices`.
  `/master/practices` and `/master/payers` work **with the Bearer token only** (no
  session): `client.bare_token()` / `list_practices()`.
- `/master/practices` names its fields `id` / `name`; the rest of the API says
  `practiceId` / `practiceName`. `from_env()` accepts both.
- With several practices, `from_env()` takes the first one and warns. For another one:
  `list_practices(creds)` and build `NextGenClient(creds, enterprise_id=…, practice_id=…)`.
- The token lasts 60 min; the client refreshes at **55** (`REFRESH_AFTER_S = 3300`).
- The server may **invalidate the session earlier**. A 401 is not an error: it forces
  re-authentication and a retry. A *generation* counter keeps 10 threads hitting a 401
  at the same time from doing 10 re-auths.
- Credentials: `.env` with `NEXTGEN_CLIENT_ID`, `NEXTGEN_CLIENT_SECRET`,
  `NEXTGEN_SITE_ID` (local) or Secrets Manager `specialOps-prod-nextgenAPI` (Glue).
  **Never read or print `.env`.**

## 2. Retries (`_raw_request`)

| Response | Behavior |
|---|---|
| 401 | re-auth (once per generation) and retry |
| 429 | wait `Retry-After` (or 30·(i+1) s) and retry |
| 500/502/503/504 | backoff `10·2^i` s, up to 6 attempts |
| `ConnectionError` | same as 5xx |
| `pass_status` | returned to the caller (404 in fan-out, 400 in GUID probing) |
| **POST** | does **not** retry 5xx: a 502 may arrive after the record was created → duplicate |

30 s timeout per request. 6 retries with backoff ≈ 10 min worst case: a "hung"
extraction is usually this, not a bug.

## 3. OData: what exists and what doesn't

| Parameter | Notes |
|---|---|
| `$filter` | operators `eq ne gt ge lt le`, `and or not`, parentheses. **There is no `in`**. |
| `$top` / `$skip` | paging (100 by default; `/master/payers` works well with 1000) |
| `$orderby` | supported |
| `$expand` | e.g. `Location` on `/appointments` |
| `$inlinecount` / `$count` | totals (not reliable for deciding paging) |
| `$select` | **DOES NOT EXIST**. Trim client-side with `prune()` |

### Literals (where hand-written filters fail most)

```
date       modifyTimestamp ge datetime'2025-01-01T00:00:00'
GUID       locationId eq guid'11c6367b-...'      <- usual
           locationId eq '11c6367b-...'          <- other endpoints (the pipeline filters
                                                    /master/diagnoses with diagnosisCodeId eq '...')
boolean    isDeleted eq true                     <- no quotes
text       lastName eq 'O''Brien'                <- doubled quote
null       field eq null
```

`odata.literal()` infers the value type; `where()` translates a compact syntax:

```python
where("appointmentDate>=2025-01-01", "appointmentDate<=2025-03-31", "locationId in g1,g2")
# appointmentDate ge datetime'2025-01-01T00:00:00' and
# appointmentDate le datetime'2025-03-31T23:59:59' and
# (locationId eq guid'g1' or locationId eq guid'g2')
```

- An upper bound given as a **bare date** with `le`/`lt` expands to `23:59:59` (so the
  last day isn't lost). With millisecond timestamps that **loses the last second**: for
  `modifyTimestamp` windows use `F.half_open(field, from, to_exclusive)`, which emits
  `ge …T00:00:00 and lt <next day>T00:00:00`.
- `F.all_of(*clauses)` joins with AND and **parenthesizes a clause with a top-level
  `or`** (without them `a or b and isDeleted eq true` reads as `a or (b and …)`).
- A very long `F.any_of` produces huge URLs: split it into batches (the pipeline uses 10
  `diagnosisCodeId` per call, `--diag_batch`).

### GUID dialect

Undocumented and not uniform. `detect_guid_dialect(ng, path, field, guid)` tries
`guid'…'`, then `'…'`; returns `"guid" | "plain" | "none"`. With `"none"`, fetch the
range without that filter and filter client-side (comparing lowercase), as
`scripts/appointments_by_location.py` does.

### Filters the API rejects

| Endpoint | Rejects | Consequence |
|---|---|---|
| `/resources` | `modifyTimestamp` (400) | always full load |
| `/master/events` | `modifyTimestamp` (it has no timestamp) | always full load |
| `persons/{id}/chart/...` (per-person detail) | `modifyTimestamp` (400/404) | incremental logic is applied when **discovering** persons |

### Field names in the swagger prose

`/persons` and `/persons/payers` document their window with `create_timestamp` /
`modify_timestamp` (snake_case), but the filter that works in production is
**`modifyTimestamp`** (camelCase). Trust the pipeline code, not the prose.

## 4. Silent default window (15 endpoints)

Without a date field in `$filter`, they return **7 days** and give no warning.

| Endpoint | Window | Field that lifts it |
|---|---|---|
| `/appointments` | **next** 7d | `appointmentDate` (also `modifyTimestamp`, measured 2026-08/09) |
| `/appointments/slots` | next 7d | `StartDate` |
| `/resources/appointments`, `/resources/:resourceId/appointments` | next 7d | `appointmentDate` |
| `/financial/transactions` | last 7d | `createTimestamp` / `modifyTimestamp` (the pipeline uses `transactionDate` for full loads) |
| `/persons` | last 7d | `modifyTimestamp` |
| `/persons/chart/charges` | last 7d | `createTimestamp` / `modifyTimestamp` |
| `/persons/charts/encounters` | last 7d | any date field |
| `/persons/charts/diagnostic/orders`, `/lab/orders`, `/referral-orders` | last 7d | any date field |
| `/persons/charts/immunizations`, `/medications` | last 7d | `createTimestamp` / `modifyTimestamp` |
| `/persons/merged`, `/persons/payers` | last 7d | `createTimestamp` / `modifyTimestamp` |

**Direction** matters: `/appointments` looks forward; the persons indexes look back.
A full load is split month by month (`fetch_monthly`) from `FULL_LOAD_START = 2024-01-01`.

## 5. Paging and envelopes

- Reliable method: `$skip += $top` until a page **shorter** than `$top`.
  `NextPageLink` exists but is not reliable.
- Lists: `{"items": […]}` (real), `{"Items": […]}` (swagger), bare list, or `null`.
  `unwrap()` covers all 4.
- Single object: dict, `{"items":[obj]}` or `[obj]`. `unwrap_one()` covers all 3.
- `get_all(max_items=N)` stops early (useful for `--limit` and probes).
- 0.1 s pause between pages and ~1 s between months: keep them on long loads.

## 6. Data shape

- **No `$select`**: `prune()` drops `_links`/`*Links`/`vendorExtensions` (hateoas),
  `*Utc`/`*UtcOffset`/`*Timezone` (timestamp variants), `createdBy`/`modifiedBy`
  (+`Id`/`LoginId`) and, with `drop_tenant=True`, `enterpriseId`/`practiceId`/`practiceName`.
  Tenant: **keep it in lists and masters** (`transform_events` uses `practice_id`),
  **drop it in fan-out**.
- `ng.tag()` adds `_practiceId`, `_enterpriseId`, `_practiceName` (and `_personId` in
  fan-out). The `_` prefix avoids collisions.
- `flatten_nested()` flattens one level of dicts (`a_b`); lists (`resourceIds`) are
  serialized to JSON on write.
- Ids: pandas turns integers into floats (`12345.0`) → `safe_id()`. `""` and `"0"` mean
  "no foreign key" → `is_blank_id()`.
- `medical_record_number` is a string with leading zeros. `management_date` /
  `disease_date` (medical_history) are free text.
- A charge carries up to 12 ICDs: `icdCodeId`, `icdCodeId2` … `icdCodeId12`.
- Timestamps: `'2026-08-11T16:25:36.56Z'`, up to 7 decimals (.NET), with or without zone.
  `parse_ts()` normalizes to naive (wall clock). `transform.to_timestamp()` uses
  `format="ISO8601"` and pins `datetime64[us]` (Spark can't read nanoseconds).
- The Glue extractor writes **everything as string** (`astype(str)`); `transform.py`
  relies on it (`base_transform` → `replace("None", None)`). **Don't change that.**

## 7. Bookmark time zones (Glue only)

- Bookmarks written by **LoadSnowflake** are **America/New_York** wall clock.
  `_bookmark_utc()` adds a **fixed +5** and rounds to the hour (during NY daylight time
  it's UTC-4: the effective overlap is 1 h smaller).
- `person_charts_ng` is written **by the extractor itself** with the API clock: it is
  read with `tz_shift=False`. Shifting it would put `since` 5 h in the future and
  discover nobody.
- Local: `extract.resolve_since()` subtracts a 1 h overlap; no zone applied.
