# Backend Prompt: Verify SOS Live-Location Runtime Fix

## Goal
Verify and deploy the SOS live-location null-handling fix for:

- `GET /api/v1/live-location/admin/sos/:sosSessionId`

## Required Checks
- Missing realtime participant rows must not cause `.get(...)` crashes.
- Optional nested payloads should be normalized as empty objects.
- If there is no active participant location row, the response should still be built from SOS record fields.
- SOS coordinate fallback should accept both `last_latitude` and `last_longitude`, and `latitude` and `longitude` when the `last_*` fields are absent.
- The endpoint must not leak raw Python exception text in the API response.

## Validation Target
Use SOS session:

- `308b680d-79b8-4ca4-9330-32e49693c345`

Confirm:

- `GET /api/v1/live-location/admin/sos/308b680d-79b8-4ca4-9330-32e49693c345` returns `200`
- The response contains mappable coordinates
- The response does not contain `NoneType` error text

## Regression Coverage Added
- Unit coverage for building an SOS live-location session when realtime rows are missing but SOS coordinates and customer identity are present.