# Debugging command reference

Run these commands from the repository root, `D:\b2_recomp`.

```powershell
$Xbe = '.\data\local\extracted\burnout_2_poi_usa\default.xbe'
$Extracted = '.\data\local\extracted\burnout_2_poi_usa'
```

## Choosing a workflow

Use the smallest diagnostic mode that answers the current question:

1. Reproduce the issue in a normal live run and press F12 on the first stable
   bad frame. Treat the BMP, sibling `*-render-capture` directory,
   `native-live.json`, presenter event log, and generated reports as one
   evidence set.
2. Run `render_debug_suite.py --skip-build --pretty`. It selects the newest
   valid standalone F12 bundle and distinguishes guest transform, viewport,
   texture, primitive, feedback, and Vulkan findings without rerunning the
   guest.
3. Use `--audit-scene-records` when draw counts, pointers, or scene ownership
   disagree. It stops after the first complete scene table.
4. Use `--audit-world-matrix-address ADDRESS` only after the report identifies
   a specific matrix. Broad matrix tracing is intentionally opt-in.
5. Use `--lossless-flip-audit` for transport and exact producer-boundary
   validation, not as the default visual-debug loop.

For throughput questions, start with an ordinary diagnostics-off warm run,
then regenerate `performance-debug-report.json`. Enable `--profile-hot-paths`
only when native-dispatch or callback attribution remains ambiguous; it has
measurable overhead.

Normal live diagnostics write:

- `reports/local/playability/native-live.json`
- `reports/local/playability/render-debug-events.jsonl`
- `reports/local/playability/render-debug-report.json`
- `reports/local/playability/performance-debug-report.json`

Native-run summaries retain the first 8 and latest 56 entries. Edge collection
is capped at 65,536 slots with at most 1,024 edges serialized, and callback
timing tracks at most 256 addresses. Reports disclose dropped records instead
of presenting a truncated trace as exact.

## Live runtime and manual capture

Normal diagnostic run:

```powershell
python .\tools\playability\live_test.py --skip-host-build
```

Minimal-overhead baseline:

```powershell
python .\tools\playability\live_test.py --no-diagnostics --skip-host-build
```

Bounded execution:

```powershell
python .\tools\playability\live_test.py --max-steps 1000000 --skip-host-build
```

Override the first-frame startup timeout:

```powershell
python .\tools\playability\live_test.py `
  --startup-timeout-seconds 1800 `
  --skip-host-build
```

Presenter hotkeys during a normal diagnostic run:

| Input | Function |
| --- | --- |
| F9 | Toggle completed-guest-flip FPS in the title |
| F11 | Write a timestamped metrics snapshot |
| F12 | Capture a BMP and standalone frozen render bundle |
| Escape | Stop the runtime |

The F12 bundle includes the exact command/resource generation, interpreter
bootstrap, and `render.json`. Capture validation checks its physical
append-only record count and bootstrap before a frozen replay is trusted.

## Live performance and A/B modes

Hot-path profiling:

```powershell
python .\tools\playability\live_test.py `
  --profile-hot-paths `
  --skip-host-build
```

Lock-step pipeline comparison:

```powershell
python .\tools\playability\live_test.py `
  --presentation-pipeline-depth 1 `
  --skip-host-build
```

Normal live execution uses presentation depth two. Depth one is for focused
handshake comparisons and is mandatory for lossless flip audits.

CPU vertex-program reference path:

```powershell
python .\tools\playability\live_test.py `
  --cpu-vertex-programs `
  --skip-host-build
```

CPU indexed-attribute reference path:

```powershell
python .\tools\playability\live_test.py `
  --cpu-vertex-attributes `
  --skip-host-build
```

CPU texture-conversion reference path:

```powershell
python .\tools\playability\live_test.py `
  --cpu-texture-conversion `
  --skip-host-build
```

Developer-only live frontier compilation:

```powershell
python .\tools\playability\live_test.py `
  --developer-live-compile `
  --skip-host-build
```

The last command materially changes runtime behavior and is unsuitable for a
baseline.

## Exact guest/render audits

Compact scene-record audit:

```powershell
python .\tools\playability\live_test.py `
  --audit-scene-records `
  --skip-host-build
```

Broad RenderWare world-matrix audit:

```powershell
python .\tools\playability\live_test.py `
  --audit-world-matrices `
  --skip-host-build
```

Exact 64-byte matrix write audit:

```powershell
python .\tools\playability\live_test.py `
  --audit-world-matrix-address 0x20D74D00 `
  --skip-host-build
```

The broad audit records matrix callers, source/owner candidates, basis state,
guest flip, and native step, and attributes world-mesh entries to indexed
draws. The exact-address form adds writer before/after state and shader inputs
for one matrix. Both install per-object callbacks and are unsuitable for a
representative performance run.

Lossless completed-flip audit:

```powershell
python .\tools\playability\live_test.py `
  --lossless-flip-audit `
  --skip-host-build
```

The guest records every producer boundary. Health-selected flips are published
with their exact command/resource prefix, strictly rendered and read back, and
then acknowledged. Outputs are written under `reports/local/flip-audit/`.

Bound it to a fixed number of flips:

```powershell
python .\tools\playability\live_test.py `
  --lossless-flip-audit `
  --flip-audit-max-flips 100 `
  --skip-host-build
```

Audit every flip rather than candidate/interval sampling:

```powershell
python .\tools\playability\live_test.py `
  --lossless-flip-audit `
  --flip-audit-health-interval 1 `
  --skip-host-build
```

## Render-debug reports and replay

Run the complete headless render-debug suite against the latest F12 bundle:

```powershell
python .\tools\playability\render_debug_suite.py --pretty
```

Reuse the current presenter build:

```powershell
python .\tools\playability\render_debug_suite.py `
  --skip-build `
  --pretty
```

Select a particular frozen capture:

```powershell
python .\tools\playability\render_debug_suite.py `
  --render-manifest <path-to-render.json> `
  --skip-build `
  --pretty
```

Regenerate the aggregate render report without rerunning:

```powershell
python .\tools\playability\render_debug_report.py `
  --probe-summary .\reports\local\playability\native-live.json `
  --presenter-events .\reports\local\playability\render-debug-events.jsonl `
  --json-output .\reports\local\playability\render-debug-report.json `
  --pretty
```

Regenerate the performance report:

```powershell
python .\tools\playability\performance_debug_report.py `
  --probe-summary .\reports\local\playability\native-live.json `
  --json-output .\reports\local\playability\performance-debug-report.json `
  --pretty
```

The performance report separates guest flip rate, host waits, and active guest
time; ranks native, cache, callback, and presenter boundaries; and distinguishes
initial, synchronous incremental, and background frontier compilation.

Replay a frozen render stream:

```powershell
python .\tools\host\first_frame_smoke.py `
  --max-frames 3 `
  --render-stream-json .\reports\local\render\recovered-d3d-stream.json `
  --pretty
```

Analyze a render stream without Vulkan or a window:

```powershell
python .\tools\host\first_frame_smoke.py `
  --render-stream-json <path-to-render.json> `
  --analyze-render-stream `
  --pretty
```

Strict resource/render validation:

```powershell
python .\tools\host\first_frame_smoke.py `
  --render-stream-json <path-to-render.json> `
  --strict-render-validation `
  --max-frames 3 `
  --pretty
```

The same CPU A/B flags are available on `first_frame_smoke.py`:
`--cpu-vertex-programs`, `--cpu-vertex-attributes`, and
`--cpu-texture-conversion`.

The aggregate render report correlates guest draw and vertex provenance with
host reloads, completed flips, texture state, NV2A inputs/constants/outputs,
geometry coverage, readbacks, frame pacing, and native cache counters. It also
compares required and active render-target feedback addresses so stale camera
or shadow targets become explicit findings.

## Direct guest probe

Bounded native probe:

```powershell
python .\tools\playability\playability_probe.py `
  $Xbe `
  --extracted-root $Extracted `
  --dynamic-block-cache .\build\native-guest-loop\decoded-blocks.sqlite3 `
  --native-guest-loop `
  --max-steps 1000000 `
  --json-output .\reports\local\playability\native-loop-1m.json
```

Direct-probe-only diagnostic switches:

| Switch | Purpose |
| --- | --- |
| `--audit-title-main-loop-exit` | Instrument exact writes to the main-loop exit flag |
| `--render-watchpoint-start N` | Skip the first N observed D3D writes |
| `--render-watchpoint-limit N` | Stop after retaining N D3D writes |
| `--no-execute-entry` | Decode the entry prefix without executing |
| `--profile-hot-paths` | Exact module edges and sampled callback timing |
| `--audit-world-matrices` | Broad world-matrix observer |
| `--audit-world-matrix-address ADDRESS` | Exact matrix-write and shader-input audit |
| `--audit-scene-records` | Compact scene/source audit |
| `--developer-live-compile` | Enable runtime frontier compilation |

Decoded blocks are stored as individually compressed records in
`build/native-guest-loop/decoded-blocks.sqlite3`; a lookup decodes only the
requested block. Normal runs persist new frontier information for the next AOT
build without compiling it in the active process.

## ETW/WPA profiling

Inventory and hash profiler installations:

```powershell
python .\tools\profiling\system_profile.py doctor --pretty
```

Preview the ETW command without launching:

```powershell
python .\tools\profiling\system_profile.py capture-etw --dry-run
```

Capture CPU and GPU ETW data from an Administrator PowerShell:

```powershell
python .\tools\profiling\system_profile.py capture-etw
```

CPU-only ETW capture:

```powershell
python .\tools\profiling\system_profile.py capture-etw --cpu-only
```

Force a presenter rebuild before capture:

```powershell
python .\tools\profiling\system_profile.py capture-etw --rebuild-presenter
```

Open a retained trace:

```powershell
wpa .\reports\local\profiling\etw-YYYYMMDD-HHMMSS\b2-gameplay.etl
```

```powershell
& 'C:\Program Files (x86)\Windows Kits\10\Windows Performance Toolkit\gpuview\GPUView.exe' `
  .\reports\local\profiling\etw-YYYYMMDD-HHMMSS\b2-gameplay.etl
```

## RenderDoc profiling

Preview the launch:

```powershell
python .\tools\profiling\system_profile.py capture-renderdoc --dry-run
```

Capture a frame:

```powershell
python .\tools\profiling\system_profile.py capture-renderdoc
```

Force a presenter rebuild first:

```powershell
python .\tools\profiling\system_profile.py capture-renderdoc --rebuild-presenter
```

Open a capture directly:

```powershell
& 'C:\Program Files\RenderDoc\qrenderdoc.exe' `
  <path-to-capture.rdc>
```

Under the wrapper's diagnostics-off mode, F12 belongs to RenderDoc rather than
the application screenshot handler.

## Preflight and regression gates

Asset-free synthetic preflight:

```powershell
python .\tools\playability\preflight_validation.py `
  --synthetic-fixtures `
  --pretty
```

Strict local captured suite:

```powershell
python .\tools\playability\preflight_validation.py `
  --extracted-root $Extracted `
  --suite .\tools\playability\preflight_suite.json `
  --run-replays `
  --probe-summary .\reports\local\playability\native-live.json `
  --json-output .\reports\local\preflight\full-report.json `
  --pretty
```

Compare early and late boot checkpoints:

```powershell
python .\tools\playability\frontend_boot_gate.py `
  --early <early-probe.json> `
  --late <late-probe.json> `
  --json-output .\reports\local\playability\frontend-boot-gate.json `
  --pretty
```

Complete asset-free quality gate:

```powershell
python .\tools\quality_gate.py --full
```

Native debug/profiling/sanitizer checks:

```powershell
python .\tools\native_build.py --preset debug
python .\tools\native_build.py --preset profiling
python .\tools\native_build.py --preset sanitizer
python .\tools\native_build.py --preset release --presenter
```

## Static inspection and provenance

Exact supported-XBE identity:

```powershell
python .\tools\project_identity.py $Xbe --pretty
```

XBE structure:

```powershell
python .\tools\xbe\xbe_info.py $Xbe --pretty
```

Loaded-image mapping:

```powershell
python .\tools\loader\xbe_loader.py $Xbe --pretty
```

Analysis database:

```powershell
python .\tools\analysis\analysis_db.py `
  $Xbe `
  --output .\reports\local\analysis-db.json `
  --pretty
```

Runtime binding smoke test:

```powershell
python .\tools\runtime\runtime_smoke.py `
  $Xbe `
  --extracted-root $Extracted `
  --pretty
```

IA-32 coverage audit:

```powershell
python .\tools\recomp\audit_x86_coverage.py `
  $Xbe `
  --dynamic-block-cache .\build\native-guest-loop\decoded-blocks.sqlite3 `
  --json-output .\reports\local\recomp\x86-coverage-audit.json `
  --pretty
```

## Unsafe research overrides

`--allow-unsupported-xbe` and `--allow-stale-artifacts` exist for unsupported
research. Both conspicuously invalidate compatibility or performance evidence.

There is not yet a Dear ImGui debugger or overlay; that remains P4.
