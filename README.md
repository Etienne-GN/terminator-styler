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

- Internal padding around every VTE pane (configurable, 0–60 px,
  default 10). Applied as VTE margins, so it works without a compositor
  and covers panes opened later.
- Rounded window corners (12 px). Requires a compositor (GNOME Shell,
  picom, …) to actually render transparent corners. Some compositors
  draw their own window rounding regardless and override this.

### Maximise indicators

When a pane is maximised and sibling panes are hidden, show passive cues:

- A **badge** appended to the maximised pane's titlebar.
- A **marker** appended to the window title (and tab label, if any).
- A subtle **border** around the maximised pane.
- Optional: **border color follows the focused pane's profile foreground**,
  so the indicator color matches whatever profile is in use.

`{n}` in badge/title format expands to the number of hidden sibling panes.
Only siblings in the **current tab** are counted; all cues clear on
unmaximise.

A titlebar you renamed by hand ignores the badge (Terminator keeps the
custom label); the title marker and border still show.

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
when "follows profile" is enabled. The per-pane titlebar coloring also
covers the small group menu on its left.

Your shell sets the window title through escape sequences (most distros
do this by default in `~/.bashrc` / `~/.zshrc`); the plugin watches that
title and reverts to the theme colors when no rule matches.

> The **Window** target needs GTK3 client-side decorations (CSD), the
> default on modern GNOME (X11 and Wayland). It has no visual effect
> under window managers that draw their own (server-side) decorations.
> The **Titlebar** target works regardless.

Example rules:

| Name       | Pattern      | BG        | FG        | Matches                         |
| ---------- | ------------ | --------- | --------- | ------------------------------- |
| root       | `root@`      | `#cc0000` | `#ffffff` | `sudo -i`, `su -` prompts       |
| SSH        | `@.*\..*:`   | `#1a5276` | `#d6eaf8` | `user@host.domain:` prompts     |
| production | `prod`       | `#7b241c` | `#fdfefe` |                                 |
| staging    | `stag`       | `#7d6608` | `#fef9e7` |                                 |
| Docker     | `\(docker\)` | `#154360` | `#d6eaf8` |                                 |

To tag environments explicitly, put them in the title from your prompt
and match `\[prod\]`, `\[staging\]`, …:

```bash
# ~/.bashrc: title becomes  [env] user@host:path
PROMPT_COMMAND='echo -ne "\033]0;[${ENV:-dev}] \u@\h:\w\007"'
```

### Profile switcher

Auto-switch the Terminator profile based on the foreground command (and
optionally its argv) in each terminal. Detection is local — reads
`/proc/<pgid>/comm` and `/proc/<pgid>/cmdline` of the foreground process
group of the terminal's PTY. No shell setup or remote configuration
required.

Each rule has a command (matched exactly against
`/proc/<pid>/comm`, truncated at 15 chars), an optional argument glob
(case-insensitive `fnmatch` against the joined argv), and a profile.
The profile must exist in *Preferences → Profiles*; a missing one falls
back to `default`. First matching rule wins; no match reverts to the
`default` profile, but only if a rule had previously been applied, so a
profile you picked by hand is never overwritten.

Each terminal is polled once per second: `os.tcgetpgrp()` on its PTY
gives the foreground process group. Nothing runs in the shell or on
remote hosts, and any shell works. The argument glob follows Python's
[`fnmatch`](https://docs.python.org/3/library/fnmatch.html).

Examples:

| Command | Argument  | Profile     |
| ------- | --------- | ----------- |
| ssh     | `*prod*`  | red         |
| ssh     | `*stage*` | yellow      |
| top     | (empty)   | dark        |
| python3 | (empty)   | solarized   |

## Requirements

- Terminator (developed against 2.1.x)
- Python 3 with PyGObject (both come with Terminator)
- Linux: the profile switcher reads `/proc`

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

- `TitlebarChanger` (or its predecessor `TitleReact`, including the
  old single `target = titlebar | window` key)
- `ProfileSwitcher`
- `WindowStyler`
- `MaximiseAware`

into a single new `TerminatorStyler` block. The old blocks are left
untouched so you can roll back; once the new block exists the migration
does not run again.

## Configuration file

All settings live in one block of `~/.config/terminator/config` and are
normally edited from the Preferences dialog:

```ini
[plugins]
  [[TerminatorStyler]]
    enable_window = True
    enable_maximise = True
    enable_scrollbar = True
    enable_titlebar = True
    enable_profileswitcher = True
    ws_padding = 10
    mx_enable_badge = True
    mx_enable_title = True
    mx_enable_border = True
    mx_badge_format = [⊞ {n}]
    mx_title_format = "   ◆ ⊞ {n} HIDDEN"
    mx_border_color = "#5294e2"
    mx_border_width = 1
    mx_border_follow_profile = False
    tb_target_titlebar = False
    tb_target_window = True
    tb_window_follow_focus = False
    tb_follow_profile = False
    [[[tb_rule_0]]]
      name = root
      pattern = root@
      bg_color = "#cc0000"
      fg_color = "#ffffff"
      enabled = True
      position = 0
    [[[ps_rule_0]]]
      command = ssh
      argument = *prod*
      profile = red
      position = 0
```

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

## Tests

```sh
pytest-3 tests
```

The tests cover the pure helpers and config parsing; they import
`terminatorlib`, so Terminator must be installed.

## License

MIT. See `LICENSE`.
