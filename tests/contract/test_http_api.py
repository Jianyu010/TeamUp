"""The Server's HTTP surface: routes, and request/response schemas from OpenAPI."""

import asyncio
import re
from importlib.metadata import version
from pathlib import Path

import httpx
from teamup_server.main import create_app

ROOT = Path(__file__).resolve().parents[2]

DOCS = (
    "docs/modules/client-server-v1.md",
    "docs/modules/account-login-v1.md",
    "docs/modules/member-lifecycle.md",
    "docs/modules/room-tasks.md",
    "docs/modules/room-archive.md",
    "docs/modules/direct-messages.md",
    "docs/modules/control-console.md",
    "docs/modules/maintenance-console.md",
    "docs/modules/admin-console.md",
    "docs/modules/focused-rooms.md",
    "docs/modules/room-sync.md",
)
METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}
# FastAPI's generic 422 body model is framework output, not a TeamUp contract.
FRAMEWORK_SCHEMAS = {"HTTPValidationError", "ValidationError"}


def openapi(tmp_path):
    return create_app(tmp_path / "contract.sqlite3").openapi()


def routes(spec):
    return sorted(
        f"{method.upper()} {path}"
        for path, operations in spec["paths"].items()
        for method in operations
        if method.upper() in METHODS
    )


def documented_routes():
    found = set()
    pattern = r"`(GET|POST|PUT|PATCH|DELETE) (/[^`?]*)"
    for doc in DOCS:
        text = (ROOT / doc).read_text(encoding="utf-8")
        for method, path in re.findall(pattern, text):
            found.add(f"{method} {path}")
    return found


def test_server_reports_the_installed_build_without_authentication(tmp_path):
    async def scenario():
        installed = version("teamup-server")
        app = create_app(tmp_path / "version.sqlite3")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://teamup.test"
        ) as client:
            response = await client.get("/v1/version")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.json() == {
            "version": None if installed == "0.0.0+unknown" else installed,
        }

    asyncio.run(scenario())


def test_every_route_is_documented_and_every_documented_route_exists(tmp_path):
    served = set(routes(openapi(tmp_path)))
    documented = documented_routes()
    assert served == documented, (
        f"Served but undocumented: {sorted(served - documented)}; "
        f"documented but not served: {sorted(documented - served)}. "
        "Update the contract documents under docs/modules/ first."
    )


def test_routes_match_golden(tmp_path, golden):
    golden("http_routes", routes(openapi(tmp_path)))


def test_request_and_response_schemas_match_golden(tmp_path, golden):
    spec = openapi(tmp_path)
    for operations in spec["paths"].values():
        for operation in operations.values():
            operation["responses"].pop("422", None)
            operation.pop("operationId", None)
    schemas = {
        name: schema
        for name, schema in spec["components"]["schemas"].items()
        if name not in FRAMEWORK_SCHEMAS
    }
    golden("http_openapi", {"paths": spec["paths"], "schemas": schemas})
