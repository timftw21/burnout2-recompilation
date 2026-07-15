# Burnout 2: Point of Impact Static Recompilation

This repository explores static recompilation of the Xbox release of *Burnout 2:
Point of Impact*. It rehosts recovered IA-32 game code behind native services for
graphics, audio, input, files, timing, threading, and memory while preserving
guest-visible behavior.

The Xbox release is the base target because its x86 CPU makes it a practical
static-recompilation candidate. This is reverse engineering, not a source port.
Original game assets, executable data, trademarks, and copyrighted content
remain owned by their rights holders.

## Project Status

Updated: July 14, 2026.

The recovered boot path now runs through a resumable native guest loop, presents
completed NV2A frames through Vulkan, plays frontend audio, and accepts
keyboard-backed Xbox input. The presenter is paced at the console target of
60 FPS, and guest execution has measured headroom above that target without
skipping completed flips or weakening render validation.

| Area | Status |
| --- | --- |
| XBE inspection and loader | Complete for the current title |
| Analysis database | Complete and reproducible |
| Runtime ABI shims | Implemented for the currently reached path |
| IA-32 lifter and native executor | Running the recovered boot/frontend path |
| Vulkan presentation | Live completed-flip presentation with strict validation |
| Audio and input | Live frontend audio and keyboard-backed controller state |
| Gameplay | In progress; frontend execution is sustained, gameplay is not yet validated |

The current visible frontier is the Load/New/Don't Save flow. Its overlay draws
correctly, but the guest does not submit the expected fullscreen background for
the final captured frame. Evidence points upstream of Vulkan command execution.
There is also no complete save-state system yet.

## Ground Rules

- Supply your own legally obtained Xbox copy.
- Do not commit or distribute ISOs, extracted assets, XBE sections, SDK files,
  symbols, generated source containing original code/data, or proprietary local
  reports.
- Keep extraction, analysis, lifting, patching, and build steps scripted.
- Prefer observed behavior and ABI evidence over title-specific approximations.
- Treat graphics, audio, input, files, timing, threading, and memory as explicit
  host boundaries.

## Local Data

Developer-owned inputs and generated outputs stay ignored:

| Purpose | Default path |
| --- | --- |
| Local XISO | `Burnout 2/Burnout 2 - Point of Impact (USA).xiso.iso` |
| Extracted disc | `data/local/extracted/burnout_2_poi_usa/` |
| Persistent save data | `data/local/save-data/` |
| Dashboard/cache data | `data/local/dashboard-data/`, `data/local/cache-data/` |
| Generated reports | `reports/local/` |
| Native/host builds | `build/local/` |
| Local third-party tools | `data/local/tools/` |

The current local extraction contains 667 files. Its `default.xbe` imports 142
Xbox kernel symbols and no non-kernel libraries. Sixteen of its seventeen
section digests match; the local report records the `.text` mismatch.

## Quick Start

The examples use PowerShell on Windows. The native presenter requires Python, a
C++17 compiler, and a working Vulkan development/runtime environment.

Install the pinned local extraction tool:

```powershell
.\tools\extract\install_extract_xiso.ps1
```

Extract the local image:

```powershell
python .\tools\extract\extract_disc.py `
  --iso ".\Burnout 2\Burnout 2 - Point of Impact (USA).xiso.iso"
```

Run the unit suite:

```powershell
python -m unittest discover -s tests/unit
```

Launch the normal live guest and Vulkan presenter:

```powershell
python .\tools\playability\live_test.py
```

Close the window or press Escape to stop both processes. Use
`--skip-host-build` when the presenter is already current. While its window is
focused, F12 writes a uniquely named 32-bit BMP under
`reports/local/screenshots/`.

## Core Workflows

### Inspect and map the XBE

```powershell
python .\tools\xbe\xbe_info.py `
  .\data\local\extracted\burnout_2_poi_usa\default.xbe `
  --pretty

python .\tools\loader\xbe_loader.py `
  .\data\local\extracted\burnout_2_poi_usa\default.xbe `
  --pretty

python .\tools\analysis\analysis_db.py `
  .\data\local\extracted\burnout_2_poi_usa\default.xbe `
  --output .\reports\local\analysis-db.json `
  --pretty
```

### Exercise runtime bindings

```powershell
python .\tools\runtime\runtime_smoke.py `
  .\data\local\extracted\burnout_2_poi_usa\default.xbe `
  --extracted-root .\data\local\extracted\burnout_2_poi_usa `
  --pretty
```

### Generate a recompilation sample

```powershell
python .\tools\recomp\recompile_range.py `
  --hex 33C0C3 `
  --virtual-address 0x12594 `
  --symbol zero_return `
  --pretty
```

For title-derived ranges, send C++ and JSON output only to ignored local paths:

```powershell
python .\tools\recomp\recompile_range.py `
  .\data\local\extracted\burnout_2_poi_usa\default.xbe `
  --virtual-address 0x12594 `
  --size 3 `
  --symbol b2_default_zero_return `
  --cpp-output .\reports\local\recomp\b2_default_zero_return.cpp `
  --json-output .\reports\local\recomp\b2_default_zero_return.json
```

### Audit IA-32 coverage

```powershell
python .\tools\recomp\audit_x86_coverage.py `
  .\data\local\extracted\burnout_2_poi_usa\default.xbe `
  --dynamic-block-cache .\reports\local\playability\dynamic-block-cache.json `
  --json-output .\reports\local\recomp\x86-coverage-audit.json `
  --pretty
```

The audit seeds dynamically recovered blocks, follows direct control flow, and
uses Capstone as an independent instruction-length oracle. New instruction
support remains trace-driven.

### Run a bounded native probe

```powershell
python .\tools\playability\playability_probe.py `
  .\data\local\extracted\burnout_2_poi_usa\default.xbe `
  --extracted-root .\data\local\extracted\burnout_2_poi_usa `
  --dynamic-block-cache .\reports\local\playability\dynamic-block-cache.json `
  --native-guest-loop `
  --max-steps 1000000 `
  --json-output .\reports\local\playability\native-loop-1m.json
```

`--max-steps 0` means unlimited execution. Native state is preserved across
compiled modules and Xbox-service boundaries.

### Validate before manual testing

```powershell
python .\tools\playability\preflight_validation.py `
  --extracted-root .\data\local\extracted\burnout_2_poi_usa `
  --suite .\tools\playability\preflight_suite.json `
  --run-replays `
  --probe-summary .\reports\local\playability\native-live.json `
  --json-output .\reports\local\preflight\full-report.json `
  --pretty
```

The preflight gate inventories captured texture, primitive, and draw-source
requirements; runs strict Vulkan validation; rejects blank or near-solid
frames; and checks guest progress, frontend state, and menu-audio health. It
exits nonzero on a missing implementation or regression.

### Audit exact completed flips

```powershell
python .\tools\playability\live_test.py `
  --lossless-flip-audit `
  --skip-host-build
```

The guest records every producer boundary. Health-selected flips are published
with their exact command/resource prefix, strictly rendered and read back by
Vulkan, then acknowledged to the guest. Runs are written under
`reports/local/flip-audit/`. Add `--flip-audit-max-flips N` for bounded
automation or `--flip-audit-health-interval 1` for exhaustive per-flip checks.

### Regenerate rendering diagnostics

Normal live runs write:

- `reports/local/playability/native-live.json`
- `reports/local/playability/render-debug-events.jsonl`
- `reports/local/playability/render-debug-report.json`

Regenerate the aggregate report without rerunning the game:

```powershell
python .\tools\playability\render_debug_report.py `
  --probe-summary .\reports\local\playability\native-live.json `
  --presenter-events .\reports\local\playability\render-debug-events.jsonl `
  --json-output .\reports\local\playability\render-debug-report.json `
  --pretty
```

The report correlates guest vertex and draw provenance with host reloads,
completed flips, texture state, geometry health, readbacks, frame pacing, and
native hot paths.

### Replay a recovered stream

```powershell
python .\tools\host\first_frame_smoke.py `
  --max-frames 3 `
  --render-stream-json .\reports\local\render\recovered-d3d-stream.json `
  --pretty
```

Set both `--max-frames 0` and `--timeout-seconds 0` for an unbounded manual
replay. This replays captured state; it is not a live guest loop.

## Live Runtime Model

The live path is intentionally split at explicit boundaries:

1. The native executor runs recovered guest blocks against explicit IA-32 state
   and sparse, image-backed guest memory.
2. Runtime shims handle observed Xbox ABI calls, including stack cleanup,
   out-parameters, handles, timing, input, files, and audio.
3. Completed NV097 flips publish immutable command and texture generations.
4. The Vulkan presenter incrementally consumes each exact flip, retains
   unchanged resources and pipelines, presents at 60 Hz, and acknowledges it.
5. The launcher propagates presenter closure to the guest and performs bounded
   process-tree cleanup only if graceful shutdown fails.

Normal live presentation does not skip guest flips, truncate render work, or
substitute reduced-fidelity frames for performance. The guest and presenter
overlap their work through a cross-process publication/acknowledgement handshake.

The presenter maps arrow keys, `S`, Backspace, Space, Enter, `B`, `X`, and
`Y` to Xbox D-pad, Start, Back, A, Start+A, and B/X/Y state. Digital presses
remain published for at least 150 ms so short taps survive native guest slices.

## Current Performance Evidence

The July 14 regression under
`reports/local/playability/jitter-fix-v10` is the current 60 FPS proof:

| Metric | Result |
| --- | --- |
| Guest instructions / completed flips | 12,000,000 / 494 |
| Publication ordering | Initial load + 493 distinct exact reloads; no redundant reloads |
| Presenter steady-state FPS | 59.900 |
| Frame p50 / p95 / p99 | 16.647 / 17.048 / 17.216 ms |
| Reload p50 / p95 / p99 | 0.775 / 1.026 / 1.305 ms |
| Command-load p50 / p99 | 0.109 / 0.288 ms |
| Presenter reload busy time | 3.73% |
| Shutdown | Guest, presenter, and launcher exited normally |

The earlier ten-million-instruction native proof measured 1,322,044 guest
instructions/s and 80.356 guest flips/s over its recent 120 publications, with
the same guest steps, ABI calls, dirty-page writebacks, invalidations, and render
writes as its pre-optimization comparison. A follow-up five-flip lossless audit
retained all boundaries, acknowledged every selected frame, and passed strict
render validation.

The main performance mechanisms are exact dirty-page worklists, native
cross-module dispatch, incremental command ingestion, persistent shared handles,
compact binary texture generations, retained Vulkan resources/pipelines, and a
high-resolution 60 Hz waitable timer. These are host-side optimizations; guest
instruction, memory, timing, ABI, render, and flip semantics remain
authoritative.

## Repository Layout

```text
runtime/
  host/          Native Vulkan presenter
  xbox/          Xbox runtime and hardware shims
tools/
  analysis/      Project-owned analysis database
  extract/       Local disc extraction
  host/          Presenter build and smoke wrapper
  loader/        XBE mapping and loader tools
  playability/   Live launcher, probes, audits, and reports
  recomp/        IA-32 lifting, execution, coverage, and C++ emission
  render/        D3D8/NV2A stream normalization
  runtime/       Runtime smoke tools
  xbe/           XBE inspection
tests/unit/      Unit and regression tests
```

`data/local/`, `reports/local/`, `build/local/`, extracted game content,
generated C++, dynamic-block caches, and recovered stream reports are ignored.

## Development Priorities

1. Trace why the Load/New/Don't Save frontend omits its background draw, using
   exact guest submission and host flip correlation rather than host-side
   placement or asset substitutions.
2. Reach and validate gameplay, then add each stable milestone to the strict
   preflight replay suite.
3. Design a versioned save state covering CPU state, sparse-memory changes,
   runtime objects, audio, and the live bridge.
4. Compact or replace the large JSON dynamic-block cache to reduce startup cost.
5. Measure longer-run input/audio behavior and replace remaining probe-only
   models with recovered semantics where evidence permits.
6. Continue adding IA-32, NV2A, and Xbox ABI support only for observed paths,
   with focused regressions for each new edge.
