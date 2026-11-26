# vcca package — module interfaces

Spec: `../docs/重构计划.md`. This file is the contract between modules; signatures in the stub modules are
authoritative (docstrings there carry the detailed rules). Implemented now (steps 1–2): `config`, `models`,
`util_time`, `caac` (+`biff8`), `vamsys`, CLI `status`/`test`. Everything else is a stub raising
`NotImplementedError` (the CLI turns that into a friendly "尚未实现" message, exit code 2).

## Conventions (all modules)

| Thing | Representation |
|---|---|
| route key | `RouteKey = (op, callsign, orig, dest)`; airports in **VAMSYS codes** (`cfg.vamsys_airport()` / `airports.alias` applied). Exception: `PlanLeg.orig/dest` and `RotationGap` keep plan codes. |
| time of day | `'HH:MM'` UTC string (plus `*_min` ints where useful). Fields named `bjt_*` are plan-local. |
| weekdays | `frozenset[int]`, 0 = Monday. Convert with `util_time.days_to_vdays / vdays_to_days / days_to_fd3 / fd3_to_days`. |
| timestamps | int UNIX seconds UTC. |
| fleet IDs | `str` (`'26071'`); sets are `frozenset[str]`, tuples sorted ascending. |
| text | Chinese text only in the output layer via `models.describe_reason`, `models.CATEGORY_ZH`. Decisions carry `Reason(code, params)`. |
| config | one frozen `Config` passed explicitly; no module-level globals / paths. |
| VAMSYS DB | read-only (`vamsys.connect_ro`), never written. |
| FlightRadarAPI | only `fr24_client` imports it, after `config.ensure_flightradarapi_on_path()`. |

## Data flow

```
settings.toml + season.toml + rules/*.toml ($VCCA_HOME) ──> config.load_config() ──> Config
                                                        │
 CAAC .xls ──> caac.load_plan(cfg) ──> PlanData(rows, legs: [PlanLeg])
                caac.group_legs(legs, cfg.airport_alias) ──> {RouteKey: PlanGroup(raw_slots: [PlanSlot])}
                caac.detect_rotation_gaps(legs) ──> {RouteKey: RotationGap}
 workspaces.db ──> vamsys.load_vamsys(cfg) ──> VamsysSnapshot(airports, fleets, aircraft, routes: [VRoute], revision)

 fetch:  fr24_fetch.fetch(cfg, ...) : derive_targets(plan, vamsys, store) -> plan_fetch -> run_fetch(client)
           fr24_client.FR24Client.flight_list() ──FetchResult(rows: [FlightRow])──> fr24_store.FR24Store.upsert_rows()
                                                                                   fr24_cache/fr24_cache.db
 build:  reconcile.build(cfg):
           store.iter_rows() ──> fr24_obs.build_observations(cfg, rows, cao_numbers=...) ──> ObsIndex([Obs])
           evidence.build_all_evidence(keys, index, cfg) ──> {RouteKey: Evidence(state, banks: [Bank])}
           fleets.recommend_fleets / audit_fleets
           reconcile.reconcile(cfg, plan, groups, gaps, vamsys, evidence, index) ──> BuildResult
           check_closure() -> models.dump_decisions(run_dir/'decisions.json', meta, decisions)
           store.upsert_targets(result.target_states)          (refresh tiers for next fetch)
 export: models.load_decisions(run_dir/'decisions.json') ──> (RunMeta, [Decision])
           export_xlsx.export_xlsx / export_csv.export_csv / export_html.export_html / report_summary.write_summary
```

`build` writes; `export` only reads `decisions.json` (re-exportable). Run dir: `cfg.new_run_dir()` =
`<season>/output/<YYYY-MM-DD_HHMM>/` (BJT clock); `cfg.latest_run()`, `cfg.resolve_run(name)`.

## Implemented modules

### `config`
- Data directory: `vcca_home()` = `$VCCA_HOME` (default `/srv/vcca`; `ConfigError` "数据目录不存在" when missing) — the ONLY path setting. `layout(home=None) -> Layout(home)` with `settings` (`settings.toml`), `settings_history` (`.config_history/`), `seasons`, `rules`, `cache`, `state`, `vamsys_dir`, `vamsys_db` (`vamsys/workspaces.db`); shortcuts `seasons_dir()`, `rules_dir()`, `state_dir()`, `settings_path()`, `vamsys_db_path()`, `fr24_cache_dir()` (all read the environment at call time).
- `load_config(season=None, *, strict=True, derive_dates=True, home=None) -> Config` — season = folder name/path under `seasons/`; default = latest (`discover_seasons()`: folders with `season.toml` OR a standard CAAC plan file, `is_season_dir`). Merge per key: `DEFAULTS` → `settings.toml` (site sections `SITE_SECTIONS`; `[season]` there is warned about and ignored) → `season.toml` (optional; `[season]` + overrides). `REMOVED_KEYS` (`[vamsys] db_path`, `[fetch] cache_dir`) are warned about and ignored. `strict=False` downgrades missing plan files / DB to `cfg.warnings`. Raises `ConfigError(errors: list[str])`; empty `workspace_id` → "请在数据页上传 workspaces.db 并选择工作区".
- `effective_values(season=None, *, home=None) -> {rows: [{section, key, value, default, source: default|site|season, source_label, changed}], warnings, settings_file, season_file, season}` — works without validation (web "生效值" view); `default_values()`.
- `migrate_config(old_season_toml, season_name=None) -> Migration(season_name, settings, season, dropped, settings_text, season_text)` — keeps only keys that differ from the defaults (CLI `migrate-config --from F [--write] [--force]`); `dump_toml(sections, header)`, `toml_value(v)`.
- `Config` (frozen): `season_name, season_label, season_dir, start_date, end_date, dates_source, plan_files, plan_groups, plan_tz_offset_min, vamsys_db, workspace_id, collection, target_ops, cargo_ops, number_prefix, airport_alias, airport_names, effective_time_mode, default_cost_index, fetch: FetchSettings, thresholds: Thresholds, airport_rules: AirportRules, fleet_rules: FleetRules, cache_dir, output_root, warnings, raw (season.toml), home, settings_file, site_raw, sources ('sect.key' -> 'site'|'season')`;
  props `cache_db, season_start_ts, season_end_ts`; methods `effective_time(now=None)`, `new_run_dir(now=None, create=False)`, `list_runs()`, `latest_run()`, `resolve_run(name)`, `callsign_to_number()`, `number_to_callsign(op, num)`, `op_of_callsign()`, `vamsys_airport(code)`, `is_cargo_op(op)`, `map_ac_to_fleet(op, ac)`.
- `Thresholds` (plan 5.9 + extras: `micro_split_merge, extra_slot_separation, extra_slot_min_acts, dst_shift_min, bank_weight_actual, bank_weight_recent, real_time_round_min, never_flown_lookback_days, rotation_gap_add_min_acts`); pairs are `(dep, arr)` tuples; helpers `Thresholds.within(pair, dd, da)`, `bank_merge_ok(dd, da)`, `as_dict()`.
- `AirportRules`: `grade(icao) -> str|None` (None = unrated), `grade_or_fallback`, `route_min_grade(o, d) -> (g_o, g_d, g_min, unrated_tuple)`, `airport_allows_b752`, `route_allows_b752`, `route_fleet_exclusions(o, d)`; data `airport_grade, high_plateau, historic_b757, fleet_exclusions, fleet_groups`.
- `FleetRules`: `cca_narrowbody['3C'|'4C'|'4D']`, `cca_widebody['4D'|'4E'|'4F']` (fully expanded sets), `fixed_fleets[op]`, `dirty`, `air_force`, `excluded`, `cargo_ac_codes`, `widebody_fd2_extra`, `ac_to_fleet_pax`, `ac_to_fleet_cargo`, `nb_all`, `wb_all`, `map_ac_to_fleet(op, ac, cargo=)`.
- `ensure_flightradarapi_on_path()`; credentials only from the environment: `fr24_credentials() -> (user, pw) | (None, None)` (FR24_USER / FR24_PASSWORD; systemd EnvironmentFile `/etc/vcca/vcca.env`), `credential_status(cfg=None) -> {'source': 'ENV'|None, 'checked': [(label, has_user, has_pw)]}` (never values).
- `text_overrides({path: text})` context manager: `load_config` / `effective_values` read those TOML files from the given text instead of disk (web config editor validates unsaved text without writing; works for files that do not exist yet).

### `util_time`
`parse_hhmm, fmt_hm, norm_hhmm, circ_diff, abs_circ_diff, hm_circ_diff, duration_min, fd3_to_days, days_to_fd3, vdays_to_days, days_to_vdays, days_to_zh, shift_days, local_to_utc(local_min, tz, day_offset) -> (utc_min, day_shift), local_days_to_utc, ts_to_dt, ts_utc_weekday, ts_utc_hm, ts_utc_min, ts_utc_date, ts_utc_str, round_ts, local_midnight_utc_ts(date, tz), next_local_midnight_utc(now, tz), monday_anchor_ts, week_index(ts, anchor_ts), iso_week_key`.

### `caac`
`load_plan(cfg) -> PlanData`, `iter_plan_rows(path)`, `parse_row_legs(row, ...) -> [PlanLeg]`, `group_legs(legs, alias) -> {key: PlanGroup}`, `build_slots(legs) -> [PlanSlot]`, `detect_rotation_gaps(legs, prefix) -> {key: RotationGap}`, `fd23_fd24_modes(paths, ops)`, `fd5_as_utc_alternative(leg) -> (dep, days)`, `bjt_block_min(leg)`, `legacy_leg_dict(leg)`.
Parity: legs equal legacy `load_raw_preplan()` field-by-field (3475 rows, 3571 legs, 2087 numbers, 29 gaps).
`PlanGroup.raw_slots` = raw plan slots (identical UTC dep/arr merged, no FR24 calibration); single-day legs are kept in `legs` (`single_day_legs`, `is_single_day_only`).

### `vamsys`
`load_vamsys(cfg) -> VamsysSnapshot(workspace_id, workspace_name, revision, storage_revision, airports{code: VAirport}, fleets{id: VFleet}, aircraft[VAircraft], routes[VRoute], routings)`; views `commercial_routes, special_routes, routes_by_key(), route_by_id(), has_airport(code), fleet_labels(fids), registrations_by_fleet(), counts()`; `read_revision(cfg)`, `connect_ro(path)`, `parse_route(row, ops)`.
`VRoute.is_special` = type training/repositioning, op not in target ops, or tag MIL/TRAINING (`special_reason` says which).

### `models`
Dataclasses: `PlanLeg, PlanSlot, PlanGroup, RotationGap, PlanData, VRoute, Obs, Bank, Evidence, TargetState, Reason, RouteTarget, FieldChange, EvidenceSummary, Decision, RunMeta`.
Enums (StrEnum): `EvidenceState, StoppedKind, DecisionCategory, Confidence, BodyTier, TargetKind, Priority, ReasonCode`.
Tables: `CSV_FILE[category]`, `VROUTE_CATEGORIES`, `CATEGORY_ZH`, `REASON_TEXT`.
Functions: `describe_reason(reason)`, `describe_reasons(list)`, `dump_decisions(path, meta, decisions)`, `load_decisions(path)`.
`Decision.add_reason(code, review=False, **params)` appends a reason and (review=True) sets `needs_review` + `review_flags`.

## Stub modules (to implement; signatures fixed)

| Module | Step | Public API |
|---|---|---|
| `fr24_client` | 3 | `FR24Client(cfg).login/flight_list(query, kind, page)/airport_board/search/airport_info/close`; `FetchResult`, `AirportInfo`, `FR24Error/FR24AuthError/FR24RateLimited`; `normalize_flight_list_item`; `ROW_KEYS`; `credentials_available`; session cache `default_session_cache_path()` = `$VCCA_HOME/state/fr24_session.json` (0600) (done) |
| `fr24_store` | 3 | `FR24Store(db_path, readonly)` / `.open(cfg)`: `init_schema, upsert_rows, iter_rows, count_rows, log_fetch, last_fetch, get_target, iter_targets, upsert_targets, delete_targets, upsert_airport_info, airport_info, iata_to_icao, get_meta, set_meta, search_index, stats`; `synthetic_id`; `validate_db(path)` (web import: schema + quick_check). The DB is the only FR24 source — no JSON import/fallback (done) |
| `fr24_fetch` | 3 | `derive_targets, plan_fetch -> FetchPlan, run_fetch -> FetchRunReport, fetch (CLI)`; `TokenBucket` |
| `fr24_obs` | 4 | `build_observations(cfg, rows, cao_numbers=, iata_to_icao=, search_index=, fetched_numbers=, fetched_callsigns=) -> ObsIndex`; `build_attribution_context`, `attribute_row`, `normalize_airport`; `is_actual_row` (done) |
| `evidence` | 4 | `build_evidence(key, index, cfg)`, `build_all_evidence`, `cluster_banks`, `drop_superseded`, `time_pairs`, `unplanned_keys`, `fd5_utc_check -> Fd5Check` |
| `fleets` | 5 | `recommend_fleets(cfg, op, orig, dest, route_type=, plan_fd2=, fr24_fleet_ids=, vamsys_fleet_ids=) -> FleetRecommendation`, `audit_fleets -> FleetAudit`, `body_tier_from_codes`, `fleet_usage` |
| `reconcile` | 5 | `reconcile(...) -> BuildResult` (pure), `calibrate_slots -> [CalibratedSlot]`, `pair_slots`, `check_closure`, `apply_decisions`, `target_states_from`, `build(cfg) -> run_dir` (CLI) |
| `export_csv` | 6 | `export_csv(run_dir, meta, decisions, cfg, vamsys) -> [Path]`, `decision_to_row`, `validate_rows`; `COLUMNS`, `STEM_CATEGORIES` (data, done) |
| `export_xlsx` | 6 | `export_xlsx(run_dir, meta, decisions, cfg, vamsys=None) -> Path`; `SHEETS` |
| `export_html` | 6 | `export_html(run_dir, meta, decisions, cfg, previous_run_dir=None) -> Path`, `diff_runs` |
| `report_summary` | 6 | `summarize, write_summary, print_summary`; `setup_run_log` (done) |

Parallel-work boundaries: step-3 agents own `fr24_client/fr24_store/fr24_fetch`; step-4 `fr24_obs/evidence`;
step 5 `fleets/reconcile`; step 6 exporters. Shared types live only in `models.py` — add new shared
types/enum members there (append-only; don't rename existing fields). New `ReasonCode`s need a `REASON_TEXT`
entry (a test enforces it).

## FR24 row (`FlightRow`) — store ⇄ obs contract

`{"id", "number", "callsign", "airline_icao", "origin_icao", "origin_iata", "dest_icao", "dest_iata",
"aircraft_code", "registration", "sched_dep", "sched_arr", "real_dep", "real_arr", "status_live",
"status_text", "status_generic"}` (+ `"source"`, `"first_seen"`, `"last_seen"` from the store).
Same shape as legacy `flight_histories.json` rows, so fixtures can be cut from the old cache.

## decisions.json schema (version 1)

```jsonc
{
  "schema_version": 1,
  "meta": {                                   // models.RunMeta
    "season": "2026-27-1", "season_label": "2026-2027 冬春",
    "generated_at": "2026-11-26 01:30:00",    // UTC
    "effective_time": "2026-11-26 16:00:00",  // End/Start Date written to CSVs
    "season_start": "2026-10-25", "season_end": "2027-03-27",
    "plan_rows": 3475, "plan_legs": 3571, "plan_numbers": 2087,
    "vamsys_revision": 26056, "vamsys_storage_revision": 26055,
    "vamsys_routes": 2369, "vamsys_commercial_routes": 2364,
    "fr24_rows": 0, "fr24_latest_ts": null, "fr24_last_fetch": "",
    "thresholds": {"recent_window_days": 21, "bank_merge": [[45, 60], [20, 75], [65, 20]], ...},
    "threshold_hits": {"time_override_min_acts": 37, ...},
    "counts": {"UPDATE": 120, "KEEP": 1800, ...},
    "warnings": []
  },
  "decisions": [{                             // models.Decision
    "category": "UPDATE",                     // DecisionCategory
    "vroute_id": "11507830",                  // VAMSYS route ID; required for VROUTE_CATEGORIES, else null
    "target":  {"op": "CCA", "callsign": "CCA1109", "number": "CA1109", "orig": "ZBAA", "dest": "ZBXH",
                "type": "scheduled", "dep": "02:55", "arr": "04:20", "days": [0,1,2,3,4,5,6],
                "fleet_ids": ["26071", "..."], "cost_index": 80, "hidden": false,
                "start_date": "", "end_date": ""},
    "current": { ...same shape, VAMSYS before change... } | null,
    "changes": [{"field": "days", "old": [6], "new": [0,1,2,3,4,5,6]}],   // RouteTarget attribute names
    "reasons": [{"code": "MICRO_SPLIT_MERGED", "params": {"n": 2}}],      // ReasonCode + params
    "evidence": {"state": "active", "acts": 40, "obs": 44, "recent_acts": 18, "last_act": "2026-11-24",
                 "banks": [{"dep": "02:55", "arr": "04:20", "fd3": "1234567", "acts": 40, "obs": 44, "recent_obs": 18}],
                 "ac": {"AJ27": 30}, "regs": [], "dates": [], "other_ops": [], "other_ods": []},
    "confidence": "high",                     // high | medium | low
    "needs_review": false,
    "review_flags": [],                       // ReasonCode values that set needs_review
    "group_key": ["CCA", "CCA1109", "ZBAA", "ZBXH"] | null,
    "plan_refs": ["国内航司国内航班计划（三字码）.xls:2315"],
    "depends_on_airports": [],                // AIRPORT_ADD ICAOs this route needs first
    "slot_index": "1/1",
    "details": {}                             // category-specific JSON (AIRPORT_ADD: {"name": ...}; CHARTER: ...)
  }]
}
```
Per category: RETIRE* → `target` = current with `end_date = meta.effective_time`; CREATE_* → `vroute_id` null,
`target.start_date = meta.effective_time`, `cost_index = cfg.default_cost_index[op]`; AIRPORT_ADD →
`target.orig = ICAO`, other route fields empty, `details.name`. Closure: every commercial VAMSYS route ID
appears in exactly one decision whose category ∈ `VROUTE_CATEGORIES`.

## Web front end (`webapp`, `jobs`, `status`, `web/`)

### `status`
- `collect_status(season=None) -> dict` — everything `vcca_plan.py status` shows, JSON-able: `{ok, season, config{..., home, settings_file, warnings}, plan{files[{name,exists,size,mtime}], ok, rows, legs, ...}, vamsys{path, exists, ok, revision, counts, unrated, error?}, credentials{source, checked[{path,user,password}]}, cache{dir, db, stats{rows,last_fetch_at,targets_by_priority,stale_targets}|None}, runs{count, latest, ...}}`; config error → `{ok: False, error, errors}`. Epoch `mtime` plus the CLI's local `*_fmt` strings.
- `print_status(d) -> int` prints the CLI text. `vcca_plan.collect_status` re-exports it.

### `export_html` (additions)
- `payload_for_run(cfg, run_dir, *, prev="auto"|run name|path|None, vamsys=None) -> dict` — `load_decisions` + previous run (`previous_run(cfg, run_dir)` for "auto") + `build_payload`; shared by export and the web app. Payload `csv[]` entries now also carry `key` (file key → name).
- `render_html(payload)` inlines `vcca/web/report.css` + `report.js` (`report_assets()`) into a single offline file; the JS is `VccaReport.mount(rootEl, payload, opts)`.

### `web/report.js` — `VccaReport`
- `mount(root, payload, opts) -> {go(tab), destroy()}`; `opts`: `header`, `themeButton`, `hash` (report.html: all true), `tab`, `onTab(id)`, `fileUrl(name)` (CSV names become links), `diffRuns[]` + `onDiffChange(run)` (compare with any earlier run). No fixed element ids. Theme helpers `applyTheme/getTheme/setTheme` (localStorage `vcca-theme`, `<html data-theme>`), CSS tokens in `report.css` shared with `app.css`.

### `jobs`
- `JobManager(state_dir, *, python=sys.executable, script=<code dir>/vcca_plan.py, grace_s=10, kill_s=5)`: `start(cmd, season, opts, user=) -> job` (raises `JobBusy` when one is running, `ValueError` for anything not whitelisted), `current()`, `get(id)`, `list(limit)`, `read_log(id, since=byte) -> {text, next, job}`, `cancel(id)`, `wait(id, timeout)`, `shutdown()`.
- `build_argv(cmd, season, opts)`: cmd ∈ status/fetch/build/export/run/test; opts fetch `deep, dry_run, search, flights, regs, boards`; export `run`; run `deep`; test `verbose`.
- Files `STATE_DIR/jobs/<id>.log` (merged stdout+stderr) and `<id>.json` (`status` running/done/failed/cancelled/interrupted, `rc`, `started/ended`, `user`, `progress{done,total,elapsed,eta}` from fetch's `[done/total] … 已用 … 预计剩余 …`, `run_dir` from `decisions.json -> …` / `输出目录 …`).
- Interrupt (Linux): `start_new_session` + `os.killpg(pid, SIGINT)` (fetch saves progress like Ctrl+C); then terminate after `grace_s`, kill after `kill_s`. Where `os.killpg` is missing (Windows dev machines) cancel just terminates.

### `webapp`
- WSGI `application` (gunicorn `vcca.webapp:application`, **1 worker**, threads) / `create_app(state_dir) -> App` (state dir default `$VCCA_HOME/state`); `vcca_plan.py serve` runs it on wsgiref + threads, 127.0.0.1 only (development).
- Accounts `STATE_DIR/users.toml` (`hash_password` PBKDF2-SHA256, `set_user`, `delete_user`, `load_users`); sessions `STATE_DIR/sessions.json` (sha256 of the token), cookie `vcca_session` (HttpOnly, SameSite=Strict, Secure over HTTPS / `X-Forwarded-Proto` when `VCCA_TRUST_PROXY=1`), 12 h; writes need header `X-VCCA-CSRF`; failed logins locked out per IP (2^(n-3) s, max 300).
- API: see the module docstring (`/api/login|logout|me`, `/api/seasons`, `/api/status`, `/api/runs[/<run>[/report|/zip]]`, `/files/<season>/<run>/<file>`, `/api/jobs[/<id>[/cancel]]`, `/api/config[/history|validate|save|restore]`, `/api/data`, `/api/upload/vamsys|plan|fr24cache`, `/api/vamsys/workspace`). `/api/config` also returns `effective` (`config.effective_values`). Errors are JSON `{error, ...}` with 400/401/403/404/409/413/422/429.
- Config files (`CONFIG_KEYS`): `settings` ($VCCA_HOME/settings.toml), `season`, `airport_grades`, `fleets`. Save: mtime check (409) → TOML syntax (line/col) → full `load_config` via `text_overrides` (settings.toml without any season: syntax + known keys only) → backup to `$VCCA_HOME/.config_history/`, `<season>/.config_history/` or `rules/.history/` → atomic write → re-check from disk (restore backup on failure).
- Uploads (raw request body, `X-Filename` for .xls; fixed limit `MAX_UPLOAD_BYTES` = 512 MB): VAMSYS — SQLite header → temp file → `vamsys.slim_copy` (only `workspaces`, `workspace_revisions`, `resource_rows` + their indexes; `secrets`, `app_settings`, `batch_*`, `resource_history_*`, `resource_metadata` dropped; VACUUM) → every workspace loaded with `vamsys.load_vamsys` → old file renamed `workspaces.<time>.db` (5 kept) → atomic replace; response lists `workspaces[{id,name,revision,storage_revision,routes,ok}]`, `selected` (`vamsys.pick_workspace`: configured, else named "vCCA", else the only one), `needs_selection`. `POST /api/vamsys/workspace {season, workspace_id}` writes `[vamsys] workspace_id` into settings.toml through the config save path. FR24 cache — SQLite header → `fr24_store.validate_db` → WAL checkpoint of the old file → renamed `fr24_cache.<time>.db` (3 kept) → atomic replace. Plan — OLE header + `caac.iter_plan_rows` → old copy to `<season>/.plan_history/` → optional `plan_files` (+ `plan_groups`) update through the config save path. Both refused (409) while a job runs.
- Security headers: CSP `default-src 'self'` (no inline script), `X-Content-Type-Options`, `Referrer-Policy`, `X-Frame-Options`; path parameters regex-checked and files served only when listed directly in their run folder.
