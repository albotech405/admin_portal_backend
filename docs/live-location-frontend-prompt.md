# Frontend Prompt: Live Location Tracking Integration

## Context

This is an admin portal frontend. The backend is a FastAPI app with a base URL of `http://localhost:8000` (production: configured via `PUBLIC_BASE_URL`). All admin REST calls use the prefix `/api/v1`. Authentication is via a JWT bearer token obtained at login.

You need to implement **two live location tracking views**:

1. **Active Rides Map** — shows all active rides with live driver and customer positions, updated in real time via WebSocket.
2. **SOS Sessions Map** — shows all active SOS sessions with live distressed-driver positions, also updated via WebSocket.

---

## 1. Authentication

All REST endpoints require:
```
Authorization: Bearer <admin_jwt>
```

The WebSocket connection authenticates via a query parameter (see below).

---

## 2. WebSocket Connection

Connect **once** per admin session:

```
wss://<host>/api/v1/ws/<admin_user_id>?token=<admin_jwt>
```

- `admin_user_id` — the UUID of the logged-in admin.
- `token` — the admin JWT (same token used for REST calls).
- If authentication fails the server closes with code `4001`.
- Reconnect with exponential back-off on unexpected disconnection.

### Incoming message format

All messages are JSON:

```json
{ "event": "<event_name>", "data": { ... } }
```

### Events to handle

| `event` | Trigger | Key fields in `data` |
|---|---|---|
| `driver_location_update` | Driver GPS changed on an active ride | `ride_id`, `latitude`, `longitude`, `heading`, `speed`, `accuracy`, `updated_at` |
| `customer_location_update` | Customer GPS changed on an active ride | `ride_id`, `latitude`, `longitude`, `heading`, `speed`, `accuracy`, `updated_at` |
| `sos_location_update` | Distressed driver moved during SOS | `session_id`, `latitude`, `longitude`, `heading`, `speed`, `accuracy`, `last_update` |
| `sos_triggered` | New SOS session started | `session_id` (re-fetch the SOS list when received) |

---

## 3. Active Rides Map

### 3a. Fetch initial data

```
GET /api/v1/rides/admin/active
```

Response — array of `ActiveTripItem`:

```jsonc
[
  {
    "id": "<ride_uuid>",
    "ride_request_id": "<uuid | null>",
    "customer_id": "<uuid>",
    "driver_id": "<uuid | null>",
    "customer_name": "Jane Doe",
    "customer_phone": "+243...",
    "driver_name": "John Smith",
    "driver_phone": "+243...",
    "picking_point": { /* GeoJSON or {lat, lng} object */ },
    "destination":   { /* GeoJSON or {lat, lng} object */ },
    "price": 1500.0,
    "status": "in_progress",          // "in_progress" | "driver_en_route" | "arrived"
    "started_at": "2026-05-26T10:00:00Z",
    "created_at": "2026-05-26T09:55:00Z",
    "driver_latitude": -4.3217,       // from driver_profiles (may be null)
    "driver_longitude": 15.3219,
    "duration_minutes": null,
    "distance_km": null
  }
]
```

### 3b. Per-ride detailed location

To get the live position of **both** driver and customer for a specific ride:

```
GET /api/v1/rides/<ride_id>/location
```

Response:

```jsonc
{
  "ride_id": "<uuid>",
  "driver": {
    "latitude": -4.3217,
    "longitude": 15.3219,
    "heading": 270.0,
    "speed": 45.5,
    "accuracy": 5.0,
    "updated_at": "2026-05-26T10:05:00Z"
  },
  "customer": null   // null if the customer has not pushed a location yet
}
```

### 3c. Real-time updates

Listen on the WebSocket for `driver_location_update` and `customer_location_update`. Match on `data.ride_id` and update the corresponding marker.

### 3d. Map behaviour

- Place a **driver marker** (car icon) at `driver_latitude` / `driver_longitude` (or from the `/location` endpoint).
- Place a **customer marker** (person icon) if `customer` location is available.
- Place a **pickup pin** and **destination pin** using `picking_point` and `destination` (these may be GeoJSON `Point` objects — extract `coordinates[1]` for lat and `coordinates[0]` for lng, or a plain `{ lat, lng }` object).
- Rotate driver/customer markers by `heading` if provided.
- Show a sidebar list of active rides; clicking a ride pans the map to that ride.
- Poll `GET /api/v1/rides/admin/active` every 30 s as a fallback to catch rides that the WebSocket missed.

---

## 4. SOS Sessions Map

### 4a. Fetch active sessions

```
GET /api/v1/sos/active
```

Response — array of `SosSessionItem` (see detail structure below for relevant fields).

### 4b. Fetch session detail

```
GET /api/v1/sos/session/<session_id>
```

Response — `SosSessionDetailResponse`:

```jsonc
{
  "id": "<uuid>",
  "user_id": "<uuid>",
  "full_name": "Driver Name",
  "phone_number": "+243...",
  "is_active": true,
  "triggered_at": "2026-05-26T10:10:00Z",
  "last_latitude": -4.3300,
  "last_longitude": 15.3100,
  "last_heading": 90.0,
  "last_location_update": "2026-05-26T10:11:00Z",
  "ride_id": "<uuid | null>",
  "triggered_by_driver": true,
  "alert_radius_km": 15.0,
  "tracking_url": "https://<host>/api/v1/sos/track/<token>/map",
  "responders": [ ... ]
}
```

### 4c. Public tracking page (no auth)

A self-contained Leaflet HTML page is available at:

```
GET /api/v1/sos/track/<tracking_token>/map
```

You can open this in an iframe or a new tab. The token is included in the session detail under `tracking_url`.

### 4d. Public polling endpoint (no auth)

```
GET /api/v1/sos/track/<tracking_token>
```

Returns:

```jsonc
{
  "session_id": "<uuid>",
  "is_active": true,
  "triggered_at": "...",
  "triggered_by_driver": true,
  "ride_id": "<uuid | null>",
  "latitude": -4.3300,
  "longitude": 15.3100,
  "heading": 90.0,
  "last_update": "..."
}
```

Use this as a WebSocket fallback if the admin connection is unavailable.

### 4e. Real-time updates

Listen on the WebSocket for:
- `sos_location_update` — update the session marker. Match on `data.session_id`.
- `sos_triggered` — re-fetch the active SOS list.

### 4f. Map behaviour

- Place a **red alert marker** for each active SOS session at `last_latitude` / `last_longitude`.
- Rotate by `last_heading` if provided.
- Draw a circle with radius `alert_radius_km` km around the marker to show the alert zone.
- Show session age (time since `triggered_at`) as a badge.
- Clicking a session opens a details panel showing `full_name`, `phone_number`, `ride_id`, responders list, and a link/button to open the public tracking page (`tracking_url`).
- Poll `GET /api/v1/sos/active` every 15 s as a fallback.

---

## 5. Suggested Component Structure

```
LiveMap/
  ActiveRidesMap.tsx      — map + sidebar for active rides
  SosSessionsMap.tsx      — map + sidebar for SOS sessions
  useAdminWebSocket.ts    — singleton WebSocket hook with reconnect logic
  useActiveRides.ts       — REST polling + WS patch-in
  useSosSessions.ts       — REST polling + WS patch-in
  mapUtils.ts             — GeoJSON → {lat, lng} helpers, heading → rotation
```

---

## 6. Notes

- The `picking_point` and `destination` columns may be stored as PostGIS GeoJSON (`{ "type": "Point", "coordinates": [lng, lat] }`) or as a plain object. Handle both.
- `driver_latitude` / `driver_longitude` on `ActiveTripItem` come from `driver_profiles` and may lag behind the `ride_location_updates` table. Prefer the WebSocket / `/location` endpoint values once available.
- The WebSocket manager keeps one connection per `admin_user_id`. If you open multiple browser tabs, only the most recently connected tab will receive events. Consider a shared worker or BroadcastChannel if multi-tab support is needed.
- All timestamps are ISO 8601 UTC strings.
