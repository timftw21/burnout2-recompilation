# Getting started

This guide covers host setup, local game data, target verification, the stable
diagnostic launcher, and the work-in-progress static IA-32 launcher. Run all
commands from the repository root.

> **Current limitation:** the Phase 7 static backend reaches the Load/Save
> frontend but stops at an AOT coverage guard while creating a new save slot.
> Use it for development and evidence gathering, not as a finished game build.

## Requirements

The supported native development host is Windows x86-64. You need:

- Python 3.11 or newer;
- the exact locked Python packages;
- the pinned CMake, Ninja, Clang, Vulkan SDK, and SDL3 toolchain; and
- a legally obtained Xbox copy of *Burnout 2: Point of Impact*.

Install Python dependencies:

```powershell
python -m pip install --requirement .\requirements-dev.lock
```

Validate the native toolchain:

```powershell
python .\tools\native_toolchain.py --build-tools-only --pretty
python .\tools\native_toolchain.py --presenter-tools-only --pretty
```

See [BUILDING.md](BUILDING.md) for locked versions, compiler caching, native
presets, and validation commands.

## Supply and extract the game

Install the pinned extraction tool:

```powershell
.\tools\extract\install_extract_xiso.ps1
```

Extract your local image:

```powershell
python .\tools\extract\extract_disc.py `
  --iso ".\Burnout 2\Burnout 2 - Point of Impact (USA).xiso.iso"
```

Game files, recovered title code, save data, SDK material, and local evidence
must remain outside source control. See [ASSET_POLICY.md](ASSET_POLICY.md).

## Local paths

| Purpose | Default path |
| --- | --- |
| Local XISO | `Burnout 2/Burnout 2 - Point of Impact (USA).xiso.iso` |
| Extracted disc | `data/local/extracted/burnout_2_poi_usa/` |
| Persistent save data | `data/local/save-data/` |
| Dashboard/cache data | `data/local/dashboard-data/`, `data/local/cache-data/` |
| Generated reports and captures | `reports/local/` |
| Native/host builds | `build/local/` |
| Decoded block store | `build/native-guest-loop/decoded-blocks.sqlite3` |
| Native debug metadata | `build/native-guest-loop/native-debug-index.json` |
| Presenter build manifest | `build/local/first-frame/b2_first_frame.build.json` |
| Vulkan pipeline cache | `build/local/first-frame/vulkan-pipeline-cache.bin` |
| Local third-party tools | `data/local/tools/` |

## Verify the supported target

Verify the XBE before starting either backend:

```powershell
python .\tools\project_identity.py `
  .\data\local\extracted\burnout_2_poi_usa\default.xbe `
  --pretty
```

Normal evidence requires an exact match in
`tools/targets/supported_targets.json`. `--allow-unsupported-xbe` and
`--allow-stale-artifacts` are conspicuous research overrides; they invalidate
compatibility and performance claims.

Every live run writes `reports/local/playability/run-manifest.json` with the
repository state, target, backend, artifact, presenter build, cache state, and
run identity. Reports are accepted only when their run ID matches that
manifest.

## Validate before launching

Use the changed-file-aware gate while editing:

```powershell
python .\tools\dev_check.py --explain
```

Use the detached closeout matrix before an integration handoff:

```powershell
python .\tools\dev_check.py --launch-closeout
```

The full foreground compatibility gate remains available when explicitly
needed:

```powershell
python .\tools\quality_gate.py --full
```

## Launch modes

### Diagnostic oracle

The default launcher uses the established diagnostic oracle:

```powershell
python .\tools\playability\live_test.py
```

Use this for broad compatibility work and historical comparisons. Its results
must not be described as static IA-32 acceptance.

### Work-in-progress static IA-32 backend

The static backend requires a verified XBE-entry normal-live artifact. Build
one only from retained local capsule/profile evidence using the command in
[BUILDING.md](BUILDING.md). Then launch it explicitly:

```powershell
python .\tools\playability\live_test.py `
  --guest-backend same-isa-ia32 `
  --ia32-artifact .\build\local\ia32-live\manifest.json `
  --skip-host-build
```

This path fails closed if the artifact is missing, stale, fixed-replay-only, or
coverage-incomplete for a reached transfer. It never falls back to the
diagnostic oracle. Do not enable `--developer-live-compile` for ordinary runs.

The first launch after AOT changes can spend several minutes generating and
linking the large 32-bit module. A warm launch reuses the content-addressed
artifact. `--skip-host-build` is valid only while the presenter manifest is
current.

## Manual input and controls

SDL3 owns the window, event pump, keyboard, gamepads, host audio stream, and
Vulkan surface. The platform layer accepts controllers recognized by SDL's
mapping database. Face buttons map by position to Xbox A/B/X/Y; shoulders map
to White/Black. Start/Back, D-pad, stick clicks, sticks, and triggers are
forwarded. An optional `gamecontrollerdb.txt` beside the presenter executable
can extend SDL mappings.

Keyboard mappings include arrows, `S`, Backspace, Space/Enter, `B`, `X`, and
`Y`. Short digital presses are latched to survive slow guest polling.

| Input | Function |
| --- | --- |
| F8 | Trigger an armed manual replay-capsule capture |
| F9 | Toggle completed-guest-flip FPS in the window title |
| F10 | Start or stop an armed native hot-path capture |
| F11 | Write a timestamped metrics snapshot under `reports/local/` |
| F12 | Capture a BMP and standalone frozen render bundle |
| Escape | Request runtime stop |

Gameplay navigation used as evidence is manual. Do not add or use automated
input to advance the title.

## Stopping and classifying a frozen frame

Closing the presenter or pressing Escape publishes the shared native stop word.
If a frame appears frozen, inspect the backend summary before assuming a render
deadlock. The static backend deliberately raises `0x80000003` at an unverified
indirect transfer, and the presenter may continue displaying the last frame
after the guest process has stopped.

The key files are:

- `reports/local/playability/native-live.json`;
- `reports/local/playability/native-live.log`;
- `reports/local/playability/render-debug-events.jsonl`;
- `reports/local/playability/render-debug-report.json`; and
- `reports/local/playability/performance-debug-report.json`.

Use [DEBUGGING_COMMANDS.md](DEBUGGING_COMMANDS.md) for exact fault triage and
[PROFILING.md](PROFILING.md) for controlled performance captures.
