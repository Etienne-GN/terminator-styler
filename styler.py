# Terminator Styler - unified plugin
#
# Merges previously separate Terminator plugins into one:
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
#   - Degauss             : CRT degauss animation (+ optional sound) on a
#                           pane, from the context menu or the `degauss`
#                           command (degauss.py) via a Unix socket
#
# Configure via right-click menu: Styler Preferences… (one dialog with one
# tab per feature). On first load, settings from the old plugins
# (TitlebarChanger or its predecessor TitleReact, ProfileSwitcher,
# WindowStyler, MaximiseAware) are migrated automatically.

import atexit
import os
import re
import fnmatch
import math
import random
import shutil
import socket
import subprocess
import tempfile
import threading
import wave
from array import array

import cairo
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


# ── degauss shared: begin ── (identical copy in degauss.py; see tests)

DG_DEFAULTS = {
    'effect':            'wobble',
    'duration':          1.9,
    'flash':             True,
    'sound':             False,
    'volume':            30,
    'mains_hz':          60,
    'player':            'auto',
    'wobble_strength':   100,
    'strip_px':          2,
    'blotches':          False,
    'blotches_count':    3,
    'blotches_strength': 100,
    'pattern_rainbow':   100,
    'pattern_fps':       30,
}

DG_CHOICES = {
    'effect':   ('wobble', 'pattern'),
    'mains_hz': (50, 60),
    'player':   ('auto', 'pw-play', 'paplay', 'aplay'),
}

DG_RANGES = {
    'duration':          (0.5, 5.0),
    'volume':            (0, 100),
    'wobble_strength':   (0, 200),
    'strip_px':          (1, 8),
    'blotches_count':    (1, 8),
    'blotches_strength': (0, 100),
    'pattern_rainbow':   (0, 100),
    'pattern_fps':       (10, 60),
}

DG_RATE = 44100

DG_BARS_TOP = [(192, 192, 192), (192, 192, 0), (0, 192, 192), (0, 192, 0),
               (192, 0, 192), (192, 0, 0), (0, 0, 192)]
DG_BARS_MID = [(0, 0, 192), (19, 19, 19), (192, 0, 192), (19, 19, 19),
               (0, 192, 192), (19, 19, 19), (192, 192, 192)]
DG_BARS_LOW = [(0, 33, 76), (255, 255, 255), (50, 0, 106), (19, 19, 19),
               (9, 9, 9), (19, 19, 19), (29, 29, 29), (19, 19, 19)]


def dg_settings(raw):
    """Parse dg_* keys from a config block; bad or missing values fall
    back to DG_DEFAULTS, numbers are clamped to DG_RANGES."""
    out = dict(DG_DEFAULTS)
    for key, default in DG_DEFAULTS.items():
        value = raw.get('dg_' + key)
        if value is None:
            continue
        text = str(value).strip()
        if isinstance(default, bool):
            out[key] = text.lower() in ('1', 'true', 'yes', 'on')
        elif key in DG_CHOICES:
            for choice in DG_CHOICES[key]:
                if text == str(choice):
                    out[key] = choice
        else:
            try:
                num = float(text)
            except ValueError:
                continue
            if num != num:
                continue
            lo, hi = DG_RANGES[key]
            num = max(lo, min(hi, num))
            out[key] = int(round(num)) if isinstance(default, int) else num
    return out


def dg_bars():
    """SMPTE-style color bars as (x0, x1, y0, y1, rgb) in fractions of
    the pane."""
    top, mid = 0.67, 0.75
    rects = []
    for i, rgb in enumerate(DG_BARS_TOP):
        rects.append((i / 7.0, (i + 1) / 7.0, 0.0, top, rgb))
    for i, rgb in enumerate(DG_BARS_MID):
        rects.append((i / 7.0, (i + 1) / 7.0, top, mid, rgb))
    for i, rgb in enumerate(DG_BARS_LOW[:4]):
        rects.append((i * 5 / 28.0, (i + 1) * 5 / 28.0, mid, 1.0, rgb))
    for i, rgb in enumerate(DG_BARS_LOW[4:]):
        x0 = 5 / 7.0 + i * 2 / 28.0
        rects.append((x0, x0 + 2 / 28.0, mid, 1.0, rgb))
    return rects


def dg_hue(h):
    h = (h % 1.0) * 6
    c = int(h)
    f = h - c
    return [(1, f, 0), (1 - f, 1, 0), (0, 1, f),
            (0, 1 - f, 1), (f, 0, 1), (1, 0, 1 - f)][c % 6]


def dg_sound_seconds(s):
    return s['duration'] + 0.3


def dg_wav_path(s):
    cache = os.environ.get('XDG_CACHE_HOME') or os.path.expanduser('~/.cache')
    return os.path.join(cache, 'degauss', 'degauss-v2-%.2fs-%dhz-vol%03d.wav'
                        % (dg_sound_seconds(s), s['mains_hz'], s['volume']))


def dg_synth(path, seconds, mains_hz, volume):
    """Write the thunk + mains hum as a mono 16-bit WAV at path."""
    rng = random.Random(1995)
    n = int(DG_RATE * seconds)
    samples = []
    peak = 0.0
    for i in range(n):
        t = i / DG_RATE
        thunk = math.exp(-t / 0.045) * (math.sin(2 * math.pi * 45 * t)
                                        + 0.6 * (rng.random() * 2 - 1))
        ring = (0.35 * math.exp(-t / 0.25) * math.sin(2 * math.pi * 310 * t)
                * math.sin(2 * math.pi * 3 * t))
        attack = min(1.0, t / 0.02)
        hum_env = attack * math.exp(-t / 0.55)
        base = math.sin(2 * math.pi * mains_hz * t)
        buzz = (math.tanh(4.0 * base) * 0.6
                + 0.4 * math.sin(2 * math.pi * 2 * mains_hz * t))
        chatter = (0.25 * math.tanh(8.0 * math.sin(2 * math.pi * 2 * mains_hz * t))
                   * (rng.random() * 0.5 + 0.5))
        s = thunk + ring + hum_env * (buzz + chatter)
        samples.append(s)
        peak = max(peak, abs(s))
    fade = int(DG_RATE * 0.05)
    out = array('h')
    for i, s in enumerate(samples):
        if i > n - fade:
            s *= (n - i) / fade
        out.append(int(s / peak * volume * 32767))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix='.part')
    try:
        with os.fdopen(fd, 'wb') as f, wave.open(f, 'wb') as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(DG_RATE)
            w.writeframes(out.tobytes())
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def dg_ensure_wav(s):
    path = dg_wav_path(s)
    if not os.path.exists(path):
        dg_synth(path, dg_sound_seconds(s), s['mains_hz'], s['volume'] / 100.0)
    return path


def dg_player(name):
    if name == 'auto':
        for candidate in ('pw-play', 'paplay', 'aplay'):
            found = shutil.which(candidate)
            if found:
                return found
        return None
    return shutil.which(name)

# ── degauss shared: end ──


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


def _truthy(v, default=False):
    if v is None:
        return default
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
        # A manually renamed titlebar (EditableLabel._custom) ignores set_text,
        # so the badge is skipped there; the title and border cues still show.
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


# ─── Degauss (from the standalone degauss plugin) ────────────────────────────

DG_SOCKET = 'terminator-styler-degauss-%d.sock'
DG_SWIRL_COLS = 48
DG_BLOTCH_SCALE = 8


def _dg_socket_path():
    rundir = os.environ.get('XDG_RUNTIME_DIR')
    if not rundir:
        return None
    return os.path.join(rundir, DG_SOCKET % os.getpid())


def _dg_surface(widget):
    alloc = widget.get_allocation()
    if alloc.width <= 1 or alloc.height <= 1 or not widget.get_mapped():
        return None
    scale = widget.get_scale_factor()
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32,
                                 alloc.width * scale, alloc.height * scale)
    surface.set_device_scale(scale, scale)
    return surface, alloc.width, alloc.height


def _dg_snapshot(widget):
    made = _dg_surface(widget)
    if made is None:
        return None
    surface = made[0]
    widget.draw(cairo.Context(surface))
    return surface


def _dg_bars_surface(widget):
    made = _dg_surface(widget)
    if made is None:
        return None
    surface, w, h = made
    cr = cairo.Context(surface)
    for x0, x1, y0, y1, (r, g, b) in dg_bars():
        cr.set_source_rgb(r / 255.0, g / 255.0, b / 255.0)
        cr.rectangle(math.floor(x0 * w), math.floor(y0 * h),
                     math.ceil((x1 - x0) * w) + 1, math.ceil((y1 - y0) * h) + 1)
        cr.fill()
    return surface


def _dg_swirl_surface(w, h, t, decay, amount):
    """Rainbow swirl of the terminal Test pattern, rendered on a coarse
    grid and scaled up by the caller (per-pixel hue in Python is too slow
    for a full pane)."""
    cols = DG_SWIRL_COLS
    rows = max(2, int(round(cols * h / float(w))))
    stride = cairo.ImageSurface.format_stride_for_width(cairo.FORMAT_ARGB32,
                                                        cols)
    data = bytearray(stride * rows)
    cx, cy = cols / 2.0, rows / 2.0
    spin = t * 7.0
    for y in range(rows):
        ey = (y + 0.5 - cy) / cy * 0.5
        for x in range(cols):
            ex = (x + 0.5 - cx) / cx
            dist = math.hypot(ex, ey)
            alpha = decay * amount * (0.35 + 0.65 * min(1.0, dist))
            if alpha <= 0.0:
                continue
            r, g, b = dg_hue(math.atan2(ey, ex) / (2 * math.pi)
                             + dist * 0.7 - spin)
            a = min(1.0, alpha)
            i = y * stride + x * 4
            # ARGB32 is native-endian premultiplied: BGRA on little-endian.
            data[i]     = int(b * a * 255)
            data[i + 1] = int(g * a * 255)
            data[i + 2] = int(r * a * 255)
            data[i + 3] = int(a * 255)
    return cairo.ImageSurface.create_for_data(
        data, cairo.FORMAT_ARGB32, cols, rows, stride), cols, rows


def _dg_paint(cr, source, w, h, t, s, mode, rng):
    """One frame: source (pane snapshot or color bars) shaken in strips,
    then the mode's color overlay and the initial flash."""
    pattern = mode == 'pattern'
    decay = math.exp(-t / 0.42)
    strength = s['wobble_strength'] / 100.0
    strip = s['strip_px']

    cr.set_operator(cairo.OPERATOR_SOURCE)
    cr.set_source_rgb(0, 0, 0)
    cr.paint()
    cr.set_operator(cairo.OPERATOR_OVER)

    cr.save()
    breathe = 1 + 0.04 * decay * strength * math.sin(2 * math.pi * 4 * t)
    cr.translate(w / 2, h / 2)
    cr.scale(breathe, breathe)
    cr.translate(-w / 2, -h / 2)
    dy = (h * (0.04 if pattern else 0.03) * decay * strength
          * math.sin(2 * math.pi * 9 * t))
    amp = w * (0.12 if pattern else 0.06) * decay * strength
    y = 0
    while y < h:
        dx = (amp * math.sin(2 * math.pi * 6 * t + y * 0.02)
              + amp * 0.25 * math.sin(2 * math.pi * 23 * t + y * 0.11))
        if pattern:
            dx += (rng.random() - 0.5) * amp * 0.3
        cr.set_source_surface(source, dx, dy)
        cr.rectangle(0, y, w, strip)
        cr.fill()
        y += strip
    cr.restore()

    if pattern and s['pattern_rainbow'] > 0 and decay > 0.02:
        swirl, cols, rows = _dg_swirl_surface(
            w, h, t, decay, s['pattern_rainbow'] / 100.0)
        cr.save()
        cr.scale(w / float(cols), h / float(rows))
        cr.set_source_surface(swirl, 0, 0)
        cr.get_source().set_filter(cairo.FILTER_BILINEAR)
        cr.paint()
        cr.restore()

    if not pattern and s['blotches'] and decay > 0.02:
        # The standalone plugin blended each blotch with HSL_COLOR at full
        # resolution, too slow for a large pane at frame rate. A coarse
        # layer blended with OVERLAY looks the same: dark stays dark.
        count = s['blotches_count']
        amount = decay * s['blotches_strength'] / 100.0
        k = DG_BLOTCH_SCALE
        lw, lh = max(1, int(w / k)), max(1, int(h / k))
        layer = cairo.ImageSurface(cairo.FORMAT_ARGB32, lw, lh)
        lcr = cairo.Context(layer)
        radius = max(lw, lh) * 0.45
        for i in range(count):
            a = i * 2 * math.pi / count + t * 3.5
            bx = lw / 2 + math.cos(a) * lw * 0.28
            by = lh / 2 + math.sin(a) * lh * 0.28
            r, g, b = dg_hue(i / float(count) + t * 0.8)
            grad = cairo.RadialGradient(bx, by, 0, bx, by, radius)
            grad.add_color_stop_rgba(0, r, g, b, 1)
            grad.add_color_stop_rgba(1, r, g, b, 0)
            lcr.set_source(grad)
            lcr.paint()
        cr.save()
        cr.scale(w / float(lw), h / float(lh))
        for op, alpha in ((cairo.OPERATOR_OVERLAY, amount),
                          (cairo.OPERATOR_ADD, amount * 0.12)):
            cr.set_operator(op)
            cr.set_source_surface(layer, 0, 0)
            cr.get_source().set_filter(cairo.FILTER_BILINEAR)
            cr.paint_with_alpha(alpha)
        cr.restore()

    if s['flash']:
        flash = 0.55 * math.exp(-t / 0.07)
        if flash > 0.01:
            cr.set_operator(cairo.OPERATOR_ADD)
            cr.set_source_rgba(1, 1, 1, flash)
            cr.paint()
    cr.set_operator(cairo.OPERATOR_OVER)


class DegaussEffect(object):
    """Runs one degauss animation on a widget by overriding its draw."""

    def __init__(self, widget, source, settings, on_done):
        self.widget = widget
        self.source = source
        self.s = settings
        self.mode = settings['effect']
        self.on_done = on_done
        self.rng = random.Random()
        self.start = None
        self.t = 0.0
        self.draw_id = widget.connect('draw', self._on_draw)
        self.unmap_id = widget.connect('unmap', lambda _w: self.stop())
        self.tick_id = widget.add_tick_callback(self._on_tick)
        widget.queue_draw()

    def _on_tick(self, widget, clock):
        now = clock.get_frame_time() / 1e6
        if self.start is None:
            self.start = now
        self.t = now - self.start
        if self.t >= self.s['duration']:
            self.tick_id = None
            self.stop()
            return GLib.SOURCE_REMOVE
        widget.queue_draw()
        return GLib.SOURCE_CONTINUE

    def _on_draw(self, widget, cr):
        alloc = widget.get_allocation()
        _dg_paint(cr, self.source, alloc.width, alloc.height, self.t,
                  self.s, self.mode, self.rng)
        return True

    def stop(self):
        if self.draw_id is None:
            return
        for hid in (self.draw_id, self.unmap_id):
            try:
                self.widget.disconnect(hid)
            except Exception:
                pass
        self.draw_id = self.unmap_id = None
        if self.tick_id is not None:
            try:
                self.widget.remove_tick_callback(self.tick_id)
            except Exception:
                pass
            self.tick_id = None
        self.widget.queue_draw()
        self.on_done()


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
        'enable_degauss':        True,

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
        self.enable_degauss        = self.DEFAULTS['enable_degauss']

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

        # Degauss state.
        self.dg               = dict(DG_DEFAULTS)
        self.dg_sock          = None
        self.dg_path          = None
        self.dg_watch         = None
        self.dg_clients       = {}   # socket -> [request bytes, io watch, timeout]
        self.dg_running       = {}   # terminal -> DegaussEffect
        self.dg_sound_lock    = threading.Lock()
        self.dg_menu_terminal = None

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
        if self.enable_degauss:
            self._dg_listen()
            self._dg_prepare_sound()
        # Terminator does not unload plugins on quit; remove the socket.
        atexit.register(self._dg_unlisten)

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

        self._dg_stop_all()
        self._dg_unlisten()

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
                    'enable_titlebar', 'enable_profileswitcher',
                    'enable_degauss'):
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

        self.dg = dg_settings(sections)

        dbg('Styler: loaded — window=%s maximise=%s scrollbar=%s '
            'titlebar=%s(rules=%d) profile_switcher=%s(rules=%d) '
            'degauss=%s(%s)'
            % (self.enable_window, self.enable_maximise, self.enable_scrollbar,
               self.enable_titlebar, len(self.tb_rules),
               self.enable_profileswitcher, len(self.ps_rules),
               self.enable_degauss, self.dg['effect']))

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
            'enable_degauss':        self.enable_degauss,
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
        for key, value in self.dg.items():
            cfg.plugin_set(name, 'dg_' + key, value)

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
        if not (isinstance(tb, dict) and tb):
            # TitlebarChanger's own predecessor.
            tb = cfg.plugin_get_config('TitleReact')
        if isinstance(tb, dict) and tb:
            migrated = True
            for k in ('target_titlebar', 'target_window',
                      'window_follow_focus', 'follow_profile'):
                if k in tb:
                    sections['tb_' + k] = tb[k]
            if ('target_titlebar' not in tb and 'target_window' not in tb
                    and 'target' in tb):
                # Single-target schema: target = titlebar | window.
                on_titlebar = str(tb['target']) == 'titlebar'
                sections['tb_target_titlebar'] = on_titlebar
                sections['tb_target_window'] = not on_titlebar
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
            dbg('Styler: sb tint skipped, profile %r has no background_color'
                % profile)
            return
        dbg('Styler: tinting scrollbar profile=%s bg=%s' % (profile, bg))
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
            match = self._tb_profile_override(terminal)
        self.tb_override[terminal] = match

    def _tb_profile_override(self, terminal):
        # Each color is taken as-is: filling a missing bg from fg (or the
        # reverse) would paint the label in its own background color.
        try:
            cfg = terminal.config
            bg = (cfg['background_color'] or '').strip()
            fg = (cfg['foreground_color'] or '').strip()
        except Exception:
            return None
        if not bg and not fg:
            return None
        return (bg, fg)

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
        dbg('Styler: switching terminal to profile %s' % profile)
        try:
            terminal.force_set_profile(None, profile)
        except Exception as ex:
            err('Styler: force_set_profile failed: %s' % ex)
            return False
        if self.enable_scrollbar:
            self._sb_tint(terminal, profile)
        return True

    # ── Degauss ──────────────────────────────────────────────────────────────

    def _dg_listen(self):
        if self.dg_sock is not None:
            return
        path = _dg_socket_path()
        if path is None:
            err('Styler: XDG_RUNTIME_DIR unset, degauss command disabled')
            return
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            if os.path.exists(path):
                os.unlink(path)
            sock.bind(path)
            os.chmod(path, 0o600)
            sock.listen(4)
            sock.setblocking(False)
        except OSError as ex:
            err('Styler: degauss socket %s failed: %s' % (path, ex))
            sock.close()
            return
        self.dg_sock = sock
        self.dg_path = path
        self.dg_watch = GLib.io_add_watch(sock.fileno(), GLib.PRIORITY_DEFAULT,
                                          GLib.IO_IN, self._dg_on_accept)
        dbg('Styler: degauss listening on %s' % path)

    def _dg_unlisten(self):
        for conn in list(self.dg_clients):
            self._dg_drop(conn)
        if self.dg_watch is not None:
            GLib.source_remove(self.dg_watch)
            self.dg_watch = None
        if self.dg_sock is not None:
            self.dg_sock.close()
            self.dg_sock = None
            try:
                os.unlink(self.dg_path)
            except OSError:
                pass

    def _dg_on_accept(self, _fd, _cond):
        if self.dg_sock is None:
            return False
        try:
            conn, _addr = self.dg_sock.accept()
        except OSError:
            return True
        conn.setblocking(False)
        watch = GLib.io_add_watch(conn.fileno(), GLib.PRIORITY_DEFAULT,
                                  GLib.IO_IN | GLib.IO_HUP | GLib.IO_ERR,
                                  self._dg_on_readable, conn)
        timer = GLib.timeout_add_seconds(2, self._dg_on_client_timeout, conn)
        self.dg_clients[conn] = [b'', watch, timer]
        return True

    def _dg_on_readable(self, _fd, _cond, conn):
        entry = self.dg_clients.get(conn)
        if entry is None:
            return False
        try:
            data = conn.recv(256)
        except BlockingIOError:
            return True
        except OSError as ex:
            dbg('Styler: degauss client read failed: %s' % ex)
            data = b''
        if not data:
            entry[1] = None
            self._dg_drop(conn)
            return False
        entry[0] += data
        if b'\n' not in entry[0] and len(entry[0]) < 256:
            return True
        GLib.source_remove(entry[2])
        entry[1] = entry[2] = None
        self._dg_request(conn, entry[0].decode('ascii', 'replace').strip())
        return False

    def _dg_on_client_timeout(self, conn):
        entry = self.dg_clients.get(conn)
        if entry is not None:
            entry[2] = None
            self._dg_drop(conn)
        return False

    def _dg_drop(self, conn):
        entry = self.dg_clients.pop(conn, None)
        if entry is not None:
            for source in entry[1:]:
                if source is not None:
                    GLib.source_remove(source)
        conn.close()

    def _dg_reply(self, conn, answer):
        if conn not in self.dg_clients:
            return
        try:
            conn.settimeout(1.0)
            conn.sendall(answer.encode('ascii') + b'\n')
        except OSError as ex:
            dbg('Styler: degauss client left before %r: %s' % (answer, ex))
        self._dg_drop(conn)

    def _dg_request(self, conn, uuid):
        terminal = next((t for t in self.terminator.terminals
                         if t.uuid.urn == uuid), None)
        if terminal is None:
            self._dg_reply(conn, 'unknown')
            return
        result = self._dg_start(terminal, dict(self.dg),
                                lambda: self._dg_reply(conn, 'done'))
        if result != 'started':
            self._dg_reply(conn, result)

    def _dg_start(self, terminal, settings, on_done=None):
        """Degauss one pane. Returns 'started', 'busy' or 'unavailable'."""
        if terminal in self.dg_running:
            return 'busy'
        widget = getattr(terminal, 'vte', None)
        if widget is None:
            return 'unavailable'
        if settings['effect'] == 'pattern':
            source = _dg_bars_surface(widget)
        else:
            source = _dg_snapshot(widget)
        if source is None:
            return 'unavailable'

        def finished():
            self.dg_running.pop(terminal, None)
            if on_done is not None:
                on_done()

        self.dg_running[terminal] = DegaussEffect(widget, source, settings,
                                                  finished)
        if settings['sound'] and settings['volume'] > 0:
            threading.Thread(target=self._dg_sound_worker,
                             args=(dict(settings), True), daemon=True).start()
        return 'started'

    def _dg_stop_all(self):
        for effect in list(self.dg_running.values()):
            effect.stop()

    def _dg_prepare_sound(self):
        if self.dg['sound'] and self.dg['volume'] > 0:
            threading.Thread(target=self._dg_sound_worker,
                             args=(dict(self.dg), False), daemon=True).start()

    def _dg_sound_worker(self, settings, play):
        # Worker thread: never touches GTK.
        try:
            with self.dg_sound_lock:
                path = dg_ensure_wav(settings)
        except Exception as ex:
            err('Styler: degauss sound synthesis failed: %s' % ex)
            return
        if not play:
            return
        player = dg_player(settings['player'])
        if player is None:
            dbg('Styler: no audio player found (%s)' % settings['player'])
            return
        try:
            proc = subprocess.Popen([player, path], stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
        except OSError as ex:
            err('Styler: cannot run %s: %s' % (player, ex))
            return
        proc.wait()

    # ── context menu ─────────────────────────────────────────────────────────

    def callback(self, menuitems, _menu, terminal):
        # Opportunistically catch any newly-visible window/terminals.
        for t in self.terminator.terminals:
            self._connect_terminal(t)

        self.dg_menu_terminal = terminal
        if self.enable_degauss:
            item = Gtk.MenuItem.new_with_mnemonic(_('_Degauss this pane'))
            item.connect('activate',
                         lambda _i: self._dg_start(terminal, dict(self.dg)))
            menuitems.append(item)

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
        notebook.append_page(
            self._cfg_degauss_tab(commit_callbacks),
            Gtk.Label(label=_('Degauss')))

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
        old_enable_degauss = self.enable_degauss
        for cb in commit_callbacks:
            cb()

        if old_enable_degauss and not self.enable_degauss:
            self._dg_stop_all()
            self._dg_unlisten()
        elif self.enable_degauss:
            self._dg_listen()
            self._dg_prepare_sound()

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
            ('enable_degauss',        _('_Degauss '
                                        '(CRT degauss effect from the menu or the degauss command)')),
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
        p_spin.set_tooltip_text(
            _('Whitespace added inside every terminal pane on all four sides.'))
        grid.attach(p_spin, 1, 0, 1, 1)

        box.pack_start(grid, False, False, 0)

        hint = Gtk.Label()
        hint.set_markup(_(
            '<small>'
            '<b>Internal padding</b> is whitespace inside each terminal pane '
            'on all four sides, applied immediately to every pane including '
            'ones opened later.\n'
            '<b>Rounded corners</b> are applied to the window (12 px); '
            'a compositor is required for the cut corners to be transparent. '
            'Some compositors draw their own rounding and override this.'
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

        hint = Gtk.Label()
        hint.set_markup(_(
            '<small>'
            'Regex matched against the VTE window title (set by your shell '
            'prompt). First matching rule wins.\n'
            'Rules always have priority — when <b>follows active profile</b> '
            'is on, the profile color is used only when no rule matches.\n'
            '<b>Per-pane titlebar</b> and <b>window title bar</b> targets are '
            'independent and can be enabled together. The window target '
            'needs client-side decorations (CSD).'
            '</small>'))
        hint.set_line_wrap(True)
        hint.set_xalign(0)
        box.pack_start(hint, False, False, 4)

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
            'argv (excluding the command). Empty matches any invocation.\n'
            '<b>Profile</b> must exist in Preferences → Profiles.\n'
            'Examples: <tt>ssh</tt> + <tt>*staging*</tt>, '
            '<tt>ssh</tt> + <tt>*prod*</tt>, '
            '<tt>python3</tt> + <i>(empty)</i>.'
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

    def _cfg_degauss_tab(self, commit):
        box = Gtk.VBox(spacing=6)
        box.set_border_width(12)
        s = self.dg

        def lbl(text):
            l = Gtk.Label(label=text)
            l.set_halign(Gtk.Align.END)
            return l

        def combo(choices, active):
            c = Gtk.ComboBoxText()
            for cid, text in choices:
                c.append(cid, text)
            c.set_active_id(str(active))
            return c

        def scale(lo, hi, value):
            sc = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, lo, hi, 1)
            sc.set_value(value)
            sc.set_value_pos(Gtk.PositionType.RIGHT)
            sc.set_digits(0)
            sc.set_hexpand(True)
            return sc

        def spin(lo, hi, step, value, digits=0):
            sp = Gtk.SpinButton.new_with_range(lo, hi, step)
            sp.set_digits(digits)
            sp.set_value(value)
            return sp

        def framed(title, rows):
            frame = Gtk.Frame(label=' %s ' % title)
            grid = Gtk.Grid()
            grid.set_row_spacing(6)
            grid.set_column_spacing(10)
            grid.set_border_width(8)
            for i, (label, widget) in enumerate(rows):
                if label is None:
                    grid.attach(widget, 0, i, 2, 1)
                else:
                    grid.attach(lbl(label), 0, i, 1, 1)
                    grid.attach(widget, 1, i, 1, 1)
            frame.add(grid)
            box.pack_start(frame, False, False, 0)
            return frame

        effect = combo((('wobble', _('Wobble (shake a snapshot of the pane)')),
                        ('pattern', _('Test pattern (shake color bars)'))),
                       s['effect'])
        duration = spin(DG_RANGES['duration'][0], DG_RANGES['duration'][1],
                        0.1, s['duration'], digits=1)
        flash = Gtk.CheckButton.new_with_mnemonic(_('Initial white _flash'))
        flash.set_active(s['flash'])
        framed(_('Effect'), ((_('Effect:'), effect),
                             (_('Duration (s):'), duration),
                             (None, flash)))

        sound = Gtk.CheckButton.new_with_mnemonic(
            _('Play _sound (thunk + mains hum)'))
        sound.set_active(s['sound'])
        volume = scale(0, 100, s['volume'])
        mains = combo((('50', _('50 Hz')), ('60', _('60 Hz'))), s['mains_hz'])
        player = combo([(p, p) for p in DG_CHOICES['player']], s['player'])
        framed(_('Sound'), ((None, sound),
                            (_('Volume (%):'), volume),
                            (_('Mains hum:'), mains),
                            (_('Player:'), player)))

        strength = scale(DG_RANGES['wobble_strength'][0],
                         DG_RANGES['wobble_strength'][1], s['wobble_strength'])
        strip = spin(DG_RANGES['strip_px'][0], DG_RANGES['strip_px'][1], 1,
                     s['strip_px'])
        framed(_('Wobble'), ((_('Shake strength (%):'), strength),
                             (_('Strip height (px):'), strip)))

        blotches = Gtk.CheckButton.new_with_mnemonic(
            _('_Rainbow blotches over the pane (Wobble effect)'))
        blotches.set_active(s['blotches'])
        b_count = spin(DG_RANGES['blotches_count'][0],
                       DG_RANGES['blotches_count'][1], 1, s['blotches_count'])
        b_strength = scale(0, 100, s['blotches_strength'])
        b_frame = framed(_('Rainbow blotches'), ((None, blotches),
                                                 (_('Blotches:'), b_count),
                                                 (_('Strength (%):'), b_strength)))

        p_rainbow = scale(0, 100, s['pattern_rainbow'])
        p_fps = spin(DG_RANGES['pattern_fps'][0], DG_RANGES['pattern_fps'][1],
                     1, s['pattern_fps'])
        framed(_('Test pattern'), ((_('Rainbow swirl (%):'), p_rainbow),
                                   (_('Frames per second:'), p_fps)))

        def read():
            return {
                'effect':            effect.get_active_id(),
                'duration':          round(duration.get_value(), 1),
                'flash':             flash.get_active(),
                'sound':             sound.get_active(),
                'volume':            int(volume.get_value()),
                'mains_hz':          int(mains.get_active_id()),
                'player':            player.get_active_id(),
                'wobble_strength':   int(strength.get_value()),
                'strip_px':          int(strip.get_value()),
                'blotches':          blotches.get_active(),
                'blotches_count':    int(b_count.get_value()),
                'blotches_strength': int(b_strength.get_value()),
                'pattern_rainbow':   int(p_rainbow.get_value()),
                'pattern_fps':       int(p_fps.get_value()),
            }

        def sync(*_args):
            on = sound.get_active()
            for w in (volume, mains, player):
                w.set_sensitive(on)
            b_frame.set_sensitive(effect.get_active_id() == 'wobble')
            for w in (b_count, b_strength):
                w.set_sensitive(blotches.get_active())

        for w in (effect, sound, blotches):
            w.connect('changed' if w is effect else 'toggled', sync)
        sync()

        test_row = Gtk.HBox(spacing=8)
        test_btn = Gtk.Button.new_with_mnemonic(_('_Test on this pane'))
        target = self.dg_menu_terminal
        if target is None or target not in self.terminator.terminals:
            test_btn.set_sensitive(False)
            target = None
        test_btn.connect('clicked',
                         lambda _b: self._dg_start(target, read()))
        test_row.pack_start(test_btn, False, False, 0)
        box.pack_start(test_row, False, False, 4)

        hint = Gtk.Label()
        hint.set_markup(_(
            '<small>'
            'Run <tt>degauss</tt> in a pane, or use <b>Degauss this pane</b> '
            'in the context menu. <b>Test</b> uses the values shown here '
            'before you press OK.\n'
            'Shake strength applies to both effects. Outside Terminator, '
            '<tt>degauss</tt> reads these settings from Terminator\'s config '
            'file and always draws the Test pattern with terminal colors; '
            '<b>Frames per second</b> only applies there.'
            '</small>'))
        hint.set_line_wrap(True)
        hint.set_xalign(0)
        box.pack_start(hint, False, False, 6)

        def _apply():
            self.dg = read()
        commit.append(_apply)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.add(box)
        return scroll

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
