# nextgen-api — Claude Code skill for the NextGen Enterprise API

A Claude Code skill that knows the NextGen Enterprise API: its **547 routes**, the
filters and fields of each one, **how routes depend on each other** (where every
`:personId`, `:encounterId`, `:orderId`… comes from and in what order to call them),
the silent traps that cause most extraction errors, and the `nextgen/` library in the
**nextgen-platform** repo.

It works **offline and without credentials**: the route catalog ships with the skill.
Inside the nextgen-platform repo, Claude also uses it to write extraction scripts that
reuse the repo's library.

## Install

Requires Python 3.8+ and Claude Code.

```powershell
# Windows
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

```sh
# macOS / Linux / Git Bash
sh install.sh
```

This copies `nextgen-api/` to `~/.claude/skills/nextgen-api/`, so it's available in
every project. Restart Claude Code afterwards. To update, re-run the installer.

To share it with one project only, copy `nextgen-api/` to `<project>/.claude/skills/` instead.

## Use

Just ask Claude; the skill loads on its own for NextGen questions. You can also call it
explicitly with `/nextgen-api <question>`. Examples:

- "Which endpoint gives me the insurances of an encounter, and what do I call first?"
- "I need 2025 appointments for these locations, including deleted ones."
- "Where do I get an `orderId` for `/persons/{personId}/chart/lab/orders/{orderId}/tests`?"
- "My charges extraction returns fewer rows than expected. Why?"
- "How many calls would pulling diagnoses for every person cost?"

The explorer also works on its own from a terminal:

```sh
python ~/.claude/skills/nextgen-api/scripts/ngcat.py chain "persons/{personId}/chart/encounters/{encounterId}/insurances"
```

| Command | What it answers |
|---|---|
| `search <term> [GET]` | which endpoints match |
| `show <path>` | filters, fields and types, traps, deleted-row behavior, dependencies |
| `fields <field>` | which endpoints return a field |
| `chain <path>` | ordered call plan to reach a route, with cost |
| `params [name]` | which route produces each path parameter |
| `children <path>` / `tree [prefix] --depth N` | what hangs off a route |
| `traps` / `lifecycle [path]` | 7-day default windows / how deleted rows are handled |
| `stats` / `folders` | catalog totals and when the index was built |

## What's inside

```
nextgen-api/
  SKILL.md                      entry point Claude reads
  scripts/ngcat.py              offline route explorer (standard library only)
  scripts/build_index.py        rebuilds the index from the repo (maintainers only)
  data/endpoints.json           route catalog: paths, filters, field names and types
  references/
    route-dependencies.md       route tree, parameter producers, call chains, cost
    api-fundamentals.md         auth, retries, OData, default windows, paging, data shape
    deleted-and-lifecycle.md    hidden deleted rows and how to fetch them
    query-strategies.md         proven extraction strategies from the Glue pipeline
    recipes.md                  copy-ready code using the nextgen/ library
    library.md                  nextgen/ modules, functions and their dependencies
    troubleshooting.md          symptom -> cause -> fix
    pipeline-map.md             the 26 production tables
  hooks/guard_sensitive.py      optional PHI guard (see below)
```

`data/endpoints.json` contains **metadata only** (paths, parameters, field names and
types). The source collection's examples are typed placeholders (`<string>`,
`<dateTime>`), so no patient data ships with the skill.

## PHI and approvals

NextGen data is PHI. The skill tells Claude never to show patient rows, `.env`,
`output/` or `bookmarks/`, and to show counts and column names only. Using Claude Code
(or any other integration) **with PHI** requires that service and mode to be approved in
[vipmed-technology/approved-services](https://github.com/vipmed-technology/approved-services);
if you can't confirm it, check with VIPMedicalGroup Cybersecurity before running
extractions that touch PHI. Exploring routes with `ngcat` involves no PHI.

## Optional: PHI guard hook

The skill's rules are instructions; the hook enforces them. It blocks Claude from
reading `.env`, `output/`, `bookmarks/` and the swagger files, including through the
shell. Add this to `~/.claude/settings.json` (merge it with any existing `hooks`):

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash|PowerShell|Read|Grep|Glob",
        "hooks": [
          {
            "type": "command",
            "command": "python \"$HOME/.claude/skills/nextgen-api/hooks/guard_sensitive.py\"",
            "timeout": 10
          }
        ]
      }
    ]
  }
}
```

The nextgen-platform repo already ships an equivalent hook in its project settings, so
inside that repo this is redundant.

## Maintaining the index

The catalog is a snapshot of the repo's `nextgen/catalog.py` (`ngcat.py stats` shows
the build date). Rebuild it when the swagger changes or someone records a new
measurement there (`HIDES_DELETED`, `SHOWS_DELETED`, `NO_DELETED_FLAG`, `DELETED_VIA`,
`_EXTRA_TRAPS`):

```sh
python nextgen-api/scripts/build_index.py --repo /path/to/nextgen-platform
```

If a new hand-written note appears in the repo's catalog (written in Spanish), the
script warns until you add its English text to `EXTRA_TRAPS_EN` / `TEXT_EN` in
`build_index.py`. Then redistribute this folder and have everyone re-run the installer.
