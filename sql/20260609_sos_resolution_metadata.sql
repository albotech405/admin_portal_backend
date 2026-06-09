begin;

alter table public.sos_sessions
  add column if not exists resolved_at timestamptz,
  add column if not exists resolved_by uuid null references public.users(id) on delete set null,
  add column if not exists resolution_notes text;

create index if not exists sos_sessions_resolved_at_idx
  on public.sos_sessions (resolved_at desc);

update public.sos_sessions
set resolved_at = coalesce(resolved_at, cancelled_at)
where is_active = false
  and cancelled_at is not null
  and resolved_at is null;

commit;
