# Architecture

b2_recomp separates reproducible offline recovery from the native gameplay hot
path. Python owns analysis, generation, orchestration, diagnostic reports, and
exact audit tools. C++ owns resident guest dispatch, hot services, worker
lifecycle, shared-memory publication, SDL audio/input, and Vulkan presentation.

## Normal runtime flow

1. The launcher validates the exact XBE and content-addressed presenter build.
2. One native process hosts the persistent guest dispatcher and presenter.
3. Native service routing handles hot calls and guest ABI continuation. Python
   supplies cold service bodies and tooling without rebuilding scheduler state.
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
and worker lifecycle state. Cold service bodies can still execute in Python,
but they return through the resident dispatcher instead of rebuilding guest or
worker state at every cadence boundary.

Dirty guest pages remain in the bound native page view across service calls.
Native per-page write generations make resource invalidation explicit and
avoid full memory scans or unconditional copyback. Newly discovered executable
blocks are interpreted and persisted for the next deterministic AOT build.
Runtime native promotion is available only in the visibly nonrepresentative
`--developer-live-compile` mode.

### Completed-flip publication

Each completed NV097 flip publishes an immutable command interval and the
resource generations it references. Normal live mode writes command deltas to
a bounded SPSC shared-memory ring and changed resources to alternating
immutable slots. The presenter copies and validates a delta before advancing
the read cursor, and slots are reused only after the completed-flip
acknowledgement makes that safe.

The producer retains append-only command history until at least 1,048,576
completed records (16 MiB) can be retired at an exact flip boundary. Absolute
base and record counts preserve continuation and diagnostic identity across an
epoch change. Lossless audit mode deliberately retains cumulative snapshots so
each audited flip remains a standalone replay.

Normal presentation never skips completed guest flips or substitutes reduced
render work. Guest execution and presentation overlap through the publication
and acknowledgement handshake; lock-step depth one remains available for
exact audits.

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
live mappings, and diagnostics own their output formats. Python retains
title-specific audio decoding and mix policy and submits normalized PCM16
chunks through the presenter's narrow native audio ABI. The renderer retains
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
