-- Restore the admin guard helpers used by RPC functions and refresh the schema cache.

create or replace function public.is_admin_user(p_user_id uuid)
returns boolean
language sql
stable
security definer
set search_path = public
as $$
  select exists(
    select 1
    from public.users
    where id = p_user_id
      and is_admin = true
  );
$$;

create or replace function public.assert_admin(p_admin_id uuid)
returns uuid
language plpgsql
security definer
set search_path = public
as $$
begin
  if p_admin_id is null then
    raise exception 'admin_id is required';
  end if;

  if not public.is_admin_user(p_admin_id) then
    raise exception 'admin access required';
  end if;

  return p_admin_id;
end;
$$;

notify pgrst, 'reload schema';