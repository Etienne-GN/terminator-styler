# Terminator Styler

A single Terminator plugin that bundles five appearance / behavior tweaks
under one context menu entry and one Preferences dialog.

It replaces four previously standalone plugins:

- `terminator-titlebar-changer`
- `terminator-profile-changer`
- `terminator-window-styler`
- `terminator-maximise-aware`

## Features

Each feature has its own tab in Preferences and can be toggled independently
from the **General** tab.

### Window styling

- Internal padding around every VTE pane (configurable, 0–60 px).
- Rounded window corners (12 px). Requires a compositor (GNOME Shell,
  picom, …) to actually render transparent corners.

### Maximise indicators

When a pane is maximised and sibling panes are hidden, show passive cues:

- A **badge** appended to the maximised pane's titlebar.
- A **marker** appended to the window title (and tab label, if any).
- A subtle **border** around the maximised pane.
- Optional: **border color follows the focused pane's profile foreground**,
  so the indicator color matches whatever profile is in use.

`{n}` in badge/title format expands to the number of hidden sibling panes.

### Scrollbar tinting

The scrollbar gutter of every terminal is colored to match its active
profile background. The slider itself is left to the GTK theme so the
tint stays compatible across themes.

### Titlebar painter

Color the per-pane titlebar and/or the OS window CSD header bar based on
the window title. Two independent color targets:

- **Titlebar** — per-pane strip; each split reacts independently.
- **Window** — OS-level header bar; any matching pane in the window is
  enough to trigger the color change. The window can be set to follow
  the focused pane instead.

Rules are regex patterns matched against the VTE window title; first
match wins. Each rule has a name, regex, optional BG/FG colors, and an
enabled toggle. Rules can also fall through to the active profile colors
when "follows profile" is enabled.

### Profile switcher

Auto-switch the Terminator profile based on the foreground command (and
optionally its argv) in each terminal. Detection is local — reads
`/proc/<pgid>/comm` and `/proc/<pgid>/cmdline` of the foreground process
group of the terminal's PTY. No shell setup or remote configuration
required.

Each rule has a command (matched exactly against
`/proc/<pid>/comm`, truncated at 15 chars), an optional argument glob
(case-insensitive `fnmatch` against the joined argv), and a profile.
First matching rule wins; no match reverts to the `default` profile if a
rule had previously been applied.

Examples:

| Command | Argument  | Profile     |
| ------- | --------- | ----------- |
| ssh     | `*prod*`  | red         |
| ssh     | `*stage*` | yellow      |
| top     | (empty)   | dark        |
| python3 | (empty)   | solarized   |

## Install

```sh
bash install.sh
```

Then restart Terminator and enable **TerminatorStyler** in
*Preferences → Plugins*. If you previously had any of the four old
plugins installed, disable them in the same dialog and remove their
files from `~/.config/terminator/plugins/`.

## Migration from the old plugins

The first time TerminatorStyler loads, it copies settings from any of
these old plugin config blocks it finds in `~/.config/terminator/config`:

- `TitlebarChanger`
- `ProfileSwitcher`
- `WindowStyler`
- `MaximiseAware`

into a single new `TerminatorStyler` block. The old blocks are left
untouched so you can roll back; once the new block exists the migration
does not run again.

## Uninstall

```sh
bash install.sh --uninstall
```

Then disable **TerminatorStyler** in *Preferences → Plugins* and restart.

## Notes

- Plugins are only scanned at Terminator startup. Any install / enable
  change requires a restart.
- All five features can be enabled together; they share one signal
  hub and do not race on `focus-in` / `title-change` / `maximise`.

## License

MIT. See `LICENSE`.
