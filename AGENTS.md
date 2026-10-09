# Burnout 2 fresh start

Build a small, fast Windows x64 static recompilation of the original Xbox game.
Read DESIGN.txt and BUILDING.txt for design, commands and coverage.

- Performance comes first. Target sustained 60 FPS at native resolution; measure
  frame times, spikes, loading and memory on named hardware in menu/race/crash
  scenes. Component benchmarks do not establish game FPS.
- Use C++23/CMake, with C/HLSL where useful. Require no Python in any workflow.
- Compile game code ahead of time. Decode only in build tools; use native
  functions, direct calls and verified indirect tables. No JIT/interpreter fallback
  or invented callbacks. Report platform bindings separately from CPU coverage.
- Use SDL3 for windows, events, input and audio, native D3D11 for graphics and
  Dear ImGui for persistent, accessible settings. Keep one game executable and
  one native tool, sharing their cores.
- Aim for visual parity using original Xbox video screenshots. Record source,
  timestamp and viewpoint; account for capture artifacts. Original hardware and
  xemu gameplay references are not required.
- Provide autonomous CPU, service, asset, render and audio diagnostics without
  booting. Produce structured failures, images and timings. Keep gameplay tracing
  and captures opt-in.

Preserve knowledge/, private media, provenance and Git history. Verify the target
against knowledge/supported_target.json and verify discoveries from original
bytes. Report unsupported behavior explicitly. Ignore private/generated artifacts.

Batch connected work and builds. Fix root causes from evidence. Keep code and
dependencies minimal; avoid allocation, blocking I/O, compilation and logging in
critical gameplay paths. Add tests only when necessary, and no unsolicited Markdown.

Use bounded native analysis and streaming reads. Never run whole-file
Python/Capstone scans or parallel memory-intensive commands. After heavy/timed-out
commands, check owned workers and stop only exact surviving processes they created.

Explain concisely. Subagents and computer use require explicit user authorization.
