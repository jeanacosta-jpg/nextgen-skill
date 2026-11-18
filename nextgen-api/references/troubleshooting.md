# Troubleshooting extractions

Almost every failure is one of these cases. Identify it by the symptom before
investigating. Always ask for the `--explain` output: it shows the filters without
spending calls and often gives the problem away.

Answer format:

```
CAUSE      what's happening and why
EVIDENCE   the fact that confirms it (message, ngcat output, line of code)
FIX        the concrete change
```

---

## HTTP errors

| Symptom | Cause | Check | Fix |
|---|---|---|---|
| 401 on everything | hand-written auth without `PUT /users/me/login-defaults` / no `X-NG-SessionId` | `grep -n "oauth/token\|login-defaults\|requests\." scripts/x.py` | `NextGenClient.from_env()` |
| intermittent 401 | session invalidated by the server | "forcing refresh" log line | already retried; if it persists, credentials (don't print `.env`) |
| 400 filtering by GUID | wrong dialect | `detect_guid_dialect` | use the returned dialect or client-side fallback |
| 400 with `modifyTimestamp` | `/resources`, `/master/events`, per-person details | `ngcat show <path>` | full load / incremental at discovery |
| 400 with `isDeleted` | endpoint without that filter | `probe_lifecycle_flags.py` → `NO FILTRABLE` | filter client-side or reconcile by id |
| generic 400 | malformed literal (date without `datetime'…'`, undoubled quote, `in`) | print the `$filter` | `where()` / `F` |
| 404 in fan-out | the person has no records of that type | — | not an error (0 rows) |
| 404 on detail | the id doesn't exist in this practice or isn't visible | `appointment_show.py <id>` | check the practice (`list_practices`) |
| 429 | rate limit | log | retried with `Retry-After`; lower `workers` |
| 5xx / timeouts | server | retry log | backoff 10·2^i; reduce volume |

## Missing rows (no error) — the treacherous case

Check in this order:

1. **7-day window.** `ngcat traps`. If the endpoint is listed and the `$filter`
   doesn't name its field → explicit range / `fetch_monthly`. Mind the direction:
   `/appointments` = next 7 days.
2. **Hidden deleted rows.** `ngcat lifecycle <path>`. If it's `hides`/`probable` and
   deletions are missing → second pass. Test with a known id: `appointment_show.py <id>`;
   if the detail returns it with `isDeleted=true` and the list doesn't, that's it.
3. **`isDeleted eq false` in the base filter** together with a deleted pass → the pass
   returns 0.
4. **Deleted pass by business date** in an incremental → loses deletions of records
   dated outside the range. It must go by `modifyTimestamp`.
5. **Upper bound** `le …T23:59:59` on `modifyTimestamp` → loses the last second of each
   slice. Use `F.half_open`.
6. **Range ending today** on appointments → future ones are missing. Horizon +365.
7. **Badly discovered person universe**: derived from appointments, or an index
   without a filter (7 days). Use the right cross-person index with a date
   (`ngcat chain <path>` lists the producers).
8. **Fan-out truncated to 100 per person**: it was a bug in `extract.fan_out` (first
   page only). Fixed: it now pages. If old outputs show exactly 100 rows for many
   persons, they were truncated.
9. A forgotten **`--limit`**, or `max_items`.
10. **Wrong practice** (several practices; `from_env` takes the first).
11. **GUID filter with `"none"`** and client-side filtering comparing without
    normalizing case.
12. **Fan-out template key missing** in the parent ("al padre le falta el campo X"
    warning): those parents are skipped. Check with `ngcat show <parent>`.

## 0 rows

By likelihood: the filter doesn't match (GUID dialect, ids from another practice),
empty range, or the `$filter` uses a field the endpoint doesn't return
(`ngcat show <path>` lists the real fields). Try the same query without the suspect
filter with `--limit 5`.

## Odd data

| Symptom | Cause | Fix |
|---|---|---|
| ids `12345.0` | pandas → float | `safe_id()` |
| foreign keys `"0"` | "no FK" sentinel | `is_blank_id()` |
| `NaT` in timestamps | mixed formats / 7 decimals / zone | `parse_ts()`; `transform.to_timestamp` (ISO8601) |
| parquet column fails on mixed type | lists/dicts | `extract.to_frame()` serializes to JSON |
| Spark rejects a column | all null → VOID | `base_transform` drops them |
| master missing the row a fact points to | master without deleted pass | `with_deleted` |
| booleans `"TRUE"`/`1` | CSV/Oracle | `parse_bool()` |

## Slowness

A fan-out = one GET per person (×2 with `include_deleted`, ×(1+N) with detail, × the
next level for 2-level routes). Bound it with `since`, `--limit` or a sample of
persons. The pauses between pages (0.1 s) and months (1 s) are deliberate.
`workers=10` by default; more threads → more 429s.

## Glue

| Symptom | Cause |
|---|---|
| table skipped "bookmark is from today" | `ALWAYS_FULL` already ran today (UTC): expected |
| chart tables take tens of minutes | no `person_charts_ng` bookmark → 7-day fallback; `--full_reload` does the full crawl |
| windows shifted 4-5 h | NY vs UTC bookmark (`_bookmark_utc`, fixed +5) |
| "0 deleted rows in the whole run" | the `isDeleted` filter stopped working or `--include_deleted` was disabled |
| `DELETED_FAILED` in the summary | windows whose deleted pass failed: incomplete deletions |
