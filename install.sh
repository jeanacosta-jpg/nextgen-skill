#!/usr/bin/env sh
# Installs the nextgen-api skill for the current user (available in every project).
#   sh install.sh
# Re-run it to update: it replaces the installed copy.
set -e
SRC="$(cd "$(dirname "$0")" && pwd)/nextgen-api"
DST="$HOME/.claude/skills/nextgen-api"

[ -f "$SRC/SKILL.md" ] || { echo "SKILL.md not found in $SRC"; exit 1; }
mkdir -p "$(dirname "$DST")"
rm -rf "$DST"
cp -R "$SRC" "$DST"

echo "Installed in $DST"
python3 "$DST/scripts/ngcat.py" stats 2>/dev/null || python "$DST/scripts/ngcat.py" stats
echo
echo "Restart Claude Code and ask, e.g.: 'which endpoint gives me encounter insurances?'"
echo "Optional PHI guard hook: see README.md, section 'Optional: PHI guard hook'."
