import ast
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = {
    "teamup_server": ROOT / "server/src/teamup_server",
    "teamup_client": ROOT / "client/shared/src/teamup_client",
    "teamup_mcp": ROOT / "plugin/mcp/src/teamup_mcp",
}


def imports(path, package_root):
    package = ".".join(
        (package_root.name, *path.parent.relative_to(package_root).parts)
    )
    for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:
                module = importlib.util.resolve_name("." * node.level + module, package)
            yield module
            yield from (f"{module}.{alias.name}" for alias in node.names)


def test_production_imports_follow_published_package_boundaries():
    for package, directory in PACKAGES.items():
        files = list(directory.rglob("*.py"))
        assert files, package
        for path in files:
            for imported in imports(path, directory):
                target, _, _ = imported.partition(".")
                if target in PACKAGES and target != package:
                    assert package == "teamup_mcp" and (
                        imported == "teamup_client.ipc"
                        or imported.startswith("teamup_client.ipc.")
                    ), (path, imported)
                if target == "sqlite3" and package == "teamup_server":
                    assert (
                        path.stem in {"collaboration", "_room_changes"}
                        or "collaboration" in path.parts
                    ), path
                if imported == "teamup_server._room_changes" or imported.startswith(
                    "teamup_server._room_changes."
                ):
                    assert package == "teamup_server" and path.stem == "collaboration", (
                        path,
                        imported,
                    )
                if imported == "teamup_client._room_store" or imported.startswith(
                    "teamup_client._room_store."
                ):
                    assert package == "teamup_client" and path.stem in {
                        "_room_commands",
                        "_room_sync",
                        "main",
                    }, (path, imported)
                if path.stem != "main":
                    assert imported != f"{package}.main" and not imported.startswith(
                        f"{package}.main."
                    ), (path, imported)


def test_room_store_file_and_table_names_are_private():
    directory = PACKAGES["teamup_client"]
    owner = directory / "_room_store.py"
    private_names = (".rooms.sqlite3", "room_scopes", "room_records", "room_commands")
    for path in directory.rglob("*.py"):
        if path != owner:
            for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    assert all(name not in node.value for name in private_names), (
                        path,
                        node.lineno,
                    )
