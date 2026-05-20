-- Migration: device_tokens table + notification read_at column
-- Run this once against your Supabase project.

begin;

-- 1. Device tokens table for FCM push notifications
create table if not exists public.device_tokens (
  id          uuid primary key default gen_random_uuid(),
  user_id     uuid not null references public.users(id) on delete cascade,
  token       text not null,
  platform    text not null default 'android', -- 'android' | 'ios'
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now(),
  unique (user_id, token)
);

create index if not exists idx_device_tokens_user_id
  on public.device_tokens (user_id);

-- 2. Add read_at to notifications if it doesn't already exist
alter table public.notifications
  add column if not exists read_at timestamptz;

-- 3. Index for fast unread-count queries
create index if not exists idx_notifications_user_unread
  on public.notifications (user_id, read_at)
  where read_at is null;

-- 4. Row-level security for device_tokens
alter table public.device_tokens enable row level security;

drop policy if exists device_tokens_user_self on public.device_tokens;
create policy device_tokens_user_self
  on public.device_tokens
  for all
  using (user_id = (
    select id from public.users where supabase_uid = auth.uid()::text limit 1
  ))
  with check (user_id = (
    select id from public.users where supabase_uid = auth.uid()::text limit 1
  ));

drop policy if exists device_tokens_admin_all on public.device_tokens;
create policy device_tokens_admin_all
  on public.device_tokens
  for all
  using (public.is_admin_user(auth.uid()))
  with check (public.is_admin_user(auth.uid()));

-- 5. notifications: allow users to update their own read_at
drop policy if exists notifications_user_self_update on public.notifications;
create policy notifications_user_self_update
  on public.notifications
  for update
  using (user_id = (
    select id from public.users where supabase_uid = auth.uid()::text limit 1
  ))
  with check (user_id = (
    select id from public.users where supabase_uid = auth.uid()::text limit 1
  ));

commit;
