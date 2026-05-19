import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER_PATH = ROOT / "binary_ninja_headless_mcp" / "server.py"
BACKEND_PATH = ROOT / "binary_ninja_headless_mcp" / "backend.py"


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _server_class(module: ast.Module) -> ast.ClassDef:
    for node in module.body:
        if isinstance(node, ast.ClassDef) and node.name == "SimpleMcpServer":
            return node
    raise AssertionError("SimpleMcpServer class not found")


def _backend_class(module: ast.Module) -> ast.ClassDef:
    for node in module.body:
        if isinstance(node, ast.ClassDef) and node.name == "BinjaBackend":
            return node
    raise AssertionError("BinjaBackend class not found")


def _server_methods(server_cls: ast.ClassDef) -> dict[str, ast.FunctionDef]:
    return {node.name: node for node in server_cls.body if isinstance(node, ast.FunctionDef)}


def _tool_handlers(server_methods: dict[str, ast.FunctionDef]) -> dict[str, str]:
    init = server_methods["__init__"]
    handlers: dict[str, str] = {}
    for stmt in ast.walk(init):
        if not isinstance(stmt, ast.Assign):
            continue
        if len(stmt.targets) != 1:
            continue
        target = stmt.targets[0]
        if not (isinstance(target, ast.Attribute) and target.attr == "_tool_handlers"):
            continue
        if not isinstance(stmt.value, ast.Dict):
            continue
        for key_node, value_node in zip(stmt.value.keys, stmt.value.values, strict=True):
            if not (isinstance(key_node, ast.Constant) and isinstance(key_node.value, str)):
                continue
            if not isinstance(value_node, ast.Attribute):
                continue
            if not (isinstance(value_node.value, ast.Name) and value_node.value.id == "self"):
                continue
            handlers[key_node.value] = value_node.attr
    if not handlers:
        raise AssertionError("Failed to parse _tool_handlers registry")
    return handlers


def _tool_definitions(server_methods: dict[str, ast.FunctionDef]) -> set[str]:
    defs = set()
    for call in ast.walk(server_methods["_tool_definitions"]):
        if not isinstance(call, ast.Call):
            continue
        if not isinstance(call.func, ast.Attribute):
            continue
        if not (
            isinstance(call.func.value, ast.Name)
            and call.func.value.id == "self"
            and call.func.attr == "_tool"
        ):
            continue
        if not call.args:
            continue
        first_arg = call.args[0]
        if isinstance(first_arg, ast.Constant) and isinstance(first_arg.value, str):
            defs.add(first_arg.value)
    return defs


def _backend_calls_from_tool_methods(server_methods: dict[str, ast.FunctionDef]) -> set[str]:
    calls = set()
    for name, method in server_methods.items():
        if not name.startswith("_tool_"):
            continue
        for call in ast.walk(method):
            if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)):
                continue
            attr = call.func
            if not (isinstance(attr.value, ast.Attribute) and attr.value.attr == "_backend"):
                continue
            if not (isinstance(attr.value.value, ast.Name) and attr.value.value.id == "self"):
                continue
            calls.add(attr.attr)
    return calls


def _backend_public_methods(backend_cls: ast.ClassDef) -> set[str]:
    return {
        node.name
        for node in backend_cls.body
        if isinstance(node, ast.FunctionDef) and not node.name.startswith("_")
    }


def test_tool_registry_and_definitions_match() -> None:
    module = _parse(SERVER_PATH)
    methods = _server_methods(_server_class(module))
    handlers = set(_tool_handlers(methods))
    definitions = _tool_definitions(methods)
    assert handlers == definitions


def test_tool_handlers_map_to_backend_methods_without_dead_public_api() -> None:
    server_module = _parse(SERVER_PATH)
    backend_module = _parse(BACKEND_PATH)
    server_methods = _server_methods(_server_class(server_module))
    backend_methods = _backend_public_methods(_backend_class(backend_module))
    backend_calls = _backend_calls_from_tool_methods(server_methods)

    missing_backend_methods = sorted(backend_calls - backend_methods)
    assert not missing_backend_methods, "tools reference missing backend methods: " + ", ".join(
        missing_backend_methods
    )

    dead_backend_methods = sorted(backend_methods - backend_calls - {"shutdown"})
    assert not dead_backend_methods, "backend public methods not reachable by tools: " + ", ".join(
        dead_backend_methods
    )


def _ast_to_value(node: ast.AST) -> object:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.List):
        return [_ast_to_value(e) for e in node.elts]
    if isinstance(node, ast.Dict):
        out: dict[object, object] = {}
        for k, v in zip(node.keys, node.values, strict=True):
            if isinstance(k, ast.Constant):
                out[k.value] = _ast_to_value(v)
        return out
    return None


def _array_without_items(schema: object, path: str, issues: list[str]) -> None:
    if not isinstance(schema, dict):
        return
    type_field = schema.get("type")
    types = (
        [type_field]
        if isinstance(type_field, str)
        else [t for t in type_field if isinstance(t, str)]
        if isinstance(type_field, list)
        else []
    )
    if "array" in types and "items" not in schema:
        if not (schema.get("oneOf") or schema.get("anyOf")):
            issues.append(path)
    for combinator in ("oneOf", "anyOf", "allOf"):
        sub = schema.get(combinator)
        if isinstance(sub, list):
            for i, branch in enumerate(sub):
                _array_without_items(branch, f"{path}.{combinator}[{i}]", issues)
    props = schema.get("properties")
    if isinstance(props, dict):
        for name, sub in props.items():
            _array_without_items(sub, f"{path}.{name}", issues)
    items = schema.get("items")
    if isinstance(items, dict):
        _array_without_items(items, f"{path}.items", issues)


def test_array_schemas_declare_items() -> None:
    """Strict JSON Schema clients (Vercel AI SDK, etc.) reject array schemas without `items`."""
    methods = _server_methods(_server_class(_parse(SERVER_PATH)))
    issues: list[str] = []
    for call in ast.walk(methods["_tool_definitions"]):
        if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)):
            continue
        if call.func.attr != "_tool":
            continue
        if len(call.args) < 3 or not isinstance(call.args[2], ast.Dict):
            continue
        first = call.args[0]
        tool_name = first.value if isinstance(first, ast.Constant) else "<?>"
        for prop_key, prop_val in zip(call.args[2].keys, call.args[2].values, strict=True):
            if not (isinstance(prop_key, ast.Constant) and isinstance(prop_val, ast.Dict)):
                continue
            _array_without_items(_ast_to_value(prop_val), f"{tool_name}.{prop_key.value}", issues)
    assert not issues, "array schemas missing 'items': " + ", ".join(issues)
