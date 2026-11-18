# The `nextgen/` library in the nextgen-platform repo

The code lives in the **nextgen-platform** repo (not in this skill). This page says
what's there, how the modules depend on each other and which packages are needed, so
you **reuse instead of rewriting** (the repo was born because 8 scripts were
reimplementing the same HTTP client).

## Module dependencies

```
              requests  (python-dotenv optional, boto3 lazy)
                 │
            client.py ──────────── NextGenClient, unwrap, unwrap_one, bare_token,
                 │                  list_practices, first_practice, creds_from_env/aws_secret
            odata.py  (stdlib) ─── where, F, params, literal, month_ranges,
                 │                  detect_guid_dialect(client, ...)
            extract.py ─────────── re-exports F, params, where, month_ranges
                 │   (pandas + pyarrow only when writing)
     scripts/*.py  ─── import client + odata + extract (+ catalog)
     catalog.py    (stdlib) ─── reads reference/swagger.json; depends on nothing
     transform.py  (pandas) ─── raw -> processed; imported by glue/transformation_nextgen_api.py
     glue/extractor.py ──────── STANDALONE: does not import nextgen/ (Glue doesn't package it);
                                duplicates the logic on purpose. If you change a
                                behavior, change it in both places.
```

`requirements.txt` (local): `requests>=2.31`, `pandas>=2.0`, `pyarrow>=14.0`,
`python-dotenv>=1.0`; `boto3` only for Secrets Manager. Glue already ships all of them.
Scripts run from the repo root (they add the root to `sys.path`).

## `client.py`

| Symbol | What it does |
|---|---|
| `BASE_URL`, `AUTH_URL` | `https://nativeapi.nextgen.com/nge/prod/nge-api/api`, `.../nge-oauth/token` |
| `NextGenClient.from_env(**kw)` | reads `NEXTGEN_CLIENT_ID/SECRET/SITE_ID`, resolves the practice and does the 2-step auth |
| `NextGenClient(creds, enterprise_id=, practice_id=)` | to pick another practice (`list_practices(creds)`) |
| `ng.get(path, params=, pass_status=())` | raw GET with retries (401 re-auth, 429 Retry-After, 5xx backoff) |
| `ng.post(...)` | JSON POST; does **not** retry 5xx (avoids duplicates) |
| `ng.iter_pages(path, params=)` / `ng.get_all(path, params=, max_items=)` | paging `$skip += $top` until a short page |
| `ng.get_one(path)` | singleton; 404 → `None` |
| `ng.map(fn, items)` | parallel |
| `ng.tag(items, person_id=)` | adds `_practiceId`, `_enterpriseId`, `_practiceName`, `_personId` |
| `unwrap(payload)` / `unwrap_one(payload)` | envelopes `items`/`Items`/list/null |
| `bare_token(creds)` / `list_practices(creds)` | routes that work with the Bearer only |
| `REFRESH_AFTER_S=3300`, `MAX_RETRIES=6`, `TIMEOUT_S=30` | |

## `odata.py`

| Symbol | What it does |
|---|---|
| `where("field>=value", "field in a,b", dialect="guid")` | compact syntax → `$filter` (turns `in` into ORs, dates into `datetime'…'`) |
| `F.cmp`, `F.any_of`, `F.between`, `F.since(v, field="modifyTimestamp")` | builders |
| `F.half_open(field, from, to_excl)` | `ge … and lt …` for millisecond timestamps |
| `F.all_of(*clauses)` | AND, parenthesizing clauses with `or` |
| `params(filter, expand=, orderby=, **extra)` | dict ready for `get_all` |
| `literal`, `dt_literal`, `guid_literal` | OData literals |
| `month_ranges(from, to)` | monthly ranges |
| `detect_guid_dialect(ng, path, field, guid)` | `"guid"` / `"plain"` / `"none"` |

## `extract.py`

| Symbol | What it does |
|---|---|
| `fetch_monthly(ng, path, date_field, start, end=None, *, base_filter=, include_deleted=, drop_tenant=)` | full load month by month (mandatory with a 7d window) |
| `with_deleted(ng, path, *, params=)` | default view + `isDeleted eq true`, joined by id (rejects `isDeleted eq false`) |
| `deleted_windows(since=, until=, start=, end=)` | plan of the deleted pass (for `--explain`) |
| `fetch_deleted(ng, path, *, since=, until=, ...)` | deletions by `modifyTimestamp` (incrementals) |
| `merge_by_id(active, extra, key="id")` | → `(rows, n_added)` |
| `discover_person_ids(ng, *, since=, start="2024-01-01", limit=, path="persons")` | person ids to walk |
| `fan_out(ng, parents, path_template, *, mode="list"/"singleton", include_deleted=, drop_tenant=True)` | one GET per parent; 404 = 0 rows; dict parents → chains levels |
| `prune(row, drop_tenant=)` / `prune_all` / `flatten_nested` | trimming (there's no `$select`) |
| `safe_id`, `is_blank_id`, `parse_ts`, `parse_bool` | normalization |
| `read_bookmark`, `write_bookmark`, `resolve_since(table)`, `max_timestamp(rows)` | local incrementals (`bookmarks/`) |
| `run_id()`, `to_frame`, `write(rows, table, formats=("parquet","csv"))`, `write_frame(df, …)` | `output/{table}/{run_id}/data.*` |
| `DELETED_CLAUSE` | `"isDeleted eq true"` |

## `catalog.py`

Same as `ngcat.py` but reading the repo's swagger:
`python -m nextgen.catalog search|show|fields|traps|lifecycle|stats|folders`.
It's the **source of truth** for measurements (`HIDES_DELETED`, `SHOWS_DELETED`,
`NO_DELETED_FLAG`, `DELETED_VIA`, `_EXTRA_TRAPS`). `ngcat.py` is a snapshot of that;
when something new is recorded there, rebuild the skill's index
(`scripts/build_index.py --repo <repo>`). (The repo's catalog prints in Spanish.)

## `transform.py`

`base_transform(dfs, name, keep, casts, pk, ts_col="modify_timestamp")` and the 26
raw → processed transformations. **It assumes everything arrives as a string**: don't change that.

## Reference scripts (the repo's `scripts/`)

| Script | Pattern |
|---|---|
| `appointments_by_location.py` | **canonical template**: argparse → `plan()` → `explain()` → `extract_*()` → `report()` → `main()`; month by month; GUID probing with client-side fallback |
| `appointments_modified_on_day.py` | incremental by `modifyTimestamp` + deleted pass + future horizon + bookmark |
| `probe_lifecycle_flags.py` | measure whether an endpoint hides deleted rows (counts only) |
| `appointment_show.py` | viewer by id (`--mask` for PHI) |
| `person_everything.py` | every `chart/*` route for one person |
| `persons_sample.py` | person sampling |

Every new script: `--explain` without network, `--limit N`, `--from/--to`, ids via CLI or
`@file`, `report()` with counts only, `extract.write()`, and `--include-deleted` if the
endpoint hides deleted rows.
