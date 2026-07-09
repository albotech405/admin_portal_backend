# Backend Prompt: Fix Admin Login For Portal

## Goal
Restore admin sign-in for the portal at `http://localhost:3000` using:

- Email: `kagiso.thobane.73@gmail.com`
- Password: `123456`

## Confirmed Issue
The old Supabase auth host `hydocrtebmnxkrqqduwa.supabase.co` does not resolve and causes browser login failures before password auth completes.

Current browser validation after routing auth to the reachable project shows a new blocker:

- `422 Email logins are disabled`

That means the frontend is now reaching the correct Supabase project, but password-based email auth is not enabled there yet.

## Current Working Supabase Project
Use the reachable project ref:

- `yphyaoefwawmsrelnnqd.supabase.co`

## Required Checks
1. Update the frontend env values to the reachable project URL and matching anon key.
2. Confirm the Supabase Auth password provider is enabled.
3. Confirm the admin user exists in Supabase Auth with the email above.
4. Confirm the app database has the matching admin role record for that user, including the expected admin role and active status.
5. Confirm login from `http://localhost:3000` can reach `POST /auth/v1/token?grant_type=password` without DNS, CORS, or `Failed to fetch` errors.

## Acceptance Criteria
- The portal signs in successfully with the admin credentials above.
- The session persists after refresh.
- The app routes to the admin dashboard after sign-in.
- Browser console shows no DNS resolution failures during auth.