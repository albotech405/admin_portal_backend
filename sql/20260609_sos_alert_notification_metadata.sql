begin;

alter table public.notifications
  add column if not exists metadata jsonb not null default '{}'::jsonb;

create index if not exists notifications_metadata_gin_idx
  on public.notifications using gin (metadata);

alter table public.sos_driver_alerts
  add column if not exists distance_to_incident_km numeric(8,3),
  add column if not exists delivery_status text,
  add column if not exists delivery_error text,
  add column if not exists metadata jsonb not null default '{}'::jsonb;

create index if not exists sos_driver_alerts_session_notified_idx
  on public.sos_driver_alerts (sos_session_id, notified_at desc);

commit;
