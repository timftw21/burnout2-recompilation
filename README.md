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

## Technical Milestones

### Milestone 0: Project Foundation

- Add `.gitignore` for proprietary and generated files.
- Create `docs/legal.md` describing what may and may not be committed.
- Create scripts for reproducible local extraction.
- Add hash verification for the expected Xbox XISO and extracted XBE.

### Milestone 1: XBE Introspection

- Parse and validate XBE headers.
- Emit a machine-readable memory map.
- Dump imports, sections, entry point, TLS, and certificate data.
- Add tests using synthetic XBE-like fixtures instead of copyrighted binaries.

### Milestone 2: Loader Skeleton

- Map executable sections into a controlled host memory arena.
- Model Xbox virtual memory assumptions.
- Provide import resolution hooks for kernel and library calls.
- Add trace logging for initialization order.

### Milestone 3: Analysis Database

- Establish naming conventions for functions, data, vtables, and subsystems.
- Track confidence levels for discovered symbols.
- Document compiler/runtime patterns.
- Identify startup code, allocator paths, filesystem paths, rendering setup,
  audio setup, input polling, and main loop structure.

### Milestone 4: Runtime Shims

- Implement filesystem access against extracted local files.
- Implement timing, threading, synchronization, and memory APIs.
- Add controller input abstraction.
- Stub graphics/audio enough to boot through initialization.

### Milestone 5: Recompilation Prototype

- Lift a small, well-understood function range.
- Generate native code or C/C++ with a deterministic build step.
- Verify CPU flags, stack behavior, memory reads/writes, and call targets.
- Add automated comparison tests against captured traces where possible.

### Milestone 6: First Interactive Frame

- Reach main loop execution.
- Render a visible first frame or menu through the host graphics layer.
- Accept input.
- Establish a repeatable debugging workflow for crashes, mismatches, and missing
  platform behavior.

### Milestone 7: Playability Push

- Replace broad stubs with accurate subsystem implementations.
- Improve renderer translation, asset streaming, audio playback, save data,
  input latency, frame pacing, and determinism.
- Build regression tests around known boot and gameplay paths.

## Immediate Next Steps

1. Initialize Git and add ignore rules before extracting or generating anything.
2. Move local proprietary inputs under an ignored `data/local/` convention.
3. Create a small XBE metadata parser as the first real tool.
4. Generate a sanitized metadata report from the local executable.
5. Use that report to design the loader memory model and import shim boundary.

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

Project documentation has started. No executable parsing, extraction pipeline,
runtime shim, or recompilation tooling has been implemented yet.
