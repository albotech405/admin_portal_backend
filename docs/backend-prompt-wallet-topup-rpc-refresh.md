# Backend Prompt: Restore Wallet Top-Up Approval RPC

## Problem
Wallet top-up approval fails with:

- `PGRST202`
- `Could not find the function public.approve_wallet_topup(p_admin_id, p_request_id) in the schema cache`

## Observed Behavior
The API calls the RPC with named arguments:

- `p_request_id`
- `p_admin_id`

The function either is not deployed in the current Supabase database, or PostgREST has stale function metadata.

## Required Fix
1. Deploy or re-deploy `public.approve_wallet_topup(uuid, uuid)` in the current Supabase database.
2. Ensure the function argument names are exactly:
   - `p_admin_id`
   - `p_request_id`
3. Confirm the function is `security definer` and uses `set search_path = public`.
4. Reload the PostgREST schema cache after deployment (`notify pgrst, 'reload schema'`).
5. Ensure `public.assert_admin(...)` exists and approving admins have `is_admin = true` in `public.users`.

## Acceptance Criteria
- `PATCH /api/v1/wallet/admin/topup/requests/{request_id}/approve` succeeds.
- No `PGRST202` is returned.
- The top-up request moves from `pending` to `approved`.
- The driver wallet balance increases by the requested amount.
- A wallet transaction, notification, and admin log entry are created.
