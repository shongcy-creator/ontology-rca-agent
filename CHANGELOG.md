# Changelog

All notable changes to this project are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning: [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `tools/ontology_ab_replay.py` — replays the **real** alert texts archived by the e2e run
  (`.chaos/rca_diagnosis_report.json` → `alert_text`) against any two ontology versions and reports
  **paired strict hits**. Same inputs, so any delta can only come from the ontology.
- `tools/oom_evidence_counterfactual.py` — builds keyword-tightening variants of
  `con:oom-detection` and scores them on those inputs, to test whether a proposed tightening can
  actually move the number (instead of assuming it did).
- `tools/publish_reports.py` — publishes `.chaos/` artifacts into `reports/` with host paths
  scrubbed to `<repo>` / `<home>` / `<python>`. `--check` turns the promise in `reports/README.md`
  into a **gate** (a `X:\Users\<name>` path makes it exit 1); `--scrub FILE` cleans one in place.
- `tools/strict_score_check.py` — recomputes the **strict** score (measurable scenarios only) from
  any two e2e reports and asserts a change's blast radius: the exactly-improved / exactly-regressed
  sets and an alert's firing scope. Exits 1 when an assertion fails, so "19/20 → 20/20, one case
  changed, 0 regressions, alert fired only there" is one command instead of four claims.
- `rca-agent/backend/services/metric_discriminator.py` — the metric-plane rules for
  "memory pressure vs OOM-killed" (see **Changed** below); its decision is returned as
  `metric_discriminator` on `/api/rca/infer` and appended to the reasoning chain.
- `tools/oom_discriminator_verify.py` — offline regression test for those rules: 24 assertions over
  a stub Prometheus (not ambiguous / text hard evidence / kill observed / no kill / metric
  unavailable), so the OOM-vs-pressure judgement has a check that **can fail** without needing a
  cluster. `--live` prints the real counter.
- `tools/round_candidate_probe.py` — a **pre-publication screen** for candidate rounds: it compares
  a candidate (a built-in variant or `--candidate-records`) against the parent on three fixed input
  sets — the 21 scenarios in their offline form, the held-out exam (observed, never tuned on), and
  the archived real alert texts (strict hits). **Any regression exits 1**, so a candidate that would
  make things worse is stopped before a round is opened rather than after it is published.
- `tools/incident_heldout_eval.py` — turns the RCA evidence inbox
  (`<workspace>/rca_evidence_inbox.jsonl`, written since evidence injection stopped touching the
  active ontology) into a **retained evaluation set**: recorded vs recomputed conclusion (knowledge
  drift, with the ontology version the record was written under), strict hits where the alert text
  matches a known scenario verbatim, and a "knowledge gap" queue (confidence below the fast-path
  threshold / no candidate / no constraint support). Report-only — this material is never tuned on,
  and it **refuses to score ambiguous ground truth**: the same alert text mapping to scenarios with
  conflicting expectations (the collision fixed in this release) is reported, not silently counted
  as right or wrong.

### Findings — published as a negative result

- **The round-6 keyword tightening is invisible to the engine on real inputs.** Replaying the
  archived alert texts gives `ontology_v5 → ontology_v6` = **19/20 → 19/20 (Δ0, zero cases flipped)**,
  while the round's own gate recorded `0/9 → 9/9` on its `memory_semantics` invariants. The words were
  removed from the *constraint*, but the engine's conclusion is driven by the *Term* dictionary:
  `rc:oom-kill`'s own id/name/scoring-keywords still contain `oom` / `kill` / `被内核杀死`, and the
  text-fit term saturates at `min(weighted, 3)/3`, so `negative_keywords` cannot pull it below a
  competitor either. A stronger tightening is therefore a **no-op**, not an improvement.
- **The strict metric has a structural ceiling of 19/20 on the current benchmark.** `app_memory_stress`
  and `res_cluster_memory` receive a **byte-identical** alert text (both declare
  `AppContainerMemoryPressure`, so both get the same `rca_hint`), yet accept disjoint root causes
  (`{metric:mem-pressure, rc:oom-kill}` vs `{rc:cluster-capacity, metric:mem-pressure}`). One input
  yields one top-1, so at most one of the pair can hit — unless the engine concludes with an
  *observation* (`metric:mem-pressure`), which is the "conclusion lands on an observation" defect the
  role weights exist to prevent. Reaching 20/20 needs a change to the **ground truth** or to the
  **input**, not to the ontology; changing the ground truth would be the "edit the number to make the
  check green" move this repo refuses.
- **Live re-test confirms it**: injecting `res_cluster_memory` under the published `ontology_v6`
  (real alert, real deterministic engine) still returns `rc:oom-kill` → strict miss
  ([`reports/rca_retest_res_cluster_memory.json`](reports/rca_retest_res_cluster_memory.json)).
- `AppContainerOOMKilled` declares `onto_constraint: con:container-oom`, which **does not exist** in
  the ontology (`ontology_v6` has `con:oom-detection`), so that alert's wiring to the ontology is dead.

### Fixed

- **`db_conn_saturation` was unmeasurable because the fault blinded its own monitoring.** Two
  independent defects, both found by measuring instead of assuming:
  1. *Sizing.* Injecting 170 long-lived connections peaked at **197/200**, so the mysqld exporter's own
     scrape connection was refused: `mysql_up` → 0, a **false `MySQLDown` (P0)** fired, and
     `mysql_global_*` went stale — leaving `MySQLTooManyConnections` **impossible to evaluate**
     (`alert_coverage: False`, text fell back to "监控无对应告警"). The default is now **150**
     (measured **179/200 = 0.895**, above the 0.85 threshold, leaving **21 slots** for the exporter and
     for recovery). Re-measured in isolation: only the declared alerts fire, **no `MySQLDown`**, and the
     engine answers `rc:conn-exhaust` (strict hit).
  2. *Alert text.* `collect_alerts()` read `/api/v1/rules`, whose annotations are **unrendered rule
     templates**, so the engine was handed `MySQL 实例 {{ $labels.instance }} 不可用` — no node identity
     plus template noise. It now reads `/api/v1/alerts` (instantiated alerts: real labels, substituted
     annotations), and the report records `instance`/`db_role` alongside a rendered summary.
  The **0.85 threshold was not the problem and is unchanged** — the earlier "阈值贴边" explanation for
  this scenario was incomplete, and the rule comments now carry the measured reality (including that a
  simultaneous occupancy alert means the *exporter* was refused, not the database).
  **Measured after the fix** (full 21-scenario run, console disabled, guard reporting no
  interference): 21/21 injected, **alert coverage 21/21** (the database layer went 8/9 → 9/9),
  21/21 diagnosed, 21/21 recovered, 0 tokens — and **strict 21/21 with nothing excluded from the
  denominator**, the scenario itself answering `rc:conn-exhaust`
  ([`reports/rca_diagnosis_report_connfix.json`](reports/rca_diagnosis_report_connfix.json)).
  Only two cases differ from the previous full run: `db_conn_saturation` (unmeasurable →
  `rc:conn-exhaust`) and `db_replica_kill` (now the more specific `rc:db-replica-loss`, also in its
  expected set — a direct consequence of the alert text finally carrying the real node identity).
- **A sweep now refuses to be counted when something else moved the cluster.** The end-to-end
  harness checks, right before it calls the agent, that the *only* active fault is the one it
  injected itself (`interference.expected_active` vs `observed_active`). On a mismatch it warns,
  records `interference` per scenario plus `stopped_by_interference` / `complete: false` and an
  `interference.scenarios` list at the top level, **stops the sweep**, rolls back, and exits
  non-zero instead of blending the scenario into a "complete-looking" summary. Verified in both
  directions: a clean scenario reports `外部干扰 无` and exits 0, while an externally recovered fault
  (`recover` issued 35 s into the hold) is caught and exits 3. The second test is the reason this
  guard exists: **that polluted run still answered correctly** (`rc:cpu-throttle`), so "the number
  looked fine" was never evidence that the input was still the one we injected.
- **The metric-plane judgement is now visible where the measurement happens.** `POST
  /api/agent/diagnose` returned only the conclusion, so a report could say "answered
  `rc:mem-pressure`" without showing which rule decided it or what the counter read. The response
  (top level and under `seed`) and each `diagnosis_runs[]` entry in the e2e report now carry a
  compact `metric_discriminator` digest — `applicable`, `rule`, `winner`, `why`, the text hard
  evidence / hypothetical hints, and the metric reading (`available`, `increase`, `max_increase`,
  `kill_observed`) with the raw series stripped out. This is what makes the OOM-vs-pressure call
  auditable in the live criterion, not just reproducible in the unit test. (The already-published
  [`reports/rca_diagnosis_report_discriminator.json`](reports/rca_diagnosis_report_discriminator.json)
  predates this field — its `app_memory_stress → rc:mem-pressure` line can be cross-checked against
  [`reports/rca_retest_metric_discriminator.json`](reports/rca_retest_metric_discriminator.json) and
  the 24/24 rule test; a re-run is what records the digest inline.)
- **`docker compose` was silently running the image's baked code instead of the host mounts.**
  On this host with Compose **v5.5.1**, the short-syntax volume `./backend:/app/backend,z` is parsed
  as `target: /app/backend,z` (verified with `docker compose config --format json`): the bare `,z`
  label becomes part of the **path**, so the real `/app/backend` stays the copy baked into the image
  and the container runs **stale code while reporting `healthy`**. It bit a re-run —
  `POST /api/agent/diagnose` returned `root_cause: null` because the newly added
  `metric_discriminator` was missing from the image. Evidence: the container's mount table showed
  `D:\ on /app/backend,z`; `grep -c metric_discriminator /app/backend/services/rca_engine.py` read
  **0 before** and **7 after** the fix; `/app/backend/services` went 13 → 18 files.
  The three bare-`,z` mounts now use **long syntax** (`type: bind` + `bind.selinux: z`), which is
  unambiguous and keeps the SELinux label the Linux migration needs (`:ro,z` parses correctly and is
  unchanged). Every mount is verified by rendering the config and checking the targets. Anyone
  recreating services with this CLI should check for a `,z` suffix on the target.
- **The environment tools no longer point at the pre-cluster container.**
  `env_container_inspect`'s schema advertised `cc-credit-card-app` (the single-container name from
  before the cluster rounds), and a failed lookup answered with a bare `no such object: …` — so an
  agent calling the tool with its own example argument got nothing and could not self-correct. That
  was one of the two failures in the LLM regression. Now:
  - the `container` description is derived from the deployment env (`RCA_APP_SERVICE` /
    `RCA_APP_REPLICAS` / `RCA_APP_GATEWAY`) and points the caller at `env_containers` when unsure;
  - a miss returns the **available container names** instead of just `no such object`;
  - the agent's domain-knowledge prompt and the "资源" remediation suggestions no longer name the old
    container;
  - `tools/agent_tools_verify.py` resolves its target from `env_containers` rather than hardcoding a
    name.

  Verified: `tools/agent_tools_verify.py` → **42 passed / 0 failed** (was 39/2), invocation summary
  `invoked=24 ok=24 fail=0`, auto-selected `rca-agent-payment-app-1`; the schema served by the running
  backend matches too. `tools/init_evo_ontology.py` still names `cc-credit-card-app` **on purpose** —
  it seeds `ontology_v0` from the legacy `demo/topology-snapshot.json`, so those strings are
  provenance, not stale runtime references.
- **RCA evidence injection no longer switches the active ontology.**
  `RCAEngine._inject_evidence()` used to load `ontology_v0`, append the evidence record, save it as
  `ontology_v0-rca-agent` and call `SemanticStore.set_active(...)` — so a single
  `POST /api/rca/infer` (whose `inject_evidence` defaults to **True**) silently downgraded the live
  knowledge base to "v0 + one evidence record". It now:
  - appends the record to `<workspace>/rca_evidence_inbox.jsonl` (with the ontology version it was
    produced under), and **does not touch `active.json` or any version directory** — which ontology
    the engine answers from may only change through a versioned round + publication gate, never as a
    side effect of one inference;
  - keeps the old behaviour available behind an explicit `RCA_EVIDENCE_ACTIVATE=1`, and in that case
    bases the write on the **current** active version (not `ontology_v0`), with the target overridable
    via `RCA_EVIDENCE_VERSION`.

  Verified against the running backend: `POST /api/rca/infer` → HTTP 200, `active.json` still
  `ontology_v6`, `versions/ontology_v0-rca-agent/*` mtimes unchanged, one record in the inbox, and
  the log line `Evidence recorded: ev:rca-inc_… -> rca_evidence_inbox.jsonl（active 仍为
  ontology_v6；不切换本体）`. The same holds after running the six LLM scripts (including the agent
  loop that drives backend tools).
- **The input collision behind the last strict miss.** `res_cluster_memory` (cluster-wide: all 3
  replicas × 220 MB) declared `AppContainerMemoryPressure`, whose `rca_hint` is worded as a
  *per-container* risk ("存在 OOM Kill 风险") — so it was fed a **byte-identical** input to
  `app_memory_stress` (single replica × 200 MB) while accepting a different root cause.
  Added the missing **cluster-level** alert `AppClusterMemoryCapacity`
  (`count(working_set/limit > 0.85) >= 3`, `onto_constraint: con:cluster-capacity`) and made the
  scenario declare it. A single-replica stress cannot satisfy `>= 3`; the live re-test confirms it —
  the new alert fires for `res_cluster_memory` and **not** for `app_memory_stress`
  ([`reports/rca_retest_oom_input_fix.json`](reports/rca_retest_oom_input_fix.json)).
- **Verified on a full 21-scenario re-run** (real injection + real alerts + real engine, same
  `ontology_v6`, same ground truth): strict **19/20 → 20/20**, with **exactly one case changed**
  (`res_cluster_memory`: `rc:oom-kill` → `rc:cluster-capacity`) and **0 regressions**
  ([`reports/e2e_postfix_ab.json`](reports/e2e_postfix_ab.json),
  [`reports/rca_diagnosis_report_postfix.json`](reports/rca_diagnosis_report_postfix.json)).
  The new alert fired for `res_cluster_memory` **only**, and the single-replica control
  (`app_memory_stress`) kept `rc:oom-kill`. The `+1` comes from fixing the **input**, not from a
  keyword edit — round 6's ontology tightening moved nothing on the same kind of replay
  ([`reports/oom_evidence_replay.json`](reports/oom_evidence_replay.json)).
- **Host paths and a username were baked into tracked files.** The new report artifacts embedded the
  interpreter path (`C:\Users\<user>\...\python.exe`) in the recorded stress command —
  `tools/publish_reports.py` now rewrites it to `<python>`. The same absolute path appeared in
  `config/evoontology.cordis.yml`, `tools/mcp_test.py`, `tools/restart_and_test.py` and the ontology
  integration guide; those now use a PATH-resolved interpreter (`python`, overridable via
  `CC_PYTHON`) and repo-relative paths, so no committed file carries a username or a host layout.
  The MCP overlay must now be launched **from the repo root** (its `cwd`/`PYTHONPATH`/implicit
  `--store` are relative; the server's store default is `<cwd>/.evoontology`).
- **Hardcoded host paths in scripts (portability, no identity leak): 21 files / 25 occurrences.**
  Sixteen `tools/*.py`, `generate_ontology_review.py`, `generate_ontology_ttl.py` and
  `rca-agent/run_local.py` pinned the repo to `D:\05_code\credit-card-sys-ops`; four scripts
  (`tools/chaos/core.py`, `tools/alert_firing_verify.py`, `tools/cluster_bootstrap.py`,
  `tools/fault_drill.py`) pinned Docker to `C:\Program Files\Docker\...\docker.exe`. All now derive
  paths from `__file__` and resolve tools via `PATH` (`CC_PYTHON` / `CC_DOCKER` override). The
  migration guide's "fixed" table was one-sided — it listed 2 files while 21 were affected — and is
  now corrected. Every touched script was re-run; 18 executed clean, 1 exceeded its cap, 8 are
  deliberately not executed with the reason recorded
  ([`reports/host_path_regression.json`](reports/host_path_regression.json)).

### Changed

- **The OOM-vs-pressure call moved to the metric plane.** The deterministic fast path decided
  "memory pressure" vs "was OOM-killed" purely from the alert text, which cannot separate *at risk*
  from *already happened* (round 7 proved that no dictionary tuning fixes it — the same sentence
  contains both "cgroup oom_kill 计数增加" and "内存超限被内核杀死"). `RCAEngine.infer()` now runs
  `backend/services/metric_discriminator.py` after text scoring, with rules that are recorded in the
  result as `metric_discriminator` (and in the reasoning chain):
  1. not ambiguous (both terms absent, or top-1 is neither) → **do not intervene**;
  2. the text states a kill already happened (`OOMKilled` / `oom_kill 计数增加` / `已被内核杀死`) →
     `rc:oom-kill`, **without querying Prometheus**;
  3. otherwise, if the metric is available → `increase(cc_container_oom_kill_total[5m]) > 0` means
     `rc:oom-kill`, `== 0` means `rc:mem-pressure` (the term `ontology_v6` added for exactly this);
  4. metric unavailable → **do not intervene** (keep the text order) rather than guess.

  Rule 2 outranks the metric on purpose: the counter is monotonic and sticky across scenarios
  (a documented hazard), so a text that states a kill is the more reliable signal, while
  hypothetical wording ("存在 OOM Kill 风险，可能被内核杀死") falls through to rule 3.
  Verified: 24/24 rule assertions offline (`tools/oom_discriminator_verify.py`, stub Prometheus);
  live `/api/rca/infer` → kill text = `rc:oom-kill` / rule 2 (no metric query), risk text =
  `rc:mem-pressure` / rule 3 with `increase(...[5m]) = 0.0`; real faults
  ([`reports/rca_retest_metric_discriminator.json`](reports/rca_retest_metric_discriminator.json)):
  `app_oom_kill` → `rc:oom-kill` (strict hit), `app_memory_stress` → `rc:mem-pressure`;
  engine smoke tests unchanged (`test_rca_engine.py`, `agent_tools_verify.py` 42/42).

### Findings — a benchmark label now disagrees with the engine, and the engine has the evidence

- With rule 3 in place, `app_memory_stress` (single replica, 200 MB, "逼近 cgroup 上限, OOM 前兆")
  is answered as `rc:mem-pressure`, which is **what `ontology_v6` defined that term for**. The
  benchmark's expected terms for that scenario (`{metric:mem-pressure, rc:oom-kill}`) do not include
  it, so the strict score for a full re-run becomes **19/20** instead of 20/20.
  The container evidence says the *label* is the stale part: across the archived full run and this
  re-test, every replica reports `oom_killed = false` during that scenario — the single
  `restart_count = 1` is the documented sticky residue from an earlier kill (the counter never
  resets). **No ground truth was edited**: correcting `app_memory_stress` to accept
  `rc:mem-pressure` is a benchmark decision, reported here and left pending.

### Findings — two measurement attempts were invalidated before one could be trusted

- **The fault-injection console was driving the same cluster while a sweep was measuring it.**
  The backend log shows write calls from the browser side (via the frontend container):
  `172.18.0.11 POST /api/chaos/recover` at 11:32:42 and `POST /api/chaos/inject` at 11:33:31 — i.e.
  it **rolled back a fault the sweep was holding** — plus another `app_gateway_stop` injection at
  11:56 while the backend was being repaired. Two sweeps were **killed rather than reported**
  (the harness injects docker-direct and never creates console jobs, so the two drivers were
  independent). The console was then disabled for the duration with the documented kill switch
  (`CHAOS_UI_ENABLED=0`, write endpoints return 503, `{"busy": false, "jobs": []}`), and the restart
  of the sweep picks up and cleans any residual fault before it settles.
- **A sweep can look healthy while measuring nothing.** The stale-code bug above produced a run whose
  first scenario reported `ok=False, root=-` in 2.5 s; the honest response was to stop it (the third
  start, after the mount fix, produced `rc:cpu-throttle` in 3.3 s and a normal 380 s scenario).
  Both stops are recorded here rather than being counted as results.

### Findings — a benchmark label was corrected by evidence (and the offline criteria's boundary is now explicit)

- **Measured after the correction** — full 21-scenario live re-run, with the console disabled so no
  second driver could touch the cluster: **strict 20/20** over the 20 measurable scenarios
  (21/21 injected, 21/21 recovered, alert coverage 20/21, 0 tokens). Against the previous full run
  **exactly one case changed**: `app_memory_stress` `rc:oom-kill` → `rc:mem-pressure` — and the true
  kill (`app_oom_kill`) still answers `rc:oom-kill`, i.e. the metric-plane rule separates the two
  rather than trading one for the other
  ([`reports/rca_diagnosis_report_discriminator.json`](reports/rca_diagnosis_report_discriminator.json)).
  The same run is the counter-example to the round-7 candidate, which could only fix one of the pair.

- `app_memory_stress` ("single replica, 200 MB, approaching the limit, OOM *precursor*") had its
  expected terms set to `{metric:mem-pressure, rc:oom-kill}`. The container evidence says the label
  was the stale part: during that scenario **every replica reports `oom_killed = false`** (archived
  full run and this re-test), and the lone `restart_count = 1` is the documented sticky residue from
  an earlier kill. The label is now `["metric:mem-pressure", "rc:mem-pressure"]`
  (`tools/ontology_cluster_patch.py`, mirrored in the catalog's `ontology_terms`), with the reasoning
  written next to it. `rc:oom-kill` was **replaced**, not merely appended — appending it would keep
  accepting "already happened" as an answer to "was only at risk", which is what the label got wrong.
- **This exposes a capability boundary worth stating plainly**: the case is *metric-gated*
  (`increase(cc_container_oom_kill_total[5m])`), so **text-only replays cannot score it by
  construction**. Under today's labels the replay criteria read:
  pre-fix archived run **18/20**, post-fix archived run **19/20**, offline catalog form **19/21** —
  each missing exactly this case (plus, for the pre-fix run, the input-collision case). The
  metric-aware live e2e is the criterion that can measure it; `tools/ontology_ab_replay.py` gained
  `--current-labels` so a historical run can be re-scored against today's truth (the default keeps
  the report's own snapshot labels so historical A/B stays self-consistent).
- **The round-7 screen was strengthened after this label change found a hole in it.** The screen
  compared only aggregate deltas, so a candidate that trades one case for another looked neutral.
  Re-judged under the corrected label, the round-7 candidate indeed *improves* the catalog form
  (19/21 → 20/21: `app_memory_stress` now matches) while **still breaking `app_oom_kill`** — a
  genuine kill answered as pressure. Net across the real-input set: 18 → 18, i.e. exactly the
  zero-sum trade the old judge would have waved through. The screen now compares **per case** and
  rejects on any hit→miss, which keeps the same verdict (REJECT) for the right reason
  ([`reports/round7_candidate_rejected.json`](reports/round7_candidate_rejected.json) is the
  re-measured artifact).

### Findings — the round-7 candidate, rejected before publication

- **The seventh ontology round was opened and then refused by measurement.** The evolution trigger
  was due (343 trajectories vs a threshold of 30), and the candidate came from the §11.42 diagnosis:
  `rc:oom-kill`'s *definition* demands evidence that a kill already happened, while its **term
  dictionary** still claimed symptom words (`内存压力` / `内存不足` / `内存超限` / `被内核杀死`).
  Moving those words to `rc:mem-pressure` and adding hypothetical-phrasing negatives measured
  **worse on two of three fixed input sets**:
  - 21 scenarios (offline form), engine top-1: 20/21 → **19/21**
  - held-out exam: 4/10 → 4/10 (unchanged)
  - archived real alert texts, strict: 19/20 → **17/20**

  The two regressions are both on faults where a kill *did* happen: `app_oom_kill` and
  `app_memory_stress` were re-judged as `rc:mem-pressure`, because a real kill alert says both
  "cgroup **oom_kill 计数增加**" and "**内存超限**被内核杀死" — the symptom word is not noise there, so
  moving it hands the culprit's own evidence to the pressure term.
  **No version was published** ([`reports/round7_candidate_rejected.json`](reports/round7_candidate_rejected.json)).
- **This also corrects §11.42**: "the words were tightened in the wrong layer" must not be read as
  "so go clean the term dictionary". The text plane cannot separate *at risk* from *already
  happened* no matter how the dictionaries are tuned — the discriminator has to live in the **metric
  plane** (`increase(cc_container_oom_kill_total[5m])`, which `ontology_v6` already models but the
  deterministic fast path never reads). That is an engine change, not an ontology round.

### Findings — discovered while running that regression (all pre-existing)

- **One call to `/api/rca/infer` silently switched the active ontology back to
  `ontology_v0-rca-agent`** — *now fixed, see the entry under "Fixed" below.*
  `RCAEngine.infer(..., inject_evidence=True)` is the **default**, and `_inject_evidence()` loaded
  `ontology_v0`, hardcoded `new_version = "ontology_v0-rca-agent"`, then called
  `SemanticStore.save_version(...)` **and `SemanticStore.set_active(...)`**. So the live backend and
  every tool that reads `active.json` then served the 18-term legacy ontology — the exact state six
  rounds of evolution removed. The regression hit it through
  `tools/verify_all.py` → `tools/fault_drill.py` → `POST /api/rca/infer` (backend log:
  `[RCA] Evidence injected: ev:rca-inc_*`). Restored: `active.json`,
  `versions/ontology_v0-rca-agent/` and `ontology_turtle/` were reverted to HEAD.
- **`tools/fault_drill.py` injects faults on `import`** — its drill body is top-level code with no
  `__main__` guard (an import probe really started the drill). Not rewritten: indenting ~140 lines of
  an untested fault-injecting script is riskier than the documented hazard; a warning now sits at the
  top of the file.
- **`tools/verify_all.py` is a destructive aggregator** whose suite list still names scripts from an
  older layout; two of its seven suites inject faults.
- **`generate_ontology_ttl.py` no longer reproduces its committed output** — regenerating produced
  370 lines *fewer* across `ontology_turtle/*.ttl`. The generator has drifted from the committed
  artifacts (unrelated to this change); the files were reverted.
- **The six LLM-calling scripts were then run** (user-approved; real spend, ~127.6k tokens reported as
  totals plus per-call counts ≈ 1.9e5 visible — see
  [`reports/llm_scripts_regression.json`](reports/llm_scripts_regression.json)). 4/6 exited 0; both
  non-zero results are pre-existing and unrelated to the path change:
  - `tools/llm_client_verify.py` (13 passed / 1 failed): the "bad key" case expects an error, but
    `llm_config` has provider fallbacks, so a bad key is masked by another provider — the test is not
    hermetic.
  - `tools/agent_tools_verify.py` (39 passed / 2 failed): `env_container_inspect` targets
    `cc-credit-card-app`, the **pre-cluster** container name (the app is now
    `rca-agent-payment-app-1/2/3`). The stale name is still in
    `backend/services/tools/env_tools.py:315`, `backend/services/agent.py:143`,
    `backend/routers/chat.py:189-190` and `demo/`, so the tool returns nothing when called with its
    own default/example argument.

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

- **The e2e accuracy counted unmeasurable scenarios as hits.** When a scenario's declared alerts never fire, the harness sends the engine a placeholder ("no corresponding alert") and whatever it answers was still scored as a hit — inflating `diagnosis_accuracy` to 1.0. The honest three-state view is: hit / miss / **not measurable** (excluded from the denominator). Recomputed over the measurable set the strict figure is 19/20. The verifier itself still needs the three-state split in code.
- **The deterministic engine has no no-fault exit.** A false-positive probe with healthy inputs (8 cases x 3 runs) shows **22/24 = 91.7% false positives**; the deterministic fast path asserted a fault in **15/15** runs, including input that explicitly states the incident is over. This axis was never evaluated before. Contract and acceptance criteria: `docs/设计_无故障出口.md`; the probe is its regression gate.
- **Routing is not deterministic for identical input.** The same input routes to the deterministic path in some runs and to the LLM agent in others, so the published e2e (16/18) and A/B (17 to 18) numbers carry run-to-run variance that has not been quantified.
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
