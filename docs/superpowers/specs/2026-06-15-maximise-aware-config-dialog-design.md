# MaximiseAware config dialog — design

## Problem

Border color, border width, and the title-bar text are configurable today, but
only by hand-editing `~/.config/terminator/config`. Add a point-and-click
Preferences dialog, reachable from Terminator's right-click menu, that edits
these three settings and applies them live.

## Scope

- **In scope:** a `terminal_menu` menu item opening a modal dialog with three
  fields (border color, border width, title text), Save/Cancel, and live
  re-apply of the indicators on Save.
- **Out of scope:** GUI controls for `enable_badge`/`enable_title`/
  `enable_border` and `badge_format` (remain config-file-only); any change to
  the existing background behavior (signal wiring, counting, teardown).

Verified against Terminator 2.1.5. Established pattern: `custom_commands.py`
(`plugin.MenuItem` with `callback` + `configure` + `Config().plugin_set` +
`config.save()`); the context menu invokes `terminal_menu` plugins'
`callback(menuitems, menu, terminal)` at `terminal_popup_menu.py:311-313`.

## Architecture

`MaximiseAware` gains a second capability and two methods; everything else is
unchanged.

- `capabilities = ['maximise_aware', 'terminal_menu']`.
- **`callback(self, menuitems, menu, terminal)`** — appends one
  `Gtk.MenuItem` labelled "MaximiseAware Preferences" to `menuitems`, with its
  `activate` signal connected to `self.configure`.
- **`configure(self, widget, data=None)`** — builds a modal `Gtk.Dialog`,
  transient for `widget.get_toplevel()`, runs it, and on `ACCEPT` saves config
  and live-rebuilds indicators. Always `destroy()`s the dialog.
- **`_rebuild_indicators(self)`** — clears cues on every tracked terminal using
  the *current* indicators, then `self.indicators = self._build_indicators()`.
  This is the live-apply mechanism; the dialog calls it after saving.

The dialog only reads/writes config and calls `_rebuild_indicators`; it never
touches signal wiring or the `_handlers`/registry-wrap state.

## The dialog

Modal `Gtk.Dialog`, title "MaximiseAware Preferences", buttons Cancel
(`Gtk.ResponseType.REJECT`) / Save (`Gtk.ResponseType.ACCEPT`). Content is a
`Gtk.Grid` (or stacked boxes) with three labelled rows:

1. **Border color** — `Gtk.ColorButton`. On open, parse the stored hex
   `border_color` into a `Gdk.RGBA` via `rgba.parse(hexstr)`; if parse fails,
   use the default color and log via `err()`. On save, read
   `colorbutton.get_rgba()` and convert to `#rrggbb`.
2. **Border width** — `Gtk.SpinButton` with `Gtk.Adjustment(value, 0, 10, 1)`.
   Initialised from `border_width` (coerced via `int`, default on failure).
3. **Title text** — `Gtk.Entry` initialised from `title_format`, plus a small
   hint label: "`{n}` = number of hidden terminals". Accepts any string.

## Pure helpers (the only new testable logic)

Two module-level, GTK-free functions:

- `rgba_to_hex(r, g, b)` — floats in `[0,1]` → `'#rrggbb'`, each channel
  `int(round(c * 255))` clamped to `0..255`, lowercase two-digit hex.
- `hex_to_rgb(hexstr, default_rgb)` — parse `'#rrggbb'` (with or without `#`,
  case-insensitive) → `(r, g, b)` floats in `[0,1]`; on any malformed input
  return `default_rgb`.

The dialog uses `Gdk.RGBA` for the widget itself; these helpers do the
hex<->float conversion so the conversion logic is unit-testable without GTK.
`rgba_to_hex(*hex_to_rgb('#5294e2', ...))` round-trips back to `'#5294e2'`.

## Save + live apply

On **Save** (`ACCEPT`):

1. `color = rgba_to_hex(rgba.red, rgba.green, rgba.blue)` from the ColorButton.
2. `width = int(spin.get_value())`.
3. `title = entry.get_text()`.
4. `config = Config()`; `config.plugin_set('MaximiseAware', 'border_color', color)`;
   same for `'border_width'` (as `str(width)`) and `'title_format'` (title);
   `config.save()`.
5. `self._rebuild_indicators()`.

On **Cancel** (`REJECT`) or close: do nothing. Always `dbox.destroy()`.

Live apply detail: `_rebuild_indicators` first clears cues on currently-tracked
terminals (so an in-progress maximised border/title is removed with the old
indicator instances), then rebuilds from the just-saved config. The next
maximise renders the new look. (Re-showing immediately on an already-maximised
terminal is intentionally out of scope — unmaximise/re-maximise shows it.)

## Error handling

- Bad stored hex → `hex_to_rgb` returns the default; dialog opens normally,
  logged via `err()`. Never crashes.
- Malformed title text (no `{n}`, stray braces) → handled downstream by the
  existing `render_marker` safe fallback.
- `configure`/`callback` bodies guard against unexpected GTK failures with
  `err()` logging so the right-click menu is never broken.
- `config.save()` writes the user's real Terminator config; only the three
  `MaximiseAware` keys are touched via `plugin_set` (no wholesale rewrite of
  other plugins' settings).

## Testing

- **Unit (pure, no GTK):** `rgba_to_hex` exact output; `hex_to_rgb` valid parse,
  no-`#` form, uppercase form, malformed → default; round-trip
  `rgba_to_hex(*hex_to_rgb(h, d)) == h` for several hex values.
- **Manual checklist (isolated test window):**
  1. Right-click → "MaximiseAware Preferences" opens the dialog.
  2. Dialog shows current values (color swatch, width, title text).
  3. Change color + width + title, Save → maximise → new look applies live, no
     restart.
  4. Reopen dialog → shows the saved values.
  5. Edit a field, Cancel → change discarded (reopen confirms old value).
  6. Hand-edit `border_color` to garbage in config, reopen dialog → opens with
     default color, no crash.
  7. Existing cues still work after rebuild (border + title on maximise, clear
     on unmaximise).

## File layout

```
maximise_aware.py            # + rgba_to_hex, hex_to_rgb, callback, configure, _rebuild_indicators
tests/test_helpers.py        # + color-conversion tests
README.md                    # note the Preferences dialog
```
