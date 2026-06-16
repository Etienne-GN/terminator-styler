#!/usr/bin/env bash
# Installer for the Terminator "Styler" plugin.
#
# Usage:
#   bash install.sh             # install (or update)
#   bash install.sh --uninstall # remove the plugin

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_SRC="$SCRIPT_DIR/styler.py"
PLUGIN_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/terminator/plugins"
PLUGIN_DST="$PLUGIN_DIR/styler.py"

# Files left behind by the old standalone plugins this one replaces.
LEGACY_PLUGINS=(
    "$PLUGIN_DIR/titlebar_changer.py"
    "$PLUGIN_DIR/profile_switcher.py"
    "$PLUGIN_DIR/window_styler.py"
    "$PLUGIN_DIR/maximise_aware.py"
)

if [[ "${1:-}" == "--uninstall" ]]; then
    if [[ -f "$PLUGIN_DST" ]]; then
        rm -f "$PLUGIN_DST"
        echo "Removed $PLUGIN_DST"
        echo "Disable 'TerminatorStyler' in Terminator > Preferences > Plugins,"
        echo "then restart Terminator."
    else
        echo "Nothing to remove (no $PLUGIN_DST)."
    fi
    exit 0
fi

if [[ ! -f "$PLUGIN_SRC" ]]; then
    echo "Error: plugin file not found at $PLUGIN_SRC" >&2
    exit 1
fi

if ! command -v terminator >/dev/null 2>&1; then
    echo "Warning: 'terminator' not found in PATH." >&2
    echo "         Installing the plugin file anyway." >&2
fi

if ! command -v python3 >/dev/null 2>&1; then
    echo "Error: 'python3' is required." >&2
    exit 1
fi

if ! python3 -c "import ast; ast.parse(open('$PLUGIN_SRC').read())" 2>/dev/null; then
    echo "Error: plugin failed Python syntax check." >&2
    exit 1
fi

mkdir -p "$PLUGIN_DIR"

if [[ -f "$PLUGIN_DST" ]]; then
    backup="$PLUGIN_DST.bak.$(date +%Y%m%d-%H%M%S)"
    cp -p "$PLUGIN_DST" "$backup"
    echo "Backed up existing plugin to $backup"
fi

cp "$PLUGIN_SRC" "$PLUGIN_DST"

# Warn (don't auto-remove) about the four legacy plugin files.
legacy_found=()
for f in "${LEGACY_PLUGINS[@]}"; do
    if [[ -f "$f" ]]; then
        legacy_found+=("$f")
    fi
done

cat <<MSG

Installed: $PLUGIN_DST

Next steps:
  1. (Re)start Terminator -- plugins are only scanned at startup.
  2. Open Preferences > Plugins, enable 'TerminatorStyler'
     (and disable any of the four old plugins it replaces).
  3. Right-click any terminal > Styler > Preferences...
     to configure each feature (one tab per feature).

Settings from the four old plugins are migrated automatically the first
time TerminatorStyler loads. The migration only runs once.

MSG

if (( ${#legacy_found[@]} > 0 )); then
    cat <<MSG
The following legacy plugin files are still present and should be removed
or disabled once you have verified that TerminatorStyler works:

MSG
    printf '  %s\n' "${legacy_found[@]}"
    cat <<MSG

Remove them with:
  rm ${legacy_found[*]}

Then disable the old plugin names in Terminator > Preferences > Plugins:
  TitlebarChanger, ProfileSwitcher, WindowStyler, MaximiseAware

MSG
fi
