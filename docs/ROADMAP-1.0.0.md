# vlambda 1.0.0 roadmap

Status: in progress
Theme: stability, safety and operations for a single-host deployment

## Scope

- Single host, Linux x86_64/aarch64.
- SQLite on a local volume.
- No multi-host active/active and no RBAC beyond owner-scoped resources.
- Compatibility may break before 1.0; it is frozen at the 1.0 release.

## Progress

- [x] Phase 1: data lifecycle.
  - Schema version (`PRAGMA user_version`), legacy/newer database rejection.
  - `vlambda backup` / `vlambda restore`.
  - `vlambda reset --wipe`.
  - Backup integrity and encryption-key fingerprint checks.
- [x] Phase 2: security and operations.
  - Secret rotation with key identifiers.
  - Login throttling per username and source IP.
  - Registration closed by default in the production image.
  - Startup checks for writable directories and Landlock + seccomp.
  - `SECURITY.md`, SBOM workflow and GHCR image workflow.
- [x] Phase 3: documentation and API freeze.
  - API, CLI, compatibility and upgrade references.
  - `vlambda --version` and version in `/health` and `/ready`.
  - Contributing guide.
- [~] Phase 4: quality and release candidate.
  - [x] Coverage threshold (55%) and unit CI job.
  - [x] aarch64 integration job, not only wheel build.
  - [x] Failure-isolation and HTTP/JSON fuzz smoke tests.
  - [x] Native callback result leak fixed under sustained load.
  - [x] Soak and throughput tooling (`scripts/soak.py`, `VLAMBDA_MIN_RPS`).
  - [x] 2-minute in-process soak: 156k requests, 0 errors, ~10 MB RSS growth.
  - [ ] Full 24 h soak on a staging host.
  - [ ] Extended chaos: DB lock, broker down, lease loss, disk full.
  - [x] Broker protocol fuzzing for malformed, oversized and unauthenticated messages.
  - [x] Fresh install from a release wheel in a clean environment (CI).
- [ ] Phase 5: 1.0.0 release.
  - Freeze API and schema.
  - Rehearse upgrade/backup/restore on a clean host.
  - Publish wheels, sdist, SBOM, image and release notes.
  - Deploy only after PyPI verification.

## Release checklist

- [ ] `1.0.0rc1` tag and GitHub pre-release.
- [ ] Unit + integration + four-interpreter matrix green.
- [x] Fresh install from the wheel on x86_64 (CI `wheel-install`); aarch64 develop smoke in CI.
- [ ] Backup → wipe → restore → smoke on a clean host.
- [ ] Secret rotation and login throttle smoke.
- [ ] Docker image by digest with registration closed.
- [ ] `SECURITY.md` and compatibility docs reviewed.
- [ ] `CHANGELOG.md` final.
- [ ] PyPI publish verified; `deploy` updated only afterwards.
- [ ] `1.0.0` tag, GitHub Release and image published.
