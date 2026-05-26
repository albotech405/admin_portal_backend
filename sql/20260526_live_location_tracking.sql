-- Live GPS tracking tables for active rides and SOS sessions.
-- Ride location updates: stores the latest driver / customer position per ride.
-- Uses INSERT … ON CONFLICT UPDATE so there is always at most one row per (ride, role).

create table if not exists public.ride_location_updates (
  id           uuid        primary key default gen_random_uuid(),
  ride_id      uuid        not null references public.rides(id) on delete cascade,
  role         text        not null check (role in ('driver', 'customer')),
  latitude     double precision not null,
  longitude    double precision not null,
  heading      double precision,
  speed        double precision,
  accuracy     double precision,
  updated_at   timestamptz not null default now()
);

-- One "current" record per ride+role
create unique index if not exists ride_location_updates_ride_role_uidx
  on public.ride_location_updates (ride_id, role);

-- Fast lookups by ride
create index if not exists ride_location_updates_ride_idx
  on public.ride_location_updates (ride_id);

-- Row-level security: any authenticated user can upsert their own row; admins can read all.
alter table public.ride_location_updates enable row level security;

create policy "ride_location_rls_insert" on public.ride_location_updates
  for insert with check (true);   -- mobile app (authenticated) may insert

create policy "ride_location_rls_select" on public.ride_location_updates
  for select using (true);        -- all authenticated can read (admin portal uses service key)

create policy "ride_location_rls_update" on public.ride_location_updates
  for update using (true);

-- SOS sessions: add a short, unguessable public-tracking token and
-- an optional heading field so we can display direction on the admin map.
alter table public.sos_sessions
  add column if not exists tracking_token text unique,
  add column if not exists last_heading   double precision;

-- Backfill tracking tokens for existing sessions that don't have one.
update public.sos_sessions
set    tracking_token = encode(gen_random_bytes(16), 'hex')
where  tracking_token is null;

-- Make the token non-nullable now that all rows have one.
alter table public.sos_sessions
  alter column tracking_token set not null,
  alter column tracking_token set default encode(gen_random_bytes(16), 'hex');

create index if not exists sos_sessions_tracking_token_idx
  on public.sos_sessions (tracking_token);
