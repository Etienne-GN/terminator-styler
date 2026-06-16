# Terminator MaximiseAware Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Terminator plugin that shows three passive cues (titlebar badge, window/tab title suffix, subtle border) whenever a terminal is maximised while siblings in the current tab are hidden, and clears them cleanly on unmaximise.

**Architecture:** A single plugin file (`maximise_aware.py`) installed to `~/.config/terminator/plugins/`. A coordinator connects (via `connect_after`) to each terminal's verified `maximise`/`zoom`/`unzoom` GObject signals, counts hidden current-tab siblings from `Window.zoom_data`, and drives three independent indicator objects. Pure logic (formatting, counting, config parsing) lives in module-level functions that are unit-tested without GTK; GTK-touching code is verified with a manual checklist.

**Tech Stack:** Python 3, PyGObject (GTK 3), Terminator 2.1.5 plugin API (`terminatorlib`). Tests run with `pytest`.

**Verified API facts (Terminator 2.1.5, `/usr/lib/python3/dist-packages/terminatorlib`):**
- `Terminal` emits `maximise`, `zoom`, `unzoom` (`terminal.py:63-65`).
- `Window.is_zoomed()` / `term_zoomed` property (`window.py:524-530`); on zoom, `Window.zoom_data` holds `old_child` (stashed subtree, maximised widget already removed), and `notebook_tabnum`/`notebook_label` when tabs exist (`window.py:543-556`).
- `Terminator` singleton: `terminals` list, `register_terminal(t)` / `deregister_terminal(t)` (`terminator.py:162-181`).
- `Factory().isinstance(obj, 'Terminal'|'Notebook'|'Window')` for type checks.
- `terminal.titlebar.label` is an `EditableLabel` with `get_text()` / `set_text(text, force=False)` (`editablelabel.py:60-68`).
- Window title: `terminal.get_toplevel()` is the `Window` (a `Gtk.Window`); use `get_title()` / `set_title()`.
- Notebook: `notebook.page_num_descendant(widget)` → tabnum; `notebook.get_nth_page(n)` → page; `notebook.get_tab_label(page)` → `TabLabel` with `get_label()` / `set_label(text)` (`notebook.py:608-612`).
- Config: `Config().plugin_get('MaximiseAware', key, default)` reads a value or returns `default`.
- `Plugin.unload()` is called on disable (`plugin.py:112,155`) — used for teardown.

**Plugin must be listed in `AVAILABLE`** (Terminator only loads plugins named there).

---

## File Structure

```
terminator-maximise-aware/
├── docs/superpowers/specs/2026-06-15-terminator-maximise-aware-design.md
├── docs/superpowers/plans/2026-06-15-terminator-maximise-aware.md
├── maximise_aware.py          # the plugin (single file; installed to ~/.config/terminator/plugins/)
├── tests/test_helpers.py      # unit tests for pure helpers
└── README.md                  # install + config notes
```

Single-file plugin because Terminator loads plugins as standalone modules; sibling imports from the plugins dir are unreliable. Pure helpers live at module top so they import cleanly into tests (importing `maximise_aware` only imports `terminatorlib`/`gi`, which work headless — no widgets are created at import time).

---

## Task 1: Pure helpers + test harness

**Files:**
- Create: `maximise_aware.py`
- Create: `tests/test_helpers.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_helpers.py`:

```python
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from maximise_aware import render_marker, collect_terminals, as_bool, build_border_css


class FakeWidget:
    def __init__(self, children=None, terminal=False):
        self._children = children or []
        self.terminal = terminal

    def get_children(self):
        return self._children


def is_term(widget):
    return getattr(widget, "terminal", False)


def test_render_marker_substitutes_n():
    assert render_marker("[⊞ {n}]", 3) == "[⊞ 3]"
    assert render_marker(" ⊞×{n}", 2) == " ⊞×2"


def test_render_marker_bad_format_falls_back():
    assert render_marker("broken {x}", 4) == " [4 hidden]"
    assert render_marker("{", 1) == " [1 hidden]"


def test_collect_terminals_counts_leaves_only():
    tree = FakeWidget(children=[
        FakeWidget(terminal=True),
        FakeWidget(children=[FakeWidget(terminal=True), FakeWidget(terminal=True)]),
    ])
    assert len(collect_terminals(tree, is_term)) == 3


def test_collect_terminals_empty_subtree():
    assert collect_terminals(FakeWidget(), is_term) == []


def test_collect_terminals_widget_is_terminal():
    assert len(collect_terminals(FakeWidget(terminal=True), is_term)) == 1


def test_as_bool():
    assert as_bool(True, False) is True
    assert as_bool(False, True) is False
    assert as_bool(None, True) is True
    assert as_bool("False", True) is False
    assert as_bool("true", False) is True
    assert as_bool("1", False) is True
    assert as_bool("0", True) is False


def test_build_border_css():
    css = build_border_css("maximise-aware-border", 1, "#5294e2")
    assert css == ".maximise-aware-border { border: 1px solid #5294e2; }"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/git/terminator-maximise-aware && python3 -m pytest tests/test_helpers.py -v`
Expected: FAIL — `ImportError: cannot import name 'render_marker' from 'maximise_aware'` (file/symbols don't exist yet).

- [ ] **Step 3: Create `maximise_aware.py` with the pure helpers**

```python
"""Terminator plugin: show passive cues when a terminal is maximised while
sibling terminals in the current tab are hidden."""

AVAILABLE = ['MaximiseAware']


def render_marker(fmt, n):
    """Format a marker string with the hidden count, falling back safely."""
    try:
        return fmt.format(n=n)
    except (KeyError, IndexError, ValueError):
        return ' [%d hidden]' % n


def collect_terminals(widget, is_terminal):
    """Recursively collect terminal-like leaves under a widget subtree."""
    if is_terminal(widget):
        return [widget]
    found = []
    get_children = getattr(widget, 'get_children', None)
    if get_children is not None:
        for child in get_children():
            found.extend(collect_terminals(child, is_terminal))
    return found


def as_bool(value, default):
    """Coerce a config value (bool or string) to a boolean."""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in ('true', '1', 'yes', 'on')


def build_border_css(css_class, width, color):
    """Build the CSS rule for the subtle border indicator."""
    return '.%s { border: %dpx solid %s; }' % (css_class, int(width), color)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/git/terminator-maximise-aware && python3 -m pytest tests/test_helpers.py -v`
Expected: PASS — 7 passed.

- [ ] **Step 5: Commit**

```bash
cd ~/git/terminator-maximise-aware
git init -q
git add maximise_aware.py tests/test_helpers.py docs/
git commit -m "feat: pure helpers for marker/count/config parsing

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: BorderIndicator

**Files:**
- Modify: `maximise_aware.py` (add imports + `BorderIndicator` class)

GTK rendering is verified manually (Task 8); only `build_border_css` (Task 1) is unit-tested.

- [ ] **Step 1: Add GTK imports at the top of `maximise_aware.py`**

Insert immediately after the module docstring, before `AVAILABLE`:

```python
import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk

from terminatorlib import plugin
from terminatorlib.config import Config
from terminatorlib.factory import Factory
from terminatorlib.terminator import Terminator
from terminatorlib.util import dbg, err
```

- [ ] **Step 2: Add the `BorderIndicator` class** (after the pure helpers)

```python
class BorderIndicator(object):
    """Draws a subtle border around the maximised terminal via per-terminal CSS."""

    CSS_CLASS = 'maximise-aware-border'

    def __init__(self, color, width):
        self.provider = Gtk.CssProvider()
        css = build_border_css(self.CSS_CLASS, width, color)
        try:
            self.provider.load_from_data(css.encode('utf-8'))
        except Exception as ex:
            err('MaximiseAware: bad border CSS %r: %s' % (css, ex))
            self.provider = None

    def show(self, terminal, count):
        if self.provider is None:
            return
        ctx = terminal.get_style_context()
        ctx.add_provider(self.provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        ctx.add_class(self.CSS_CLASS)

    def clear(self, terminal):
        if self.provider is None:
            return
        ctx = terminal.get_style_context()
        ctx.remove_class(self.CSS_CLASS)
        ctx.remove_provider(self.provider)
```

- [ ] **Step 3: Verify the module still imports and unit tests still pass**

Run: `cd ~/git/terminator-maximise-aware && python3 -c "import maximise_aware" && python3 -m pytest tests/test_helpers.py -q`
Expected: no import error; 7 passed.

- [ ] **Step 4: Commit**

```bash
cd ~/git/terminator-maximise-aware
git add maximise_aware.py
git commit -m "feat: BorderIndicator with subtle per-terminal CSS border

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: BadgeIndicator

**Files:**
- Modify: `maximise_aware.py` (add `BadgeIndicator` class)

The titlebar label is rebuilt by `Titlebar.update()`, so the badge re-applies on the terminal's `title-change` signal while active.

- [ ] **Step 1: Add the `BadgeIndicator` class** (after `BorderIndicator`)

```python
class BadgeIndicator(object):
    """Appends a badge to the maximised terminal's titlebar label."""

    def __init__(self, fmt):
        self.fmt = fmt
        self._markers = {}   # terminal -> marker string
        self._handlers = {}  # terminal -> title-change handler id

    def show(self, terminal, count):
        marker = render_marker(self.fmt, count)
        self._markers[terminal] = marker
        self._append(terminal)
        if terminal not in self._handlers:
            self._handlers[terminal] = terminal.connect_after(
                'title-change', self._on_title_change)

    def _on_title_change(self, terminal, *args):
        if terminal in self._markers:
            self._append(terminal)

    def _append(self, terminal):
        marker = self._markers.get(terminal)
        if not marker:
            return
        label = terminal.titlebar.label
        text = label.get_text()
        if not text.endswith(marker):
            label.set_text(text + marker)

    def clear(self, terminal):
        marker = self._markers.pop(terminal, None)
        handler = self._handlers.pop(terminal, None)
        if handler is not None:
            terminal.disconnect(handler)
        if marker:
            label = terminal.titlebar.label
            text = label.get_text()
            if text.endswith(marker):
                label.set_text(text[:-len(marker)])
```

- [ ] **Step 2: Verify the module still imports**

Run: `cd ~/git/terminator-maximise-aware && python3 -c "import maximise_aware"`
Expected: no output, exit 0.

- [ ] **Step 3: Commit**

```bash
cd ~/git/terminator-maximise-aware
git add maximise_aware.py
git commit -m "feat: BadgeIndicator that re-applies across titlebar updates

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: TitleIndicator

**Files:**
- Modify: `maximise_aware.py` (add `TitleIndicator` class + `find_notebook` helper)

Marks both the window title (always) and the tab title (when a tab bar exists). The window title is rebuilt by `WindowTitle.update()` on title changes, so the window marker re-applies on title changes while active. NOTE: `title-change` is a signal on `Terminal`, **not** on `Window` (`terminal.py:41`; the `Window` only connects to its terminals' `title-change` and routes it into `WindowTitle.set_title`, `window.py:418`). So the re-apply handler connects to the maximised **terminal's** `title-change` and derives the window inside the handler. Terminator's own handler runs first (normal `connect`), resetting the window title to its base; our `connect_after` handler then re-appends the marker.

- [ ] **Step 1: Add a `find_notebook` module helper** (after the pure helpers, before the indicator classes)

```python
def find_notebook(window):
    """Return the window's Notebook child, or None if there are no tabs."""
    maker = Factory()
    children = window.get_children()
    if children and maker.isinstance(children[0], 'Notebook'):
        return children[0]
    return None
```

- [ ] **Step 2: Add the `TitleIndicator` class** (after `BadgeIndicator`)

```python
class TitleIndicator(object):
    """Appends a marker to the window title (always) and tab title (if tabs)."""

    def __init__(self, fmt):
        self.fmt = fmt
        self._markers = {}        # terminal -> marker string
        self._orig_window = {}    # window -> original title
        self._orig_tab = {}       # TabLabel -> original label text
        self._handlers = {}       # terminal -> title-change handler id

    def show(self, terminal, count):
        marker = render_marker(self.fmt, count)
        self._markers[terminal] = marker
        window = terminal.get_toplevel()

        if window not in self._orig_window:
            self._orig_window[window] = window.get_title() or ''
        window.set_title(self._orig_window[window] + marker)
        if terminal not in self._handlers:
            self._handlers[terminal] = terminal.connect_after(
                'title-change', self._on_title_change)

        notebook = find_notebook(window)
        if notebook is not None:
            tabnum = notebook.page_num_descendant(terminal)
            if tabnum != -1:
                page = notebook.get_nth_page(tabnum)
                tablabel = notebook.get_tab_label(page)
                if tablabel is not None and tablabel not in self._orig_tab:
                    self._orig_tab[tablabel] = tablabel.get_label()
                    tablabel.set_label(self._orig_tab[tablabel] + marker)

    def _on_title_change(self, terminal, *args):
        marker = self._markers.get(terminal)
        if marker is None:
            return
        window = terminal.get_toplevel()
        base = window.get_title() or ''
        if not base.endswith(marker):
            self._orig_window[window] = base
            window.set_title(base + marker)

        notebook = find_notebook(window)
        if notebook is not None:
            tabnum = notebook.page_num_descendant(terminal)
            if tabnum != -1:
                page = notebook.get_nth_page(tabnum)
                tablabel = notebook.get_tab_label(page)
                if tablabel is not None:
                    base_tab = tablabel.get_label()
                    if not base_tab.endswith(marker):
                        self._orig_tab[tablabel] = base_tab
                        tablabel.set_label(base_tab + marker)

    def clear(self, terminal):
        marker = self._markers.pop(terminal, None)
        handler = self._handlers.pop(terminal, None)
        if handler is not None:
            try:
                terminal.disconnect(handler)
            except Exception:
                pass
        window = terminal.get_toplevel()
        orig = self._orig_window.pop(window, None)
        if orig is not None:
            window.set_title(orig)

        notebook = find_notebook(window)
        if notebook is not None and marker:
            tabnum = notebook.page_num_descendant(terminal)
            if tabnum != -1:
                page = notebook.get_nth_page(tabnum)
                tablabel = notebook.get_tab_label(page)
                if tablabel is not None and tablabel in self._orig_tab:
                    tablabel.set_label(self._orig_tab.pop(tablabel))
```

- [ ] **Step 3: Verify the module still imports**

Run: `cd ~/git/terminator-maximise-aware && python3 -c "import maximise_aware"`
Expected: no output, exit 0.

- [ ] **Step 4: Commit**

```bash
cd ~/git/terminator-maximise-aware
git add maximise_aware.py
git commit -m "feat: TitleIndicator for window and tab title markers

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: MaximiseAware coordinator — config, indicators, hidden-count

**Files:**
- Modify: `maximise_aware.py` (add the `MaximiseAware` plugin class)

- [ ] **Step 1: Add the `MaximiseAware` class** (after the indicator classes)

```python
class MaximiseAware(plugin.Plugin):
    """Coordinator: reads config, builds enabled indicators, and drives them
    from maximise/unmaximise events."""

    capabilities = ['maximise_aware']

    DEFAULTS = {
        'enable_badge': True,
        'enable_title': True,
        'enable_border': True,
        'badge_format': '[⊞ {n}]',
        'title_format': ' ⊞×{n}',
        'border_color': '#5294e2',
        'border_width': 1,
    }

    def __init__(self):
        plugin.Plugin.__init__(self)
        self.terminator = Terminator()
        self.maker = Factory()
        self.indicators = self._build_indicators()

    def _get(self, key):
        config = Config()
        return config.plugin_get(self.__class__.__name__, key, self.DEFAULTS[key])

    def _build_indicators(self):
        indicators = []
        if as_bool(self._get('enable_badge'), self.DEFAULTS['enable_badge']):
            indicators.append(BadgeIndicator(str(self._get('badge_format'))))
        if as_bool(self._get('enable_title'), self.DEFAULTS['enable_title']):
            indicators.append(TitleIndicator(str(self._get('title_format'))))
        if as_bool(self._get('enable_border'), self.DEFAULTS['enable_border']):
            try:
                width = int(self._get('border_width'))
            except (TypeError, ValueError):
                width = self.DEFAULTS['border_width']
            indicators.append(BorderIndicator(str(self._get('border_color')), width))
        return indicators

    def count_hidden(self, window, terminal):
        """Count hidden sibling terminals in the maximised terminal's tab."""
        zoom_data = getattr(window, 'zoom_data', None)
        if not zoom_data:
            return 0
        subtree = zoom_data.get('old_child')
        if subtree is None:
            return 0
        if 'notebook_tabnum' in zoom_data and self.maker.isinstance(subtree, 'Notebook'):
            page = subtree.get_nth_page(zoom_data['notebook_tabnum'])
            if page is not None:
                subtree = page
        is_term = lambda w: self.maker.isinstance(w, 'Terminal')
        return len(collect_terminals(subtree, is_term))
```

- [ ] **Step 2: Verify the module still imports**

Run: `cd ~/git/terminator-maximise-aware && python3 -c "import maximise_aware; print(maximise_aware.MaximiseAware.DEFAULTS['enable_badge'])"`
Expected: prints `True`.

- [ ] **Step 3: Commit**

```bash
cd ~/git/terminator-maximise-aware
git add maximise_aware.py
git commit -m "feat: MaximiseAware coordinator with config and hidden-count

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: Signal wiring — existing + future terminals

**Files:**
- Modify: `maximise_aware.py` (extend `MaximiseAware`)

Connects (via `connect_after`, so it runs after `Window` performs the zoom) to `maximise`/`zoom`/`unzoom` on every current terminal, and wraps the singleton's `register_terminal`/`deregister_terminal` to track terminals created/destroyed later.

- [ ] **Step 1: Extend `MaximiseAware.__init__`** to start tracking

Replace the body of `__init__` with:

```python
    def __init__(self):
        plugin.Plugin.__init__(self)
        self.terminator = Terminator()
        self.maker = Factory()
        self.indicators = self._build_indicators()
        self._handlers = {}  # terminal -> [handler ids]
        self._orig_register = None
        self._orig_deregister = None
        self._install()
```

- [ ] **Step 2: Add the tracking/wiring methods** (inside `MaximiseAware`, after `count_hidden`)

```python
    def _install(self):
        if getattr(self.terminator, '_maximise_aware_installed', False):
            return
        for terminal in list(self.terminator.terminals):
            self._connect_terminal(terminal)
        # Wrap registry methods so future terminals are tracked too.
        self._orig_register = self.terminator.register_terminal
        self._orig_deregister = self.terminator.deregister_terminal

        def register(terminal, _orig=self._orig_register):
            _orig(terminal)
            self._connect_terminal(terminal)

        def deregister(terminal, _orig=self._orig_deregister):
            self._disconnect_terminal(terminal)
            _orig(terminal)

        self.terminator.register_terminal = register
        self.terminator.deregister_terminal = deregister
        self.terminator._maximise_aware_installed = True

    def _connect_terminal(self, terminal):
        if terminal in self._handlers:
            return
        ids = [
            terminal.connect_after('maximise', self._on_maximise),
            terminal.connect_after('zoom', self._on_maximise),
            terminal.connect_after('unzoom', self._on_unmaximise),
        ]
        self._handlers[terminal] = ids

    def _disconnect_terminal(self, terminal):
        self._clear_all(terminal)
        ids = self._handlers.pop(terminal, [])
        for hid in ids:
            try:
                terminal.disconnect(hid)
            except Exception:
                pass

    def _on_maximise(self, terminal, *args):
        window = terminal.get_toplevel()
        count = self.count_hidden(window, terminal)
        if count <= 0:
            return
        for indicator in self.indicators:
            try:
                indicator.show(terminal, count)
            except Exception as ex:
                err('MaximiseAware: indicator.show failed: %s' % ex)

    def _on_unmaximise(self, terminal, *args):
        self._clear_all(terminal)

    def _clear_all(self, terminal):
        for indicator in self.indicators:
            try:
                indicator.clear(terminal)
            except Exception as ex:
                err('MaximiseAware: indicator.clear failed: %s' % ex)
```

- [ ] **Step 3: Verify the module still imports**

Run: `cd ~/git/terminator-maximise-aware && python3 -c "import maximise_aware"`
Expected: no output, exit 0.

- [ ] **Step 4: Commit**

```bash
cd ~/git/terminator-maximise-aware
git add maximise_aware.py
git commit -m "feat: wire maximise/zoom/unzoom signals for all terminals

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: Teardown via unload()

**Files:**
- Modify: `maximise_aware.py` (add `unload` to `MaximiseAware`)

On disable, restore the wrapped registry methods, clear every active cue, and disconnect every handler — leaving Terminator exactly as found.

- [ ] **Step 1: Add `unload`** (inside `MaximiseAware`, after `_clear_all`)

```python
    def unload(self):
        if self._orig_register is not None:
            self.terminator.register_terminal = self._orig_register
            self._orig_register = None
        if self._orig_deregister is not None:
            self.terminator.deregister_terminal = self._orig_deregister
            self._orig_deregister = None
        for terminal in list(self._handlers.keys()):
            self._disconnect_terminal(terminal)
        self.terminator._maximise_aware_installed = False
```

Paired with the `_install` sentinel guard (Task 6), this makes load/unload
idempotent: a re-instantiated plugin won't stack registry wrappers, and
disable restores the originals and clears the sentinel.

- [ ] **Step 2: Verify the module still imports and unit tests still pass**

Run: `cd ~/git/terminator-maximise-aware && python3 -c "import maximise_aware" && python3 -m pytest tests/test_helpers.py -q`
Expected: no import error; 7 passed.

- [ ] **Step 3: Commit**

```bash
cd ~/git/terminator-maximise-aware
git add maximise_aware.py
git commit -m "feat: clean teardown in unload()

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 8: Install, README, and manual verification

**Files:**
- Create: `README.md`
- Install: copy `maximise_aware.py` to `~/.config/terminator/plugins/`

- [ ] **Step 1: Write `README.md`**

```markdown
# Terminator MaximiseAware

Passive cues that other terminals are hidden while a terminal is maximised:

- a titlebar badge (`[⊞ N]`),
- a window-title and tab-title suffix (` ⊞×N`),
- a subtle 1px border around the maximised terminal.

All three count only the hidden siblings in the **current tab**, and clear on
unmaximise.

## Install

    cp maximise_aware.py ~/.config/terminator/plugins/

Then enable **MaximiseAware** in Terminator: Preferences → Plugins.

## Configuration

Optional, in `~/.config/terminator/config` under `[plugins][[MaximiseAware]]`:

    [plugins]
      [[MaximiseAware]]
        enable_badge = True
        enable_title = True
        enable_border = True
        badge_format = "[⊞ {n}]"
        title_format = " ⊞×{n}"
        border_color = "#5294e2"
        border_width = 1

`{n}` is the hidden-terminal count. Each cue can be toggled independently.

## Compatibility

Verified against Terminator 2.1.5.
```

- [ ] **Step 2: Install the plugin**

Run:
```bash
mkdir -p ~/.config/terminator/plugins
cp ~/git/terminator-maximise-aware/maximise_aware.py ~/.config/terminator/plugins/
```
Expected: file copied, no error.

- [ ] **Step 3: Enable and run the manual verification checklist**

Enable **MaximiseAware** in Terminator → Preferences → Plugins, then restart Terminator. Run a temporary instance with debug to watch for warnings: `terminator -d 2>&1 | grep -i maximise` (optional).

Verify each item (record PASS/FAIL):

1. Split into 3 panes, maximise one (right-click → Maximise) → badge `[⊞ 2]` on the titlebar, window title ends in ` ⊞×2`, subtle border around the visible terminal.
2. Unmaximise → all three cues gone; window title is byte-for-byte the original.
3. Maximise a lone terminal (no split) → no cues appear.
4. With 2+ tabs, split + maximise inside one tab → count reflects only that tab; the tab label also carries the marker.
5. Change the shell title while maximised (e.g. `printf '\033]0;hello\007'`) → badge and window marker persist (re-applied after the title update).
6. Close a terminal while maximised → cues clear, no stale border/title remains.
7. In `config`, set `enable_border = False`, restart → only the border is gone; badge and title still work. Repeat per cue.
8. Disable the plugin in Preferences → titles/labels/borders fully restored, no errors in `terminator -d`.

- [ ] **Step 4: Commit**

```bash
cd ~/git/terminator-maximise-aware
git add README.md
git commit -m "docs: README with install, config, and verification checklist

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review notes

- **Spec coverage:** detection (Task 6), current-tab count incl. notebook restriction (Task 5 `count_hidden`), badge (Task 3), window+tab title (Task 4), subtle border (Task 2), config with defaults + per-cue toggles (Task 5), teardown/unload (Task 7), pure-helper unit tests + manual checklist incl. close-while-maximised and title-change-while-maximised (Tasks 1, 8). All spec sections map to a task.
- **Type/name consistency:** indicator interface is uniform — every indicator implements `show(terminal, count)` and `clear(terminal)`; coordinator calls only those. Helper names (`render_marker`, `collect_terminals`, `as_bool`, `build_border_css`, `find_notebook`) are defined once and used consistently.
- **No placeholders:** every code step contains complete code; every run step states the exact command and expected result.
```
