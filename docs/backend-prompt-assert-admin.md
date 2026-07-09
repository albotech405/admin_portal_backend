# Backend Prompt: Restore `public.assert_admin(uuid)`

## Problem
The admin portal is surfacing:

- `function public.assert_admin(uuid) does not exist`
- `code: 42883`

## Confirmed State
The production database currently does not expose either of these helpers:

- `public.is_admin_user(uuid)`
- `public.assert_admin(uuid)`

That means the RPCs that call `public.assert_admin(...)` are failing before their permission checks can run.

## Required Fix
1. Deploy `public.is_admin_user(uuid)` and `public.assert_admin(uuid)` to the production database.
2. Keep both functions under `public` with the `uuid` signature expected by the backend.
3. Use `security definer` and `set search_path = public` so PostgREST can resolve them consistently.
4. Reload the PostgREST schema cache after deployment.
5. Confirm admin-only RPCs and routes return `403` for non-admin users instead of bubbling `42883` as a `500`.

## Expected Behavior
- Valid admin users can approve drivers and other admin-only actions.
- Invalid admin users fail with `401/403`.
- No `function public.assert_admin(uuid) does not exist` errors appear in production logs or browser responses.

## Acceptance Criteria
- The admin portal no longer fails on any `assert_admin`-backed route.
- Driver approval, wallet approval, and other admin RPCs complete successfully.