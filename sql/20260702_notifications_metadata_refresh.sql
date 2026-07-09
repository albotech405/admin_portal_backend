-- Restore the notifications metadata column required by create_system_notification.

alter table public.notifications
  add column if not exists metadata jsonb not null default '{}'::jsonb;

create index if not exists notifications_metadata_gin_idx
  on public.notifications using gin (metadata);

notify pgrst, 'reload schema';