"""Execution records, with explicit coverage of required checks."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def coverage(
    bindings: dict[str, list[str]], reports: list[dict[str, str]]
) -> dict[str, list[str]]:
    if not bindings:
        raise ValueError("no required checks were declared")
    result = {}
    for obligation, selectors in bindings.items():
        matched: list[str] = []
        if not selectors:
            raise ValueError(f"{obligation}: no check selectors")
        for selector in selectors:
            cases = [
                r
                for r in reports
                if r["id"] == selector
                or r["id"].startswith(selector + "[")
                or r["id"].startswith(selector + "::")
            ]
            if not cases or any(r["outcome"] != "passed" for r in cases):
                raise ValueError(
                    f"{obligation}: {selector} did not execute successfully in every case"
                )
            matched.extend(r["id"] for r in cases)
        result[obligation] = sorted(set(matched))
    return result


def safe_selectors(bindings: dict[str, list[str]]) -> list[str]:
    selectors = sorted({s for values in bindings.values() for s in values})
    for selector in selectors:
        path = Path(selector.split("::", 1)[0])
        if path.is_absolute() or ".." in path.parts or selector.startswith("-"):
            raise ValueError(
                f"check selector must stay inside the checkout: {selector}"
            )
    if not selectors:
        raise ValueError("no checks selected")
    return selectors
