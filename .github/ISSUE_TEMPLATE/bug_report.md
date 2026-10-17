---
name: Bug report
about: Something in the stack does not behave as documented
title: "[bug] "
labels: bug
---

## What happened

<!-- Describe the failure, with the exact command you ran and the full error output. -->

## What you expected

<!-- What did the docs/README promise? -->

## Reproduction

```bash
# minimal command sequence
```

## Environment

- OS / Docker version:
- Branch / commit:
- Did `python tools/fault_injector.py doctor` pass before the failure? (yes/no, which item)

## Which surface is affected

- [ ] `demo/` single-node stack
- [ ] `rca-agent/` cluster stack
- [ ] `tools/` verification harness
- [ ] `.evoontology/` ontology
- [ ] console (http://localhost:3001)
