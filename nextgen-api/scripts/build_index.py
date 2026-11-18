"""Rebuild data/endpoints.json from the nextgen-platform repo.

Only whoever MAINTAINS the skill needs this (when the swagger changes or a new
endpoint is measured in nextgen/catalog.py). Everyone else uses the pre-built
index that ships with the skill.

    python build_index.py --repo C:/path/to/nextgen-platform

It reads reference/swagger.json through nextgen.catalog (never by hand) and keeps
ONLY metadata: path, method, filters, field names and TYPES, default window and
deleted-row class. The collection's examples are typed placeholders (<string>,
<dateTime>), so no real data ships with the skill.

The repo's catalog writes its explanations in Spanish; this script emits the
English equivalents so ngcat prints English only.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE.parent / "data" / "endpoints.json"
DESC_MAX = 600

# English versions of the hand-written notes in nextgen/catalog.py. When a new
# entry appears there, add its translation here (otherwise the Spanish text is
# used as-is and the script warns).
EXTRA_TRAPS_EN = {
    "GET appointments": (
        "The list HIDES appointments with isDeleted=true. No warning: fewer rows come "
        "back. To see them, request them explicitly with isDeleted eq true in the "
        "$filter (a second pass per range). It hurts in incremental loads: a deletion "
        "updates modifyTimestamp, so filtering by modification date loses exactly the "
        "deletions. GET /appointments/{appointmentId} does return them. Found on "
        "2026-08-20: 0 of 1024 rows for one day had isDeleted=true, and 2 ids deleted "
        "that day only showed up when requested by id."
    ),
}
TEXT_EN = {
    "0 / 104 activas, 15 borradas al pedirlas": "0 / 104 active, 15 deleted when requested",
    "trae las bajas por si solo; el estado va en isActive":
        "returns deactivated rows on its own; the state is in isActive",
    "persons/merged (dropPersonId = la persona que desaparece al fusionar)":
        "persons/merged (dropPersonId = the person that disappears in the merge)",
}


def en(text: str) -> str:
    return TEXT_EN.get(text, text)


def why_en(c, ep, kind: str) -> str:
    p = ep.path
    if kind == "hides":
        return f"HIDES deleted rows (measured {en(c.HIDES_DELETED[p])}) -> second pass isDeleted eq true"
    if kind == "shows":
        return f"returns deleted rows without asking: {en(c.SHOWS_DELETED[p])}"
    if kind == "via":
        return f"no isDeleted; deletions in /{en(c.DELETED_VIA[p])}"
    if kind == "none":
        return "no isDeleted: a deletion just disappears and no filter can bring it back"
    if kind == "probable":
        return "has isDeleted, NOT MEASURED: assume it hides deleted rows and probe it"
    if kind == "detail":
        return ("detail by id: returns the record even if deleted, with isDeleted=true "
                "(measured on /appointments/{id}; others by analogy)")
    return "not a GET" if ep.method != "GET" else "does not expose isDeleted"


def extra_trap_en(c, ep, kind: str) -> str | None:
    key = f"{ep.method} {ep.path}"
    if key in c._EXTRA_TRAPS:
        if key not in EXTRA_TRAPS_EN:
            print(f"  WARNING: no English text for _EXTRA_TRAPS['{key}'], using the original")
        return EXTRA_TRAPS_EN.get(key, c._EXTRA_TRAPS[key])
    if kind == "hides":
        return (f"The list HIDES rows with isDeleted=true (measured: {en(c.HIDES_DELETED[ep.path])}). "
                "To see them, run a second pass with isDeleted eq true: extract.with_deleted() "
                "or fan_out(..., include_deleted=True).")
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", required=True, help="root of the nextgen-platform repo")
    ap.add_argument("--out", default=str(OUT))
    a = ap.parse_args(argv)

    repo = Path(a.repo).resolve()
    if not (repo / "nextgen" / "catalog.py").exists():
        print(f"nextgen/catalog.py not found under {repo}")
        return 1
    sys.path.insert(0, str(repo))
    from nextgen import catalog as c  # noqa: E402

    eps = c.load()
    rows = []
    for ep in eps:
        kind, _ = c.deleted_behavior(ep)
        rows.append({
            "folder": ep.folder,
            "name": ep.name,
            "method": ep.method,
            "path": ep.path,
            "description": ep.description[:DESC_MAX],
            "query": ep.query,
            "path_params": ep.path_params,
            "fields": ep.fields,
            "envelope": ep.envelope,
            "default_window": ep.default_window,
            "deleted": {"kind": kind, "why": why_en(c, ep, kind)},
            "extra_trap": extra_trap_en(c, ep, kind),
        })

    for v in [*c.HIDES_DELETED.values(), *c.SHOWS_DELETED.values(), *c.DELETED_VIA.values()]:
        if any(w in v for w in ("activas", "borradas", "bajas", "persona")) and v not in TEXT_EN:
            print(f"  WARNING: no English text for '{v}' (add it to TEXT_EN)")

    payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "nextgen-platform/reference/swagger.json via nextgen.catalog",
        "stats": c.stats(eps),
        "lifecycle": {
            "hides_deleted": {k: en(v) for k, v in c.HIDES_DELETED.items()},
            "shows_deleted": {k: en(v) for k, v in c.SHOWS_DELETED.items()},
            "no_deleted_flag": list(c.NO_DELETED_FLAG),
            "deleted_via": {k: en(v) for k, v in c.DELETED_VIA.items()},
        },
        "endpoints": rows,
    }
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"{len(rows)} requests -> {out}  ({out.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
