"use strict";

function createMaintenanceView({ api, basePath, element, failureText, sessionExpired }) {
  const refreshInterval = 15000;
  const healthDeadlineMs = 10000;
  const hour = 60 * 60 * 1000;
  const healthPath = basePath + "/maintenance/health";
  const logsPath = basePath + "/maintenance/logs";
  const maintenanceGrants = "console_read and maintenance_read";
  const numberFormat = new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 });
  const missing = "—";
  const els = {
    healthStatus: document.getElementById("maintenanceHealthStatus"),
    sources: document.getElementById("maintenanceSources"),
    trafficPanel: document.getElementById("maintenanceTrafficPanel"),
    traffic: document.getElementById("maintenanceTraffic"),
    roomSelect: document.getElementById("maintenanceRoomSelect"),
    roomStatus: document.getElementById("maintenanceRoomStatus"),
    roomMetrics: document.getElementById("maintenanceRoomMetrics"),
    eventsForm: document.getElementById("maintenanceEventsForm"),
    since: document.getElementById("maintenanceSince"),
    until: document.getElementById("maintenanceUntil"),
    component: document.getElementById("maintenanceComponent"),
    eventsSubmit: document.getElementById("maintenanceEventsSubmit"),
    eventsStatus: document.getElementById("maintenanceEventsStatus"),
    eventsTable: document.getElementById("maintenanceEventsTable"),
    eventsBody: document.getElementById("maintenanceEventsBody"),
  };
  const pending = { health: null, room: null, events: null };
  let active = false;
  let healthTimer = null;
  let healthDeadline = null;
  let health = null;
  let selectedRoom = "";

  function number(value, unit = "") {
    return Number.isFinite(value) ? numberFormat.format(value) + unit : missing;
  }

  function bytes(value) {
    if (!Number.isFinite(value)) return missing;
    const units = ["B", "KiB", "MiB", "GiB", "TiB"];
    let unit = 0;
    while (value >= 1024 && unit < units.length - 1) {
      value /= 1024;
      unit += 1;
    }
    return number(value, " " + units[unit]);
  }

  function elapsed(value) {
    if (value >= 86400) return number(value / 86400, " d");
    if (value >= 3600) return number(value / 3600, " h");
    if (value >= 60) return number(value / 60, " min");
    return number(value, " s");
  }

  function timeUTC(value) {
    const at = Date.parse(value);
    if (!Number.isFinite(at)) return missing;
    return new Date(at).toISOString()
      .replace(/\.000Z$/, "Z")
      .replace("T", " ")
      .replace("Z", " UTC");
  }

  function sum(rows, field) {
    if (!Array.isArray(rows) || rows.some((row) => !Number.isFinite(row[field]))) return null;
    return rows.reduce((total, row) => total + row[field], 0);
  }

  function meanLatency(seconds, count) {
    return Number.isFinite(seconds) && count > 0 ? number(seconds * 1000 / count, " ms") : missing;
  }

  function hostMetrics(values) {
    const metrics = [
      ["Process uptime", elapsed(values.process_uptime_seconds)],
      ["Process CPU time", elapsed(values.process_cpu_seconds_total)],
      ["Resident memory", bytes(values.process_resident_memory_bytes)],
      ["Open file descriptors", number(values.process_open_file_descriptors)],
      ["Database volume free", bytes(values.db_volume_free_bytes)],
      ["Database volume size", bytes(values.db_volume_total_bytes)],
    ];
    if (Number.isFinite(values.host_cpu_busy_fraction)) {
      metrics.push(["Host CPU busy", number(values.host_cpu_busy_fraction * 100, "%")]);
    }
    if (Number.isFinite(values.host_memory_total_bytes)) {
      metrics.push(["Host memory used / total", bytes(values.host_memory_used_bytes) + " / " + bytes(values.host_memory_total_bytes)]);
    }
    const loads = [values.load_1m, values.load_5m, values.load_15m];
    if (loads.some(Number.isFinite)) {
      metrics.push(["Host load (1m / 5m / 15m)", loads.map((value) => number(value)).join(" / ")]);
    }
    return metrics;
  }

  function gatewayMetrics(values) {
    const seconds = sum(values.http_request_duration_seconds_total, "seconds");
    const count = sum(values.http_request_duration_count, "count");
    return [
      ["HTTP requests since start", number(sum(values.http_requests_total, "count"))],
      ["Error responses since start", number(sum(values.http_error_responses_total, "count"))],
      ["Mean handler time", meanLatency(seconds, count)],
      ["Active HTTP requests", number(values.active_http_requests)],
      ["Active SSE streams", number(values.active_sse_streams)],
      ["SSE connections since start", number(values.sse_connections_total)],
    ];
  }

  function collaborationMetrics(values) {
    const denials = values.authorization_denials_total;
    return [
      ["Pending room deliveries", number(values.pending_inbox_events)],
      ["Oldest pending delivery", elapsed(values.oldest_pending_age_seconds)],
      ["Database size", bytes(values.db_size_bytes)],
      ["Schema version", number(values.schema_version)],
      ["SQLite busy errors since start", number(values.sqlite_busy_errors_total)],
      ["Transactions since start", number(values.transactions_total)],
      ["Mean transaction time", meanLatency(values.transaction_duration_seconds_total, values.transactions_total)],
      ["Stored team rooms", number(values.team_rooms_stored)],
      ["Stored members", number(values.members_stored)],
      ["Stored room messages", number(values.messages_stored)],
      ["Login failures since start", number(values.login_failures_total)],
      ["Login rate limits since start", number(values.rate_limited_total)],
      ["Authorization denials since start", number(denials ? sum(
        Object.values(denials).map((count) => ({ count })), "count"
      ) : null)],
    ];
  }

  function metricsList(metrics) {
    const list = element("dl", "metrics-list");
    for (const [label, value] of metrics) {
      const row = element("div", "metric");
      row.append(element("dt", "metric-label", label), element("dd", "metric-value", value));
      list.append(row);
    }
    return list;
  }

  function sourceReading(sample, generatedAt, elapsedMs) {
    const sampledAt = Date.parse(sample?.sampled_at);
    const age = Number.isFinite(sampledAt) ? Math.max(0, (generatedAt - sampledAt + elapsedMs) / 1000) : null;
    if (
      age === null ||
      (sample.state !== "fresh" && sample.state !== "stale") ||
      age > sample.unavailable_after_seconds
    ) {
      return { state: "unavailable", age, values: {} };
    }
    const state = age > sample.stale_after_seconds ? "stale" : sample.state;
    return { state, age, values: sample.values || {} };
  }

  function sourceCard(title, reading, metrics) {
    const card = element("article", "maintenance-panel health-source");
    const header = element("div", "source-heading");
    const status = reading.state[0].toUpperCase() + reading.state.slice(1);
    header.append(
      element("h3", "", title),
      element("span", "sample-state " + reading.state, status)
    );
    const sampleTime = reading.age === null
      ? "No sample time"
      : "Sampled " + Math.floor(reading.age) + " s ago";
    card.append(header, element("p", "sample-time", sampleTime), metricsList(metrics));
    return card;
  }

  function dataRow(values, tag) {
    const row = element("tr");
    for (const value of values) {
      const cell = element(tag, "", value);
      if (tag === "th") cell.scope = "col";
      row.append(cell);
    }
    return row;
  }

  function trafficTable(title, headings, rows) {
    const section = element("section", "traffic-table");
    section.append(element("h3", "", title));
    if (rows.length === 0) {
      section.append(element("p", "footnote", "No observations yet."));
      return section;
    }
    const wrap = element("div", "table-wrap");
    const table = element("table", "maintenance-table");
    table.setAttribute("aria-label", title);
    const thead = element("thead");
    const tbody = element("tbody");
    thead.append(dataRow(headings, "th"));
    tbody.append(...rows.map((row) => dataRow(row, "td")));
    table.append(thead, tbody);
    wrap.append(table);
    section.append(wrap);
    return section;
  }

  function renderTraffic(values) {
    const requests = values.http_requests_total;
    const durations = values.http_request_duration_seconds_total;
    const counts = values.http_request_duration_count;
    const errors = values.http_error_responses_total;
    if (![requests, durations, counts, errors].every(Array.isArray)) {
      els.traffic.replaceChildren();
      els.trafficPanel.hidden = true;
      return;
    }
    const secondsByRoute = new Map(durations.map(({ route, seconds }) => [route, seconds]));
    els.traffic.replaceChildren(
      trafficTable("Requests", ["Route", "Method", "Status", "Count"], requests.map(
        ({ route, method, status_class, count }) => [route, method, status_class, number(count)]
      )),
      trafficTable("Handler latency", ["Route", "Samples", "Mean"], counts.map(
        ({ route, count }) => [route, number(count), meanLatency(secondsByRoute.get(route), count)]
      )),
      trafficTable("Error responses", ["Code", "Count"], errors.map(
        ({ code, count }) => [code, number(count)]
      ))
    );
    els.trafficPanel.hidden = false;
  }

  function renderHealth() {
    if (!health) {
      els.sources.replaceChildren();
      renderTraffic({});
      return;
    }
    const elapsedMs = performance.now() - health.receivedAt;
    const { sources, generatedAt } = health;
    const host = sourceReading(sources.host, generatedAt, elapsedMs);
    const gateway = sourceReading(sources.gateway, generatedAt, elapsedMs);
    const collaboration = sourceReading(sources.collaboration, generatedAt, elapsedMs);
    els.sources.replaceChildren(
      sourceCard("Server host", host, hostMetrics(host.values)),
      sourceCard("Gateway", gateway, gatewayMetrics(gateway.values)),
      sourceCard("Collaboration", collaboration, collaborationMetrics(collaboration.values))
    );
    renderTraffic(gateway.values);
  }

  function notice(node, text, error = false) {
    if (node.textContent !== text) node.textContent = text;
    node.classList.toggle("error", error);
  }

  function finishRequest(kind) {
    pending[kind] = null;
    if (kind === "health") {
      clearTimeout(healthDeadline);
      healthDeadline = null;
    }
  }

  function cancel(kind) {
    const controller = pending[kind];
    finishRequest(kind);
    controller?.abort();
  }

  function maintenancePermissionDenied() {
    cancel("health");
    cancel("room");
    health = null;
    renderHealth();
    els.roomMetrics.replaceChildren();
    notice(els.healthStatus, "System health requires " + maintenanceGrants + ".", true);
    if (selectedRoom) notice(els.roomStatus, "Room load requires " + maintenanceGrants + ".", true);
  }

  function checkResponse(result, node, action) {
    if (result.status === 401) {
      sessionExpired();
    } else if (result.status !== 200) {
      notice(node, failureText(result, action), true);
    } else {
      return true;
    }
    return false;
  }

  async function refreshHealth() {
    if (!active || document.hidden) return;
    renderHealth();
    if (pending.health) return;
    if (!els.healthStatus.textContent) notice(els.healthStatus, "Loading system health…");
    const controller = new AbortController();
    pending.health = controller;
    healthDeadline = setTimeout(() => {
      if (pending.health !== controller) return;
      cancel("health");
      notice(els.healthStatus, "System health request timed out.", true);
    }, healthDeadlineMs);
    const result = await api(healthPath, { controller });
    if (pending.health !== controller) return;
    finishRequest("health");
    if (result.status === 403) {
      maintenancePermissionDenied();
      return;
    }
    if (!checkResponse(result, els.healthStatus, "System health")) return;
    const generatedAt = Date.parse(result.payload?.generated_at);
    if (!Number.isFinite(generatedAt) || !result.payload?.sources) {
      notice(els.healthStatus, "System health data is unavailable.", true);
      return;
    }
    health = { generatedAt, sources: result.payload.sources, receivedAt: performance.now() };
    renderHealth();
    notice(els.healthStatus, "Refreshes every 15 seconds while visible.");
  }

  function visibilityChanged() {
    clearInterval(healthTimer);
    healthTimer = null;
    if (!active || document.hidden) {
      cancel("health");
      return;
    }
    healthTimer = setInterval(refreshHealth, refreshInterval);
    refreshHealth();
  }

  function roomOption(label, value) {
    const option = element("option", "", label);
    option.value = value;
    return option;
  }

  function clearRoom() {
    cancel("room");
    selectedRoom = "";
    els.roomMetrics.replaceChildren();
  }

  function setRooms(rooms, pane) {
    if (pane.loading) {
      els.roomSelect.disabled = true;
      if (!selectedRoom) notice(els.roomStatus, "Loading team rooms…");
      return;
    }
    const options = pane.error || pane.denied ? [] : rooms;
    els.roomSelect.replaceChildren(
      roomOption("Select a room", ""),
      ...options.map((room) => roomOption(room.name, room.room_id))
    );
    if (!options.some((room) => room.room_id === selectedRoom)) clearRoom();
    els.roomSelect.value = selectedRoom;
    els.roomSelect.disabled = options.length === 0;
    if (pane.denied) {
      notice(els.roomStatus, "Team room list requires console_read.", true);
    } else if (pane.error) {
      notice(els.roomStatus, pane.error, true);
    } else if (options.length === 0) {
      notice(els.roomStatus, "No team rooms are available.");
    } else if (!selectedRoom) {
      notice(els.roomStatus, "");
    }
  }

  async function loadRoom() {
    cancel("room");
    els.roomMetrics.replaceChildren();
    if (!selectedRoom) {
      notice(els.roomStatus, "");
      return;
    }
    const roomId = selectedRoom;
    const controller = new AbortController();
    pending.room = controller;
    notice(els.roomStatus, "Loading room load…");
    const result = await api(basePath + "/rooms/" + encodeURIComponent(roomId) + "/load", { controller });
    if (pending.room !== controller) return;
    finishRequest("room");
    if (result.status === 403) {
      maintenancePermissionDenied();
      return;
    }
    if (result.status === 404) {
      notice(els.roomStatus, "This team room is no longer available. Use Refresh to reload the list.", true);
      return;
    }
    if (!checkResponse(result, els.roomStatus, "Room load")) return;
    const room = result.payload?.room;
    if (room?.room_id !== roomId || (room.state !== "fresh" && room.state !== "stale")) {
      notice(els.roomStatus, "Room load is unavailable.", true);
      return;
    }
    const label = room.state === "fresh" ? "Fresh at read" : "Stale at read";
    notice(els.roomStatus, label + " · Sampled " + timeUTC(room.sampled_at));
    els.roomMetrics.replaceChildren(metricsList([
      ["Members", number(room.member_count)],
      ["Stored messages", number(room.messages_stored)],
      ["Pending deliveries", number(room.pending_inbox_events)],
      ["Oldest pending delivery", elapsed(room.oldest_pending_age_seconds)],
      ["Last room activity", timeUTC(room.last_activity_at)],
    ]));
  }

  function clearEvents() {
    els.eventsBody.replaceChildren();
    els.eventsTable.hidden = true;
  }

  function eventRow(entry) {
    const request = [entry.method, entry.route, entry.status_class, entry.error_code].filter(
      (value) => typeof value === "string" && value
    );
    if (Number.isFinite(entry.duration_ms)) request.push(number(entry.duration_ms, " ms"));
    return dataRow([
      timeUTC(entry.at),
      entry.component,
      entry.severity,
      entry.event_code,
      request.join(" · ") || missing,
    ], "td");
  }

  async function loadEvents(event) {
    event.preventDefault();
    if (!active || pending.events) return;
    clearEvents();
    const since = Date.parse(els.since.value + "Z");
    const until = Date.parse(els.until.value + "Z");
    if (!Number.isFinite(since) || !Number.isFinite(until) || until <= since || until - since > hour) {
      notice(els.eventsStatus, "Choose Since and Until times in order, at most one hour apart.", true);
      return;
    }
    const query = new URLSearchParams({
      since: new Date(since).toISOString(),
      until: new Date(until).toISOString(),
    });
    const component = els.component.value;
    if (component) query.set("component", component);
    const controller = new AbortController();
    pending.events = controller;
    els.eventsSubmit.disabled = true;
    notice(els.eventsStatus, "Loading redacted events…");
    const result = await api(logsPath + "?" + query, { controller });
    if (pending.events !== controller) return;
    finishRequest("events");
    els.eventsSubmit.disabled = false;
    if (result.status === 403) {
      notice(els.eventsStatus, "Redacted events require console_read and logs_read.", true);
      return;
    }
    if (!checkResponse(result, els.eventsStatus, "Redacted events")) return;
    const payload = result.payload;
    if (payload?.state !== "available" || !Array.isArray(payload.entries)) {
      notice(els.eventsStatus, "Redacted events are unavailable.", true);
      return;
    }
    const { entries, truncated } = payload;
    els.eventsBody.append(...entries.map(eventRow));
    els.eventsTable.hidden = entries.length === 0;
    const count = entries.length + (entries.length === 1 ? " event" : " events");
    const scope = component ? " · " + component : "";
    const windowLabel = timeUTC(query.get("since")) + " to " + timeUTC(query.get("until"));
    notice(els.eventsStatus, count + scope + " · " + windowLabel + (truncated ? " · Partial coverage" : ""));
  }

  function open() {
    active = true;
    const until = Date.now();
    els.since.value = new Date(until - hour / 4).toISOString().slice(0, 19);
    els.until.value = new Date(until).toISOString().slice(0, 19);
    visibilityChanged();
  }

  function close() {
    active = false;
    visibilityChanged();
    health = null;
    renderHealth();
    els.trafficPanel.open = false;
    notice(els.healthStatus, "");
    clearRoom();
    els.roomSelect.replaceChildren();
    els.roomSelect.disabled = true;
    notice(els.roomStatus, "");
    cancel("events");
    clearEvents();
    els.since.value = "";
    els.until.value = "";
    els.component.value = "";
    els.eventsSubmit.disabled = false;
    notice(els.eventsStatus, "");
  }

  function refresh() {
    refreshHealth();
    if (active && selectedRoom) loadRoom();
  }

  els.roomSelect.addEventListener("change", () => {
    if (!active || els.roomSelect.disabled) return;
    selectedRoom = els.roomSelect.value;
    return loadRoom();
  });
  els.eventsForm.addEventListener("submit", loadEvents);

  return { open, close, refresh, setRooms, visibilityChanged };
}
