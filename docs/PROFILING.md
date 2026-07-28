# System profiling

Priority 9 uses external profilers first. ETW/WPA answers CPU scheduling,
context-switch, wait, and CPU/GPU overlap questions. RenderDoc answers Vulkan
event, resource, pipeline, barrier, and pass-structure questions. The existing
`--profile-hot-paths` counters are reserved for guest-semantic attribution that
those tools cannot provide.

The in-process profiler is native-clean and windowed. Start
`live_test.py --profile-hot-paths`, navigate while the title
shows `ARMED`, press F10 to begin the representative workload, press F10 again
to complete it, and then close the presenter. Exact CPU module/edge accounting,
sampled native target timing, presenter CPU/GPU timestamps, guest flip rate,
and Python-boundary acceptance use that same window. The target timer samples
the first call and every 256th call; the report includes sample confidence and
an estimated exact-accounting cost. Treat its FPS as instrumented and consult
that cost before comparing it with an ordinary diagnostics-off or ETW baseline.
`performance-debug-report.json` marks the profile invalid if the capture window
crosses a Python runtime callback, live-compilation/frontier/promotion boundary,
disables native observer dispatch, is incomplete, or records no native module
calls. Session-wide counters remain available but do not invalidate a clean
controlled window.

The expanded report keeps all populated target and transition records, not
only a short "top N" list. It provides four complementary views:

- exact calls, guest steps, exit reasons, entry-to-exit transitions, fan-in,
  fan-out, and transition probabilities;
- sampled target and native host-service duration with min/max, standard
  deviation, 95% margin of error, and time per sampled guest step;
- exact primary-worker-vblank execution-lane attribution, per-second and
  per-completed-flip rates, and 16.667 ms frame-budget shares;
- strongly connected guest cycles, hot branch entropy, concentration ratios,
  sample-instability warnings, and normalized before/after regressions.

Deterministic samples are not random independent observations. The 95% interval
quantifies observed sample dispersion; it does not remove phase-alias risk.
Keep a capture running long enough for high-cost targets to reach high or
medium confidence, and use ETW when native stack, scheduling, or host-library
attribution is the actual question. A profiled live run also writes sortable
`targets.csv`, `transitions.csv`, `cycles.csv`, and `native-services.csv` under
`reports/local/playability/hot-path-tables/`.

All profiler output is local evidence. ETL files contain system-wide process,
thread, module, and command-line data; RenderDoc captures contain rendered game
resources. Keep both under `reports/local/profiling/` and never commit or share
them as repository artifacts.

## Tool inventory

Run from a normal PowerShell:

```powershell
python .\tools\profiling\system_profile.py doctor --pretty
```

The doctor records exact paths, sizes, timestamps, and SHA-256 identities for
WPR, WPA, xperf, GPUView, `renderdoccmd`, and `qrenderdoc`. It also reports
whether WPR is already recording and whether the current shell is elevated.
The workflow searches `PATH`, the Windows Performance Toolkit, `%RENDERDOC_HOME%`,
`data/local/tools/renderdoc/`, and the standard RenderDoc install directory.

## CPU and GPU scheduling capture

First make one ordinary diagnostics-off run so the presenter and native-module
caches are warm. Then open **PowerShell as Administrator**, change to the
repository root, and run:

```powershell
python .\tools\profiling\system_profile.py capture-etw
```

WPR kernel CPU/GPU profiles require elevation. The wrapper refuses to replace
an existing WPR session, starts the built-in CPU and GPU profiles in file mode,
then launches exactly:

```powershell
python .\tools\playability\live_test.py --no-diagnostics --skip-host-build
```

Reach the same stable Lesson One segment used for the manual baseline, drive it
for 20-30 seconds, and close the presenter normally. The wrapper stops and
compresses the ETL in a `reports/local/profiling/etw-*` directory even when the
live command fails. If WPR cannot stop cleanly, it cancels only the session it
started and marks the capture incomplete.

Each capture contains:

- `b2-gameplay.etl`, hashed in `capture-manifest.json`;
- the Git state, clean live command, WPR/xperf identities, elapsed time, and
  matching live run ID/cache/configuration;
- xperf text reports for sampled CPU modules, one-second utilization,
  process/thread context-switch CPU, process/thread/image identity, and trace
  loss statistics.

Open the ETL in WPA and filter to the live Python process. The important threads
are named `b2-guest-runtime` and `b2-presenter-vulkan`. Inspect:

1. **CPU Usage (Sampled):** inclusive/exclusive stacks and modules on the guest
   thread; distinguish generated native modules, dispatcher, Python, audio, and
   presenter work.
2. **CPU Usage (Precise):** running, ready, and waiting time by thread, wait
   reason, and core. This decides whether the guest is compute-bound, blocked,
   or being descheduled.
3. **GPU Usage / GPUView:** queue packets, CPU submission-to-GPU execution,
   present cadence, and idle gaps. Compare this with presenter GPU timestamps;
   neither metric alone proves the guest bottleneck.
4. **Trace statistics:** reject a trace with lost sampled-profile, context-switch,
   or GPU events before making a performance claim.

The first accepted capture is `etw-20260723-185742`. Its guest thread used
93.32% of one logical core while the Vulkan presenter maintained 58.44
presents/s. Sample attribution concentrated in `python314.dll` with `_ctypes`
on most inclusive stacks, so the next CPU question is inside the remaining
Python/native boundary rather than the presenter or GPU queue.

Open the same trace directly when needed:

```powershell
wpa .\reports\local\profiling\etw-YYYYMMDD-HHMMSS\b2-gameplay.etl
& 'C:\Program Files (x86)\Windows Kits\10\Windows Performance Toolkit\gpuview\GPUView.exe' `
  .\reports\local\profiling\etw-YYYYMMDD-HHMMSS\b2-gameplay.etl
```

Use `capture-etw --cpu-only` only when GPU provider overhead or driver behavior
needs an A/B. Extra live arguments may follow `--`, but the wrapper rejects
audits, live compilation, bounded execution, stale/unsupported overrides, and
in-process hot-path instrumentation.

## Vulkan frame capture

Install a stable RenderDoc build from the project's official distribution and
make `renderdoccmd.exe` and `qrenderdoc.exe` discoverable through one of the
doctor paths. Then run from a normal PowerShell:

```powershell
python .\tools\profiling\system_profile.py capture-renderdoc
```

Reach the representative gameplay segment, press F12 once, then close the
presenter. Diagnostics-off mode leaves the application's screenshot path
disabled, so F12 is available to RenderDoc. The resulting `.rdc` files and the
exact RenderDoc executable hash are recorded under
`reports/local/profiling/renderdoc-*`.

RenderDoc injection and capture alter pacing. Use the capture to inspect the
event browser, render/compute passes, draw and dispatch population, barriers,
resource lifetimes, descriptor/pipeline churn, attachment sizes, and supported
hardware counters. Use ETW, native timestamps, and an uninstrumented baseline
for timing conclusions.

Start by answering these bounded questions:

- Is texture conversion dispatching or synchronizing unexpectedly every flip?
- Are off-screen feedback passes or attachment transitions dominating the
  event population?
- Are pipeline/descriptor objects changing when their retained identities
  should be stable?
- Does the graphics queue have enough work to explain a 22-24 FPS guest rate,
  or does it go idle waiting for CPU publication?

If a regression prevents the representative gameplay segment from being
reached, capture the latest stable rendered boot frame and label the evidence
accordingly. It may localize missing draws, malformed resources, pipeline
state, barriers, or queue behavior, but it must not be presented as gameplay
timing evidence. The first such regression capture is
`renderdoc-20260723-192000/frame132`, with
`renderdoc-20260723-191930/frame464` retained as its transition comparison.

Only after ETW or RenderDoc leaves one question unresolved should a new custom
counter or ETW/Vulkan marker be added. The counter must be bounded, opt-in when
it has measurable cost, and removed or documented once the question is closed.
