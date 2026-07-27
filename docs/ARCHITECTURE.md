# Architecture

b2_recomp separates reproducible offline recovery from the native gameplay hot
path. Python owns analysis, generation, orchestration, diagnostic reports, and
exact audit tools. C++ owns resident guest dispatch, an expanding set of host
services, worker lifecycle, shared-memory transport, SDL audio/input, and
Vulkan presentation. The normal-play migration is complete through the
currently reached path; Python remains the offline analysis, artifact,
launcher, and post-run diagnostic layer rather than a guest-execution callback
boundary.

## Normal runtime flow

1. The launcher validates the exact XBE and content-addressed presenter build.
2. One native process hosts the persistent guest dispatcher and presenter.
3. Native service routing handles registered calls and guest ABI continuation.
   Reached normal-play service bodies do not cross into Python. Unknown guest
   targets are rejected as AOT coverage gaps and persisted only by explicit
   developer diagnostics for a subsequent deterministic build.
4. A sequence-guarded control mapping publishes input, stop, acknowledgement,
   and completed-flip metadata. A bounded SPSC ring publishes command deltas;
   alternating immutable slots publish changed resource generations.
5. The Vulkan presenter retains interpreter and GPU objects while their
   identities remain valid and presents every completed guest flip.

Bounded probes, lossless audits, and frozen replay deliberately retain isolated
process/file paths because exact diagnostics are their product. They are not
the shipping gameplay boundary.

## Runtime mechanics

### Resident guest execution

Normal execution keeps one native dispatch session across consecutive guest
modules and registered host calls. The dispatcher owns routing, guest ABI
continuation, measured clock/yield services, title input, cooperative cadence,
and worker lifecycle state. Reached normal-play service bodies execute in
native code; Python service callbacks remain limited to explicit diagnostic
paths.

Dirty guest pages remain in the bound native page view across service calls.
Native per-page write generations make resource invalidation explicit and
avoid full memory scans or unconditional copyback. An unknown executable target
stops normal execution as an AOT coverage gap. Explicit developer diagnostics
may interpret and persist that target for the next deterministic build;
runtime native promotion is available only in the visibly nonrepresentative
`--developer-live-compile` mode.

## Python runtime migration checkpoint (2026-07-24)

| Audit poor-fit path | Current ownership |
| --- | --- |
| Per-slice scheduler and guest loop | Complete for normal play: the resident native coordinator owns primary and worker execution, lifecycle, waits/yields, cadence, and stop handling. |
| ABI and call dispatch | Complete for normal play: registered services unwind and return in the dispatcher; validated bounded runs report zero Python ABI, handler, or cold-host callbacks. |
| Dirty-memory synchronization | Complete for normal play: native page views, dirty ranges/lists, generations, and resource ownership stay resident. Python only materializes the final post-run diagnostic snapshot. |
| Hot Xbox services | Complete for the currently reached normal path: allocation, persistence, save crypto, AV, threading/semaphores, timing, input, filesystem, offline PHY, and reached title services are native. Address-taken guest code remains an AOT-coverage concern. |
| Audio callback/data movement | Native-owned for normal play: RWS PCM/Xbox ADPCM decode, DirectSound buffer/stream observation, mixing, buffering, and SDL3 ABI submission replace the former Python mix path. The reached buffer `Play` method now completes guest voice state, HRESULT, and stack cleanup in the native service dispatcher instead of forwarding into the Xbox SDK hardware body; manual gameplay acceptance remains pending. |
| Runtime compilation and DLL promotion | Removed from normal execution. Developer live compilation remains an explicit diagnostic-only mode. |
| High-frequency render transport | Complete for normal play: completed-flip detection, aperture-normalized command publication, immutable resource slots, named-event wakeups, and presentation waits are native/shared-memory. Bounded run `e0884dd7-5a27-403f-88c6-a4be1263df12` published and acknowledged 53 exact generations; the presenter applied 52 incremental reloads with zero Python callbacks or frontier-interpreter work. |

The former `sample.xsb` XACT failure is cleared. Warm runs pass the old
8.355-million-instruction semaphore boundary and remain native-clean through
20 million instructions; longer runs reach about 91 million instructions with
frontend audio decoded and mixed natively. Static AOT closure resolves bounded
absolute IA-32 jump tables, including the CRT `memcpy` family, and now includes
the address-taken `0x00073150` and `0x00055970` targets. The initial July 25
acceptance run was fully warm (96 native cache hits, no misses or compilation).
The subsequent render acceptance run completed 2 million instructions, 53
native manifest/resource publications, and 53 presentation acknowledgements
with normal-runtime failure code zero.

Native title streaming also owns Track PSS prelinked-image publication. The
service validates the file-authored stream and scene-record tables, then copies
the exact image into its encoded guest address during the first synchronous
read. This preserves the absolute pointers consumed by level loading without a
Python callback or runtime-generated guest code.

An unknown native dispatch target terminates normal execution as transport code
9. Post-run artifact preparation validates that the target is executable,
decodes it into the SQLite decoded block store, and marks the diagnostic as
requiring a static rebuild. The stopped run never interprets, compiles, or
continues the discovered block; the next AOT build consumes it.

Known address-taken callback families are recovered during deterministic
artifact preparation rather than discovered one member per normal run. The
currently reached level-loading table contributes 11 exact callback entries to
the decoded store and therefore to the next static native build.

The normal scheduler also owns registered D3D vertical-blank delivery. It
reads the title callback from the emulated D3D context, constructs the Xbox
`D3DVBLANKDATA` payload, and dispatches it through a separate native AOT context
at a wall-clock-paced 60 Hz. This preserves primary-thread registers while the
callback advances guest frame state and asynchronous asset work; missing
callback code remains a transport-code-9 AOT coverage gap rather than a runtime
interpreter path.

The isolated process model used by bounded and audit runs assigns its guest and
presenter children to a Windows kill-on-close job. Normal cleanup still requests
guest stop and waits before forcing termination; if the launcher disappears
entirely, closing its last job handle makes child cleanup a kernel-owned
invariant. Unbounded normal gameplay remains the single embedded process.

### Completed-flip publication

Each completed NV097 flip publishes an immutable command interval and the
resource generations it references. Normal live mode writes command deltas to
a bounded SPSC shared-memory ring and changed resources to alternating
immutable slots. The presenter copies and validates a delta before advancing
the read cursor, and slots are reused only after the completed-flip
acknowledgement makes that safe.

The producer canonicalizes the title's dynamically allocated D3D ring into the
renderer's stable `0x80000000` 16 MiB command aperture before publication. The
binary manifest carries both named-event identities so every generation wakes
the presenter and every acknowledgement releases the native guest wait.

The producer retains append-only command history until at least 1,048,576
completed records (16 MiB) can be retired at an exact flip boundary. Absolute
base and record counts preserve continuation and diagnostic identity across an
epoch change. Lossless audit mode deliberately retains cumulative snapshots so
each audited flip remains a standalone replay.

Normal presentation never skips completed guest flips or substitutes reduced
render work. Guest execution and presentation overlap through the publication
and acknowledgement handshake; lock-step depth one remains available for
exact audits. Normal play consumes and acknowledges at most one publication at
each 16.667 ms presenter boundary, so the completed guest-frame rate is capped
at the title's 60 Hz output rate even on a higher-refresh host display.

### Platform and control

SDL3 owns the ordinary platform boundary: window lifecycle, events, keyboard,
gamepads, DPI, audio device/stream, and Vulkan surface creation. Input, stop,
and completed-flip acknowledgements use sequence-guarded records in one named
mapping. Digital input is retained for at least 150 ms and two guest flips so
short taps survive native slices and slow title polling. Direct tools without
the mapping retain a legacy file fallback.

The launcher sets the shared stop flag when the presenter closes. Isolated
diagnostic modes use bounded process-tree cleanup only when graceful shutdown
fails.

## Source ownership

| Area | Responsibility |
| --- | --- |
| `runtime/xbox/` | Guest ABI, hardware, scheduler, and service semantics |
| `runtime/host/vulkan_first_frame.cpp` | Thin executable/DLL entry adapter |
| `runtime/host/vulkan_presenter.cpp` | Presenter session, live reload, pacing, platform dispatch, and acknowledgement |
| `runtime/host/vulkan_presenter_runtime.h` | Private presenter declaration and shared runtime state |
| `runtime/host/vulkan_presenter_internal.h` | Renderer-private state and template-based stream interpretation |
| `runtime/host/vulkan_renderer.cpp` | Vulkan instance/device/swapchain, pipelines, command recording, synchronization, and cleanup |
| `runtime/host/vulkan_resources.cpp` | Texture conversion, host texture/binding caches, and offscreen render targets |
| `runtime/host/presenter_diagnostics.cpp` | Presented-geometry diagnostics, metrics collection, readback, and screenshots |
| `runtime/host/nv2a_command_processor.cpp` | NV2A command, draw-state, vertex, and recovered-resource processing |
| `runtime/host/live_presenter_transport.*` | Live shared-memory transport and legacy diagnostic-file fallback |
| `runtime/host/presenter_options.*` | Presenter command-line contract |
| `runtime/host/presenter_debug_log.*` | Structured presenter event logging |
| `runtime/host/presenter_metrics.*` | Immutable metrics snapshots and report output |
| `runtime/platform/sdl/sdl_platform.*` | SDL3 window, events, keyboard/gamepads, hotkeys, and Vulkan surface |
| `runtime/platform/sdl/sdl_audio*` | SDL3 playback device, audio stream, bounded queue, reset, shutdown, and C ABI bridge |
| `runtime/host/live_transport_layout.h` | Versioned wire layout and seqlock publication rules |
| `runtime/host/dirty_ranges.h` | Dirty-span normalization, merging, and accounting |
| `runtime/host/frame_metrics.h` | Completed-flip FPS sampling |
| `runtime/host/native_pipeline_state.h` | Pipeline identity and cache key semantics |
| `runtime/nv2a/texture_layout.h` | NV2A texture format/layout and Morton unswizzle rules |
| `tools/analysis/`, `tools/recomp/` | Offline recovery, lifting, generation, and coverage |
| `tools/playability/` | Launch, bounded probes, strict audits, and report composition |

Dependency-light renderer cores intentionally have no SDL window or Vulkan
device dependency. This makes transport layout, dirty ranges, texture layout,
pipeline identity, FPS sampling, and vertex-program behavior directly testable
in CTest. The presenter is a composition root: SDL3 owns ordinary platform
behavior—including the host audio device and stream—the transport module owns
live mappings, and diagnostics own their output formats. The native runtime
owns title-specific audio decoding and mix policy and submits normalized PCM16
chunks through the presenter's narrow audio ABI. The renderer retains
direct ownership of the Vulkan instance, device, swapchain, resources,
synchronization, pipelines, and readback because those objects share one
explicit GPU lifetime.

## Boundary rules

- Python must not regain per-slice scheduling, filesystem polling, or normal
  per-frame JSON/payload IPC.
- Immutable completed-flip publication and sequence checks are correctness
  boundaries; producer data is never read while its sequence is odd or changes.
- Host resources are retained only under explicit identity/generation keys.
  Overlapping writes invalidate or merge dirty spans before reuse.
- Title-specific repairs need a supported-build guard, evidence, a named hook,
  and a focused regression.
- Diagnostic and audit paths are bounded and disclose truncation. Normal
  gameplay keeps profiling, capture, and lossless materialization opt-in.

See [BUILDING.md](BUILDING.md) for toolchain enforcement and
[VALIDATION.md](VALIDATION.md) for retained evidence and known limitations.
