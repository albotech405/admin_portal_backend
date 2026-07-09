# Backend Prompt: Restore notifications.metadata

## Problem
Driver approval is failing with:

- `column "metadata" of relation "notifications" does not exist`
- `code: 42703`

## Cause
`public.create_system_notification(...)` inserts into `public.notifications.metadata`, but the production table is missing that column.

## Required Fix
1. Add `metadata jsonb not null default '{}'::jsonb` to `public.notifications`.
2. Recreate the `notifications_metadata_gin_idx` index if needed.
3. Reload the PostgREST schema cache after the migration.

## Acceptance Criteria
- Driver approval completes without `42703`.
- Notification rows are created with metadata preserved.