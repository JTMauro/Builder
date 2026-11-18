# Builder's copy of Craft Anything

This folder is the Craft Anything engine, copied from
[storytold/craft-anything](https://github.com/storytold/craft-anything) at commit `70e4d98`
(version 0.6.1). It is used under its own license, MIT OR Apache-2.0: see `LICENSE-MIT`,
`LICENSE-APACHE`, `NOTICE` and `ATTRIBUTION.md`, which stay with the code.

Builder (the Python server one folder up) runs this engine's command-line tool for every Craft
build. You can change the engine here when Builder needs more from it.

## Changes from the original

- `apps/craft-anything-cli/src/rebuild_cmd.rs`: `rebuild --events-json`. Each run event is printed
  as one JSON line on stdout, and each answer to a question or approval is read as one JSON line
  on stdin. Builder's server uses this to follow a build live and to show approval cards.

Keep this list up to date when you change the engine, so you can later take updates from the
original project and see what's yours.

## Build it

```bash
cd engine
cargo build --release -p craft-anything-cli
```

The first build takes several minutes and needs about 3 GB of disk space. Builder finds
`engine/target/release/craft-anything-cli` (or `debug`) automatically. Set `CRAFT_ENGINE_BIN`
to use another copy.

## Taking updates from the original

```bash
git -C /path/to/craft-anything pull
```

Compare that checkout with this folder, carry the changes listed above across, rebuild, and run
Builder's tests (`pytest -q` in the repository root).

The engine's own rules for contributors are in `AGENTS.md` and `docs/pitfalls.md` (never crash,
everything is a tool, run programs only through `craft_anything_platform::process`, and so on).
Follow them when you change code in this folder.
