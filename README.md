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
| Strict root-cause top-1 | **16/18 scenarios** by strict top-1 (the report's own `diagnosed_ok` is 17/18, a looser criterion); the run was cut off at 18/21 by a too-small step budget, since fixed | e2e, [`reports/rca_diagnosis_report.json`](reports/rca_diagnosis_report.json) |
| Component→root-cause promotion (A/B) | strict **17→18/21**, top-5 **20→21/21**, 0 regressions | [`reports/promotion_ab.json`](reports/promotion_ab.json) |
| Recovery verified | **18/18** | e2e, after switching the lag-recovery criterion to config state |
| Deterministic fast path | **0 tokens, ~15 s/case** | when routing stays on the fast path |
| Component→root-cause promotion (A/B) | strict **17→18/21**, top-5 **20→21/21**, 0 regressions | `rootcause_promotion_verify.py` |
| Cross-cluster attribution | **16/18 = 89%** | `crosscluster_verify.py` |
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

## License

MIT — see [LICENSE](LICENSE). Third-party components and their licenses:
[THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md).
