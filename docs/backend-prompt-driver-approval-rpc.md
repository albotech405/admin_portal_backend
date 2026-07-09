# Backend Prompt: Restore Driver Approval RPC

## Problem
Driver approval fails with:

- `PGRST202`
- `Could not find the function public.approve_driver(p_admin_id, p_driver_id) in the schema cache`

## Observed Behavior
The app already calls the RPC with named arguments:

- `p_driver_id`
- `p_admin_id`

The database either does not have `public.approve_driver` deployed, or PostgREST has not refreshed its schema cache after the function was added.

## Required Fix
1. Deploy or re-deploy `public.approve_driver(uuid, uuid default null)` in the current Supabase database.
2. Confirm the function is `security definer` and uses `set search_path = public`.
3. Reload the PostgREST schema cache after deployment.
4. Confirm `public.driver_profiles` has the expected `verification_status`, `verification_feedback`, `activation_date`, `is_suspended`, and `updated_at` columns.
5. Verify the approving admin passes `public.assert_admin(...)` and has a valid row in `public.users` with `is_admin = true`.

## Acceptance Criteria
- `PATCH /api/v1/drivers/{driver_id}/activate` succeeds.
- No `PGRST202` is returned.
- The driver profile moves to `approved`.
- The system notification and admin log are created.