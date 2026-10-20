"""Regenerate public capability metadata without importing tau or reading tasks.

Usage: python scripts/generate_tau_domain_catalog.py /path/to/tau2-bench-v1.0.1
The checkout must match the declared commit; root release tooling verifies it.
Only the three public source files below are read. No database, task, evaluator,
golden trajectory, or callable enters the resulting runtime catalog.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import pprint
from pathlib import Path
from typing import Any

PIN = "fc0055dc4e0a316c3f83133267fbd6faaa770992"
SOURCES = (
    "src/tau2/domains/retail/tools.py",
    "src/tau2/domains/banking_knowledge/tools.py",
    "src/tau2/domains/banking_knowledge/retrieval_mixins.py",
)
RETRIEVAL_CLASSES = {
    "KBSearchMixin",
    "GrepMixin",
    "KBSearchBm25AllToolsMixin",
    "KBSearchDenseAllToolsMixin",
}
SAFE_GENERIC = {
    "calculate": "read",
    "transfer_to_human_agents": "control",
    "give_discoverable_user_tool": "control",
    "unlock_discoverable_agent_tool": "control",
    "initial_transfer_to_human_agent_0218": "control",
    "initial_transfer_to_human_agent_1822": "control",
    "emergency_credit_bureau_incident_transfer_1114": "control",
}


def annotation(node: ast.expr, aliases: dict[str, ast.expr]) -> dict[str, Any]:
    if isinstance(node, ast.Name):
        if node.id in aliases:
            return annotation(aliases[node.id], aliases)
        if node.id == "Any":
            return {}
        return {
            "type": {"str": "string", "int": "integer", "float": "number", "bool": "boolean"}[
                node.id
            ]
        }
    if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):
        kind = node.value.id
        if kind in {"List", "list"}:
            return {"type": "array", "items": annotation(node.slice, aliases)}
        if kind == "Optional":
            return {"anyOf": [annotation(node.slice, aliases), {"type": "null"}]}
        if kind == "Literal":
            items = node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]
            values = [ast.literal_eval(item) for item in items]
            if not all(type(value) is str for value in values):
                raise ValueError("Unsupported literal type")
            return {"type": "string", "enum": values}
        if kind in {"Dict", "dict"} and isinstance(node.slice, ast.Tuple):
            if ast.unparse(node.slice.elts[0]) != "str":
                raise ValueError("Non-string mapping key")
            return {
                "type": "object",
                "additionalProperties": annotation(node.slice.elts[1], aliases),
            }
    raise ValueError(f"Unsupported annotation: {ast.unparse(node)}")


def generate(checkout: Path) -> str:
    domains: dict[str, dict[str, Any]] = {"retail": {}, "banking_knowledge": {}}
    provenance = {}
    for relative in SOURCES:
        source = (checkout / relative).read_bytes()
        provenance[relative] = hashlib.sha256(source).hexdigest()
        tree = ast.parse(source)
        aliases = {
            node.targets[0].id: node.value
            for node in tree.body
            if isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Subscript)
        }
        domain = "retail" if "/retail/" in relative else "banking_knowledge"
        for cls in (node for node in tree.body if isinstance(node, ast.ClassDef)):
            if (
                cls.name
                not in {"RetailTools", "KnowledgeTools", "KnowledgeUserTools"} | RETRIEVAL_CLASSES
            ):
                continue
            for fn in (node for node in cls.body if isinstance(node, ast.FunctionDef)):
                decorator = next(
                    (
                        d
                        for d in fn.decorator_list
                        if isinstance(d, ast.Call)
                        and isinstance(d.func, ast.Name)
                        and d.func.id in {"is_tool", "is_discoverable_tool"}
                    ),
                    None,
                )
                if decorator is None:
                    continue
                tool_type = ast.unparse(decorator.args[0]).split(".")[-1]
                effect = {"READ": "read", "WRITE": "write"}.get(tool_type)
                if effect is None:
                    effect = SAFE_GENERIC[fn.name]  # New generic effects require review.
                mutates = next(
                    (
                        ast.literal_eval(k.value)
                        for k in decorator.keywords
                        if k.arg == "mutates_state"
                    ),
                    tool_type == "WRITE",
                )
                positional = fn.args.posonlyargs + fn.args.args
                default_start = len(positional) - len(fn.args.defaults)
                properties = {}
                required = []
                for index, arg in enumerate(positional):
                    if arg.arg == "self":
                        continue
                    if arg.annotation is None:
                        raise ValueError("Missing type annotation")
                    properties[arg.arg] = annotation(arg.annotation, aliases)
                    if index < default_start:
                        required.append(arg.arg)
                if fn.args.vararg or fn.args.kwarg or fn.args.kwonlyargs:
                    raise ValueError("Variadic or keyword-only signature needs explicit review")
                domains[domain][fn.name] = {
                    "name": fn.name,
                    "parameters": {
                        "type": "object",
                        "properties": properties,
                        "required": required,
                        "additionalProperties": False,
                    },
                    "effect": effect,
                    "mutates_state": bool(mutates),
                    "discoverable": decorator.func.id == "is_discoverable_tool",
                    "actor": "user" if cls.name == "KnowledgeUserTools" else "agent",
                }
    data = {"upstream_commit": PIN, "public_source_sha256": provenance, "domains": domains}
    return (
        '"""Generated public tau v1.0.1 signatures; see scripts/generate_tau_domain_catalog.py."""\n\n'
        + "CATALOG = "
        + pprint.pformat(data, width=100, sort_dicts=True)
        + "\n"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkout", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    target = Path(__file__).resolve().parents[1] / "src/safe_agent/domains/_catalog_data.py"
    result = generate(args.checkout)
    if args.check:
        if target.read_text() != result:
            raise SystemExit("Catalog does not match pinned public sources")
    else:
        target.write_text(result)


if __name__ == "__main__":
    main()
