"""
Admin portal HTML pages — served directly by FastAPI.

Routes (no /api/v1 prefix):
  GET /admin/rides/live   — Active Rides live map (admin-authenticated via localStorage JWT)
  GET /admin/sos/live     — SOS Live Monitor      (admin-authenticated via localStorage JWT)

Authentication is handled client-side: the pages read the JWT from localStorage
and use it for every REST/WebSocket call.  A 401/403 response from the API
triggers an in-page error banner.
"""

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from app.core.config import settings

router = APIRouter(prefix="/admin", tags=["admin-ui"])

# ── Shared JS helpers injected into every page ────────────────────────────────

_COMMON_JS = """
  const API   = "{base}/api/v1";
  const WS_URL = "{ws_base}/api/v1/ws";

  // Read JWT stored by the login page
  function getToken() {{
    return localStorage.getItem("admin_token") || sessionStorage.getItem("admin_token") || "";
  }}

  function authHeaders() {{
    return {{ Authorization: "Bearer " + getToken() }};
  }}

  async function apiFetch(path, opts = {{}}) {{
    const res = await fetch(API + path, {{
      ...opts,
      headers: {{ ...authHeaders(), ...(opts.headers || {{}}) }},
    }});
    if (res.status === 401 || res.status === 403) {{
      showError("Session expired or insufficient permissions. Please log in again.");
      throw new Error("Unauthorized");
    }}
    if (res.status === 404) return null;          // caller decides
    if (!res.ok) throw new Error(await res.text());
    return res.json();
  }}

  function showError(msg) {{
    const b = document.getElementById("error-banner");
    if (b) {{ b.textContent = msg; b.style.display = "block"; }}
  }}
"""


def _common_js() -> str:
    base = settings.PUBLIC_BASE_URL.rstrip("/")
    ws_base = base.replace("https://", "wss://").replace("http://", "ws://")
    return _COMMON_JS.format(base=base, ws_base=ws_base)


# ── Active Rides Live Map ──────────────────────────────────────────────────────

_RIDES_MAP_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>Live Rides Map — AlboTax Admin</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
  <style>
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{font-family:system-ui,sans-serif;background:#0f1117;color:#e0e0e0;display:flex;flex-direction:column;height:100vh}}
    #topbar{{background:#1c2233;padding:10px 16px;display:flex;align-items:center;gap:12px;border-bottom:1px solid #2a3250}}
    #topbar h1{{font-size:1rem;font-weight:700;color:#fff;flex:1}}
    #ride-count{{background:#2563eb;color:#fff;padding:3px 10px;border-radius:999px;font-size:.8rem;font-weight:600}}
    #ws-dot{{width:10px;height:10px;border-radius:50%;background:#ef4444}}
    #ws-dot.connected{{background:#22c55e}}
    #error-banner{{display:none;background:#7f1d1d;color:#fca5a5;padding:8px 16px;font-size:.85rem}}
    #body{{display:flex;flex:1;overflow:hidden}}
    #sidebar{{width:300px;min-width:260px;background:#141926;overflow-y:auto;border-right:1px solid #2a3250;padding:8px}}
    #sidebar h2{{font-size:.8rem;color:#94a3b8;text-transform:uppercase;letter-spacing:.06em;padding:6px 4px}}
    .ride-card{{background:#1c2233;border-radius:6px;padding:8px 10px;margin-bottom:6px;cursor:pointer;border:1px solid transparent;font-size:.82rem}}
    .ride-card:hover,.ride-card.active{{border-color:#3b82f6}}
    .ride-card .name{{font-weight:600;color:#e2e8f0}}
    .ride-card .sub{{color:#94a3b8;margin-top:2px}}
    .status-pill{{display:inline-block;padding:2px 7px;border-radius:999px;font-size:.72rem;font-weight:600;margin-left:6px}}
    .in_progress{{background:#166534;color:#bbf7d0}}
    .driver_en_route{{background:#1e3a5f;color:#93c5fd}}
    .arrived{{background:#713f12;color:#fde68a}}
    #map{{flex:1}}
  </style>
</head>
<body>
  <div id="topbar">
    <span>🗺</span>
    <h1>Active Rides — Live Map</h1>
    <span id="ride-count">0 rides</span>
    <span id="ws-dot" title="WebSocket status"></span>
  </div>
  <div id="error-banner"></div>
  <div id="body">
    <div id="sidebar">
      <h2>Active Rides</h2>
      <div id="ride-list"></div>
    </div>
    <div id="map"></div>
  </div>

  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <script>
    {common_js}

    /* ── Map setup ── */
    const map = L.map("map").setView([-4.32, 15.32], 12);
    L.tileLayer("https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png", {{
      attribution: "© OpenStreetMap contributors"
    }}).addTo(map);

    /* ── State ── */
    const rides = {{}};          // ride_id → {{data, driverMarker, customerMarker, line}}

    /* ── Icons ── */
    function driverIcon(heading) {{
      const deg = heading ?? 0;
      return L.divIcon({{
        html: `<div style="transform:rotate(${{deg}}deg);font-size:20px;line-height:1">🚕</div>`,
        className: "", iconSize: [24, 24], iconAnchor: [12, 12]
      }});
    }}
    const customerIcon = L.divIcon({{
      html: '<div style="width:14px;height:14px;background:#3b82f6;border:3px solid #fff;border-radius:50%;box-shadow:0 2px 4px rgba(0,0,0,.4)"></div>',
      className: "", iconSize: [14, 14], iconAnchor: [7, 7]
    }});

    /* ── Sidebar card ── */
    function renderCard(ride) {{
      let el = document.getElementById("card-" + ride.id);
      const statusCls = ride.status.replace(/_/g, "_");
      const html = `
        <div class="name">
          ${{ride.driver_name || "Driver"}}
          <span class="status-pill ${{ride.status}}">${{ride.status.replace(/_/g," ")}}</span>
        </div>
        <div class="sub">Customer: ${{ride.customer_name || "—"}}</div>
        <div class="sub">Price: ${{ride.price}} CDF</div>`;
      if (!el) {{
        el = document.createElement("div");
        el.className = "ride-card";
        el.id = "card-" + ride.id;
        el.addEventListener("click", () => focusRide(ride.id));
        document.getElementById("ride-list").appendChild(el);
      }}
      el.innerHTML = html;
    }}

    function focusRide(id) {{
      const r = rides[id];
      if (!r) return;
      document.querySelectorAll(".ride-card").forEach(c => c.classList.remove("active"));
      const card = document.getElementById("card-" + id);
      if (card) card.classList.add("active");
      if (r.driverMarker) map.panTo(r.driverMarker.getLatLng());
    }}

    /* ── Add / update map layers for a ride ── */
    function upsertRide(ride) {{
      if (!rides[ride.id]) rides[ride.id] = {{ data: ride }};
      rides[ride.id].data = ride;
      renderCard(ride);
      updateCount();
    }}

    function setDriverLocation(rideId, lat, lng, heading) {{
      if (!rides[rideId]) return;
      const r = rides[rideId];
      const latlng = [lat, lng];
      if (!r.driverMarker) {{
        r.driverMarker = L.marker(latlng, {{ icon: driverIcon(heading) }})
          .bindTooltip(r.data.driver_name || "Driver")
          .addTo(map);
      }} else {{
        r.driverMarker.setLatLng(latlng);
        r.driverMarker.setIcon(driverIcon(heading));
      }}
      refreshLine(rideId);
    }}

    function setCustomerLocation(rideId, lat, lng) {{
      if (!rides[rideId]) return;
      const r = rides[rideId];
      const latlng = [lat, lng];
      if (!r.customerMarker) {{
        r.customerMarker = L.marker(latlng, {{ icon: customerIcon }})
          .bindTooltip(r.data.customer_name || "Customer")
          .addTo(map);
      }} else {{
        r.customerMarker.setLatLng(latlng);
      }}
      refreshLine(rideId);
    }}

    function refreshLine(rideId) {{
      const r = rides[rideId];
      if (!r || !r.driverMarker || !r.customerMarker) return;
      const pts = [r.driverMarker.getLatLng(), r.customerMarker.getLatLng()];
      if (!r.line) {{
        r.line = L.polyline(pts, {{ color: "#3b82f6", weight: 2, dashArray: "4 4" }}).addTo(map);
      }} else {{
        r.line.setLatLngs(pts);
      }}
    }}

    function removeRide(rideId) {{
      const r = rides[rideId];
      if (!r) return;
      if (r.driverMarker)   map.removeLayer(r.driverMarker);
      if (r.customerMarker) map.removeLayer(r.customerMarker);
      if (r.line)           map.removeLayer(r.line);
      delete rides[rideId];
      const card = document.getElementById("card-" + rideId);
      if (card) card.remove();
      updateCount();
    }}

    function updateCount() {{
      document.getElementById("ride-count").textContent = Object.keys(rides).length + " rides";
    }}

    /* ── Initial load ── */
    async function loadAll() {{
      const data = await apiFetch("/rides/admin/active");
      if (!data) return;
      for (const ride of data) {{
        upsertRide(ride);
        if (ride.driver_latitude && ride.driver_longitude) {{
          setDriverLocation(ride.id, ride.driver_latitude, ride.driver_longitude, null);
        }}
        // Fetch precise per-ride location (includes heading, customer)
        const loc = await apiFetch(`/rides/${{ride.id}}/location`);
        if (loc) {{
          if (loc.driver) setDriverLocation(ride.id, loc.driver.latitude, loc.driver.longitude, loc.driver.heading);
          if (loc.customer) setCustomerLocation(ride.id, loc.customer.latitude, loc.customer.longitude);
        }}
      }}
    }}

    /* ── WebSocket ── */
    let ws = null;
    function connectWS() {{
      const token = getToken();
      if (!token) {{ setTimeout(connectWS, 3000); return; }}
      // Use a stable pseudo-user-id from the token payload
      let adminId = "admin";
      try {{
        const payload = JSON.parse(atob(token.split(".")[1]));
        adminId = payload.sub || "admin";
      }} catch(_) {{}}

      ws = new WebSocket(`${{WS_URL}}/${{adminId}}?token=${{encodeURIComponent(token)}}`);
      ws.onopen  = () => document.getElementById("ws-dot").classList.add("connected");
      ws.onclose = () => {{
        document.getElementById("ws-dot").classList.remove("connected");
        setTimeout(connectWS, 5000);
      }};
      ws.onerror = () => ws.close();
      ws.onmessage = (evt) => {{
        try {{
          const frame = JSON.parse(evt.data);
          const d = frame.data;
          if (frame.event === "driver_location_update" && rides[d.ride_id]) {{
            setDriverLocation(d.ride_id, d.latitude, d.longitude, d.heading);
          }} else if (frame.event === "customer_location_update" && rides[d.ride_id]) {{
            setCustomerLocation(d.ride_id, d.latitude, d.longitude);
          }}
        }} catch(_) {{}}
      }};
    }}

    /* ── Polling fallback (runs always; reconciles rides list every 30 s) ── */
    async function pollReconcile() {{
      const data = await apiFetch("/rides/admin/active").catch(() => null);
      if (!data) return;
      const activeIds = new Set(data.map(r => r.id));
      // Remove rides no longer active
      for (const id of Object.keys(rides)) {{
        if (!activeIds.has(id)) removeRide(id);
      }}
      // Add new rides
      for (const ride of data) {{
        if (!rides[ride.id]) {{
          upsertRide(ride);
          const loc = await apiFetch(`/rides/${{ride.id}}/location`).catch(() => null);
          if (loc) {{
            if (loc.driver)   setDriverLocation(ride.id, loc.driver.latitude, loc.driver.longitude, loc.driver.heading);
            if (loc.customer) setCustomerLocation(ride.id, loc.customer.latitude, loc.customer.longitude);
          }}
        }}
      }}
    }}

    loadAll();
    connectWS();
    setInterval(pollReconcile, 30000);
    // Location poll per active ride every 5 s (WS fallback)
    setInterval(async () => {{
      for (const rideId of Object.keys(rides)) {{
        const loc = await apiFetch(`/rides/${{rideId}}/location`).catch(() => null);
        if (!loc) {{ removeRide(rideId); continue; }}
        if (loc.driver)   setDriverLocation(rideId, loc.driver.latitude, loc.driver.longitude, loc.driver.heading);
        if (loc.customer) setCustomerLocation(rideId, loc.customer.latitude, loc.customer.longitude);
      }}
    }}, 5000);
  </script>
</body>
</html>"""


@router.get("/rides/live", response_class=HTMLResponse)
def rides_live_map():
    """Admin live map for all active rides."""
    return HTMLResponse(content=_RIDES_MAP_HTML.format(common_js=_common_js()))


# ── SOS Live Monitor ───────────────────────────────────────────────────────────

_SOS_MAP_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>SOS Live Monitor — AlboTax Admin</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
  <style>
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{font-family:system-ui,sans-serif;background:#0f1117;color:#e0e0e0;display:flex;flex-direction:column;height:100vh}}
    #topbar{{background:#7f1d1d;padding:10px 16px;display:flex;align-items:center;gap:12px;border-bottom:1px solid #991b1b}}
    #topbar h1{{font-size:1rem;font-weight:700;color:#fff;flex:1}}
    #sos-count{{background:#dc2626;color:#fff;padding:3px 10px;border-radius:999px;font-size:.8rem;font-weight:600}}
    #ws-dot{{width:10px;height:10px;border-radius:50%;background:#ef4444}}
    #ws-dot.connected{{background:#22c55e}}
    #error-banner{{display:none;background:#450a0a;color:#fca5a5;padding:8px 16px;font-size:.85rem}}
    #new-sos-banner{{display:none;background:#dc2626;color:#fff;padding:10px 16px;font-size:.9rem;font-weight:600;text-align:center;cursor:pointer}}
    #body{{display:flex;flex:1;overflow:hidden}}
    #sidebar{{width:320px;min-width:280px;background:#141926;overflow-y:auto;border-right:1px solid #2a3250;padding:8px}}
    #sidebar h2{{font-size:.8rem;color:#94a3b8;text-transform:uppercase;letter-spacing:.06em;padding:6px 4px 4px}}
    .sos-card{{background:#1c2233;border-radius:6px;padding:10px 12px;margin-bottom:8px;cursor:pointer;border:1px solid transparent;font-size:.82rem}}
    .sos-card:hover,.sos-card.selected{{border-color:#ef4444}}
    .sos-card .name{{font-weight:700;color:#fca5a5;font-size:.9rem}}
    .sos-card .sub{{color:#94a3b8;margin-top:2px;line-height:1.4}}
    .sos-card .actions{{margin-top:8px;display:flex;gap:6px}}
    .btn{{padding:4px 10px;border-radius:4px;border:none;cursor:pointer;font-size:.78rem;font-weight:600}}
    .btn-track{{background:#2563eb;color:#fff}}
    .btn-resolve{{background:#16a34a;color:#fff}}
    .type-driver{{border-left:4px solid #ef4444}}
    .type-customer{{border-left:4px solid #f97316}}
    .responder-list{{margin-top:8px;font-size:.78rem;color:#94a3b8}}
    .responder-list .r-item{{padding:2px 0;border-top:1px solid #1e293b}}
    .r-responded{{color:#4ade80}}
    #detail-panel{{padding:10px}}
    #detail-panel h3{{font-size:.85rem;font-weight:700;color:#e2e8f0;margin-bottom:6px}}
    #map{{flex:1}}
    #no-sessions{{color:#64748b;text-align:center;padding:20px;font-size:.85rem}}
  </style>
</head>
<body>
  <div id="topbar">
    <span style="font-size:1.4rem">🆘</span>
    <h1>SOS Live Monitor</h1>
    <span id="sos-count">0 active</span>
    <span id="ws-dot" title="WebSocket"></span>
  </div>
  <div id="new-sos-banner" onclick="this.style.display='none'">
    ⚠ New SOS session triggered! Click to dismiss.
  </div>
  <div id="error-banner"></div>
  <div id="body">
    <div id="sidebar">
      <h2>Active SOS Sessions</h2>
      <div id="sos-list"><div id="no-sessions">No active SOS sessions</div></div>
    </div>
    <div id="map"></div>
  </div>

  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <script>
    {common_js}

    /* ── Map ── */
    const map = L.map("map").setView([-4.32, 15.32], 12);
    L.tileLayer("https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png", {{
      attribution: "© OpenStreetMap contributors"
    }}).addTo(map);

    const sessions = {{}};  // session_id → {{data, marker, responders[]}}

    /* ── Icons ── */
    function sosIcon(byDriver) {{
      const color = byDriver ? "#dc2626" : "#ea580c";
      return L.divIcon({{
        html: `<div style="width:20px;height:20px;background:${{color}};border:3px solid #fff;border-radius:50%;box-shadow:0 2px 8px rgba(0,0,0,.6);display:flex;align-items:center;justify-content:center;font-size:10px">🆘</div>`,
        className: "", iconSize: [20, 20], iconAnchor: [10, 10]
      }});
    }}

    /* ── Sidebar card ── */
    function renderCard(session) {{
      let el = document.getElementById("scard-" + session.id);
      const typeCls = session.triggered_by_driver ? "type-driver" : "type-customer";
      const typeLabel = session.triggered_by_driver ? "🔴 Driver SOS" : "🟠 Customer SOS";
      const triggeredAt = session.triggered_at ? new Date(session.triggered_at).toLocaleTimeString() : "—";
      const respCount = (sessions[session.id]?.responders || []).length;
      const html = `
        <div class="name">${{typeLabel}}</div>
        <div class="sub"><b>${{session.full_name || "Unknown"}}</b></div>
        <div class="sub">📞 ${{session.phone_number || "—"}}</div>
        <div class="sub">Triggered: ${{triggeredAt}}</div>
        <div class="sub">Responders: ${{session.responder_count || respCount}}</div>
        <div class="actions">
          ${{session.tracking_url
            ? `<button class="btn btn-track" onclick="window.open('${{session.tracking_url}}','_blank')">Open tracking page</button>`
            : ""}}
          <button class="btn btn-resolve" onclick="resolveSession('${{session.id}}')">Resolve</button>
        </div>
        <div class="responder-list" id="resp-${{session.id}}"></div>`;
      if (!el) {{
        el = document.createElement("div");
        el.className = `sos-card ${{typeCls}}`;
        el.id = "scard-" + session.id;
        el.addEventListener("click", (e) => {{
          if (e.target.tagName === "BUTTON") return;
          focusSession(session.id);
        }});
        document.getElementById("sos-list").appendChild(el);
        document.getElementById("no-sessions").style.display = "none";
      }}
      el.innerHTML = html;
      loadResponders(session.id);
    }}

    async function loadResponders(sessionId) {{
      if (!sessions[sessionId]?.data?.triggered_by_driver) return;
      const data = await apiFetch(`/sos/session/${{sessionId}}/responders`).catch(() => null);
      if (!data) return;
      sessions[sessionId].responders = data.responders || [];
      const el = document.getElementById("resp-" + sessionId);
      if (!el) return;
      if (!data.responders.length) {{ el.innerHTML = ""; return; }}
      el.innerHTML = data.responders.map(r =>
        `<div class="r-item ${{r.responded ? "r-responded" : ""}}">${{r.driver_name || r.driver_user_id}} ${{r.responded ? "✓" : ""}}</div>`
      ).join("");
    }}

    function focusSession(id) {{
      document.querySelectorAll(".sos-card").forEach(c => c.classList.remove("selected"));
      const card = document.getElementById("scard-" + id);
      if (card) card.classList.add("selected");
      const s = sessions[id];
      if (s?.marker) map.panTo(s.marker.getLatLng());
    }}

    /* ── Add / update session on map ── */
    function upsertSession(session) {{
      if (!sessions[session.id]) sessions[session.id] = {{ data: session, responders: [] }};
      sessions[session.id].data = session;
      renderCard(session);

      if (session.last_latitude && session.last_longitude) {{
        setMarker(session.id, session.last_latitude, session.last_longitude, session.triggered_by_driver);
      }}
      updateCount();
    }}

    function setMarker(sessionId, lat, lng, byDriver) {{
      const s = sessions[sessionId];
      if (!s) return;
      const latlng = [lat, lng];
      if (!s.marker) {{
        s.marker = L.marker(latlng, {{ icon: sosIcon(byDriver) }})
          .bindTooltip((s.data.full_name || "SOS") + " — click for details")
          .addTo(map)
          .on("click", () => focusSession(sessionId));
      }} else {{
        s.marker.setLatLng(latlng);
      }}
    }}

    function removeSession(sessionId) {{
      const s = sessions[sessionId];
      if (!s) return;
      if (s.marker) map.removeLayer(s.marker);
      delete sessions[sessionId];
      const card = document.getElementById("scard-" + sessionId);
      if (card) card.remove();
      if (!Object.keys(sessions).length)
        document.getElementById("no-sessions").style.display = "";
      updateCount();
    }}

    function updateCount() {{
      const n = Object.keys(sessions).length;
      document.getElementById("sos-count").textContent = n + " active";
    }}

    async function resolveSession(id) {{
      if (!confirm("Mark this SOS session as resolved?")) return;
      await apiFetch(`/sos/admin/sessions/${{id}}/resolve`, {{ method: "PATCH" }}).catch(() => null);
      removeSession(id);
    }}

    /* ── Initial load ── */
    async function loadAll() {{
      const data = await apiFetch("/sos/sessions/active");
      if (!data) return;
      for (const s of data.sessions || []) upsertSession(s);
    }}

    /* ── WebSocket ── */
    let ws = null;
    function connectWS() {{
      const token = getToken();
      if (!token) {{ setTimeout(connectWS, 3000); return; }}
      let adminId = "admin";
      try {{
        const payload = JSON.parse(atob(token.split(".")[1]));
        adminId = payload.sub || "admin";
      }} catch(_) {{}}

      ws = new WebSocket(`${{WS_URL}}/${{adminId}}?token=${{encodeURIComponent(token)}}`);
      ws.onopen  = () => document.getElementById("ws-dot").classList.add("connected");
      ws.onclose = () => {{
        document.getElementById("ws-dot").classList.remove("connected");
        setTimeout(connectWS, 5000);
      }};
      ws.onerror = () => ws.close();
      ws.onmessage = (evt) => {{
        try {{
          const frame = JSON.parse(evt.data);
          const d = frame.data;
          if (frame.event === "sos_location_update" && sessions[d.session_id]) {{
            setMarker(d.session_id, d.latitude, d.longitude,
              sessions[d.session_id].data.triggered_by_driver);
          }} else if (frame.event === "sos_triggered") {{
            document.getElementById("new-sos-banner").style.display = "block";
            // Reload sessions to pick up the new one
            loadAll();
          }}
        }} catch(_) {{}}
      }};
    }}

    /* ── Polling: reconcile active sessions list every 30 s ── */
    async function pollSessions() {{
      const data = await apiFetch("/sos/sessions/active").catch(() => null);
      if (!data) return;
      const activeIds = new Set((data.sessions || []).map(s => s.id));
      for (const id of Object.keys(sessions)) {{
        if (!activeIds.has(id)) removeSession(id);
      }}
      for (const s of data.sessions || []) {{
        upsertSession(s);
      }}
    }}

    loadAll();
    connectWS();
    setInterval(pollSessions, 30000);
  </script>
</body>
</html>"""


@router.get("/sos/live", response_class=HTMLResponse)
def sos_live_monitor():
    """Admin SOS live monitor page."""
    return HTMLResponse(content=_SOS_MAP_HTML.format(common_js=_common_js()))
