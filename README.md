# Terminator Styler

A single Terminator plugin that bundles six appearance / behavior tweaks
under one Preferences dialog, plus a `degauss` command.

It replaces these previously standalone plugins:

- `terminator-titlebar-changer`
- `terminator-profile-changer`
- `terminator-window-styler`
- `terminator-maximise-aware`
- the `degauss` experiment (`degauss_plugin.py` + `degauss`)

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

### Degauss

Degauss a pane like a 90s CRT monitor: a white flash, the picture
shaking and settling, optionally with the thunk and mains hum of the
degauss coil. Trigger it with **Degauss this pane** in the context menu,
or run `degauss` in the pane (handy at the end of a script or in an
alias).

Two effects, picked on the **Degauss** tab:

- **Wobble** — the pane's own content shakes in horizontal strips.
  Optional **Rainbow blotches** orbit over it, like a magnetized tube.
- **Test pattern** — color bars shake instead, with a **rainbow swirl**.

Every parameter is on the tab: duration, initial flash, shake strength,
strip height, blotch count and strength, swirl amount, and the sound
(off by default; volume, 50/60 Hz hum, player). **Test on this pane**
previews the values in the dialog before you press OK.

How `degauss` behaves:

- Inside Terminator it asks the plugin, through
  `$XDG_RUNTIME_DIR/terminator-styler-degauss-<pid>.sock`, to animate
  the pane it runs in (found through `$TERMINATOR_UUID`). The plugin
  draws the effect and plays the sound; the command returns when the
  effect is over.
- Anywhere else (another terminal emulator, an SSH session, plugin not
  loaded) it plays the sound itself and draws the Test pattern with
  terminal colors, using the same settings read from Terminator's config
  file. **Frames per second** only applies there.
- With Degauss switched off on the General tab, it does nothing.

#### Fire on typos

List your usual typos under **Fire on these commands** (pre-filled with
`systemclt`) and add this line to `~/.bash_aliases` (sourced by Debian's
default `~/.bashrc`) or to `~/.bashrc`:

```bash
. ~/.local/share/degauss/hook.bash
```

`install.sh` writes that file from `degauss --shell-init`; re-run it
after updating the repo to refresh the hook. The typo list itself is
not in the file, so Preferences edits never need a reinstall.

It installs a bash `command_not_found_handle`: typing one of those
exact names degausses the terminal silently, like a shell function
would. Only names that are not real commands can fire, the list is read
at the moment of the typo (edits apply to open shells immediately),
and nothing is added to tab completion. Other typos, and listed ones in
a pipe or without a terminal, get the usual *command not found*. An
existing handler, such as Debian's `command-not-found`, keeps handling
everything else. It only runs in local interactive bash: scripts and
remote shells never load it.

The sound is synthesized once per sound setting and cached in
`~/.cache/degauss/`. Playback uses `pw-play`, `paplay` or `aplay`,
whichever is found first unless one is picked.

## Requirements

- Terminator (developed against 2.1.x)
- Python 3 with PyGObject (both come with Terminator)
- Linux: the profile switcher reads `/proc`
- For Degauss: pycairo (a Terminator dependency) and, for sound, one of
  `pw-play`, `paplay` or `aplay`

## Install

```sh
bash install.sh
```

This installs the plugin into `~/.config/terminator/plugins/`, the
command as `~/.local/bin/degauss` and the typo hook as
`~/.local/share/degauss/hook.bash`, backing up any different existing
copy of the first two. Then restart Terminator and enable **TerminatorStyler** in
*Preferences → Plugins*. If you previously had any of the old plugins
installed (including the standalone **Degauss**), disable them in the
same dialog and remove their files from `~/.config/terminator/plugins/`;
the installer lists the ones it finds.

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
    enable_degauss = True
    dg_effect = wobble          # wobble | pattern
    dg_duration = 1.9           # seconds, 0.5-5
    dg_flash = True
    dg_sound = False
    dg_volume = 30              # %, 0-100
    dg_mains_hz = 60            # 50 | 60
    dg_player = auto            # auto | pw-play | paplay | aplay
    dg_wobble_strength = 100    # %, 0-200
    dg_strip_px = 2             # 1-8
    dg_blotches = False
    dg_blotches_count = 3       # 1-8
    dg_blotches_strength = 100  # %, 0-100
    dg_pattern_rainbow = 100    # %, 0-100
    dg_pattern_fps = 30         # 10-60, terminal fallback only
    dg_triggers = systemclt     # space-separated command names
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
- All six features can be enabled together; they share one signal
  hub and do not race on `focus-in` / `title-change` / `maximise`.

## Tests

```sh
pytest-3 tests
```

The tests cover the pure helpers and config parsing; they import
`terminatorlib`, so Terminator must be installed.

## License

MIT. See `LICENSE`.
