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

Updated: July 31, 2026.

The supported Xbox build boots through a static, resumable native guest loop
and is playable through the frontend and Lesson One. Vulkan output is close to
the original console, and SDL3 audio, keyboard, and gamepad input are active.
Performance is the main blocker: current gameplay is well below the 60 Hz
simulation target. Autosave and controller rumble remain incomplete.

| Area | Status |
| --- | --- |
| XBE inspection and loader | Complete for the current title |
| Analysis database | Complete and reproducible |
| Runtime ABI shims | Implemented for the currently reached path |
| IA-32 execution | Static native boot, frontend, and Lesson One path running |
| Vulkan presentation | Live completed-flip presentation with close visual parity |
| Audio and input | Native PCM/Xbox ADPCM mixing and SDL3 submission/input; rumble pending |
| Provenance | Exact supported-XBE gate and content-addressed build/run manifests |
| Saves | Local storage works; autosave remains incomplete |

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

Run the changed-file-aware development gate, then launch normal live execution:

```powershell
python .\tools\dev_check.py --explain
python .\tools\playability\live_test.py
```

Use `python .\tools\dev_check.py --launch-closeout` to run the focused handoff
gate and then start the exhaustive closeout matrix without blocking the edit
loop. `tools/validation_closeout.py status <run-directory>` reports progress.

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

Normal gameplay reports zero Python runtime callbacks, runtime compilation,
native promotion, and frontier-interpreter activity. Python remains responsible
for deterministic artifact preparation, launch, and post-run diagnostics. See
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the ownership model and
[docs/VALIDATION.md](docs/VALIDATION.md) for retained run evidence.

## Validation

The current asset-free suite discovers 727 Python cases and 13 native CTest
cases. The July 31 exhaustive closeout passed the asset-free gate, debug,
release, sanitizer, and strict-presenter stages in 130.16 seconds. Validation
uses changed-file selection, case-level sharding, content-addressed caches,
runtime budgets, and bounded first-failure capsules. Run the focused gate while
editing and reserve the exhaustive matrix for integration or handoff.

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

See [LICENSE](LICENSE) and [NOTICE.md](NOTICE.md) for licensing and third-party
notices.
