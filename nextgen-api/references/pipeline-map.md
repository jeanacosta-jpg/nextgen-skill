# Production pipeline map (Glue)

`glue/extractor.py` extracts ~26 logical tables to
`s3://amazon-glue-etl-files/raw/nextgen/{table}/{run_id}/` (parquet, all strings);
`nextgen/transform.py` turns them into processed (snake_case, keep-list, casts, dedup by PK).
It doesn't run locally (needs `awsglue`/`pyspark`). Use it as a **reference of proven
strategies**, not as code to import.

Job parameters: `--full_reload`, `--tables a,b`, `--appt_start`, `--diag_batch`,
`--prune_fields`, `--include_deleted`, `--include_deleted_per_person`,
`--appt_forward_days` (365), `--chart_fallback_days` (7).

Bookmarks: `s3://etl-bookmarks-bucket/bookmarks/nextgen_api/{table}_ng/modify_timestamp_datetime.json`.
They are written by the **Load** layer (NY clock) except `person_charts_ng` (written by
the extractor, API clock).

| Step | Table | Endpoint | Shape | Incremental / discovery | Deleted | PK (transform) |
|---|---|---|---|---|---|---|
| 1 | practices | `master/practices` (bare token) | list | always full | — | — |
| 1 | payers | `master/payers` (bare token, `$top` 1000) | list | always full | **2 passes** | `id` |
| 3 | locations | `master/locations` + `isSchedulable eq true` | list | `modifyTimestamp` | **2 passes** | `id` |
| 3 | providers | `providers` | list | `modifyTimestamp` | **2 passes** | `id` |
| 3 | resources | `resources` | list | always full (400) | **2 passes** | `id` |
| 3 | service_items | `master/procedures` | list | `modifyTimestamp` | **2 passes** | `service_item_id` |
| 3 | transaction_codes | `master/transaction-codes` | list | `modifyTimestamp` | **2 passes** | `id` |
| 3 | events | `master/events` | list | always full | **2 passes** | `id` |
| 4 | appointments | `appointments` + `$expand=Location` | monthly / incremental | `appointmentDate` (full) · `modifyTimestamp` + range + horizon (incr.) | **own pass by `modifyTimestamp`** | `id` |
| 5 | transactions | `financial/transactions` | list | `transactionDate` (full) · `modifyTimestamp` with fallback | no `isDeleted` | `transaction_id` |
| 6 | persons | index `persons` → `persons/{id}` | singleton fan-out | index by `modifyTimestamp` | no `isDeleted` (see `persons/merged`) | `id` |
| 7 | encounters | index `persons/charts/encounters` → `chart/encounters` | fan-out | index | no `isDeleted` | `id` |
| 7 | charges | index `persons/chart/charges` → `chart/charges` | fan-out | index | no `isDeleted` | `id` |
| 7 | patient_procedures | encounters index → `chart/procedures` | fan-out | index | **2nd call per person** | `id` |
| 7 | encounter_insurances | encounters index → `chart/encounters/{enc}/insurances` | 2-level fan-out | index | — | `(encounter_id, person_payer_id)` |
| 7 | coverages | index `persons` → `insurances` + detail `insurances/{personPayerId}` | fan-out + detail | index | returns deletions (`isActive`) | `person_payer_id` |
| 7b | charge_icd_codes | charges index → `chart/charges` (melt 12 ICDs) | fan-out + melt | index | — | `(charge_id, icd_position)` |
| 7b | charge_diagnoses | `master/diagnoses` in batches of `diagnosisCodeId` | batched OR | derived | not measured | `diagnosis_code_id` |
| 8 | insurances | persons **from appointments** → `insurances` | fan-out | inherited from appointments | returns deletions | `person_payer_id` |
| 9 | history_of_present_illness | `chart/history-of-present-illness` | fan-out | `persons` + bookmark `person_charts_ng` | no `isDeleted` | `ehr_control_id` |
| 9 | care_plan_assessments | `chart/care-plan-assessments` | fan-out | same | no `isDeleted` | `id` |
| 9 | assessments | `chart/assessments` | fan-out | same | no `isDeleted` | `id` |
| 9 | diagnoses | `chart/diagnoses` | fan-out | same | no `isDeleted` → **`chart/deleted-diagnoses`** (unused) | `id` |
| 9 | physical_exam | `chart/physical-exam` | fan-out | same | no `isDeleted` | `sequence_number` |
| 9 | medical_history | `chart/medical-history` | fan-out | same | no `isDeleted` | `history_id` |
| 9 | sticky_notes | `chart/sticky-notes` | fan-out | same | no `isDeleted` | `encounter_id` |
| 9 | person_chart | `persons/{id}/chart` | singleton fan-out | same | — | `person_id` |

## Lessons baked into the code

- **One table failing doesn't kill the job** (`run_step`): it's recorded in `FAILURES`
  and the job ends in error at the end, with the full summary.
- **The summary states each table's window** (`WINDOWS`), the skipped ones (`SKIPPED`)
  and the recovered/failed deleted rows. Copy that transparency into local `report()`s.
- **Paging heartbeat**: one INFO every 20 pages; without it, a long crawl is
  indistinguishable from a hang.
- **Bookmark at the end**: if the job dies before, the next run repeats. Bound the
  windows so they fit the timeout (60 min).
- **Robust bookmark parsing**: date and time only (`_BM_WALL_RE`); Python 3.10's
  `fromisoformat` rejects 1-2/4-5 digit fractions and the `Z`.

## Transform (`nextgen/transform.py`)

`base_transform(dfs, name, keep, casts, pk, ts_col="modify_timestamp")`:
`camel_to_snake` → `astype(str).replace("None"/"nan"/"NaT", None)` → keep-list →
casts (`timestamp`, `integer`→Int64, `double`, `boolean`) → `dedup` by PK keeping the
most recent `ts_col` → `fillna("")` on strings only → drops fully-null columns (Spark
maps them to VOID). Tables with `is_deleted` keep it as boolean: **filtering current
rows is the consumer's job**.
