# Upgrading vlambda

## Before any upgrade

1. Stop writes or stop the server.
2. Create a backup and copy it outside the host:

   ```bash
   python -m vlambda backup --out /data/backups/vlambda-$(date +%F).tar.gz
   ```

3. Verify the archive can be restored into a disposable directory or host.
4. Read the `CHANGELOG.md` entry for the target version.

## 0.4.x to 1.0

1.0 starts with a new schema and deliberately refuses a pre-1.0 database. It
does **not** migrate users, lambdas, KV, schedules or encrypted secrets.

If you need to keep lambda source, export it from 0.4 first:

```bash
curl -H "Authorization: Bearer $VLAMBDA_TOKEN" \
  "https://your-host/vlambda/<slug>/export" > lambda.json
```

Then, with the 0.4 server stopped, install 1.0 (or use the 1.0 container
image) so the `reset --wipe` command exists, and run:

```bash
# Optional: copy the whole data volume first
cp -a /data /data.backup

# Remove the old SQLite files and project/package/cache data
python -m vlambda reset --yes --wipe
```

Start 1.0 with the same data paths. It creates the 1.0 schema. Create users with
`POST /admin/users` and `X-Admin-Token`, then re-import lambdas:

```bash
curl -X POST "https://your-host/vlambda/import" \
  -H "Authorization: Bearer $VLAMBDA_TOKEN" \
  -H "Content-Type: application/json" \
  --data-binary @lambda.json
```

Secrets are not portable across the 0.4 → 1.0 format change; set them again.
A dedicated `VLAMBDA_ENCRYPTION_KEY` makes future rotations independent of
`VLAMBDA_SECRET`.

## 1.x to 1.y

1. Back up.
2. Replace the package or image with the new version.
3. Start the server. It applies forward schema migrations, if any.
4. Check `/health` for the expected version and run a smoke invocation.

If startup fails:

- restore the backup with `vlambda restore --from ... --force`;
- do not try to downgrade the database in place. Older builds refuse a newer
  schema, and the backup is the supported rollback path.

## Key rotation

Rotating the encryption key is separate from upgrading:

```bash
python -m vlambda backup --out before-rotate.tar.gz
python -m vlambda secrets rotate --old-key "$OLD" --new-key "$NEW"
```

Restart with the new `VLAMBDA_ENCRYPTION_KEY`. If `VLAMBDA_SECRET` also signs
JWTs, rotating it invalidates existing sessions and API tokens.

## Docker

Pin the image by digest for production:

```bash
docker pull ghcr.io/rroblf01/vlambda:1.0.0
docker image inspect ghcr.io/rroblf01/vlambda:1.0.0 --format '{{index .RepoDigests 0}}'
```

Use the digest in the deployment instead of a floating tag.
