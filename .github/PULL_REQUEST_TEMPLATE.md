# Pull request

<!-- Keep the diff scoped. The bar here is evidence, not feature count
     (CONTRIBUTING: "how you verified it"). -->

## What changed

- 

## How it was verified

```bash
# exact commands + numbers (before/after when behaviour changed)
```

## Checkboxes

- [ ] CI is green (lint + ontology offline checks, frontend build, smoke + doctor)
- [ ] The affected doc under `docs/` is updated (the handbook is the record of truth)
- [ ] If behaviour changed: before/after measurement is included above
- [ ] If ontology content changed: it went through a versioned round
      (`tools/evolve_ontology_cluster.py --round <name>`), not a hand edit
- [ ] No host absolute paths leaked into `reports/` (`python tools/publish_reports.py --check`)
- [ ] Commit style: conventional (`feat:` / `fix:` / `docs:` / `chore:` / `test:`),
      measurement in the body for fixes that came out of a measurement
