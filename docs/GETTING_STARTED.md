# Getting started and normal operation

This guide covers initial setup, local data, an ordinary live run, and the
presenter controls. Run commands from the repository root, `D:\b2_recomp`.

## Requirements

The supported native development host is Windows x86-64. You need Python 3.11
or newer, a C++17 compiler, and the pinned Vulkan SDK. The SDK supplies the
Vulkan and SDL3 headers, import libraries, and runtime used by the presenter.
See [BUILDING.md](BUILDING.md) for exact toolchain versions and validation.

Install the locked development dependencies:

```powershell
python -m pip install --requirement .\requirements-dev.lock
```

## Supply and extract the game

Provide your own legally obtained Xbox copy. Install the pinned extraction tool:

```powershell
.\tools\extract\install_extract_xiso.ps1
```

Extract the local image:

```powershell
python .\tools\extract\extract_disc.py `
  --iso ".\Burnout 2\Burnout 2 - Point of Impact (USA).xiso.iso"
```

Game content, recovered title code, SDK files, and local evidence must remain
outside source control. The complete boundary is in
[ASSET_POLICY.md](ASSET_POLICY.md).

## Local paths

Developer-owned inputs and generated outputs stay ignored:

| Purpose | Default path |
| --- | --- |
| Local XISO | `Burnout 2/Burnout 2 - Point of Impact (USA).xiso.iso` |
| Extracted disc | `data/local/extracted/burnout_2_poi_usa/` |
| Persistent save data | `data/local/save-data/` |
| Dashboard/cache data | `data/local/dashboard-data/`, `data/local/cache-data/` |
| Generated reports | `reports/local/` |
| Native/host builds | `build/local/` |
| Decoded-block store | `build/native-guest-loop/decoded-blocks.sqlite3` |
| Native module manifest | `build/native-guest-loop/native-module-manifest.sqlite3` |
| Presenter build manifest | `build/local/first-frame/b2_first_frame.build.json` |
| Vulkan pipeline cache | `build/local/first-frame/vulkan-pipeline-cache.bin` |
| Local third-party tools | `data/local/tools/` |

## Verify the target

Verify the XBE without starting the runtime:

```powershell
python .\tools\project_identity.py `
  .\data\local\extracted\burnout_2_poi_usa\default.xbe `
  --pretty
```

Normal execution requires an exact match in
`tools/targets/supported_targets.json`. The identity gate covers the whole
file, normalized loaded image, certificate, virtual layout, and actual and
embedded section hashes. `--allow-unsupported-xbe` is a developer-only escape
hatch; using it invalidates compatibility and performance claims.

Presenter builds are also content-addressed. Their manifest records source,
shader, compiler, import-library, runtime, executable, DLL, and SPIR-V hashes
plus the exact compiler commands. `--skip-host-build` rejects a missing or
stale manifest. `--allow-stale-artifacts` is likewise unsupported research
mode.

Every live run writes `reports/local/playability/run-manifest.json` with the
repository state, target and generated-code identities, configuration, cache
state, host identity, result, and presenter build identity. Post-run reports
are accepted only when their run ID matches the active manifest.

## Validate and launch

Run the complete asset-free gate:

```powershell
python .\tools\quality_gate.py --full
```

Launch the normal live guest and Vulkan presenter:

```powershell
python .\tools\playability\live_test.py
```

The first launch after native-dispatch changes may rebuild hundreds of cached
modules. The default startup allowance is 30 minutes. Use
`--startup-timeout-seconds SECONDS` to change it, and use `--skip-host-build`
only when the presenter manifest is current.

Close the presenter or press Escape to stop the runtime. The launcher publishes
the stop request through the same sequence-guarded control mapping used for
input and completed-flip acknowledgements.

## SDL3 input and presenter controls

SDL3 owns the window, event pump, keyboard, gamepads, high-DPI behavior, audio
device/stream, and Vulkan surface. Direct Vulkan continues to own rendering.

The platform layer accepts hot-plugged Xbox, PlayStation, Nintendo, virtual,
and third-party controllers recognized by SDL's mapping database. Face buttons
map by position to Xbox A/B/X/Y; shoulders map to White/Black. Start/Back, the
D-pad, stick clicks, both sticks, and analog triggers are forwarded.
DS4Windows is not required for a DualShock 4. An optional
`gamecontrollerdb.txt` beside `b2_first_frame.exe` can extend SDL's mappings.

Keyboard controls map arrow keys, `S`, Backspace, Space/Enter, `B`, `X`, and
`Y` to Xbox D-pad, Start, Back, A, and B/X/Y state. Short digital presses are
latched for at least 150 ms and two completed guest flips so they survive slow
guest polling.

| Input | Function |
| --- | --- |
| F9 | Toggle completed-guest-flip FPS in the window title |
| F10 | Start or stop an armed native hot-path capture |
| F11 | Write a timestamped metrics snapshot under `reports/local/` |
| F12 | Capture a BMP and standalone frozen render bundle |
| Escape | Stop the runtime |

F9 reports guest flips, not 60 Hz presenter ticks. F11 includes guest and
presenter FPS, native execution/cache counters, render work, upload/readback
volume, and CPU/GPU/fence timing. F12 retains the exact command prefix,
resource generation, and interpreter bootstrap needed for a frozen replay.

## Representative and diagnostic runs

The default live command records a bounded diagnostic summary. For a
minimal-overhead run without guest summaries, presenter events, screenshots,
runner logs, or post-run reports:

```powershell
python .\tools\playability\live_test.py `
  --no-diagnostics `
  --skip-host-build
```

Transport and provenance remain active in this mode because they are runtime
correctness boundaries. Existing reports are not deleted.

For captures, exact audits, A/B modes, direct probes, and report regeneration,
use [DEBUGGING_COMMANDS.md](DEBUGGING_COMMANDS.md). For profiler capture, use
[PROFILING.md](PROFILING.md).
