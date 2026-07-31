# Burnout 2: Point of Impact Static Recompilation

This repository explores static recompilation of the Xbox release of *Burnout
2: Point of Impact*. It rehosts recovered IA-32 game code behind native
services for graphics, audio, input, files, timing, threading, and memory while
preserving guest-visible behavior.

The Xbox release is the base target because its x86 CPU makes it a practical
static-recompilation candidate. This is reverse engineering, not a source port.
Original game assets, executable data, trademarks, and copyrighted content
remain owned by their rights holders.

## Project status

Updated: July 26, 2026.

The recovered boot path runs through a resumable native guest loop, presents
completed NV2A frames through Vulkan, and uses SDL3 for frontend audio plus
keyboard and gamepad input. Lesson One renders the world, HUD, and player car
at the correct chase-camera scale. Guest throughput and unimplemented title
semantics remain active work.

| Area | Status |
| --- | --- |
| XBE inspection and loader | Complete for the current title |
| Analysis database | Complete and reproducible |
| Runtime ABI shims | Implemented for the currently reached path |
| IA-32 execution | Recovered boot, frontend, and Lesson One path running |
| Vulkan presentation | Live completed-flip presentation with strict validation |
| Audio and input | Native PCM/Xbox ADPCM observation, mixing, SDL3 submission, and buffer `Play` ABI completion; gameplay audio still needs manual acceptance |
| Provenance | Exact supported-XBE gate and content-addressed build/run manifests |
| Save states | Not implemented |

Detailed rendering coverage, retained measurements, and current limitations
are maintained in [docs/VALIDATION.md](docs/VALIDATION.md).

## Ground rules

- Supply your own legally obtained Xbox copy.
- Do not commit or distribute ISOs, extracted assets, XBE sections, SDK files,
  symbols, generated title code/data, or proprietary local reports.
- Keep extraction, analysis, lifting, patching, and build steps scripted.
- Prefer observed behavior and ABI evidence over title-specific approximations.
- Treat graphics, audio, input, files, timing, threading, and memory as explicit
  host boundaries.

The complete contribution boundary is documented in
[docs/ASSET_POLICY.md](docs/ASSET_POLICY.md). Project-owned source is MIT
licensed; third-party and proprietary inputs are excluded.

## Quick start

The supported development host is Windows x86-64. Install the locked Python
dependencies:

```powershell
python -m pip install --requirement .\requirements-dev.lock
```

Install the extraction tool and extract your local image:

```powershell
.\tools\extract\install_extract_xiso.ps1
python .\tools\extract\extract_disc.py `
  --iso ".\Burnout 2\Burnout 2 - Point of Impact (USA).xiso.iso"
```

Run the complete asset-free gate, then launch normal live execution:

```powershell
python .\tools\quality_gate.py --full
python .\tools\playability\live_test.py
```

The native presenter requires the pinned Vulkan/SDL3 toolchain. Initial native
recovery can take substantially longer than a warm launch. Setup details,
target verification, local paths, controls, and ordinary runtime options are in
[docs/GETTING_STARTED.md](docs/GETTING_STARTED.md).

## Documentation

| Document | Purpose |
| --- | --- |
| [Getting started](docs/GETTING_STARTED.md) | Setup, local data, target identity, normal launch, and controls |
| [Architecture](docs/ARCHITECTURE.md) | Runtime boundaries, ownership, and data flow |
| [Building](docs/BUILDING.md) | Locked toolchain, native presets, quality gates, and artifact maintenance |
| [Debugging commands](docs/DEBUGGING_COMMANDS.md) | Live diagnostics, exact audits, frozen replay, and direct probes |
| [Profiling](docs/PROFILING.md) | ETW/WPA, GPUView, and RenderDoc capture protocol |
| [Validation evidence](docs/VALIDATION.md) | Retained results, performance history, and known limitations |
| [Asset policy](docs/ASSET_POLICY.md) | Legal and repository-content boundary |
| [Contributing](CONTRIBUTING.md) | Change discipline and required checks |

## Runtime overview

Normal play keeps the guest dispatcher and Vulkan presenter in one process:

1. The launcher verifies the exact XBE and presenter build identities.
2. Native code owns resident guest dispatch, registered service ABI
   continuation, primary/worker scheduling, allocation state, dirty-memory
   ownership, reached kernel/title services, audio decode/mix/submission, and
   render publication. Python prepares deterministic artifacts, validates
   identity, launches the runtime, and materializes post-run diagnostics.
3. A versioned shared-memory control record carries input, stop, and
   completed-flip acknowledgements. A bounded command ring and immutable
   resource slots publish NV2A work.
4. SDL3 owns ordinary host-platform behavior, including windowing, events,
   input, audio-device/stream management, DPI, and Vulkan surface creation.
5. Direct Vulkan retains device, swapchain, resource, pipeline,
   synchronization, presentation, and readback ownership.

The native normal runtime decodes title-specific RenderWare PCM/ADPCM, applies
gain and looping policy, mixes normalized PCM16, and submits it through the
narrow SDL3 presenter ABI. Bounded probes, lossless audits, and frozen replays
intentionally retain isolated Python diagnostic paths.

The loading path now services the title's registered D3D vertical-blank
callback natively at a wall-clock-paced 60 Hz, which unblocks the asynchronous
asset pump that previously remained on `Loading - please wait`. The reached
`IDirectSoundBuffer::Play` wrapper is now a complete native host ABI boundary:
it commits the recovered guest voice state, returns the guest HRESULT, performs
the exact stack cleanup, and does not enter the Xbox SDK hardware path. Manual
gameplay acceptance beyond the former stall remains pending; current status and
the exact retained snapshot are recorded in
[docs/VALIDATION.md](docs/VALIDATION.md#july-26-native-vblank-and-audio-investigation).

The native-boundary checkpoint now reports zero Python runtime ABI, handler,
cold-host, memory, observer, and slice-yield callbacks on warm bounded runs
through the currently reached path. One Python dirty-page materialization
occurs only after guest execution to compose the diagnostic report. Bounded run
`e0884dd7-5a27-403f-88c6-a4be1263df12` completed the render-publication
acceptance: native code published 53 exact command/resource generations, the
presenter acknowledged all 53 and applied 52 incremental reloads, and all
generations passed render validation with zero frontier-interpreter activity
and runtime compilation disabled. See
[docs/VALIDATION.md](docs/VALIDATION.md#native-migration-acceptance).

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full ownership model.

## Validation

The current Python unit suite passes 675 tests and the current native suite
passes 13 CTest cases; the last retained asset-free closeout gate passed 548
Python tests and 10 native CTest cases. The validated
exact-target baseline reaches Lesson One with live Vulkan rendering, SDL3
frontend audio and input, local storage, streamed track data, and cooperative
workers. Native local storage includes first-run Xbox cache-partition table
assignment and FATX device formatting. The migrated native path has cleared the
former `sample.xsb` XACT
failure and runs native-clean through 20 million guest instructions, with
longer coverage probes reaching about 91 million. The `0x00073150` and
`0x00055970` address-taken targets are persisted, and the fully warm first-flip
acceptance run completes the current native migration checkpoint.

Local evidence under `reports/local/` is ignored and must not be committed.
Performance and compatibility claims must cite their exact run identity,
configuration, cache state, sampling duration, and workload. Historical
results and caveats live in [docs/VALIDATION.md](docs/VALIDATION.md).

## Repository layout

```text
docs/             Setup, architecture, building, debugging, and validation
runtime/
  host/          Presenter orchestration, Vulkan renderer, transport, diagnostics
  nv2a/          Dependency-light NV2A texture and vertex-program support
  platform/sdl/  SDL3 window, events, input, audio, and Vulkan surface
  xbox/          Xbox runtime and hardware shims
tools/
  analysis/      Project-owned analysis database
  extract/       Local disc extraction
  host/          Presenter build and smoke wrapper
  loader/        XBE mapping and loader tools
  playability/   Live launcher, probes, audits, and reports
  profiling/     System-profiler orchestration
  recomp/        IA-32 lifting, execution, coverage, and C++ emission
  render/        D3D8/NV2A stream normalization
  runtime/       Runtime smoke tools
  xbe/           XBE inspection
tests/unit/      Python unit and regression tests
tests/native/    Asset-free C++/CTest regressions
```

`data/local/`, `reports/local/`, build/cache directories, extracted game
content, generated C++, and recovered stream reports are ignored.

## Development priorities

1. Build a bounded, workload-locked gameplay performance distribution.
2. Promote corrected complete-world and Load/Save captures into strict
   preflight replay coverage.
3. Optimize remaining guest simulation cost without changing guest-visible
   behavior.
4. Design a versioned save state covering CPU, sparse-memory changes, runtime
   objects, audio, and the live bridge.
5. Characterize longer-run input/audio behavior and replace remaining
   probe-only models when evidence permits.
6. Extend IA-32, NV2A, and Xbox ABI support only for observed paths, with a
   focused regression for every new edge.

See [LICENSE](LICENSE) and [NOTICE.md](NOTICE.md) for licensing and third-party
notices.
