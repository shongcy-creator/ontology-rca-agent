[English](README.md) | [中文](README.zh.md)

# Ontology-Driven RCA Agent — with a Reproducible Fault-Injection Benchmark

An **ontology-driven root-cause-analysis agent** for a containerized cluster
(3 app replicas behind a gateway + MySQL primary/2 replicas), shipped together with
a **reproducible fault-injection benchmark**: 21 fault scenarios, alert-coverage checks,
end-to-end diagnosis scoring, and a versioned ontology-evolution protocol.

> ⚠️ **Isolation only.** This project injects faults into containers and mutates database
> configuration. The injection API is equivalent to container-control permission.
> Never point it at anything you care about. See [SECURITY.md](SECURITY.md).

## What it is

- A **dual-engine** RCA agent: a deterministic ontology fast path, and an LLM agent for
  open-ended questions. Routing is decided by the *input*, not by the conclusion.
- A **fault-injection catalog** of 21 scenarios (app / database / cluster-resource layers),
  each with declared signals, blast radius and expected root cause.
- A **verification harness** that measures the things that usually go unmeasured:
  does the fault actually reproduce its signal? does the declared alert really fire?
  is the diagnosis right? did remediation actually make the criterion go away?
- A **versioned ontology** (`.evoontology/`) evolved through the
  [EvoOntology](https://github.com/ruc-datalab/EvoOntology) protocol: candidate version → paired evaluation →
  gate → accept. Current: `ontology_v6` (56 terms / 87 relations / 30 constraints / 22 evidence).

## What it is NOT

- Not a production monitoring stack, and not safe to run against production.
- Not "zero-instrumentation". The app must expose a small SLI contract
  (request rate, error rate, latency, instance health) via `/metrics` or an exporter.
- Not a finished generalization: today the injection layer is **Docker-only** and the
  database dialect is **MySQL-only**. Portability work is tracked in `docs/`.

## Key features, with screenshots

> Every screenshot below is from a **real run**, and every feature states **why you can trust
> it**. A feature list is cheap; making the conclusion verifiable is the hard part.

### 1. Fault-injection console — 21 scenarios, grouped by layer and graded by risk

![Fault injection console](docs/images/故障注入控制台.png)

Each card declares its layer (app / database / cluster-resource), category, **blast radius** and
expected root cause. High-risk scenarios need explicit confirmation, and only one injection may
run at a time. **Why trust it:** every scenario declares the signals that must hold *while
injected*, and `fault_injector verify` checks each one — currently **21/21 reproduced, 20/21
signals all hold** ([report](reports/fault_verify_report.json)).

### 2. One-click stop & roll back — recovery must pass its criteria

![Stop and roll back](docs/images/故障注入_停止并回滚.png)

Rollback re-checks that the **recovery criteria actually turned false**, not that "it looks fine
now". **Why trust it:** this is exactly the defect class fixed three times
([`docs/lessons/05`](docs/lessons/05-criteria-that-cannot-pass.md)) — a recovery criterion written
backwards makes recovery **permanently fail** instead of silently passing.

### 3. A running job survives tab switches

![Global running indicator](docs/images/注入_切页全局指示.png)
![Stress job survives a tab switch](docs/images/压测_切页状态保持.png)

Switching tabs **unmounts** the page, so "a job is running" is persisted and shown globally; coming
back **re-attaches** to the running job. **Why trust it:** verified over CDP in four steps —
state persisted, indicator appeared, re-attached on return, job finished at 100% success.

### 4. Stress harness — HTTP and MySQL modes

![Stress harness](docs/images/压测台.png)

Before starting it **probes reachability** (TCP for MySQL, `/health` for HTTP) and refuses with a
reason if unreachable. **Why trust it:** this is the fix for
[`docs/lessons/02`](docs/lessons/02-stress-harness-connected-to-itself.md) — a configuration error
must not look like a performance result (842/842 "failures" at the time).

### 5. Root-cause conclusions — deterministic engine and LLM take different paths

![Root cause](docs/images/根因结论_RCA面板.png)

Routing is decided by the **input**, not by the conclusion: structured alerts use the ontology fast
path (**0 tokens, ~15 s**); open-ended natural-language questions are **forced** onto the LLM agent.
**Why trust it:** every answer returns `mode` and `route_reason`, so you can check why it took that
path.

### 6. Ask in natural language, with the reasoning visible

![Chat](docs/images/根因结论_聊天框.png)
![Reasoning in the chat](docs/images/诊断_推理在聊天框.png)

### 7. Metric view and topology view of the same diagnosis

![Metrics panel](docs/images/诊断_指标面板.png)
![Topology panel](docs/images/诊断_拓扑面板.png)

### 8. Remediation — P0 is read-only, writes need approval, failures roll back

![Remediation actions](docs/images/根因处置动作.png)

**Invariant: the most urgent (P0) remediation actions are always read-only**, enforced as an
ontology **release gate** rather than a comment. Write actions are dry-run by default, carry an
inverse operation and a post-check, and roll back automatically if the check fails.

### 9. Model settings and a cost dashboard

![Model settings](docs/images/模型设置_LLM切换.png)
![Cost dashboard](docs/images/成本看板.png)

### 10. Cluster monitoring — Prometheus + 23 alert rules + Grafana

![Grafana dashboard](docs/images/Grafana_面板.png)

Alert coverage is **measured**: inject → wait long enough (max rule `for` + 60 s) → check whether
the declared alert actually fires. Currently **20/21 = 95%**.

## Architecture

```
                 ┌──────────── Nginx gateway :8080 ────────────┐
                 │            (load balanced)                  │
        ┌────────┴────────┬─────────────────┬──────────────────┴───┐
        │  app replica 1  │  app replica 2  │  app replica 3       │  ← cc_* metrics
        └────────┬────────┴────────┬────────┴──────────────────────┘
                 └──────────┬──────┘
                    MySQL primary ── GTID ── replica 1 / replica 2
                            │
      Prometheus + 23 alert rules + Alertmanager + blackbox   Grafana :3000
                            │
                  FastAPI RCA backend :8088 ── React console :3001
                     │            │
        deterministic engine   LLM agent (tools + ontology)
                     │
              .evoontology/  ← evolved through versioned rounds + gate
```

## Quickstart

> **One command is enough** (recommended):
>
> ```bash
> python tools/dev_up.py        # up → bootstrap(schema+replication) → seed → doctor; or `make up`
> ```
>
> The five steps below are what it does internally — kept for step-by-step troubleshooting.
> They used to live in **three** places (docs / CI / compose), and the bootstrap step was
> missed three times (symptom: replicas stay empty instances — "Access denied" on the read
> path, no replication — while **every container reports healthy**). It is now one entry point.


```bash
# 1) bring up the cluster (app x3 + gateway, MySQL primary + 2 replicas,
#    Prometheus/Grafana/Alertmanager, RCA backend + console)
docker compose -f rca-agent/docker-compose.yml up -d

# 2) bootstrap the cluster: schema + replication. **This is NOT done by compose** —
#    the replicas start as empty instances (no appuser, no tables, no replication).
#    Skipping it shows up as "Access denied" on the read path plus two replication FAILs.
python tools/cluster_bootstrap.py

# 3) build the synthetic dataset (~4.8M rows) — required by the scenarios
python tools/fault_injector.py seed

# 4) environment health check (18 items) — expect all PASS
python tools/fault_injector.py doctor

# 5) open the console
#    http://localhost:3001   (chaos / stress / cost / diagnosis tabs)
```

Requirements: Docker with **compose v2**, Python 3.11+, Node 20+ (only to rebuild the frontend).

## What you can do

```bash
python tools/fault_injector.py verify                 # all 21 scenarios: inject → signal → recover
python tools/fault_injector.py verify --fast          # representative subset (7) — CI friendly
python tools/alert_coverage_check.py                  # inject → wait → did the declared alert fire?
python tools/cluster_rca_verify.py                    # end-to-end: inject → stress → alerts → diagnose → score
python tools/evolve_ontology_cluster.py --round memory  # one ontology round (paired eval + gate)
python tools/run_all_verification.py --full            # the whole pipeline (hours; 14 steps)
```

## Measured results

Every number below was produced by a script in `tools/`; the raw reports land in `.chaos/`.
Numbers are labelled with their measurement basis on purpose — the project's rule is that
a green check must be able to fail.

| Metric | Result | Basis |
|---|---|---|
| Fault scenarios reproduced | **21/21** | `fault_verify`, [`reports/fault_verify_report.json`](reports/fault_verify_report.json) |
| Injected-state signals all hold | **20/21** criteria observed at injection time | `fault_verify`, [`reports/fault_verify_report.json`](reports/fault_verify_report.json) |
| Declared alerts actually fire | **20/21 = 95%** | `alert_coverage_check` (hold = max rule `for` + 60s) |
| Strict root-cause top-1 | **19/20 = 95.0%** by root-cause term over the **measurable** scenarios — one scenario declared no alert to diagnose and is excluded rather than counted (the report's own looser criterion counts 20/20; the two are NOT interchangeable) | e2e, [`reports/rca_diagnosis_report.json`](reports/rca_diagnosis_report.json) |
| Component→root-cause promotion (A/B) | strict **17→18/21**, top-5 **20→21/21**, 0 regressions | [`reports/promotion_ab.json`](reports/promotion_ab.json) |
| Recovery verified | **18/18** | e2e, after switching the lag-recovery criterion to config state |
| Deterministic fast path | **0 tokens, ~15 s/case** | when routing stays on the fast path |
| Component→root-cause promotion (A/B) | strict **17→18/21**, top-5 **20→21/21**, 0 regressions | `rootcause_promotion_verify.py` |
| Cross-cluster attribution | **16/18 = 89%** | `crosscluster_verify.py` |
| **vs. a pure-threshold baseline** | ontology **16/18 = 88.9%** vs baseline **11/18 = 61.1%** (the baseline is misled by pre-existing noise alerts in 4 cases); the baseline never wins a single case | offline replay of the archived run, [`reports/baseline_compare.json`](reports/baseline_compare.json) |
| **Held-out phrasing (not tuned on)** | **4/10 = 40%** vs 81–86% on the tuned set | `heldout_verify.py` |

The last row is the most important one: it is a **negative result we chose to publish**.
The scoring keywords are overfitted to the phrasings of the 21 scenarios. Fixing that is
tracked work — and it must not be done by tuning on the same held-out set.

## Safety model

- All host ports bind to `127.0.0.1` by default (`CC_BIND` to change deliberately).
- Optional injection token `CHAOS_API_TOKEN`; console has a token field.
- `CHAOS_UI_ENABLED=0` kills the injection console entirely.
- High/critical scenarios require an explicit confirm; `/jobs/current/abort` always available.
- Remediation actions: read-only diagnostics auto-execute; **writes require `--approve`**,
  carry an inverse operation and a post-check, and auto-roll back when the check fails.
- **Invariant: the most urgent (P0) remediation action is always read-only** — enforced as a
  publication gate on the ontology, not as a comment.

## Repository layout

```
rca-agent/            compose stack, FastAPI backend, React console, Prometheus/MySQL config
tools/                fault catalog, verification harnesses, ontology tooling
.evoontology/         versioned ontology (active.json + versions/*) ← the knowledge base
ontology_turtle/      OWL/TTL single source of truth
vendor/EvoOntology/   MIT-licensed ontology evolution engine
docs/                 handbook + migration notes (mostly Chinese)
```

## Documentation

| Doc | Language | Content |
|---|---|---|
| `docs/集群化_故障注入_验证手册.md` | 中文 | The main handbook: design, 40+ verified findings, self-corrections |
| `docs/EvoOntology_接入手册.md` | 中文 | Ontology protocol integration and rounds |
| `docs/Linux迁移适配.md` | 中文 | Moving from Windows Docker to Linux Docker |
| `docs/RCA_Agent_智能化改造方案.md` | 中文 | Agent design and roadmap |
| [`docs/lessons/`](docs/lessons/README.md) | EN/ZH | **Six real incidents** from building this: the traps that make a broken system look healthy |

## License

MIT — see [LICENSE](LICENSE). Third-party components and their licenses:
[THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md).
