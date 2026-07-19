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

Updated: July 19, 2026.

The recovered boot path now runs through a resumable native guest loop, presents
completed NV2A frames through Vulkan, plays frontend audio, and accepts
keyboard-backed Xbox input. The presenter is paced at the console target of
60 FPS without skipping completed flips or weakening render validation. Guest
throughput is still under active optimization on the newly reached gameplay
path.

| Area | Status |
| --- | --- |
| XBE inspection and loader | Complete for the current title |
| Analysis database | Complete and reproducible |
| Runtime ABI shims | Implemented for the currently reached path |
| IA-32 lifter and native executor | Running the recovered boot, frontend, and Lesson One path |
| Vulkan presentation | Live completed-flip presentation with strict validation |
| Audio and input | Live frontend audio and keyboard-backed controller state |
| Gameplay | Lesson One renders the world, HUD, and player car at the correct chase-camera scale |

The first playable 3D scene now renders its sky, road, foliage, walls, lights,
HUD, and player car from the intended chase camera. Exact camera, scene-record,
and presenter audits agree on the draw population, resources, matrices, and
completed-flip boundary. Strict replays preserve the foliage mip chain and
current vertex attributes, apply X8 and active-stage alpha semantics, and
restore both car-shadow layers. The opening logo renders the guest's styled,
fading `Loading - please wait` label on its matching completed flip. The
Load/Save memory-card strip is restored, and gameplay camera-atlas passes remain
excluded from the presented surface. There is no complete save-state system.

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

### Use the fast render-debug loop

Start with one normal live run and reuse the current presenter build:

```powershell
python .\tools\playability\live_test.py --skip-host-build
```

Press F12 on the first stable bad frame, then close the window. F12 writes the
screenshot plus a sibling `*-render-capture` directory containing the exact
completed command prefix, resource generation, inherited interpreter-state
bootstrap, and `render.json` manifest used for that frame.
Treat that bundle, `native-live.json`, `render-debug-events.jsonl`, and
`render-debug-report.json` as one evidence set. The report should decide the
next layer before another run:

1. Reproduce a presenter-side result with
   `render_debug_suite.py --skip-build --pretty`. It automatically selects the
   latest retained F12 manifest and avoids rerunning the guest.
2. Use `live_test.py --audit-scene-records --skip-host-build` when draw counts,
   pointers, or scene ownership disagree. It closes after the first complete
   scene table.
3. Add `--audit-world-matrix-address ADDRESS` only after the report identifies
   one matrix. Broad matrix and lossless-flip audits are opt-in because they
   intentionally trade throughput and output size for provenance.

This order keeps the default path representative, makes each additional run
answer one question, and preserves strict geometry/resource/flip validation.
The suite also compares the live presenter's active GPU-feedback cache with the
target set derived from the retained frame, so stale camera/shadow images are a
ranked finding rather than a visual guess. Capture validation verifies the
physical append-only record count and interpreter bootstrap before trusting a
frozen replay; contaminated or non-standalone bundles produce a critical
finding and a nonzero exit.

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
Use this workflow for transport validation, not as the default visual-debug
loop.

### Regenerate rendering diagnostics

Normal live runs write:

- `reports/local/playability/native-live.json`
- `reports/local/playability/render-debug-events.jsonl`
- `reports/local/playability/render-debug-report.json`

Run the complete headless suite against the latest retained F12 capture:

```powershell
python .\tools\playability\render_debug_suite.py --pretty
```

This builds the presenter without opening a window, selects the latest valid
F12 `*-render-capture/render.json` (falling back to
`reports/local/live/render.json` when no retained capture exists), and writes
`render-stream-analysis.jsonl`, `render-stream-analysis-summary.json`, and the
correlated report under `reports/local/playability/`. Add `--skip-build` when
the presenter executable is already current, or `--render-manifest PATH` to
analyze a specific generation.

Regenerate the aggregate report without rerunning the game:

```powershell
python .\tools\playability\render_debug_report.py `
  --probe-summary .\reports\local\playability\native-live.json `
  --presenter-events .\reports\local\playability\render-debug-events.jsonl `
  --json-output .\reports\local\playability\render-debug-report.json `
  --pretty
```

The report correlates guest vertex and draw provenance with host reloads,
completed flips, texture state, per-draw NV2A inputs/constants/outputs, geometry
coverage, readbacks, native memory/cache counters, frame pacing, and presenter
hot paths. Findings are severity-ranked so a single report distinguishes a
guest transform failure from a viewport, texture, primitive, or Vulkan issue.
The report also retains the guest instruction, ESI value, step range, and
push-buffer range for each captured `c96..c99` position-matrix upload. To trace
collapsed transforms back through the title's RenderWare object input, run:

```powershell
python .\tools\playability\live_test.py `
  --audit-world-matrices
```

This opt-in observer records the `0x000C4570` matrix caller, source pointer,
owner candidate, basis determinant, raw matrix words, guest flip, and native
step. It also attributes `0x000C4A70` world mesh entries and their index counts
through the `0x00219750` D3D indexed-draw helper. Normal runs avoid its
per-object callback cost. Native reports also sample read-callback hotspots at
1/1024 frequency with negligible overhead.

The report covers per-draw sampler addressing, projective coordinates,
alpha-test/alpha-kill, fixed-function transforms, register combiners, fog,
multiple texture stages, empty texture payloads, canonical Xbox GPU address
aliases, replay of sampled off-screen targets, isolated depth ownership, and
retained F12 evidence. It compares required and active render-target feedback
addresses and rejects missing or stale cached targets.

Once the singular object address is known, capture only its 64-byte write range:

```powershell
python .\tools\playability\live_test.py `
  --audit-world-matrix-address 0x20D74D00 `
  --skip-host-build
```

The address option also enables the shader-input audit. It records the exact
writer instruction and before/after singularity state without changing matrix
contents. It also pairs the two Lesson One `RwMatrixRotate` calls with their
actual axes, angle, generated sine and `1-cos`, x87 state, and complete
input/output matrices. Normal runs do not install these callbacks. Dynamic-block
cache v1 records are re-decoded once from the matching XBE and atomically
rewritten as v2, so decoder fixes cannot leave stale lifted instructions in a
warmed run.

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
   Normal live mode places each acknowledged command interval in a bounded
   epoch sidecar while preserving its absolute stream offset.
4. The Vulkan presenter consumes each exact interval, carries split method
   packets and post-flip writes across epoch changes, retains unchanged
   resources and pipelines, presents at 60 Hz, and acknowledges it.
5. The launcher propagates presenter closure to the guest and performs bounded
   process-tree cleanup only if graceful shutdown fails.

Normal live presentation does not skip guest flips, truncate render work, or
substitute reduced-fidelity frames for performance. The guest and presenter
overlap their work through a cross-process publication/acknowledgement handshake.
After an acknowledgement, the producer rotates at that exact completed flip and
deletes a retired command/resource generation only after its successor is safe.
The manifest's `command_snapshot_base_record_count` and
`command_snapshot_record_count` keep all diagnostics and continuation checks in
absolute command space. Lossless-flip audit mode deliberately retains its
cumulative snapshots for standalone replay.

The presenter maps arrow keys, `S`, Backspace, Space/Enter, `B`, `X`, and `Y`
to Xbox D-pad, Start, Back, A, and B/X/Y state. Digital presses remain
published for at least 150 ms and two completed guest flips so short taps
survive both native guest slices and slow title input polling. Independent
per-key and per-button state preserves aliases and prevents overlapping keys
from extending each other, while a two-second fallback releases a latch if
guest flips stop.

## Current Validation Evidence

### Rendering fidelity

Lesson One now renders the complete world, HUD, and player car at the intended
chase-camera scale. Exact scene-record, camera, transform, command, resource,
and completed-flip audits agree on the presented draw population. Strict frozen
replays cover the rendering behaviors reached so far:

- Indexed vertex arrays, recovered vertex programs, inherited current vertex
  attributes, and inline primitives.
- Multi-level compressed textures, sampler filters and LOD bias, register
  combiners, fog, alpha test, blend equations, color masks, and dynamic
  scissors.
- Address-aliased render-target feedback with size-matched off-screen depth,
  restoring both the contact shadow and sun-projected car shadow.
- Active-stage alpha-kill and X8 opaque-alpha semantics, preserving foliage and
  generated shadow consumers.
- The opening `Loading - please wait` label, including its condensed Impact
  styling, antialiasing, completed-flip ownership, and guest-driven fade.

The loading animation has a bounded 106-flip proof under
`reports/local/flip-audit/loading-fade-live/`: producer and host sequences are
contiguous, every flip is acknowledged and strictly validated, and text opacity
follows the guest's 0-to-1-to-0 RGBA ramp.

### Runtime coverage

The resumable native executor reaches and runs Lesson One with frontend audio,
keyboard-backed Xbox input, local storage, streamed track data, and cooperative
guest workers. The dynamic-cache-seeded IA-32 audit covers 66,194 reachable
blocks and 392,161 instructions with no remaining decoder gaps in the observed
set. Native and interpreted regressions cover recovered x87, SSE/MMX, integer,
string, atomic, flag, and system forms.

Live publication uses immutable completed-flip generations and bounded epoch
sidecars. Frozen F12 captures retain the exact command prefix, referenced
resources, and interpreter bootstrap required for standalone replay. The
current full unit suite contains 413 passing tests.

### Performance

The July 14 presenter regression remains the clean 60 FPS pacing proof:

| Metric | Result |
| --- | --- |
| Guest instructions / completed flips | 12,000,000 / 494 |
| Publication ordering | Initial load + 493 distinct exact reloads |
| Presenter steady-state FPS | 59.900 |
| Frame p50 / p95 / p99 | 16.647 / 17.048 / 17.216 ms |
| Reload p50 / p95 / p99 | 0.775 / 1.026 / 1.305 ms |
| Command-load p50 / p99 | 0.109 / 0.288 ms |
| Presenter reload busy time | 3.73% |
| Shutdown | Guest, presenter, and launcher exited normally |

A later audited gameplay run spent 5.58% of wall time in presenter reload work,
with vertex transformation averaging 0.509 ms. Current optimization work is
therefore focused on guest execution and simulation throughput rather than
Vulkan presentation.

### Known limitations

- There is no complete save-state system.
- Fifteen car draws use a second reflection/cubemap texture stage that is
  diagnosed but not yet replayed.
- Corrected gameplay and Load/Save captures still need promotion into the
  checked-in strict preflight suite.
- Longer un-audited gameplay runs are still needed to characterize guest-side
  throughput and input/audio behavior.

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

1. Add the corrected complete-world frame and restored Load/Save frame to the
   strict preflight replay suite.
2. Measure normal un-audited gameplay throughput and profile the remaining
   guest simulation cost.
3. Design a versioned save state covering CPU state, sparse-memory changes,
   runtime objects, audio, and the live bridge.
4. Compact or replace the large JSON dynamic-block cache to reduce startup cost.
5. Measure longer-run input/audio behavior and replace remaining probe-only
   models with recovered semantics where evidence permits.
6. Continue adding IA-32, NV2A, and Xbox ABI support only for observed paths,
   with focused regressions for each new edge.
