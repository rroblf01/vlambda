# CLI reference

The CLI runs from the same package as the server:

```bash
python -m vlambda <command> [options]
```

`--version` prints the version. Commands that talk to a server use
`VLAMBDA_URL`/`VLAMBDA_TOKEN` or `--url`/`--token`. `backup`, `restore`,
`secrets` and `reset` are offline and do not need a token.

## Server

```bash
python -m vlambda --port 8080 --sqlite_path ./vlambda.db --num_coroutines 10
```

| Flag | Env | Default |
|---|---|---|
| `--port` | `VLAMBDA_PORT` | 8080 |
| `--sqlite_path` | `VLAMBDA_DB` | `./vlambda.db` |
| `--num_coroutines` | `VLAMBDA_COROUTINES` | 10 |
| `--max_body_size` | `VLAMBDA_MAX_BODY` | 1048576 |
| `--rate_limit` | `VLAMBDA_RATE_LIMIT` | 100 |
| `--invoke_timeout` | `VLAMBDA_TIMEOUT` | 30 |
| `--max_memory_mb` | `VLAMBDA_MAX_MEMORY_MB` | 0 |
| `--cors_origin` | `VLAMBDA_CORS` | `*` |
| `--cert` | `VLAMBDA_CERT` | |
| `--key` | `VLAMBDA_KEY` | |

## invoke

```bash
python -m vlambda invoke <slug> [--method GET] [--data BODY] [--query 'a=1' ]
  [--header 'X-Test: yes'] [--content-type application/json] [--raw]
```

## deploy

Create or update a code lambda from a file.

```bash
python -m vlambda deploy main.py --name my-lambda [--slug <slug>] [--method GET]
```

## logs

```bash
python -m vlambda logs [--days 1] [--slug <slug>]
```

## env

Set or list environment variables and secrets for a lambda.

```bash
python -m vlambda env <slug> [--set KEY=value] [--secret KEY=value]
```

At least one `--set` or `--secret` performs a write; no flags reads.

## kv

```bash
python -m vlambda kv <slug> [--prefix P] [--get KEY] [--set KEY=value]
  [--delete KEY] [--clear] [--ttl SECONDS]
```

## egress

```bash
python -m vlambda egress <slug> [--set HOST] [--clear]
```

`--set` is repeatable.

## schedule

```bash
python -m vlambda schedule list <slug>
python -m vlambda schedule create <slug> --name nightly --cron '0 3 * * *' \
  [--timezone UTC] [--method POST] [--path /schedule] [--payload '{}'] [--disabled]
python -m vlambda schedule delete <slug> <id>
python -m vlambda schedule run <slug> <id>
python -m vlambda schedule runs <slug> <id>
```

## openapi

```bash
python -m vlambda openapi [--out openapi.json]
```

Without `--out`, prints the document to stdout.

## task

```bash
python -m vlambda task <task-id>
```

## backup

Create an offline archive. Safe while the server is running.

```bash
python -m vlambda backup --out backup.tar.gz [--db PATH] [--metrics-db PATH]
  [--projects-dir PATH] [--packages-dir PATH] [--with-packages]
```

## restore

Stop the server first.

```bash
python -m vlambda restore --from backup.tar.gz [--force]
  [--allow-key-mismatch] [--db PATH] [--metrics-db PATH]
  [--projects-dir PATH] [--packages-dir PATH]
```

## secrets rotate

Re-encrypt stored secrets from one encryption key to another.

```bash
python -m vlambda secrets rotate --old-key "$OLD" --new-key "$NEW" [--db PATH]
```

The command is idempotent and does not require the server.

## reset

```bash
python -m vlambda reset --yes [--url URL --token ADMIN_TOKEN]
```

Add `--wipe` to delete the SQLite database files instead of only their rows.
This is the supported way to start a 1.0 deployment from a pre-1.0 database.
