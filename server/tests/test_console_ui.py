"""Static regression tests for the Console UI package (#5).

The console ships as three static files under
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
from pathlib import Path

import pytest

CONSOLE_DIR = Path(__file__).parents[1] / "src" / "teamup_server" / "console"
EXPECTED_FILES = {"index.html", "console.css", "console.js"}

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


def test_console_static_file_set():
    assert CONSOLE_DIR.is_dir(), f"Missing console package directory: {CONSOLE_DIR}"
    actual = {entry.name for entry in CONSOLE_DIR.iterdir()}
    assert actual == EXPECTED_FILES


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
    ] == ["console.css"]
    scripts = re.findall(r"<script\b[^>]*>", html, re.IGNORECASE)
    assert len(scripts) == 1
    assert re.search(r'src=["\']console\.js["\']', scripts[0])
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
    }
    for element_id in required_ids:
        assert f'id="{element_id}"' in html, f"Missing id: {element_id}"
    assert 'type="password"' in html
    assert re.search(r'role=["\']tablist["\']', html)
    assert html.lower().count('role="tab"') >= 2


def test_js_no_persistence_or_injection(js):
    for api in STORAGE_APIS:
        assert api not in js, f"Console must not touch {api}"
    for sink in INJECTION_SINKS:
        assert sink not in js, f"Console must not use {sink}"
    assert "srcdoc" not in js


def test_js_no_external_origins(js):
    assert "https://" not in js and "http://" not in js


def test_js_fetch_targets_and_methods(js):
    assert '"/v1/auth/login"' in js
    assert '"/v1/console"' in js or '"/v1/console/"' in js
    assert "/v1/members" not in js
    assert js.count('"POST"') == 1
    assert "https://fetch" not in js
    literal_paths = set(re.findall(r'["\'](/v1/[a-z0-9/_${}.-]*)["\']', js))
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


def test_js_timeline_uses_prefixed_message_references(js):
    detail = function_source(js, "activityDetail")
    assert "if (activity.message_id)" in detail
    assert '"message msg-" + String(activity.message_id)' in detail


def test_js_referenced_ids_exist(html, js):
    referenced = set(re.findall(r'getElementById\(["\']([^"\']+)["\']\)', js))
    defined = set(re.findall(r'id="([^"]+)"', html))
    assert referenced
    assert referenced <= defined, referenced - defined


def test_css_no_external_references(css):
    assert "url(" not in css.lower()
    assert "@import" not in css.lower()


def test_css_responsive_density(css):
    assert "@media" in css
    assert "focus-visible" in css
