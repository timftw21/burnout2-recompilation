# Burnout 2: Point of Impact Static Recompilation

This project is an exploratory static recompilation effort for the Xbox release of
*Burnout 2: Point of Impact*. The goal is to understand, document, and eventually
rehost the original Xbox executable behavior on modern platforms through a
native runtime, while preserving the game's original mechanics, timing, and asset
formats.

The Xbox version is the base target because the original Xbox CPU is x86, which
makes it a more practical static recompilation candidate than the PowerPC,
MIPS, or Emotion Engine based console releases. The project should still be
treated as a reverse-engineering effort, not as a source port. All original game
assets, executable data, trademarks, and copyrighted content remain owned by
their respective rights holders.

## Project Principles

- **No proprietary distribution:** Do not commit, redistribute, or publish game
  ISOs, extracted assets, executable sections, SDK files, symbols, or generated
  source containing copyrighted original code/data.
- **User-owned media only:** Development should assume each developer provides
  their own legally obtained Xbox copy.
- **Behavior-first accuracy:** Prioritize matching the original executable's
  behavior over rewriting systems from memory or preference.
- **Reproducible tooling:** Every extraction, analysis, lifting, patching, and
  build step should be scripted and repeatable.
- **Document decisions early:** Reverse-engineering assumptions, calling
  conventions, memory maps, ABI details, and runtime shims should be recorded as
  the project learns them.
- **Modern host boundaries:** The final host runtime should isolate platform
  services such as graphics, audio, input, files, timing, threading, and memory.

## Current Starting Point

The workspace currently contains a local Xbox XISO and metadata generated from a
Redump source image:

- Game: `Burnout 2 - Point of Impact (USA)`
- Local image: `Burnout 2/Burnout 2 - Point of Impact (USA).xiso.iso`
- Source metadata: `Burnout 2/iso_details.txt`

These files are local development inputs only. They should not become part of
the public project history.

The extraction workflow now writes proprietary generated output only to ignored
local paths:

- Extracted disc tree: `data/local/extracted/burnout_2_poi_usa/`
- Local extraction report: `reports/local/disc-extraction.json`
- Local third-party tools: `data/local/tools/`

## Recommended Direction

The practical path is to build the project in layers:

1. **Repository hygiene**
   - Initialize version control.
   - Add ignore rules for ISOs, extracted game files, generated dumps, symbols,
     logs, and local analysis databases.
   - Create a documented local data directory convention.

2. **Disc and executable extraction**
   - Script XISO extraction into a local ignored directory.
   - Identify the main `.xbe`, media layout, filesystem case sensitivity, and
     title metadata.
   - Record hashes for every local input and extracted executable.
   - Current status: implemented and locally executed.

3. **XBE loader research**
   - Parse XBE headers, section tables, imports, entry point, TLS, certificate
     metadata, and memory layout.
   - Build a small loader model that can map executable sections exactly as the
     Xbox runtime expects.

4. **Static analysis baseline**
   - Import the executable into Ghidra, IDA, Binary Ninja, or equivalent tooling.
   - Establish calling conventions, function boundaries, vtables, jump tables,
     exception patterns, and compiler fingerprints.
   - Maintain project-owned symbol names and notes without embedding original
     copyrighted code.

5. **Runtime shim design**
   - Implement host-side replacements for Xbox kernel calls.
   - Stub and then emulate Direct3D 8, DirectSound, XInput, filesystem,
     threading, timing, memory allocation, and synchronization behavior.
   - Keep shims observable with structured logs and deterministic replay hooks.

6. **Recompiler pipeline**
   - Start with a small executable-region lifter for known code ranges.
   - Translate x86 instructions into a safe intermediate representation or
     generated C/C++.
   - Preserve flags, calling conventions, stack behavior, memory ordering, and
     self-referential code assumptions.
   - Build per-function tests from captured original behavior where practical.

7. **Vertical slice**
   - Boot through process initialization.
   - Reach early file access and renderer initialization.
   - Display the first stable frame or menu using host shims.
   - Iterate from observable failures instead of broad speculative rewrites.

## Proposed Repository Layout

```text
b2_recomp/
  README.md
  docs/
    architecture.md
    legal.md
    reverse-engineering-notes.md
    roadmap.md
  tools/
    extract/
    xbe/
    loader/
    analysis/
    recomp/
  runtime/
    host/
    xbox/
    graphics/
    audio/
    input/
    fs/
  tests/
    unit/
    integration/
    fixtures/
  data/
    local/
      .gitkeep
```

`data/local/` should be ignored by version control and used for developer-owned
game inputs, extracted files, generated databases, and temporary artifacts.

## Disc Extraction Workflow

The project uses
[`XboxDev/extract-xiso`](https://github.com/XboxDev/extract-xiso) as an external
local tool. The helper installer pins the current tool release used by this
project (`build-202505152050`), stores it under ignored `data/local/tools/`,
and records a local install manifest with hashes.

Install the extractor locally:

```powershell
.\tools\extract\install_extract_xiso.ps1
```

Extract the current local XISO and generate the ignored report:

```powershell
python .\tools\extract\extract_disc.py `
  --iso ".\Burnout 2\Burnout 2 - Point of Impact (USA).xiso.iso"
```

Regenerate the report from an existing extraction without extracting again:

```powershell
python .\tools\extract\extract_disc.py `
  --iso ".\Burnout 2\Burnout 2 - Point of Impact (USA).xiso.iso" `
  --scan-only
```

Inspect a single XBE directly:

```powershell
python .\tools\xbe\xbe_info.py `
  .\data\local\extracted\burnout_2_poi_usa\default.xbe `
  --pretty
```

Emit only the loader-oriented memory map:

```powershell
python .\tools\xbe\xbe_info.py `
  .\data\local\extracted\burnout_2_poi_usa\default.xbe `
  --memory-map `
  --pretty
```

Map the executable into the loader skeleton and emit a non-byte summary:

```powershell
python .\tools\loader\xbe_loader.py `
  .\data\local\extracted\burnout_2_poi_usa\default.xbe `
  --pretty
```

The report is marked `public_safe: false` because it contains local filesystem
metadata, file hashes, and extracted executable metadata. Keep it ignored.

## Current Local Extraction Summary

> **Update (June 24, 2026):** This summary reflects the local extraction
> generated by `tools/extract/extract_disc.py`. The full generated report remains
> ignored because it is local analysis output.

> **Update (June 24, 2026):** Milestone 1 introspection is now complete. The
> local report includes loader memory maps, TLS metadata, library versions,
> library feature descriptors, kernel imports, non-kernel imports, and section
> digest verification for each extracted XBE.

> **Update (June 24, 2026):** Milestone 2 loader skeleton is now complete. The
> loader maps XBE headers and sections into a bounded host-owned arena, keeps
> image gaps unmapped, preserves section zero-fill, patches import thunk tables
> through resolver hooks or synthetic stubs, and records structured load traces.

Last local extraction: June 24, 2026.

- Extracted `667` files across `109` directories.
- Found three XBE files: `default.xbe`, `dashupdate.xbe`, and `update.xbe`.
- Selected main executable: `default.xbe`.
- `default.xbe` title metadata:
  - Title name: `Burnout 2`
  - Title ID: `41430019` (`AC-025`)
  - Region: `NA`
  - Allowed media: `DVD_X2`
  - XBE timestamp: `2003-04-16T10:56:12Z`
  - Certificate timestamp: `2003-04-18T19:32:44Z`
  - Section count: `17`
- `default.xbe` introspection summary:
  - Loader memory map zero-fill total: `2,571,497` bytes
  - TLS table: present
  - Library versions: `10`
  - Library feature descriptors: `0`
  - Kernel imports: `142`
  - Non-kernel imports: `0`
  - Section digest verification: `16/17` sections match; `.text` is recorded
    as mismatched in the local report.
- `default.xbe` loader summary:
  - Mapped regions: `18`
  - Entry point: `0x000E2555`
  - Patched imports: `142`
  - Registered host shims: `0`
  - Synthetic unresolved stubs: `142`
  - First load phases: `parse`, `digest`, `arena`, `map_headers`, `map_section`
- No case-only path collisions were detected in the extracted layout.
- Full local input hashes, extracted executable hashes, root layout, extension
  counts, and XBE metadata are stored in `reports/local/disc-extraction.json`.

## Technical Milestones

| Status | Meaning |
| --- | --- |
| `Done` | Implemented, verified, and safe to repeat from documented commands. |
| `Partial` | Useful project capability exists, but the milestone still has open scope. |
| `Pending` | Not started beyond planning and documentation. |

### Milestone 0: Project Foundation

> **Status:** `Partial`

Completed:

- Added Git history, remote tracking, `.gitignore`, and `.gitattributes`.
- Added an ignored `data/local/` convention for proprietary inputs and generated
  local artifacts.
- Added reproducible extraction and reporting commands.
- Added local hash reporting for the source XISO, `extract-xiso`, and extracted
  XBE files.

Remaining:

- Create `docs/legal.md` with explicit commit/publication rules.
- Add a short contributor workflow once the repository structure stabilizes.

### Milestone 1: XBE Introspection

> **Status:** `Done`

Completed:

- Added `tools/xbe/xbe_info.py` for loader-oriented XBE metadata parsing.
- Parse XBE magic, header fields, extended header fields, encoded entry point,
  encoded kernel thunk address, certificate metadata, debug strings, section
  headers, and section flags.
- Emit a machine-readable loader memory map with raw file ranges, virtual
  ranges, file-backed sizes, zero-fill sizes, raw overflow sizes, image gaps,
  and total zero-fill accounting.
- Parse TLS tables and TLS callback address tables.
- Parse library versions, kernel library version metadata, XAPI library version
  metadata, and library feature descriptors.
- Parse kernel import thunk tables as ordinals with best-effort known names.
- Parse non-kernel import descriptors and thunk arrays.
- Verify per-section SHA-1 digests using the XBE section digest format and
  report expected/actual digest state for every section.
- Added direct JSON inspection and memory-map-only CLI output.
- Expanded synthetic XBE unit tests for imports, TLS tables, library metadata,
  memory mapping, zero-fill handling, malformed headers, and digest mismatches.

Remaining:

- No remaining Milestone 1 tasks.

### Milestone 2: Loader Skeleton

> **Status:** `Done`

Completed:

- Added `tools/loader/xbe_loader.py` as the first executable loader skeleton.
- Added a controlled `XbeMemoryArena` with a 32-bit Xbox virtual image range,
  mapped-region tracking, bounds-checked reads/writes, and region permissions.
- Map XBE headers and every section into host-owned memory from parsed XBE
  metadata.
- Preserve Xbox section virtual memory behavior by copying file-backed bytes,
  zero-filling `virtual_size - raw_size`, and leaving image gaps unmapped.
- Model section permissions from XBE flags: readable by default, writable when
  `WRITABLE` is set, executable when `EXECUTABLE` is set.
- Provide `ImportResolver` hooks for kernel imports by ordinal/name and
  non-kernel library imports by image name and ordinal.
- Patch import thunk tables during load with registered hook addresses or
  deterministic synthetic unresolved stubs.
- Support fail-fast unresolved import policy and optional strict section digest
  validation.
- Add structured trace logging for parse, digest validation, arena creation,
  header mapping, section mapping, import resolution, and load completion.
- Add a loader CLI that emits a safe summary without dumping executable bytes.
- Added synthetic-only loader tests for mapping, zero-fill, permissions, bounds,
  import patching, unresolved import policy, digest policy, and trace order.

Remaining:

- No remaining Milestone 2 tasks.

### Milestone 3: Analysis Database

> **Status:** `Pending`

- Establish naming conventions for functions, data, vtables, and subsystems.
- Track confidence levels for discovered symbols.
- Document compiler/runtime patterns.
- Identify startup code, allocator paths, filesystem paths, rendering setup,
  audio setup, input polling, and main loop structure.

### Milestone 4: Runtime Shims

> **Status:** `Pending`

- Implement filesystem access against extracted local files.
- Implement timing, threading, synchronization, and memory APIs.
- Add controller input abstraction.
- Stub graphics/audio enough to boot through initialization.

### Milestone 5: Recompilation Prototype

> **Status:** `Pending`

- Lift a small, well-understood function range.
- Generate native code or C/C++ with a deterministic build step.
- Verify CPU flags, stack behavior, memory reads/writes, and call targets.
- Add automated comparison tests against captured traces where possible.

### Milestone 6: First Interactive Frame

> **Status:** `Pending`

- Reach main loop execution.
- Render a visible first frame or menu through the host graphics layer.
- Accept input.
- Establish a repeatable debugging workflow for crashes, mismatches, and missing
  platform behavior.

### Milestone 7: Playability Push

> **Status:** `Pending`

- Replace broad stubs with accurate subsystem implementations.
- Improve renderer translation, asset streaming, audio playback, save data,
  input latency, frame pacing, and determinism.
- Build regression tests around known boot and gameplay paths.

## Immediate Next Steps

1. Start Milestone 3 by creating the first analysis database format for
   project-owned names, notes, confidence levels, and subsystem tags.
2. Seed the analysis database from sanitized XBE metadata: sections, libraries,
   imports, entry point, TLS, and loader memory regions.
3. Define naming conventions for functions, globals, vtables, thunks, assets,
   and subsystem boundaries.
4. Add tests that prove analysis records are generated from synthetic XBE
   metadata without proprietary fixtures.
5. Keep all extracted game content and local reports ignored.

## Open Questions

- Which host platforms are first-class targets: Windows only, or Windows/Linux?
- Should generated code target C++, LLVM IR, or a custom interpreter-assisted
  hybrid while the recompilation strategy matures?
- Which renderer backend should be used first: Direct3D 11/12, Vulkan, OpenGL,
  or a higher-level abstraction?
- What level of determinism is required for testing and replay?
- How much of the Xbox API surface should be emulated generally versus tailored
  specifically to this title?

## Status

Project foundation, disc/executable extraction tooling, XBE introspection, and
the loader skeleton are in place. The local Burnout 2 XISO has been extracted
into ignored storage, `default.xbe` has been identified as the main executable,
and a local JSON report now records the disc layout, executable hashes, loader
memory maps, TLS metadata, library metadata, imports, and section digest
verification.

No runtime shim or recompilation pipeline has been implemented yet.
