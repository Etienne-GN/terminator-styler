import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from styler import (TerminatorStyler, _render_marker, _collect_terminals,
                    _truthy, _rgb_floats_to_hex, _hex_to_rgb_floats)


class FakeWidget:
    def __init__(self, children=None, terminal=False):
        self._children = children or []
        self.terminal = terminal

    def get_children(self):
        return self._children


class FakeConfig:
    def __init__(self, blocks):
        self.blocks = blocks
        self.saved = False

    def plugin_get_config(self, name):
        return self.blocks.get(name)

    def plugin_del_config(self, name):
        self.blocks.pop(name, None)

    def plugin_set(self, name, key, value):
        self.blocks.setdefault(name, {})[key] = value

    def save(self):
        self.saved = True


def is_term(widget):
    return getattr(widget, "terminal", False)


def bare_styler():
    return TerminatorStyler.__new__(TerminatorStyler)


def test_render_marker_substitutes_n():
    assert _render_marker("[⊞ {n}]", 3) == "[⊞ 3]"
    assert _render_marker(" ⊞×{n}", 2) == " ⊞×2"


def test_render_marker_bad_format_falls_back():
    assert _render_marker("broken {x}", 4) == " [4 hidden]"
    assert _render_marker("{", 1) == " [1 hidden]"


def test_collect_terminals_counts_leaves_only():
    tree = FakeWidget(children=[
        FakeWidget(terminal=True),
        FakeWidget(children=[FakeWidget(terminal=True), FakeWidget(terminal=True)]),
    ])
    assert len(_collect_terminals(tree, is_term)) == 3


def test_collect_terminals_empty_subtree():
    assert _collect_terminals(FakeWidget(), is_term) == []


def test_collect_terminals_widget_is_terminal():
    assert len(_collect_terminals(FakeWidget(terminal=True), is_term)) == 1


def test_truthy():
    assert _truthy(True, False) is True
    assert _truthy(False, True) is False
    assert _truthy(None, True) is True
    assert _truthy(None) is False
    assert _truthy("False", True) is False
    assert _truthy("true", False) is True
    assert _truthy("1", False) is True
    assert _truthy("0", True) is False


def test_rgb_floats_to_hex_basic():
    assert _rgb_floats_to_hex(0.0, 0.0, 0.0) == "#000000"
    assert _rgb_floats_to_hex(1.0, 1.0, 1.0) == "#ffffff"
    assert _rgb_floats_to_hex(82 / 255, 148 / 255, 226 / 255) == "#5294e2"


def test_rgb_floats_to_hex_clamps_out_of_range():
    assert _rgb_floats_to_hex(-0.5, 1.5, 0.5) == "#00ff80"


def test_hex_to_rgb_floats_valid():
    r, g, b = _hex_to_rgb_floats("#5294e2", (0.0, 0.0, 0.0))
    assert (round(r, 3), round(g, 3), round(b, 3)) == (0.322, 0.58, 0.886)


def test_hex_to_rgb_floats_no_hash_and_uppercase():
    assert (_hex_to_rgb_floats("5294E2", (0.0, 0.0, 0.0))
            == _hex_to_rgb_floats("#5294e2", (0.0, 0.0, 0.0)))


def test_hex_to_rgb_floats_invalid_returns_default():
    default = (0.1, 0.2, 0.3)
    assert _hex_to_rgb_floats("nope", default) == default
    assert _hex_to_rgb_floats("#12", default) == default
    assert _hex_to_rgb_floats("", default) == default
    assert _hex_to_rgb_floats(None, default) == default


def test_color_round_trip():
    for h in ("#5294e2", "#000000", "#ffffff", "#ff5555"):
        assert _rgb_floats_to_hex(*_hex_to_rgb_floats(h, (0.0, 0.0, 0.0))) == h


def test_rule_ps_without_profile_is_skipped():
    s = bare_styler()
    assert s._rule_ps({"command": "ssh", "argument": ""}) is None
    assert s._rule_ps({"command": "ssh", "profile": ""}) is None


def test_rule_ps_legacy_schema():
    s = bare_styler()
    assert s._rule_ps({"pattern": "top", "type": "command",
                       "profile": "dark"}) == {
        "command": "top", "argument": "", "profile": "dark"}
    assert s._rule_ps({"pattern": "prod", "type": "host",
                       "profile": "red"}) is None


def test_migrate_titlereact_single_target():
    cfg = FakeConfig({"TitleReact": {
        "target": "titlebar",
        "rule_0": {"name": "root", "pattern": "root@",
                   "bg_color": "#cc0000", "fg_color": "", "enabled": True},
    }})
    sections = bare_styler()._migrate_from_legacy(cfg)
    assert sections["tb_target_titlebar"] is True
    assert sections["tb_target_window"] is False
    assert sections["tb_rule_0"]["pattern"] == "root@"
    assert cfg.saved


def test_migrate_dual_target_keys_win_over_single_target():
    cfg = FakeConfig({"TitlebarChanger": {
        "target": "titlebar",
        "target_titlebar": "False",
        "target_window": "True",
    }})
    sections = bare_styler()._migrate_from_legacy(cfg)
    assert sections["tb_target_titlebar"] == "False"
    assert sections["tb_target_window"] == "True"
