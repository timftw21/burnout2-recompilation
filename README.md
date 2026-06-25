# Burnout 2: Point of Impact Static Recompilation

This project is an exploratory static recompilation effort for the Xbox release
of *Burnout 2: Point of Impact*. The goal is to understand, document, and
eventually rehost the original Xbox executable behavior on modern platforms
through a native runtime while preserving the original mechanics, timing, and
asset formats.

The Xbox version is the base target because the original Xbox CPU is x86, which
makes it a more practical static recompilation candidate than the PowerPC, MIPS,
or Emotion Engine based console releases. Treat this as reverse engineering, not
as a source port. Original game assets, executable data, trademarks, and
copyrighted content remain owned by their rights holders.

## Ground Rules

- Do not commit or distribute game ISOs, extracted assets, executable sections,
  SDK files, symbols, generated source containing original code/data, or local
  analysis reports with proprietary-derived payloads.
- Development assumes each contributor provides their own legally obtained Xbox
  copy.
- Keep extraction, analysis, lifting, patching, and build steps scripted and
  repeatable.
- Prefer behavior-first accuracy over rewrites from memory.
- Isolate host platform services: graphics, audio, input, files, timing,
  threading, and memory.

## Current State

Updated: June 25, 2026.

The repository has reproducible tooling for disc extraction, XBE introspection,
loader mapping, metadata-seeded analysis, runtime shim registration, a narrow
IA-32 lifter/emitter, a Windows/Vulkan first-frame harness, and a repeatable
playability probe that executes recovered boot control flow against modeled
runtime ABI boundaries.

Milestone status:

| Milestone | Status | Notes |
| --- | --- | --- |
| 0. Project foundation | Partial | Repo hygiene and local-data conventions exist; legal/contributor docs still need splitting out. |
| 1. XBE introspection | Done | Header, section, TLS, library, import, digest, and memory-map parsing are implemented and tested. |
| 2. Loader skeleton | Done | XBE headers/sections map into a bounded arena with import resolver hooks and structured traces. |
| 3. Analysis database | Done | Project-owned analysis records are generated from sanitized XBE and loader metadata. |
| 4. Runtime shims | Done | Current `default.xbe` kernel imports register with modeled handlers/data exports and no placeholder stubs. |
| 5. Recompilation prototype | Done | A narrow IA-32 lifter, executor, and deterministic Windows C++17 emitter are implemented and tested. |
| 6. First interactive frame | Done | The Windows/Vulkan harness builds, opens a window, presents bounded frames, and records diagnostics. |
| 7. Playability push | Partial | Boot/control-flow execution reaches the guest-thread scheduler boundary with asset, runtime, and render evidence. |

## Local Inputs and Outputs

The local Redump-derived XISO and extracted files are developer-owned inputs and
must remain ignored:

- Local image: `Burnout 2/Burnout 2 - Point of Impact (USA).xiso.iso`
- Extracted disc tree: `data/local/extracted/burnout_2_poi_usa/`
- Local extraction report: `reports/local/disc-extraction.json`
- Local third-party tools: `data/local/tools/`
- Local probe/render/build reports: `reports/local/`, `build/local/`

Last local extraction summary:

- Extracted `667` files across `109` directories.
- Main executable: `default.xbe`
- Additional XBE files: `dashupdate.xbe`, `update.xbe`
- Title metadata: `Burnout 2`, title ID `41430019` (`AC-025`), region `NA`,
  media `DVD_X2`
- `default.xbe` imports: `142` kernel imports, `0` non-kernel imports
- Section digest verification: `16/17` match; `.text` is recorded as
  mismatched in the local report

## Common Commands

Install the pinned local `extract-xiso` tool:

```powershell
.\tools\extract\install_extract_xiso.ps1
```

Extract the local XISO and generate the ignored report:

```powershell
python .\tools\extract\extract_disc.py `
  --iso ".\Burnout 2\Burnout 2 - Point of Impact (USA).xiso.iso"
```

Regenerate the report from an existing extraction:

```powershell
python .\tools\extract\extract_disc.py `
  --iso ".\Burnout 2\Burnout 2 - Point of Impact (USA).xiso.iso" `
  --scan-only
```

Inspect an XBE:

```powershell
python .\tools\xbe\xbe_info.py `
  .\data\local\extracted\burnout_2_poi_usa\default.xbe `
  --pretty
```

Emit only the loader memory map:

```powershell
python .\tools\xbe\xbe_info.py `
  .\data\local\extracted\burnout_2_poi_usa\default.xbe `
  --memory-map `
  --pretty
```

Run the loader skeleton:

```powershell
python .\tools\loader\xbe_loader.py `
  .\data\local\extracted\burnout_2_poi_usa\default.xbe `
  --pretty
```

Generate the project-owned analysis database:

```powershell
python .\tools\analysis\analysis_db.py `
  .\data\local\extracted\burnout_2_poi_usa\default.xbe `
  --pretty `
  --output .\reports\local\analysis-db.json
```

Smoke-test runtime shim registration:

```powershell
python .\tools\runtime\runtime_smoke.py `
  .\data\local\extracted\burnout_2_poi_usa\default.xbe `
  --extracted-root .\data\local\extracted\burnout_2_poi_usa `
  --pretty
```

Generate a deterministic C++ recompilation prototype from synthetic bytes:

```powershell
python .\tools\recomp\recompile_range.py `
  --hex 33C0C3 `
  --virtual-address 0x12594 `
  --symbol zero_return `
  --pretty
```

Generate an ignored local C++ artifact from the developer-owned XBE:

```powershell
python .\tools\recomp\recompile_range.py `
  .\data\local\extracted\burnout_2_poi_usa\default.xbe `
  --virtual-address 0x12594 `
  --size 3 `
  --symbol b2_default_zero_return `
  --cpp-output .\reports\local\recomp\b2_default_zero_return.cpp `
  --json-output .\reports\local\recomp\b2_default_zero_return.json `
  --pretty
```

Build and run the Windows/Vulkan first-frame smoke:

```powershell
python .\tools\host\first_frame_smoke.py `
  --max-frames 3 `
  --pretty
```

Probe recovered boot/control flow against runtime ABI bindings:

```powershell
python .\tools\playability\playability_probe.py `
  .\data\local\extracted\burnout_2_poi_usa\default.xbe `
  --extracted-root .\data\local\extracted\burnout_2_poi_usa `
  --json-output .\reports\local\playability\probe-summary.json `
  --pretty
```

Optional writable/configurable roots can be supplied when comparing recovered
save, dashboard, or cache paths against host data:

```powershell
python .\tools\playability\playability_probe.py `
  .\data\local\extracted\burnout_2_poi_usa\default.xbe `
  --extracted-root .\data\local\extracted\burnout_2_poi_usa `
  --save-data-root .\data\local\save-data `
  --dashboard-root .\data\local\dashboard `
  --cache-root .\data\local\cache `
  --pretty
```

Export, decode, and replay the recovered D3D8/NV2A command stream:

```powershell
python .\tools\playability\playability_probe.py `
  .\data\local\extracted\burnout_2_poi_usa\default.xbe `
  --extracted-root .\data\local\extracted\burnout_2_poi_usa `
  --json-output .\reports\local\playability\probe-summary.json `
  --dynamic-block-cache .\reports\local\playability\dynamic-block-cache.json `
  --render-stream-output .\reports\local\render\recovered-d3d-stream.json `
  --pretty

python .\tools\render\d3d8_stream.py `
  --input .\reports\local\playability\probe-summary.json `
  --stream-output .\reports\local\render\recovered-d3d-stream.json `
  --decode-output .\reports\local\render\decoded-d3d-stream.json `
  --replay-output .\reports\local\render\replay-summary.json `
  --pretty

python .\tools\host\first_frame_smoke.py `
  --max-frames 3 `
  --render-stream-json .\reports\local\render\recovered-d3d-stream.json `
  --pretty
```

## Current Recovery Evidence

The current local playability probe:

- Executes the real entry prefix at `0x000E2555`.
- Decodes entry handoffs at `0x000E68C4`, `0x000E5D5D`, and `0x000E099C`.
- Recovers `681` static basic blocks with no remaining static frontier.
- Follows the created guest thread at `0x000E682C`.
- Dynamically decodes `438` executed blocks and reaches `219` runtime ABI calls
  from the guest thread.
- Stops cleanly at the recovered guest-thread scheduler work-queue scan at
  `0x000F2A10`.

Scheduler boundary evidence:

- Requested key: `0x0000040D`, loaded by `0x000F2A10` from stack offset `0x24`
  at stack address `0x70FFFECC`.
- Observed node base/key/next: `0x00000008`, `0x003445C0`, `0x00000008`.
- The observed node self-loops and does not contain the awaited key.
- Producer candidates preserve `8` bounded writes to watched scheduler slots.
- Current producer keys observed through `0x000F2A5B` -> `0x000F2A63` are
  `0x0000040F` and `0x00000401`, not `0x0000040D`.
- Producer/consumer key arguments both use stack offset `0x24`; the current
  missing-key hypothesis is that the producer path for `0x0000040D` has not
  executed before this wait.
- Recovered producer labels: `0x00127E9B` initial node key seed,
  `0x00127ED2` initial pointer-byte seed, `0x000F2A63` work-item key writer,
  `0x000F2AAB` next-pointer initializer, `0x000F2AF9` next-pointer linker, and
  `0x000F29F6` watched queue-slot replacement.

Asset and runtime evidence:

- Ordered asset I/O summary records `15` file/symbolic-link edges.
- The `d:\dashupdate.xbe` open/read/query/set-position/read/close sequence is
  complete and streamed `840` bytes into guest buffers.
- The `\??\D:` symbolic-link open/query/close sequence resolves to
  `\Device\Cdrom0`.
- The next edge after those completed sequences is the dashboard XODash fallback
  `\Device\Harddisk0\partition2\XODash\xonlinedash.xbe` at ABI invocation `38`,
  followed by the disc XODash fallback at invocation `40`; both are clean
  not-found probes.
- Dashboard/cache assessment records `2` dashboard probes and `1` cache probe,
  all clean not-found, with `requires_configured_roots_for_observed_edges:
  false`.
- Deterministic service validation currently covers `2` file-read events,
  `840` bytes read, and `1,500` deterministic 100 ns latency ticks. Save-data,
  audio, and input-latency sections remain zero until later gameplay paths
  exercise them.

Render evidence:

- Guest-thread trace captures `143` D3D command writes: `24` MMIO writes and
  `119` push-buffer writes.
- Offline D3D stream decode currently reassembles `33` push-buffer dwords,
  preserves `3` zero-count command-shaped words, and finds `0` valid recovered
  method packets in the boot artifact.
- The dominant recovered surface payload dword is `0x808080FF`, selected from
  `19` of `33` complete dword samples.
- Offline replay and the Vulkan smoke emit one Vulkan-facing render-pass clear
  work item from the recovered surface payload.
- Synthetic regression coverage exercises method-specific D3D ARGB
  `clear_color`, `clear_surface` mask selection, and `begin_end` primitive draw
  translation.

## Implementation Areas

Runtime shims:

- Model filesystem, object handles, allocator, timing, synchronization,
  threading, input, rendering, audio, diagnostics, hardware, loader, and crypto
  behavior reached by current boot paths.
- Route observed imported calls through `RuntimeAbiBridge` with guest stack
  argument marshaling, return-value writes, out-parameter writes, and stack
  cleanup.
- Preserve deterministic runtime summaries for streaming, save data, audio,
  input, clock, handles, and open files.

Recompilation and execution:

- Decode/lift the observed IA-32 subset needed by current boot execution.
- Execute lifted blocks against explicit CPU state and a sparse image-backed
  guest memory overlay.
- Recover direct handoffs, branch/fallthrough frontiers, import tail jumps, and
  dynamically reached executable blocks.
- Keep new instruction coverage trace-driven rather than broad and speculative.

Rendering:

- Export recovered D3D command streams as ignored local JSON artifacts.
- Decode D3D8/NV2A MMIO/setup writes, submission kicks, push-buffer packet
  shapes, method packets, preserved unknowns, and recovered surface payloads.
- Feed decoded state into Vulkan-facing work records in the host smoke harness.
- Keep trace-driven HLE constrained to stable D3D/runtime boundaries; CPU static
  recompilation remains authoritative for game logic.

## Repository Layout

```text
b2_recomp/
  README.md
  docs/
  tools/
    analysis/
    extract/
    host/
    loader/
    playability/
    recomp/
    render/
    runtime/
    xbe/
  runtime/
    host/
    xbox/
  tests/
    unit/
  data/
    local/
```

`data/local/`, `reports/local/`, `build/local/`, extracted game content, local
generated C++, dynamic block caches, and recovered stream reports are ignored.

## Immediate Next Steps

1. Continue from the recovered `guest_thread_scheduler` work-queue scan at
   `0x000F2A10` by tracing the producer caller/key path for missing requested
   key `0x0000040D`, starting from producer stack slot `0x24`, source load
   `0x000F2A5B`, key writer `0x000F2A63`, and grouped producer anchor
   `0x000F29F6`.
2. Continue from the XODash dashboard/disc fallback probes and determine whether
   later boot/gameplay paths require configured local dashboard/cache roots or
   whether deterministic not-found remains correct title behavior.
3. Validate streaming, audio, save-data, input-latency, and deterministic
   summary models against the next recovered gameplay paths.
4. Feed newly decoded Direct3D 8/NV2A method packets and recovered surface
   payload samples into the Vulkan state/work translator.
5. Broaden regression tests only as new real boot and gameplay paths are
   recovered.

## Open Questions

- What determinism level is required for long-run gameplay testing and replay?
- How much of the Xbox API surface should be emulated generally versus tailored
  specifically to this title?
- Which recovered render packets should graduate from preserved artifacts to
  method-specific Vulkan work once gameplay command streams stabilize?
