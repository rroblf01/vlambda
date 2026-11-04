# Compatibility and versioning

## Supported platforms (1.x)

- Linux x86_64 and aarch64.
- CPython 3.14 and 3.15, including free-threaded builds (`cp314t`, `cp315t`).
- SQLite on a local filesystem. Network filesystems are not supported.
- Single host. Multiple replicas on one host are supported with a shared local
  volume; active/active across hosts is out of scope.

Windows and macOS are not supported targets for 1.x. The in-process development
mode may run elsewhere, but it is not part of the supported matrix.

## What is stable in 1.x

These interfaces follow semantic versioning:

- The handler contract: `def handler(request)` or `async def handler(request)`,
  returning a `Response` or a serializable `dict`, `str` or `bytes`.
- `Request` fields and methods: `method`, `path`, `query_string`, `query`,
  `headers`, `cookies`, `body`, `remote_ip`, `host`, `scheme`, `request_id`,
  `slug`, `env`, `kv`, `fetch`, `header()`, `json()`, `form()`, `log()`.
- `Response` constructors: `json()`, `text()`, `html()`, `redirect()`,
  `no_content()`, status codes, headers, cookies and binary bodies.
- The HTTP API documented in [API.md](API.md), including status codes and the
  `X-Request-Id` header.
- The CLI commands and flags documented in [CLI.md](CLI.md).
- Environment variables documented in the README and CLI reference.
- The database schema version (`PRAGMA user_version`) and forward migrations.

## What is not stable

- Internal Python modules (`vlambda.pool`, `vlambda.runner`, `vlambda.cache`,
  `vlambda.forkd`, …) are implementation details.
- UI markup, CSS classes and the bundled JavaScript API.
- The exact wording of log messages and error strings.
- The internal runner/broker protocol between vlambda components.
- Experimental environment variables that are not documented.

## Versioning

vlambda follows [Semantic Versioning](https://semver.org/):

- **MAJOR** for breaking changes to the stable interfaces.
- **MINOR** for backwards-compatible features and deprecations.
- **PATCH** for backwards-compatible fixes.

### Deprecations

A stable interface is deprecated for at least one minor release before removal
in the next major release. Deprecations are recorded in `CHANGELOG.md` and,
where practical, surfaced in logs or the OpenAPI document. Removing an endpoint,
CLI flag or environment variable without a deprecation release is a bug.

## Database compatibility

- The main and metrics databases carry a schema version.
- Startup refuses a database written by a newer build.
- 1.0 refuses pre-1.0 databases; use a fresh database or `vlambda reset --wipe`.
- Migrations are forward-only. Restoring a backup is the rollback path.
- Back up before every upgrade and verify the archive with `vlambda restore`
  on a staging copy.

## Security guarantees

The Docker default runs each invocation in a fresh Landlock + seccomp child.
The sandbox blocks filesystem access outside the runtime/project/vendor/temp,
TCP/UDP networking, cross-process signals, process creation and privileged
syscalls. Egress only goes through the audited `request.fetch` capability.

These are defense-in-depth measures, not a hypervisor boundary. See
[SECURITY.md](../SECURITY.md) for reporting and deployment assumptions.
