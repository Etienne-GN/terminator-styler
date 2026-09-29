# Terminator Styler - unified plugin
#
# Merges four previously separate Terminator plugins into one:
#   - WindowStyler        : round window corners + internal VTE padding
#   - MaximiseIndicator   : badge / title / border cue when panes are hidden
#                           by a maximised pane (border can follow the focused
#                           profile color)
#   - ScrollbarTinter     : color the scrollbar gutter to match the focused
#                           terminal's profile background
#   - TitlebarPainter     : color the per-pane titlebar and/or the OS window
#                           CSD header bar by regex match on the window title,
#                           or follow the active profile
#   - ProfileSwitcher     : auto-switch Terminator profile based on the
#                           foreground command + argv in each terminal
#
# Configure via right-click menu: Styler -> Preferences (one dialog with one
# tab per feature). On first load, settings from the old plugins
# (TitlebarChanger, ProfileSwitcher, WindowStyler, MaximiseAware) are migrated
# automatically.

import os
import re
import fnmatch

import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, Gdk, GObject, GLib

import terminatorlib.plugin as plugin
import terminatorlib.titlebar as _titlebar_module
from terminatorlib.config import Config
from terminatorlib.factory import Factory
from terminatorlib.terminator import Terminator
from terminatorlib.translation import _
from terminatorlib.util import dbg, err

AVAILABLE = ['TerminatorStyler']

DEFAULT_PROFILE = 'default'

# Poll cadences (kept identical to the original plugins).
POLL_MS = 1000          # foreground-command + follow-profile re-evaluation
SCAN_MS = 500           # discover newly-spawned terminals

# Constants from the four original plugins.
_CORNER_RADIUS = 12

(COL_ENABLED, COL_NAME, COL_PATTERN, COL_BG, COL_FG) = range(5)
(COL_COMMAND, COL_ARGUMENT, COL_PROFILE) = (0, 1, 2)


# ─── color helpers (from TitlebarChanger) ────────────────────────────────────

def _rgba_to_hex(rgba):
    return '#%02x%02x%02x' % (
        int(rgba.red * 255), int(rgba.green * 255), int(rgba.blue * 255))


def _hex_to_rgba(hex_str):
    rgba = Gdk.RGBA()
    return rgba if (hex_str and rgba.parse(hex_str)) else None


def _darken(hex_str, factor=0.75):
    rgba = _hex_to_rgba(hex_str)
    if rgba is None:
        return hex_str
    return '#%02x%02x%02x' % (
        int(rgba.red * 255 * factor),
        int(rgba.green * 255 * factor),
        int(rgba.blue * 255 * factor))


def _hex_to_rgb_floats(hex_str, default_rgb):
    text = (hex_str or '').strip().lstrip('#')
    if len(text) != 6:
        return default_rgb
    try:
        return (int(text[0:2], 16) / 255.0,
                int(text[2:4], 16) / 255.0,
                int(text[4:6], 16) / 255.0)
    except ValueError:
        return default_rgb


def _rgb_floats_to_hex(r, g, b):
    def ch(v):
        v = max(0.0, min(1.0, v))
        return '%02x' % int(round(v * 255))
    return '#' + ch(r) + ch(g) + ch(b)


def _truthy(v):
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ('1', 'true', 'yes', 'on')


def _render_marker(fmt, n):
    try:
        return fmt.format(n=n)
    except (KeyError, IndexError, ValueError):
        return ' [%d hidden]' % n


def _collect_terminals(widget, is_terminal):
    if is_terminal(widget):
        return [widget]
    found = []
    get_children = getattr(widget, 'get_children', None)
    if get_children is not None:
        for child in get_children():
            found.extend(_collect_terminals(child, is_terminal))
    return found


def _find_notebook(window):
    maker = Factory()
    children = window.get_children()
    if children and maker.isinstance(children[0], 'Notebook'):
        return children[0]
    return None


# ─── Maximise indicators (from MaximiseAware) ────────────────────────────────

class BorderIndicator(object):
    """Subtle border around the maximised terminal via per-terminal CSS."""

    CSS_CLASS = 'styler-maximise-border'

    def __init__(self, color, width):
        self.provider = Gtk.CssProvider()
        self._active = set()
        self.color = color
        self.width = int(width)
        self._reload_css()

    def _reload_css(self):
        if self.provider is None:
            return
        css = '.%s { border: %dpx solid %s; }' % (
            self.CSS_CLASS, self.width, self.color)
        try:
            self.provider.load_from_data(css.encode('utf-8'))
        except Exception as ex:
            err('Styler: bad border CSS %r: %s' % (css, ex))
            self.provider = None

    def update_color(self, color):
        """Swap the border color in-place; affects already-active terminals."""
        if not color or color == self.color:
            return
        self.color = color
        self._reload_css()

    def show(self, terminal, count):
        if self.provider is None or terminal in self._active:
            return
        ctx = terminal.get_style_context()
        ctx.add_provider(self.provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        ctx.add_class(self.CSS_CLASS)
        self._active.add(terminal)

    def clear(self, terminal):
        if self.provider is None or terminal not in self._active:
            return
        ctx = terminal.get_style_context()
        ctx.remove_class(self.CSS_CLASS)
        ctx.remove_provider(self.provider)
        self._active.discard(terminal)


class BadgeIndicator(object):
    """Append a badge to the maximised terminal's titlebar label."""

    def __init__(self, fmt):
        self.fmt = fmt
        self._markers = {}
        self._handlers = {}

    def show(self, terminal, count):
        marker = _render_marker(self.fmt, count)
        self._markers[terminal] = marker
        self._append(terminal)
        if terminal not in self._handlers:
            self._handlers[terminal] = terminal.connect_after(
                'title-change', self._on_title_change)

    def _on_title_change(self, terminal, *_args):
        if terminal in self._markers:
            self._append(terminal)

    def _append(self, terminal):
        marker = self._markers.get(terminal)
        if not marker:
            return
        label = terminal.titlebar.label
        text = label.get_text()
        # Strip any pre-existing trailing markers, then append exactly one.
        # Terminator can re-set the label between our updates, leading to
        # repeated appends if we only check endswith().
        while text.endswith(marker):
            text = text[:-len(marker)]
        label.set_text(text + marker)

    def clear(self, terminal):
        marker = self._markers.pop(terminal, None)
        handler = self._handlers.pop(terminal, None)
        if handler is not None:
            try:
                terminal.disconnect(handler)
            except Exception:
                pass
        if marker:
            label = terminal.titlebar.label
            text = label.get_text()
            if text.endswith(marker):
                label.set_text(text[:-len(marker)])


class TitleIndicator(object):
    """Append marker to window title (and tab label, if any)."""

    def __init__(self, fmt):
        self.fmt = fmt
        self._markers = {}
        self._orig_window = {}
        self._orig_tab = {}
        self._handlers = {}

    def show(self, terminal, count):
        marker = _render_marker(self.fmt, count)
        self._markers[terminal] = marker
        window = terminal.get_toplevel()

        base = window.get_title() or ''
        while base.endswith(marker):
            base = base[:-len(marker)]
        self._orig_window[window] = base
        window.set_title(base + marker)
        if terminal not in self._handlers:
            self._handlers[terminal] = terminal.connect_after(
                'title-change', self._on_title_change)

        notebook = _find_notebook(window)
        if notebook is not None:
            tabnum = notebook.page_num_descendant(terminal)
            if tabnum != -1:
                page = notebook.get_nth_page(tabnum)
                tablabel = notebook.get_tab_label(page)
                if tablabel is not None:
                    tab_text = tablabel.get_label() or ''
                    while tab_text.endswith(marker):
                        tab_text = tab_text[:-len(marker)]
                    self._orig_tab[tablabel] = tab_text
                    tablabel.set_label(tab_text + marker)

    def _on_title_change(self, terminal, *_args):
        marker = self._markers.get(terminal)
        if marker is None:
            return
        window = terminal.get_toplevel()
        base = window.get_title() or ''
        while base.endswith(marker):
            base = base[:-len(marker)]
        self._orig_window[window] = base
        window.set_title(base + marker)

        notebook = _find_notebook(window)
        if notebook is not None:
            tabnum = notebook.page_num_descendant(terminal)
            if tabnum != -1:
                page = notebook.get_nth_page(tabnum)
                tablabel = notebook.get_tab_label(page)
                if tablabel is not None:
                    base_tab = tablabel.get_label() or ''
                    while base_tab.endswith(marker):
                        base_tab = base_tab[:-len(marker)]
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

        notebook = _find_notebook(window)
        if notebook is not None and marker:
            tabnum = notebook.page_num_descendant(terminal)
            if tabnum != -1:
                page = notebook.get_nth_page(tabnum)
                tablabel = notebook.get_tab_label(page)
                if tablabel is not None and tablabel in self._orig_tab:
                    tablabel.set_label(self._orig_tab.pop(tablabel))


# ─── main plugin ─────────────────────────────────────────────────────────────

class TerminatorStyler(plugin.MenuItem):
    """Unified styling/auto-profile plugin (see file header for features)."""

    capabilities = ['terminal_menu', 'maximise_aware']

    DEFAULTS = {
        # Master enables per feature.
        'enable_window':         True,
        'enable_maximise':       True,
        'enable_scrollbar':      True,
        'enable_titlebar':       True,
        'enable_profileswitcher': True,

        # WindowStyler.
        'ws_padding': 10,

        # MaximiseIndicator.
        'mx_enable_badge':           True,
        'mx_enable_title':           True,
        'mx_enable_border':          True,
        'mx_badge_format':           '[⊞ {n}]',
        'mx_title_format':           '   ◆ ⊞ {n} HIDDEN',
        'mx_border_color':           '#5294e2',
        'mx_border_width':           1,
        'mx_border_follow_profile':  False,

        # TitlebarPainter.
        'tb_target_titlebar':     False,
        'tb_target_window':       True,
        'tb_window_follow_focus': False,
        'tb_follow_profile':      False,
    }

    _class_serial = 0       # unique CSS class name counter
    _patched_titlebar = False
    _titlebar_instances = []
    _orig_titlebar_update = None

    def __init__(self):
        plugin.MenuItem.__init__(self)
        self.terminator = Terminator()
        self.maker = Factory()

        # Master flags.
        self.enable_window         = self.DEFAULTS['enable_window']
        self.enable_maximise       = self.DEFAULTS['enable_maximise']
        self.enable_scrollbar      = self.DEFAULTS['enable_scrollbar']
        self.enable_titlebar       = self.DEFAULTS['enable_titlebar']
        self.enable_profileswitcher = self.DEFAULTS['enable_profileswitcher']

        # WindowStyler state.
        self.ws_padding      = self.DEFAULTS['ws_padding']
        self.ws_classes      = {}   # window -> css class
        self.ws_providers    = {}   # window -> CssProvider

        # MaximiseIndicator state.
        self.mx_enable_badge          = self.DEFAULTS['mx_enable_badge']
        self.mx_enable_title          = self.DEFAULTS['mx_enable_title']
        self.mx_enable_border         = self.DEFAULTS['mx_enable_border']
        self.mx_badge_format          = self.DEFAULTS['mx_badge_format']
        self.mx_title_format          = self.DEFAULTS['mx_title_format']
        self.mx_border_color          = self.DEFAULTS['mx_border_color']
        self.mx_border_width          = self.DEFAULTS['mx_border_width']
        self.mx_border_follow_profile = self.DEFAULTS['mx_border_follow_profile']
        self.mx_indicators = []     # list of indicator objects
        self.mx_handlers   = {}     # terminal -> [handler ids]

        # ScrollbarTinter state.
        self.sb_providers = {}      # terminal -> CssProvider
        self.sb_last      = {}      # terminal -> last applied profile name

        # TitlebarPainter state.
        self.tb_rules               = []
        self.tb_target_titlebar     = self.DEFAULTS['tb_target_titlebar']
        self.tb_target_window       = self.DEFAULTS['tb_target_window']
        self.tb_window_follow_focus = self.DEFAULTS['tb_window_follow_focus']
        self.tb_follow_profile      = self.DEFAULTS['tb_follow_profile']
        self.tb_watched             = set()
        self.tb_handler_ids         = {}   # terminal -> [(obj, hid)]
        self.tb_override            = {}   # terminal -> (bg, fg) | None
        self.tb_window_class        = {}   # window -> css class
        self.tb_window_provider     = {}   # window -> CssProvider
        self.tb_class               = {}   # terminal -> css class
        self.tb_provider            = {}   # terminal -> CssProvider
        self.tb_focused_terminal    = {}   # window -> terminal

        # ProfileSwitcher state.
        self.ps_rules        = []
        self.ps_watched      = set()
        self.ps_state        = {}    # terminal -> dict
        self.ps_handler_ids  = {}    # terminal -> [(obj, hid)]
        self.ps_timer_ids    = {}    # terminal -> GLib source id

        # Shared timers.
        self.scan_timer_id = None
        self.poll_timer_id = None

        # Hooked register/deregister.
        self._orig_register   = None
        self._orig_deregister = None

        # Load config + run one-shot migration if needed.
        self._load_config()

        # Wire features.
        if self.enable_maximise:
            self.mx_indicators = self._build_mx_indicators()
        if self.enable_titlebar:
            TerminatorStyler._install_titlebar_patch(self)

        self._install_register_hook()
        # Cover terminals that already exist.
        for terminal in list(self.terminator.terminals):
            self._connect_terminal(terminal)

        # Idle sweep + periodic scan cover anything created late.
        GLib.idle_add(self._initial_sweep)
        self.scan_timer_id = GLib.timeout_add(SCAN_MS, self._scan_tick)
        self.poll_timer_id = GLib.timeout_add(POLL_MS, self._poll_tick)

        # Apply window painter to any already-shown windows.
        if self.enable_window:
            GObject.idle_add(self._ws_apply_all)

    # ── lifecycle ────────────────────────────────────────────────────────────

    def unload(self):
        # Stop timers first.
        for tid in (self.scan_timer_id, self.poll_timer_id):
            if tid is not None:
                try:
                    GLib.source_remove(tid)
                except Exception:
                    pass
        self.scan_timer_id = None
        self.poll_timer_id = None
        for tid in list(self.ps_timer_ids.values()):
            try:
                GLib.source_remove(tid)
            except Exception:
                pass
        self.ps_timer_ids.clear()

        # Disconnect every signal handler.
        for entries in list(self.tb_handler_ids.values()):
            for (obj, hid) in entries:
                try:
                    obj.disconnect(hid)
                except Exception:
                    pass
        self.tb_handler_ids.clear()
        for entries in list(self.ps_handler_ids.values()):
            for (obj, hid) in entries:
                try:
                    obj.disconnect(hid)
                except Exception:
                    pass
        self.ps_handler_ids.clear()
        for terminal in list(self.mx_handlers.keys()):
            self._mx_clear_all(terminal)
            for hid in self.mx_handlers.pop(terminal, []):
                try:
                    terminal.disconnect(hid)
                except Exception:
                    pass

        # Restore window/VTE styling.
        self._tb_clear_all_window_css()
        self._tb_clear_all_titlebar_css()
        self._ws_clear_all()
        self._sb_clear_all()

        # Restore register/deregister and the titlebar.update monkey-patch.
        self._uninstall_register_hook()
        TerminatorStyler._uninstall_titlebar_patch(self)

    # ── config (single block; one-shot migration from old blocks) ───────────

    def _load_config(self):
        cfg = Config()
        sections = cfg.plugin_get_config(self.__class__.__name__)
        if not isinstance(sections, dict) or not sections:
            sections = self._migrate_from_legacy(cfg)

        # Master flags.
        for key in ('enable_window', 'enable_maximise', 'enable_scrollbar',
                    'enable_titlebar', 'enable_profileswitcher'):
            if key in sections:
                setattr(self, key, _truthy(sections[key]))

        # WindowStyler.
        try:
            self.ws_padding = int(sections.get('ws_padding', self.ws_padding))
        except (TypeError, ValueError):
            pass

        # MaximiseIndicator.
        if 'mx_enable_badge' in sections:
            self.mx_enable_badge = _truthy(sections['mx_enable_badge'])
        if 'mx_enable_title' in sections:
            self.mx_enable_title = _truthy(sections['mx_enable_title'])
        if 'mx_enable_border' in sections:
            self.mx_enable_border = _truthy(sections['mx_enable_border'])
        self.mx_badge_format = str(sections.get('mx_badge_format',
                                                self.mx_badge_format))
        self.mx_title_format = str(sections.get('mx_title_format',
                                                self.mx_title_format))
        self.mx_border_color = str(sections.get('mx_border_color',
                                                self.mx_border_color))
        try:
            self.mx_border_width = int(sections.get('mx_border_width',
                                                   self.mx_border_width))
        except (TypeError, ValueError):
            pass
        if 'mx_border_follow_profile' in sections:
            self.mx_border_follow_profile = _truthy(
                sections['mx_border_follow_profile'])

        # TitlebarPainter flags.
        if 'tb_target_titlebar' in sections or 'tb_target_window' in sections:
            self.tb_target_titlebar = _truthy(
                sections.get('tb_target_titlebar', False))
            self.tb_target_window = _truthy(
                sections.get('tb_target_window', False))
            if not (self.tb_target_titlebar or self.tb_target_window):
                self.tb_target_window = True
        if 'tb_window_follow_focus' in sections:
            self.tb_window_follow_focus = _truthy(
                sections['tb_window_follow_focus'])
        if 'tb_follow_profile' in sections:
            self.tb_follow_profile = _truthy(sections['tb_follow_profile'])

        # Rule lists.
        self.tb_rules = self._load_rules(sections, 'tb_rule_', self._rule_tb)
        self.ps_rules = self._load_rules(sections, 'ps_rule_', self._rule_ps)

        dbg('Styler: loaded — window=%s maximise=%s scrollbar=%s '
            'titlebar=%s(rules=%d) profile_switcher=%s(rules=%d)'
            % (self.enable_window, self.enable_maximise, self.enable_scrollbar,
               self.enable_titlebar, len(self.tb_rules),
               self.enable_profileswitcher, len(self.ps_rules)))

    def _load_rules(self, sections, prefix, parse):
        ordered = []
        for key, item in sections.items():
            if not key.startswith(prefix):
                continue
            if not isinstance(item, dict):
                continue
            try:
                pos = int(item.get('position', len(ordered)))
            except (TypeError, ValueError):
                pos = len(ordered)
            parsed = parse(item)
            if parsed is not None:
                ordered.append((pos, parsed))
        ordered.sort(key=lambda x: x[0])
        return [rule for _pos, rule in ordered]

    def _rule_tb(self, item):
        return {
            'name':     item.get('name', ''),
            'pattern':  item.get('pattern', ''),
            'bg_color': item.get('bg_color', ''),
            'fg_color': item.get('fg_color', ''),
            'enabled':  bool(item.get('enabled', True)),
        }

    def _rule_ps(self, item):
        if not item.get('profile'):
            err('Styler: skipping profile rule without a profile: %r' % item)
            return None
        command = item.get('command')
        argument = item.get('argument', '')
        if command is None and 'pattern' in item:
            # Legacy schema fallback (old ProfileSwitcher).
            if item.get('type', 'host') == 'command':
                command = item['pattern']
            else:
                err('Styler: skipping legacy host rule %r '
                    '(re-add as ssh + glob argument)' % item.get('pattern'))
                return None
        if not command:
            return None
        return {'command': command,
                'argument': argument,
                'profile': item['profile']}

    def _save_config(self):
        cfg = Config()
        name = self.__class__.__name__
        cfg.plugin_del_config(name)

        flags = {
            'enable_window':         self.enable_window,
            'enable_maximise':       self.enable_maximise,
            'enable_scrollbar':      self.enable_scrollbar,
            'enable_titlebar':       self.enable_titlebar,
            'enable_profileswitcher': self.enable_profileswitcher,
            'ws_padding':            self.ws_padding,
            'mx_enable_badge':       self.mx_enable_badge,
            'mx_enable_title':       self.mx_enable_title,
            'mx_enable_border':      self.mx_enable_border,
            'mx_badge_format':       self.mx_badge_format,
            'mx_title_format':       self.mx_title_format,
            'mx_border_color':       self.mx_border_color,
            'mx_border_width':       str(self.mx_border_width),
            'mx_border_follow_profile': self.mx_border_follow_profile,
            'tb_target_titlebar':    self.tb_target_titlebar,
            'tb_target_window':      self.tb_target_window,
            'tb_window_follow_focus': self.tb_window_follow_focus,
            'tb_follow_profile':     self.tb_follow_profile,
        }
        for key, value in flags.items():
            cfg.plugin_set(name, key, value)

        for i, rule in enumerate(self.tb_rules):
            cfg.plugin_set(name, 'tb_rule_%d' % i, {
                'name':     rule['name'],
                'pattern':  rule['pattern'],
                'bg_color': rule['bg_color'],
                'fg_color': rule['fg_color'],
                'enabled':  rule['enabled'],
                'position': i,
            })
        for i, rule in enumerate(self.ps_rules):
            cfg.plugin_set(name, 'ps_rule_%d' % i, {
                'command':  rule['command'],
                'argument': rule['argument'],
                'profile':  rule['profile'],
                'position': i,
            })
        cfg.save()

    def _migrate_from_legacy(self, cfg):
        """Copy settings from the four old plugin blocks into a Styler
        section keyed with our prefixes. Runs once: returns the assembled
        dict so _load_config can read from it, and writes it back to disk so
        the migration only happens once."""
        sections = {}
        migrated = False

        tb = cfg.plugin_get_config('TitlebarChanger')
        if isinstance(tb, dict) and tb:
            migrated = True
            for k in ('target_titlebar', 'target_window',
                      'window_follow_focus', 'follow_profile'):
                if k in tb:
                    sections['tb_' + k] = tb[k]
            i = 0
            for key, item in tb.items():
                if isinstance(item, dict) and 'pattern' in item:
                    rule = dict(item)
                    rule['position'] = rule.get('position', i)
                    sections['tb_rule_%d' % i] = rule
                    i += 1

        ps = cfg.plugin_get_config('ProfileSwitcher')
        if isinstance(ps, dict) and ps:
            migrated = True
            i = 0
            for key, item in ps.items():
                if isinstance(item, dict) and 'profile' in item:
                    rule = dict(item)
                    rule['position'] = rule.get('position', i)
                    sections['ps_rule_%d' % i] = rule
                    i += 1

        ws = cfg.plugin_get_config('WindowStyler')
        if isinstance(ws, dict) and ws:
            migrated = True
            settings = ws.get('settings', {})
            if isinstance(settings, dict) and 'padding' in settings:
                sections['ws_padding'] = settings['padding']

        mx = cfg.plugin_get_config('MaximiseAware')
        if isinstance(mx, dict) and mx:
            migrated = True
            for k in ('enable_badge', 'enable_title', 'enable_border',
                      'badge_format', 'title_format',
                      'border_color', 'border_width'):
                if k in mx:
                    sections['mx_' + k] = mx[k]

        if migrated:
            dbg('Styler: migrated settings from legacy plugin blocks')
            # Persist the migrated values under our block so we don't run the
            # migration again on next start.
            name = self.__class__.__name__
            cfg.plugin_del_config(name)
            for key, value in sections.items():
                cfg.plugin_set(name, key, value)
            cfg.save()

        return sections

    # ── register_terminal hook (shared) ──────────────────────────────────────

    def _install_register_hook(self):
        if getattr(self.terminator, '_styler_installed', False):
            return
        self.terminator._styler_installed = True
        self._orig_register   = self.terminator.register_terminal
        self._orig_deregister = self.terminator.deregister_terminal

        def register(terminal, _orig=self._orig_register):
            _orig(terminal)
            def _later():
                self._connect_terminal(terminal)
                return False
            GObject.idle_add(_later)

        def deregister(terminal, _orig=self._orig_deregister):
            self._disconnect_terminal(terminal)
            _orig(terminal)

        self.terminator.register_terminal   = register
        self.terminator.deregister_terminal = deregister

    def _uninstall_register_hook(self):
        if self._orig_register is not None:
            self.terminator.register_terminal = self._orig_register
            self._orig_register = None
        if self._orig_deregister is not None:
            self.terminator.deregister_terminal = self._orig_deregister
            self._orig_deregister = None
        self.terminator._styler_installed = False

    def _connect_terminal(self, terminal):
        # WindowStyler.
        # Called from the 500 ms scan too, so only touch CSS that is not yet
        # in place; reloading a provider re-styles the whole window.
        if self.enable_window:
            self._ws_set_vte_margin(terminal, self.ws_padding)
            window = terminal.get_toplevel()
            if isinstance(window, Gtk.Window) \
                    and window not in self.ws_providers:
                self._ws_apply_window_radius(window)

        # MaximiseIndicator.
        if self.enable_maximise and terminal not in self.mx_handlers:
            ids = [
                terminal.connect_after('maximise', self._mx_on_maximise),
                terminal.connect_after('zoom',     self._mx_on_maximise),
                terminal.connect_after('unzoom',   self._mx_on_unmaximise),
            ]
            self.mx_handlers[terminal] = ids

        # TitlebarPainter.
        if self.enable_titlebar:
            self._tb_watch(terminal)

        # ProfileSwitcher + ScrollbarTinter both depend on the scrollbar
        # existing — the periodic scan picks them up when it appears.
        if (self.enable_profileswitcher or self.enable_scrollbar) \
                and getattr(terminal, 'scrollbar', None) is not None:
            if self.enable_profileswitcher and terminal not in self.ps_watched:
                self._ps_watch(terminal)
            if self.enable_scrollbar:
                profile = terminal.get_profile() or DEFAULT_PROFILE
                if self.sb_last.get(terminal) != profile:
                    self._sb_tint(terminal, profile)

    def _disconnect_terminal(self, terminal):
        # MaximiseIndicator.
        self._mx_clear_all(terminal)
        for hid in self.mx_handlers.pop(terminal, []):
            try:
                terminal.disconnect(hid)
            except Exception:
                pass

        # TitlebarPainter.
        self._tb_unwatch(terminal)

        # ProfileSwitcher.
        for (obj, hid) in self.ps_handler_ids.pop(terminal, []):
            try:
                obj.disconnect(hid)
            except Exception:
                pass
        tid = self.ps_timer_ids.pop(terminal, None)
        if tid is not None:
            try:
                GLib.source_remove(tid)
            except Exception:
                pass
        self.ps_state.pop(terminal, None)
        self.ps_watched.discard(terminal)

        # ScrollbarTinter.
        provider = self.sb_providers.pop(terminal, None)
        if provider is not None:
            sb = getattr(terminal, 'scrollbar', None)
            screen = None
            if sb is not None:
                try:
                    screen = sb.get_screen()
                except Exception:
                    screen = None
            if screen is None:
                screen = Gdk.Screen.get_default()
            try:
                Gtk.StyleContext.remove_provider_for_screen(screen, provider)
            except Exception:
                pass
        self.sb_last.pop(terminal, None)

    def _initial_sweep(self):
        for terminal in list(self.terminator.terminals):
            self._connect_terminal(terminal)
        return False

    def _scan_tick(self):
        # Catch terminals that didn't go through our register hook (initial
        # window, late scrollbar attachment).
        for terminal in list(self.terminator.terminals):
            self._connect_terminal(terminal)
        return True

    def _poll_tick(self):
        # ProfileSwitcher: re-check foreground command on every watched
        # terminal.
        if self.enable_profileswitcher:
            for terminal in list(self.ps_watched):
                self._ps_check(terminal)

        # TitlebarPainter follow-profile: re-evaluate so we pick up
        # profile changes and late-arriving profile colors.
        if self.enable_titlebar and self.tb_follow_profile:
            for terminal in list(self.tb_watched):
                old = self.tb_override.get(terminal)
                self._tb_check_and_set(
                    terminal, terminal.get_window_title() or '')
                if self.tb_override.get(terminal) != old:
                    self._tb_dispatch_update(terminal)
        return True

    # ── WindowStyler ─────────────────────────────────────────────────────────

    def _ws_window_class(self, window):
        if window not in self.ws_classes:
            TerminatorStyler._class_serial += 1
            cls = 'styler-win-%d' % TerminatorStyler._class_serial
            window.get_style_context().add_class(cls)
            self.ws_classes[window] = cls
        return self.ws_classes[window]

    def _ws_window_provider(self, window):
        if window not in self.ws_providers:
            provider = Gtk.CssProvider()
            screen = window.get_screen() or Gdk.Screen.get_default()
            Gtk.StyleContext.add_provider_for_screen(
                screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            self.ws_providers[window] = provider
        return self.ws_providers[window]

    def _ws_apply_window_radius(self, window):
        cls      = self._ws_window_class(window)
        provider = self._ws_window_provider(window)
        screen = window.get_screen()
        if screen:
            visual = screen.get_rgba_visual()
            if visual:
                window.set_visual(visual)
                window.set_app_paintable(True)
        css = ('window.{c} {{'
               '  border-radius: {r}px;'
               '  background-color: transparent;'
               '}}').format(c=cls, r=_CORNER_RADIUS)
        try:
            provider.load_from_data(css.encode())
        except Exception as exc:
            err('Styler: window CSS failed: %s' % exc)

    def _ws_set_vte_margin(self, terminal, value):
        vte = getattr(terminal, 'vte', None)
        if vte is None:
            return
        vte.set_margin_top(value)
        vte.set_margin_bottom(value)
        vte.set_margin_start(value)
        vte.set_margin_end(value)

    def _ws_clear_all(self):
        for provider in self.ws_providers.values():
            try:
                provider.load_from_data(b'')
            except Exception:
                pass
        for terminal in list(self.terminator.terminals):
            self._ws_set_vte_margin(terminal, 0)

    def _ws_apply_all(self):
        seen = set()
        for terminal in self.terminator.terminals:
            self._ws_set_vte_margin(terminal, self.ws_padding)
            window = terminal.get_toplevel()
            if isinstance(window, Gtk.Window) and window not in seen:
                seen.add(window)
                self._ws_apply_window_radius(window)
        return False

    # ── MaximiseIndicator ────────────────────────────────────────────────────

    def _build_mx_indicators(self):
        out = []
        if self.mx_enable_badge:
            out.append(BadgeIndicator(self.mx_badge_format))
        if self.mx_enable_title:
            out.append(TitleIndicator(self.mx_title_format))
        if self.mx_enable_border:
            out.append(BorderIndicator(self.mx_border_color,
                                       self.mx_border_width))
        return out

    def _mx_count_hidden(self, window, _terminal):
        zoom_data = getattr(window, 'zoom_data', None)
        if not zoom_data:
            return 0
        subtree = zoom_data.get('old_child')
        if subtree is None:
            return 0
        if ('notebook_tabnum' in zoom_data
                and self.maker.isinstance(subtree, 'Notebook')):
            page = subtree.get_nth_page(zoom_data['notebook_tabnum'])
            if page is not None:
                subtree = page
        is_term = lambda w: self.maker.isinstance(w, 'Terminal')
        return len(_collect_terminals(subtree, is_term))

    def _mx_on_maximise(self, terminal, *_args):
        if not self.enable_maximise:
            return
        window = terminal.get_toplevel()
        count = self._mx_count_hidden(window, terminal)
        if count <= 0:
            return
        if self.mx_border_follow_profile:
            self._mx_refresh_border_color(terminal)
        for indicator in self.mx_indicators:
            try:
                indicator.show(terminal, count)
            except Exception as ex:
                err('Styler: indicator.show failed: %s' % ex)

    def _mx_on_unmaximise(self, terminal, *_args):
        self._mx_clear_all(terminal)

    def _mx_clear_all(self, terminal):
        for indicator in self.mx_indicators:
            try:
                indicator.clear(terminal)
            except Exception as ex:
                err('Styler: indicator.clear failed: %s' % ex)

    def _mx_refresh_border_color(self, terminal):
        """When follow_profile is on, set the BorderIndicator's color from
        the focused terminal's profile foreground (more visible than bg)."""
        color = self._terminal_profile_color(terminal, prefer='fg') \
            or self.mx_border_color
        for indicator in self.mx_indicators:
            if isinstance(indicator, BorderIndicator):
                indicator.update_color(color)

    def _mx_rebuild_indicators(self):
        for terminal in list(self.mx_handlers.keys()):
            self._mx_clear_all(terminal)
        self.mx_indicators = self._build_mx_indicators() \
            if self.enable_maximise else []

    # ── ScrollbarTinter ──────────────────────────────────────────────────────

    def _sb_tint(self, terminal, profile):
        scrollbar = getattr(terminal, 'scrollbar', None)
        if scrollbar is None or not self.enable_scrollbar:
            return
        try:
            cfg = Config()
            cfg.set_profile(profile)
            bg = cfg['background_color']
        except Exception as ex:
            err('Styler: cannot read colors for %r: %s' % (profile, ex))
            return
        if not bg:
            return
        css_class = 'styler-sb-%d' % id(terminal)
        ctx = scrollbar.get_style_context()
        if not ctx.has_class(css_class):
            ctx.add_class(css_class)
        css = (
            'scrollbar.{cls},'
            'scrollbar.{cls} trough,'
            'scrollbar.{cls} contents {{'
            ' background-color: {bg};'
            ' background-image: none;'
            ' border-color: {bg};'
            ' box-shadow: none;'
            ' }}\n'
        ).format(cls=css_class, bg=bg)

        provider = self.sb_providers.get(terminal)
        if provider is None:
            provider = Gtk.CssProvider()
            try:
                screen = scrollbar.get_screen() or Gdk.Screen.get_default()
                Gtk.StyleContext.add_provider_for_screen(
                    screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_USER)
            except Exception as ex:
                err('Styler: sb add_provider failed: %s' % ex)
                return
            self.sb_providers[terminal] = provider
        try:
            provider.load_from_data(css.encode('utf-8'))
        except Exception as ex:
            err('Styler: sb css load failed: %s' % ex)
            return
        self.sb_last[terminal] = profile
        try:
            scrollbar.reset_style()
        except Exception:
            pass
        try:
            scrollbar.queue_resize()
            scrollbar.queue_draw()
        except Exception:
            pass

    def _sb_clear_all(self):
        for terminal, provider in list(self.sb_providers.items()):
            sb = getattr(terminal, 'scrollbar', None)
            screen = None
            if sb is not None:
                try:
                    screen = sb.get_screen()
                except Exception:
                    screen = None
            if screen is None:
                screen = Gdk.Screen.get_default()
            try:
                Gtk.StyleContext.remove_provider_for_screen(screen, provider)
            except Exception:
                pass
            if sb is not None:
                try:
                    sb.reset_style()
                    sb.queue_draw()
                except Exception:
                    pass
        self.sb_providers.clear()
        self.sb_last.clear()

    def _terminal_profile_color(self, terminal, prefer='bg'):
        try:
            cfg = terminal.config
            bg = (cfg['background_color'] or '').strip()
            fg = (cfg['foreground_color'] or '').strip()
        except Exception:
            return None
        if prefer == 'fg':
            return fg or bg or None
        return bg or fg or None

    # ── TitlebarPainter ──────────────────────────────────────────────────────

    @classmethod
    def _install_titlebar_patch(cls, instance):
        if instance not in cls._titlebar_instances:
            cls._titlebar_instances.append(instance)
        if cls._patched_titlebar:
            return
        cls._orig_titlebar_update = _titlebar_module.Titlebar.update

        def _patched_update(this_self, other=None):
            cls._orig_titlebar_update(this_self, other)
            for inst in list(cls._titlebar_instances):
                try:
                    inst._tb_post_update(this_self)
                except Exception as exc:
                    err('Styler: post-update hook failed: %s' % exc)

        _titlebar_module.Titlebar.update = _patched_update
        cls._patched_titlebar = True

    @classmethod
    def _uninstall_titlebar_patch(cls, instance):
        try:
            cls._titlebar_instances.remove(instance)
        except ValueError:
            pass
        if cls._titlebar_instances or not cls._patched_titlebar:
            return
        if cls._orig_titlebar_update is not None:
            _titlebar_module.Titlebar.update = cls._orig_titlebar_update
            cls._orig_titlebar_update = None
        cls._patched_titlebar = False

    def _tb_post_update(self, titlebar_widget):
        if not (self.enable_titlebar and self.tb_target_titlebar):
            return
        terminal = getattr(titlebar_widget, 'terminal', None)
        if terminal is None or terminal not in self.tb_watched:
            return
        override = self.tb_override.get(terminal)
        if override is None:
            return
        self._tb_apply_modify(titlebar_widget, override)

    def _tb_apply_modify(self, titlebar_widget, override):
        try:
            ebox       = getattr(titlebar_widget, 'ebox', None)
            label      = getattr(titlebar_widget, 'label', None)
            grouplabel = getattr(titlebar_widget, 'grouplabel', None)
            if override is not None:
                bg_hex, fg_hex = override
                if bg_hex:
                    color = Gdk.color_parse(bg_hex)
                    if color is not None:
                        titlebar_widget.modify_bg(Gtk.StateType.NORMAL, color)
                        if ebox is not None:
                            ebox.modify_bg(Gtk.StateType.NORMAL, color)
                if fg_hex:
                    color = Gdk.color_parse(fg_hex)
                    if color is not None:
                        if label is not None:
                            label.modify_fg(Gtk.StateType.NORMAL, color)
                        if grouplabel is not None:
                            grouplabel.modify_fg(Gtk.StateType.NORMAL, color)
            else:
                titlebar_widget.modify_bg(Gtk.StateType.NORMAL, None)
                if ebox is not None:
                    ebox.modify_bg(Gtk.StateType.NORMAL, None)
                if label is not None:
                    label.modify_fg(Gtk.StateType.NORMAL, None)
                if grouplabel is not None:
                    grouplabel.modify_fg(Gtk.StateType.NORMAL, None)
        except Exception as exc:
            err('Styler: modify_bg/fg failed: %s' % exc)

    def _tb_watch(self, terminal):
        if terminal in self.tb_watched:
            return
        hids = []
        hids.append((terminal, terminal.connect(
            'title-change', self._tb_on_title_change)))
        hids.append((terminal, terminal.connect(
            'focus-in', self._tb_on_focus_in)))
        hids.append((terminal, terminal.connect(
            'focus-out', self._tb_on_focus_out, None)))
        self.tb_handler_ids[terminal] = hids
        self.tb_watched.add(terminal)
        self.tb_override[terminal] = None
        self._tb_check_and_set(terminal, terminal.get_window_title() or '')
        self._tb_dispatch_update(terminal)

    def _tb_unwatch(self, terminal):
        for obj, hid in self.tb_handler_ids.pop(terminal, []):
            try:
                obj.disconnect(hid)
            except Exception:
                pass
        self.tb_override.pop(terminal, None)
        for win, t in list(self.tb_focused_terminal.items()):
            if t is terminal:
                self.tb_focused_terminal.pop(win, None)
        self.tb_watched.discard(terminal)

    def _tb_on_title_change(self, terminal, title):
        old = self.tb_override.get(terminal)
        self._tb_check_and_set(terminal, title or '')
        if self.tb_override.get(terminal) != old:
            self._tb_dispatch_update(terminal)
        return False

    def _tb_on_focus_in(self, terminal, *_args):
        window = terminal.get_toplevel()
        if isinstance(window, Gtk.Window):
            self.tb_focused_terminal[window] = terminal
            if self.tb_target_window:
                self._tb_update_window(window)
        return False

    def _tb_on_focus_out(self, _terminal, _event, _data):
        GObject.idle_add(self._initial_sweep)
        return False

    def _tb_check_and_set(self, terminal, title):
        match = None
        for rule in self.tb_rules:
            if not rule.get('enabled', True):
                continue
            pattern = (rule.get('pattern') or '').strip()
            if not pattern:
                continue
            try:
                if re.search(pattern, title):
                    match = (rule['bg_color'], rule['fg_color'])
                    break
            except re.error as exc:
                err('Styler: bad regex %r — %s' % (pattern, exc))
        if match is None and self.tb_follow_profile:
            color = self._terminal_profile_color(terminal, prefer='bg')
            fg    = self._terminal_profile_color(terminal, prefer='fg')
            if color or fg:
                match = (color or '', fg or '')
        self.tb_override[terminal] = match

    def _tb_dispatch_update(self, terminal):
        if self.tb_target_titlebar:
            self._tb_apply_titlebar_css(terminal,
                                        self.tb_override.get(terminal))
        if self.tb_target_window:
            window = terminal.get_toplevel()
            if isinstance(window, Gtk.Window):
                self._tb_update_window(window)

    def _tb_get_window_override(self, window):
        if self.tb_window_follow_focus:
            focused = self.tb_focused_terminal.get(window)
            if focused is None or focused not in self.tb_watched:
                return None
            return self.tb_override.get(focused)
        for terminal in self.tb_watched:
            if terminal.get_toplevel() is window:
                override = self.tb_override.get(terminal)
                if override is not None:
                    return override
        return None

    def _tb_update_window(self, window):
        self._tb_apply_window_css(window, self._tb_get_window_override(window))

    def _tb_ensure_window_class(self, window):
        if window not in self.tb_window_class:
            TerminatorStyler._class_serial += 1
            cls = 'styler-tb-win-%d' % TerminatorStyler._class_serial
            window.get_style_context().add_class(cls)
            self.tb_window_class[window] = cls
        return self.tb_window_class[window]

    def _tb_ensure_window_provider(self, window):
        if window not in self.tb_window_provider:
            provider = Gtk.CssProvider()
            screen = window.get_screen() or Gdk.Screen.get_default()
            Gtk.StyleContext.add_provider_for_screen(
                screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            self.tb_window_provider[window] = provider
        return self.tb_window_provider[window]

    def _tb_apply_window_css(self, window, override):
        cls      = self._tb_ensure_window_class(window)
        provider = self._tb_ensure_window_provider(window)
        if override:
            bg_hex, fg_hex = override
            parts = []
            if bg_hex:
                dark = _darken(bg_hex)
                parts.append(
                    'window.%(cls)s .titlebar,'
                    'window.%(cls)s headerbar {'
                    '  background-color: %(bg)s;'
                    '  background-image: none;'
                    '}' % {'cls': cls, 'bg': bg_hex})
                parts.append(
                    'window.%(cls)s:backdrop .titlebar,'
                    'window.%(cls)s:backdrop headerbar {'
                    '  background-color: %(dark)s;'
                    '  background-image: none;'
                    '}' % {'cls': cls, 'dark': dark})
            if fg_hex:
                parts.append(
                    'window.%(cls)s .titlebar *,'
                    'window.%(cls)s headerbar * {'
                    '  color: %(fg)s;'
                    '}' % {'cls': cls, 'fg': fg_hex})
                parts.append(
                    'window.%(cls)s:backdrop .titlebar *,'
                    'window.%(cls)s:backdrop headerbar * {'
                    '  color: mix(%(fg)s, #888888, 0.3);'
                    '}' % {'cls': cls, 'fg': fg_hex})
            css = '\n'.join(parts)
        else:
            css = ''
        try:
            provider.load_from_data(css.encode())
        except Exception as exc:
            err('Styler: tb window CSS load failed: %s' % exc)

    def _tb_apply_titlebar_css(self, terminal, override):
        titlebar_widget = getattr(terminal, 'titlebar', None)
        if titlebar_widget is None:
            return
        if terminal not in self.tb_class:
            TerminatorStyler._class_serial += 1
            cls = 'styler-tb-pane-%d' % TerminatorStyler._class_serial
            titlebar_widget.get_style_context().add_class(cls)
            self.tb_class[terminal] = cls
        cls = self.tb_class[terminal]
        if terminal not in self.tb_provider:
            provider = Gtk.CssProvider()
            screen = titlebar_widget.get_screen() or Gdk.Screen.get_default()
            Gtk.StyleContext.add_provider_for_screen(screen, provider, 850)
            self.tb_provider[terminal] = provider
        provider = self.tb_provider[terminal]
        if override:
            bg_hex, fg_hex = override
            parts = []
            if bg_hex:
                parts.append(
                    '.%(cls)s {'
                    '  background-color: %(bg)s;'
                    '  background-image: none;'
                    '}' % {'cls': cls, 'bg': bg_hex})
            if fg_hex:
                parts.append(
                    '.%(cls)s label {'
                    '  color: %(fg)s;'
                    '}' % {'cls': cls, 'fg': fg_hex})
            css = '\n'.join(parts)
        else:
            css = ''
        try:
            provider.load_from_data(css.encode())
        except Exception as exc:
            err('Styler: tb pane CSS load failed: %s' % exc)
        self._tb_apply_modify(titlebar_widget, override)

    def _tb_clear_all_window_css(self):
        for provider in self.tb_window_provider.values():
            try:
                provider.load_from_data(b'')
            except Exception:
                pass

    def _tb_clear_all_titlebar_css(self):
        for provider in self.tb_provider.values():
            try:
                provider.load_from_data(b'')
            except Exception:
                pass
        for terminal in list(self.tb_watched):
            tb = getattr(terminal, 'titlebar', None)
            if tb is not None:
                self._tb_apply_modify(tb, None)
        try:
            focused = self.terminator.get_focussed_terminal()
        except Exception:
            focused = None
        for terminal in list(self.tb_watched):
            tb = getattr(terminal, 'titlebar', None)
            if tb is None:
                continue
            try:
                tb.update(focused if focused is not None else 'window-focus-out')
            except Exception:
                pass

    # ── ProfileSwitcher ──────────────────────────────────────────────────────

    def _ps_ensure_state(self, terminal):
        if terminal not in self.ps_state:
            self.ps_state[terminal] = {
                'last_applied':   None,
                'last_signature': None,
            }
        return self.ps_state[terminal]

    def _ps_watch(self, terminal):
        if terminal in self.ps_watched:
            return
        try:
            hid = terminal.connect('focus-out',
                                   self._ps_on_focus_out_delayed, None)
            self.ps_handler_ids.setdefault(terminal, []).append((terminal, hid))
            self._ps_ensure_state(terminal)
            self.ps_watched.add(terminal)
        except Exception as ex:
            err('Styler: failed to wire ps terminal: %s' % ex)

    def _ps_on_focus_out_delayed(self, _terminal, _event, _arg=None):
        GObject.idle_add(self._initial_sweep)
        return False

    def _ps_foreground(self, terminal):
        try:
            pty = terminal.get_vte().get_pty()
            if pty is None:
                return (None, None)
            fd = pty.get_fd()
            pgrp = os.tcgetpgrp(fd)
        except Exception:
            return (None, None)
        if pgrp <= 0:
            return (None, None)
        if terminal.pid is not None and pgrp == terminal.pid:
            return (None, None)
        try:
            with open('/proc/%d/comm' % pgrp, 'r') as f:
                cmd = f.read().strip() or None
        except Exception:
            return (None, None)
        args = ''
        try:
            with open('/proc/%d/cmdline' % pgrp, 'rb') as f:
                raw = f.read()
            argv = [p.decode('utf-8', 'replace')
                    for p in raw.split(b'\0') if p]
            if len(argv) > 1:
                args = ' '.join(argv[1:])
        except Exception:
            pass
        return (cmd, args)

    def _ps_check(self, terminal):
        state = self._ps_ensure_state(terminal)
        cmd, args = self._ps_foreground(terminal)
        signature = (cmd, args)
        if signature != state['last_signature']:
            state['last_signature'] = signature
            target = self._ps_match(cmd, args) if cmd else None
            self._ps_apply(terminal, target)

        # Catch profile switches not driven by us, so the scrollbar
        # tinter and titlebar follow-mode stay in sync.
        if self.enable_scrollbar:
            try:
                current = terminal.get_profile()
            except Exception:
                current = None
            if current and current != self.sb_last.get(terminal):
                self._sb_tint(terminal, current)

    def _ps_match(self, cmd, args):
        cmd_lc  = (cmd  or '').lower()
        args_lc = (args or '').lower()
        for rule in self.ps_rules:
            if rule['command'].lower() != cmd_lc:
                continue
            arg_pat = (rule['argument'] or '').strip()
            if not arg_pat:
                return rule['profile']
            if fnmatch.fnmatchcase(args_lc, arg_pat.lower()):
                return rule['profile']
        return None

    def _ps_apply(self, terminal, target):
        state = self._ps_ensure_state(terminal)
        if target is None:
            if state['last_applied'] is None:
                return
            target = DEFAULT_PROFILE
        if target == state['last_applied']:
            return
        if self._ps_set_profile(terminal, target):
            state['last_applied'] = (target
                                     if target != DEFAULT_PROFILE else None)

    def _ps_set_profile(self, terminal, profile):
        try:
            available = Config().list_profiles()
        except Exception:
            available = [DEFAULT_PROFILE]
        if profile not in available:
            err('Styler: profile %r missing, falling back to %r'
                % (profile, DEFAULT_PROFILE))
            profile = DEFAULT_PROFILE
        if terminal.get_profile() == profile:
            if self.enable_scrollbar:
                self._sb_tint(terminal, profile)
            return True
        try:
            terminal.force_set_profile(None, profile)
        except Exception as ex:
            err('Styler: force_set_profile failed: %s' % ex)
            return False
        if self.enable_scrollbar:
            self._sb_tint(terminal, profile)
        return True

    # ── context menu ─────────────────────────────────────────────────────────

    def callback(self, menuitems, _menu, terminal):
        # Opportunistically catch any newly-visible window/terminals.
        for t in self.terminator.terminals:
            self._connect_terminal(t)

        item = Gtk.MenuItem.new_with_mnemonic(_('_Styler Preferences…'))
        item.connect('activate', self.configure)
        menuitems.append(item)

    # ── unified Preferences dialog ──────────────────────────────────────────

    def configure(self, widget, _data=None):
        dialog = Gtk.Dialog(
            _('Styler — Preferences'),
            None,
            Gtk.DialogFlags.MODAL,
            (_('_Cancel'), Gtk.ResponseType.REJECT,
             _('_OK'),     Gtk.ResponseType.ACCEPT))
        if widget:
            try:
                dialog.set_transient_for(widget.get_toplevel())
            except Exception:
                pass
        dialog.set_default_size(760, 540)

        notebook = Gtk.Notebook()
        commit_callbacks = []

        notebook.append_page(
            self._cfg_general_tab(commit_callbacks),
            Gtk.Label(label=_('General')))
        notebook.append_page(
            self._cfg_window_tab(commit_callbacks),
            Gtk.Label(label=_('Window')))
        notebook.append_page(
            self._cfg_maximise_tab(commit_callbacks),
            Gtk.Label(label=_('Maximise')))
        notebook.append_page(
            self._cfg_scrollbar_tab(commit_callbacks),
            Gtk.Label(label=_('Scrollbar')))
        notebook.append_page(
            self._cfg_titlebar_tab(commit_callbacks, dialog),
            Gtk.Label(label=_('Titlebar')))
        notebook.append_page(
            self._cfg_profileswitcher_tab(commit_callbacks),
            Gtk.Label(label=_('Profile Switcher')))

        dialog.vbox.pack_start(notebook, True, True, 6)
        dialog.show_all()

        try:
            if dialog.run() == Gtk.ResponseType.ACCEPT:
                self._apply_preferences(commit_callbacks)
        except Exception as ex:
            err('Styler: applying preferences failed: %s' % ex)
        finally:
            dialog.destroy()

    def _apply_preferences(self, commit_callbacks):
        old_enable_titlebar = self.enable_titlebar
        old_enable_maximise = self.enable_maximise
        old_enable_window = self.enable_window
        old_enable_scrollbar = self.enable_scrollbar
        for cb in commit_callbacks:
            cb()

        # Rewire features that flipped on/off.
        if old_enable_window and not self.enable_window:
            self._ws_clear_all()
        if old_enable_scrollbar and not self.enable_scrollbar:
            self._sb_clear_all()
        if old_enable_titlebar and not self.enable_titlebar:
            self._tb_clear_all_window_css()
            self._tb_clear_all_titlebar_css()
            for t in list(self.tb_watched):
                self._tb_unwatch(t)
            TerminatorStyler._uninstall_titlebar_patch(self)
        elif not old_enable_titlebar and self.enable_titlebar:
            TerminatorStyler._install_titlebar_patch(self)
            for t in list(self.terminator.terminals):
                self._tb_watch(t)

        if old_enable_maximise != self.enable_maximise:
            if not self.enable_maximise:
                for terminal in list(self.mx_handlers.keys()):
                    self._mx_clear_all(terminal)
                    for hid in self.mx_handlers.pop(terminal, []):
                        try:
                            terminal.disconnect(hid)
                        except Exception:
                            pass
                self.mx_indicators = []
            else:
                self.mx_indicators = self._build_mx_indicators()
                for t in list(self.terminator.terminals):
                    if t not in self.mx_handlers:
                        ids = [
                            t.connect_after('maximise',
                                            self._mx_on_maximise),
                            t.connect_after('zoom',
                                            self._mx_on_maximise),
                            t.connect_after('unzoom',
                                            self._mx_on_unmaximise),
                        ]
                        self.mx_handlers[t] = ids
        else:
            if self.enable_maximise:
                self._mx_rebuild_indicators()

        self._save_config()

        # Re-evaluate state for every terminal.
        if self.enable_titlebar:
            for terminal in self.tb_watched:
                self._tb_check_and_set(
                    terminal, terminal.get_window_title() or '')
                self._tb_dispatch_update(terminal)
        if self.enable_window:
            self._ws_apply_all()
        if self.enable_scrollbar:
            for terminal in list(self.terminator.terminals):
                try:
                    prof = terminal.get_profile() or DEFAULT_PROFILE
                except Exception:
                    prof = DEFAULT_PROFILE
                self._sb_tint(terminal, prof)

    # ── Preferences tabs ────────────────────────────────────────────────────

    def _cfg_general_tab(self, commit):
        box = Gtk.VBox(spacing=6)
        box.set_border_width(12)

        intro = Gtk.Label()
        intro.set_markup(_(
            '<small>Master switches for each feature. Each feature has its '
            'own tab with detailed options.</small>'))
        intro.set_line_wrap(True)
        intro.set_xalign(0)
        box.pack_start(intro, False, False, 0)

        toggles = (
            ('enable_window',         _('_Window styling '
                                        '(rounded corners + internal padding)')),
            ('enable_maximise',       _('_Maximise indicators '
                                        '(badge / title / border when panes hidden)')),
            ('enable_scrollbar',      _('_Scrollbar tinting '
                                        '(match scrollbar gutter to profile background)')),
            ('enable_titlebar',       _('_Titlebar painter '
                                        '(rule-based titlebar coloring)')),
            ('enable_profileswitcher', _('_Profile switcher '
                                        '(auto-switch profile based on foreground command)')),
        )
        widgets = {}
        for attr, label in toggles:
            cb = Gtk.CheckButton.new_with_mnemonic(label)
            cb.set_active(getattr(self, attr))
            box.pack_start(cb, False, False, 0)
            widgets[attr] = cb

        def _apply():
            for attr, cb in widgets.items():
                setattr(self, attr, cb.get_active())
        commit.append(_apply)
        return box

    def _cfg_window_tab(self, commit):
        box = Gtk.VBox(spacing=6)
        box.set_border_width(12)

        grid = Gtk.Grid()
        grid.set_row_spacing(10)
        grid.set_column_spacing(10)

        def lbl(text):
            l = Gtk.Label(label=text)
            l.set_halign(Gtk.Align.END)
            return l

        grid.attach(lbl(_('Internal padding (px):')), 0, 0, 1, 1)
        p_spin = Gtk.SpinButton.new_with_range(0, 60, 1)
        p_spin.set_value(self.ws_padding)
        p_spin.set_hexpand(True)
        grid.attach(p_spin, 1, 0, 1, 1)

        box.pack_start(grid, False, False, 0)

        hint = Gtk.Label()
        hint.set_markup(_(
            '<small>'
            '<b>Internal padding</b> is whitespace inside each terminal pane '
            'on all four sides.\n'
            '<b>Rounded corners</b> are applied to the window (12 px); '
            'a compositor is required for the cut corners to be transparent.'
            '</small>'))
        hint.set_line_wrap(True)
        hint.set_xalign(0)
        box.pack_start(hint, False, False, 6)

        def _apply():
            try:
                self.ws_padding = int(p_spin.get_value())
            except Exception:
                pass
        commit.append(_apply)
        return box

    def _cfg_maximise_tab(self, commit):
        box = Gtk.VBox(spacing=6)
        box.set_border_width(12)

        grid = Gtk.Grid()
        grid.set_row_spacing(8)
        grid.set_column_spacing(10)

        cb_badge  = Gtk.CheckButton.new_with_mnemonic(_('Show _badge in titlebar'))
        cb_title  = Gtk.CheckButton.new_with_mnemonic(_('Show marker in window _title'))
        cb_border = Gtk.CheckButton.new_with_mnemonic(_('Show _border around maximised pane'))
        cb_badge.set_active(self.mx_enable_badge)
        cb_title.set_active(self.mx_enable_title)
        cb_border.set_active(self.mx_enable_border)
        grid.attach(cb_badge,  0, 0, 2, 1)
        grid.attach(cb_title,  0, 1, 2, 1)
        grid.attach(cb_border, 0, 2, 2, 1)

        def lbl(text):
            l = Gtk.Label(label=text, xalign=0)
            l.set_halign(Gtk.Align.END)
            return l

        grid.attach(lbl(_('Badge format:')), 0, 3, 1, 1)
        badge_entry = Gtk.Entry()
        badge_entry.set_text(self.mx_badge_format)
        badge_entry.set_hexpand(True)
        grid.attach(badge_entry, 1, 3, 1, 1)

        grid.attach(lbl(_('Title format:')), 0, 4, 1, 1)
        title_entry = Gtk.Entry()
        title_entry.set_text(self.mx_title_format)
        title_entry.set_hexpand(True)
        grid.attach(title_entry, 1, 4, 1, 1)

        grid.attach(lbl(_('Border color:')), 0, 5, 1, 1)
        rgb = _hex_to_rgb_floats(self.mx_border_color, (0.32, 0.58, 0.886))
        rgba = Gdk.RGBA()
        rgba.red, rgba.green, rgba.blue, rgba.alpha = rgb[0], rgb[1], rgb[2], 1.0
        color_btn = Gtk.ColorButton()
        color_btn.set_rgba(rgba)
        grid.attach(color_btn, 1, 5, 1, 1)

        grid.attach(lbl(_('Border width (px):')), 0, 6, 1, 1)
        try:
            wval = int(self.mx_border_width)
        except (TypeError, ValueError):
            wval = self.DEFAULTS['mx_border_width']
        width_spin = Gtk.SpinButton()
        width_spin.set_adjustment(
            Gtk.Adjustment(value=wval, lower=0, upper=10, step_increment=1))
        width_spin.set_value(wval)
        grid.attach(width_spin, 1, 6, 1, 1)

        cb_follow = Gtk.CheckButton.new_with_mnemonic(
            _('Border follows _focused profile color '
              '(uses profile foreground; border color above is the fallback)'))
        cb_follow.set_active(self.mx_border_follow_profile)
        grid.attach(cb_follow, 0, 7, 2, 1)

        box.pack_start(grid, False, False, 0)

        hint = Gtk.Label()
        hint.set_markup(_(
            '<small>'
            '<tt>{n}</tt> in badge/title format expands to the number of '
            'hidden sibling panes.'
            '</small>'))
        hint.set_line_wrap(True)
        hint.set_xalign(0)
        box.pack_start(hint, False, False, 6)

        def _apply():
            self.mx_enable_badge          = cb_badge.get_active()
            self.mx_enable_title          = cb_title.get_active()
            self.mx_enable_border         = cb_border.get_active()
            self.mx_badge_format          = badge_entry.get_text()
            self.mx_title_format          = title_entry.get_text()
            self.mx_border_color          = _rgb_floats_to_hex(
                color_btn.get_rgba().red,
                color_btn.get_rgba().green,
                color_btn.get_rgba().blue)
            try:
                self.mx_border_width = int(width_spin.get_value())
            except Exception:
                pass
            self.mx_border_follow_profile = cb_follow.get_active()
        commit.append(_apply)
        return box

    def _cfg_scrollbar_tab(self, _commit):
        box = Gtk.VBox(spacing=6)
        box.set_border_width(12)
        hint = Gtk.Label()
        hint.set_markup(_(
            '<small>'
            'When enabled (General tab), the scrollbar gutter of every '
            'terminal is colored to match its active profile background.\n'
            'The slider itself is left to the GTK theme.'
            '</small>'))
        hint.set_line_wrap(True)
        hint.set_xalign(0)
        box.pack_start(hint, False, False, 0)
        return box

    def _cfg_titlebar_tab(self, commit, parent_dialog):
        box = Gtk.VBox(spacing=6)
        box.set_border_width(12)

        follow_cb = Gtk.CheckButton.new_with_mnemonic(_(
            'Titlebar follows active _profile '
            '(rules below take priority and override profile colors)'))
        follow_cb.set_active(self.tb_follow_profile)
        box.pack_start(follow_cb, False, False, 0)

        target_frame = Gtk.Frame(label=_(' Color targets '))
        target_box = Gtk.VBox(spacing=4)
        target_box.set_border_width(8)

        cb_titlebar = Gtk.CheckButton.new_with_mnemonic(
            _('Per-pane _titlebar  (each split reacts independently)'))
        cb_titlebar.set_active(self.tb_target_titlebar)
        target_box.pack_start(cb_titlebar, False, False, 0)

        cb_window = Gtk.CheckButton.new_with_mnemonic(
            _('OS _window title bar  (CSD header bar)'))
        cb_window.set_active(self.tb_target_window)
        target_box.pack_start(cb_window, False, False, 0)

        focus_row = Gtk.HBox()
        focus_row.pack_start(Gtk.Label(label='    '), False, False, 0)
        cb_follow_focus = Gtk.CheckButton.new_with_mnemonic(_(
            'Window follows _focused pane '
            '(otherwise: any matching pane in the window)'))
        cb_follow_focus.set_active(self.tb_window_follow_focus)
        cb_follow_focus.set_sensitive(self.tb_target_window)
        focus_row.pack_start(cb_follow_focus, False, False, 0)
        target_box.pack_start(focus_row, False, False, 0)

        cb_window.connect(
            'toggled',
            lambda w: cb_follow_focus.set_sensitive(w.get_active()))

        target_frame.add(target_box)
        box.pack_start(target_frame, False, False, 6)

        store = Gtk.ListStore(bool, str, str, str, str)
        for rule in self.tb_rules:
            store.append([rule.get('enabled', True),
                          rule.get('name', ''),
                          rule.get('pattern', ''),
                          rule.get('bg_color', ''),
                          rule.get('fg_color', '')])

        treeview = Gtk.TreeView(model=store)
        treeview.get_selection().set_mode(Gtk.SelectionMode.SINGLE)

        rend = Gtk.CellRendererToggle()
        rend.connect('toggled', self._on_tb_toggled, store)
        treeview.append_column(
            Gtk.TreeViewColumn(_('On'), rend, active=COL_ENABLED))

        rend = Gtk.CellRendererText()
        rend.set_property('editable', True)
        rend.connect('edited', self._on_tb_text_edited, store, COL_NAME)
        col = Gtk.TreeViewColumn(_('Name'), rend, text=COL_NAME)
        col.set_min_width(110)
        treeview.append_column(col)

        rend = Gtk.CellRendererText()
        rend.set_property('editable', True)
        rend.connect('edited', self._on_tb_text_edited, store, COL_PATTERN)
        col = Gtk.TreeViewColumn(_('Regex Pattern'), rend, text=COL_PATTERN)
        col.set_expand(True)
        treeview.append_column(col)

        for title, col_idx in ((_('BG Color'), COL_BG),
                               (_('FG Color'), COL_FG)):
            rend = Gtk.CellRendererText()
            rend.set_property('editable', False)
            col = Gtk.TreeViewColumn(title, rend, text=col_idx)
            col.set_cell_data_func(rend, self._render_color_cell, col_idx)
            col.set_min_width(88)
            treeview.append_column(col)

        treeview.connect('row-activated',
                         lambda tv, _p, _c: self._on_tb_edit(
                             None, tv, parent_dialog))

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.add(treeview)

        hbox = Gtk.HBox(spacing=6)
        hbox.pack_start(scroll, True, True, 0)

        btn_box = Gtk.VBox(spacing=4)
        for label, fn in (
                (_('Add'),    lambda b, tv: self._on_tb_add(b, tv, parent_dialog)),
                (_('Edit'),   lambda b, tv: self._on_tb_edit(b, tv, parent_dialog)),
                (_('Delete'), self._on_tb_delete),
                (_('Up'),     self._on_tb_up),
                (_('Down'),   self._on_tb_down)):
            btn = Gtk.Button(label=label)
            btn.connect('clicked', fn, treeview)
            btn_box.pack_start(btn, False, False, 0)
        hbox.pack_start(btn_box, False, False, 0)

        rules_frame = Gtk.Frame(label=_(' Regex rules '))
        rules_frame.add(hbox)
        box.pack_start(rules_frame, True, True, 6)

        def _apply():
            new_titlebar     = cb_titlebar.get_active()
            new_window       = cb_window.get_active()
            if not (new_titlebar or new_window):
                new_window = True
            new_follow_focus = cb_follow_focus.get_active()
            new_follow       = follow_cb.get_active()

            if self.tb_target_titlebar and not new_titlebar:
                self._tb_clear_all_titlebar_css()
            if self.tb_target_window and not new_window:
                self._tb_clear_all_window_css()

            self.tb_target_titlebar     = new_titlebar
            self.tb_target_window       = new_window
            self.tb_window_follow_focus = new_follow_focus
            self.tb_follow_profile      = new_follow

            self.tb_rules = []
            it = store.get_iter_first()
            while it is not None:
                self.tb_rules.append({
                    'enabled':  store.get_value(it, COL_ENABLED),
                    'name':     (store.get_value(it, COL_NAME) or '').strip(),
                    'pattern':  (store.get_value(it, COL_PATTERN) or '').strip(),
                    'bg_color':  store.get_value(it, COL_BG) or '',
                    'fg_color':  store.get_value(it, COL_FG) or '',
                })
                it = store.iter_next(it)
        commit.append(_apply)
        return box

    def _cfg_profileswitcher_tab(self, commit):
        box = Gtk.VBox(spacing=6)
        box.set_border_width(12)

        store = Gtk.ListStore(str, str, str)
        for rule in self.ps_rules:
            store.append([rule['command'], rule['argument'], rule['profile']])

        treeview = Gtk.TreeView(model=store)
        treeview.get_selection().set_mode(Gtk.SelectionMode.SINGLE)

        renderer_cmd = Gtk.CellRendererText()
        renderer_cmd.set_property('editable', True)
        renderer_cmd.connect('edited', self._on_ps_edited, store, COL_COMMAND)
        treeview.append_column(
            Gtk.TreeViewColumn(_('Command'), renderer_cmd, text=COL_COMMAND))

        renderer_arg = Gtk.CellRendererText()
        renderer_arg.set_property('editable', True)
        renderer_arg.connect('edited', self._on_ps_edited, store, COL_ARGUMENT)
        col_arg = Gtk.TreeViewColumn(
            _('Argument (glob, empty = any)'),
            renderer_arg, text=COL_ARGUMENT)
        col_arg.set_expand(True)
        treeview.append_column(col_arg)

        profile_store = Gtk.ListStore(str)
        for p in Config().list_profiles():
            profile_store.append([p])
        renderer_prof = Gtk.CellRendererCombo()
        renderer_prof.set_property('editable', True)
        renderer_prof.set_property('model', profile_store)
        renderer_prof.set_property('text-column', 0)
        renderer_prof.set_property('has-entry', False)
        renderer_prof.connect('edited', self._on_ps_edited, store, COL_PROFILE)
        treeview.append_column(
            Gtk.TreeViewColumn(_('Profile'), renderer_prof, text=COL_PROFILE))

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.add(treeview)

        hbox = Gtk.HBox(spacing=6)
        hbox.pack_start(scroll, True, True, 0)

        btn_box = Gtk.VBox(spacing=4)
        for label, handler in (
                (_('Add'),    self._on_ps_add),
                (_('Delete'), self._on_ps_delete),
                (_('Up'),     self._on_ps_up),
                (_('Down'),   self._on_ps_down)):
            btn = Gtk.Button(label=label)
            btn.connect('clicked', handler, treeview)
            btn_box.pack_start(btn, False, False, 0)
        hbox.pack_start(btn_box, False, False, 0)

        box.pack_start(hbox, True, True, 0)

        hint = Gtk.Label()
        hint.set_markup(_(
            '<small>'
            'First matching rule wins. No match reverts to <b>default</b>.\n'
            '<b>Command</b> matches <tt>/proc/&lt;pid&gt;/comm</tt> exactly '
            '(truncated at 15 chars).\n'
            '<b>Argument</b> is a case-insensitive glob against the joined '
            'argv (excluding the command).'
            '</small>'))
        hint.set_line_wrap(True)
        hint.set_xalign(0)
        box.pack_start(hint, False, False, 6)

        def _apply():
            self.ps_rules = []
            it = store.get_iter_first()
            while it is not None:
                cmd  = (store.get_value(it, COL_COMMAND)  or '').strip()
                arg  = (store.get_value(it, COL_ARGUMENT) or '').strip()
                prof = store.get_value(it, COL_PROFILE) or DEFAULT_PROFILE
                if cmd:
                    self.ps_rules.append({'command':  cmd,
                                          'argument': arg,
                                          'profile':  prof})
                it = store.iter_next(it)
            # Force re-evaluation on next poll.
            for s in self.ps_state.values():
                s['last_signature'] = None
        commit.append(_apply)
        return box

    # ── tree-view helpers (shared) ──────────────────────────────────────────

    def _render_color_cell(self, _col, cell, model, it, col_idx):
        hex_color = model.get_value(it, col_idx) or ''
        cell.set_property('text', hex_color or '—')
        rgba = _hex_to_rgba(hex_color) if hex_color else None
        if rgba:
            cell.set_property('background-rgba', rgba)
            lum = 0.299 * rgba.red + 0.587 * rgba.green + 0.114 * rgba.blue
            cell.set_property('foreground', '#000000' if lum > 0.5 else '#ffffff')
            cell.set_property('foreground-set', True)
            cell.set_property('background-set', True)
        else:
            cell.set_property('background-set', False)
            cell.set_property('foreground-set', False)

    def _on_tb_toggled(self, _rend, path, store):
        it = store.get_iter(path)
        store.set_value(it, COL_ENABLED, not store.get_value(it, COL_ENABLED))

    def _on_tb_text_edited(self, _rend, path, new_text, store, col):
        store[path][col] = new_text

    def _on_tb_add(self, _btn, treeview, parent):
        result = self._tb_edit_rule_dialog(None, parent)
        if result:
            treeview.get_model().append([
                result['enabled'], result['name'], result['pattern'],
                result['bg_color'], result['fg_color']])

    def _on_tb_edit(self, _btn, treeview, parent):
        store, it = treeview.get_selection().get_selected()
        if it is None:
            return
        current = {
            'enabled':  store.get_value(it, COL_ENABLED),
            'name':     store.get_value(it, COL_NAME),
            'pattern':  store.get_value(it, COL_PATTERN),
            'bg_color': store.get_value(it, COL_BG),
            'fg_color': store.get_value(it, COL_FG),
        }
        result = self._tb_edit_rule_dialog(current, parent)
        if result:
            store.set(it,
                      COL_ENABLED, result['enabled'],
                      COL_NAME,    result['name'],
                      COL_PATTERN, result['pattern'],
                      COL_BG,      result['bg_color'],
                      COL_FG,      result['fg_color'])

    def _on_tb_delete(self, _btn, treeview):
        store, it = treeview.get_selection().get_selected()
        if it is not None:
            store.remove(it)

    def _on_tb_up(self, _btn, treeview):
        store, it = treeview.get_selection().get_selected()
        if it is None:
            return
        idx = store.get_path(it).get_indices()[0]
        if idx > 0:
            store.swap(it, store.get_iter(idx - 1))

    def _on_tb_down(self, _btn, treeview):
        store, it = treeview.get_selection().get_selected()
        if it is None:
            return
        nxt = store.iter_next(it)
        if nxt is not None:
            store.swap(it, nxt)

    def _tb_edit_rule_dialog(self, current, parent):
        dialog = Gtk.Dialog(
            _('Edit Rule') if current else _('Add Rule'),
            parent,
            Gtk.DialogFlags.MODAL,
            (_('_Cancel'), Gtk.ResponseType.REJECT,
             _('_OK'),     Gtk.ResponseType.ACCEPT))
        dialog.set_default_size(440, 0)

        grid = Gtk.Grid()
        grid.set_row_spacing(10)
        grid.set_column_spacing(10)
        grid.set_border_width(14)

        def lbl(text):
            l = Gtk.Label(label=text)
            l.set_halign(Gtk.Align.END)
            return l

        row = 0
        grid.attach(lbl(_('Enabled:')), 0, row, 1, 1)
        enabled_cb = Gtk.CheckButton()
        enabled_cb.set_active((current or {}).get('enabled', True))
        grid.attach(enabled_cb, 1, row, 2, 1)
        row += 1

        grid.attach(lbl(_('Name:')), 0, row, 1, 1)
        name_entry = Gtk.Entry()
        name_entry.set_text((current or {}).get('name', ''))
        name_entry.set_hexpand(True)
        grid.attach(name_entry, 1, row, 2, 1)
        row += 1

        grid.attach(lbl(_('Regex Pattern:')), 0, row, 1, 1)
        pat_entry = Gtk.Entry()
        pat_entry.set_text((current or {}).get('pattern', ''))
        pat_entry.set_hexpand(True)
        grid.attach(pat_entry, 1, row, 2, 1)
        row += 1

        grid.attach(lbl(_('BG color:')), 0, row, 1, 1)
        bg_init  = (current or {}).get('bg_color', '')
        bg_check = Gtk.CheckButton(label=_('Custom'))
        bg_check.set_active(bool(bg_init))
        bg_btn   = Gtk.ColorButton()
        bg_rgba  = _hex_to_rgba(bg_init) if bg_init else Gdk.RGBA(0.8, 0.0, 0.0, 1.0)
        if bg_rgba:
            bg_btn.set_rgba(bg_rgba)
        bg_btn.set_sensitive(bool(bg_init))
        bg_check.connect('toggled', lambda w: bg_btn.set_sensitive(w.get_active()))
        grid.attach(bg_check, 1, row, 1, 1)
        grid.attach(bg_btn,   2, row, 1, 1)
        row += 1

        grid.attach(lbl(_('FG color:')), 0, row, 1, 1)
        fg_init  = (current or {}).get('fg_color', '')
        fg_check = Gtk.CheckButton(label=_('Custom'))
        fg_check.set_active(bool(fg_init))
        fg_btn   = Gtk.ColorButton()
        fg_rgba  = _hex_to_rgba(fg_init) if fg_init else Gdk.RGBA(1.0, 1.0, 1.0, 1.0)
        if fg_rgba:
            fg_btn.set_rgba(fg_rgba)
        fg_btn.set_sensitive(bool(fg_init))
        fg_check.connect('toggled', lambda w: fg_btn.set_sensitive(w.get_active()))
        grid.attach(fg_check, 1, row, 1, 1)
        grid.attach(fg_btn,   2, row, 1, 1)

        dialog.vbox.pack_start(grid, True, True, 0)
        dialog.show_all()

        result = None
        while True:
            if dialog.run() != Gtk.ResponseType.ACCEPT:
                break
            pattern = pat_entry.get_text().strip()
            try:
                re.compile(pattern)
            except re.error as exc:
                msg = Gtk.MessageDialog(
                    dialog, Gtk.DialogFlags.MODAL,
                    Gtk.MessageType.ERROR, Gtk.ButtonsType.CLOSE,
                    _('Invalid regular expression:\n%s') % str(exc))
                msg.run()
                msg.destroy()
                continue
            result = {
                'enabled':  enabled_cb.get_active(),
                'name':     name_entry.get_text().strip(),
                'pattern':  pattern,
                'bg_color': _rgba_to_hex(bg_btn.get_rgba()) if bg_check.get_active() else '',
                'fg_color': _rgba_to_hex(fg_btn.get_rgba()) if fg_check.get_active() else '',
            }
            break
        dialog.destroy()
        return result

    # ── profile-switcher tree-view helpers ──────────────────────────────────

    def _on_ps_edited(self, _renderer, path, new_text, store, column):
        store[path][column] = new_text

    def _on_ps_add(self, _button, treeview):
        treeview.get_model().append(['command', '', DEFAULT_PROFILE])

    def _on_ps_delete(self, _button, treeview):
        sel = treeview.get_selection()
        (store, it) = sel.get_selected()
        if it is not None:
            store.remove(it)

    def _on_ps_up(self, _button, treeview):
        sel = treeview.get_selection()
        (store, it) = sel.get_selected()
        if it is None:
            return
        idx = store.get_path(it).get_indices()[0]
        if idx == 0:
            return
        store.swap(it, store.get_iter(idx - 1))

    def _on_ps_down(self, _button, treeview):
        sel = treeview.get_selection()
        (store, it) = sel.get_selected()
        if it is None:
            return
        nxt = store.iter_next(it)
        if nxt is not None:
            store.swap(it, nxt)
