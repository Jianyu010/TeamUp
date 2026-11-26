"""Regression tests for the Rooms and Maintenance console UI (#5 and #32).

The console ships as static files under
``server/src/teamup_server/console/`` (see docs/modules/control-console.md,
section "Console UI 消费方契约"). These tests lock the consumer contract
without a browser: the exact file set, same-origin-only resources, no
storage or cookie access, no HTML injection sinks, console-only fetch
targets, the stale-response guards, room-scoped DOM clearing on
sign-out, session expiry, room switch, and access loss, and the
transient next-Timeline / older-Chat page semantics of #12 F3: loaded
rows survive retryable failures, retry re-issues the failed page's
cursor, and only denial, room change, or sign-out clear private rows.
They also lock the #12 F6 jump semantics: rendering a valid room detail
re-renders the already loaded Timeline so the "Open in Chats" control
follows the detail's ``transcript_allowed`` verdict whichever response
arrives first, and the control requires a live ``message_id`` because
retention-cleared message activities have no jump target.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

CONSOLE_DIR = (
    Path(__file__).resolve().parents[4] / "server" / "src" / "teamup_server" / "console"
)
EXPECTED_FILES = {
    "index.html",
    "console.css",
    "console.js",
    "maintenance.js",
    "accounts.js",
}

STORAGE_APIS = ("localStorage", "sessionStorage", "indexedDB", "document.cookie")
INJECTION_SINKS = (
    "innerHTML",
    "outerHTML",
    "insertAdjacentHTML",
    "insertAdjacentElement",
    "document.write",
    "createContextualFragment",
    "DOMParser",
    "eval(",
    "Function(",
)


def read(name):
    path = CONSOLE_DIR / name
    if not path.is_file():
        pytest.fail(f"Missing console static file: {name}")
    return path.read_text(encoding="utf-8")


def function_source(js, name):
    start = re.search(rf"function {name}\(", js)
    assert start is not None, f"console.js must define function {name}"
    rest = js[start.start() + 1 :]
    end = re.search(r"\n(async )?function ", rest)
    assert end is not None, f"Cannot isolate function {name}"
    return js[start.start() : start.start() + 1 + end.start()]


@pytest.fixture(scope="module")
def html():
    return read("index.html")


@pytest.fixture(scope="module")
def css():
    return read("console.css")


@pytest.fixture(scope="module")
def js():
    return read("console.js")


@pytest.fixture(scope="module")
def scripts(js):
    return "\n".join((js, read("maintenance.js"), read("accounts.js")))


def test_console_static_file_set():
    assert CONSOLE_DIR.is_dir(), f"Missing console package directory: {CONSOLE_DIR}"
    actual = {entry.name for entry in CONSOLE_DIR.iterdir()}
    assert actual == EXPECTED_FILES


def test_css_keeps_hidden_regions_out_of_layout(css):
    assert re.search(r"\[hidden\]\s*\{\s*display:\s*none\s*!important\s*;\s*\}", css)


def test_html_document_basics(html):
    assert html.lstrip().lower().startswith("<!doctype html>")
    assert re.search(r"<html\b[^>]*\blang=", html)
    assert re.search(r'<meta\b[^>]*charset\s*=\s*["\']?utf-8', html, re.IGNORECASE)
    assert re.search(r'<meta\b[^>]*name\s*=\s*["\']viewport', html, re.IGNORECASE)
    assert "<title>" in html


def test_html_same_origin_resources_only(html):
    assert "https://" not in html and "http://" not in html
    links = re.findall(r"<link\b[^>]*>", html, re.IGNORECASE)
    assert [
        re.search(r'href=["\']([^"\']*)["\']', link).group(1) for link in links
    ] == ["/console/console.css"]
    scripts = re.findall(r"<script\b[^>]*>", html, re.IGNORECASE)
    assert [
        re.search(r'src=["\']([^"\']*)["\']', script).group(1) for script in scripts
    ] == [
        "/console/maintenance.js",
        "/console/accounts.js",
        "/console/console.js",
    ]
    assert all(re.search(r"\bdefer\b", script) for script in scripts)
    for tag in ("img", "iframe", "source", "video", "object", "embed"):
        assert f"<{tag}" not in html.lower()
    assert "<style" not in html.lower()


def test_html_no_inline_scripting_or_style(html):
    assert not re.search(r"\son[a-z]+\s*=", html)
    assert "javascript:" not in html.lower()


def test_html_required_structure(html):
    required_ids = {
        "loginView",
        "loginForm",
        "username",
        "password",
        "loginStatus",
        "mainView",
        "statusLine",
        "roomList",
        "roomEmpty",
        "roomDetail",
        "roomTitle",
        "roomMeta",
        "rosterBody",
        "tabTimeline",
        "tabChats",
        "panelTimeline",
        "timelineList",
        "timelineStatus",
        "timelineMore",
        "panelChats",
        "chatsDenied",
        "chatsStatus",
        "chatsList",
        "chatsMore",
        "refreshButton",
        "logoutButton",
        "roomsViewButton",
        "maintenanceViewButton",
        "maintenanceView",
        "maintenanceHealthStatus",
        "maintenanceSources",
        "maintenanceTraffic",
        "maintenanceRoomSelect",
        "maintenanceRoomStatus",
        "maintenanceRoomMetrics",
        "maintenanceEventsForm",
        "maintenanceSince",
        "maintenanceUntil",
        "maintenanceComponent",
        "maintenanceEventsStatus",
        "maintenanceEventsBody",
    }
    for element_id in required_ids:
        assert f'id="{element_id}"' in html, f"Missing id: {element_id}"
    assert 'type="password"' in html
    assert re.search(r'role=["\']tablist["\']', html)
    assert html.lower().count('role="tab"') >= 2


def test_js_no_persistence_or_injection(scripts):
    for api in STORAGE_APIS:
        assert api not in scripts, f"Console must not touch {api}"
    for sink in INJECTION_SINKS:
        assert sink not in scripts, f"Console must not use {sink}"
    assert "srcdoc" not in scripts


def test_js_ci_frame_has_the_only_external_origin(scripts):
    assert re.findall(r'https?://[^"\s]+', scripts) == [
        "https://jianyu010.github.io/TeamUp/"
    ]
    frame = function_source(scripts, "loadCiDashboard")
    assert '"sandbox", "allow-scripts allow-same-origin"' in frame
    assert '"referrerpolicy", "no-referrer"' in frame


def test_js_fetch_targets_and_methods(scripts):
    assert '"/v1/auth/login"' in scripts
    assert '"/v1/console"' in scripts or '"/v1/console/"' in scripts
    assert "/v1/members" not in scripts
    assert scripts.count('"POST"') == 2
    assert "https://fetch" not in scripts
    literal_paths = set(re.findall(r'["\'](/v1/[a-z0-9/_${}.-]*)["\']', scripts))
    assert literal_paths <= {"/v1/auth/login", "/v1/console"}


def test_js_bearer_only_token_flow(js):
    assert "Authorization" in js and "Bearer" in js
    assert not re.search(r"[?&](access_)?token=", js)
    assert '"purpose"' in js and '"console"' in js
    assert '"access_token"' in js and '"expires_at"' in js


def test_js_console_contract_fields(js):
    for field in (
        "transcript_allowed",
        "has_more",
        "created_at",
        '"before"',
        '"after"',
        "activity_id",
        "message_id",
        "board_state",
        "role_description",
        "member_count",
        "last_activity",
    ):
        assert field in js, f"Console JS must handle {field}"


def test_js_stale_response_guards(js):
    assert "AbortController" in js
    assert ".abort()" in js
    assert js.count("state.epoch") >= 3
    assert js.count("textContent") >= 8


def test_js_signout_and_expiry_clear_room_scoped_dom(js):
    """Sign-out and session expiry clear room DOM, not only memory state.

    Regression for the Chrome smoke finding: showLogin used to hide mainView
    and drop in-memory state while leaving transcript text, roster, timeline
    rows, and room cards in hidden DOM nodes.
    """
    for name in ("logout", "sessionExpired"):
        body = function_source(js, name)
        assert "showLogin(" in body, f"{name} must return to the login view"
    show_login = function_source(js, "showLogin")
    assert "clearRoomPanes()" in show_login
    clearing = function_source(js, "clearRoomPanes")
    for node in ("roomList", "rosterBody", "timelineList", "chatsList"):
        assert f"els.{node}.replaceChildren()" in clearing
    for node in (
        "roomTitle",
        "roomMeta",
        "roomId",
        "timelineStatus",
        "chatsDenied",
        "chatsStatus",
    ):
        assert f'els.{node}.textContent = ""' in clearing
    for node in ("timelineMore", "chatsMore"):
        assert f"els.{node}.hidden = true" in clearing


def test_js_room_switch_and_access_loss_clear_stale_dom(js):
    """Room switch, refresh, and detail access loss clear stale panes promptly."""
    for name in ("renderDetailShell", "renderDetailError"):
        body = function_source(js, name)
        assert "clearRoomPanes()" in body, f"{name} must clear stale panes"


def test_js_transient_page_failure_preserves_rows_and_retries_same_cursor(js):
    """#12 F3: retryable next-Timeline / older-Chat failures keep loaded rows.

    Incremental pagination failures must not clear or hide the already
    loaded rows; retry re-issues the failed page with its own cursor
    (Timeline ``after`` cursor, Chats ``before``) instead of resetting
    to the latest page. Denial (403) still clears rows, so only denial,
    room change, refresh, and sign-out remove private content. The
    behavioral twin of this lock runs the real script under a stub DOM:
    server/tests/test_console_ui_behavior.mjs (node, no dependencies).
    """
    assert "retryBefore: undefined" in function_source(js, "freshRoom")
    load_chats = function_source(js, "loadChats")
    assert "pane.retryBefore = before" in load_chats
    assert "pane.retryBefore = undefined" in load_chats
    chats_denied = load_chats.split("result.status === 403")[1]
    assert "pane.rows = []" in chats_denied.split("renderChats()")[0]
    chats_failure = load_chats.split("result.status !== 200")[1]
    assert "pane.rows = []" not in chats_failure.split("renderChats()")[0]
    retry = function_source(js, "retryChats")
    assert "rows = []" not in retry
    assert "loaded = false" not in retry
    assert "loadChats(state.epoch, room.chats.retryBefore)" in retry
    render_chats = function_source(js, "renderChats")
    chats_error = render_chats.split("if (pane.error)")[1].split("return;")[0]
    assert "for (const message of pane.rows)" in chats_error
    assert "errorRow(pane.error, retryChats)" in chats_error
    load_timeline = function_source(js, "loadTimeline")
    timeline_denied = load_timeline.split("result.status === 403")[1]
    assert "pane.rows = []" in timeline_denied.split("renderTimeline()")[0]
    timeline_failure = load_timeline.split("result.status !== 200")[1]
    assert "pane.rows = []" not in timeline_failure.split("renderTimeline()")[0]
    render_timeline = function_source(js, "renderTimeline")
    timeline_error = render_timeline.split("if (pane.error)")[1].split("return;")[0]
    assert "for (const activity of pane.rows)" in timeline_error
    assert "errorRow(" in timeline_error


def test_js_timeline_jump_follows_late_detail_grant_and_live_message_id(js):
    """#12 F6: the Timeline->Chats jump follows the room-detail verdict.

    Room detail is the sole authority for ``transcript_allowed`` and its
    response may arrive after the first timeline page, so rendering a
    valid detail must re-render the already loaded timeline rows from
    pane state (no extra timeline fetch): the control is granted when
    the late detail allows transcripts and removed when it does not,
    whichever response lands first. The jump itself also requires a live
    ``message_id``: retention-cleared message activities link to null
    and have no jump target per the Timeline contract.
    """
    load_detail = function_source(js, "loadRoomDetail")
    assert "room.transcriptAllowed = payload.transcript_allowed === true" in load_detail
    assert load_detail.index("room.transcriptAllowed =") < load_detail.index(
        "renderRoomDetail()"
    )
    render_detail = function_source(js, "renderRoomDetail")
    assert "renderTimeline()" in render_detail
    assert "api(" not in render_detail
    render_timeline = function_source(js, "renderTimeline")
    assert "api(" not in render_timeline
    timeline_row = function_source(js, "timelineRow")
    gate = timeline_row.split('"Open in Chats"')[0]
    assert 'activity.kind === "message"' in gate
    assert "activity.message_id" in gate
    assert "room.transcriptAllowed" in gate


def test_html_no_implementation_only_or_redundant_copy(html):
    """The page keeps user-facing status labels, not packaging commentary."""
    assert "This package ships" not in html
    for token in (
        'class="badge"',
        'class="subtitle"',
        'class="page-foot"',
        'class="harness-note"',
        "login-note muted",
    ):
        assert token not in html


def test_js_referenced_ids_exist(html, scripts):
    referenced = set(re.findall(r'getElementById\(["\']([^"\']+)["\']\)', scripts))
    defined = set(re.findall(r'id="([^"]+)"', html))
    assert referenced
    assert referenced <= defined, referenced - defined


def test_css_no_external_references(css):
    assert "url(" not in css.lower()
    assert "@import" not in css.lower()


def test_css_responsive_density(css):
    assert "@media" in css
    assert "focus-visible" in css


CONSOLE_BROWSER = r"""
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const vm = require("node:vm");
const [dir, pathname] = process.argv.slice(1);
const html = readFileSync(dir + "/index.html", "utf8");

class Element {
  children = [];
  ownText = "";
  value = "";
  attributes = {};
  listeners = {};
  classList = { toggle() {} };

  constructor(tag, hidden = false) {
    this.tagName = tag;
    this.hidden = hidden;
  }

  set textContent(value) { this.ownText = String(value); this.children = []; }
  get textContent() {
    return this.ownText + this.children.map((child) => child.textContent).join(" ");
  }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.textContent = ""; this.append(...nodes); }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  removeAttribute(name) { delete this.attributes[name]; }
  addEventListener(type, listener) {
    (this.listeners[type] ||= []).push(listener);
  }
  emit(type) {
    return Promise.all((this.listeners[type] || []).map((listener) =>
      listener({ preventDefault() {} })));
  }
  focus() {}
  reset() {}
  setCustomValidity() {}
  querySelectorAll() { return []; }
}

const nodes = Object.fromEntries(
  [...html.matchAll(/<(\w+)\b[^>]*\bid="([^"]+)"([^>]*)>/g)]
    .map(([, tag, id, attrs]) => [id, new Element(tag, /\bhidden\b/.test(attrs))])
);
const el = (id) => {
  assert.ok(nodes[id], "Missing console element " + id);
  return nodes[id];
};
const document = new Element("document");
document.hidden = false;
document.visibilityState = "visible";
document.getElementById = el;
document.createElement = (tag) => new Element(tag);

const initialTime = Date.parse("2026-11-25T02:00:00Z");
let now = initialTime;
let expiresIn = 3600;
let nextTimer = 0;
const timers = new Map();
function schedule(callback, delay, repeats) {
  timers.set(++nextTimer, { callback, delay, repeats, at: now + delay });
  return nextTimer;
}
function advance(ms) {
  const until = now + ms;
  while (true) {
    const next = [...timers.entries()]
      .filter(([, timer]) => timer.at <= until)
      .sort((left, right) => left[1].at - right[1].at)[0];
    if (!next) break;
    const [id, timer] = next;
    now = timer.at;
    if (timer.repeats) timer.at += timer.delay;
    else timers.delete(id);
    timer.callback();
  }
  now = until;
}

const base = "/v1/console";
const healthPath = base + "/maintenance/health";
const roomsPath = base + "/rooms";
const logsPath = base + "/maintenance/logs";
const loginPath = "/v1/auth/login";
const sampledAt = new Date(now).toISOString();
const response = (status, payload) => ({ status, json: async () => payload });
const sample = (source, values) => ({
  source, sampled_at: sampledAt, state: "fresh", values, rates: null,
  sample_interval_seconds: 15, stale_after_seconds: 45, unavailable_after_seconds: 90,
});
const healthy = {
  generated_at: sampledAt,
  sources: {
    host: sample("host", { process_uptime_seconds: 214, host_cpu_busy_fraction: null }),
    gateway: sample("gateway", {
      http_requests_total: [{ route: "/v1/rooms", method: "GET", status_class: "2xx", count: 4 }],
      http_request_duration_seconds_total: [{ route: "/v1/rooms", seconds: 0.1 }],
      http_request_duration_count: [{ route: "/v1/rooms", count: 4 }],
      http_error_responses_total: [{ code: "forbidden", count: 1 }],
      active_http_requests: 1, active_sse_streams: 0, sse_connections_total: 0,
    }),
    collaboration: sample("collaboration", { pending_inbox_events: 12 }),
  },
};
const rooms = ["alpha", "beta"].map((room_id) => ({
  room_id, name: room_id, member_count: 1, last_activity: null,
}));
const roomLoad = (room_id, members) => response(200, { room: {
  room_id, sampled_at: sampledAt, state: "fresh", member_count: members,
  messages_stored: 0, pending_inbox_events: 0, oldest_pending_age_seconds: null,
  last_activity_at: null, load_rates: null,
} });
const events = response(200, { state: "available", truncated: true, entries: [{
  at: sampledAt, severity: "warning", component: "gateway", event_code: "request_error",
  method: "GET", route: "/v1/rooms", status_class: "4xx", error_code: "forbidden",
  duration_ms: 4, raw_body: "PRIVATE-FIELD",
}] });

const calls = [];
const handlers = {};
const callsFor = (path) => calls.filter(({ url }) => url.pathname === path);
async function fetch(path, options) {
  const url = new URL(path, "http://localhost");
  calls.push({ url, options });
  if (handlers[url.pathname]) return handlers[url.pathname](url, options);
  if (url.pathname === loginPath) {
    return response(200, { access_token: "operator-token", expires_at: now / 1000 + expiresIn });
  }
  if (url.pathname === base + "/capabilities") return response(200, {maintenance: true, accounts: false});
  if (url.pathname === roomsPath) return response(200, { rooms });
  if (url.pathname === healthPath) return response(200, healthy);
  throw new Error("Unexpected request " + path);
}

class Clock extends Date {
  static now() { return now; }
  constructor(...args) { super(...(args.length ? args : [now])); }
}
const context = vm.createContext({
  document, location: { pathname }, fetch, AbortController, URLSearchParams, Date: Clock,
  performance: { now: () => now - initialTime },
  setTimeout: (fn, ms) => schedule(fn, ms, false),
  setInterval: (fn, ms) => schedule(fn, ms, true),
  clearTimeout: (id) => timers.delete(id),
  clearInterval: (id) => timers.delete(id),
});
for (const [, src] of html.matchAll(/<script\b[^>]*\bsrc="([^"]+)"/g)) {
  const file = src.split("/").at(-1);
  vm.runInContext(readFileSync(dir + "/" + file, "utf8"), context, { filename: file });
}
const settle = () => new Promise((resolve) => setImmediate(resolve));
async function signIn() {
  el("username").value = "operator";
  el("password").value = "password";
  await el("loginForm").emit("submit");
  await settle();
}
function visibility(visible) {
  document.hidden = !visible;
  document.visibilityState = visible ? "visible" : "hidden";
  return document.emit("visibilitychange");
}
function queryEvents() {
  return el("maintenanceEventsForm").emit("submit");
}
"""


def run_console_browser(scenario, entry="/dashboard/"):
    node = shutil.which("node")
    if node is None:
        pytest.skip(
            "Node.js is required for the console's browser lifecycle regressions"
        )
    script = (
        CONSOLE_BROWSER
        + "\n(async () => {\n"
        + scenario
        + "\n})().catch((error) => { console.error(error); process.exitCode = 1; });"
    )
    result = subprocess.run(
        [node, "-e", script, str(CONSOLE_DIR), entry],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("entry", ["/dashboard/", "/console/"])
def test_entry_switches_console_views_without_reauthenticating(entry):
    run_console_browser(
        """
        assert.equal(calls.length, 0);
        await signIn();
        const dashboard = pathname === "/dashboard/";
        assert.equal(el("loginView").hidden, true);
        assert.equal(el("mainView").hidden, dashboard);
        assert.equal(el("maintenanceView").hidden, !dashboard);
        assert.equal(callsFor(healthPath).length, dashboard ? 1 : 0);
        assert.equal(JSON.parse(calls[0].options.body).purpose, "console");
        assert.equal(calls[0].options.headers.Authorization, undefined);
        await el(dashboard ? "roomsViewButton" : "maintenanceViewButton").emit("click");
        await settle();
        assert.equal(el("mainView").hidden, !dashboard);
        assert.equal(el("maintenanceView").hidden, dashboard);
        assert.equal(callsFor(loginPath).length, 1);
        assert.equal(callsFor(healthPath).length, 1);
        assert.ok(calls.slice(1).every(({ options }) =>
          options.headers.Authorization === "Bearer operator-token"));
        """,
        entry,
    )


def test_health_polling_pauses_when_hidden_or_switched_and_drops_late_results():
    run_console_browser(
        """
        await signIn();
        assert.equal(callsFor(healthPath).length, 1);
        advance(14999);
        assert.equal(callsFor(healthPath).length, 1);
        let finish;
        handlers[healthPath] = () => new Promise((resolve) => { finish = resolve; });
        advance(1);
        assert.equal(callsFor(healthPath).length, 2);
        await visibility(false);
        assert.ok(callsFor(healthPath)[1].options.signal.aborted);
        advance(30000);
        assert.equal(callsFor(healthPath).length, 2);
        finish(response(403, null));
        await settle();
        assert.doesNotMatch(el("maintenanceHealthStatus").textContent, /maintenance_read/);
        delete handlers[healthPath];
        await visibility(true);
        await settle();
        assert.equal(callsFor(healthPath).length, 3);
        await el("roomsViewButton").emit("click");
        advance(30000);
        assert.equal(callsFor(healthPath).length, 3);
        assert.equal(callsFor(logsPath).length, 0);
        """
    )


def test_maintenance_permissions_are_scoped_and_log_queries_are_explicit():
    run_console_browser(
        """
        handlers[healthPath] = () => response(403, { error: { code: "forbidden" } });
        handlers[roomsPath + "/alpha/load"] = handlers[healthPath];
        handlers[logsPath] = handlers[healthPath];
        await signIn();
        assert.match(el("maintenanceHealthStatus").textContent, /maintenance_read/);
        assert.equal(callsFor(logsPath).length, 0);
        assert.equal(el("loginView").hidden, true);
        el("maintenanceRoomSelect").value = "alpha";
        await el("maintenanceRoomSelect").emit("change");
        await settle();
        assert.match(el("maintenanceRoomStatus").textContent, /maintenance_read/);

        const since = el("maintenanceSince").value;
        el("maintenanceSince").value = "2026-11-24T23:00:00";
        await queryEvents();
        assert.match(el("maintenanceEventsStatus").textContent, /hour/i);
        assert.equal(callsFor(logsPath).length, 0);
        el("maintenanceSince").value = since;
        await queryEvents();
        await settle();
        assert.match(el("maintenanceEventsStatus").textContent, /logs_read/);
        assert.doesNotMatch(el("maintenanceEventsStatus").textContent, /maintenance_read/);

        handlers[logsPath] = () => events;
        el("maintenanceComponent").value = "gateway";
        await queryEvents();
        await settle();
        const query = callsFor(logsPath).at(-1).url.searchParams;
        assert.equal(query.get("since"), "2026-11-25T01:45:00.000Z");
        assert.equal(query.get("until"), "2026-11-25T02:00:00.000Z");
        assert.equal(query.get("component"), "gateway");
        assert.equal(query.has("limit"), false);
        assert.match(el("maintenanceEventsStatus").textContent, /partial|truncated/i);
        assert.match(el("maintenanceEventsStatus").textContent, /01:45:00 UTC to .*02:00:00 UTC/);
        const entries = el("maintenanceEventsBody").textContent;
        assert.match(entries, /request_error/);
        assert.doesNotMatch(entries, /PRIVATE-FIELD/);
        el("refreshButton").emit("click");
        await settle();
        assert.equal(callsFor(logsPath).length, 2);
        """
    )


def test_room_load_replaces_only_the_current_selection():
    run_console_browser(
        """
        let finishAlpha;
        handlers[roomsPath + "/alpha/load"] = () => new Promise((resolve) => {
          finishAlpha = resolve;
        });
        handlers[roomsPath + "/beta/load"] = () => roomLoad("beta", 28);
        await signIn();
        const select = el("maintenanceRoomSelect");
        assert.deepEqual(select.children.map((child) => child.value), ["", "alpha", "beta"]);
        select.value = "alpha";
        select.emit("change");
        select.value = "beta";
        await select.emit("change");
        await settle();
        assert.ok(callsFor(roomsPath + "/alpha/load")[0].options.signal.aborted);
        assert.match(el("maintenanceRoomMetrics").textContent, /28/);
        finishAlpha(roomLoad("alpha", 95));
        await settle();
        assert.doesNotMatch(el("maintenanceRoomMetrics").textContent, /95/);
        assert.equal(calls.filter(({ url }) => url.pathname.startsWith(roomsPath + "/")).length, 2);
        """
    )


@pytest.mark.parametrize("room_state", ["displayed", "pending"])
def test_maintenance_denial_discards_room_load_and_preserves_events(room_state):
    run_console_browser(
        "const roomState = "
        + repr(room_state)
        + ";\n"
        + """
        await signIn();
        handlers[logsPath] = () => events;
        await queryEvents();
        await settle();
        let finishRoom;
        const roomPath = roomsPath + "/alpha/load";
        handlers[roomPath] = roomState === "displayed"
          ? () => roomLoad("alpha", 28)
          : () => new Promise((resolve) => { finishRoom = resolve; });
        const select = el("maintenanceRoomSelect");
        select.value = "alpha";
        select.emit("change");
        await settle();
        if (roomState === "displayed") assert.match(el("maintenanceRoomMetrics").textContent, /28/);
        handlers[healthPath] = () => response(403, { error: { code: "forbidden" } });
        advance(15000);
        await settle();
        assert.equal(el("maintenanceRoomMetrics").textContent, "");
        assert.match(el("maintenanceRoomStatus").textContent, /maintenance_read/);
        assert.match(el("maintenanceEventsBody").textContent, /request_error/);
        assert.equal(el("loginView").hidden, true);
        if (roomState === "pending") {
          assert.ok(callsFor(roomPath)[0].options.signal.aborted);
          finishRoom(roomLoad("alpha", 28));
          await settle();
          assert.equal(el("maintenanceRoomMetrics").textContent, "");
        }
        """
    )


def test_stalled_health_request_is_aborted_and_next_poll_retries():
    run_console_browser(
        """
        let finish;
        handlers[healthPath] = () => new Promise((resolve) => { finish = resolve; });
        await signIn();
        assert.equal(callsFor(healthPath).length, 1);
        advance(10000);
        await settle();
        assert.ok(callsFor(healthPath)[0].options.signal.aborted);
        assert.match(el("maintenanceHealthStatus").textContent, /timed out/i);
        delete handlers[healthPath];
        advance(5000);
        await settle();
        assert.equal(callsFor(healthPath).length, 2);
        assert.match(el("maintenanceSources").textContent, /fresh/i);
        finish(response(403, null));
        await settle();
        assert.match(el("maintenanceSources").textContent, /fresh/i);
        assert.doesNotMatch(el("maintenanceHealthStatus").textContent, /maintenance_read/);
        """
    )


def test_room_load_denial_discards_health_data_for_the_shared_grant():
    run_console_browser(
        """
        await signIn();
        assert.match(el("maintenanceSources").textContent, /fresh/i);
        handlers[roomsPath + "/alpha/load"] = () => response(403, null);
        el("maintenanceRoomSelect").value = "alpha";
        await el("maintenanceRoomSelect").emit("change");
        assert.equal(el("maintenanceSources").textContent, "");
        assert.match(el("maintenanceHealthStatus").textContent, /maintenance_read/);
        """
    )


@pytest.mark.parametrize("end_session", ["expiry", "unauthorized", "sign out"])
def test_maintenance_session_end_clears_data_and_drops_the_pending_events(end_session):
    run_console_browser(
        "const reason = "
        + repr(end_session)
        + ";\n"
        + """
        expiresIn = 5;
        await signIn();
        assert.match(el("maintenanceSources").textContent, /Pending/i);
        handlers[logsPath] = () => events;
        await queryEvents();
        await settle();
        assert.match(el("maintenanceEventsBody").textContent, /request_error/);

        let finish;
        handlers[logsPath] = () => new Promise((resolve) => { finish = resolve; });
        queryEvents();
        if (reason === "expiry") advance(5000);
        if (reason === "unauthorized") {
          handlers[healthPath] = () => response(401, null);
          el("refreshButton").emit("click");
        }
        if (reason === "sign out") await el("logoutButton").emit("click");
        await settle();
        assert.equal(el("loginView").hidden, false);
        assert.equal(el("maintenanceView").hidden, true);
        assert.equal(el("maintenanceSources").textContent, "");
        assert.equal(el("maintenanceEventsBody").textContent, "");
        assert.ok(callsFor(logsPath).at(-1).options.signal.aborted);
        finish(events);
        await settle();
        assert.equal(el("maintenanceEventsBody").textContent, "");
        const healthCalls = callsFor(healthPath).length;
        advance(20000);
        assert.equal(callsFor(healthPath).length, healthCalls);
        """
    )


def test_maintenance_labels_cached_sample_ages_and_gateway_mean():
    run_console_browser(
        """
        await signIn();
        assert.match(el("maintenanceSources").textContent, /fresh/i);
        assert.match(el("maintenanceSources").textContent, /sampled.*0 s/i);
        assert.match(el("maintenanceTraffic").textContent, /25(?:\\.0+)? ms/);
        assert.match(el("maintenanceTraffic").textContent, /forbidden/);
        handlers[healthPath] = () => response(503, null);
        advance(60000);
        await settle();
        assert.match(el("maintenanceSources").textContent, /stale/i);
        assert.match(el("maintenanceSources").textContent, /sampled.*60 s/i);
        advance(45000);
        await settle();
        assert.match(el("maintenanceSources").textContent, /unavailable/i);
        assert.doesNotMatch(el("maintenanceTraffic").textContent, /25(?:\\.0+)? ms/);
        """
    )


def test_sign_in_without_operator_access_keeps_every_admin_surface_hidden():
    run_console_browser(
        """
        handlers[base + "/capabilities"] = () => response(403, {});
        await signIn();
        assert.equal(el("loginView").hidden, false);
        assert.equal(el("pageTitle").textContent, "TeamUp");
        assert.equal(el("consoleViews").hidden, true);
        assert.equal(el("accountsView").hidden, true);
        assert.equal(callsFor(roomsPath).length, 0);
        assert.equal(callsFor(healthPath).length, 0);
        """
    )


def test_accounts_and_ci_are_opened_inside_the_authorized_console():
    run_console_browser(
        """
        handlers[base + "/capabilities"] = () => response(200, {maintenance: false, accounts: true});
        handlers[base + "/accounts"] = () => response(200, {accounts: [
          {username: "operator", disabled: false, can_reset_password: true, can_disable: false},
        ]});
        await signIn();
        assert.equal(el("maintenanceViewButton").hidden, true);
        assert.equal(el("accountsViewButton").hidden, false);
        await el("accountsViewButton").emit("click");
        await settle();
        assert.equal(el("accountsView").hidden, false);
        assert.match(el("accountsRows").textContent, /operator Active Reset password/);
        assert.equal(callsFor(base + "/accounts").length, 1);
        await el("ciViewButton").emit("click");
        assert.equal(el("accountsRows").children.length, 0);
        assert.equal(el("ciView").children[0].src, "https://jianyu010.github.io/TeamUp/");
        await el("logoutButton").emit("click");
        assert.equal(el("ciView").children.length, 0);
        assert.equal(callsFor(loginPath).length, 1);
        """
    )
