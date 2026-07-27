# b2_recomp Agent Requirements

Read `README.md` before changing code and follow the repository's existing
formatting, file structure, validation commands, and asset policy.

## Static recompilation boundary

- Treat b2_recomp as a static-recompilation project. Guest code must be
  analyzed, lifted, and compiled into deterministic native artifacts before a
  normal run.
- Never enable `--developer-live-compile` for normal execution or validation.
  Runtime compilation is developer-only diagnostic machinery and must not
  become a gameplay dependency.
- Treat an unknown executable target as a coverage gap. The discovery
  interpreter may record it during an explicit diagnostic run, but the block
  must be added to the decoded block store and consumed by the next
  ahead-of-time build.
- Do not add or expand permanent interpreter, JIT, runtime code-generation, or
  native-promotion fallbacks to make gameplay work.
- A warm validation run is static-clean only when it reports live compilation
  disabled, native promotion disabled, zero frontier-interpreter invocations,
  and zero frontier-interpreter steps.
- Prefer the terms `decoded block store`, `decoded_block_store`, and
  `--decoded-block-store`. The old dynamic-block-cache spelling is reserved
  only for hidden CLI compatibility and migration of legacy local data.
- Preserve explicit host ABI boundaries for graphics, audio, input, files,
  timing, threading, and memory. Host services do not justify runtime guest
  code generation.
- Any exception to these requirements needs explicit user approval and must be
  documented with a narrow diagnostic purpose and a removal path.

## Native gameplay boundary

- Python may prepare deterministic artifacts, validate provenance, launch the
  native runtime, and compose diagnostics after a run. Normal guest execution
  must not call back into Python.
- The guest scheduler, ABI dispatch and service bodies, dirty-memory ownership,
  render publication, and audio decode/mix/submission path must execute in
  native code during normal gameplay.
- A normal-run validation is native-clean only when it reports zero Python
  runtime callbacks, including memory, service, scheduler/yield, render, and
  audio callbacks. Developer-only audit tools may retain Python callbacks when
  the report identifies the diagnostic mode and callback counts explicitly.
- Do not optimize around a Python runtime boundary. Remove the boundary and
  establish a native-clean baseline before performance tuning.
