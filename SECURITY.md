# Security policy

## Supported versions

vlambda 1.x is the supported line. Security fixes land on the latest 1.x
release. The 0.4 line is only supported until the 1.0 release is published.

## Reporting a vulnerability

Please do not open a public issue for a suspected vulnerability. Use GitHub's
private vulnerability reporting:

<https://github.com/rroblf01/vlambda/security/advisories/new>

If that is not available, contact the maintainer at
<ricardo.r.f@hotmail.com> with:

- affected version and commit;
- a minimal reproduction or proof of concept;
- the impact you believe it has;
- whether the report can be published after the fix.

You will get an acknowledgement as soon as possible. Please allow time for a
fix and a coordinated disclosure before publishing details.

## Scope

In scope:

- sandbox escapes (filesystem, network, signals, process creation, syscalls);
- authentication or authorization bypasses;
- cross-user data access (lambdas, KV, env, metrics, logs, tasks, projects);
- secret disclosure in logs, errors, OpenAPI, backups or the UI;
- SSRF or egress allow-list bypasses;
- remote code execution outside the intended lambda sandbox;
- SQL injection or unsafe archive extraction.

Out of scope for the hosted demo:

- denial of service caused by your own lambda consuming its CPU/memory quota;
- attacks that require the `VLAMBDA_ADMIN_TOKEN` or the host root account;
- missing hardening on a deployment that runs without `VLAMBDA_SANDBOX`;
- multi-host active/active, which 1.0 explicitly does not support.

## Secret storage

Lambda secrets are encrypted at rest with a per-value random nonce, an
HMAC-SHA256 keystream and an HMAC-SHA256 tag, and each ciphertext carries a
short key identifier for rotation. This protects against casual database reads;
it is not a KMS and does not protect a host that is already compromised. Keep
`VLAMBDA_ENCRYPTION_KEY` outside the database and backups, and rotate it with
`vlambda secrets rotate` if it may have leaked. A dependency-free standard AEAD
is a candidate for a later 1.x release.

## Deployment assumptions

vlambda 1.0 targets a single host, Linux x86_64/aarch64, with SQLite on a local
volume. The Docker image runs an outer container boundary plus a fresh
Landlock + seccomp child per invocation. That is defense in depth, not a
guarantee against a kernel or hypervisor escape. For hostile multi-tenant code,
add gVisor, Firecracker or one container per tenant.

Do not expose the server without TLS, and do not reuse `VLAMBDA_SECRET` or
`VLAMBDA_ADMIN_TOKEN` across deployments. Backups contain encrypted secrets:
protect the archive and the encryption key separately.
