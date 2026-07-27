# b2_recomp Architecture, Workflow, Performance, and UI Audit

**Audit date:** 2026-07-22

**Audited revision:** `e34ba0eaf4d4722c156c6064e5ef3f113afce63f` (`Improve live rendering and frontier performance`)

**Scope:** repository structure, recompilation/runtime architecture, build and test workflow, debugging, current performance evidence, Python and SDL3 usage, and a proposed Dear ImGui interface.

**Remediation updated:** 2026-07-23

## Remediation progress

- [x] Local artifact cleanup. Removed approximately 20.28 GiB of generated
  reports, host builds, and stale native source/DLL cache entries while
  retaining the 55 MiB decoded-block database, native manifest, and audio
  cache.
- [x] Exact XBE provenance. `tools/targets/supported_targets.json` now pins the
  whole file, normalized loaded image, certificate, layout, and every actual
  and embedded section hash. Both the live launcher and direct probe reject an
  unknown executable before guest execution; the developer override is
  conspicuous and marks the run unsupported.
- [x] Build and run identity. Presenter builds now carry a content-addressed
  source/shader/compiler/library/artifact manifest. `--skip-build` rejects
  missing or stale artifacts. Every live launch writes a run manifest with Git,
  target, generated-code, configuration, cache, host, build, and result
  identity, and post-run reports reject a probe summary from another run.
- [x] F11 guest-FPS measurement. The presenter now maintains the completed-
  guest-flip sample whether or not F9 is visible. F11 records that guest FPS
  with its flip count and duration, and labels the latest host-frame rate
  separately as presenter FPS.
- [x] Change-driven render resources. Completed flips no longer force a full
  resource scan. Retained binding generations and cached guest-page generations
  trigger a snapshot only when bindings or bound memory actually change.
- [x] Fixed-rate controller delivery. Guest slices retain the last valid state
  and cap controller snapshot filesystem checks at 60 Hz; skipped slice checks
  and actual polls are counted separately.
- [x] Bounded diagnostics. Native-run history preserves the first 8 and latest
  56 records, native edge collection/reporting and callback address timing have
  hard caps, and every truncation or overflow is disclosed in summaries.
- [x] Presenter state and deltas. Normal live transport appends only new packed
  command spans, explicitly marks unchanged resource generations, and retains
  interpreter state, textures, buffers, render targets, descriptors, and
  pipelines while their identities remain valid.
- [x] Controlled overlay-off performance baseline. Run
  `a0e9bb76-365c-4319-a3fc-adbd233f333a` completed normally in 65.432 seconds
  with the exact target and presenter identities, warm caches before and after,
  diagnostics/profiling/audits and CPU fallbacks disabled, and presentation
  depth two. Its F11 snapshot at host frame 2,910 retained a self-verifying
  sample of 23 completed guest flips over 1.001 seconds: **22.986 guest FPS**,
  or 38.3% of the 60 FPS target. At the same workload, guest throughput needs
  approximately 2.61x improvement. The snapshot also recorded 628,548,647
  guest instructions, 0.105 ms latest-frame CPU render time, and 2.345 ms GPU
   time. This is a precise one-second point sample, not a frame-time
   distribution.
- [x] P3 project professionalization. Exact Python and transitive development
  dependencies plus CMake, Ninja, Clang, Vulkan SDK, SDL3, and native artifact
  hashes are locked and validated before builds. A one-command quality gate,
  strict type checking, pre-commit, and Windows Python/native CI are checked in.
  Ten native cases now cover vertex programs and renderer transport, texture,
  dirty-range, pipeline-key, and FPS-sampling boundaries. Presenter entry,
  options, logging, metrics, live transport, NV2A command processing, Vulkan
  device/rendering, retained resources, capture/diagnostics, session
  orchestration, and SDL3 platform ownership are separate modules. README
  operational evidence moved to focused docs, and architecture/build/
  contribution/license/asset policies now define the project boundary.
- [x] Focused project documentation. The README is now a 170-line project
  entry point instead of an operational run log. Initial setup and ordinary
  use live in `docs/GETTING_STARTED.md`; runtime mechanics, build/maintenance,
  diagnostic workflows, profiler protocol, and retained evidence live in
  their architecture, building, debugging, profiling, and validation guides.
- [ ] Controlled overlay-on performance baseline. Deferred until the planned
  P4 host overlay exists; no substitute measurement is being claimed.

## Executive verdict

`b2_recomp` is a technically impressive reverse-engineering prototype wrapped in prototype-grade engineering. The rendering progress is real, the capture/replay discipline is unusually strong, and there is evidence of careful investigation. The project nevertheless does **not** yet have a professional or scalable foundation.

The central problem is not Vulkan, SDL, or a single slow function. It is that offline analysis, guest execution, dynamic recompilation, target-specific repairs, host services, transport, presentation, profiling, and developer tooling have grown into a few enormous Python and C++ files. The repository calls itself a static recompilation, but normal execution still behaves substantially like a Python-orchestrated dynamic binary translator: it discovers frontier blocks during play, emits C++, invokes a compiler, loads DLLs, and promotes them into the running executor. That architecture makes cold start terrible, profiling noisy, correctness difficult to localize, and the runtime much harder to ship than it needs to be.

The foundation contains good ideas, but the current structure is not sound enough to build on indefinitely. Preserve the reverse-engineering knowledge, deterministic artifacts, and validation work. Replace the runtime shape around them.

| Area | Grade | Blunt assessment |
| --- | --- | --- |
| Reverse-engineering method | A- | Strong evidence capture and disciplined replay. |
| Rendering parity | B+ | Impressive progress; validation is ahead of architecture. |
| Runtime architecture | D+ | Too much Python, live compilation, filesystem IPC, and cross-cutting state. |
| Maintainability | D | Several files are effectively subsystems, not source files. |
| Build and reproducibility | F | No coherent build system, dependency lock, or CI contract. |
| Tracked repository hygiene | B | The Git tree itself is relatively small and ignores proprietary/generated data. |
| Local artifact hygiene | F | Tens of gigabytes of reports and caches with weak lifecycle management. |
| Testing | C | Broad Python coverage, but too much source-text testing and too little native behavioral coverage. |
| Debugging | C+ | Powerful forensic tools implemented in a slow, manual, data-explosive workflow. |
| Performance readiness | D | The measurements identify real bottlenecks, but the baseline itself is not trustworthy enough. |

## What is genuinely good

This project is not bad work. It is good research work that has outgrown its scaffolding.

- Guest/host responsibilities are at least conceptually separated.
- Frozen captures, exact command/resource generations, strict replay, and readback comparison are the correct techniques for renderer parity.
- Content-addressed and SQLite-backed caches show good instincts.
- Proprietary game data and most generated artifacts are correctly ignored by Git.
- Vulkan is an appropriate renderer for the low-level control required by NV2A translation.
- The unit suite completed successfully: 502 tests passed in about 56 seconds during this audit.
- The current performance report contains enough stage and boundary data to identify several major costs instead of merely guessing.

Those strengths are worth preserving. They do not excuse the architecture around them.

## Repository shape and cleanliness

The tracked repository is deceptively small: approximately 60 files and 3.98 MiB, but roughly 95,644 lines in total and 90,526 nonblank lines. About 68,683 nonblank lines are implementation and 21,299 are tests. The problem is concentration, not raw repository size.

The worst offenders are:

| File | Approximate size | Problem |
| --- | ---: | --- |
| `tools/playability/playability_probe.py` | More than 22,000 lines | Runtime, target repairs, diagnostics, capture, services, and orchestration collapsed into one module. |
| `runtime/host/vulkan_first_frame.cpp` | 15,901 lines | A “first frame” prototype that became the full presenter. |
| `tools/recomp/x86_lifter.py` | 8,009 lines | Analysis, lifting policy, and emission are too tightly coupled. |
| `tools/recomp/native_executor.py` | 4,211 lines | Runtime dispatch, compilation, caching, DLL loading, and policy in one place. |
| `runtime/xbox/shims.py` | More than 4,500 lines | Xbox services need domain modules and explicit interfaces. |

`VulkanFirstFrameApp` alone spans roughly 10,762 lines (`runtime/host/vulkan_first_frame.cpp:4944-15705`). That is not a class; it is an application hidden inside a class. Likewise, `playability_probe.py` is no longer a probe. These names preserve the archaeology of the project rather than explaining its present architecture.

**Remediation note (2026-07-23):** the historical presenter file is now a
25-line executable/DLL adapter. Presenter methods are defined out of class and
split by responsibility: session/live-reload/pacing (about 2.25 KLOC), Vulkan
device/render/synchronization (3.71 KLOC), textures and retained resources
(2.65 KLOC), diagnostics/readback/capture (1.68 KLOC), and NV2A command support
(3.20 KLOC). A private 652-line runtime header holds the declaration and shared
state. SDL3 platform, shared-memory transport, options, logging, and metrics
reporting are separately owned modules rather than more methods on the
presenter class.

There are 349 unique `TITLE_*` symbols and 1,163 occurrences, plus dozens of title-specific classes/functions in the playability runtime. The title repair fallbacks are disabled by default, which is good, but their presence in the core execution path still makes it difficult to distinguish emulation/recompilation semantics from Burnout-specific compensation.

### Local bloat

The Git tree is not the main bloat problem. The working environment is:

- `reports/`: approximately 17.6 GiB.
- `build/`: approximately 3.25 GiB.
- `reports/local/live/`: approximately 10.35 GiB.
- Playability reports: approximately 6.94 GiB.
- Individual command payloads exceed 1 GiB; `native-live.json` was about 753 MiB, a targeted matrix about 481 MiB, and a render debug report about 380 MiB.

Startup cleanup in `tools/playability/live_test.py` removes selected known files, but it is not a crash-safe generation scavenger. The native cache permits up to 2,048 artifacts and 4 GiB (`tools/recomp/native_executor.py:59`). There is no documented retention policy or first-class clean command.

**Remediation note (2026-07-23):** the immediate 20.28 GiB workspace cleanup is
complete. `tools/project_maintenance.py` now provides budget inspection,
age/count/byte retention, `clean-reports`, and `clean`; destructive operations
require `--apply`, validate every resolved file against a managed root, and
never target `data/local/`.

**Verdict:** orderly as a private research notebook, not orderly as a project another developer could clone, understand, reproduce, and maintain.

## The architectural identity crisis

The public framing suggests a static recompilation. The live architecture is hybrid dynamic recompilation:

1. Python drives guest execution and discovers blocks at the frontier.
2. It interprets or handles uncovered work.
3. It emits native C++.
4. It invokes `clang-cl` during execution.
5. It loads generated DLLs through `ctypes`.
6. It promotes the generated executor and continues.

`NativeResumableExecutor` begins around `tools/recomp/native_executor.py:1516`. The measured cold initial native compile took approximately **188.5 seconds**, with approximately 199.4 seconds of total compile time in the profiled run. That is not a tolerable normal launch path and it makes the product harder to debug, cache, package, secure, and reproduce.

If live promotion is useful for research, keep it as an explicitly selected developer mode. It should not define the shipping runtime. Persist newly discovered frontier information as an input to the next deterministic AOT build.

The target identity should be unambiguous:

- **Offline toolchain:** inspect the supported XBE, produce versioned IR/metadata, emit native code, and build it reproducibly.
- **Native runtime:** execute prebuilt guest code, provide Xbox services, present audio/video/input, and expose bounded diagnostics.
- **Developer fallback:** optional interpreter/live promotion for research, visibly nonrepresentative and excluded from release benchmarks.

## Target provenance is not strict enough

The README reports that 16 of 17 section digests match while `.text` does not. The XBE inspection tooling confirms the expected/actual mismatch. Hundreds of target-specific addresses and assumptions are unsafe without a hard identity gate.

**Remediation note (2026-07-23):** the hard gate is now implemented for live
and direct-probe execution. The supported manifest deliberately records the
known `.text` embedded-digest mismatch but requires the exact audited whole-file
and normalized-image identities, so "mostly matching" is no longer accepted.

Before execution, the project should require an explicit supported-build manifest containing at least:

- Whole-file hash and normalized image hash.
- Region/revision identity.
- Per-section hashes and expected virtual layout.
- Analysis schema/tool version.
- Generated-code build identifier.
- Compatible runtime version.

Refuse unknown images by default. A developer override may exist, but it should print an unmistakable warning and mark every produced report as unsupported. “Mostly matching” is not an acceptable provenance contract for address-sensitive recompilation.

## Build and workflow audit

The workflow is not professional yet.

There is no project-level CMake/Meson/Ninja configuration, Python project metadata and lock file, CI workflow, formatting/linting/type-check contract, pre-commit configuration, contributor guide, or license. `tools/host/first_frame_smoke.py` manually constructs compiler commands and contains environment-specific defaults, including `C:/VulkanSDK/1.4.341.1` and a local LLVM path. The default workflow recompiles the presenter; `--skip-build` trusts the existing binary without a source, compiler, or shader freshness check.

At audit time, the latest performance report predated the current commit, and the presenter executable and some shader/source timestamps did not describe one coherent build. Consequently, the current numbers are useful clues, not a valid performance claim about the audited revision.

The recent Git history also contains commits with tens of thousands of inserted lines. Large prototype imports are sometimes unavoidable, but repeatedly landing subsystem-sized changes as giant commits prevents meaningful review and makes regression bisection nearly useless.

**Remediation note (2026-07-23):** presenter builds now emit and validate a
content-addressed manifest, `--skip-build` rejects stale or unidentified
artifacts, and live runs record build/run identity. Python project metadata,
exact-version runtime/development locks, Windows CI, Ruff/bytecode checks,
synthetic preflight, and first-class maintenance commands are now present.
CMake/Ninja now builds asset-free native tests in debug, release, profiling,
and sanitizer configurations; the optional strict release target also builds
the Vulkan executable, embedded DLL, and source-dependent SPIR-V locally.
The exact native toolchain/artifact lock, ten-case native boundary suite,
strict type gate, pre-commit/CI quality command, and focused project
documentation are now present. Dear ImGui remains a P4 dependency and must be
revision-locked when it is introduced.

### Required professional baseline

1. Add CMake with Ninja presets for debug, release, profiling, and sanitizer builds.
2. Pin native dependencies and the exact Dear ImGui/SDL3 revisions.
3. Add `pyproject.toml` plus a lock file for offline tools.
4. Add CI that builds the native runtime, runs Python and native tests, validates generated artifacts, and performs a small deterministic replay.
5. Adopt formatters, static analysis, and type checking with one documented command.
6. Make every run manifest record Git SHA, dirty state, XBE identity, generated-code ID, executable/shader hashes, compiler and flags, cache state, profile mode, hardware, and warm/cold state.
7. Add `clean`, `clean-reports`, cache inspection, size budgets, and age/count retention.
8. Split operational history out of the 603-line README into focused documentation.
9. Add `LICENSE`, `CONTRIBUTING.md`, and an explicit policy around original-game assets.
10. Prefer reviewable commits that each preserve a buildable/testable state.

## Testing audit

Passing 502 tests is valuable, but the number overstates confidence in the product.

- The suite is almost entirely Python; there is no meaningful native C++ unit-test layer.
- `tests/unit/test_first_frame_smoke.py` reads C++ source and contains hundreds of `assertIn`/regular-expression assertions. These test spelling and implementation layout, not renderer behavior.
- The Python suite can pass even when the native presenter or SPIR-V is stale.
- `tools/playability/preflight_suite.json` references ignored `reports/local` artifacts, so a fresh clone or CI worker cannot reproduce the preflight suite.
- Successful test output still included noisy negative diagnostics such as a failed lossless audit and guest termination. A passing test command should not make the reader decide which failures are expected.

Replace source-text assertions with:

- Native unit tests for resource lifetime, format conversion, tiling, dirty tracking, synchronization, and pipeline-key behavior.
- Golden IR/emitter tests with schema/version checks.
- Small redistributable synthetic command/resource fixtures.
- Headless Vulkan integration tests where the runner supports them.
- Deterministic replay image/readback comparisons with tolerances stated in data, not code comments.

**Remediation note (2026-07-23):** CI can exercise the same preflight boundaries
on a fresh clone with ephemeral synthetic asset, render-stream, frame, and
guest-health fixtures. The CTest layer now has ten native cases: five NV2A
vertex-program cases plus direct transport/seqlock, texture-layout/unswizzle,
dirty-range ownership, pipeline-key, and FPS-sampler tests. Strict presenter
debug/release builds and ASan/UBSan remain available; a headless Vulkan runner
and redistributable golden captures remain future coverage.
- CI performance smoke thresholds and a separate controlled benchmark job.

## Performance audit

### First: the baseline is contaminated

The latest examined run enabled hot-path profiling and exact edge instrumentation, generated very large diagnostic data, and does not describe a clean post-promotion release build. It also predates the current revision. Do not advertise its FPS as current performance, and do not optimize to a single profiled trace without first producing a controlled baseline.

**Remediation note (2026-07-23):** a clean O2, warm-cache, profiling-off
overlay-off run now retains a 22.986 guest-FPS sample with exact run/build/XBE
identity. It supersedes the contaminated FPS evidence below; longer sampling is
still required for a distribution and the overlay-on comparison awaits P4.

The run is still revealing:

| Metric | Observed value |
| --- | ---: |
| Recent guest frame rate | 4.828 FPS (8.05% of 60 FPS) |
| Guest steps | 3,577,374,283 |
| Flips | 4,948 |
| Average steps per flip | Approximately 722,994 |
| Active guest throughput | 16.25 million steps/s |
| Effective guest throughput | 14.63 million steps/s |
| Implied active upper bound at the same workload | Approximately 22.5 FPS |
| Host wait ratio | 9.99% |
| Presenter p50 / p95 frame time | 16.551 ms / 25.040 ms |
| Presenter frame misses | 42.53% |
| Presenter reload busy time | 44.106 s (16.79%) |
| Initial native compile | 188.478 s |
| Total native compile | 199.397 s |

At the same average guest workload, active execution needs roughly **2.67x** more throughput for 60 FPS; effective throughput needs roughly **2.97x**. Host wait is only about 10%, so this is not primarily “the GPU is slow.”

The largest inclusive boundaries were approximately:

| Boundary | Inclusive time |
| --- | ---: |
| Native dispatch | 83.446 s |
| Yield handling | 82.482 s |
| Host bridge `on_slice` | 76.377 s |
| Render resource scan | 30.347 s |
| Dirty-page synchronization | 25.295 s |
| Observed-write span batching | 21.513 s |
| Call handling | 17.065 s |
| Controller sampling | 8.732 s |

These are inclusive and must not be summed, but they show that boundary traffic and bookkeeping consume enormous time.

### Concrete performance mistakes

1. **Every flip forces a render-resource scan.** `LiveHostBridge.publish_render(force=True)` near `tools/playability/playability_probe.py:11873`, combined with a `force or ...` condition near line 11912, defeats change detection. The measured scan averaged roughly 6.139 ms across 4,943 calls and once took several seconds.
2. **Controller input polls the filesystem every slice.** Code near `playability_probe.py:12325` performs a `stat()` around 42,031 times. It averaged about 208 microseconds and accumulated 8.732 seconds, then may write consumed JSON. Input needs event-driven or bounded-rate delivery, not per-slice filesystem metadata calls.
3. **Filesystem IPC is being used as a high-frequency bus.** Manifests, command/resource sidecars, controller JSON, acknowledgements, Windows events, and generation files moved gigabytes per run. The profiler recorded about 2.06 GiB of command payload movement and 2.11 GiB read.
4. **Presenter reload work is too expensive.** Interpret/method interpretation consumed about 21.4/19.9 seconds; resource update/native creation about 13.4/13.2 seconds; command loading about 5.35 seconds. The current report's “texture mostly CPU” ranking understates larger costs elsewhere.
5. **Diagnostic collection changes the workload.** Exact-edge dictionaries, raw span lists, and large JSON serialization can turn the profiler into a major part of the program.
6. **Live compilation destroys startup and iteration quality.** It should be an offline or explicitly opt-in development operation.

**P1 remediation note (2026-07-23):** forced per-flip resource snapshots and
per-slice controller `stat()` calls have been removed. Resource invalidation is
driven by retained binding/page generations, controller filesystem checks are
capped at 60 Hz, diagnostic histories and high-cardinality profiles are bounded
with explicit dropped counts, and the live presenter consumes command deltas
while retaining identity-valid resource and Vulkan state.

The first post-P1 warm, profiling-off snapshot measured 23.861 guest FPS (24
completed flips over 1.006 seconds), versus the earlier 22.986 sample. The
approximately 3.8% increase is only directional: these one-second gameplay
windows were not workload-locked. That run also added three native modules,
which directly motivated the P2 developer-only live-compilation gate.

**P2 progress note (2026-07-23):** normal live traffic now uses versioned shared
memory throughout: fixed records for controller input, stop, acknowledgement,
and the render manifest; a bounded SPSC command ring; and alternating immutable
resource slots. The presenter retains only the current epoch/resource bytes and
materializes standalone files for F12. The native dispatcher now remains in one
session across consecutive guest modules and registered ABI calls. Dirty pages
stay resident across safe ABI/yield boundaries, with native write generations
keeping resource snapshots coherent, and the steady post-flip heartbeat is one
million instructions while exact flips still yield immediately. Filesystem
fallback remains for direct tools and lossless audits. Normal unbounded play now
runs the guest thread and embedded SDL/Vulkan presenter in one OS process; the
native dispatcher owns cooperative cadence and the measured clock, yield, and
title XInput service bodies. Cold Xbox calls are now table-routed by the native
dispatcher, which owns their ABI unwind around Python host implementations. A
persistent native registry controls worker creation, runnable selection,
waiting, suspension/resume, termination, and terminal state; runtime snapshots
occur only initially and after lifecycle mutations. Runtime frontier
compilation is off unless explicitly enabled. The executable and embedded DLL
build cleanly, the exported entry loads, and the full Python unit suite passes 546
tests. The post-Priority 7 startup
failure was not cooperative-worker starvation. Bounded live sampling found no
secondary session and captured the primary at `0x0021DD80` with `ESI=0`, zero
PFIFO semantic reads, zero flips, and roughly 158,000 Python/native idle
boundaries after 30 seconds. The synthetic D3D context had explicitly seeded its
`+0x17F8` NV2A service pointer to zero instead of the title initializer's
`0xFD000000` MMIO base. That field is now correct, and the speculative per-poll
yield/forced worker tick has been removed. A warm embedded handshake published
the first manifest and stopped cleanly in 8.020 seconds with the native module
count unchanged at 277. After cold-service and worker-lifecycle migration, a
second warm, profiling-off, diagnostics-off embedded handshake completed with
return code 0 in 10.377 seconds under run
`4fdf822e-b1a2-4875-bf39-c787d75414de`, again with 277 native modules before
and after. Diagnostic run `589748f6-1105-451b-b5a5-8664392954f8` then recorded
1,510 cold calls through the native table, two worker entries, five lifecycle
transitions, and one native-recorded completion before a clean stop. The manual
gameplay baseline remains approximately 22--23 FPS.
One-time native cache recovery has a 30-minute default startup window rather
than the previous 10-minute cutoff.

### Performance priorities

1. [x] Establish a clean, hashed release baseline with caches warm and profiling disabled.
2. [x] Remove forced full resource scans. Guest binding/page generations now map dirty memory to affected resources.
3. [x] Push controller state through fixed shared-memory records sampled at a bounded host rate.
4. [x] Replace high-frequency JSON/filesystem IPC with a versioned shared-memory command ring, resource slots, fixed control records, and named events.
5. [x] Keep resources, decoded state, and pipeline state resident; send deltas rather than reload generations.
6. [x] Bound traces with fixed retention, counters, sampling, and opt-in payload capture.
7. [x] Move the normal-play guest scheduler, ABI dispatch, memory synchronization, reached host services, audio path, and render publication into native code. The migration-acceptance runs record zero Python runtime callbacks and zero frontier-interpreter work through sustained presenter-acknowledged completed flips; see the checkpoint below.
8. [x] Remove normal-play compilation and retain frontier discoveries for deterministic AOT builds.
9. [x] Profile with ETW/WPA or Tracy for CPU scheduling/boundaries and RenderDoc or Nsight for Vulkan work. Use custom probes only for questions those tools cannot answer.
   - [x] Add a first-class, contamination-resistant capture workflow. It
     inventories and hashes installed profiler binaries, records the CPU/GPU
     WPR profiles around the normal warm diagnostics-off live command, produces
     deterministic xperf CPU/context-switch/trace-loss summaries, names the
     guest and presenter threads, and associates every ETL or RenderDoc capture
     with the Git and live-run identity.
   - [x] Retain and analyze a representative Lesson One ETW trace. Capture
     `etw-20260723-185742` is a clean warm diagnostics-off run with zero lost
     events or buffers. The guest ran for 39.106 seconds and consumed 36.494
     CPU-seconds (93.32% of one logical core); it accounted for 79.62% of the
     process CPU. Guest-thread samples were 84.45% exclusive in
     `python314.dll`, with `_ctypes.pyd` on 72.75% of inclusive stacks, while
     generated native-loop/dispatch modules were not prominent. The Vulkan
     presenter consumed 4.44% of one core and issued 1,776 presents across
     30.374 seconds (58.44/s, 16.62 ms median, 17.26 ms p95). The ~22--23 FPS
     guest rate is therefore CPU-bound at the remaining Python/native boundary,
     not presenter/GPU throughput. ETW cannot resolve Python frames, so a
     bounded semantic probe is now justified for that boundary.
   - [x] Install and validate RenderDoc. The local profiler doctor recognizes
     RenderDoc 1.45 (`renderdoccmd` build
     `2fc0bc04cb95499635f63986a55bc6f67849dd9f`) and hashes both
     `renderdoccmd.exe` and `qrenderdoc.exe`; the contamination-resistant launch
     wrapper also passes its non-launching dry run.
   - [x] Retain and audit a representative frame from the currently reachable
     boot regression. Clean capture `renderdoc-20260723-192000/frame132` and
     transition comparison `renderdoc-20260723-191930/frame464` have the same
     one-pass, two-draw, one-barrier, one-submit/present action stream, no
     dispatches, no validation messages, and no in-frame pipeline, descriptor,
     or resource churn. The first draw is present and binds the expected
     256x128 nine-mip texture, but resolves to a flat gray/white panel; the
     second draw renders the loading label. This localizes the visible boot
     regression to published texture/UV/vertex data or its shader inputs, not a
     missing Vulkan draw, pipeline, submit, or present. Both draws disable depth
     testing/writes, yet the generic pass clears a 640x480 D32 attachment, and
     the no-readback path still ends in transfer-source layout before one
     transfer-aware present barrier. Those are real but minor renderer costs,
     not an explanation for the guest CPU ceiling or failure to reach Lesson
     One. Repeat the frame audit at gameplay only after reachability is
     restored; it is not required to justify the next bounded semantic probe.

## Is Python necessary?

**Yes for the toolchain. No for the shipping hot path.**

Python is a good fit for:

- XBE inspection and extraction.
- Offline analysis and database generation.
- Coverage and compatibility reports.
- Code-generation orchestration.
- Test fixture creation.
- Developer CLI commands and offline trace analysis.

Python is a poor fit for:

- The per-slice scheduler and guest execution loop.
- ABI and call dispatch.
- Dirty-memory synchronization.
- Hot Xbox kernel/service paths.
- Audio callback/data movement.
- Runtime native compilation and DLL promotion.
- High-frequency render transport.

Do not rewrite everything at once. Define stable versioned boundaries, then use a strangler migration: native implementations replace individual hot responsibilities while Python remains the oracle/tooling layer until parity tests prove the replacement.

### Python runtime migration checkpoint (2026-07-24)

| Poor-fit path | Status | Evidence / remaining work |
| --- | --- | --- |
| Per-slice scheduler and guest loop | Complete for normal play | The resident native coordinator owns the primary context, worker selection/lifecycle, wait/yield transitions, cadence, and stop handling. Warm bounded runs through 91 million guest instructions report zero Python slice yields. |
| ABI and call dispatch | Complete for normal play | Registered services unwind and return inside the native dispatcher. Warm bounded runs report zero handler calls, zero native cold-host calls, and zero Python runtime ABI invocations. |
| Dirty-memory synchronization | Complete for normal play | Native page views, dirty lists, byte ranges, generations, and resource ownership stay resident for guest execution. Read/write callback counts are zero; one final dirty writeback is retained only for post-run diagnostic materialization after the guest stops. |
| Hot Xbox kernel/service paths | Complete for the currently reached normal-play path | Allocation, persistence, AV, worker/semaphore, timing, input, filesystem, offline PHY, and title services reached by the bounded path execute natively. The July 25 title-to-Load/Save path also executes `NtQueryDirectoryFile` natively with deterministic wildcard ordering and a per-handle cursor. The 20-million-instruction run dispatched 16,779 native services with zero Python handlers or memory callbacks. Later address-taken guest targets remain AOT coverage work, not service fallbacks. |
| Audio callback/data movement | Complete for normal play | Native code owns RWS PCM/ADPCM decode, looping/gain policy, mixing, bounded buffering, and submission to the SDL3 presenter ABI. By 20 million instructions it decoded one music track and two special clips with zero failures and mixed/submitted 12 buffers (115,200 bytes) without Python callbacks. |
| Runtime native compilation and DLL promotion | Complete for normal play | Live compilation and promotion are disabled in normal execution. Frontier discoveries are diagnostic inputs to the decoded block store and the next AOT build. |
| High-frequency render transport | Complete for normal play | Native code owns completed-flip detection, command-span publication, every live texture binding required by a completed frame, immutable resource scanning/slots, native frontend-text metadata, named-event wakeups, presentation waits, and the shared control record. Bounded run `e0884dd7-5a27-403f-88c6-a4be1263df12` published and acknowledged 53 exact command/resource generations. The presenter performed 52 incremental reloads, interpreted 9,894 NV2A methods with zero unknown or truncated packets, matched the centered textured draw, and passed render validation on every generation. The July 25 title regression repair added multi-binding resource publication and the `Loading - please wait` binary manifest fields; focused native tests cover both without Python callbacks. |

The former `sample.xsb`/XACT blocker is cleared. Warm normal runs now pass the
old 8.355-million-instruction semaphore boundary, remain native-clean through
20 million instructions, and reach roughly 91 million instructions while
decoding/mixing frontend audio. The primary-thread semaphore wait no longer
mutates a resumable-worker lifecycle entry. Static AOT closure now includes
direct calls, direct branches, conditional fallthroughs, and bounded absolute
IA-32 jump tables; the complete CRT `memcpy` jump-table family has been added to
the decoded block store.

The `0x00073150` handoff target and the subsequently reached `0x00055970`
target are now persisted in the decoded block store and consumed by the AOT
cache. The initial July 25 warm acceptance run had 96 native cache hits, zero
misses, zero stores, and no compilation. The later render-transport acceptance
run `e0884dd7-5a27-403f-88c6-a4be1263df12` completed 2 million instructions
and 53 presentation waits with normal-runtime failure code zero. Python runtime
ABI, handler, memory read/write, observer, and slice-yield callbacks were all
zero; the frontier interpreter recorded zero invocations and zero steps, native
promotion was disabled, and developer live compilation was disabled.
This closes the Python runtime migration checkpoint for the currently reached
normal-play path. Later unknown executable targets remain deterministic AOT
coverage gaps, not permission to restore a runtime fallback.

### Title/render regression checkpoint (2026-07-25)

The manual captures
`b2-recomp-20260725-121422-615-frame-31-1.bmp` and
`b2-recomp-20260725-121433-525-frame-662-2.bmp` exposed two transport omissions
that the previous report incorrectly classified as healthy:

- the native `0x000BF6F0` text-draw service consumed the loading label without
  publishing its text, placement, size, or color; and
- live resource publication retained only the last stage-zero texture binding,
  so the title font survived while the background and Burnout 2 logo resources
  were absent and sampled white.

The native title service now publishes `Loading - please wait` through fields
4--8 of the binary manifest, and the resource publisher collects and
deduplicates every binding referenced by the completed command generation.
The render-debug report now treats a textured presented draw with no matching
resource as a critical `presented_texture_resources_missing` translation
mismatch. A real title generation after the repair contained four draws and
four texture resources, with no unmatched presented texture draw. This closes
the source-level render transport regression for normal play.

The first post-title stall was a separate native ABI gap. The scheduler stopped
at import target `0xE0000510`, ordinal 207, because `NtQueryDirectoryFile` was
not registered in the native normal service table. Its native implementation
now owns mask decoding, deterministic case-insensitive wildcard ordering,
per-handle enumeration position, Xbox `FileDirectoryInformation` records, and
status/IO-block writes. The focused native test enumerates a file and directory,
returns `NO_MORE_FILES`, and records zero Python handler calls. Manual execution
then advanced from the title into Load/Save.

Two later manual boundaries exposed independent native service-lifetime gaps.
Repeated Load/Save volume probes exhausted the 128-entry native file table
because `NtClose` marked slots inactive but registration only advanced the
high-water count. `NtOpenFile("U:\\")` then returned invalid parameter, the
volume-allocation helper returned zero, and guest `idiv` at `0x000D9496`
faulted. Closed slots are now reused before the high-water mark advances; a
focused test performs 140 open/close volume probes with one resident slot,
zero overflow, and zero Python handlers.

After that repair, manual play advanced through Load/Save and stopped while
entering the main menu. The fixed live scheduler header captured phase 41 at
`0xE0000630`, ordinal 253, `PhyInitialize`. The offline PHY shims were still
generic cold callbacks, which are intentionally unavailable in native normal
play, so the network worker failed and the frontend waited indefinitely.
`PhyInitialize` and `PhyGetLinkState` now use native success-result services
with their existing ABI stack cleanup. Native coverage failures also retain
their real target and code instead of being replaced by a pending render-span
observer error. Focused unit coverage passes, and the next manual run confirmed
the repaired main-menu transition. These service failures do not reopen the
completed render transport boundary.

That manual run (`41b28351-0200-4b33-a442-5a96dad024ea`) then exposed a level
loading hang rather than a crash. It retired 1.598 billion guest instructions,
published 8,460 native render generations, and stopped only when the user
closed the window. Normal-runtime failure code, worker failures, Python runtime
callbacks, and title-asset open failures were all zero; 22 title assets totaling
50,296,036 bytes opened successfully. The regression was in the native asset
reader introduced by the runtime migration: the older diagnostic path
validated Track PSS files and published their prelinked image at the absolute
guest base encoded in the file, while the native service copied bytes only to
the caller's temporary buffer. Scene and stream descriptor pointers therefore
continued to reference an unpopulated `0x8381C000` image and the title never
completed level loading.

The native title-asset service now recognizes source Track PSS paths, validates
both stream-descriptor tables and all three scene-record tables, allocates the
encoded guest image range, and publishes the exact file bytes there during the
first read. Bounded counters and per-stream flags report candidates,
publication attempts, validation failures, byte counts, descriptor counts,
scene-record counts, and the encoded image base. The native executor module's
88 tests pass, including a no-Python Track PSS publication regression, and all
30 extracted Track PSS files pass the same structural validation.

Manual run `c2607c60-4d55-4560-a305-8cc228b5a01c` advanced beyond the former
infinite load and then stopped with native transport code 9 at `0x00111040`
after 146,052,791 guest instructions. Static disassembly identifies that target
as a valid address-taken callback installed by `0x00111060` and invoked by the
list walker at `0x00110CE2`; it was absent from the decoded block store, so the
normal native dispatcher correctly rejected it as an AOT coverage gap. The
callback is now stored under the supported XBE's exact image identity for the
next static build. Native code-9 exits also decode and persist an executable
failure target after execution has stopped, record the result under
`native_normal_runtime_coverage_gap`, and never interpret, compile, or continue
that block in the active normal run. Failure reports now retain the Track PSS
publication counters and per-stream flags as well. Focused coverage and decoded
block-store tests pass; entering the first level awaits another manual run.

Manual run `17e6fef4-d8b6-462c-8765-762681fd9b2c` then progressed further and
stopped at address-taken callback `0x00116DD0` after 207,854,878 guest
instructions. The code-9 post-run path worked as designed: it decoded the
11-instruction callback for the next AOT build and reported
`decoded_for_next_aot`. The same failure report proves that two Track PSS
images totaling 9,568,256 bytes were published with zero validation failures.
Static inspection found `0x00116DD0` in the callback family rooted at data table
`0x00347100`; relying on serial runtime discovery would expose the remaining
members one launch at a time. All 11 exact table targets are now recovered
during deterministic pre-run artifact preparation, and the local supported-XBE
store contains every member. Normal execution still performs no live decode,
interpretation, compilation, or promotion.

Three earlier isolated-process runs also remained visible after their launchers
had exited. Windows reported all three processes as exited with one retained
console-wait thread, so normal process termination could not remove them. The
orphan console and exact retained threads were cleared, and the process list now
contains no b2_recomp playability process. Isolated/bounded launches now assign
the guest and presenter to a Windows job with `KILL_ON_JOB_CLOSE`; graceful
shutdown remains first, while launcher loss closes the job and terminates its
children. A real disposable-child check verified the kernel behavior. Another
manual first-level entry remains the acceptance check.

Manual run `93ca18d9-8b28-4d7b-8995-30f670f297c4` progressed to the next
address-taken level callback, `0x00116C30`, after 146,566,558 guest
instructions. The post-run coverage recorder persisted that block as
`decoded_for_next_aot`. Static inspection found that `0x00116D00` installs
`0x00116C30` into a callback slot and that the adjacent level constructor
installs the paired callback `0x00116D80`; both targets are now members of the
deterministic pre-run callback set, and the second block is present in the
supported-XBE decoded block store. This closes the constructor-installed pair
without live interpretation or compilation.

The same run exposed a different lifetime bug in embedded normal play. A
terminal native-runtime failure raised from `NativeResumableExecutor.run`
before the caller reached `shutdown_normal_runtime`, leaving the native audio
worker and its loaded presenter audio library alive after the run manifest was
finalized. That worker kept both the real Python process and the Windows Python
launcher resident. Terminal native-runtime failures now shut down that state
before raising while preserving the original failure code, target, and run
summary. The two retained `live_test.py --skip-host-build` processes from the
manual run were stopped, and no recomp or playability process remains. Focused
native failure, callback recovery, launcher, lint, and syntax checks pass; a
manual first-level entry remains the acceptance check.

Manual run `0c95ccea-a43c-4790-a9ad-6f7ce9b1feba` reached another member of
the same constructor-installed pattern, `0x001162C0`, after 112,735,050 guest
instructions. Rather than add its `0x00116270` pair and continue serial
discovery, pre-run AOT preparation now batches statically stored title-code
pointers. It scans decoded `.text` instructions for immediate code addresses
written to memory, rejects targets outside file-backed title code or inside an
already decoded instruction, validates standalone function boundaries from
returns/alignment padding, and admits non-aligned entries only from initializer
tables containing at least three absolute callback slots. Recovery repeats
after direct-branch and jump-table closure until no new stored callback remains.

The supported-XBE audit found 78 initially uncovered values. It accepted 75
callback entries and rejected the packed numeric values `0x00020000`,
`0x00020003`, and `0x00020007`. Their recovered CFG exposed one further stored
callback, `0x00114310`, in block `0x00114937`. Offline preparation completed in
two rounds: 76 callback roots plus 3,353 direct-branch/jump-table blocks, all
3,429 selected blocks decoded and stored successfully. The local decoded block
store now contains 75,779 records, and a warm rescan finds no remaining
validated stored-code target. The complete 224-test playability-probe suite,
the native terminal-failure regression, all 25 launcher tests, Ruff, and syntax
checks pass. Manual first-level entry remains the acceptance check.

Manual run `398070fe-bc3f-4263-8caf-b65d47d4cdbb` passed the loading screen,
rendered 1,633 frames, and then stopped with native transport code 9 at
`0x0023557A` after 129,650,823 guest instructions. The retained stack and
registers identify the source exactly: DirectSound code at `0x00231195` calls
vtable slot `+0x20`, the vtable begins at `.rdata` address `0x002C95A4`, and
that slot contains `0x0023557A`. This is a static DirectSound COM-style method
table, not another title initializer store.

Pre-run AOT preparation now scans file-backed `.rdata` and `.data` for
contiguous code-pointer tables. A run must contain at least three entries, at
least two raw return/alignment-proven function boundaries, and boundary proof
for at least half of its unique targets; once proven, the whole method family
is retained so nonstandard thunks are not discovered one crash at a time.
Static data sections are no longer accepted as code targets by indirect
jump-table closure merely because their XBE flags include execute, and the
IA-32 coverage auditor applies the same file-backed code-section boundary.

Offline materialization reached a fixed point with no remaining validated
stored-code target, static-table member, direct branch, conditional
fallthrough, or bounded jump-table target. The exact supported-XBE store now
contains 84,762 records under its single uppercase image hash, includes
`0x0023557A` and the complete proven DirectSound table families, and passes
SQLite integrity validation. The complete 226-test playability-probe suite and
all 25 launcher tests pass, as do Ruff, syntax compilation, and diff checks.
The uncapped 131,072-block IA-32 audit completed 92,686 code blocks with no
pending work or traversal cap; its remaining 124 opcode findings are confined
to XGRPH, XONLINE, and XNET, with no DirectSound finding. No recomp process is
running. Manual first-level play remains the acceptance check.

Manual run `d73a861b-2387-42e1-8080-881a766beebe` did not reach the
30-minute startup timeout. The native guest stopped after 476.9 seconds and
460,446 instructions with transport code 9 at `0x0028C180`; the launcher then
misreported the terminated guest as "still preparing" and recorded return code
zero. The failing target is XPP callback slot `+0x04` in the descriptor at
`0x0028BFE8`, reached by the indirect call at `0x0028C571`. XPP interleaves
that descriptor family with code in its executable section, so the prior
`.rdata`/`.data`-only pointer-table scan could not see it.

Static pointer-table recovery now scans every file-backed XBE section while
still accepting targets only in file-backed executable code; headers and
zero-fill tails remain excluded. The existing raw return/alignment and family
density proof admitted 65 uncovered callbacks across the supported image,
including all nine observed XPP family entries, and their direct branch,
conditional fallthrough, call, and bounded jump-table closure added 1,377
blocks. The expanded traversal exposed the CL-count `SHLD` form at
`0x001206AA`; decoder, diagnostic executor, and native emitter support now
cover it, adding its final block without exposing another title-code gap.

Offline preparation reached a fixed point after two rounds. The exact-XBE
decoded block store contains 86,215 records under one uppercase image hash,
contains `0x0028C180` and all eight previously uncovered XPP siblings, and
passes SQLite integrity validation. The uncapped audit now visits 94,038 blocks
and 572,792 instructions with no pending work or traversal cap; its 124
remaining findings are again confined to XGRPH, XONLINE, and XNET. The probe
now returns failure for fatal entry/thread execution, and both embedded and
process launchers describe a pre-first-frame exit as an exit and coerce a
zero-code/no-frame termination to failure. All 255 playability-probe and
launcher tests plus all 63 IA-32 recomp tests pass, along with Ruff, syntax,
and diff checks. No gameplay process was launched during this repair; manual
first-level entry remains the acceptance check.

Manual run `8e494bc6-ae4f-40f8-a15a-401f7c3d9d1f` correctly reported a
pre-first-frame failure after 280.0 seconds and 466,281 guest instructions:
native transport code 9 at XPP target `0x0028D945`. The retained XBE bytes show
the complete source edge. XPP code at `0x0028C2C4` pushes `0x0028D945` as stack
argument 3 to the CRT vector-constructor helper at `0x000134D0`; that helper
loads the argument and invokes it through `call ebx`. The target is an element
constructor beginning immediately after a sized return. It is neither an
aligned static table entry nor a `mov`-installed callback, so neither existing
address-taken recovery path could seed it.

AOT preparation now models the proven CRT helper ABI directly. For each direct
call to a declared stack-callback consumer, it traces a bounded same-block push
sequence without crossing a stack mutation or control transfer, selects the
declared callback argument, requires an immediate file-backed executable
target and a raw function boundary, then includes that target in the same
callback/CFG fixed point as stored pointers and static tables. Removing
`0x0028D945` from the known-function set makes this analysis recover exactly
`0x0028D945`; with the block present, the warm rescan has zero stored, table,
or stack-callback candidates.

A deliberately broader push-immediate experiment was rejected: executable Xbox
library sections also contain strings and packed data passed to APIs, so a raw
boundary-looking value alone is insufficient. Its 6,268 generated records were
removed from the local store after an SQLite backup, preserving only the
user-run `0x0028D945` capture. The exact-XBE store now contains 86,216 records
under one uppercase hash and passes integrity validation. The uncapped audit
visits 94,039 blocks and 572,798 instructions with no pending work or traversal
cap; the same 124 findings remain confined to XGRPH, XONLINE, and XNET. All 320
combined probe, launcher, IA-32, and compiled-native regressions pass, along
with Ruff, syntax, and diff checks. No recomp process is running. Manual
first-level entry remains the acceptance check.

Manual run `d3a7cfcb-c4ab-4385-a33e-a1f57ee0032e` advanced preparation into
the embedded presenter, then stopped after 65.5 seconds and 2,398,950 guest
instructions with native transport code 9 at `0x000AE2B0`. The run correctly
persisted that exact nine-instruction block for the next static build. The
retained title code identifies a broader source family: the call at
`0x000AE2FA` passes `0x000AE2B0` as stack argument 1 to `0x000FD810`, whose
body loads argument 1 and invokes it indirectly as a visitor callback.

The decoded title contains exactly three direct calls to that linked-list
iterator. Their complete callback family is `0x000AE2B0`, `0x000AE310`, and
`0x000FDC40`. AOT preparation now declares the proven `0x000FD810` argument-1
callback ABI alongside the existing CRT vector-constructor contract. The same
strict proven-consumer, stack-safe push-trace, immediate executable target, and
raw-boundary checks therefore recover every visitor before native execution;
no generic immediate or runtime compilation fallback was added.

Offline materialization added the two unobserved sibling callbacks and seven
direct branch/fallthrough blocks in their CFG closure. The exact-XBE store now
contains 86,226 records under one uppercase image hash, has SHA-256
`2CDDE6C1656C966E8FD79352DADC9D8D0C2E025811E233DB699623582C288C46`,
and passes SQLite integrity validation. A warm analysis has zero stored,
static-table, or stack-callback candidates. The uncapped 131,072-block audit
visits 94,049 blocks and 572,863 instructions with no pending work or traversal
cap; its unchanged 124 findings remain confined to XGRPH, XONLINE, and XNET.
All 320 combined regressions pass, along with Ruff and syntax checks. No recomp
process was launched or left running. Manual first-level entry remains the
acceptance check.

Manual run `a74436af-56dc-43b5-844f-5c6f2a4b65a3` passed the frontend and
level loading path, then stopped after 98.8 seconds and 110,258,745 guest
instructions with native transport code 9 at `0x000B2370`. The user's reported
two-run sequence is consistent with the retained evidence: after the prior
86,226-record checkpoint, the failed runs persisted 39 exact and dependent
blocks, so the second preparation emitted additional native DLLs and advanced
past the first run's main-menu boundary. This is correct static persistence,
but it exposed that address-taken family recovery was still incomplete.

The exact source edge at `0x000B261B` passes `0x000B2370` as explicit stack
argument 2 to the spatial-query consumer at `0x0008F3D0`. That consumer invokes
argument 2 through `call [ebp+0x10]` at `0x0008FAE5`. Its callers interleave
floating-point and register work between argument pushes, so the earlier
contiguous-push requirement could not identify them. All six raw direct
callsites resolve to the five-member visitor family `0x00078B90`, `0x00083470`,
`0x00092EE0`, `0x0009B180`, and `0x000B2370`; the middle two were entirely
absent from AOT coverage.

Declared-consumer recovery now permits intervening instructions only while
walking the same linear block and preserving the CPU stack. It stops at calls,
branches, returns, stack-pointer writes or exchanges, pops, and non-argument
push forms, and remains bounded to 64 instructions. This admits the proven
spatial-query ABI without accepting generic code-looking immediates. Offline
materialization added both missing siblings plus seven branch/fallthrough
blocks in their CFG closure. The warm stored-pointer, static-table, and declared
stack-callback scans are all empty.

The exact-XBE store now contains 86,274 records under one uppercase image hash,
has SHA-256
`3AE02F5F93ECB9E904711A92E11D0476B9B01AF240D9C9297AC4D4AC5CBDADF2`,
and passes SQLite integrity validation. The uncapped 131,072-block audit visits
94,105 blocks and 574,012 instructions with no pending work or traversal cap;
its unchanged 124 findings remain confined to XGRPH, XONLINE, and XNET. The
321-test combined probe, launcher, IA-32, and compiled-native suite passes,
including positive interleaved-push recovery and stack-mutation rejection
regressions. Ruff, syntax, and diff checks also pass. No recomp process was
launched or left running. Manual first-level play remains the acceptance check.

Manual run `70eae22e-991e-4449-81c8-b1a57ec848b9` advanced to 291,386,955
guest instructions before native transport code 9 at `0x000B22A0`. The source
edge at `0x000B283B` passes that target as explicit stack argument 2 to the
spatial-collision consumer at `0x00090420`; the consumer invokes argument 2 at
`0x0009059E`. All 13 raw direct callsites resolve to the four-member visitor
family `0x00056EA0`, `0x0005EBE0`, `0x000A3FB0`, and `0x000B22A0`.
`0x000A3FB0` was the uncompiled sibling, while the failed run had persisted
only the exact `0x000B22A0` entry.

AOT preparation now declares the proven `0x00090420` argument-2 ABI and feeds
its complete family through the existing stack-safe callback/CFG fixed point.
Offline materialization added `0x000A3FB0` plus eleven dependent blocks,
including the complete branch/fallthrough closure of the persisted
`0x000B22A0` entry. Warm stored-pointer, static-table, and declared
stack-callback scans are all empty; no interpreter or live-compilation fallback
was added.

The same run confirmed the reported frame-rate error. The presenter produced
1,293 host frames over 21.549 seconds at an average 16.666 ms per frame, but
performed 2,435 live reloads. It consumed one publication at the top of the
60 Hz loop and a second while waiting for the frame deadline, acknowledging and
releasing almost two guest flips for every displayed frame. The deadline wait
now only sleeps; publication ingestion and acknowledgement occur once at the
next 16.667 ms loop boundary. Normal guest simulation/output is therefore
capped at 60 FPS without changing Vulkan FIFO presentation or diagnostic audit
semantics.

The exact-XBE store now contains 86,287 records under one uppercase image hash,
has SHA-256
`3E1786C553A84B853FEAE597B8BEDCC575DE8288B08EF7A5F944D3CD0E473275`,
and passes SQLite integrity validation. The uncapped 131,072-block audit visits
94,118 blocks and 574,142 instructions with no pending work or traversal cap;
its unchanged 124 findings remain confined to XGRPH, XONLINE, and XNET. All
391 relevant Python regressions and all 10 release native tests pass. The local
presenter executable/library were rebuilt without launching gameplay, and
manifest build
`6CB8464743FBA5A81E7E7ADF55F3F9F15E3943218482F969CB5B5EC118352E6F`
validates. Manual first-level play and the observed FPS counter remain the
acceptance checks.

Manual run `fc0243ad-2d48-4288-8b02-b3192a859d8d` reached the first level's
How to Play card and remained there until the window was closed. The run
published 1,539 guest flips, retained a healthy exact F12 render snapshot, and
stopped through `native_runtime_stop` with failure code zero. Controller input
also continued to reach the guest. The frozen render-debug suite reported no
geometry, command, resource, or presentation finding, so the retained card was
not a render-transport regression.

The native scheduler evidence instead showed 2,728,218 worker resumes and
4,593,130,040 worker instructions without a completion. Static stack unwind
placed the active worker in the `Sleep(0)` poll reached from the DirectSound
failure-cleanup callsite at `0x001079DB`; the retained status word was still
`1`, or playing. The recovered stop path submits an MCPX voice command, can
mark the voice pending with bit `0x8000`, and expects the hardware completion
path to clear pending/playing state. The native runtime never delivered that
transition. The recovered status method reports playing for either active low
state `0x0003` or pending/allocated mask `0x8001`; the first report did not
retain the dynamic voice word needed to distinguish them.

The AOT native fast-path table now models that missing audio-hardware boundary
at the actual loop header `0x00107080`. It activates only after a stop was
submitted for a valid DirectSound voice satisfying either exact playing
predicate, clears only the 16-bit pending (`0x8000`) and playing (`0x0002`)
bits, and resumes at `0x00107086`. The guest still executes its own `Sleep(0)`,
status query, branch, and finalizer through `0x001070AA`; the generic
DirectSound status method is not replaced.

Manual run `84b2268a-9f6a-40af-98d5-3243c59cf15c` proved why the initial
active-state-only guard was insufficient. It rebuilt three AOT modules, listed
the `0x00107080` fast path in native diagnostics, but recorded zero invocations
while the same worker stack retained status `1`. Because the same valid voice
had failed the active-state predicate, the recovered GetStatus logic uniquely
selects its other playing case, pending/allocated mask `0x8001`. The run
published 1,910 flips and stopped cleanly only on the user's close after
925,376,739 primary and 10,385,716,473 worker instructions; there were zero
worker failures and normal-runtime failure code zero.

The guard now mirrors both recovered GetStatus predicates. Focused native
coverage verifies `0x8003` active/pending and `0x8001` pending-only voices both
transition to stopped state `0x0001`, preserves adjacent bytes, and leaves an
already stopped voice untouched. The combined native-executor and playability
probe suite passes all 321 tests. No gameplay process was launched during the
repair; passing the How to Play boundary remains the manual acceptance check.

The later retained snapshot at public `IDirectSoundBuffer::Play` wrapper
`0x0022D896` exposed a separate ownership error: the native audio observer
mirrored the request and then forwarded into the original Xbox SDK hardware
body. Static recovery proves the wrapper's full ABI. It subtracts `0x1C` from
the public interface pointer, the internal method at `0x0022C90E` loads the
voice from object offset `0x20` (public offset `+4`), the voice core at
`0x00235136` establishes allocated/playing state at voice offset `0x12`, and
the wrapper returns with `ret 0x10`.

Normal play now terminates `Play` in the native service dispatcher. The service
updates that exact 16-bit guest voice state while preserving adjacent bytes,
clears the pending MCPX bit because no guest hardware transaction remains,
returns `S_OK`, performs the existing 16-byte cleanup, and never makes the
original body dispatchable. Null objects return `E_FAIL`; host decode or SDL
output failures remain diagnostics rather than guest ABI failures. Focused
compiled regressions prove the SDK sentinel result is unreachable, and all 103
native-executor tests pass. Manual first-level progression beyond run
`aadc8980-d227-4793-a39b-2d7df3515a86` remains the acceptance check.

## Should SDL3 do more than controllers?

**Yes, selectively.** SDL3 should own the ordinary platform layer after the presenter is modularized:

- Window creation and lifecycle.
- Event pumping, keyboard, mouse, and gamepads.
- DPI/display queries and fullscreen behavior.
- Vulkan surface creation.
- Audio-device and stream management.

Keep the Vulkan renderer. Do **not** migrate to SDL_GPU merely for uniformity. The NV2A translation layer needs explicit control over formats, aliasing, barriers, descriptors, pipelines, readback, and capture behavior; replacing direct Vulkan would introduce risk without addressing the measured bottlenecks. SDL3 audio is a much cleaner destination than Python `ctypes` around WinMM.

Using SDL3 for the window will not magically fix Windows-specific events or file transport. Its real value is a clean platform boundary, portability, and removal of bespoke OS boilerplate.

**Remediation note (2026-07-23):** the presenter is now modularized around an
SDL3 platform object, live-transport object, immutable metrics/reporting
boundary, structured logger, standalone options parser, NV2A command processor,
and direct Vulkan presenter. SDL3 now owns video initialization, high-DPI window
creation/lifecycle, the event pump, keyboard and gamepad state/hot-plugging,
presenter hotkeys, Vulkan extension discovery, and surface creation. Direct
Vulkan intentionally continues to own the renderer. SDL3 now also owns the
default playback device, audio stream, bounded native queue, reset, and
shutdown through exported presenter-library functions. The native normal
runtime now owns title-specific RenderWare PCM/ADPCM decoding, application
gain, looping, software mixing, and PCM submission; the former Python
`ctypes` WinMM sink and Python mix path have been removed from normal play.

Relevant SDL documentation:

- [SDL3 Vulkan category](https://wiki.libsdl.org/SDL3/CategoryVulkan)
- [`SDL_Vulkan_CreateSurface`](https://wiki.libsdl.org/SDL3/SDL_Vulkan_CreateSurface)
- [`SDL_OpenAudioDeviceStream`](https://wiki.libsdl.org/SDL3/SDL_OpenAudioDeviceStream)
- [SDL3 GPU category](https://wiki.libsdl.org/SDL3/CategoryGPU)

## Dear ImGui recommendation

**Use Dear ImGui. It is an excellent fit for developer tooling and an acceptable fit for a modest user settings overlay. It is not a substitute for fixing the runtime architecture.**

The project should call it **Dear ImGui** (or ImGui), not “ImGUI.” Its immediate-mode design fits live inspection and tuning well, and its official backends support SDL3 and Vulkan. The official documentation explicitly positions it around game-like interactive loops and content/debug tooling.

### Appropriate uses

Developer panels should include:

- Guest FPS, presenter FPS, frame-time graphs, waits, queue depths, and bounded timing histograms.
- Guest CPU state, threads, registers, current block, dispatch/fallback counts, and breakpoint controls.
- NV2A draw/event stream, render targets, textures, shaders, pipeline keys, and resource lifetimes.
- Dirty pages and guest-resource mappings.
- Capture/replay controls and exact run-manifest identity.
- Validation status, readback diffs, warnings, and a bounded searchable log.
- Toggles for expensive diagnostic modes, with their measured overhead and recording state clearly visible.

User-facing panels may include:

- Display mode, scaling, aspect/presentation options, VSync/frame pacing, and overlay scale.
- Audio device and volume.
- Controller assignment, mapping, vibration, and dead zones.
- Stable compatibility options that have documented semantics.
- Import/export/reset of a versioned configuration.

Keep **Settings** and **Developer Tools** visibly separate. Ordinary users should not be presented with raw renderer repair switches or accuracy-breaking hacks.

### Required architecture boundary

Do not add ImGui calls throughout `vulkan_first_frame.cpp`, `playability_probe.py`, or recompilation logic. Create a native UI subsystem such as:

```text
runtime/ui/
  ui_context.*
  settings_model.*
  settings_store.*
  metrics_snapshot.*
  user_settings_panel.*
  developer_panels.*
```

The UI should read immutable or double-buffered snapshots. Mutations should go through a small validated settings API or command queue. Panel code must not reach into renderer/executor internals, mutate global state, write files every frame, or become the new control plane.

One versioned settings model should serve config files, command-line overrides, and ImGui. ImGui's own `imgui.ini` is for window layout, **not** application configuration. Make precedence explicit; if a command-line value overrides a setting, show it as locked and explain why. Mark restart-required settings and reject invalid combinations centrally.

### SDL3/Vulkan integration

- Use `imgui_impl_sdl3` plus `imgui_impl_vulkan` on the now-unified SDL3/Vulkan path. Do not reintroduce competing Win32 and SDL event ownership.
- Render ImGui as the final host overlay after the game image and before present.
- Keep guest-framebuffer captures clean. Include the host overlay only when a capture option explicitly requests it.
- Pin a tagged release or exact commit and compile only the core and required backends into the native build. Do not copy an unversioned pile of source files into the repository.
- The Vulkan backend needs correctly managed descriptor capacity and swapchain/render-target integration; follow the official SDL3+Vulkan example rather than inventing a custom backend.

Official references:

- [Dear ImGui repository](https://github.com/ocornut/imgui)
- [Official Getting Started guide, including SDL3 + Vulkan](https://github.com/ocornut/imgui/wiki/Getting-Started)
- [Official backend documentation](https://github.com/ocornut/imgui/blob/master/docs/BACKENDS.md)
- [Official docking guidance](https://github.com/ocornut/imgui/wiki/Docking)

### Performance and product guardrails

Dear ImGui itself is unlikely to be a major cost if used sensibly. Careless panel code absolutely can be.

- With the overlay disabled, the runtime should pay at most a trivial branch and no instrumentation/allocation cost.
- Sample expensive metrics at 10-30 Hz instead of rebuilding them every rendered frame.
- Cap graph/log histories (for example, 600 samples), avoid per-frame disk access, and do not construct hidden panels.
- Benchmark overlay-off and overlay-on modes. Set a budget such as less than 0.5 ms p95 CPU overhead on the reference system, then enforce it.
- Do not make always-on exact tracing a prerequisite for drawing the UI. Display cheap aggregates by default.
- Start without multi-viewports. They introduce additional native windows and Vulkan swapchain/presentation complexity that this presenter does not need yet.
- Docking is useful for the developer workspace and the official docking branch is maintained, but adopt it only after the basic overlay is stable. Keep the end-user settings screen simple.
- Keyboard and gamepad navigation can be enabled, but user-facing DPI scaling, focus behavior, localization, accessibility, and polished controller navigation still require deliberate work. Immediate mode does not provide product UX for free.

If the user UI grows into onboarding, rich menus, accessibility-heavy flows, or extensive localization, move that surface to a more suitable retained UI. Dear ImGui should remain the diagnostic cockpit and a simple settings surface.

### What ImGui must not become

- A global god object with callbacks that mutate arbitrary emulator state.
- The only way to configure or reproduce a run.
- A hiding place for accuracy hacks.
- A reason to keep Python/runtime internals coupled to presentation.
- An always-on profiler that changes the workload being measured.
- Another 10,000 lines appended to `vulkan_first_frame.cpp`.

### Recommended rollout

1. Split window/event, Vulkan presentation, metrics, and settings out of `VulkanFirstFrameApp`.
2. Establish CMake and pin Dear ImGui and SDL3.
3. Add a minimal overlay with build/run identity and cheap frame metrics.
4. Add the versioned settings model and user settings panel.
5. Add developer panels against bounded snapshots and command queues.
6. Add capture integration and explicit overlay inclusion/exclusion.
7. Establish overlay performance budgets before adding plots and resource browsers.
8. Consider docking only for the developer layout; postpone multi-viewports.

## Debugging workflow audit

The forensic debugging method is one of the project's strongest ideas and one of its weakest implementations.

What works:

- Frozen capture of exact command and resource generations.
- Standalone replay to separate guest behavior from presenter behavior.
- Strict readback and layer-specific comparisons.
- Ability to distinguish guest-side production, transport, interpretation, native Vulkan work, and presentation.

What does not:

- Gigabyte-scale JSON/binary artifacts are treated as routine.
- Full datasets are repeatedly loaded instead of queried or streamed.
- Custom profiling and exact edge collection substantially alter the workload.
- A manual matrix of flags makes runs difficult to reproduce.
- Preflight depends on ignored local artifacts.
- The audit originally found no first-class system-profiler workflow. Priority
  9 now has a self-identifying WPR CPU/GPU capture, deterministic xperf reports,
  WPA/GPUView instructions, and a RenderDoc launch/capture path. Retained ETW
  and RenderDoc evidence plus a crash-dump workflow remain open.

Dear ImGui can make debugging much faster by surfacing bounded state and capture controls in the running application. It should complement reproducible CLI runs and durable reports, not replace them.

A professional debugging ladder would be:

1. Reproduce with a small run manifest and bounded ring-buffer trace.
2. Inspect cheap live metrics in Dear ImGui.
3. Trigger a precise frozen capture around the fault.
4. Replay it in isolation.
5. Use native CPU/GPU profilers for scheduling and GPU questions.
6. Export a compact evidence bundle containing hashes, configuration, logs, and the minimal failing generation.

## Recommended target architecture

```mermaid
flowchart LR
    XBE["Supported XBE + identity manifest"] --> TOOL["Python offline analysis and code generation"]
    TOOL --> IR["Versioned IR / analysis database"]
    IR --> AOT["Deterministic native AOT build"]
    AOT --> CPU["Native guest CPU and dispatcher"]
    CPU --> SVC["Native Xbox services"]
    CPU --> GPU["NV2A translation and Vulkan renderer"]
    SDL["SDL3 window, events, input, audio"] --> CPU
    SDL --> GPU
    GPU --> UI["Dear ImGui host overlay"]
    CPU --> SNAP["Bounded metrics and control boundary"]
    GPU --> SNAP
    SNAP --> UI
    SNAP --> TRACE["Bounded binary traces"]
    TRACE --> REPORT["Python offline reports"]
```

Suggested native module boundaries:

- `runtime/cpu`: generated-code ABI, dispatcher, scheduler, fallback interface.
- `runtime/memory`: guest memory, dirty generations, observations, protection.
- `runtime/xbox`: kernel, filesystem, input, audio, timing, title hooks.
- `runtime/nv2a`: command decoding, state, shaders, surfaces, textures.
- `runtime/vulkan`: device, swapchain, resources, synchronization, pipelines, readback.
- `runtime/platform/sdl`: window, events, input, audio, DPI.
- `runtime/ui`: Dear ImGui context, settings, snapshots, user and developer panels.
- `runtime/diagnostics`: counters, bounded tracing, capture, run manifests.
- `tools`: offline Python analysis, generation, reports, fixtures, and CLI.

Title-specific code should live behind explicit hooks with names, evidence, supported-build guards, tests, and an explanation of whether each hook is a hardware semantic, OS semantic, compiler recovery, or compatibility repair.

## Ruthless priority order

### P0: make measurements trustworthy

- [x] Add complete run/build identity and stale-artifact rejection.
- [x] Produce a clean O2, warm-cache, profiling-off, overlay-off baseline. The
  July 23 retained sample is 23 completed guest flips over 1.001 seconds, or
  22.986 guest FPS, under run
  `a0e9bb76-365c-4319-a3fc-adbd233f333a`.
- [ ] Produce the overlay-on comparison after the P4 host overlay exists.
- [x] Enforce exact XBE provenance.

### P1: remove obvious live-path waste

- [x] Stop forced resource scans. Binding and guest-page generations now gate
  resource snapshots; `force=True` only forces the exact flip manifest/command
  boundary.
- [x] Replace per-slice controller filesystem polling. Snapshot checks are
  fixed-rate at a maximum of 60 Hz while the last valid input remains resident.
- [x] Bound diagnostic collection. Startup/tail run histories, native edge
  tables/output, and callback-address samples have explicit limits and dropped
  counters.
- [x] Retain presenter state and transmit deltas. Commands are append-only
  spans, unchanged resource generations are explicit, and identity-valid host
  resources and NV2A interpreter state remain resident.

### P2: fix the process boundary

- [x] Replace normal-run JSON/filesystem IPC with shared memory and fixed binary
  schemas. Sequence-guarded control records carry input, stop, acknowledgement,
  and the render manifest; a bounded SPSC ring carries command spans; alternating
  immutable slots carry resource generations. Lossless audits and requested F12
  captures deliberately materialize standalone files.
- [x] Converge the shipping runtime toward one native process. Normal unbounded
  gameplay uses one OS process; bounded and exact audit tools retain isolation.
- [x] Move the normal scheduler, dispatch, memory synchronization, and reached
  host-service path out of Python.
  - [x] Keep normal guest/ABI continuation in one native dispatch session and
    retain dirty pages and generations across native service/presentation
    boundaries. Python only prepares and validates artifacts and materializes
    post-run diagnostics.
  - [x] Own measured clock/yield/title-XInput service bodies, cooperative
    cadence, and the presenter in the normal native gameplay process. Bounded
    and exact audit tools deliberately retain isolated diagnostic processes.
  - [x] Route reached cold runtime calls through the native service table with
    native ABI continuation, persistent worker discovery/selection/lifecycle,
    and zero normal-run Python callbacks. A later unknown target remains an AOT
    coverage gap; a later missing service requires another native body.
  - [x] Seed the synthetic D3D context's NV2A service pointer with the title's
    `0xFD000000` MMIO base. The real native idle routine now observes modeled
    PFIFO status and returns under normal slice cadence; the temporary per-poll
    yield and forced worker tick were removed after bounded live sampling showed
    they amplified the bad zero pointer instead of scheduling useful work.
- [x] Make live compilation developer-only and persist discoveries for AOT.
  Normal runs require ahead-of-time decoded coverage and do not interpret or
  promote unknown blocks. Explicit diagnostic discovery may record a frontier
  in the SQLite decoded block store for the next deterministic native build;
  DLL promotion requires `--developer-live-compile`.

### P3: professionalize the project

- [x] Add reproducible builds, dependencies, tests, analysis, and maintenance.
  - [x] Add `pyproject.toml` and exact-version runtime/development lock files.
  - [x] Add a Windows CI gate for bytecode compilation, Ruff, maintenance
    budgets, synthetic preflight, and the Python unit suite.
  - [x] Add dry-run-first cache/report inspection, retention, `clean-reports`,
    and `clean` commands with path-confinement tests.
  - [x] Add pinned CMake/Ninja tooling and debug, release, profiling, and
    ASan/UBSan presets. The optional strict release target builds both presenter
    forms and source-dependent SPIR-V against the installed Vulkan/SDL3 SDK.
  - [x] Add the first meaningful C++/CTest layer for the standalone NV2A
    vertex-program boundary and run debug/release/sanitizer configurations in
    Windows CI.
  - [x] Pin the compiler, Vulkan SDK, and SDL3 revisions and exact native
    artifacts; expand native tests to renderer ownership/lifetime/
    synchronization boundaries; add strict type checking.
- [x] Split monoliths along actual ownership boundaries. Transport layout,
  dirty-range management, completed-flip metrics, pipeline identity, and NV2A
  texture layout/unswizzling are isolated, dependency-light modules with direct
  native tests. Presenter options, logging, reporting, live transport, NV2A
  processing, presenter session flow, Vulkan device/rendering, retained
  resources, capture/diagnostics, and SDL3 platform behavior now have separate
  implementation modules. SDL3 owns the window/event/input/surface lifetime;
  Vulkan owns only the explicit renderer lifetime.
- [x] Make preflight runnable from a fresh clone with ephemeral synthetic asset,
  render-stream, rendered-frame, and guest-health fixtures.
- [x] Establish reviewable commit discipline and contributor/legal
  documentation through pre-commit, a PR checklist, `CONTRIBUTING.md`, MIT
  `LICENSE`, `NOTICE.md`, and the explicit original-game asset policy.

### P4: add Dear ImGui deliberately

- Add it only after the metrics/settings boundary exists.
- Start with identity, frame timing, capture status, and stable settings.
- Grow developer panels from bounded snapshots.
- Treat docking as optional developer ergonomics and defer multi-viewports.

## Final assessment

The project is not doomed and does not need a ground-up rewrite. Its reverse-engineering knowledge and renderer validation are the expensive assets. The disposable part is the prototype shell that accumulated around them.

The worst possible next move is to keep adding features—Dear ImGui included—inside the existing monoliths. The correct move is to turn the current prototype into an oracle, define stable data and runtime boundaries, migrate hot execution into a native AOT runtime, and make every build and benchmark self-identifying and reproducible.

Use Python aggressively where it is strongest: offline analysis, generation, automation, and reports. Use SDL3 where the host platform is ordinary: windowing, events, input, audio, and Vulkan surface creation. Keep direct Vulkan for the renderer. Use Dear ImGui as a thin native diagnostic and settings surface over a validated model.

That would transform `b2_recomp` from an impressive personal experiment into a credible recompilation project.
