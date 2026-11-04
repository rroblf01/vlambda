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
- [ ] Phase 4: quality and release candidate.
  - Coverage threshold and missing unit tests.
  - Chaos: DB lock, broker down, lease loss, disk full.
  - Soak test with memory-leak checks.
  - Fuzzing for HTTP/JSON and the broker protocol.
  - aarch64 test run, not only wheel build.
  - Performance regression gate around `bench.py`.
- [ ] Phase 5: 1.0.0 release.
  - Freeze API and schema.
  - Rehearse upgrade/backup/restore on a clean host.
  - Publish wheels, sdist, SBOM, image and release notes.
  - Deploy only after PyPI verification.

## Release checklist

- [ ] `1.0.0rc1` tag and GitHub pre-release.
- [ ] Unit + integration + four-interpreter matrix green.
- [ ] Fresh install from the wheel on x86_64; aarch64 smoke.
- [ ] Backup → wipe → restore → smoke on a clean host.
- [ ] Secret rotation and login throttle smoke.
- [ ] Docker image by digest with registration closed.
- [ ] `SECURITY.md` and compatibility docs reviewed.
- [ ] `CHANGELOG.md` final.
- [ ] PyPI publish verified; `deploy` updated only afterwards.
- [ ] `1.0.0` tag, GitHub Release and image published.
