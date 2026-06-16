# Terminator MaximiseAware plugin — design

## Problem

Terminator's **Maximise** and **Zoom** actions hide the sibling terminals in
the current split, leaving no on-screen reminder that other panes still exist.
It is easy to forget the layout and lose track of running work.

This plugin adds passive, always-visible cues that other terminals are hidden
while a terminal is maximised, and clears them cleanly on unmaximise.

## Scope

- **In scope:** three passive indicators, current-tab siblings only,
  independently toggleable, full teardown on unload.
- **Out of scope:** counting terminals in other tabs or other windows; any
  click/menu interaction; restoring or cycling hidden terminals.

Verified against the target environment: **Terminator 2.1.5**, library at
`/usr/lib/python3/dist-packages/terminatorlib`.

## Architecture

A single plugin file installed to `~/.config/terminator/plugins/`. The class
declares a plugin capability so Terminator instantiates it at startup; all work
begins in `__init__` and is undone in an `unload`/cleanup path.

Four units behind one coordinator. Each has one purpose and a narrow interface:

- **Coordinator (`MaximiseAware`)** — acquires the `Terminator` singleton,
  attaches the Detector, owns the set of enabled Indicators, reads Config.
  Wires `Detector → update(terminal, hidden_count)` to every enabled Indicator.
- **Detector** — knows *when* maximise state changes and *how many* siblings are
  hidden in the current tab. Emits a single call `update(terminal, count)` on
  maximise and `clear(terminal)` on unmaximise.
- **Indicators** — three independent renderers (`BadgeIndicator`,
  `TitleIndicator`, `BorderIndicator`). Each implements `show(terminal, count)`
  and `clear(terminal)` and knows nothing about the others.
- **Config** — reads toggles/format/colour from Terminator's plugin config with
  safe defaults.

Adding or removing a cue later is adding or removing one Indicator.

## Detection (verified)

Each `Terminal` emits GObject signals (`terminal.py:63-65`):

- `maximise` — maximise without font scaling
- `zoom` — maximise with font scaling
- `unzoom` — restore layout

`Window` keeps a `term_zoomed` boolean property and `is_zoomed()`
(`window.py:524-530`). The plugin connects to each terminal's `maximise`,
`zoom`, and `unzoom` signals using `connect_after`, so the handler runs *after*
`Window` has performed the zoom and flipped `term_zoomed`.

New terminals are created over the plugin's lifetime, so the coordinator also
connects to terminal creation and tracks/cleans up signal handler ids per
terminal (no leaks, no double-binding).

### Counting current-tab hidden siblings (verified)

On zoom, `Window.zoom()` stashes state in `Window.zoom_data`
(`window.py:543-556`):

- `old_child` — the window's former single child (the stashed subtree). The
  maximised `widget` has already been removed from its parent before this
  subtree is detached, so it is **not** present in `old_child`.
- `notebook_tabnum` / `notebook_label` — set only when the old parent is a
  `Notebook` (i.e. tabs exist).

Count = number of `Terminal` descendants of the stashed current-tab subtree:

1. If a `Notebook` is involved, restrict the walk to the zoomed page
   (`notebook_tabnum`); otherwise walk `old_child` directly.
2. Recursively collect `Terminal` instances (identified via Terminator's
   `Factory().isinstance(obj, 'Terminal')`).
3. `count` is the length of that collection. The maximised terminal is already
   excluded.

If the maximised terminal was alone in its tab, `count == 0` and all indicators
stay off.

The plan must empirically confirm the exact attribute path to `zoom_data` and
the page-restriction logic on Terminator 2.1.5 before relying on it; if the
stashed subtree proves awkward to access from a signal handler, the fallback is
to enumerate `Terminator().terminals`, filter to the maximised terminal's
window and tab, and subtract the one visible terminal.

## Indicators

### BadgeIndicator
Appends `badge_format` (default `[⊞ {n}]`) to the maximised terminal's titlebar
label. Because `Titlebar.update()` rebuilds the label from `termtext` plus
held/size text (`titlebar.py:102-113`), the badge is re-applied on the
terminal's title-change signal while maximised, and removed on unmaximise.

### TitleIndicator
Appends `title_format` (default ` ⊞×{n}`) to **both**:

- the **window title** (always — primary cue, since tabs are rarely used here);
- the **tab title**, when a tab bar exists.

The original window/tab title is stored on `show` and restored exactly on
`clear`. No accumulation across repeated maximise/unmaximise cycles.

### BorderIndicator
Draws a thin accent border around the maximised terminal widget via a
per-terminal `GtkCssProvider` added to the widget's style context on `show` and
removed on `clear`. Default `border_width = 1`, `border_color = #5294e2`
(muted). Subtle by design — a hint, not an alarm.

## Configuration

Persisted in Terminator's plugin config. Defaults (all cues on, subtle):

```
enable_badge   = True
enable_title   = True
enable_border  = True
badge_format   = "[⊞ {n}]"
title_format   = " ⊞×{n}"
border_color   = "#5294e2"
border_width   = 1
```

Each cue is independently toggleable. `{n}` is the hidden count. Invalid config
values fall back to the defaults with a logged warning.

## Lifecycle & safety

- **Teardown:** on unload/disable the coordinator disconnects every signal
  handler, restores all titles/labels, and detaches every CSS provider —
  Terminator is left exactly as found.
- **Edge cases handled explicitly:**
  - closing a terminal while maximised (Terminator auto-unzooms; ensure cues
    clear and no stale state remains);
  - unmaximise via any path (`unzoom` signal, keybinding, close);
  - terminals created after plugin load (tracked and bound);
  - multiple windows (each tracked independently by terminal/window identity);
  - repeated maximise/unmaximise (idempotent show/clear; titles never drift).
- **Errors are not silenced.** A failure to attach a signal or read config logs
  a clear warning via Terminator's `dbg`/`err`; the plugin degrades (e.g. one
  cue off) rather than half-installing or crashing Terminator.

## Testing

GTK + the `Terminator` singleton make full unit tests impractical, so:

- **Pure helpers, unit-tested in isolation:** the sibling-count walk over a
  mock container tree, and the format functions (`badge_format`/`title_format`
  rendering, including `{n}` substitution and invalid-format fallback).
- **Manual test checklist (run against installed Terminator 2.1.5):**
  1. Split into 3 panes, maximise one → badge `[⊞ 2]`, window title `⊞×2`,
     subtle border present.
  2. Unmaximise → all three cues gone; title byte-for-byte original.
  3. Maximise a lone terminal (no siblings) → no cues.
  4. With 2+ tabs, maximise in one tab → count reflects that tab only; tab
     label also carries the marker.
  5. Close a terminal while maximised → cues clear, no stale border/title.
  6. Toggle each `enable_*` off individually → only that cue disappears.
  7. Disable the plugin → titles/labels/borders fully restored.
- **Done means verified:** the checklist passes on the user's actual install
  before the work is declared complete.

## File layout

```
terminator-maximise-aware/
├── docs/superpowers/specs/2026-06-15-terminator-maximise-aware-design.md
├── maximise_aware.py            # the plugin (installed to ~/.config/terminator/plugins/)
├── tests/test_helpers.py        # pure-helper unit tests
└── README.md                    # install + config notes
```
