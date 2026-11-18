"""ngcat - portable explorer for the NextGen Enterprise API (routes, filters,
fields, traps, deleted rows and DEPENDENCIES between routes).

Offline, no credentials, no dependencies (standard library only). Reads
data/endpoints.json, built from the nextgen-platform repo's swagger with
build_index.py.

    python ngcat.py stats                       catalog totals
    python ngcat.py folders                     folders (Chart, Master, Persons...)
    python ngcat.py search <term> [GET]         search by path / name / description
    python ngcat.py show <path>                 card: filters, fields, traps, deleted rows, dependencies
    python ngcat.py fields <field>              which endpoints return that field
    python ngcat.py traps                       silent default windows + measured traps
    python ngcat.py lifecycle [fragment]        how each list endpoint treats deleted rows
    python ngcat.py params [name]               path parameters (:personId...) and who produces them
    python ngcat.py chain <path>                ordered call plan to reach a route
    python ngcat.py children <path>             subroutes hanging off a route
    python ngcat.py tree [prefix] [--depth N]   route tree

Paths are accepted in any form: persons/:personId/chart,
/persons/{personId}/chart, persons/{id}/chart ...
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter, defaultdict
from functools import lru_cache
from pathlib import Path

DATA = Path(os.getenv("NGCAT_DATA", Path(__file__).resolve().parent.parent / "data" / "endpoints.json"))
ODATA = ("$top", "$skip", "$filter", "$orderby", "$expand", "$inlinecount", "$count")
LIFECYCLE_RE = re.compile(
    r"^(isDeleted|isActive|isCancelled|isNoShow|isKept|isRescheduled|isExpired|status|statusId"
    r"|statusDescription|deleteReason|cancelReason(Id)?|inactiveReason|rescheduledAppointmentId"
    r"|rescheduleReasonId|inactiveDate|deleteDate|cancelledDate|expirationDate|expiredDate|resolvedDate)$"
)
DROP_RULES = [
    ("hateoas", re.compile(r"(^_links$|Links$|^vendorExtensions$)")),
    ("ts_variant", re.compile(r"(Utc|UtcOffset|Timezone)$")),
    ("audit", re.compile(r"^(created|modified)By(Id|LoginId)?$", re.I)),
    ("tenant", re.compile(r"^(enterpriseId|practiceId|practiceName)$")),
]
# Cross-person indexes: the right way to DISCOVER persons before a fan-out.
PERSON_INDEXES = ("persons", "persons/chart/charges", "persons/charts/encounters", "appointments")
AUTH_STEPS = [
    "POST nge-oauth/token (client_credentials + site_id)              -> access_token",
    "GET  /master/practices   (Bearer only, no session)                -> practiceId, enterpriseId",
    "PUT  /users/me/login-defaults {enterpriseId, practiceId}         -> x-ng-sessionid header",
    "(NextGenClient.from_env() does all 3 steps; from here on: Bearer + X-NG-SessionId)",
]
LIFECYCLE_TITLES = {
    "hides": "HIDE deleted rows (measured) -> second pass isDeleted eq true",
    "probable": "have isDeleted, NOT MEASURED -> assume they hide; probe with the repo's scripts/probe_lifecycle_flags.py",
    "via": "no isDeleted, dedicated endpoint for deletions",
    "none": "no isDeleted -> deletions can't be recovered with a filter",
    "shows": "return deleted rows without asking",
    "detail": "detail by id (returns the record even if deleted)",
}


# --------------------------------------------------------------------------
# Loading and normalization
# --------------------------------------------------------------------------
@lru_cache(maxsize=1)
def load() -> dict:
    if not DATA.exists():
        sys.exit(f"{DATA} not found. Rebuild it with build_index.py --repo <nextgen-platform>")
    return json.loads(DATA.read_text(encoding="utf-8"))


def eps() -> list[dict]:
    return load()["endpoints"]


@lru_cache(maxsize=1)
def gets() -> dict[str, dict]:
    return {e["path"]: e for e in eps() if e["method"] == "GET"}


def norm(path: str) -> str:
    """'/persons/{personId}/chart' -> 'persons/:personId/chart'."""
    p = path.strip().strip("/").split("?")[0]
    p = re.sub(r"^.*?/nge-api/api/", "", p)
    return re.sub(r"\{(\w+)\}", r":\1", p)


def shape(path: str) -> str:
    """Shape without parameter names, so persons/{id} matches persons/:personId."""
    return re.sub(r":\w+", ":*", norm(path)).lower()


def resolve(path: str, method: str = "GET") -> list[dict]:
    """Endpoints matching a path: exact > same shape > fragment."""
    n = norm(path).lower()
    pool = [e for e in eps() if not method or e["method"] == method] or eps()
    for test in (lambda e: e["path"].lower() == n,
                 lambda e: shape(e["path"]) == shape(n),
                 lambda e: n in e["path"].lower()):
        hits = [e for e in pool if test(e)]
        if hits:
            return sorted(hits, key=lambda e: (e["method"] != "GET", len(e["path"])))
    return []


def segs(path: str) -> list[str]:
    return path.split("/") if path else []


# --------------------------------------------------------------------------
# Dependency graph
# --------------------------------------------------------------------------
def id_field(ep: dict, param: str) -> str:
    """Which response field of `ep` feeds :param."""
    f = {k.lower(): k for k in ep["fields"]}
    if param.lower() in f:
        return f[param.lower()]
    if "id" in f:
        return f["id"]
    return "?"


@lru_cache(maxsize=1)
def list_producers() -> dict[str, Counter]:
    """param -> Counter(parent_path): GET routes whose path is the prefix right before :param."""
    out: dict[str, Counter] = defaultdict(Counter)
    g = gets()
    for e in eps():
        s = segs(e["path"])
        for i, seg in enumerate(s):
            if seg.startswith(":"):
                parent = "/".join(s[:i])
                if parent in g:
                    out[seg[1:]][parent] += 1
    return out


def field_producers(param: str) -> list[dict]:
    """GET lists that return a field named exactly like the parameter."""
    p = param.lower()
    hits = [e for e in gets().values()
            if p in (k.lower() for k in e["fields"]) and p not in (x.lower() for x in e["path_params"])]
    return sorted(hits, key=lambda e: (len(e["path_params"]), not (e["envelope"] == "paged" or "$top" in e["query"]),
                                       e["path"] not in PERSON_INDEXES, len(e["path"])))


def producers(param: str, available: list[str] | None = None) -> list[tuple[dict, str, str]]:
    """(endpoint, field, kind) that can produce :param using only the params already available."""
    avail = {a.lower() for a in (available or [])}
    ok = lambda e: all(x.lower() in avail for x in e["path_params"])  # noqa: E731
    out, seen = [], set()
    for parent, _ in list_producers().get(param, Counter()).most_common():
        e = gets()[parent]
        if ok(e) and parent not in seen:
            seen.add(parent)
            out.append((e, id_field(e, param), "parent list"))
    for e in field_producers(param):
        if ok(e) and e["path"] not in seen:
            seen.add(e["path"])
            out.append((e, id_field(e, param), "field"))
    return out


def chain(path: str) -> list[str]:
    """Ordered plan: auth -> producer of each :param -> the requested route."""
    hits = resolve(path)
    if not hits:
        return [f"No match for '{path}'. Try: search {path}"]
    ep = hits[0]
    s = segs(ep["path"])
    L = [f"{ep['method']} /{ep['path']}", "", "0. AUTHENTICATION"]
    L += [f"     {x}" for x in AUTH_STEPS]
    resolved: list[str] = []
    step = 1
    for i, seg in enumerate(s):
        if not seg.startswith(":"):
            continue
        param = seg[1:]
        parent = "/".join(s[:i])
        cands = producers(param, resolved)
        # The direct parent wins if it exists: it's the route's natural relationship.
        direct = [c for c in cands if c[0]["path"] == parent]
        pick = direct or cands
        L.append("")
        if not pick:
            L.append(f"{step}. :{param}  -> NO PRODUCER in the catalog. It comes from outside (another")
            L.append("     system, a previous detail call or the user). Check with: fields " + param)
        else:
            e, fld, how = pick[0]
            L.append(f"{step}. :{param}  <- GET /{e['path']}  (field {fld}; {how})")
            L.extend(f"     {n}" for n in notes(e))
            alts = pick[1:4] if direct else cands[1:4]
            if param == "personId":
                idx = [x for x in PERSON_INDEXES if x != e["path"]]
                L.append(f"     discover persons by date: {', '.join('/' + x for x in idx)}"
                         " (extract.discover_person_ids)")
            elif alts:
                L.append("     alternatives: " + ", ".join(f"/{a[0]['path']} ({a[1]})" for a in alts))
        resolved.append(param)
        step += 1
    L.append("")
    L.append(f"{step}. {ep['method']} /{ep['path']}")
    L.extend(f"     {n}" for n in notes(ep))
    n_params = len(ep["path_params"])
    if n_params:
        L.append("")
        L.append(f"   COST: {n_params} fan-out level(s) -> one call per "
                 + " x ".join(f":{p}" for p in ep["path_params"])
                 + ". Use extract.fan_out and validate with --explain / --limit.")
    return L


def notes(e: dict) -> list[str]:
    n = []
    dw = e.get("default_window")
    if dw:
        when = "next" if dw["direction"] == "next" else "last"
        fields = " or ".join(dw["fields"]) if dw["fields"] else "a date field"
        n.append(f"! 7d TRAP: only the {when} {dw['days']} days unless $filter names {fields}")
        if any("_" in f for f in dw["fields"]):
            n.append("  (the prose says snake_case; the filter that works in production is camelCase: modifyTimestamp)")
    k = e["deleted"]["kind"]
    if k in ("hides", "probable", "via", "none"):
        n.append(f"! DELETED [{k}]: {e['deleted']['why']}")
    if "$filter" not in e["query"] and e["method"] == "GET" and not e["path_params"]:
        n.append("no documented $filter")
    return n


def children(path: str) -> list[dict]:
    base = shape(path).rstrip("/")
    return sorted((e for e in eps() if shape(e["path"]).startswith(base + "/")),
                  key=lambda e: (e["path"], e["method"]))


# --------------------------------------------------------------------------
# Presentation
# --------------------------------------------------------------------------
def keep_drop(ep: dict):
    keep, drop = [], {}
    for f in ep["fields"]:
        for reason, rx in DROP_RULES:
            if rx.search(f):
                drop[f] = reason
                break
        else:
            keep.append(f)
    return sorted(keep), drop


def describe(ep: dict) -> str:
    L = [f"{ep['method']} /{ep['path']}", f"  folder: {ep['folder']}    envelope: {ep['envelope'] or 'n/a'}"]
    if ep["description"]:
        L.append(f"  {ep['description'][:300]}")
    for n in notes(ep):
        L.append(f"  {n}")
    if ep.get("extra_trap"):
        L.append(f"  ! MEASURED TRAP: {ep['extra_trap']}")
    flags = sorted(f for f in ep["fields"] if LIFECYCLE_RE.match(f))
    if flags:
        L.append(f"  LIFECYCLE: {', '.join(flags)}   (cancelled/no-show/inactive are NOT deletions)")
    od = [q for q in ep["query"] if q in ODATA]
    other = [q for q in ep["query"] if q not in ODATA]
    if od:
        L.append(f"  OData:  {'  '.join(od)}   (there is no $select)")
    if other:
        L.append(f"  Other:  {'  '.join(other)}")
    if ep["path_params"]:
        L.append(f"  Path:   {', '.join(':' + p for p in ep['path_params'])}   -> see: chain {ep['path']}")
        resolved: list[str] = []
        for p in ep["path_params"]:
            c = producers(p, resolved)
            src = f"/{c[0][0]['path']} ({c[0][1]})" if c else "external"
            L.append(f"          :{p} <- {src}")
            resolved.append(p)
    kids = children(ep["path"])
    if kids:
        L.append(f"  Subroutes: {len(kids)}   -> see: children {ep['path']}")
    if ep["fields"]:
        keep, drop = keep_drop(ep)
        L.append(f"\n  USEFUL FIELDS ({len(keep)}):")
        for i in range(0, len(keep), 4):
            L.append("      " + "  ".join(f"{f} <{ep['fields'][f]}>" for f in keep[i:i + 4]))
        if drop:
            L.append(f"  DROPPABLE ({len(drop)}, extract.prune): "
                     + ", ".join(f"{f}[{r}]" for f, r in sorted(drop.items())))
    L.append("\n  Example:\n      " + example(ep))
    return "\n".join(L)


def example(ep: dict) -> str:
    if ep["method"] != "GET":
        return f"ng.{ep['method'].lower()}('{ep['path']}')   # write call: check the body; don't use it to extract"
    path = re.sub(r":(\w+)", r"{\1}", ep["path"])
    paged = ep["envelope"] == "paged" or "$top" in ep["query"]
    if ep["path_params"]:
        return f"extract.fan_out(ng, parents, '{path}')" if paged else f"ng.get_one('{path}'.format(...))"
    call = f"ng.get_all('{path}'" if paged else f"ng.get_one('{path}'"
    dw = ep.get("default_window")
    if dw and dw["fields"]:
        f = dw["fields"][0].replace("modify_timestamp", "modifyTimestamp").replace("create_timestamp", "createTimestamp")
        call += f", params=params(where('{f}>=2025-01-01', '{f}<=2025-12-31'))"
    elif dw:
        call += ", params=params(F.since('2025-01-01'))   # mandatory"
    elif "$filter" in ep["query"]:
        call += ", params=params(F.since('2025-01-01'))"
    return call + ")"


def print_tree(prefix: str, depth: int) -> None:
    base = segs(norm(prefix)) if prefix else []
    tree: dict = {}
    methods: dict[str, set] = defaultdict(set)
    for e in eps():
        s = segs(e["path"])
        if [x.lower() for x in s[:len(base)]] != [x.lower() for x in base]:
            continue
        rel = s[len(base):]
        node = tree
        for x in rel[:depth]:
            node = node.setdefault(x, {})
        methods["/".join(s[:len(base) + min(len(rel), depth)])].add(e["method"])

    def walk(node: dict, parts: list[str], indent: int):
        for k in sorted(node):
            full = "/".join(base + parts + [k])
            m = ",".join(sorted(methods.get(full, ())))
            sub = len(children(full)) if len(parts) + 1 == depth else 0
            more = f"  (+{sub} subroutes)" if sub else ""
            print(f"{'  ' * indent}{k}  {('[' + m + ']') if m else ''}{more}")
            walk(node[k], parts + [k], indent + 1)

    print("/" + "/".join(base))
    walk(tree, [], 1)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main(argv: list[str]) -> int:
    try:
        sys.stdout.reconfigure(errors="replace")
    except AttributeError:
        pass
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    cmd, args = argv[0], argv[1:]

    if cmd == "stats":
        d = load()
        print(f"  built       {d['generated_at']}  ({d['source']})")
        for k, v in d["stats"].items():
            print(f"  {k:11} {v}")
        print(f"  params      {len({p for e in eps() for p in e['path_params']})} distinct path parameters")

    elif cmd == "folders":
        for f, n in Counter(e["folder"] for e in eps()).most_common():
            print(f"  {n:5}  {f}")

    elif cmd == "search":
        term = (args[0] if args else "").lower()
        method = args[1].upper() if len(args) > 1 else ""
        hits = [e for e in eps() if (not method or e["method"] == method) and (
            term in e["path"].lower() or term in e["name"].lower() or term in e["description"].lower())]
        print(f"{len(hits)} endpoints\n")
        for e in hits[:60]:
            flag = " [7d TRAP]" if e["default_window"] else (" [TRAP]" if e["extra_trap"] else "")
            print(f"  {e['method']:6} /{e['path']}{flag}")
        if len(hits) > 60:
            print(f"\n  ... and {len(hits) - 60} more. Refine the term or filter by method: search {term} GET")

    elif cmd == "show":
        if not args:
            print("usage: show <path>")
            return 1
        hits = resolve(args[0], method="")
        if not hits:
            print(f"No match for '{args[0]}'. Try: search {args[0]}")
            return 1
        for e in hits[:3]:
            print(describe(e))
            print("\n" + "-" * 70 + "\n")
        if len(hits) > 3:
            print(f"... and {len(hits) - 3} more matches; refine the path.")

    elif cmd == "fields":
        if not args:
            print("usage: fields <field>")
            return 1
        q = args[0].lower()
        hits = [e for e in eps() if e["method"] == "GET" and any(q in f.lower() for f in e["fields"])]
        hits.sort(key=lambda e: (len(e["path_params"]), e["path"]))
        print(f"{len(hits)} GETs return a field like '{args[0]}' (those needing no params first)\n")
        for e in hits[:40]:
            m = [f for f in e["fields"] if q in f.lower()]
            print(f"  /{e['path']}   -> {', '.join(m)}")

    elif cmd == "traps":
        t = [e for e in eps() if e["default_window"] or e["extra_trap"]]
        print(f"{len(t)} endpoints with a trap:\n")
        for e in sorted(t, key=lambda e: e["path"]):
            print(f"  {e['method']} /{e['path']}")
            for n in notes(e):
                if "TRAP" in n or n.startswith("  ("):
                    print(f"      {n}")
            if e["extra_trap"]:
                print(f"      ! {e['extra_trap']}")
        lc = load()["lifecycle"]
        print("\nHide deleted rows (measured, default view / when requested):")
        for p, m in lc["hides_deleted"].items():
            print(f"  /{p:45} {m}")

    elif cmd == "lifecycle":
        term = norm(args[0]).lower() if args else ""
        groups = defaultdict(list)
        for e in gets().values():
            if term and term not in e["path"].lower():
                continue
            groups[e["deleted"]["kind"]].append(e)
        for k, title in LIFECYCLE_TITLES.items():
            hs = sorted(groups.get(k, []), key=lambda e: (":" in e["path"], e["path"]))
            if not hs:
                continue
            print(f"\n[{k}] {title}  ({len(hs)})")
            shown = hs if term or k not in ("probable", "detail") else hs[:25]
            for e in shown:
                print(f"  /{e['path']}   {e['deleted']['why'] if term else ''}")
            if len(shown) < len(hs):
                print(f"  ... and {len(hs) - len(shown)} more. Filter: lifecycle <fragment>")

    elif cmd == "params":
        allp = Counter(p for e in eps() for p in e["path_params"])
        if args:
            p = args[0].lstrip(":")
            match = [x for x in allp if x.lower() == p.lower()] or [x for x in allp if p.lower() in x.lower()]
            if not match:
                print(f"No path parameter like '{p}'. List them all with: params")
                return 1
            for name in match[:5]:
                print(f":{name}  used in {allp[name]} routes")
                c = producers(name, available=list(allp))  # unrestricted
                for e, fld, how in c[:10]:
                    req = ", ".join(":" + x for x in e["path_params"]) or "nothing"
                    print(f"   <- /{e['path']}  field {fld}  [{how}; requires {req}]")
                if not c:
                    print("   <- no producer in the catalog (comes from outside or from a detail call)")
                print()
        else:
            print(f"{len(allp)} path parameters. Main producer (no prerequisites, or the fewest):\n")
            for name, n in allp.most_common():
                c = producers(name, available=list(allp))
                src = f"/{c[0][0]['path']} ({c[0][1]})" if c else "external"
                print(f"  :{name:30} {n:4} routes   <- {src}")

    elif cmd == "chain":
        if not args:
            print("usage: chain <path>")
            return 1
        print("\n".join(chain(args[0])))

    elif cmd == "children":
        if not args:
            print("usage: children <path>")
            return 1
        kids = children(args[0])
        print(f"{len(kids)} subroutes of /{norm(args[0])}\n")
        for e in kids:
            print(f"  {e['method']:6} /{e['path']}")

    elif cmd == "tree":
        depth, rest, i = 2, [], 0
        while i < len(args):
            if args[i] == "--depth" and i + 1 < len(args):
                depth = int(args[i + 1])
                i += 2
            else:
                rest.append(args[i])
                i += 1
        print_tree(rest[0] if rest else "", depth)

    else:
        print(f"Unknown command: {cmd}\n")
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
