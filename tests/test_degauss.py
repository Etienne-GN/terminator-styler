import importlib.machinery
import importlib.util
import os
import random
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import styler


def load_cli():
    path = os.path.join(ROOT, 'degauss.py')
    loader = importlib.machinery.SourceFileLoader('degauss_cli', path)
    spec = importlib.util.spec_from_loader('degauss_cli', loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


cli = load_cli()


def shared_block(path):
    text = open(path, encoding='utf-8').read()
    begin = text.index('# ── degauss shared: begin')
    begin = text.index('\n', begin) + 1
    end = text.index('# ── degauss shared: end ──')
    return text[begin:end]


def test_shared_block_identical():
    assert (shared_block(os.path.join(ROOT, 'styler.py'))
            == shared_block(os.path.join(ROOT, 'degauss.py')))


def test_settings_defaults_when_empty():
    assert cli.dg_settings({}) == cli.DG_DEFAULTS


def test_settings_round_trip_through_strings():
    raw = {'dg_' + k: str(v) for k, v in cli.DG_DEFAULTS.items()}
    assert cli.dg_settings(raw) == cli.DG_DEFAULTS


def test_settings_parse_and_clamp():
    s = cli.dg_settings({
        'dg_effect': 'pattern',
        'dg_duration': '9',
        'dg_flash': 'False',
        'dg_sound': 'True',
        'dg_volume': '-5',
        'dg_mains_hz': '50',
        'dg_player': 'paplay',
        'dg_wobble_strength': '150.4',
        'dg_strip_px': '0',
        'dg_blotches_count': '3.6',
    })
    assert s['effect'] == 'pattern'
    assert s['duration'] == 5.0
    assert s['flash'] is False
    assert s['sound'] is True
    assert s['volume'] == 0
    assert s['mains_hz'] == 50
    assert s['player'] == 'paplay'
    assert s['wobble_strength'] == 150
    assert s['strip_px'] == 1
    assert s['blotches_count'] == 4


@pytest.mark.parametrize('key,value', [
    ('dg_effect', 'sparkles'),
    ('dg_mains_hz', '55'),
    ('dg_player', 'rm'),
    ('dg_duration', 'long'),
    ('dg_volume', 'nan'),
])
def test_settings_reject_bad_values(key, value):
    assert cli.dg_settings({key: value}) == cli.DG_DEFAULTS


def test_bars_cover_the_pane():
    area = sum((x1 - x0) * (y1 - y0) for x0, x1, y0, y1, _ in cli.dg_bars())
    assert abs(area - 1.0) < 1e-9


def test_synth_identical_in_both_copies(tmp_path):
    a = tmp_path / 'a.wav'
    b = tmp_path / 'b.wav'
    cli.dg_synth(str(a), 0.2, 50, 0.3)
    styler.dg_synth(str(b), 0.2, 50, 0.3)
    assert a.read_bytes() == b.read_bytes()
    assert len(a.read_bytes()) == 44 + int(cli.DG_RATE * 0.2) * 2


def test_wav_path_keyed_by_sound_settings(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path))
    s = dict(cli.DG_DEFAULTS)
    base = cli.dg_wav_path(s)
    assert base.startswith(str(tmp_path / 'degauss'))
    for key, value in (('volume', 31), ('mains_hz', 50), ('duration', 2.0)):
        changed = dict(s, **{key: value})
        assert cli.dg_wav_path(changed) != base
    assert cli.dg_wav_path(dict(s, strip_px=5)) == base


def test_read_config_missing_file(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path))
    assert cli.read_config() == (True, cli.DG_DEFAULTS)


def test_read_config_styler_block(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path))
    (tmp_path / 'terminator').mkdir()
    (tmp_path / 'terminator' / 'config').write_text(
        '[global_config]\n'
        '[plugins]\n'
        '  [[TerminatorStyler]]\n'
        '    enable_degauss = False\n'
        '    dg_effect = pattern\n'
        '    dg_pattern_fps = 12\n', encoding='utf-8')
    enabled, s = cli.read_config()
    assert enabled is False
    assert s['effect'] == 'pattern'
    assert s['pattern_fps'] == 12


def test_frame_draws_every_row():
    grid = cli.bar_grid(20, 6)
    assert len(grid) == 6 and all(len(row) == 20 for row in grid)
    out = cli.frame(0.3, grid, dict(cli.DG_DEFAULTS), random.Random(1))
    for y in range(6):
        assert '\x1b[%d;1H' % (y + 1) in out
    assert out.startswith('\x1b[?2026h') and out.endswith('\x1b[?2026l')


def test_frame_settles_on_plain_bars():
    grid = cli.bar_grid(14, 4)
    s = dict(cli.DG_DEFAULTS, flash=False)
    out = cli.frame(60.0, grid, s, random.Random(1))
    r, g, b = grid[0][0]
    assert '\x1b[48;2;%d;%d;%dm' % (r & 0xF0, g & 0xF0, b & 0xF0) in out


def test_ask_terminator_outside_terminator(monkeypatch):
    monkeypatch.delenv('TERMINATOR_UUID', raising=False)
    assert cli.ask_terminator(1) is None
