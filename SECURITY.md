# Security Policy

## This project is for isolated environments only

The fault-injection API can restart/pause containers, change container quotas, break
networking, mutate database configuration and hold locks. **Anyone who can reach that API
effectively has control over the containers it targets.** Do not deploy it against
production, shared, or customer-facing environments.

## Reporting a vulnerability

Please open a **private** security advisory (GitHub → Security → Advisories) or email the
maintainer at **shongcy@gmail.com**. Do not open a public issue for exploitable
problems. We aim to acknowledge within 7 days.

## Built-in boundaries (what the project already does)

| Control | Default | Notes |
|---|---|---|
| Host port binding | `127.0.0.1` only | `CC_BIND` changes it deliberately; if you set `0.0.0.0`, set a token too |
| Injection token | empty (loopback-only assumption) | `CHAOS_API_TOKEN`; the console sends `X-Chaos-Token` |
| Kill switch | injection console enabled | `CHAOS_UI_ENABLED=0` disables it outright |
| Confirmation gate | on | high/critical scenarios require `confirm: true` |
| Abort | always available | `POST /api/chaos/jobs/current/abort` (confirm required) |
| Remediation writes | refused by default | read-only actions auto-run; writes need `--approve`, carry an inverse op and a post-check, and auto-roll back on failure |
| P0 remediation | read-only, always | enforced as an ontology **publication gate** |
| Single-flight | on | only one injection/stress job at a time |

## Credentials in this repository

All passwords in `docker-compose.yml` (`apppass`, `rootpass`, `rca_readonly_pwd`,
the Grafana default) are **demo defaults for the isolated stack**, and every one of them
can be overridden (see `.env.example`). **Change all of them before exposing anything.**
No real credential is committed; `.env` is git-ignored.

The dataset is **synthetic** (generated transaction rows). No real customer data.

## Known gaps (honest list)

- TLS/auth in front of the console is not implemented; the project assumes loopback or a
  trusted network.
- The backend container mounts the Docker socket; treat a compromise of that container as
  a compromise of the Docker host.
- SSH/VM-based injection is **not** implemented. If it is added, it will require a command
  allowlist, an approval gate and sudo-scope review — see `docs/Linux迁移适配.md`.
