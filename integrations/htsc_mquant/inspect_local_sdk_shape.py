"""Inspect local MQuant Python files without claiming broker provenance.

This tool never imports or executes the supplied files. It only checks that
top-level read functions and mapped data fields have the names expected by the
shadow exporter, then computes a local drift-detection fingerprint. A passing
result does not prove that the files came from HTSC, are current, or are the
files loaded by the running MQuant process.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import sys
import tokenize
from pathlib import Path


REQUIRED_FUNCTION_PARAMETERS = {
    "get_fund_info": {"account_type"},
    "get_positions_ex": {"account_type", "symbol"},
    "get_open_orders_ex": {"page_no", "page_size", "only_this_inst", "account_type"},
    "get_orders_ex": {"page_no", "page_size", "only_this_inst", "account_type"},
    "get_trades_ex": {
        "page_no",
        "page_size",
        "only_this_inst",
        "account_type",
        "include_rejected_orders",
        "include_withdraw_orders",
    },
    "run_timely": {"func", "interval"},
}

REQUIRED_CLASS_FIELDS = {
    "FundUpdateInfo": {
        "available_cash",
        "frozen_cash",
        "hold_cash",
        "total_value",
        "market_value",
        "transferable_cash",
        "fund_account",
    },
    "Position": {
        "security",
        "total_amount",
        "closeable_amount",
        "today_amount",
        "locked_amount",
        "price",
        "value",
        "hold_cost",
    },
    "Order": {
        "order_id",
        "entrust_no",
        "security",
        "side",
        "status",
        "amount",
        "filled",
        "withdraw_amount",
        "entrust_price",
        "price",
        "add_time",
        "cancel_info",
    },
    "Trade": {
        "trade_id",
        "order_id",
        "entrust_no",
        "security",
        "side",
        "amount",
        "price",
        "business_balance",
        "real_type",
        "time",
    },
}


def _parse(path: Path) -> ast.Module:
    if not path.is_file():
        raise ValueError(f"file does not exist: {path}")
    if path.stat().st_size <= 0 or path.stat().st_size > 20 * 1024 * 1024:
        raise ValueError(f"file size is unsafe: {path}")
    with tokenize.open(path) as handle:
        return ast.parse(handle.read(), filename=str(path))


def _top_level_functions(tree: ast.Module) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            positional = [*node.args.posonlyargs, *node.args.args]
            result[node.name] = {argument.arg for argument in positional}
    return result


def _class_fields(tree: ast.Module) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        fields: set[str] = set()
        for child in ast.walk(node):
            if not isinstance(child, ast.Attribute):
                continue
            if isinstance(child.value, ast.Name) and child.value.id == "self":
                fields.add(child.attr)
        result[node.name] = fields
    return result


def _shape_id(api_path: Path, struct_path: Path) -> str:
    digest = hashlib.sha256()
    for label, path in ((b"MQuant_api.py", api_path), (b"MQuant_struct.py", struct_path)):
        digest.update(label)
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return "local-shape-sha256:" + digest.hexdigest()


def inspect_local_sdk_shape(api_path: Path, struct_path: Path) -> dict[str, object]:
    api_tree = _parse(api_path)
    struct_tree = _parse(struct_path)
    functions = _top_level_functions(api_tree)
    classes = _class_fields(struct_tree)
    missing: list[str] = []

    for function_name, required_parameters in REQUIRED_FUNCTION_PARAMETERS.items():
        actual = functions.get(function_name)
        if actual is None:
            missing.append(f"function:{function_name}")
            continue
        for parameter in sorted(required_parameters - actual):
            missing.append(f"parameter:{function_name}.{parameter}")

    for class_name, required_fields in REQUIRED_CLASS_FIELDS.items():
        actual = classes.get(class_name)
        if actual is None:
            missing.append(f"class:{class_name}")
            continue
        for field in sorted(required_fields - actual):
            missing.append(f"field:{class_name}.{field}")

    checked = not missing
    return {
        "shape_checked": checked,
        "local_shape_id": _shape_id(api_path, struct_path) if checked else None,
        "source_authenticated": False,
        "runtime_loaded_proven": False,
        "missing": missing,
        "note": (
            "local static shape and drift check only; this does not verify HTSC "
            "origin, current version, or the files loaded by MQuant"
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("api_path", type=Path)
    parser.add_argument("struct_path", type=Path)
    args = parser.parse_args(argv)
    try:
        result = inspect_local_sdk_shape(args.api_path, args.struct_path)
    except (OSError, UnicodeError, SyntaxError, ValueError) as exc:
        result = {
            "shape_checked": False,
            "local_shape_id": None,
            "source_authenticated": False,
            "runtime_loaded_proven": False,
            "missing": [],
            "error": str(exc),
        }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("shape_checked") is True else 2


if __name__ == "__main__":
    sys.exit(main())
