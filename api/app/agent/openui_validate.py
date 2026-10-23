"""Cheap validation for an LLM-authored OpenUI Lang program.

This is not a full parser. It catches the failures a small local model
actually makes — markdown fences, prose before the program, unknown
components, wrong root, unbalanced brackets — and lets the deterministic
fallback take over before an invalid program is streamed to the user.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

PROMPT_DIR = Path(__file__).parent / "prompts"
SPEC_PATH = PROMPT_DIR / "openui.spec.json"

_CALL_RE = re.compile(r"\b([A-Z][A-Za-z0-9_]*)\s*\(")
_ROOT_RE = re.compile(r"^\s*root\s*=\s*([A-Z][A-Za-z0-9_]*)\s*\(", re.MULTILINE)
_FENCE_RE = re.compile(r"```(?:openui-lang|openui)?\s*(.*?)```", re.DOTALL)


@lru_cache(maxsize=1)
def component_names() -> frozenset[str]:
    try:
        spec = json.loads(SPEC_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return frozenset()
    names = spec.get("components") or {}
    if isinstance(names, dict):
        return frozenset(names)
    return frozenset()


def clean_program(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        return ""
    fenced = _FENCE_RE.search(text)
    if fenced:
        text = fenced.group(1).strip()
    lines = [line.rstrip() for line in text.splitlines()]
    start = 0
    for i, line in enumerate(lines):
        if re.match(r"^\s*root\s*=", line):
            start = i
            break
    else:
        return ""
    return "\n".join(lines[start:]).strip()


def _balanced(text: str) -> bool:
    pairs = {"(": ")", "[": "]", "{": "}"}
    closing = {v: k for k, v in pairs.items()}
    stack: list[str] = []
    in_string = False
    escaped = False
    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in pairs:
            stack.append(ch)
        elif ch in closing and (not stack or stack.pop() != closing[ch]):
            return False
    return not stack and not in_string


def validate_program(raw: str, *, max_chars: int = 200_000) -> tuple[str | None, str | None]:
    """Return (clean_program, error). Exactly one is non-None."""
    program = clean_program(raw)
    if not program:
        return None, "no root statement"
    if len(program) > max_chars:
        return None, "program too large"
    if not program.startswith("root"):
        return None, "program must start with root"
    root = _ROOT_RE.search(program)
    if root is None:
        return None, "no root call"
    if root.group(1) != "Card":
        return None, f"root must be Card, got {root.group(1)}"
    if not _balanced(program):
        return None, "unbalanced brackets or quotes"
    known = component_names()
    if known:
        for name in _CALL_RE.findall(program):
            if name not in known:
                return None, f"unknown component {name}"
    return program, None
