# MaximiseAware Config Dialog Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a right-click "MaximiseAware Preferences" dialog to pick border color, border width, and title text, applied live on Save.

**Architecture:** `MaximiseAware` gains a second capability `terminal_menu` plus `callback()` (adds the menu item), `configure()` (modal `Gtk.Dialog`), and `_rebuild_indicators()` (live re-apply). Two GTK-free color-conversion helpers (`rgba_to_hex`, `hex_to_rgb`) are unit-tested; the dialog itself is verified manually.

**Tech Stack:** Python 3, PyGObject (GTK 3), Terminator 2.1.5 plugin API. Tests with `pytest`.

**Verified facts (Terminator 2.1.5):**
- `terminal_menu` plugins' `callback(menuitems, menu, terminal)` is invoked at `terminal_popup_menu.py:311-313`. Pattern: `custom_commands.py:176-190` — `Gtk.MenuItem.new_with_*`, `item.connect('activate', self.configure)`, `menuitems.append(item)`.
- Config dialog pattern: `custom_commands.py` `configure()` builds `Gtk.Dialog`, `dbox.run()`, on ACCEPT `Config().plugin_set(...)` + `config.save()`.
- `Gdk.RGBA().parse('#5294e2')` → True, `(red, green, blue) ≈ (0.322, 0.580, 0.886)`. `Gtk.ColorButton` has `get_rgba()`/`set_rgba()`.
- Existing `MaximiseAware`: `capabilities = ['maximise_aware']`; `DEFAULTS` holds `border_color='#5294e2'`, `border_width=1`, `title_format='   ◆ ⊞ {n} HIDDEN'`; `_build_indicators()`, `_clear_all(terminal)`, and `self._handlers` (terminal -> handler ids) already exist.

---

## File Structure

```
maximise_aware.py        # + rgba_to_hex, hex_to_rgb (helpers); + Gdk import;
                         #   + 'terminal_menu' capability, callback, configure,
                         #   _rebuild_indicators on MaximiseAware
tests/test_helpers.py    # + color-conversion tests
README.md                # + Preferences dialog note
```

---

## Task 1: Color-conversion helpers (TDD)

**Files:**
- Modify: `maximise_aware.py` (add two module-level helpers after `build_border_css`)
- Modify: `tests/test_helpers.py` (add tests + import)

- [ ] **Step 1: Add failing tests** to `tests/test_helpers.py`

Update the import line near the top of the file from:

```python
from maximise_aware import render_marker, collect_terminals, as_bool, build_border_css
```

to:

```python
from maximise_aware import (render_marker, collect_terminals, as_bool,
                            build_border_css, rgba_to_hex, hex_to_rgb)
```

Append these tests at the end of the file:

```python
def test_rgba_to_hex_basic():
    assert rgba_to_hex(0.0, 0.0, 0.0) == "#000000"
    assert rgba_to_hex(1.0, 1.0, 1.0) == "#ffffff"
    assert rgba_to_hex(82 / 255, 148 / 255, 226 / 255) == "#5294e2"


def test_rgba_to_hex_clamps_out_of_range():
    assert rgba_to_hex(-0.5, 1.5, 0.5) == "#00ff80"


def test_hex_to_rgb_valid():
    r, g, b = hex_to_rgb("#5294e2", (0.0, 0.0, 0.0))
    assert (round(r, 3), round(g, 3), round(b, 3)) == (0.322, 0.58, 0.886)


def test_hex_to_rgb_no_hash_and_uppercase():
    assert hex_to_rgb("5294E2", (0.0, 0.0, 0.0)) == hex_to_rgb("#5294e2", (0.0, 0.0, 0.0))


def test_hex_to_rgb_invalid_returns_default():
    default = (0.1, 0.2, 0.3)
    assert hex_to_rgb("nope", default) == default
    assert hex_to_rgb("#12", default) == default
    assert hex_to_rgb("", default) == default
    assert hex_to_rgb(None, default) == default


def test_color_round_trip():
    for h in ("#5294e2", "#000000", "#ffffff", "#ff5555"):
        assert rgba_to_hex(*hex_to_rgb(h, (0.0, 0.0, 0.0))) == h
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/git/terminator-maximise-aware && python3 -m pytest tests/test_helpers.py -q`
Expected: FAIL — `ImportError: cannot import name 'rgba_to_hex'`.

- [ ] **Step 3: Add the helpers** to `maximise_aware.py`, immediately after `build_border_css`:

```python
def rgba_to_hex(r, g, b):
    """Convert RGB floats in [0,1] to a '#rrggbb' string (clamped)."""
    def channel(value):
        clamped = max(0.0, min(1.0, value))
        return '%02x' % int(round(clamped * 255))
    return '#' + channel(r) + channel(g) + channel(b)


def hex_to_rgb(hexstr, default_rgb):
    """Parse '#rrggbb' (with/without '#', any case) to RGB floats in [0,1].

    Return default_rgb on any malformed input."""
    text = (hexstr or '').strip().lstrip('#')
    if len(text) != 6:
        return default_rgb
    try:
        r = int(text[0:2], 16) / 255.0
        g = int(text[2:4], 16) / 255.0
        b = int(text[4:6], 16) / 255.0
    except ValueError:
        return default_rgb
    return (r, g, b)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/git/terminator-maximise-aware && python3 -m pytest tests/test_helpers.py -q`
Expected: PASS — 13 passed (7 existing + 6 new).

- [ ] **Step 5: Commit**

```bash
cd ~/git/terminator-maximise-aware
git add maximise_aware.py tests/test_helpers.py
git commit -m "feat: hex<->rgb color conversion helpers

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Menu item, capability, and live rebuild

**Files:**
- Modify: `maximise_aware.py` (Gdk import; `terminal_menu` capability; add `_rebuild_indicators` and `callback`)

- [ ] **Step 1: Add `Gdk` to the GTK import**

Change the line:

```python
from gi.repository import Gtk
```

to:

```python
from gi.repository import Gtk, Gdk
```

- [ ] **Step 2: Add `terminal_menu` to capabilities**

Change:

```python
    capabilities = ['maximise_aware']
```

to:

```python
    capabilities = ['maximise_aware', 'terminal_menu']
```

- [ ] **Step 3: Add `_rebuild_indicators` and `callback`** to `MaximiseAware`, immediately after `_clear_all`:

```python
    def _rebuild_indicators(self):
        for terminal in list(self._handlers.keys()):
            self._clear_all(terminal)
        self.indicators = self._build_indicators()

    def callback(self, menuitems, menu, terminal):
        item = Gtk.MenuItem.new_with_label('MaximiseAware Preferences')
        item.connect('activate', self.configure)
        menuitems.append(item)
```

- [ ] **Step 4: Verify import + introspection**

Run:
```bash
cd ~/git/terminator-maximise-aware && python3 -c "import maximise_aware; print(maximise_aware.MaximiseAware.capabilities)" && python3 -m pytest tests/test_helpers.py -q
```
Expected: prints `['maximise_aware', 'terminal_menu']`; 13 passed.

(Note: `configure` does not exist yet — it is added in Task 3. Do NOT trigger the menu item until then. Import only references it via `connect`, which is not resolved until activation, so import succeeds.)

- [ ] **Step 5: Commit**

```bash
cd ~/git/terminator-maximise-aware
git add maximise_aware.py
git commit -m "feat: add terminal_menu capability, menu item, live rebuild

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: The Preferences dialog

**Files:**
- Modify: `maximise_aware.py` (add `configure` to `MaximiseAware`, after `callback`)

- [ ] **Step 1: Add the `configure` method**

```python
    def configure(self, widget, data=None):
        try:
            self._run_configure(widget)
        except Exception as ex:
            err('MaximiseAware: configure dialog failed: %s' % ex)

    def _run_configure(self, widget):
        name = self.__class__.__name__
        config = Config()
        cur_color = str(config.plugin_get(name, 'border_color',
                                          self.DEFAULTS['border_color']))
        cur_width = config.plugin_get(name, 'border_width',
                                      self.DEFAULTS['border_width'])
        cur_title = str(config.plugin_get(name, 'title_format',
                                          self.DEFAULTS['title_format']))

        dbox = Gtk.Dialog(
            'MaximiseAware Preferences', None, Gtk.DialogFlags.MODAL,
            ('Cancel', Gtk.ResponseType.REJECT, 'Save', Gtk.ResponseType.ACCEPT))
        if widget:
            dbox.set_transient_for(widget.get_toplevel())

        grid = Gtk.Grid()
        grid.set_row_spacing(6)
        grid.set_column_spacing(8)
        grid.set_border_width(10)

        default_rgb = hex_to_rgb(self.DEFAULTS['border_color'], (0.32, 0.58, 0.886))
        rgb = hex_to_rgb(cur_color, default_rgb)
        rgba = Gdk.RGBA()
        rgba.red, rgba.green, rgba.blue, rgba.alpha = rgb[0], rgb[1], rgb[2], 1.0
        color_btn = Gtk.ColorButton()
        color_btn.set_rgba(rgba)
        grid.attach(Gtk.Label(label='Border color', xalign=0), 0, 0, 1, 1)
        grid.attach(color_btn, 1, 0, 1, 1)

        try:
            wval = int(cur_width)
        except (TypeError, ValueError):
            wval = self.DEFAULTS['border_width']
        width_spin = Gtk.SpinButton()
        width_spin.set_adjustment(
            Gtk.Adjustment(value=wval, lower=0, upper=10, step_increment=1))
        width_spin.set_value(wval)
        grid.attach(Gtk.Label(label='Border width', xalign=0), 0, 1, 1, 1)
        grid.attach(width_spin, 1, 1, 1, 1)

        title_entry = Gtk.Entry()
        title_entry.set_text(cur_title)
        title_entry.set_width_chars(28)
        grid.attach(Gtk.Label(label='Title text', xalign=0), 0, 2, 1, 1)
        grid.attach(title_entry, 1, 2, 1, 1)
        hint = Gtk.Label(label='{n} = number of hidden terminals', xalign=0)
        hint.set_sensitive(False)
        grid.attach(hint, 1, 3, 1, 1)

        dbox.get_content_area().pack_start(grid, True, True, 0)
        dbox.show_all()

        if dbox.run() == Gtk.ResponseType.ACCEPT:
            new_rgba = color_btn.get_rgba()
            color = rgba_to_hex(new_rgba.red, new_rgba.green, new_rgba.blue)
            width = int(width_spin.get_value())
            title = title_entry.get_text()
            config.plugin_set(name, 'border_color', color)
            config.plugin_set(name, 'border_width', str(width))
            config.plugin_set(name, 'title_format', title)
            config.save()
            self._rebuild_indicators()
        dbox.destroy()
```

- [ ] **Step 2: Verify import + tests**

Run: `cd ~/git/terminator-maximise-aware && python3 -c "import maximise_aware" && python3 -m pytest tests/test_helpers.py -q`
Expected: clean import; 13 passed.

- [ ] **Step 3: Commit**

```bash
cd ~/git/terminator-maximise-aware
git add maximise_aware.py
git commit -m "feat: MaximiseAware Preferences dialog with live apply

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: README, install, manual verification

**Files:**
- Modify: `README.md`
- Install: copy `maximise_aware.py` to `~/.config/terminator/plugins/`

- [ ] **Step 1: Document the dialog in `README.md`**

Insert this block immediately after the `## Configuration` section's config example (after the closing of the `border_width = 1` code block, before the `## Compatibility` heading):

```markdown
### Preferences dialog

Border color, border width, and title text can also be set without editing the
config file: right-click in a terminal and choose **MaximiseAware Preferences**.
Changes apply immediately on **Save** (the next maximise shows the new look).
```

- [ ] **Step 2: Install the updated plugin**

Run:
```bash
cp ~/git/terminator-maximise-aware/maximise_aware.py ~/.config/terminator/plugins/
```
Expected: no error.

- [ ] **Step 3: Headless smoke checks** (paste output)

```bash
cd ~/git/terminator-maximise-aware
python3 -m py_compile maximise_aware.py && echo "py_compile OK"
python3 -c "import maximise_aware; m=maximise_aware.MaximiseAware; print('caps', m.capabilities, '| has callback', hasattr(m,'callback'), '| has configure', hasattr(m,'configure'), '| has _rebuild_indicators', hasattr(m,'_rebuild_indicators'))"
python3 -m pytest tests/test_helpers.py -q
```
Expected: `py_compile OK`; caps include `terminal_menu`, all three `True`; 13 passed.

- [ ] **Step 4: Commit**

```bash
cd ~/git/terminator-maximise-aware
git add README.md
git commit -m "docs: document MaximiseAware Preferences dialog

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

- [ ] **Step 5: Manual verification (PENDING USER — requires GUI, headless agent cannot run)**

In the isolated test Terminator (controller relaunches it after install):

1. Right-click a terminal → **MaximiseAware Preferences** opens the dialog.
2. Dialog shows current values (color swatch, width spin, title text).
3. Change color + width + title → **Save** → maximise a split → new border color/width and new title text apply live, no restart.
4. Reopen dialog → shows the saved values.
5. Edit a field → **Cancel** → reopen → change was discarded.
6. Hand-edit `border_color` to garbage in the test config → reopen dialog → opens with default color, no crash (check the debug log for the `err()` line, no traceback).
7. Existing cues still work: border + title on maximise, all clear on unmaximise.

---

## Self-Review notes

- **Spec coverage:** capability + menu item (Task 2), dialog with 3 fields (Task 3), color helpers + tests (Task 1), save + live `_rebuild_indicators` (Tasks 2-3), error handling via `hex_to_rgb` fallback + `configure` try/except (Tasks 1, 3), README + manual checklist (Task 4). All spec sections mapped.
- **Type/name consistency:** `rgba_to_hex(r,g,b)`/`hex_to_rgb(hexstr, default_rgb)` defined Task 1, used identically Task 3; `_rebuild_indicators` defined Task 2, called Task 3; `callback`/`configure` signatures match Terminator's invocation and the `connect('activate', self.configure)` wiring.
- **No placeholders:** every code step is complete; every run step has exact command + expected output.
- **YAGNI:** dialog limited to the three requested cosmetics; enable-toggles and badge_format intentionally left config-file-only.
