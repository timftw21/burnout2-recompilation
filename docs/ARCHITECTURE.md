# Architecture

b2_recomp separates deterministic offline recovery from native runtime
execution. Python analyzes the supported XBE, prepares content-addressed
artifacts, launches native processes, and composes diagnostics. Normal Phase 7
guest execution is intended to stay entirely inside ahead-of-time native code.

The architecture is still under active development. The current static IA-32
artifact reaches the Load/Save frontend and then fails closed at an unverified
indirect call while creating a new save slot. A historical renderer/runtime can
reach Lesson One, but that does not make the current static path complete.

## Static recompilation boundary

Guest code used by an ordinary run must be analyzed, decoded, lifted, and
compiled before launch. The runtime may dispatch only to:

- decoded guest instruction starts included in the artifact;
- verified finite indirect-target sets;
- exact native host-service thunks; or
- explicitly modeled scheduler-return sentinels and static hardware rewrites.

An unknown executable target is an AOT coverage gap. The normal process records
the address and stops. A separate developer diagnostic may gather evidence and
add the target to the decoded block store, but the resulting code is consumed
only by the next deterministic build.

Normal execution must not enable an interpreter, JIT, runtime decoding,
runtime code generation, native promotion, automatic backend fallback, or
Python guest callbacks. `--developer-live-compile` is diagnostic-only and
invalidates normal-run evidence.

## Phase 7 process model

The work-in-progress static path uses three cooperating ownership layers:

1. **Python launcher and tooling** validates the XBE, decoded block store,
   toolchain, artifact, and presenter identities; starts native processes; and
   writes post-run reports.
2. **32-bit native guest process** maps the verified Xbox address space and owns
   AOT guest dispatch, primary/worker/vblank lanes, reached service bodies,
   guest memory, dirty-page generations, filesystem state, controller
   consumption, command/resource publication, and audio decode/mix/publication.
3. **64-bit native presenter process** owns SDL3 windowing, host input and audio,
   Vulkan device/swapchain/resources/pipelines, presentation, screenshots, and
   render diagnostics.

The native processes communicate through versioned shared-memory layouts and
named events. Python does not mediate per-frame data or service calls.

## Runtime data flow

1. The launcher verifies the exact supported XBE and selected normal-live
   artifact. A fixed replay artifact or stale toolchain identity is rejected.
2. The guest process maps the boot image, code spans, stacks, TLS, static
   rewrites, host-service thunks, and shared transports at validated addresses.
3. The resident scheduler runs the primary context and separately retained
   worker/vblank contexts. Each lane owns GPRs, flags, FPU/SIMD state, EIP,
   stack, and TLS.
4. Native service dispatch preserves the declared Xbox ABI, including arguments,
   stack cleanup, return values, flags, and bounded memory effects.
5. Completed guest flips publish immutable command intervals and changed
   resource generations. The presenter validates and acknowledges each consumed
   generation before the producer reuses its slots.
6. The presenter publishes controller state to the control mapping and consumes
   normalized PCM16 publications through the dedicated audio slot.
7. Closing the presenter sets the shared stop word. Native ownership writes one
   dependency-free summary; the launcher then composes higher-level reports.

## Native ownership by subsystem

| Boundary | Native responsibility | Current status |
| --- | --- | --- |
| Guest dispatch | Fixed-address AOT modules, direct edges, verified indirect dispatch, and fail-closed guards | Active; coverage remains incomplete beyond the save frontend |
| Scheduling | Primary, cooperative worker, and 60 Hz vblank contexts | Native in Phase 7 |
| Memory | Fixed mappings, aliases, dirty ownership, architecture rewrites, and bounded device shadows | Native for the reached path |
| Host ABI | Exact registered kernel/title services and x64 broker requests where required | Native for the reached path; unknown services fail closed |
| Filesystem | Xbox path translation, persistent local storage, file/object tables, and reached save/cache operations | Native; current new-save path has not completed |
| Input | SDL3 event/gamepad collection and IA-32 XInput consumption | Manually confirmed at the frontend |
| Audio | Title RWS parsing, PCM/Xbox ADPCM conversion, mixing, PCM transport, and SDL3 submission | Menu music manually confirmed; broader continuity remains open |
| Rendering | Native command/resource publication and retained Vulkan presentation | Frontend confirmed; current static gameplay revalidation remains open |
| Diagnostics | Fault sidecar, transport events, render/performance reports, and local CDB metadata | Native/local tooling; not a gameplay dependency |

## Completed-flip publication

The guest canonicalizes the title's dynamic push ring into a stable command
aperture. A bounded SPSC ring carries command deltas, and alternating immutable
slots carry changed resource generations. Publication is protected by sequence
guards; a consumer rejects odd or changing sequences and advances the read
cursor only after copying and validating the complete record.

The presenter acknowledges a completed generation before the producer retires
its command prefix or reuses resource slots. Normal presentation does not skip
guest flips or replace them with reduced render work. Lossless audits use a
depth-one handshake; ordinary presentation may overlap guest production and
presentation at depth two.

## Control and audio transport

The control mapping contains versioned input, stop, acknowledgement, and guest
summary records. SDL3 publishes controller state through a seqlock; the IA-32
runtime consumes that state through exact XInput service bodies. Digital input
is latched long enough to survive slow guest polling, but gameplay evidence is
still collected through manual input.

A separate bounded PCM slot prevents audio publication from colliding with the
render manifest. The IA-32 mixer publishes normalized 48 kHz stereo S16 chunks;
the presenter validates the sequence and submits them to the SDL3 audio stream.

## Offline artifacts and provenance

The Phase 7 build composes several content-addressed layers:

- XBE section and decoded-store snapshot identity;
- direct-edge relocations and verified indirect-target sets;
- architecture rewrites and memory ownership;
- host-ABI and service maps;
- resident scheduler and lane state;
- measured coverage-growth profile; and
- launcher/normal-live contracts, boot image, native executable, and sidecars.

The loader verifies source, toolchain, executable, manifest, and sidecar hashes
before launch. A warm run is static-clean only when the manifest and runtime
summary both report zero frontier activity, Python callbacks, runtime
compilation/decoding/patching, promotion, and cross-backend exits.

## Diagnostics versus normal execution

Frozen replay, differential comparison, render audits, CDB, ETW, and RenderDoc
are developer tools. They may use separate processes, bounded instrumentation,
or diagnostic-only oracles because evidence is their output. Every report must
identify its mode and must not be presented as ordinary gameplay performance.

The default `live_test.py` backend currently remains the diagnostic oracle. The
static backend is selected explicitly with `--guest-backend same-isa-ia32` and
a verified normal-live artifact. There is no automatic fallback between them.

## Source ownership

| Area | Responsibility |
| --- | --- |
| `tools/recomp/` | IA-32 lifting, decoded-store artifacts, proof contracts, static generation, and debug metadata |
| `tools/playability/` | Launch orchestration, manual capture, replay/audit tools, and report composition |
| `runtime/xbox/` | Xbox ABI, scheduler, hardware, and service semantics used by the broader runtime |
| `runtime/host/live_presenter_transport.*` | Native shared-memory transport and diagnostic file fallback |
| `runtime/host/live_transport_layout.h` | Versioned control/command/resource/audio wire layout |
| `runtime/host/vulkan_presenter.cpp` | Presenter lifecycle, publication consumption, pacing, and platform orchestration |
| `runtime/host/vulkan_renderer.cpp` | Vulkan device, swapchain, pipelines, command recording, synchronization, and cleanup |
| `runtime/host/vulkan_resources.cpp` | Texture conversion, resource caches, and offscreen render targets |
| `runtime/host/nv2a_command_processor.cpp` | NV2A command/state/vertex processing |
| `runtime/host/presenter_diagnostics.cpp` | Metrics, readback, screenshots, and render reports |
| `runtime/platform/sdl/` | SDL3 windowing, events, gamepads, audio stream, DPI, and Vulkan surface |
| `runtime/nv2a/` | Dependency-light NV2A format/layout and shader support |

## Boundary rules

- Do not optimize across a Python runtime callback; remove the callback first.
- Do not make a dynamic target generally executable to clear one guarded call.
- Recover finite object/vtable, callback, import, and jump-table families with
  exact byte and provenance checks.
- Preserve guest-visible ABI and state at every observable boundary.
- Keep proprietary captures and generated title material in ignored local paths.
- Treat screenshots and performance captures as scoped evidence, not completion
  claims.

See [BUILDING.md](BUILDING.md) for artifact construction,
[DEBUGGING_COMMANDS.md](DEBUGGING_COMMANDS.md) for guarded-call triage, and
[VALIDATION.md](VALIDATION.md) for current evidence and limitations.
