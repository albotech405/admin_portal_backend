begin;

create table if not exists public.sos_location_updates (
  id uuid primary key default gen_random_uuid(),
  sos_session_id uuid not null references public.sos_sessions(id) on delete cascade,
  actor_user_id uuid,
  latitude double precision not null,
  longitude double precision not null,
  heading double precision,
  speed double precision,
  accuracy double precision,
  recorded_at timestamptz not null default now()
);

create index if not exists sos_location_updates_session_recorded_idx
  on public.sos_location_updates (sos_session_id, recorded_at desc);

alter table public.sos_location_updates enable row level security;

create policy "sos_location_updates_insert" on public.sos_location_updates
  for insert with check (true);

create policy "sos_location_updates_select" on public.sos_location_updates
  for select using (true);

commit;
