# Changelog

All notable changes to this project are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning: [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] — 2026-10-09

First public snapshot: an ontology-driven RCA agent **plus** the benchmark that measures it.

### Added

- **Fault-injection catalog** — 21 scenarios across application / database / cluster-resource
  layers, each declaring its signal, blast radius, expected root cause and the alerts it
  should raise. Single-flight, confirm gate for high/critical, always-available abort.
- **Verification harnesses** (the point of the project):
  - `fault_verify` — inject → assert declared signal → recover → assert recovery
  - `alert_coverage_check` — hold = max rule `for` + 60 s, then: did the *declared* alert fire?
  - `cluster_rca_verify` — end-to-end inject → stress → alerts → diagnose → score
  - `rootcause_promotion_verify` — paired A/B of component→root-cause promotion
  - `heldout_verify` — measures overfitting on inputs that were never tuned on
  - `crosscluster_verify` — is the root cause attributed to the right cluster ("who to call")?
  - `remediation_efficacy` — does executing the remediation actually clear the criterion?
  - `run_all_verification` — the whole pipeline (14 steps incl. rollback + clean assertions)
- **Dual-engine diagnosis** — deterministic ontology fast path (0 tokens) with an LLM agent
  fallback for open-ended questions; routing decisions are recorded and explained.
- **Ontology evolution** (`vendor/EvoOntology`, MIT) — versioned rounds with paired evaluation
  and a publication gate. Rounds shipped: cluster → scoring → hygiene → remediation →
  command-targets → memory (`ontology_v6`: 56 terms / 87 relations / 30 constraints / 22 evidence).
- **Remediation execution loop** — read-only diagnostics auto-run; writes require `--approve`,
  carry an inverse operation and a post-check, and **auto-roll back** when the check fails.
  Default is dry-run.
- **Cost dashboard** with per-model exact costing (separate input/output token columns,
  legacy rows reported separately as unpriced instead of being silently averaged).
- **Console**: chaos / stress / cost / diagnosis tabs; token field; "full sweep" opt-in;
  running-job state persisted across tab switches and reloads; optional DSH-GUI plugin.
- **Docs**: verification handbook (design + every finding, including self-corrections),
  ontology integration guide, Linux migration guide.

### Changed

- Alert rules 21 → 23 (`AppContainerOOMKilled`, `MySQLPrimaryReadOnly`), with rule
  `rca_hint` annotations de-guessed and de-coupled from a single cause.
- **Slow-query threshold `0.1 s → 1 s` plus `min_examined_row_limit=10000`.** At 0.1 s the
  slow-query counter rose even while idle, and every normal `INSERT` (>100 ms) was counted —
  it became a proxy for traffic and falsely evidenced `rc:slow-sql`. Injected slow queries
  measure 2.2–170 s, so detectability is unaffected.
- Injection durations are time-bounded; `hold` is derived from the largest alert `for`
  instead of being hardcoded.
- Host ports bind to `127.0.0.1` by default (`CC_BIND` to widen deliberately).
- Grafana no longer ships a default password; anonymous access and sign-up disabled.
- `txnRows` is cached for 60 s and the `COUNT(*)` subquery is dropped from the probe SQL —
  a measured 20 → 5 executions/minute on a 4.8 M-row table.
- Portability groundwork for Linux Docker: `.gitattributes` (LF), SELinux `:z` labels on bind
  mounts, `init: true` for the backend/frontend containers, no hardcoded Windows paths.

### Fixed

- **`app_oom_kill` could not be observed at all** — the in-container `oom_kill` counter resets to 0 when the container is killed and restarted, so `increase(...[5m]) > 0` was permanently false (while the injection layer confirmed `OOMKilled=true`). Added a container-level exporter that reads the Docker API (the counter survives restarts), pointed the alert rule and the scenario criterion at it, and made the injection deterministic (32 MB limit / 512 MB allocation).
- **Inverted recovery criteria**: `recovered_signals` are the *symptoms that must disappear*, not conditions that must hold. `app_oom_kill` used `count(up == 1) >= 3`, which is true when healthy — so recovery could never be confirmed (the check timed out with the correct value already in the result).
- **Criteria that could never be observed** in 3 more scenarios: `db_replica_kill` (a `mysql_up=0` transient is invisible to a 15 s scrape while MySQL replica cold start takes ~167 s) and `db_slow_query_flood` / `db_row_lock_hold` (rate thresholds and windows too tight for the wait budget).

- **`db_replica_lag` recovery was a false negative** — it used `Seconds_Behind_Master` (the
  *aftermath*). Recovery now checks the configured `DESIRED_DELAY`, which is the fault itself.
- **Token budget was a soft limit.** It is now enforced *after every call*: exceeding it stops
  further calls and is reported (`budget_overshoot_tokens`).
- **Criteria windows longer than the verification wait** in 4 scenarios (`rate(...[2m])` inside
  a 75 s wait can never be true). Windows shortened to `[30 s]`; semantics unchanged.
- **`app_oom_kill` could do nothing and still report success** — the injection never read back
  whether the quota change applied, and its success did not depend on observing a kill. It now
  asserts the lever moved *and* requires the cgroup `oom_kill` counter to increase.
- **Console 502** — nginx cached the backend's IP at startup, so recreating the backend broke
  every page while all containers reported healthy. nginx now re-resolves via Docker DNS.
- **MySQL-mode stress always failed** (842/842 connection errors) — the harness defaulted to
  `127.0.0.1`, which inside the backend container is the container itself. Connection settings
  are now passed from the backend's own environment, and the target is probed before starting.
- Zombie processes accumulated in the backend container (unreaped health-check orphans) —
  fixed with `init: true`.
- Redundant index created by an over-broad remediation suggestion; the index that actually
  helps (`(status, amount)`) was measured and kept.

### Security

- Loopback-only port binding, optional `CHAOS_API_TOKEN`, `CHAOS_UI_ENABLED=0` kill switch,
  confirm gate, abort endpoint, and the invariant that the **P0 remediation action is always
  read-only** — enforced as an ontology publication gate.
- No real credentials committed; `.env` git-ignored; dataset is synthetic.
- See `SECURITY.md` for the threat model and the known gaps (no TLS, Docker-socket mount).

### Known issues

- **Overfitting, measured and published**: on held-out alert phrasings that were never tuned
  on, strict top-1 drops to **4/10 = 40 %** (vs 81–86 % on the tuned set). `rc:db-replica-loss`
  acts as an attractor. Fixing it requires paraphrase coverage — and must *not* be done by
  tuning on the same held-out set.
- The full 21-scenario end-to-end run must be re-executed with the corrected step budget
  (the previous run was cut off at 18/21 by a too-small timeout, now fixed).
- `innodb_row_lock_current_waits` is stuck at 1 in this environment; row-lock detection uses a
  rate instead.
- Injection is **Docker-only** and the database dialect is **MySQL-only**. VM/physical-host
  deployment and PostgreSQL/GaussDB support are designed but not implemented.
- `app-gateway` (nginx) still lacks `init: true` (nginx reaps its own children, no symptom yet).

[0.1.0]: https://github.com/shongcy-creator/ontology-rca-agent/releases/tag/v0.1.0
