"""Run the real Console listeners and, when provisioned, their browser pages."""

import os
import re
from contextlib import contextmanager

import httpx
import pytest

from tests.scenarios._stack import (
    ROOT,
    Developer,
    clean_env,
    command,
    run_process,
    run_with_tty,
    wait_for,
)


def account_cli(server, *args, password=None):
    argv, env = command("server", server.bin_dir)
    argv += ["accounts", "--db", str(server.db), *args]
    if password is not None:
        status, output = run_with_tty(argv, [password, password], env)
        assert status == 0, output.replace(password, "[redacted]")[-1500:]
        return
    result = run_process(
        argv,
        capture_output=True,
        text=True,
        env=clean_env() | env,
        cwd=ROOT,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


@contextmanager
def client_console(ws, server, username, *, bindings=1):
    developer = Developer(
        ws,
        username,
        server,
        bindings=bindings,
        bin_dir=server.bin_dir,
        credentials=False,
        notify=False,
    )
    with console_listener(developer) as (console, url):
        yield developer, console, url


@contextmanager
def console_listener(developer):
    argv, env = command("client", developer.bin_dir)
    proc = developer.ws.spawn(
        f"console-{developer.name}",
        [*argv, "serve", "--config", str(developer.config_path), "--console"],
        env,
    )
    try:
        pattern = re.compile(
            r"^(http://127\.0\.0\.1:\d+)/#([A-Za-z0-9_-]+)$", re.MULTILINE
        )
        launch = wait_for(
            lambda: pattern.search(proc.text()), proc, what="Console launch URL"
        )
        origin, bearer = launch.groups()
        # Process logs are printed on failure. Keep the launch bearer out.
        proc.log.write_text(proc.text().replace(bearer, "[redacted]"))
        with httpx.Client(
            base_url=origin,
            headers={"Authorization": f"Bearer {bearer}"},
            timeout=10,
            trust_env=False,
        ) as console:
            yield console, launch.group()
    finally:
        proc.stop()


def console_login(console, username, password):
    response = console.post(
        "/api/login",
        headers={"Origin": str(console.base_url).rstrip("/")},
        json={"username": username, "password": password},
    )
    assert response.status_code == 200, (
        f"Console sign-in returned HTTP {response.status_code}"
    )
    assert response.json().get("state") == "ready"
    assert set(response.json()) == {"state"}


@contextmanager
def browser_page(*origins, redact=()):
    required = os.environ.get("TEAMUP_REQUIRE_BROWSER") == "1"
    if required:
        from playwright import sync_api
    else:
        sync_api = pytest.importorskip("playwright.sync_api")

    with sync_api.sync_playwright() as playwright:
        executable = os.environ.get("TEAMUP_CHROMIUM")
        try:
            browser = playwright.chromium.launch(
                executable_path=executable, headless=True, chromium_sandbox=False
            )
        except sync_api.Error as exc:
            if (
                not required
                and not executable
                and "Executable doesn't exist" in str(exc)
            ):
                pytest.skip("Playwright Chromium is not installed")
            raise
        try:
            context = browser.new_context(viewport={"width": 1400, "height": 900})
            allowed = tuple(origin.rstrip("/") + "/" for origin in origins)
            context.route(
                "**/*",
                lambda route: (
                    route.continue_()
                    if route.request.url.startswith(allowed)
                    else route.abort()
                ),
            )
            page = context.new_page()
            page.set_default_timeout(8000)
            sync_api.expect.set_options(timeout=8000)
            yield page, sync_api.expect
        except sync_api.Error as exc:
            detail = str(exc)
            for value in redact:
                detail = detail.replace(value, "[redacted]")
            raise AssertionError(detail) from None
        finally:
            browser.close()
