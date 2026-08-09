# Debugging command reference

Run these commands from the repository root.

```powershell
$Xbe = '.\data\local\extracted\burnout_2_poi_usa\default.xbe'
$Extracted = '.\data\local\extracted\burnout_2_poi_usa'
```

## Current Phase 7 checkpoint

The stable static artifact is
`ff8db42895b7970b554b9aa9fadaf62e82305e0946ca6f63b55b8ebd577936a4`.
Its exact manifest has zero guarded boundaries, 140 registered native services,
zero pending services/frontier activity/Python callbacks, and no runtime
compilation, decoding, patching, promotion, raw-XBE execution, or automatic
fallback. The offline audit resolves 170 baseline guards, adds none, and leaves
zero while passing the eight-optimization budget.

The earlier zero-guard artifact could open a black presenter window and exit
with Windows code `3221225477` (`0xC0000005`). Its normal-live vblank
start/exit thunks overlapped audited service-body slots 112 and 128; the
scheduler's service-table reinstall replaced the first 44 bytes of both context
switches. Normal-live control thunks now occupy isolated slots
`0x50002100-0x50002400` in the reserved relay page, below the service-body
region at `0x50003000`. A layout regression test prevents recurrence.

The replacement artifact was manually confirmed to render and continue beyond
the former first-vblank crash. The user stopped that diagnostic run, so it is
correctness evidence rather than a completed native-clean performance report.
The next check remains a manual diagnostics-off gameplay/performance run using
the stable index, followed by regeneration of `performance-debug-report.json`.
Do not automate input or infer an FPS gain from the static audit alone.

## Offline Phase 7 boundary sweep

Inventory and re-resolve the complete normal-live guard set from the retained
capsule, decoded block store, XBE import table, and coverage profile without
launching the game:

```powershell
python -m tools.recomp.audit_ia32_boundaries `
  .\reports\local\replay\lesson-one-phase7-observed-run-4.b2rcap `
  --xbe .\data\local\extracted\burnout_2_poi_usa\default.xbe `
  --decoded-block-store .\build\native-guest-loop\decoded-blocks.sqlite3 `
  --coverage-profile .\reports\local\replay\boot-to-lesson-one-coverage-profile.json `
  --baseline-manifest .\build\local\ia32-live\f1f79ee62ed881dee662c60f67b380ccc292825f26b7a3cd0a07ea9cd0dda8f7\manifest.json `
  --candidate-manifest .\build\local\ia32-live\manifest.json `
  --report .\reports\local\replay\phase7-offline-boundary-audit.json
```

The report names every resolved site and proof source, groups any remaining
sites into finite work batches (kernel import, callback register, indexed
table, virtual slot, or direct edge), and never injects input or executes guest code. The
candidate manifest is required for acceptance because its complete recursive
guard inventory includes downstream direct exits exposed by the new target
closure. The audit also enforces the Phase 7 performance budget: each complete
batch of 20 newly resolved guards requires another credited native runtime
optimization present in that exact artifact. An audit fails instead of
recording target coverage when that budget is not met.

The latest accepted report records 170 resolved guards, zero added, zero
remaining, and eight newly credited generated-runtime optimizations.

## Choosing a workflow

Use the smallest diagnostic mode that answers the current question:

1. Reproduce the issue with manual input. If the guest stops, inspect
   `native-live.json` before classifying the retained presenter frame as a
   render freeze. For a visual defect, press F12 on the first stable bad frame
   and treat the BMP, sibling `*-render-capture` directory,
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
only when native-dispatch attribution remains ambiguous. Its F10 capture window
has measurable overhead and is not the baseline FPS measurement.

Normal live diagnostics write:

- `reports/local/playability/native-live.json`
- `reports/local/playability/render-debug-events.jsonl`
- `reports/local/playability/render-debug-report.json`
- `reports/local/playability/performance-debug-report.json`

For Driver's Ed demonstration failures, `native-live.json` also retains
`native_title_asset_open_events` (guest path, header words, read totals, and
close count) and `native_replay_state_samples` (the replay controller state for
each published guest flip). These native-only records correlate a `DReplay`
load with the exact How to Play interval; an additional F12 capture is not
needed when the guest's black clear and HUD-only draw set are already proven.

Native-run summaries retain the first 8 and latest 56 entries. Edge collection
and serialization retain every populated entry up to the 262,144-slot table
capacity. Reports disclose table overflow instead of presenting a truncated
trace as exact.

## Live runtime and manual capture

Default diagnostic-oracle run:

```powershell
python .\tools\playability\live_test.py --skip-host-build
```

Fail-closed IA-32 normal-live preflight/launch:

```powershell
python .\tools\playability\live_test.py `
  --guest-backend same-isa-ia32 `
  --ia32-artifact .\build\local\ia32-live\manifest.json `
  --skip-host-build
```

This path accepts only a content-verified boot-entry artifact with a dynamic
native scheduler and native control/command/resource/audio ownership. The
retained Lesson One full-flip artifacts are fixed replay plans and are expected
to fail this preflight; use the command only after producing a normal-live
artifact. The default 64-bit path remains the diagnostic oracle during that
bring-up and never becomes an automatic fallback from an IA-32 launch.

If the static guest exits while the presenter keeps showing its last frame,
read the dependency-free summary:

```powershell
Get-Content -Raw .\reports\local\playability\native-live.json
```

`0x80000003` at a guest instruction address normally identifies an unverified
indirect transfer emitted as a fail-closed guard. Use `debug_metadata.py`, then
an x86 CDB capture when live registers or object/vtable contents are required.
Do not patch the normal artifact to continue past the site.

Minimal-overhead baseline:

```powershell
python .\tools\playability\live_test.py --no-diagnostics --skip-host-build
```

Bounded execution:

```powershell
python .\tools\playability\live_test.py --max-steps 1000000 --skip-host-build
```

`--max-steps` is a diagnostic instruction cap, not a play-time timeout. When it
is exhausted, guest publication stops and the presenter can remain interactive
on the last frame until it is closed. Confirm `run-manifest.json` and
`native-live.json` before classifying that display as a guest crash or wait.
For manual play, omit `--max-steps`.

Gameplay navigation for retained evidence is manual. Do not use input injection
to move through the title. Bounded instruction limits and debugger breakpoints
are acceptable when they do not synthesize game input.

The fixed live scheduler snapshot also exposes the native DirectSound buffer
`Play` stage and its last buffer metadata. `audio_buffer_play_stage` values 1-3
cover mirror resolution and decode entry, 4 means decode completed, 5 means the
SDL output is open, 6 means playback was queued, 20 means the payload or format
was rejected, and 21 means output initialization failed. The companion fields
are `audio_last_buffer`, `audio_last_data`, `audio_last_size`, and
`audio_last_sample_rate`. These fields occupy the reserved diagnostic header
and do not publish controller input or alter guest execution.

The post-run `normal_runtime.audio` summary also retains voice-lifetime and
quality evidence. `buffer_get_status_count`, `buffer_completion_count`,
`buffer_loop_wrap_count`, and `stale_playing_repair_count` distinguish a title
stop from host retirement and stale guest voice state. The fixed conversion
path reports `sample_rate_conversion` and `pitch_conversion` as
`linear_interpolation`; `linear_resampled_frame_count` and
`pitch_interpolated_frame_count` prove that the corresponding paths were
exercised. `mixer_accumulation` is `int32_clamp_after_sum`, avoiding
order-dependent per-voice saturation. A stale-playing repair is retained
instead of allowing a completed or rejected voice to remain permanently
playing in guest-visible status.

Override the first-frame startup timeout:

```powershell
python .\tools\playability\live_test.py `
  --startup-timeout-seconds 1800 `
  --skip-host-build
```

Presenter hotkeys during a normal diagnostic run:

| Input | Function |
| --- | --- |
| F8 | Trigger an armed replay-capsule capture |
| F9 | Toggle completed-guest-flip FPS in the title |
| F10 | Start or stop an armed native hot-path capture |
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
  --profile-hot-paths
```

The run starts with capture paused and renders through the normal native path.
Navigate to the section you want to measure, press F10 once to start, exercise
the section for 20-30 seconds, press F10 again to stop, then close with Escape.
The window title shows `ARMED`, `ACTIVE`, and `COMPLETE`. Guest module targets,
exact entry-to-exit edges, sampled native target time, presenter CPU stages,
and GPU timestamps are restricted to the same capture window. Target timing
samples the first call and every 256th call while call, step, exit-reason, and
edge counts remain exact. Native host-service targets use the same sampling
schedule, and guest targets are attributed exactly to primary, cooperative
worker, or vblank execution. The report includes timing dispersion and a 95%
margin of error, estimated time and calls per completed flip, strongly
connected hot cycles, branch probabilities/entropy, frame-budget shares, and
an estimate of profiling bookkeeping cost. It writes sortable targets,
transitions, cycles, and native-service CSVs under
`reports/local/playability/hot-path-tables/`. Capture acceptance uses
capture-window callbacks and guest flip counters; whole-session values remain
visible separately. The performance report rejects the sample if the window
observes Python runtime callbacks, live compilation, frontier interpretation,
native promotion, disabled native observer dispatch, or an empty/incomplete
capture.

Full manual boot-to-Lesson-One coverage window:

```powershell
python .\tools\playability\live_test.py `
  --profile-boot-path `
  --skip-host-build
```

This diagnostic starts the native capture at XBE entry. Manually navigate
through the frontend into Lesson One, press F10 once to seal the already-active
window, then close with Escape. It batches boot targets, transitions, native
services, lane attribution, render exits, and static-clean counters without
injecting input. Use this wider trace to build the normal-live IA-32 artifact;
do not use its whole-session timings as the steady Lesson One performance
window.

To isolate the AOT dispatch and guest-state changes, add one explicit
`--aot-ab-mode` value to the profiling command:

```powershell
# Pre-optimization emitter and generic static call fusion.
python .\tools\playability\live_test.py --profile-hot-paths --aot-ab-mode baseline

# Measured preferred superblocks with context-resident guest state; this is
# the accepted default normal AOT configuration.
python .\tools\playability\live_test.py --profile-hot-paths --aot-ab-mode fusion-only

# Original partitioning with registerized guest state, run-level budget checks,
# and same-page callback rejection.
python .\tools\playability\live_test.py --profile-hot-paths --aot-ab-mode registerization-only

# Both optimizations; retained to diagnose registerization regressions.
python .\tools\playability\live_test.py --profile-hot-paths --aot-ab-mode combined
```

Each mode has a distinct native-module configuration identity. Launch each arm
once to populate or adopt its AOT artifacts, exit after preparation, then
launch the same command again for the measured warm capture. Use the same scene,
input, and F10 duration for every arm. A warm arm reports zero compile/source
emission time and a `warm_hit_count` equal to
`known_reachable_partition_count`. The run manifest records
`configuration.aot_optimization_mode`; the native cache summary also records
the mode and the two resolved feature switches. Modes other than the
`fusion-only` default are developer profiling diagnostics and require
`--profile-hot-paths`.

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

## Exact guest/render audits

Compact scene-record audit:

```powershell
python .\tools\playability\live_test.py `
  --audit-scene-records `
  --skip-host-build
```

Native traffic/world-mesh submission audit:

```powershell
python .\tools\playability\live_test.py `
  --audit-traffic-meshes `
  --skip-host-build
```

This retains a bounded ring of world-draw callers, mesh entries, index buffers,
and index counts entirely inside the native runtime. It preserves the normal
renderer and controls, unlike the callback-heavy world-matrix audits below.

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

For reflection and environment-map failures, inspect the report's
`cubemap_texture_stage_draw_count`,
`cubemap_texture_stage_coverage_mismatch_draw_count`, and
`texture_stage_coverage_mismatch_draw_count` fields. A complete replay keeps
the latter two at zero even when a draw uses multiple texture stages.

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
  --tables-directory .\reports\local\playability\hot-path-tables `
  --pretty
```

The performance report separates guest flip rate, host waits, and active guest
time; ranks native, cache, callback, and presenter boundaries; and distinguishes
initial, synchronous incremental, and background frontier compilation. To rank
per-target regressions against a retained controlled capture, preserve the old
report and add:

```powershell
  --compare-to .\reports\local\playability\baseline-performance-report.json
```

The comparison normalizes guest modules by estimated native time per second,
then reports per-flip deltas when both captures contain completed guest flips.
`comparison.csv` is added to the tables directory.

Presenter reload events also expose the item-4 command-work cache. Inspect
`performance.presenter.method_interpretation.command_work_cache` for its
cacheable reloads, whole-reload and reconstruction-window hit ratios, lookup
time, materialized word count, builds, fallbacks, evictions, epoch changes, and
bounded resident size. The adjacent
method summary reports bulk state methods and exact no-op state writes. Command loading splits
the shared-memory path into provenance, vector-resize, payload-copy, and
record-validation timings. Cache entries are scoped to the command-stream
generation. MMIO/backward-delimited reconstruction segments are partitioned
into shift-invariant 16 KiB destination windows; current guest addresses and
payload values are always read on a hit. The retained 256-plan lookup uses a
normalized-hash collision bucket with exact structural comparison and a
list-backed LRU; the 64 MiB byte cap remains authoritative.

Capture the diagnostic-only structural cache trace during the same manual F10
workload used for item-4 comparisons:

```powershell
python .\tools\playability\live_test.py `
  --profile-hot-paths `
  --command-work-cache-trace `
    .\reports\local\playability\command-work-cache-trace.jsonl
```

The trace records normalized window layout IDs, actual retained plan byte
sizes, LRU stack reuse distances, eviction ages, and the descriptor-only
reconstruction segments needed for alternate window simulations. It contains
no command payload values. Trace collection adds diagnostic work and is not a
performance-acceptance run. Start and stop the workload with F10 manually; do
not automate gameplay input.

After closing the run, simulate the handoff matrix offline:

```powershell
python .\tools\playability\command_work_cache_trace.py `
  --trace .\reports\local\playability\command-work-cache-trace.jsonl `
  --presenter-events `
    .\reports\local\playability\render-debug-events.jsonl `
  --json-output `
    .\reports\local\playability\command-work-cache-simulation.json `
  --window-kib 4 8 16 32 `
  --plan-limits 256 512 1024 `
  --pretty
```

New traces carry the F10 state directly. `--presenter-events` supplies the
equivalent scope for a trace captured before that field was added; omit it for
new traces. The report includes captured plan-size/reuse-distance distributions and a
collision-safe exact-layout LRU simulation for every requested window/limit
pair under the existing 64 MiB byte cap. Use the simulation to decide whether
the cache warrants a hash-indexed collision bucket plus LRU and a higher plan
limit; validate any chosen runtime change in a separate trace-disabled warm
`fusion-only` F10 run.

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

For a large F12 capture, retain only complete command spans from its tail and
pair them with the capture's interpreter bootstrap before running the analyzer:

```powershell
python .\tools\playability\render_capture_tail.py `
  <capture-render.json> `
  .\reports\local\playability\render-tail `
  --tail-bytes 4194304
```

The generated `render.json` is suitable for the analysis command above. This
keeps late-frame material state, including secondary texture-stage and cubemap
bindings, without replaying the capture's complete command prefix.

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
  --decoded-block-store .\build\native-guest-loop\decoded-blocks.sqlite3 `
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
| `--profile-hot-paths` | F10-windowed exact native edges, sampled target timing, and profiling-cost estimate |
| `--aot-ab-mode MODE` | Profile `baseline`, `fusion-only`, `registerization-only`, or `combined` AOT emission; modes other than the `fusion-only` default require `--profile-hot-paths` |
| `--audit-traffic-meshes` | Native bounded world-mesh submission ring |
| `--audit-world-matrices` | Broad world-matrix observer |
| `--audit-world-matrix-address ADDRESS` | Exact matrix-write and shader-input audit |
| `--audit-scene-records` | Compact scene/source audit |
| `--developer-live-compile` | Diagnostic-only runtime frontier compilation; never use for normal evidence |

Decoded blocks are stored as individually compressed records in
`build/native-guest-loop/decoded-blocks.sqlite3`; a lookup decodes only the
requested block. A normal run records an unknown executable target and stops;
it does not decode or continue it. An explicit diagnostic-discovery run may
persist the target to the decoded block store, after which the next static AOT
build consumes it. Known callback tables are recovered as complete families
during artifact preparation so adjacent members do not require one failing run
apiece. Preparation also proves contiguous `.rdata` and `.data` code-pointer
families plus embedded data islands in other file-backed XBE sections from raw
return/alignment boundaries, retains the complete family, and rejects packed
numeric runs. Headers and zero-fill tails are never scanned. Static data
sections are not treated as code by jump-table closure or the IA-32 coverage
audit merely because their XBE section flags include execute.

After the canonical records for an image are materialized, the store keeps a
versioned, interpreter-tagged prepared snapshot in compact chunks. The snapshot
is invalidated whenever decoded content changes; it only avoids rebuilding the
same in-memory lifted IR on later AOT preparations. Inspect
`decoded_block_store.prepared_snapshot_hits`, `prepared_snapshot_load_us`, and
`prepared_snapshot_build_us` to distinguish snapshot loading from a rebuild.

The native module manifest keys each partition by semantic lifted content and
its module-local callback, observer, fast-path, and preferred-superblock
configuration. Measured deterministic edges can deliberately co-locate their
owners even when an owner contains a native fast path; all other fast-path
owners retain localized rebuilds. Exact generated-source matches can adopt an
existing DLL even when conservative emitter identity changes. Resumable
modules larger than 2,048 instructions are emitted as bounded internal
functions in the same DLL, limiting Clang optimizer latency without adding a
runtime compilation or cross-DLL dispatch boundary.

Within a generated module, hot guest GPRs, flags, steps, and the step budget
remain in module-local state. They are committed at module, callback, yield,
fault, and host-ABI boundaries; memory-observation sites synchronize exact EIP
and step provenance. Direct-fallthrough runs reserve their safe step allowance
with one budget check instead of rereading the context for every instruction.
The original registerization A/B capture also owned same-page callback
rejection. The accepted post-split emitter now applies that independent check,
plus coalesced same-page 16-bit and 64-bit accesses, without enabling guest-state
registerization. `baseline` alone retains the prior per-byte callback-page and
scalar memory path.

The targeted hot-block package extends the preferred set to 11 measured edges.
It co-locates the world-state continuation, indexed-state slow/return chain,
and matrix finite-check call with their owners. Guarded native bodies cover the
16-byte, 64-byte, and variable push-buffer copy routines, resource binding,
the common deferred-state flush, and the finite-check leaf. Unsupported state,
capacity pressure, callback-sensitive memory, and uncommon resource cleanup
retain the generated guest path. The indexed-draw continuation also consumes
the known wrapper leaf `ret` only when the step budget and yield state permit.
Use `native_module_cache.preferred_fusion_edge_count`,
`available_preferred_fusion_edge_count`, and
`colocated_preferred_fusion_edge_count` to confirm AOT superblock coverage. Use
`warm_hit_count`, `equivalent_source_reuse_count`, `ahead_compiled_count`,
`source_emit_us`, `compile_wall_us`, and `compiler_process_us` to separate cache
lookup, emission, wall-clock build time, and summed parallel compiler work.

A guest that exits before the first frame is a failure even if its nested
runner returned zero. Fatal entry or guest-thread execution also makes the
probe itself return nonzero, and the launcher reports the final preparation
state instead of saying that an exited guest is still preparing.

Immediate code-looking values must not be seeded generically from `push` or
register `mov` instructions: executable library sections contain interleaved
strings and packed data that can resemble function boundaries. Stack-passed
callbacks are recovered only for declared callback-consuming ABIs. The CRT
vector-constructor helper at `0x000134D0`, for example, consumes argument 3 as
an element-constructor callback. Preparation traces a bounded same-block push
sequence backwards from each direct consumer call, permits intervening
instructions only while they preserve the CPU stack and linear control flow,
and verifies the executable target before adding it to the AOT closure.
The linked-list iterator at `0x000FD810` similarly consumes argument 1 as its
visitor callback, so all direct callers contribute their complete visitor
family during the same preparation pass. The spatial-query consumer at
`0x0008F3D0` consumes argument 2; its callers interleave floating-point and
register work with argument pushes, which the stack-safe trace accepts without
weakening recovery into a generic immediate scan. The related spatial-collision
consumer at `0x00090420` also consumes argument 2; all 13 of its direct
callsites contribute their complete four-visitor family.

The frontend-card constructor at `0x0001D180` consumes argument 7 as a callback
but receives it through a register push after two reserved floating-point
stack slots. Its declared ABI recovery traces that stack layout, then collects
the bounded branch-family definitions of the pushed register. This seeds the
`0x00024440`, `0x00025150`, and `0x00025A10` card callbacks before the AOT build
without treating arbitrary register immediates as code.

Normal live play paces completed guest publications to 60 Hz in the native
runtime. With the depth-two presentation pipeline, the presenter may consume
and acknowledge one completed publication while waiting for the current frame
deadline; this avoids quantizing a narrowly missed loop-boundary probe down to
30 FPS. The remainder of that deadline wait does not consume another
publication, so the guest cannot advance twice per displayed-frame interval.

Bounded runs use separate guest and presenter processes. On Windows both are
assigned to a kill-on-close job, so closing or losing the launcher cannot leave
an executing recomp child behind; the usual graceful stop and timeout cleanup
still run first.

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

### Deterministic replay capsules

To capture the indexed-draw prototype boundary, start the live diagnostic run:

```powershell
python .\tools\playability\live_test.py `
  --capture-replay-capsule .\reports\local\replay\scheduler-boundary.b2rcap `
  --capture-replay-entry 0x000C5550 `
  --capture-replay-scheduler-stop 0x000C5570
```

Navigate manually to the representative Lesson One scene. Press F8 once to
request the replay checkpoint. The first subsequent execution of `0x000C5550`
captures CPU state, sparse pages, the decoded program, native scheduler/service
state, event/provenance records, and verified XBE instruction bytes, then stops
the run. With `--capture-replay-scheduler-stop`, the capsule also contains a
Phase-5 `resident_scheduler` plan for the real bounded primary slice. Any live
worker/vblank contexts are retained as terminal snapshots; the capture does not
invent wakeups or lane work that did not occur inside the boundary. Do not press
F8 at the title/menu: the capture is intentionally scoped to the gameplay
window, and no command automates input.

For Phase-7 full-flip acceptance, capture the same entry through the measured
Lesson One render boundary and classify that observed boundary as a flip:

```powershell
python .\tools\playability\live_test.py `
  --capture-replay-capsule .\reports\local\replay\lesson-one-phase7-full-flip.b2rcap `
  --capture-replay-entry 0x000C5550 `
  --capture-replay-scheduler-exit flip
```

Navigate to the stable Lesson One workload and press F8 once. The run stops
after the next completed flip and writes the capsule. This diagnostic mode
retains the entry CPU/memory checkpoint, observes the real native scheduler
through that flip, and stores its terminal CPU state and changed pages as the
acceptance oracle. It does not synthesize a direct edge to the render module,
invent worker/vblank execution, or automate gameplay.

For an already serialized host-boundary checkpoint specification, the lower
level packer remains available:

```powershell
python .\tools\playability\replay_capsule.py capture `
  --spec .\reports\local\replay\manual-checkpoint.json `
  --output .\reports\local\replay\before-flip.b2rcap
python .\tools\playability\replay_capsule.py inspect `
  .\reports\local\replay\before-flip.b2rcap
```

The spec must declare `provenance.manual_capture=true`. Proprietary output is
accepted only below `reports/local/` or `data/local/`. To compare the accepted
interpreter with native AOT and preserve a last-matching failure capsule:

```powershell
python .\tools\playability\differential_replay.py execute `
  .\reports\local\replay\before-flip.b2rcap `
  --experimental native `
  --max-steps 20000 `
  --build-dir .\build\local\replay\native `
  --failure-capsule .\reports\local\replay\first-divergence.b2rcap `
  --report .\reports\local\replay\first-divergence.json
```

Prototype the fixed-endpoint same-ISA IA-32 backend on a capsule that contains
only the verified compute slice. For the first performance proof, capture with
entry EIP `0x000C5550` and stop before `0x000C5570`:

```powershell
python .\tools\recomp\ia32_native_backend.py execute `
  .\reports\local\replay\indexed-draw-slice.b2rcap `
  --stop-eip 0x000C5570 `
  --build-dir .\build\local\ia32 `
  --report .\reports\local\replay\indexed-draw-ia32.json
```

This diagnostic command builds a content-addressed 32-bit PE before launching
it, maps the declared sparse pages at their guest addresses, and executes only
the capsule's verified instruction bytes. It rejects unpatched external or
indirect transfers, privileged operations, high/MMIO accesses, FS/TLS state,
and stack returns outside the selected stop boundary as named static rewrite
gaps. It is not a normal-play fallback and performs no runtime decoding or
compilation.

Run the asset-free Phase-0 contract gate directly:

```powershell
python .\tools\recomp\ia32_proof_contract.py synthetic `
  --build-dir .\build\local\ia32-phase0 `
  --report .\build\local\ia32-phase0\phase0-proof-report.json
```

The gate independently rebuilds two deterministic capsules and IA-32 PE
artifacts, then compares decoded-reference and hardware execution for a compute
slice and an `RtlEnterCriticalSection` thunk. It freezes the exchange layout,
entry/exit and host-service thunk ABIs, QPC timing interval, toolchain identity,
and architectural comparison policy. Any mismatch uses the differential replay
diagnostic format with register and bounded page differences. `dev_check.py`
selects this node for IA-32, replay/differential, lifter, and toolchain changes.

Seal the retained proprietary compute and host-service proofs with the `freeze`
subcommand. Pass both capsule paths and both artifact directories, plus their
expected capsule/artifact IDs, and write `--report` below `reports/local/` or
`data/local/`. The report records the historical MMX/x87 alias exclusion and
MXCSR sticky-status delta explicitly; neither is mislabeled as a reserved bit.
The retained artifacts and report remain ignored local evidence.

Run the Phase-1 resident-worker gate directly:

```powershell
python .\tools\recomp\ia32_proof_contract.py persistent `
  --build-dir .\build\local\ia32-phase1 `
  --report .\build\local\ia32-phase1\phase1-proof-report.json
```

This launches one 32-bit process per proof artifact and sends three versioned
dispatch commands through it. The report separates worker startup from each
resident dispatch and records process reuse, reset isolation, architectural
hashes, and deterministic rebuild identity. A mismatch includes compact
differential state/page diagnostics. Worker timeout, crash, and protocol
failures identify the artifact and last published guest EIP.

Run the Phase-2 decoded-store artifact gate directly:

```powershell
python .\tools\recomp\ia32_proof_contract.py decoded-store `
  --build-dir .\build\local\ia32-phase2 `
  --report .\build\local\ia32-phase2\phase2-proof-report.json
```

Build a complete artifact from a captured boundary, the matching XBE, and the
normal decoded block store:

```powershell
python .\tools\recomp\ia32_native_backend.py build-store `
  .\reports\local\replay\boundary.b2rcap `
  --xbe .\data\local\extracted\burnout_2_poi_usa\default.xbe `
  --decoded-block-store .\build\native-guest-loop\decoded-blocks.sqlite3 `
  --stop-eip 0x000C5570 `
  --build-dir .\build\local\ia32-phase2
```

The build fails closed on an XBE/store mismatch, decoded-byte drift, missing
direct or indirect target, or unimplemented privileged/MMIO/TLS rewrite. A
successful directory contains the PE, manifest, section and coverage maps,
direct-edge and indirect-target records, and rewrite manifest. `run` requires
the same `--xbe` and `--decoded-block-store` arguments for Phase-2 artifacts so
the loader can revalidate external provenance before execution.

Run the Phase-3 architecture and memory differential gate directly:

```powershell
python .\tools\recomp\ia32_proof_contract.py architecture `
  --build-dir .\build\local\ia32-phase3 `
  --report .\build\local\ia32-phase3\phase3-proof-report.json
```

The five capsules exercise full x87/MMX/SSE exchange state, static FS/TLS,
native stack/call behavior, physical page aliases, deterministic MMIO shadows,
cross-page renderer/audio dirty ownership, deterministic `RDTSC`, and a
repeatable recoverable access violation. Inspect `architecture-map.json` for
the exact guest address, mapped address, rewrite kind, page owner, and fault
site. Differential failures retain compact register/page diagnostics; dynamic
segment or high-address forms and executable-memory writes fail at build time
with an address-named coverage message.

Build the same Phase-3 layer over real Phase-2 decoded-store provenance with
`build-architecture` (or build and dispatch once with `execute-architecture`):

```powershell
python .\tools\recomp\ia32_native_backend.py build-architecture `
  .\reports\local\replay\boundary.b2rcap `
  --xbe .\data\local\extracted\burnout_2_poi_usa\default.xbe `
  --decoded-block-store .\build\native-guest-loop\decoded-blocks.sqlite3 `
  --stop-eip 0x000C5570 `
  --build-dir .\build\local\ia32-phase3
```

Optional repeated `--dirty-owner PAGE=OWNER`,
`--recoverable-fault-page PAGE`, and
`--mmio-shadow LOGICAL_PAGE=MAPPED_PAGE` arguments become content-addressed
architecture-map inputs. Omitting one of `--xbe` or `--decoded-block-store` is
an error; omitting both retains the capsule-only diagnostic builder.

Run the Phase-4 native host-ABI differential gate directly:

```powershell
python .\tools\recomp\ia32_proof_contract.py host-abi `
  --build-dir .\build\local\ia32-phase4 `
  --report .\build\local\ia32-phase4\phase4-proof-report.json
```

The service-rich synthetic replay exercises registered cdecl and stdcall
thunks, exact arguments and stack cleanup, native memory effects, guest
callback re-entry, and a renderer-facing request handled by a separate 64-bit
native broker. The bounded native trace records service order, arguments,
return values, entry/return stack pointers, memory before/after values, callback
state, and the execution owner. Any trace overflow or unregistered ABI fails
closed, and the report requires zero Python runtime callbacks.

Build or execute the same Phase-4 layer for a manually captured service-rich
capsule:

```powershell
python .\tools\recomp\ia32_native_backend.py execute-host-abi `
  .\reports\local\replay\service-boundary.b2rcap `
  --stop-eip 0x000C5570 `
  --build-dir .\build\local\ia32-phase4 `
  --report .\reports\local\replay\service-boundary-ia32.json
```

Each reached service must have a `service_registry` descriptor in the capsule.
The offline artifact contains `host-abi-map.json`, the IA-32 worker, and a
content-identified x64 broker; normal execution performs no runtime decoding,
compilation, guest-code patching, promotion, or raw-XBE execution.

Run the Phase-5 resident scheduler gate directly:

```powershell
python .\tools\recomp\ia32_proof_contract.py scheduler `
  --build-dir .\build\local\ia32-phase5 `
  --report .\build\local\ia32-phase5\phase5-proof-report.json
```

The ordinary proof runs two native scheduler cycles in primary, vblank, worker
order. It compares the decoded scheduler oracle with persistent IA-32 lane
contexts, per-lane TLS, safe-point service ranges, two worker wakeups, one
completed flip, and FNV-1a render/audio hashes. A second proof raises a real
access violation in the vblank lane and requires the worker and primary lanes
to finish in the same resident process with no stranded context or
unclassified exit.

Build or execute a manually captured bounded scheduler capsule with:

```powershell
python .\tools\recomp\ia32_native_backend.py execute-scheduler `
  .\reports\local\replay\scheduler-boundary.b2rcap `
  --stop-eip 0x000C5570 `
  --build-dir .\build\local\ia32-phase5 `
  --report .\reports\local\replay\scheduler-boundary-ia32.json
```

The capsule's `scheduler_state.resident_scheduler` record must declare the
three lanes, static resume EIPs, TLS pages, waits/wakeups, and render/audio
product ranges. `resident-scheduler-map.json` freezes those inputs into the
artifact. Resident builds also record equal-size static rewrites for the
accepted executor's no-effect `WBINVD` cache-publication boundary and observed
`OUT` port writes; standalone Phase-3 builds continue to reject them. Capture
input manually; the diagnostic command does not synthesize or automate
gameplay input.

Run the Phase-6 measured coverage-growth gate directly:

```powershell
python .\tools\recomp\ia32_proof_contract.py coverage `
  --build-dir .\build\local\ia32-phase6 `
  --report .\build\local\ia32-phase6\phase6-proof-report.json
```

The proof ranks decoded SCCs by measured native time, guest steps, module
calls, and unique outgoing frontier. It promotes the Lesson One SCC with
explicit service/render exits first, then boot/frontend continuity, then
worker/vblank and cold coverage. A diagnostic-discovery profile may name an
unknown target and its interpreter counts, but is never promotion eligible;
normal validation requires those targets in the decoded block store and both
frontier-interpreter counters at zero.

Convert a completed capture from the upgraded performance-debug suite without
transcribing target or transition counters:

```powershell
python .\tools\recomp\ia32_native_backend.py profile-coverage `
  .\reports\local\playability\performance-debug-report.json `
  --mode normal-validation `
  --report .\reports\local\replay\lesson-one-coverage-profile.json
```

The converter selects the hottest measured multi-target SCC for Lesson One,
uses reverse primary-lane reachability for boot/frontend continuity, assigns
the remaining worker/vblank/cold targets to the final slice, and carries over
the report's exact transition and frontier-interpreter counters. Incomplete
edge data is accepted only with `--mode diagnostic-discovery`.

Build or execute a manually captured closed profile with:

```powershell
python .\tools\recomp\ia32_native_backend.py execute-coverage `
  .\reports\local\replay\scheduler-boundary.b2rcap `
  --xbe .\data\local\extracted\burnout_2_poi_usa\default.xbe `
  --decoded-block-store .\build\native-guest-loop\decoded-blocks.sqlite3 `
  --coverage-profile .\reports\local\replay\lesson-one-coverage-profile.json `
  --stop-eip 0x000C5570 `
  --build-dir .\build\local\ia32-phase6 `
  --report .\reports\local\replay\lesson-one-ia32.json
```

The profile uses format `b2-recomp-ia32-coverage-profile`, version 1. Every
target declares its vertical slice, scheduler lane, and the three integer heat
metrics; transitions and service/render boundary exits provide the measured
frontier. `coverage-growth-map.json` binds that profile to the decoded-store
coverage and rewrite identities. Missing targets, unresolved static rewrites,
or nonzero frontier-interpreter activity stop the normal build before launch.

Run the Phase-7 normal-launcher and persistent-memory gate directly:

```powershell
python .\tools\recomp\ia32_proof_contract.py cutover `
  --build-dir .\build\local\ia32-phase7 `
  --report .\build\local\ia32-phase7\phase7-proof-report.json
```

The proof starts one generator-v23 worker, seeds its verified pages once, and
runs two complete resident schedules. Both schedules are compared against the
decoded oracle in sequence. Warm commands publish only CPU/control records and
explicit host-dirty pages into the worker; the response exposes only pages the
native worker changed. The report records seed, host-publication,
native-publication, bypassed-page, control-byte, and legacy full-roundtrip
counters. It also proves that same-ISA IA-32 is selected by default only for a
closed Phase-7 artifact, while the fusion-only executor requires the explicit
diagnostic-oracle selection.

Build and run the same cutover protocol over a manually captured, closed
Phase-6 workload with:

```powershell
python .\tools\recomp\ia32_native_backend.py execute-cutover `
  .\reports\local\replay\scheduler-boundary.b2rcap `
  --xbe .\data\local\extracted\burnout_2_poi_usa\default.xbe `
  --decoded-block-store .\build\native-guest-loop\decoded-blocks.sqlite3 `
  --coverage-profile .\reports\local\replay\lesson-one-coverage-profile.json `
  --stop-eip 0x000C5570 `
  --dispatch-count 2 `
  --build-dir .\build\local\ia32-phase7\manual-profile `
  --report .\reports\local\replay\lesson-one-phase7-cutover.json
```

`execute-cutover` never discovers or compiles guest code at runtime and never
falls back to another backend. A missing or ineligible artifact is a preflight
failure. Use the existing Phase-6 execution report or a decoded differential
replay as the first-dispatch oracle before attempting live promotion; input
for any new capture remains manual.

Validate a manually captured full-flip capsule twice with independent warm
runs:

```powershell
python .\tools\recomp\ia32_native_backend.py execute-full-flip `
  .\reports\local\replay\lesson-one-phase7-full-flip.b2rcap `
  --xbe .\data\local\extracted\burnout_2_poi_usa\default.xbe `
  --decoded-block-store .\build\native-guest-loop\decoded-blocks.sqlite3 `
  --coverage-profile .\reports\local\replay\lesson-one-coverage-profile.json `
  --dispatch-count 2 `
  --build-dir .\build\local\ia32-phase7\full-flip-run-1 `
  --report .\reports\local\replay\lesson-one-phase7-full-flip-run-1.json

python .\tools\recomp\ia32_native_backend.py execute-full-flip `
  .\reports\local\replay\lesson-one-phase7-full-flip.b2rcap `
  --xbe .\data\local\extracted\burnout_2_poi_usa\default.xbe `
  --decoded-block-store .\build\native-guest-loop\decoded-blocks.sqlite3 `
  --coverage-profile .\reports\local\replay\lesson-one-coverage-profile.json `
  --dispatch-count 2 `
  --build-dir .\build\local\ia32-phase7\full-flip-run-2 `
  --report .\reports\local\replay\lesson-one-phase7-full-flip-run-2.json
```

`execute-full-flip` fails closed unless the capsule contains a flip-classified
resident scheduler plan and an observed terminal oracle. The command infers
the native continuation EIP from that oracle. The first dispatch compares the
captured primary-lane CPU state, replayable native32 service projection,
declared render/audio products, live terminal stack, and scheduler TLS pages.
The report names this scope and the x87/MMX physical-alias and sticky-status
normalization explicitly; global worker/vblank trace traffic and dead stack or
transient page bytes are not mislabeled as outputs of a primary-only plan. The
complete changed-page resource remains identity-checked and drives explicit
host publication before repeated dispatches. Repeats also require dirty-only
persistent transport, zero scheduler faults/stranded contexts, and zero
cross-backend exits. This is the full-flip cutover acceptance path; the
ordinary interactive launcher remains unchanged until both independently
captured live windows pass and presenter/audio observation is signed off.

Compare old/new presenter traces at the typed packet boundary without running
guest code:

```powershell
python .\tools\playability\differential_replay.py compare-events `
  .\reports\local\replay\accepted-events.json `
  .\reports\local\replay\experimental-events.json `
  --stream render_events `
  --report .\reports\local\replay\packet-divergence.json
```

### Native address lookup and ETW correlation

Print the exact generated case, decoded block/IR, object, DLL, PDB, source line,
and address-derived symbol for a guest address:

```powershell
python .\tools\recomp\debug_metadata.py 0x0021976D `
  --build-dir .\build\native-guest-loop
```

Use `--metadata-only` to omit the source body and `--context N` to include
neighboring generated lines. Local debug replay retains PDBs and emits run
boundary annotations with guest EIP, current service, and native symbol through
ETW provider `{B2EC0A07-7E71-4A64-923B-327751105EB2}`. The same tuple is present
in `last_run_summary.diagnostic_context` and native fault text for crash-dump
correlation. Normal release AOT still emits the compact JSON mapping but does
not retain PDBs.

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
  --decoded-block-store .\build\native-guest-loop\decoded-blocks.sqlite3 `
  --json-output .\reports\local\recomp\x86-coverage-audit.json `
  --pretty
```

## Unsafe research overrides

`--allow-unsupported-xbe` and `--allow-stale-artifacts` exist for unsupported
research. Both conspicuously invalidate compatibility or performance evidence.

`--developer-live-compile` also exists for narrow developer diagnostics. It
changes the execution model by allowing runtime compilation and must never be
used for ordinary gameplay, static-clean validation, or a compatibility or
performance claim. Remove any diagnostic dependency on it before merging a
runtime fix.

There is not yet a Dear ImGui debugger or overlay; that remains P4.
