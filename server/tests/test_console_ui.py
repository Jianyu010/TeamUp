"""Static regression tests for the Console UI package (#5).

The console ships as three static files under
``server/src/teamup_server/console/`` (see docs/modules/control-console.md,
section "Console UI 消费方契约"). These tests lock the consumer contract
without a browser: the exact file set, same-origin-only resources, no
storage or cookie access, no HTML injection sinks, console-only fetch
targets, the stale-response guards, and room-scoped DOM clearing on
sign-out, session expiry, room switch, and access loss.
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
