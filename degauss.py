#!/usr/bin/python3
"""Degauss the terminal like a 90s CRT: thunk, hum, wobble, rainbow.

Inside Terminator with the TerminatorStyler plugin loaded, the plugin
animates the pane and plays the sound. Anywhere else, this script plays
the sound and draws the Test pattern with ANSI truecolor cells. Settings
come from the [[TerminatorStyler]] block of Terminator's config file.
"""

import glob
import math
import os
import random
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import wave
from array import array

SOCKET_GLOB = 'terminator-styler-degauss-*.sock'


# ── degauss shared: begin ── (identical copy in styler.py; see tests)

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


def config_path():
    base = os.environ.get('XDG_CONFIG_HOME') or os.path.expanduser('~/.config')
    return os.path.join(base, 'terminator', 'config')


def read_config():
    """Return (enabled, settings) from Terminator's config; defaults when
    the file or configobj is missing."""
    try:
        from configobj import ConfigObj, ConfigObjError
    except ImportError:
        return True, dict(DG_DEFAULTS)
    path = config_path()
    if not os.path.exists(path):
        return True, dict(DG_DEFAULTS)
    try:
        cfg = ConfigObj(path, encoding='utf-8')
    except (ConfigObjError, OSError) as ex:
        print('degauss: cannot read %s: %s' % (path, ex), file=sys.stderr)
        return True, dict(DG_DEFAULTS)
    block = cfg.get('plugins', {}).get('TerminatorStyler', {})
    enabled = str(block.get('enable_degauss', True)).strip().lower() \
        in ('1', 'true', 'yes', 'on')
    return enabled, dg_settings(block)


def ask_terminator(timeout):
    """Ask the plugin to degauss this pane. Returns its answer, 'timeout',
    or None when no Terminator process knows the pane."""
    uuid = os.environ.get('TERMINATOR_UUID')
    rundir = os.environ.get('XDG_RUNTIME_DIR')
    if not uuid or not rundir:
        return None
    for path in glob.glob(os.path.join(rundir, SOCKET_GLOB)):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            sock.connect(path)
            sock.sendall(uuid.encode('ascii') + b'\n')
            answer = sock.makefile('r').readline().strip()
        except (ConnectionRefusedError, FileNotFoundError):
            continue
        except socket.timeout:
            print('degauss: no answer from %s' % path, file=sys.stderr)
            return 'timeout'
        finally:
            sock.close()
        if answer != 'unknown':
            return answer
    return None


def play_sound(s):
    if not s['sound'] or s['volume'] <= 0:
        return None
    player = dg_player(s['player'])
    if player is None:
        print('degauss: no audio player found (%s)' % s['player'],
              file=sys.stderr)
        return None
    return subprocess.Popen([player, dg_ensure_wav(s)],
                            stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)


def bar_grid(cols, rows):
    grid = [[(0, 0, 0)] * cols for _ in range(rows)]
    for x0, x1, y0, y1, rgb in dg_bars():
        for y in range(int(round(y0 * rows)), int(round(y1 * rows))):
            for x in range(int(round(x0 * cols)), int(round(x1 * cols))):
                grid[y][x] = rgb
    return grid


def frame(t, grid, s, rng):
    rows = len(grid)
    cols = len(grid[0])
    decay = math.exp(-t / 0.42)
    strength = s['wobble_strength'] / 100.0
    rainbow = s['pattern_rainbow'] / 100.0
    amp = cols * 0.12 * decay * strength
    flash = 1.0 + 0.6 * math.exp(-t / 0.06) if s['flash'] else 1.0
    cx, cy = cols / 2, rows / 2
    spin = t * 7.0
    dy = int(round(rows * 0.04 * decay * strength
                   * math.sin(2 * math.pi * 9 * t)))
    buf = ['\x1b[?2026h']
    for y in range(rows):
        sy = y + dy
        dx = (amp * math.sin(2 * math.pi * 6 * t + y * 0.33)
              + (rng.random() - 0.5) * amp * 0.3)
        buf.append('\x1b[%d;1H' % (y + 1))
        last = None
        for x in range(cols):
            sx = int(math.floor(x - dx))
            if 0 <= sx < cols and 0 <= sy < rows:
                r, g, b = grid[sy][sx]
            else:
                r, g, b = 0, 0, 0
            ex, ey = (x - cx) / cx, (y - cy) / cy * 0.5
            dist = math.hypot(ex, ey)
            blot = decay * rainbow * (0.35 + 0.65 * min(1.0, dist))
            if blot > 0.03:
                hr, hg, hb = dg_hue(math.atan2(ey, ex) / (2 * math.pi)
                                    + dist * 0.7 - spin)
                k = 255 * blot
                r = r * (1 - blot) + hr * k
                g = g * (1 - blot) + hg * k
                b = b * (1 - blot) + hb * k
            col = tuple(min(255, int(v * flash)) & 0xF0 for v in (r, g, b))
            if col != last:
                buf.append('\x1b[48;2;%d;%d;%dm' % col)
                last = col
            buf.append(' ')
    buf.append('\x1b[0m\x1b[?2026l')
    return ''.join(buf)


def test_pattern(s):
    cols, rows = shutil.get_terminal_size()
    grid = bar_grid(cols, rows)
    rng = random.Random()
    step = 1.0 / s['pattern_fps']
    out = sys.stdout
    out.write('\x1b[?1049h\x1b[?25l')
    try:
        start = time.monotonic()
        while True:
            t = time.monotonic() - start
            if t > s['duration']:
                break
            out.write(frame(t, grid, s, rng))
            out.flush()
            time.sleep(max(0.0, step - (time.monotonic() - start - t)))
    finally:
        out.write('\x1b[0m\x1b[2J\x1b[?25h\x1b[?1049l')
        out.flush()


def main():
    if not (sys.stdout.isatty() and sys.stdin.isatty()):
        return
    enabled, s = read_config()
    if not enabled:
        return
    answer = ask_terminator(s['duration'] + 3)
    if answer in ('done', 'busy', 'timeout'):
        return
    if answer not in (None, 'unavailable'):
        print('degauss: unexpected answer %r' % answer, file=sys.stderr)
    play_sound(s)
    test_pattern(s)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        pass
