# Contributing

Thanks for looking. This project cares more about **evidence quality** than about feature
count, so most of the rules below are about how claims get made.

## Development setup

```bash
docker compose -f rca-agent/docker-compose.yml up -d
python tools/fault_injector.py seed        # build the synthetic dataset (~4.8M rows)
python tools/fault_injector.py doctor      # 18 checks, expect all PASS
cd rca-agent/frontend && npm ci && npm run build   # only if you touch the console
```

Fast feedback loops (no long runs):

```bash
python tools/fault_injector.py verify --fast   # 7 representative scenarios (~10-15 min)
python tools/ontology_hygiene.py               # ontology hygiene contract
python tools/ontology_scoring_verify.py        # ontology A/B top-1 regression (seconds, offline)
python -m py_compile tools/*.py rca-agent/backend/**/*.py
```

## Non-negotiable disciplines

These exist because each one was violated at least once and cost real debugging time.
Please read them before opening a PR.

1. **Ontology content changes go through a versioned round.**
   Never edit `.evoontology/versions/*` by hand, and never "just tweak" a keyword.
   Use `tools/evolve_ontology_cluster.py --round <name>`: it builds a candidate,
   runs the **paired evaluation** against the parent and enforces the **publication gate**.
   A round that only "looks good" without a pairwise delta is not acceptable.

2. **Never tune a threshold to make a check pass.** If a check fails, decide which is wrong:
   the criterion or the system. Record the decision in `docs/`.

3. **A criterion must be observable within the verification wait — in both senses.**
   A `rate(...[2m])` criterion can never become true inside a 75 s wait — that is not
   "strict", it is permanently red. (Found in 4 scenarios.) **(b) The evidence must *appear* in time and be *observable at all***: a MySQL replica takes 167 s to cold-start while the wait was 120 s, and a `mysql_up = 0` transient is invisible to a 15 s scrape — prefer the durable form (uptime reset), or take the union of both.

4. **A signal must verify what its name claims.** If a scenario is called "real container
   OOMKill", its criterion must read the cgroup `oom_kill` counter, not just memory pressure.

5. **No vendor-specific names inside scenario declarations.**
   Declarations describe *what* is required (capability, signal, expected term); the
   `docker`/`mysql` specifics belong in the execution/dialect layer. This is what keeps a
   future port from being a rewrite.

6. **Report negative results.** If held-out data performs worse than the tuned set, that
   goes into the docs, not under the rug. Published negative results are a feature here.

7. **Verify the lever actually moved.** After issuing a change (quota, config, index),
   read it back and assert it. Several past bugs were "the command was sent, so we assumed
   it took effect". Concretely: write config files **without a BOM** and read the first bytes back, and validate syntax (`py_compile`, `docker compose config`, a YAML parse). A PowerShell `Set-Content -Encoding UTF8` BOM once crash-looped Prometheus 10 times.

8. **Evidence must not live inside the thing being observed.**
   A counter that the container exposes about itself disappears when that container is killed
   and restarted — so `increase(container_oom_kill_total[5m]) > 0` was permanently false while
   the injector could see the OOM kill in `docker inspect`. Anything that can be destroyed
   along with its subject must be observed from **outside** it.

9. **`recovered_signals` are the symptoms that must DISAPPEAR**, not the conditions that should
   hold after recovery. Writing `count(up == 1) >= 3` (true when healthy) makes recovery
   impossible to confirm — the check timed out with the correct value already in the result.

10. **A gate must be satisfiable by construction.** If you use a complete acceptance
   check (like `doctor`) as a readiness gate, every precondition that check requires must be
   fulfilled **before** the gate — otherwise it can never go green. A CI gate that ran
   `doctor` (which verifies dataset size) *before* the seeding step retried 15 times and
   failed, while looking like "the stack is broken". Same failure class as rules 2 and 3:
   a check that structurally cannot pass tells you nothing.

## Pull requests

- Keep the diff scoped; explain *how you verified it* (which command, which numbers).
- If your change affects behaviour, include the before/after measurement.
- Update the relevant doc under `docs/` (the handbook is the record of truth).
- CI must be green; the smoke job runs compose + `doctor`, which is the minimum bar.

## Commit style

Conventional commits (`feat:`, `fix:`, `docs:`, `chore:`, `test:`). For fixes that came out
of a measurement, put the measurement in the body — future readers need the evidence, not
just the conclusion.
