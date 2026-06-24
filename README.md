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
   - Current status: implemented and covered by synthetic tests.

4. **Static analysis baseline**
   - Import the executable into Ghidra, IDA, Binary Ninja, or equivalent tooling.
   - Establish calling conventions, function boundaries, vtables, jump tables,
     exception patterns, and compiler fingerprints.
   - Maintain project-owned symbol names and notes without embedding original
     copyrighted code.
   - Current status: initial metadata-seeded analysis database implemented;
     interactive disassembler import and code-flow recovery remain future work.

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

Generate the project-owned analysis database without dumping executable bytes:

```powershell
python .\tools\analysis\analysis_db.py `
  .\data\local\extracted\burnout_2_poi_usa\default.xbe `
  --pretty `
  --output .\reports\local\analysis-db.json
```

Generated local reports are marked `public_safe: false` because they contain
local filesystem metadata, file hashes, executable metadata, and analysis state.
Keep them ignored.

Smoke-test runtime shim registration against the local executable without
dumping executable bytes:

```powershell
python .\tools\runtime\runtime_smoke.py `
  .\data\local\extracted\burnout_2_poi_usa\default.xbe `
  --extracted-root .\data\local\extracted\burnout_2_poi_usa `
  --pretty
```

## Current Local Extraction Summary

> [!NOTE]
> **Extraction Update - June 24, 2026:** This summary reflects the local
> extraction generated by `tools/extract/extract_disc.py`. The full generated
> report remains ignored because it is local analysis output.

> [!IMPORTANT]
> **Milestone 1 Update - June 24, 2026:** Introspection is complete. The local
> report includes loader memory maps, TLS metadata, library versions, library
> feature descriptors, kernel imports, non-kernel imports, and section digest
> verification for each extracted XBE.

> [!IMPORTANT]
> **Milestone 2 Update - June 24, 2026:** The loader skeleton is complete. The
> loader maps XBE headers and sections into a bounded host-owned arena, keeps
> image gaps unmapped, preserves section zero-fill, patches import thunk tables
> through resolver hooks or synthetic stubs, and records structured load traces.

> [!IMPORTANT]
> **Milestone 3 Update - June 24, 2026:** The first analysis database is
> complete. It records project-owned names, confidence levels, subsystem tags,
> compiler/runtime patterns, and roadmap focus areas from sanitized XBE metadata
> and loader summaries.

> [!IMPORTANT]
> **Milestone 4 Update - June 24, 2026:** The first runtime shim layer is
> complete. It provides host-side filesystem, timing, synchronization,
> threading, memory, input, rendering, audio, diagnostics, hardware, loader, and
> crypto boundaries with structured traces and deterministic import registration.

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
- `default.xbe` runtime shim smoke summary:
  - Imported kernel ordinals: `142`
  - Registered host shims: `142`
  - Implemented handler models: `86`
  - Deterministic placeholder stubs: `56`
  - Unresolved loader imports with runtime resolver: `0`
- `default.xbe` analysis database summary:
  - Records: `198`
  - Record kinds: `7` focus areas, `18` memory regions, `17` sections,
    `142` import thunks, `10` libraries, `2` library references, `1` function,
    and `1` data record.
  - Confidence levels: `146` confirmed, `50` inferred, `1` probable, and `1`
    placeholder.
  - Compiler/runtime patterns: `8`
  - Focus areas: startup, allocator paths, filesystem paths, rendering setup,
    audio setup, input polling, and main loop recovery.
- No case-only path collisions were detected in the extracted layout.
- Full local input hashes, extracted executable hashes, root layout, extension
  counts, and XBE metadata are stored in `reports/local/disc-extraction.json`.
  The generated analysis database is stored in
  `reports/local/analysis-db.json`.

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

> **Status:** `Done`

> [!IMPORTANT]
> **Milestone 3 Update - June 24, 2026:** The metadata-seeded analysis database
> is implemented and verified with synthetic XBE tests. The generated local
> database remains ignored under `reports/local/`.

Completed:

- Added `tools/analysis/analysis_db.py` as the first project-owned analysis
  database generator and CLI.
- Defined a JSON schema for project-owned records, naming conventions,
  confidence levels, subsystem taxonomy, compiler/runtime patterns, and
  roadmap focus areas.
- Established naming conventions for functions, import thunks, globals, TLS
  records, sections, memory regions, vtables, subsystems, and future assets.
- Added confidence levels: `confirmed`, `probable`, `inferred`, and
  `placeholder`.
- Seed records from sanitized XBE metadata and loader summaries: mapped
  regions, sections, entry point, TLS directory/callbacks, library versions,
  library references, library features, kernel imports, and non-kernel imports.
- Added compiler/runtime pattern tracking for XBE entry resolution, TLS,
  XDK library fingerprints, kernel/non-kernel thunks, section zero-fill,
  digest mismatches, PE stack/heap reserves, and pending main-loop recovery.
- Added explicit focus records for startup code, allocator paths, filesystem
  paths, rendering setup, audio setup, input polling, and main loop recovery.
- Added synthetic-only unit tests proving the database is generated without
  proprietary fixtures.

Remaining:

- No remaining Milestone 3 tasks.

### Milestone 4: Runtime Shims

> **Status:** `Done`

> [!IMPORTANT]
> **Milestone 4 Update - June 24, 2026:** Runtime shims are implemented and
> covered by synthetic tests. The local smoke check registers every current
> `default.xbe` kernel import through the runtime resolver; unsupported or
> unknown ordinals use deterministic placeholder stubs until execution reaches
> those paths.

Completed:

- Added `runtime/xbox/shims.py` as the host runtime shim boundary.
- Added structured runtime traces for filesystem, allocator, threading,
  synchronization, timing, input, rendering, audio, diagnostics, hardware,
  loader, crypto, and object-handle behavior.
- Implemented read-only filesystem access rooted at the ignored extracted disc
  tree, including Xbox device path normalization, case-insensitive lookup, safe
  traversal rejection, file open/read/query, and directory listing.
- Implemented deterministic memory APIs for pool, system, contiguous, and GPU
  allocations with bounds-checked reads/writes, protection changes, query,
  free, zero/fill/move helpers, and physical-address placeholders.
- Implemented deterministic timing, thread, event, semaphore, timer, wait,
  critical-section, and system-thread models.
- Added a controller input abstraction with four pollable controller ports.
- Stubbed rendering initialization through `Av*` display/saved-data calls and
  GPU instance memory claiming.
- Stubbed audio initialization, audio stream handles, and buffer submission.
- Added diagnostics, hardware/version, loader section, and crypto helper
  boundaries for imported kernel APIs that appear during early boot.
- Added `XboxRuntimeShims.register_kernel_imports()` to bind imported kernel
  ordinals into the loader `ImportResolver` with deterministic host target
  addresses.
- Added deterministic placeholder registration for unsupported or unknown
  ordinals so the loader can patch all current kernel import thunks without
  unresolved imports.
- Added synthetic unit tests covering shim registration, filesystem access,
  memory, timing, synchronization, input, graphics, and audio behavior.

Remaining:

- No remaining Milestone 4 tasks. Placeholder shims should be replaced by
  behavior-accurate implementations as Milestone 5 and boot execution expose
  concrete call sites and ABI details.

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

1. Start Milestone 5 by selecting a small, well-understood function range near
   startup, a TLS callback, or a low-risk runtime helper.
2. Define the first execution adapter that can call runtime shim handlers from
   guest ABI state rather than direct Python test calls.
3. Generate or interpret a narrow x86 instruction subset with explicit flag,
   stack, memory, and call-target accounting.
4. Add comparison tests around synthetic traces before attempting real boot
   execution.
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

Project foundation, disc/executable extraction tooling, XBE introspection, the
loader skeleton, the analysis database generator, and the first runtime shim
layer are in place. The local Burnout 2 XISO has been extracted into ignored
storage, `default.xbe` has been identified as the main executable, and local JSON
reports now record the disc layout, executable hashes, loader memory maps, TLS
metadata, library metadata, imports, section digest verification, project-owned
analysis records, subsystem tags, confidence levels, and compiler/runtime
patterns.

The runtime shim layer can register all current `default.xbe` kernel imports
through the loader resolver with deterministic host target addresses. A
recompilation or execution pipeline has not been implemented yet.
