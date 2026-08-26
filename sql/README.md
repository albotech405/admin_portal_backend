# sql/ — Database Migrations

Files here are applied automatically on every deploy, in filename-sorted order,
by `app/core/migrations.py` (hooked into FastAPI's startup lifespan in
`app/main.py`, before the background scheduler starts).

## Workflow going forward

1. Add a new file named `YYYYMMDD_short_description.sql` (chronological sort =
   application order; add a numeric suffix if you need more than one file on
   the same date, e.g. `202608271_foo.sql`, `202608272_bar.sql`).
2. Commit and push. On the next Render deploy, the app applies it automatically
   at startup, before serving any traffic.
3. No manual Supabase SQL editor step needed anymore for new files.

## Rules for new files

- Each file runs once, ever (tracked in the `schema_migrations` ledger table by
  filename) — it is **never** re-run automatically, even if edited after being
  applied. To fix a mistake, add a **new** file with corrective SQL; do not edit
  an already-applied file.
- Each file runs as a single script inside one transaction; if anything in it
  fails, the whole file rolls back and the app **refuses to start** (fails
  loudly rather than silently serving a half-migrated schema). Check Render's
  deploy logs for `[migrations]` lines if a deploy doesn't come up.
- Do not use `CREATE INDEX CONCURRENTLY` (cannot run inside a transaction) or
  any other statement that requires running outside a transaction block — this
  runner always wraps each file in one.
- There is no rollback/down-migration mechanism. To undo a change, write a new
  forward-only file that reverses it.
- Set `AUTO_MIGRATE_ENABLED=false` in Render as an escape hatch if a bad
  migration file is blocking a deploy and you need the app to boot while you
  fix the file (it will re-attempt on the next deploy with it back to `true`).

## History

The 21 files that existed before this auto-runner shipped were applied manually
via the Supabase SQL editor across earlier sessions. They're hardcoded into
`BOOTSTRAP_APPLIED_FILENAMES` in `app/core/migrations.py`, so the runner treats
them as already-applied on its first boot without re-executing them.
