# Security policy

## Supported versions

vlambda 1.x is the supported line. Security fixes land on the latest 1.x
release. The 0.4 line is only supported until the 1.0 release is published.

## Reporting a vulnerability

Please do not open a public issue for a suspected vulnerability. Use GitHub's
private vulnerability reporting:

<https://github.com/rroblf01/vlambda/security/advisories/new>

If that is not available, open a minimal public issue asking for a private
contact channel; do not include exploit details in the issue. Include in the
private report:

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

## Credentials and headers

The server refuses to start without `VLAMBDA_SECRET`; `VLAMBDA_DEV=1` is the
explicit local-development escape hatch and uses an insecure secret.
`Authorization`, `Proxy-Authorization`, `Cookie`, `X-Api-Key` and
`X-Admin-Token` are removed from `request.headers` before a lambda runs.
Passwords must be at least 12 characters, and registrations are limited per
source IP (`VLAMBDA_REGISTER_MAX`/`VLAMBDA_REGISTER_WINDOW`).

Users can change their own password at `POST /password`; a change rotates the
per-user authentication epoch, so every existing JWT is rejected immediately.
Administrators can reset a password with `POST /admin/password` and the admin
token.

API tokens can carry an expiry (`expires_in` seconds); expired tokens are
rejected at authentication time. Rate limiting is applied per source IP and,
for authenticated requests, per user (`VLAMBDA_USER_RATE_LIMIT`), so one
account cannot exhaust the instance. Administrative actions (registration,
user creation, reset, token creation and revocation) are written to
`audit_log` and exposed read-only to the admin token at `/admin/audit`.

## Syscall filter

The runner uses a seccomp **allow-list** by default. CPython startup, SQLite,
TLS, threads, asyncio, the AF_UNIX broker client and normal file I/O are
permitted; process creation, new socket families, ptrace, mounts, namespaces,
kernel keyrings, io_uring, BPF and privilege changes are denied by the default
action. `clone` is allowed only with `CLONE_THREAD`, `socket`/`socketpair` only
for `AF_UNIX`, and `clone3` reports `ENOSYS` so glibc falls back to `clone`.
`VLAMBDA_SECCOMP_MODE=denylist` is a compatibility fallback for unusual
runtimes; it is less strict.

## Backup encryption

`vlambda backup --encrypt` derives keys from `VLAMBDA_BACKUP_KEY`, splits the
archive into 1 MiB chunks, encrypts each chunk with the HMAC-SHA256 counter-mode
keystream and authenticates it with an HMAC-SHA256 encrypt-then-MAC tag bound to
the header and chunk index. Truncation, reordering and tampering fail before
anything is extracted. The key is never stored in the archive; keep it in a
secret manager, separate from `VLAMBDA_ENCRYPTION_KEY`.

## Egress

`request.fetch` resolves the host once, rejects non-public addresses and then
connects to the validated IP with the original `Host`/TLS SNI. A DNS answer
that changes between the check and the request cannot redirect the connection
to a private address. Redirects remain disabled.

## Secret storage

Lambda secrets are encrypted at rest with a documented, dependency-free
construction:

1. `K = SHA-256("vlambda-env:" || VLAMBDA_ENCRYPTION_KEY)` (256-bit key).
2. A random 128-bit nonce `N` is generated for every value.
3. The keystream is `HMAC-SHA256(K, N || counter)` with a 64-bit big-endian
   counter, truncated to the plaintext length; ciphertext `C = P xor keystream`.
4. The tag is `T = HMAC-SHA256(K, "vlambda-env:" || N || C)` (encrypt-then-MAC,
   constant-time verification).
5. The stored value is `v1:<key-id>:base64url(N || T || C)`, where the key id is
   `HMAC-SHA256(K, "vlambda-env-key-id")[:16]` and lets `vlambda secrets rotate`
   re-encrypt values without guessing which key produced them.

This is HMAC-SHA256 in counter mode with an encrypt-then-MAC tag. It is secure
under the standard PRF assumption on HMAC-SHA256, uses an independent nonce per
value, authenticates the ciphertext before decrypting and rejects values from
another key. It is not a KMS and does not protect a host that is already
compromised. Keep `VLAMBDA_ENCRYPTION_KEY` outside the database and backups,
and rotate it with `vlambda secrets rotate` if it may have leaked.

## Backups

Backups contain plaintext lambda code, KV data and logs, and encrypted secrets.
Encrypt the archive before it leaves the host, for example with `age` or GPG,
and keep that key separate from `VLAMBDA_ENCRYPTION_KEY`. A backup is only as
safe as the storage and key that protect it.

## Deployment assumptions

vlambda 1.0 targets a single host, Linux x86_64/aarch64, with SQLite on a local
volume. The Docker image runs an outer container boundary plus a fresh
Landlock + seccomp child per invocation. That is defense in depth, not a
guarantee against a kernel or hypervisor escape. For hostile multi-tenant code,
add gVisor, Firecracker or one container per tenant.

Do not expose the server without TLS, and do not reuse `VLAMBDA_SECRET` or
`VLAMBDA_ADMIN_TOKEN` across deployments. Backups contain encrypted secrets:
protect the archive and the encryption key separately.

Login and registration throttles are stored in the `auth_throttle` table, so
multiple vlambda processes that share the same SQLite database enforce one
budget. The database is still single-host: do not place it on NFS.

Package installation runs `uv pip install`/`pip install` against PyPI. Pin
versions and use a controlled index (`VLAMBDA_PIP_INDEX_URL`) or
`VLAMBDA_PIP_REQUIRE_HASHES=1` for production, and treat a package install as
code execution inside the vendor directory.
