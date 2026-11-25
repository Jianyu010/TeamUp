"""The Server's HTTP surface: routes, and request/response schemas from OpenAPI."""

import re
from pathlib import Path

from teamup_server.main import create_app

ROOT = Path(__file__).resolve().parents[2]

DOCS = (
    "docs/modules/client-server-v1.md",
    "docs/modules/account-login-v1.md",
    "docs/modules/room-tasks.md",
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
    pattern = r"`(GET|POST|PUT|PATCH|DELETE) (/v1/[^`?]*)"
    for doc in DOCS:
        text = (ROOT / doc).read_text(encoding="utf-8")
        for method, path in re.findall(pattern, text):
            found.add(f"{method} {path}")
    return found


def test_every_route_is_documented_and_every_documented_route_exists(tmp_path):
    served = set(routes(openapi(tmp_path)))
    documented = documented_routes()
    assert served == documented, (
        f"Served but undocumented: {sorted(served - documented)}; "
        f"documented but not served: {sorted(documented - served)}. "
        "Update the applicable module contract before changing routes."
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
