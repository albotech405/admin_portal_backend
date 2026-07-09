-- Restore notification helper functions and align enum casts with production schema.

create or replace function public.pick_notification_type(preferred_labels text[])
returns text
language plpgsql
stable
security definer
set search_path = public
as $$
declare
  selected_label text;
begin
  select enum_values.enumlabel
  into selected_label
  from (
    select e.enumlabel, array_position(preferred_labels, e.enumlabel) as preferred_rank
    from pg_type t
    join pg_enum e on e.enumtypid = t.oid
    where t.typname = 'notificationtype'
  ) enum_values
  order by coalesce(enum_values.preferred_rank, 2147483647), enum_values.enumlabel
  limit 1;

  return selected_label;
end;
$$;

create or replace function public.pick_notification_status(preferred_labels text[])
returns text
language plpgsql
stable
security definer
set search_path = public
as $$
declare
  selected_label text;
begin
  select enum_values.enumlabel
  into selected_label
  from (
    select e.enumlabel, array_position(preferred_labels, e.enumlabel) as preferred_rank
    from pg_type t
    join pg_enum e on e.enumtypid = t.oid
    where t.typname = 'notificationstatus'
  ) enum_values
  order by coalesce(enum_values.preferred_rank, 2147483647), enum_values.enumlabel
  limit 1;

  return selected_label;
end;
$$;

create or replace function public.create_system_notification(
  p_user_id uuid,
  p_title text,
  p_content text,
  p_type_preferences text[] default array['system', 'payment_update', 'wallet', 'general'],
  p_status_preferences text[] default array['unread', 'pending', 'sent']
)
returns void
language plpgsql
security definer
set search_path = public
as $$
declare
  selected_type text;
  selected_status text;
begin
  selected_type := public.pick_notification_type(p_type_preferences);
  selected_status := public.pick_notification_status(p_status_preferences);

  if selected_type is null then
    selected_type := 'system';
  end if;

  if selected_status is null then
    selected_status := 'unread';
  end if;

  insert into public.notifications (
    user_id,
    title,
    content,
    notification_type,
    status,
    metadata,
    created_at
  ) values (
    p_user_id,
    p_title,
    p_content,
    selected_type::notificationtype,
    selected_status::notificationstatus,
    '{}'::jsonb,
    now()
  );
end;
$$;

notify pgrst, 'reload schema';