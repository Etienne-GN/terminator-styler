# Degauss feature — design

Date: 2026-09-29
Status: sections 1–2 approved in session; section 3 (errors/testing)
written without a separate review at the user's request ("include
degauss, tell me when ready").

## Goal

Fold the standalone `~/claude/degauss/` experiment (a Terminator plugin
plus a `degauss` CLI) into TerminatorStyler as a sixth feature, with
every constant exposed in Preferences, and ship the full CLI (sound +
terminal fallback) in this repo.

## Effects

Two independently configurable effects, chosen by `dg_effect`:

- **Wobble** — snapshot of the pane, drawn in horizontal strips that
  shake and breathe while the shake decays. Optional **Rainbow
  blotches**: coloured patches orbiting over the pane.
- **Test pattern** — SMPTE-style colour bars shaken the same way, with a
  rainbow swirl whose amount is configurable.

Inside Terminator both are drawn by the plugin with cairo on the VTE
widget (no terminal output, scrollback untouched, works from the context
menu). Outside Terminator the CLI draws the Test pattern with ANSI
truecolor cells; Wobble needs a pane snapshot, so it is never used
there.

## Flow

- CLI inside Terminator: connect to
  `$XDG_RUNTIME_DIR/terminator-styler-degauss-<pid>.sock`, send
  `$TERMINATOR_UUID\n`. The plugin plays the sound, runs the effect and
  answers `done`, or `unknown` (not its pane), `busy`, `unavailable`
  (pane not drawable).
- Context menu: *Degauss this pane* runs the same code, no socket.
- CLI outside Terminator (no socket answers, or `unavailable`): read
  `[plugins] [[TerminatorStyler]]` from
  `${XDG_CONFIG_HOME:-~/.config}/terminator/config` with configobj
  (a Terminator dependency), fall back to defaults if missing, play the
  sound and draw the ANSI Test pattern.
- `enable_degauss = False`: the plugin stops listening and hides the
  menu entry; the CLI reads the flag and exits silently.

The socket name differs from the standalone plugin's
(`terminator-degauss-<pid>.sock`) so both can coexist during the switch;
`install.sh` lists `degauss_plugin.py` as a legacy file.

## Settings (`TerminatorStyler` block)

| Key                    | Range                          | Default |
| ---------------------- | ------------------------------ | ------- |
| `enable_degauss`       | bool                           | True    |
| `dg_effect`            | wobble / pattern               | wobble  |
| `dg_duration`          | 0.5–5.0 s                      | 1.9     |
| `dg_flash`             | bool                           | True    |
| `dg_sound`             | bool                           | False   |
| `dg_volume`            | 0–100 %                        | 30      |
| `dg_mains_hz`          | 50 / 60                        | 60      |
| `dg_player`            | auto / pw-play / paplay / aplay | auto   |
| `dg_wobble_strength`   | 0–200 %                        | 100     |
| `dg_strip_px`          | 1–8                            | 2       |
| `dg_blotches`          | bool                           | False   |
| `dg_blotches_count`    | 1–8                            | 3       |
| `dg_blotches_strength` | 0–100 %                        | 100     |
| `dg_pattern_rainbow`   | 0–100 %                        | 100     |
| `dg_pattern_fps`       | 10–60 (CLI fallback only)      | 30      |

At 100 % strengths the effects match the standalone versions. Sound
length is duration + 0.3 s. The synthesized WAV is cached in
`${XDG_CACHE_HOME:-~/.cache}/degauss/`, keyed by length, mains
frequency and volume.

## Shared code

The plugin must stay a single drop-in file and the CLI must run without
GTK, so the settings parser, the synthesizer and the palette are
duplicated in `styler.py` and `degauss`. A test asserts both copies
produce identical settings and identical WAV bytes.

## Preferences

General tab: *Degauss* switch. Degauss tab: Effect, Duration, Initial
flash; frames for Sound, Wobble, Rainbow blotches, Test pattern with
controls greyed out when they do not apply; a **Test** button that runs
the effect on the pane the menu was opened from with the values
currently in the dialog.

## Error handling

- `XDG_RUNTIME_DIR` unset or bind failure: log, no listener; menu entry
  still works.
- Socket reads are non-blocking (GLib IO watch per connection), so a
  stalled client cannot freeze the UI.
- Pane unmapped or closed mid-effect: effect aborts, client gets `done`.
- Plugin unload or feature switched off: running effects abort, socket
  closed and unlinked.
- No audio player found: logged at debug level, effect runs silently.
- Sound synthesis and playback run in a worker thread, never on the GTK
  main loop; the WAV is pre-generated when the sound setting is on.

## Testing

- Unit tests: settings parsing and clamping, plugin/CLI parity, ANSI
  frame generation, CLI config reading.
- Offscreen renders of both effects to PNG at several time points.
- Live run in a sandboxed Terminator (`XDG_CONFIG_HOME` pointing to a
  test directory): CLI round-trip over the socket, menu path, fallback
  outside Terminator.
