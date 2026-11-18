#!/usr/bin/env python
"""PreToolUse hook: keep PHI, credentials and the swagger out of Claude's context.

Blocks reading .env, output/, bookmarks/ and reference/swagger*.json through
Read/Grep/Glob AND through the shell (cat, Get-Content, grep, python -c that
prints a DataFrame...). Permission deny rules don't cover the shell; this does.

Input: the hook JSON on stdin. Output: permissionDecision=deny with the reason,
or nothing (let it through). On any internal error it lets the call through: a
broken hook must not block work.

Enable it in ~/.claude/settings.json (see the package README).
"""

from __future__ import annotations

import json
import re
import sys

_ENV = r"(?<![\w.-])\.env(?![\w.-])"          # .env, not .env.example
_DATA = r"(?<![\w-])(output|output_pruned|bookmarks)[/\\]"
_SWAGGER = r"reference[/\\]swagger(_response_optimization)?\.json"

_READ_VERBS = (
    r"\b(cat|type|more|less|head|tail|bat|strings|nl|od|xxd|gc|Get-Content|Select-String|sls"
    r"|grep|egrep|rg|findstr|awk|sed|jq|Import-Csv|notepad|code|vim?|nano)\b"
)
# python -c dumping a DataFrame from output/: patient rows into the context.
_DUMP_DF = r"\b(head|tail|sample|iloc|loc\[|to_string|to_markdown|to_dict|to_json|values|itertuples|iterrows|print\s*\(\s*df\b)"

REASONS = {
    "env": ".env holds the NextGen credentials: it is never read or printed. "
           "If auth fails, check that the NEXTGEN_* variables exist without printing their values.",
    "data": "output/ and bookmarks/ contain PHI. Show counts and column names "
            "(len(df), df.columns, df.shape), never rows.",
    "swagger": "reference/*.json are 6.7 MB + 1.1 MB and burn the context. "
               "Use ngcat.py (or python -m nextgen.catalog) search|show|fields|chain|lifecycle|traps.",
}


def classify_path(p: str) -> str | None:
    p = (p or "").replace("\\", "/")
    if re.search(_ENV, p):
        return "env"
    if re.search(r"(^|/)(output|output_pruned|bookmarks)(/|$)", p):
        return "data"
    if re.search(_SWAGGER, p):
        return "swagger"
    return None


def classify_command(cmd: str) -> str | None:
    if ("nextgen.catalog" in cmd or "ngcat.py" in cmd or "build_index.py" in cmd) and not re.search(
            _READ_VERBS + r"[^|;&]*" + _SWAGGER, cmd, re.I):
        return None
    for segment in re.split(r"[|;&]+|\n", cmd):
        if re.search(_READ_VERBS, segment, re.I):
            if re.search(_ENV, segment):
                return "env"
            if re.search(_DATA, segment, re.I):
                return "data"
            if re.search(_SWAGGER, segment, re.I):
                return "swagger"
    if re.search(r"python[\d.]*(\.exe)?\s+(-\w+\s+)*-c\b", cmd):
        if re.search(_SWAGGER, cmd) and "open(" in cmd:
            return "swagger"
        if re.search(_ENV, cmd) and re.search(r"open\(|read_text|dotenv_values", cmd):
            return "env"
        if re.search(_DATA, cmd) and re.search(r"read_(parquet|csv|json)|open\(", cmd) and re.search(_DUMP_DF, cmd):
            return "data"
    return None


def decide(event: dict) -> str | None:
    tool = event.get("tool_name", "")
    ti = event.get("tool_input") or {}
    if tool in ("Bash", "PowerShell"):
        return classify_command(ti.get("command", ""))
    if tool == "Read":
        return classify_path(ti.get("file_path", ""))
    if tool in ("Grep", "Glob"):
        return classify_path(ti.get("path", "")) or classify_path(ti.get("glob", "")) or (
            classify_path(ti.get("pattern", "")) if tool == "Glob" else None
        )
    return None


def main() -> int:
    try:
        event = json.load(sys.stdin)
        kind = decide(event)
    except Exception:  # noqa: BLE001 - fail open
        return 0
    if kind:
        json.dump(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": f"[nextgen-api] {REASONS[kind]}",
                }
            },
            sys.stdout,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
