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

Updated: July 14, 2026.

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
| 6. First interactive frame | Done | The Windows/Vulkan harness builds, opens a window, presents bounded frames, records diagnostics, and can read a presented swapchain image back to a validated BMP. |
| 7. Playability push | Partial | The resumable native guest loop reaches completed NV2A flips, feeds them to the live Vulkan presenter, and samples keyboard-backed Xbox controller state. A held Enter/Start+A edge now advances beyond the title frame into sustained frontend execution; reaching and validating gameplay remains outstanding. |

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

Audit lifter coverage along control flow reachable from the recovered dynamic
block cache (using Capstone as an independent instruction-length oracle):

```powershell
python .\tools\recomp\audit_x86_coverage.py `
  .\data\local\extracted\burnout_2_poi_usa\default.xbe `
  --dynamic-block-cache .\reports\local\playability\dynamic-block-cache.json `
  --json-output .\reports\local\recomp\x86-coverage-audit.json `
  --pretty
```

Build and run the Windows/Vulkan first-frame smoke:

```powershell
python .\tools\host\first_frame_smoke.py `
  --max-frames 3 `
  --pretty
```

For a manual unbounded replay of the latest recovered native frame, use zero
for both the frame limit and wrapper timeout. Close the window or press Escape
to exit cleanly:

```powershell
python .\tools\host\first_frame_smoke.py `
  --max-frames 0 `
  --timeout-seconds 0 `
  --render-stream-json .\reports\local\render\recovered-d3d-stream-texture-alias-5m.json `
  --pretty
```

This keeps the Vulkan window alive for visual/manual host testing, but currently
replays a recovered command stream each frame; it is not yet a live guest/game
loop, so controller input does not advance beyond the captured state.

An experimental resumable native guest runner is available for execution
profiling. It compiles the recovered dynamic-block cache into parallel-built,
3,000-instruction DLL modules and preserves CPU/memory state across module and
Xbox-service boundaries:

```powershell
python .\tools\playability\playability_probe.py `
  .\data\local\extracted\burnout_2_poi_usa\default.xbe `
  --extracted-root .\data\local\extracted\burnout_2_poi_usa `
  --dynamic-block-cache .\reports\local\playability\dynamic-block-cache.json `
  --native-guest-loop `
  --max-steps 1000000 `
  --json-output .\reports\local\playability\native-loop-1m.json
```

The normal live testing workflow now launches the native guest loop and Vulkan
presenter together. It uses the standard local XBE, extraction, dynamic-block
cache, live-state, report, persistent save-data, dashboard, and cache paths,
and defaults to unlimited guest execution. The writable volumes live under
`data/local/` and remain ignored.
Close the window or press Escape to stop both processes cleanly:

```powershell
python .\tools\playability\live_test.py
```

While the presenter window is focused, press F12 to capture the current frame.
Each press writes a uniquely named 32-bit BMP under
`reports/local/screenshots/` (for example,
`b2-recomp-20260711-153012-042-frame-120-1.bmp`).

Every normal live run also writes a cross-layer rendering diagnostic ledger to
`reports/local/playability/render-debug-events.jsonl` and, after both processes
stop, produces `reports/local/playability/render-debug-report.json`. The report
correlates malformed guest vertex samples with the host reload/flip that
presented them, ranks the responsible guest return addresses, retains bounded
frame-chain/stack/frontend-object snapshots, and groups repeated host geometry
signatures. It also aggregates presenter reload stages and frame pacing with
p50/p95/p99/max timing, ranks hot paths by total time, retains native-dispatch,
callback, page-cache, dirty-page, invalidation, and live-bridge timing counters,
and compares final guest/host full-screen, draw, texture, alpha-mask, and
readback coverage. Ranked addresses are decoded against the local XBE into the
preceding call/jump plus following instructions, so the report points directly
at producer call sites. It can be regenerated without rerunning the game:

```powershell
python .\tools\playability\render_debug_report.py `
  --probe-summary .\reports\local\playability\native-live.json `
  --presenter-events .\reports\local\playability\render-debug-events.jsonl `
  --json-output .\reports\local\playability\render-debug-report.json `
  --pretty
```

Pass `--skip-host-build` after the presenter has already been built. A finite
`--max-steps` can still be supplied for profiling; `--max-steps 0` means no
step limit in both `live_test.py` and `playability_probe.py`.
Native summaries distinguish Python-visible dispatches from native module calls
and report the recent 120-publication guest flip rate, making 60 Hz headroom
directly visible without inferring it from process startup time. Decoded menu
music is content-addressed under the ignored native build cache; cached PCM is
validated by its full payload hash before use, and volume conversion/playback
preparation runs on the audio worker rather than blocking the guest thread.

Before opening the live window for a manual check, run the automated pre-manual
gate. It inventories texture formats directly from the extracted Criterion
dictionaries, checks every captured texture/primitive/draw-source requirement
against the host capability matrix, verifies draw-to-resource address and
format/dimension consistency, replays the checked milestone suite with strict
Vulkan validation, rejects blank/near-solid frames, requires the boot frames to
change at the pixel level, and checks guest progress/frontiers/menu-audio health:

```powershell
python .\tools\playability\preflight_validation.py `
  --extracted-root .\data\local\extracted\burnout_2_poi_usa `
  --suite .\tools\playability\preflight_suite.json `
  --run-replays `
  --probe-summary .\reports\local\playability\native-live.json `
  --json-output .\reports\local\preflight\full-report.json `
  --pretty
```

The command exits nonzero on a missing implementation or regression, prints a
short summary, and leaves the complete evidence in the requested JSON report.
`implementation_requirements` records every observed texture decoder,
primitive, and draw source with its implemented status, so formats such as
DXT5 are exposed from assets/captures before visual inspection. Unnamed NV2A
methods, unknown push-buffer packets, and truncated capture commands remain
explicit coverage warnings for the next capture expansion.

The checked suite currently gates the publisher splash and title frame. To test
a newly named capture without editing that baseline, use, for example,
`--stage-override title=reports/local/live/render-new.json`. Add a stage or
tighten its thresholds in `tools/playability/preflight_suite.json` whenever a
new stable frontend/gameplay milestone is reached. Use `--skip-host-build` when
the Vulkan host and shaders are already current; `--full-stdout` is available
when the complete machine report is useful at the console.

For boot-sequence diagnosis, run the live loop in lossless producer-ledger,
adaptive frame-health audit mode:

```powershell
python .\tools\playability\live_test.py `
  --lossless-flip-audit `
  --skip-host-build
```

The native executor still observes every NV2A flip and records every producer
index, command boundary, flip value, and guest step in `flips.jsonl`. It does
not synchronize every ordinary frame with Vulkan. A flip becomes a health-check
candidate when it is the first or final bounded flip, changes command shape or
flip value, touches a cached texture page, or reaches the periodic sampling
interval. Only candidates publish their exact command/resource prefix and block
until Vulkan has strictly validated, rendered, presented, read back,
fingerprinted, and acknowledged them. The default periodic interval is 30
flips; `--flip-audit-health-interval 1` restores exhaustive per-frame checking,
while `0` uses detected candidates only.

Close the presenter at the title screen to audit the entire opening sequence.
Each run uses a new timestamped directory under `reports/local/flip-audit/`
containing `flips.jsonl`, `presenter-events.jsonl`, anomaly BMPs under `frames/`,
versioned resource snapshots, and the aggregate `report.json`. The Vulkan host
reads back each selected candidate and normally retains only its pixel
fingerprint and health metrics. A 32-bit BMP is written only when those metrics
flag a near-solid, whiteout, or low-information frame. The report proves that
the full producer sequence is contiguous, that host acknowledgements exactly
match the selected producer subset, that selected flips have readback metrics,
and that every candidate's strict texture/resource/primitive/draw-source
validation passed.

Candidate frames remain synchronized, but ordinary flips continue immediately;
image storage and cross-process waits therefore scale with selected/flagged
frames instead of total flips. For bounded automation or a quick handshake, add
`--flip-audit-max-flips N`; zero (the default) remains unlimited.
`--flip-audit-output-dir PATH` selects an explicit run directory. F12 manual
screenshots are unchanged and continue to write under
`reports/local/screenshots/`.
Controller-state publication retries transient Windows sharing violations for up
to ten seconds. Audit acknowledgement uses a share-friendly binary file handle,
so the guest's simultaneous read does not conflict with presenter publication.
The aggregate report also treats any nonzero guest or presenter exit as a failed
audit rather than reporting the last acknowledged prefix as a complete success.

The candidate audit handshake is optimized for the live loop. Vulkan returns
a fixed 20-byte binary acknowledgement token (`ack.bin`) containing only the
command-stream generation and acknowledged flip index; readback metrics remain
in the buffered presenter event ledger instead of being serialized into the
synchronization file. The guest batches durable `flips.jsonl` writes and flushes
the final batch when its summary is produced, rather than reopening and rereading
JSON files for every frame. Resource discovery uses the watchpoint's retained
texture-binding metadata directly, avoiding a per-flip clone of the bounded
65,536-write diagnostic history. Texture payload snapshots are cached by their
guest-memory page generations, so unchanged surfaces require only generation
checks; payload reads, SHA-256 calculation, hexadecimal encoding, and versioned
resource JSON publication occur only after a covered page changes. Stable flips
are appended to the producer ledger without presenter publication or waiting;
candidate flips retain the strict readback/acknowledgement contract.

For debugging the process boundary manually, the two atomic live-state files
can still connect the tools directly. Start the native probe in one terminal:

```powershell
python .\tools\playability\playability_probe.py `
  .\data\local\extracted\burnout_2_poi_usa\default.xbe `
  --extracted-root .\data\local\extracted\burnout_2_poi_usa `
  --save-data-root .\data\local\save-data `
  --dashboard-root .\data\local\dashboard-data `
  --cache-root .\data\local\cache-data `
  --dynamic-block-cache .\reports\local\playability\dynamic-block-cache.json `
  --native-guest-loop `
  --native-slice-steps 2500 `
  --max-steps 0 `
  --live-render-stream .\reports\local\live\render.json `
  --live-controller-state .\reports\local\live\controller.json `
  --json-output .\reports\local\playability\native-live.json
```

Once the first render snapshot exists, start the Vulkan presenter in a second
terminal:

```powershell
python .\tools\host\first_frame_smoke.py `
  --max-frames 0 `
  --timeout-seconds 0 `
  --render-stream-json .\reports\local\live\render.json `
  --live-render-stream `
  --controller-state-json .\reports\local\live\controller.json `
  --pretty
```

The presenter hot-reloads the newest rolling guest render window and maps the
arrow keys plus `S`, Backspace, Space, Enter, `B`, `X`, and `Y` to Xbox D-pad,
Start, Back, A, Start+A, and B/X/Y state. A newly pressed digital button is held
in the published controller state for at least 150 ms, so a quick keyboard tap
cannot be lost between native guest slices. Each native execution slice samples
that state into `XboxInputShim` and writes the last successfully consumed state
to `controller.json.consumed.json` for live diagnostics. The reached XAPILIB
calls at `0x0028D282`, `0x0028CF40`,
`0x0028CFA2`, and `0x0028D180` are now bound to that state, including original
Xbox digital buttons, eight analog buttons, triggers, thumb axes, packet
numbers, connected-device masks, and handles.

The July 11 local verification ran 3,000,000 guest instructions to the step
budget with no unhandled native target, captured 18,707 GPU writes, and
published 10 throttled live snapshots. The Vulkan presenter consumed 391 guest
flips as 391 native draws, presented 9,805 host frames, and produced a visible
Burnout 2 copyright/title image before exiting cleanly on Escape. A separate
A-held run observed `0x1000` in all 388 `XInputGetState` polls and wrote `1.0`
to the title's guest A-button consumer. A later normal `live_test.py` run sent a
quick Enter tap: the presenter published and then released `0x1010`, the guest
consumption receipt recorded both transitions, and the title XInput fast path
counted two A-pressed polls. The native runner now yields to HLE
handlers even when their addresses share a compiled module, keeps volatile
title/NV2A pages callback-backed, resolves ESP-relative indirect calls before
pushing their return address, and automatically recovers executable indirect
frontiers. Live snapshots are gated by elapsed time and newly captured GPU
writes so serialization no longer dominates guest execution.
Transient Windows sharing violations, missing-file replacement windows, and
incomplete controller JSON reads are deferred to the next native slice while
the runner retains the last valid controller state; presenter publication can
therefore no longer terminate the guest loop with `PermissionError`.
Compiled-module transitions also remain entirely in the native context now;
the executor synchronizes the full IA-32/x87/XMM/MMX state back to Python only
at real HLE, observer, live-slice, failure, or completion boundaries.
Execution callback targets no longer disable native memory caching for their
entire 4 KiB code pages. The callback-backed data set is restricted to the
small set of guest status, progress, submission, context-sync, and repair
sentinels whose read behavior is genuinely volatile.
Image-backed guest pages below 16 MiB are now eligible for the same generation-
tracked native page cache as heap pages. This removes the former fallback of
hot title code/global/stack-adjacent reads through a Python callback.
Those image-backed cache fills are assembled a page at a time from mapped XBE
regions plus contiguous overlay runs, avoiding thousands of per-byte arena
region searches for every newly cached page.
Large sparse/image-backed reads (including live texture snapshots) use the same
page materialization path rather than walking every payload byte in Python.
The Vulkan presenter also suppresses per-command JSONL diagnostics during live
reloads. Aggregate stream/method/draw counters remain, but tens of thousands of
synchronous log writes no longer run on the presentation thread for every
snapshot.
The host's recovered-stream loader now scans fields linearly inside each JSON
object instead of constructing and executing multiple regular expressions per
field per command during every hot reload.
The title-screen input and shutdown stall was a missing cooperative yield inside
the emitted native `rep movs*`, `rep stos*`, `repe cmpsb`, and `repne scasb`
loops. Ordinary guest instructions check the live slice budget before every
instruction, but a repeat instruction previously ran its entire ECX count as
one host C++ loop. A large or malformed count could therefore keep Python from
sampling controller updates or the presenter stop token for more than the
wrapper's 30-second shutdown grace period. Resumable native repeat instructions
now checkpoint every 65,536 iterations, preserve their partially advanced
ECX/ESI/EDI state, yield without charging an extra guest instruction, and
continue at the same guest EIP. A real headless title-range regression reached
`235,912` GPU writes, `3,186` XInput polls, and `1,487` frontend music iterations
with no decode frontier, then consumed the live stop token and exited in `0.85`
seconds instead of requiring forced termination. A second title-range run
consumed a fresh Start publication in `46.1` ms, consumed its release, advanced
from `235,874` to `315,715` GPU writes over the following five seconds, and
stopped in `0.88` seconds. The normal presenter retains its 150 ms minimum
digital-button hold so the recovered XInput polling loop sees short key taps.
Live render capture retains the exact ordered GPU command history. Each observed
write is packed once into a fixed-width record held in a bounded producer deque;
an append-only `B2APPND1` binary sidecar receives each new record independently
of frame publication. A tiny JSON manifest is now published only for a completed
NV097 flip and carries that flip's exact
`presentable_command_record_count`. Sidecar growth from the next in-progress
frame is therefore never mistaken for a presentable update. The empty startup
manifest was also removed, so the normal launcher starts Vulkan only after the
first complete frame exists. This preserves cross-epoch begin/end and
inline-vertex semantics without raw-write JSON.
The first publication and every lossless-audit publication remain atomic file
replacements. Normal steady-state publications use one shared in-place write;
the presenter opens with read/write/delete sharing and rejects an empty or
truncated JSON object, retrying the same timestamp after the write completes.
This removes per-frame rename/scanner latency without accepting partial frame
state.
The manifest carries a per-run command-stream generation ID; while it remains
stable, the presenter retains parsed command objects, NV2A method state, and
completed-flip identity, and reads/interprets only the newly presentable 16-byte
records. A changed generation or rewind forces a safe full reload.
Each generation uses a distinct sidecar path, allowing the presenter to retain
one shared read handle and advance from its previous byte offset while the
producer appends, without interfering with a later run's atomic initialization.
Steady-state reload detection watches only the validated manifest timestamp;
command/resource sidecar changes before that boundary are deliberately ignored.
Generation changes close the prior read handle before binding the new path, so
a restarted guest cannot fall back to stale history.
Texture snapshots carry address/payload hashes and are atomically maintained in
a persistent resource sidecar. Per-frame command files reference that sidecar,
so unchanged payloads are not rewritten while presenters that join after boot
still load the latest complete texture set.
Command-sidecar flushing and texture discovery are decoupled from manifest
publication: command records can drain continuously, while texture
bindings/payload hashes refresh at a bounded cadence and completed flips alone
become visible to Vulkan.
The presenter tracks the resource-sidecar timestamp independently. When it is
unchanged, live command reloads retain the existing Vulkan images, samplers,
descriptor pool, and descriptor sets instead of decompressing and uploading
the same texture again. The timestamp check now precedes sidecar loading, so an
unchanged texture payload is not reparsed or hex-decoded on command-only reloads.
Current manifests use an explicit resource-generation ID for this decision;
timestamp comparison remains only as compatibility fallback for older streams.
Normal live presentation keeps one delete-shared manifest handle across the
run and rewinds it for each generation. Opening that tiny file had cost roughly
`7.6` ms per reload on the measured Windows machine; the retained handle reduces
the manifest-read median to `28` microseconds. Appended command records are read
with one contiguous I/O operation up to the manifest's exact completed-flip
boundary, and diagnostic command-kind counts advance over only that new range.
The audit path still reopens its atomically replaced per-flip manifest so it
always follows the newest file identity.
Normal texture resources use compact `B2TEX001` binary generations at unique,
immutable paths instead of reparsing and rewriting multi-megabyte hex JSON.
Each record carries dimensions, format, raw payload, and SHA-256. Vulkan retains
images whose address/dimensions/format/content hash match the new generation,
rebuilds only descriptor bindings, uploads only new or changed images, and
destroys obsolete images after reconciliation. The preflight loader accepts
both the binary format and legacy/audit JSON snapshots.
Live debug events are buffered without synchronous stdout echo, and a growable
host-visible vertex allocation is retained across reloads, removing per-event
flushes and repeated Vulkan buffer allocation from the presentation hot path.
Push-buffer reconstruction uses a fixed 64 KiB validity/value workspace matching
the guest ring instead of allocating an ordered-tree node for every observed
byte on every reload. It reads retained command payloads in place instead of
copying thousands of small vectors, and dominant-color diagnostics use a
reserved hash table with deterministic tie resolution.
Live reload events report command loading, interpretation, device wait,
retained-resource update, command recording, and total microsecond timings for
repeatable FPS profiling. These measurements are diagnostic only: performance
work must preserve guest instruction, memory, timing, render, and ABI semantics;
frame skipping, reduced render fidelity, and probe-only execution shortcuts are
not accepted as performance fixes.
Normal native execution now yields on every exact completed NV097 flip, caps
publication at `16,666,667` ns, and uses paired cross-process events plus a
small generation/flip acknowledgement record. Vulkan ingests and acknowledges
each immutable flip while otherwise waiting for the next presentation deadline;
the guest cannot overwrite a boundary the presenter has not consumed, and the
two processes do not serialize guest frame computation behind a full host
refresh interval. The presenter uses a Windows high-resolution waitable timer
rather than integer `16` ms sleeps, eliminating scheduler-tick overshoots.
Microsecond frame timing is retained in the host events and report; millisecond
fields remain for compatibility.
Live interpretation no longer uses the former 65,536-command tail-context or
32K replay-window heuristics. Persistent NV2A state is advanced across the exact
new command range, while completed draw/vertex history is compacted to the
latest presented frame plus the in-progress frame. Reload cost is therefore
incremental without discarding guest commands or approximating state.

The July 14 jitter regression under
`reports/local/playability/jitter-fix-v10` ran `12,000,000` guest instructions
through `494` completed flips. The first load plus `493` reloads consumed all
`494` publications in order; every reload reported
`exact_completed_flip=true`, with `493` distinct flip IDs and no redundant
same-flip reload. Presenter reload p50/p95/p99 was
`0.775/1.026/1.305` ms, command-load p50/p99 was `0.109/0.288` ms, and reload
busy time was `3.73%`; the pre-fix run measured a `164.405` ms reload p99,
`110.146` ms command-load p99, and `13.78%` reload busy time. Later texture
generations reused `2/2` and then `3/3` resident images while uploading one new
image each. Excluding the one-time `195.086` ms startup frame, the presenter
delivered `59.900` FPS; steady frame p50/p95/p99 was
`16.647/17.048/17.216` ms. The bounded launcher and guest both exited normally,
without the former forced-termination warning. A follow-up five-flip lossless
audit under `reports/local/playability/jitter-fix-audit` retained all five
ordered ledger boundaries, selected and acknowledged three health-distinct
frames, passed strict render validation, and shut both processes down normally.

The live-loop workflow is now managed by `tools/playability/live_test.py`, which
starts the guest runner, waits for its first atomic render publication, starts
the presenter, and requests a clean guest stop when the window closes. Bounded
runs now close the Vulkan child through its real window before finalizing JSONL
diagnostics; process-tree discovery handles the Python smoke wrapper, and forced
cleanup terminates that wrapper's complete process tree if graceful closure
fails. A zero step budget is consistently treated as unlimited by the lifted
and native execution paths.
Startup now retries a transiently empty or truncated shared live manifest for up
to two seconds. This closes the race where Vulkan could open the manifest during
the producer's in-place write, treat the partial JSON as fatal, and immediately
close its window. A presenter stop observed by the live bridge is also a
process-level terminal condition: the scheduler records the first thread as
`live_stop` and does not start another unlimited Xbox system thread without the
controller bridge. The July 14 shutdown regression run under
`reports/local/playability/shutdown-e2e` posted a real `WM_CLOSE` after 147
presented frames; Vulkan emitted normal main-loop exit/shutdown events, the
guest recorded `yield_handler_stop` after 3,587,500 steps and 115 flips, only
one of three scheduled threads executed, and the launcher exited without its
30-second forced-termination warning.
The menu presenter now also recognizes the title's observed fullscreen
`640x448` overscan quad and scales its vertical extent to the `640x480`
swapchain. Previously only its half-width X/U encoding was reconstructed,
leaving the bottom 32 rows untouched and visible as a black bar.

The automated pre-manual gate currently inventories all 200 frontend dictionary
textures (70 DXT1 and 130 DXT5) with no unsupported or malformed payloads. Its
strict real-host replay passes both checked boot stages, validates all 1,718
title draws against eight texture snapshots, and proves the publisher/title
pixel frames differ. The matching 18,000,000-step probe has zero dynamic
frontiers, one submitted looping menu track, and no audio decode errors.

The lossless live audit path passed a fresh 20-flip native/Vulkan handshake on
July 13, 2026 (`reports/local/playability/completed-flip-e2e-v7`). All 20
producer flips were acknowledged in order, all 20 frame readbacks were
pixel-distinct, and the aggregate strict report passed. The 19 post-startup live
reloads all reported `exact_completed_flip=true`, with zero inexact or redundant
same-flip reloads and zero malformed JSONL lines. Incremental interpretation
across those reloads totaled `1.781` ms; the run ended with a normal `shutdown`
event and process code zero. Guest and presenter now honor the same bounded
audit limit, preventing a spurious wait for flip N+1 after Vulkan has completed
flip N.

The July 11 adaptive-audit performance gate recorded all 600 producer flips in
18.119 presenter seconds (`33.115` audited producer FPS) and 19.483 seconds
including guest/presenter startup. Candidate detection selected 31 flips for
strict Vulkan validation and full 640x480 health readback and allowed the other
569 to continue without a cross-process wait. All 31 selected producer indices
matched host acknowledgements, 32 validation events had zero failures, and the
aggregate report passed. The prior every-flip synchronized audit managed only
about `5.2` flips/second over its 373-flip run; sampling readback while retaining
per-flip synchronization reached `13.9` flips/second over 300 flips. Removing
the unnecessary synchronization from stable flips is what restored the audit
loop to above the 30 FPS target.

The synchronized 3,000,000-step performance gate on July 11, 2026 passed both
sides of the live loop with the presenter already in `main_loop_enter`: the
runner completed 192 guest flips in 6.116 seconds (31.39 FPS including process
startup), while Vulkan consumed 91 distinct update intervals in 2.659 seconds
(34.22 visual updates/second). Median live reload time was 8.95 ms. The final
640x480 readback retained the validated SHA-256
`9a42c57d7549310176aebc2b50d2cc698fc83d4960c176fbd2e43c62955cdadd`.

The sustained 10,000,000-step completion audit passed after history exceeded
the bounded-window threshold: the deterministic stream contained 888 flips and
81,359 writes (command SHA-256
`74f746d5e91dbe145e3aad6fca55e263d5df9461822dc913d440a91479481ce8`).
The runner completed in 19.272 seconds (46.08 guest FPS including startup), and
the presenter consumed 492 distinct update intervals in 15.033 seconds (32.73
visual updates/second). Median reload time was 7.02 ms and p90 was 9.18 ms. A
full-history static replay and the 35,570-command bounded replay produced the
same 640x480 SHA-256
`2caa340ba7db6a81a06d3efd8bd2c5c6821283cdd8d01244f3a542eeb6a24a8c`.

For a later render window, retain the one-time state prefix and merge it with a
delayed capture before replay:

```powershell
python .\tools\playability\playability_probe.py `
  .\data\local\extracted\burnout_2_poi_usa\default.xbe `
  --extracted-root .\data\local\extracted\burnout_2_poi_usa `
  --render-watchpoint-start 32768 `
  --render-watchpoint-limit 32768 `
  --render-stream-output .\reports\local\render\recovered-d3d-stream-late.json

python .\tools\render\d3d8_stream.py `
  --prefix-input .\reports\local\render\recovered-d3d-stream.json `
  --input .\reports\local\render\recovered-d3d-stream-late.json `
  --stream-output .\reports\local\render\recovered-d3d-stream-merged.json `
  --decode-output .\reports\local\render\recovered-d3d-decode-merged.json `
  --replay-output .\reports\local\render\recovered-d3d-replay-merged.json
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
  --max-steps 1000000 `
  --max-dynamic-blocks 65536 `
  --render-watchpoint-limit 32768 `
  --render-stream-output .\reports\local\render\recovered-d3d-stream.json `
  --pretty

python .\tools\render\d3d8_stream.py `
  --input .\reports\local\render\recovered-d3d-stream.json `
  --stream-output .\reports\local\render\recovered-d3d-stream.json `
  --decode-output .\reports\local\render\recovered-d3d-decode.json `
  --replay-output .\reports\local\render\recovered-d3d-replay.json `
  --pretty

python .\tools\host\first_frame_smoke.py `
  --max-frames 3 `
  --render-stream-json .\reports\local\render\recovered-d3d-stream.json `
  --pretty
```

Compare an earlier and later extended checkpoint to prove that guest execution
has left the loading overlay and entered the sustained title/menu frontend loop:

```powershell
python .\tools\playability\frontend_boot_gate.py `
  --early .\reports\local\playability\probe-summary.json `
  --late .\reports\local\playability\probe-summary-5m.json `
  --json-output .\reports\local\playability\frontend-boot-gate.json `
  --pretty
```

## Current Recovery Evidence

Latest local probe:

- The post-title "hang" is guest termination rather than a presenter input
  failure: the guest reached an indirect call through address zero while the
  presenter correctly retained its last valid title-logo stream. Dynamic
  frontier recovery now closes direct calls, conditional targets, and
  fallthrough paths statically, compiles each recovered CFG batch as a separate
  module without invalidating the stable base frame, and permits up to 65,536
  recovered blocks. The newly reached menu path also adds observed `fld/fst`
  64-bit x87 memory forms, `sqrtps`, and a safe empty
  `NtQueryDirectoryFile` enumeration. A held `0x1010` input advanced render
  writes from `235896` to `409395` over 40 seconds, kept the guest alive, and
  consumed the stop token in `1.536` seconds.
- There is not yet a restorable save-state system. Render snapshots, probe
  summaries, the dynamic-block cache, and native DLL cache do not serialize
  the complete CPU, sparse-memory overlays, runtime handles, audio state, and
  live-host bridge state required for a correct mid-title restore.
- Sequential replay checkpoints recovered a complete Criterion splash at
  `72086` D3D writes / `785` flips and a complete RenderWare splash at `104854`
  writes / `1149` flips. These prove continued guest visual progression, but
  sequential delayed capture is no longer the scaling strategy.
- Hot-loop instruction/event tracing can now be disabled or bounded, guest
  sparse memory uses 4 KiB pages, and runtime shim allocation payloads and
  diagnostic histories are also page-backed/bounded. The one-million-step
  paged checkpoint retained `9512905` written guest bytes in `2643` pages
  (`21651456` allocated page/mask bytes), rather than per-byte dictionaries or
  eagerly committed multi-gigabyte shim buffers.
- The resumable C++ ABI carries guest EIP, step budget/count, yield state, exact
  repair observers, and a cached page table. Cached dynamic blocks are split
  into 3,000-instruction modules and compiled four at a time, avoiding the
  several-minute optimizer pass on one 60,000-instruction function. Those
  modules are now partitioned by stable guest-address ranges rather than by
  ordinal instruction count. Previously, inserting each newly recovered
  frontier shifted nearly every later 3,000-instruction module, invalidating
  its native DLL cache and making the post-Dolby path appear frozen while the
  host repeatedly recompiled almost the entire frame. A newly discovered block
  now invalidates only its containing address partition. The real
  one-million-step title checkpoint reaches `983` D3D writes and `250` runtime
  ABI calls in `4.24` seconds with stable memory, versus about `19` seconds in
  the Python interpreter. Bounded live slices now preserve CPU/memory state
  while publishing the newest rolling render window and sampling presenter
  controller state. The recovered title input consumer receives that state, but
  the live native snapshot currently stops without a completed flip or draw.
  Resumable `rep stos*`, `rep movs*`, `repe cmpsb`, and `repne scasb` emission
  now checkpoints partial pointer/count progress instead of restarting a large
  guest operation on every slice. Native preflight also closes the two newly
  observed input-consumer branch gaps at `0x000DA14E` and `0x000DA2A7`. The
  corrected five-million-step native run advances past `0x000F2003`, but
  returns at unresolved target `0xFFFFFFFF` with only `983` render writes, zero
  texture snapshots, zero flips, and zero draws. The matched interpreter run
  reaches `19267` render writes and one texture snapshot, proving the remaining
  live blocker is native execution convergence rather than Vulkan presentation.

- XAPILIB build `5344` signature matching identifies `XInputGetState` at
  `0x0028D180`; its only direct title caller is the port consumer at
  `0x000DA080`. The recovered function writes packet number at state offset `0`,
  digital buttons at `+4`, eight original-Xbox analog buttons at `+6`, and four
  signed thumb axes at `+14`. The title consumer normalizes analog A into port-0
  field `0x005982DC`. A native one-million-step injected-A checkpoint records
  two successful polls and value `1.0` (`0x3F800000`) at that field; the matched
  released checkpoint records `0.0` (`0x00000000`). The connected path also
  exercises `XGetDevices` once, `XInputOpen` once, and
  `XInputGetCapabilities` once.

- Entry execution at `0x000E2555` still returns to the probe sentinel after
  `72` steps and decodes the current handoffs at `0x000E68C4`, `0x000E5D5D`,
  and `0x000E099C`.
- The recovered dynamic executor now reaches `3519` dynamic blocks with `0`
  dynamic decode frontiers and records no missing instruction boundary on the
  latest extended playability runs.
- The scheduled guest thread at `0x000E682C` clears the former startup,
  compare/search, DirectSound, post-audio, child-dispatch, asset-manager, x87,
  and record-table blockers. An earlier checkpoint reaches a classified render
  boundary with `10739` D3D writes, `4395` runtime ABI calls, `158` frontend
  method dispatches, and `471` record scans. A later checkpoint reaches the
  configured `32768`-write render watchpoint with `6087` ABI calls, `299`
  frontend method dispatches, and `894` record scans. Both retain `0` decode
  frontiers and only the same `2` record-count repairs.
- The former `0x0010C0E0` startup work-queue fast path was incomplete and is no
  longer registered in production execution. The real guest helper allocates a
  variable-sized object and builds two callback tables after its list lookup;
  the shortcut returned a small synthetic node at `0x31F0C000` without those
  tables. Frontend initialization later loaded that node through
  `[0x10098780 + 4]`, derived callback slot `0x28` from a null table, and called
  guest address zero. Production now executes the recovered guest helper. A
  bounded proof through the real helper completed successfully, so the obsolete
  synthetic model, its report field, and its isolated unit tests have been
  removed.
- Guest-thread execution now has an early scheduler-loop convergence detector
  for the observed `0x0010C100`/`0x0010C112` scan. It can classify a repeated
  stable scan after `32` identical iterations instead of spending the full
  thread step budget.
- Production execution no longer bypasses the title frontend constructor at
  `0x000CB420` or the supported 16-byte compare at `0x00106BF0`. The bounded
  root-cause run in `reports/local/playability/batch-frontier-final.json` executes
  the real 16-entry subsystem initializer table with 16 successes, builds real
  frontend object/state/resource pointers, returns constructor result `1`, and
  performs no subsystem shutdown. The old synthetic constructor and compare
  handlers remain available only to focused diagnostic fixtures.
- The `0x0010D150` compare/search path is characterized as a recursive frontend
  object-tree walk whose child-list sentinel lives at `object + 0x10` and whose
  list links map back to child objects by `0x18`. Production now runs it without
  synthetic roots or link repair: the final headless proof records 12 valid
  searches, no null root, and zero repairs.
- Earlier heap free-list, startup work-queue scan, scheduler/timer,
  GPU busy/interrupt, x87, disc root, frontend dictionary, D3D switch-target,
  title render-loop, MMIO readiness, allocation-list scan, semaphore ABI,
  stack-contract, frontend constructor, frontend child-dispatch, and frontend
  sentinel blockers are cleared on the current path.

Runtime/API stance:

- `NtDeviceIoControlFile` now uses its observed ten-argument guest contract and
  cleans up `40` guest stack bytes. The current calls use disc handle `0x108`
  and control code `0x0004D014`; they are disc verification, not the missing
  controller polling consumer.
- Runtime work is title-directed. Implement Xbox APIs only when this XBE reaches
  a concrete edge with observed arguments, side effects, and return contracts.
- Current observed fixes include five-argument `NtAllocateVirtualMemory`,
  reserve/commit base preservation for the title heap,
  four-argument guest cleanup for `AvSendTVEncoderOption`,
  six-argument guest-buffer marshaling for `HalReadWritePCISpace`, and
  `RtlCompareMemoryUlong` guest-buffer marshaling.
- `HalRegisterShutdownNotification` now consumes its Xbox registration pointer
  and Boolean flag, and `KeInitializeInterrupt` consumes all seven Xbox
  arguments. Their former one- and three-argument host signatures left guest
  arguments on the stack, corrupting multiple hardware initializers and making
  DSOUND return through bogus targets `0x00000001`/`0x10884110`.
- MCPX/NV2A completion is now event-driven: a GPU kick through `0x80000000`
  publishes the current push address to the dynamically allocated DMA status
  block; DSP reset publishes ready bit `0x100`; and the two voice-command bytes
  at `0xFEC0011B`/`0xFEC0017B` self-clear pending bit `0x02`. These are hardware
  protocol transitions, not screen-specific state edits.
- `NtQueryVolumeInformationFile` now uses its exact five-argument Xbox ABI,
  writes class-1 volume or class-3 allocation-size output, updates the I/O
  status block, and cleans all `20` guest stack bytes. The former one-argument
  host adaptation left `16` bytes on the Load/Save caller's stack after every
  free-space query. `NtQueryDirectoryFile` now writes the Xbox 0x40-byte,
  single-byte-name class-1 record and retains wildcard/cursor/restart state per
  directory handle instead of unconditionally returning `STATUS_NO_MORE_FILES`.
  `U:` and `T:` resolve to the current title's `UDATA\41430019` and
  `TDATA\41430019` directories rather than falling through to the DVD root.
  The normal live launcher supplies persistent local roots for those volumes.
  Modeled file/directory handles remain shared across these calls,
  four-argument guest `NtCreateSemaphore` writes the created handle to the
  title's out pointer, invalid `NtReleaseSemaphore` handles return
  `STATUS_INVALID_HANDLE` instead of escaping as host exceptions, and the
  observed three-argument guest `NtReleaseSemaphore` stack contract writes the
  previous count out parameter while dispatching the two host-side arguments.
- `ObReferenceObjectByHandle` now uses the observed three-argument guest stack
  contract, and `ObfDereferenceObject` uses the observed ECX object argument
  with no guest stack cleanup.
- Filesystem modeling now treats exact `\Device\CdRom0` and mapped volume roots
  as read-only directory/volume handles instead of missing stream files.
- Contiguous allocation shims now reject non-positive allocation sizes
  deterministically instead of manufacturing zero-sized allocations.
- Page lock/persist operations now validate committed 4 KiB backing pages, so
  a page-rounded `MmLockUnlockBufferPages` request is valid for a smaller
  contiguous allocation on that page while unrelated pages still fail.
- Probe memory now models `0xFE820010` as the MCPX free-running frame counter it
  actually is, advancing by four per read. All 33 title references use aligned
  progress thresholds (immediate waits range through `0x80`, with additional
  dynamic thresholds); the former fixed `0x20` value deadlocked DSOUND at
  `0x00233F42`.
- Probe memory now aliases the title's bit-31 CPU view of contiguous GPU memory
  (`0xA0000000`-`0xBFFFFFFF`) to its NV2A-visible physical address. The recovered
  texture allocator keeps surface offsets in the `0x20000000` range while its
  allocation headers and locked payload writes use the corresponding bit-31
  address; treating those as separate sparse-memory regions was why captured
  surfaces appeared zero-filled.
- Title-owned cleanup, resource-cache, registry, and initializer sentinels are
  no longer seeded or repaired by production memory. The real CRT/subsystem
  constructors initialize all four successfully. Sentinel and frontend repair
  fallbacks are disabled by default and retained only as explicit diagnostic
  fixtures; `batch-frontier-final.json` completes with every seed/repair count at
  zero.
- Probe-only list acceleration models the title allocation-list count helper at
  `0x00109BD0` against sentinel `0x005A6EBC`; the latest post-helper path no
  longer exercises it, while the prior scheduler-bound run used it `251` times,
  scanned `20504` nodes, observed a max list length of `202`, and found no
  cycles.
- Probe-only D3D modeling now seeds the observed synthetic context at
  `0x21B70000`, the title submission window globals at
  `0x002256C0`/`0x002256C4`, the D3D state descriptor switch at `0x002252D0`,
  the context list nodes used by the cleanup helper at
  `0x21B75000`/`0x21B75020`, the packet allocator at `0x0021AFD0`, and the
  push-buffer reserve helper at `0x0021B1C0`. The observed immediate vertex
  append helper at `0x000C20E0` is modeled as a probe fast path with exact
  five-argument stack cleanup and separate tail fields.
- Current IA-32 coverage added from the boot path includes no-base SIB
  addressing, MXCSR load/store, `wbinvd`, `sfence`, `out dx, al`, expanded x87
  stack/memory arithmetic and stores, `fild`/`fistp` qword paths,
  `movsx r32, r/m16`, `or al, imm8`, prefetch hints, and the observed MMX
  `movq`/`movntq`/`emms` copy path. Latest title-directed additions include
  `cmpsb`/`repe cmpsb`, `cmpsd`/`repe cmpsd`, `fldlg2`, `fyl2x`,
  `xchg r/m32, r32`, unsigned
  `mul r/m32`, `jecxz`, `scasb`, `repne scasb`, and the observed immediate
  `shld`/`shrd` double-shift forms plus one-operand byte `imul r/m8` and signed
  wide-result `imul r/m32` (first reached at `0x000C277A`). The
  frontend path now also supports observed `fisub m32int` at `0x0002A93A`,
  including executor and deterministic C++ emission semantics. The post-Dolby
  path additionally supports the `DC` x87 64-bit memory arithmetic family,
  first observed as `fadd m64real` at `0x0003733E` and `fmul m64real` at
  `0x00037344`, including little-endian 64-bit floating-point memory reads in
  both execution engines.
  DSOUND initialization also required correct x87 constant/transcendental
  register forms: the decoder and both execution engines now support the full
  `fld1`/`fldl2t`/`fldl2e`/`fldpi`/`fldlg2`/`fldln2`/`fldz` family plus
  `frndint`, `fscale`, `f2xm1`, `fsqrt`, `fsin`, and `fcos`.
- A CFG-aware coverage audit now seeds every dynamically recovered block, uses
  Capstone as an independent instruction-length oracle, follows direct calls
  and both conditional paths, and stops at returns/indirect jumps. The first
  full audit visited `15626` blocks and `93289` instructions and identified
  `213` unsupported sites across `43` mnemonic families; unlike the earlier
  linear neighborhood scan, it does not intentionally decode through data
  after control-flow terminators. After the observed byte `imul` addition and
  expanded dynamic cache, the latest audit visits `15647` blocks and `93343`
  instructions with `212` remaining sites across `42` families and no pending
  CFG blocks.
- The reached DirectSound private buffer/DSP synchronization method at
  `0x00230850` is no longer the active probe boundary. The runner intercepts
  the containing effect-image transaction at `0x00230ABD`, returns a stable
  workspace through its observed output pointer, and preserves its three-
  argument callee-cleanup contract. This avoids descending through the missing
  private DirectSound parent object while retaining the operation that the
  XACT caller actually requested.
- The reached `audio/special.rws` and `audio/fe_menu.rws` assets use RenderWare
  `0x809` containers with signed mono PCM16 payloads. The live runner parses
  those containers, and its audio backend can queue PCM to Windows waveform
  output on a dedicated worker when a real play transition
  submits it. The parser also validates every extracted title audio bank (`carext`, `crash`,
  `fe_menu`, `in_menu`, `special`, and `traffic`), covering 135 PCM clips at
  14, 22.05, or 44.1 kHz.
- Frontend sound-bank creation at `0x000CB690` no longer submits PCM to the
  host. The recovered function parses and returns an opaque bank handle; it
  does not issue a play command. Removing that eager submission eliminates
  the repeated `special.rws` effect heard during boot and leaves playback for
  the forthcoming observed voice-control transition.
- Native NV2A replay now snapshots the texture-enable bit per draw and binds
  the white fallback only for genuinely untextured work instead of reusing a
  stale stage-0 texture. The title trace also records transform-program upload,
  program-start, texture-format, blend, color-mask, depth, and render-surface
  state. The reached 12-instruction title program is a conventional screen-
  space transform (`oPos = v0 * c0 + c1`), confirming that the title artifact
  is state/resource translation rather than an unsupported Vulkan instruction.
- The post-audio frontend list scan at `0x0010C8AD` now repairs an observed
  synthetic node whose null next link should close on sentinel `0x005A70E0`.
  This prevents the recovered iterator from deriving `0xFFFFFFE8` after the
  DirectSound service boundary returns.
- The loading frontend now reaches record-table scan `0x00112873`. Its
  deterministic synthetic table at `0x10005C78` carried uninitialized count
  `0xFFFFFFFF`, and a later table at `0x10005CB8` carried aligned heap pointer
  `0x1001E688` in the count field, each producing an effectively infinite
  record walk. A probe-only entry observer clamps reached counts above `65536`
  to empty; plausible counts remain under guest control.
- The synthetic frontend asset manager now carries the two observed virtual
  method slots at offsets `0x18` and `0x1C`; calls through trampolines
  `0x000E8450` and `0x000E8440` are modeled as successful no-cleanup
  asset-service dispatches. This clears the null jumps reached from
  `0x000B8AE9` and `0x000B8B58` without broadening unrelated vtable slots.
- The dirty-disc path is no longer the active boundary. The title's internal
  stream provider was returning null for the existing `audio/special.rws`, not
  failing in the filesystem shim. The probe now validates each title-relative
  path against the configured disc root and provides distinct per-open stream
  objects with real payload bytes, read/seek methods, synchronous-ready status,
  and isolated positions. The latest three-million-step run opens `8` streams,
  performs `674` reads (`8,723,776` bytes), `17` seeks, and only `4` status
  polls; no dirty-disc text draw is observed.
- Dynamic block recovery now retries instruction-limit truncation with a
  bounded adaptive limit up to `16384` instructions. This recovered the long
  `0x00028C50` initializer as one native block instead of treating its middle
  as a new control-flow gap.
- The missing CRT state was traced to the guest ABI boundary. Constructor
  `0x00126734`, the first non-null entry in the real `0x002CD670`-`0x002E98F4`
  CRT table, calls `KeQuerySystemTime` through thunk `0x000E24B6` with one
  output pointer. The bridge inferred zero guest arguments from the host
  model's return-value signature, left that pointer on the stack, and the
  constructor restored it into `ESI`; the CRT walker then treated the pointer
  as an iterator beyond its table and exited after one of `28,832`
  constructors. `KeQuerySystemTime` now writes the 64-bit time through the
  guest pointer, preserves its void return contract, and cleans four stack
  bytes. Recovery permits the configurable `65,536`-block budget so the
  restored constructor path is not cut off by the former `4,096` ceiling.
  A bounded `327,680`-step native smoke recovered `21,870` constructor blocks,
  including `0x001392B0`, and advanced the real table iterator to
  `0x002E2B5C`; its ABI record contains one pointer argument, an eight-byte
  output write, no synthetic return value, and four-byte cleanup.
- The former synthetic no-op reset callback for `12` embedded frontend objects
  has been removed. XBE routine `0x001392B0` is entry `0x002CF664` in the real
  CRT table and writes those objects' actual vtables. A read-only audit records
  execution of that initializer and, at the first virtual reset, each object,
  vtable, method-0 target, and whether the table still points to `0x001392B0`;
  it never modifies guest state.
  Downstream singleton/runtime-table repair observers remain diagnostic and
  should report zero repairs once the original CRT table completes.
- The first normal run after the CRT ABI correction confirmed initializer
  `0x001392B0` executed once with zero null objects or vtables, then stopped at
  dynamic block `0x00053EB0` because the decoder lacked opcode `A0` at
  `0x00053EBF`. The retail instruction is an ordinary absolute byte load,
  `mov al, byte ptr [0x002BBB9C]`, inside a 24-entry initialization loop. The
  x86 lifter now supports both accumulator `moffs8` forms (`A0` load and `A2`
  store); the interpreter and generated native backend preserve the upper 24
  bits of `EAX` on the byte load. The observed retail block now lifts through
  its loop branch without any screen-specific state repair.
- The recovered frontend path also models the observed opaque special-audio
  handle and a null parsed callback owner as an explicit empty callback list. A full
  three-million-step rerun confirmed the synthetic child descriptor needs the
  method-table pointer at both observed offsets `+0x04` and `+0x08`, then
  exposed additional dispatches through table indices `1` and `4` at
  `0x0010C55F`. Index `1` now executes successfully in the extended probe.
  Synthetic child method slots `1`, `2`, `4`, and `5` are seeded and
  unit-covered. Index `4` now executes successfully in the extended probe,
  exposing a later malformed call into dispatcher `0x0010C510` with object
  argument `0x00000004`. The first widened trace confirmed that low pointer
  comes from selector helper `0x0010C5C0`: child method slot `4` receives a
  pointer to the selector and must replace it with the selected object before
  the helper returns. The synthetic slot now writes child object `0x31F06000`
  through that observed out parameter and is unit-covered. Probe failure trace
  tails retain `1024` events so future producer chains remain visible. The
  corrected selection now advances with valid object `0x31F06000` to observed
  method slots `9` and then `8`; slot `9` succeeds in the extended probe and
  slot `8` also succeeds. The next observed request is slot `10`, completing
  the visible setup sequence across slots `4`, `5`, `8`, `9`, and `10`; slot
  `10` succeeds, followed by successful slot `0` and then a request for slot
  `3`, which succeeds before the tail requests slot `6`. The synthetic table
  now covers exactly the observed indices `0`, `1`, `2`, `3`, `4`, `5`, `6`,
  `8`, `9`, and `10`, all unit-covered for the next extended run; unobserved
  slot `7` remains unset. After slot `6`, the recovered initializer exposes a
  separate malformed list head at `0x005A727C`, whose expected self-linked
  sentinel contains scalar float bits `0x3FD99989`. Probe memory now seeds the
  empty sentinel and repairs null or unaligned links without replacing aligned
  guest nodes. That repair reaches a second empty traversal owned by the
  synthetic special-audio handle `0x31F10400`; its embedded list head at
  `+0x10` now closes on the observed sentinel at `+0x0C`, replacing the former
  low-memory two-node cycle.

Asset and deterministic service evidence:

- The frontend asset initializer at `0x000B9030` now runs as guest code instead
  of being replaced by the former synthetic `impact2` dictionary. The real
  initializer opens and parses `frontend/global.dic`; its `b2logo` upload is
  `32768` bytes and exactly matches the dictionary payload. Native Vulkan replay
  of `reports/local/live/render-real-init.json` consequently binds the real
  `256x128` logo texture instead of drawing the untextured white fallback quad;
  the verified readback is
  `reports/local/screenshots/real-frontend-init-title.bmp`.
- Restoring that initializer exposed CPU translation gaps, not a D3D8-to-Vulkan
  block. The x86 lifter and both execution backends now support the observed
  `fptan`, `unpcklps`, `unpckhps`, `movlhps`, `movhlps`, and the 64-bit memory
  store forms of `movlps`/`movhps`. A four-minute run completes the real
  dictionary and frontend stream setup without a dynamic decode frontier.
- The latest extended path reaches real frontend stream assets including
  `audio/special.rws`, `frontend/check.dff`, `frontend/start.dff`,
  `frontend/finish.dff`, and `frontend/kojack.dff`. The unrelated early
  `\Device\Harddisk0\partition1\` save-root probe remains a clean not-found and
  is not the cause of the former dirty-disc panel.
- Modeled title hardware completion now includes NV2A busy-clear writes, GPU
  completion polls, GPU/PFIFO interrupt write-one-to-clear acknowledgements,
  PFIFO idle reads, a monotonic progress counter, seeded D3D context data on
  the paths that require it, and seeded push-buffer submission windows on the
  paths that require them. The latest run records `3` GPU completion polls,
  `11` PFIFO runout/status reads each, and one GPU/PFIFO interrupt
  acknowledgement each before the current sustained render loop.
- A probe-only title heap fast path now gives the observed allocation/free
  trampolines unique aligned allocations and avoids the previous stream/list
  object alias. This is constrained to the probe and should be replaced or
  validated against recovered allocator semantics before broadening it.
- An earlier frontend scan identified `Frontend/global.dic`, key `impact2`,
  embedded table data at `0x002E9940`, and global list storage at `0x00443EE8`.
  The later real-initializer trace showed that the title lookup actually requests
  `b2logo`; the synthetic null resource was the cause of the white rectangle.
- Streaming and input-latency validation remain limited beyond the current
  frontend path. Save-volume routing, free-space output, and directory records
  are unit-covered; actual title save-file contents still require manual/gameplay
  validation. The synthetic DirectSound workspace moved from
  `0x31F18000` to `0x31FF0000` so it cannot alias the ninth synthetic asset-stream
  object used by `audio/fe_menu.rws`.
- The title music wrapper at `0x000CC2F0` now repairs its null manager holder at
  `0x00443FA0`, records mode transitions in a stable synthetic manager, and
  submits music only on entry to mode `2`. The real menu track,
  `music0/trk07menust.rws`, is parsed as streamed RW `0x80D` Xbox IMA-ADPCM and
  decoded once to `48000` Hz stereo PCM (`7203840` bytes, `37.52` seconds).
  Repeated frontend polls preserve previous mode `2` and therefore no longer
  restart or duplicate playback. Audible output is now confirmed in the live
  host. The WinMM worker loops the submitted menu track while mode `2` remains
  active and consumes a stop request when the guest changes modes, rather than
  ending after the first `37.52`-second decode. Live host PCM is scaled to a
  `0.5` application-local master gain before playback without changing the
  Windows system volume; the audio summary records the configured gain.
- Frontend special-audio creation now submits the decoded `special.rws` PCM
  clip instead of returning only an opaque handle. The WinMM backend normalizes
  music and effects to 48 kHz stereo and mixes short chunks. It now keeps four
  raw PCM `waveOut` buffers queued instead of issuing a blocking, one-chunk
  `PlaySound` call at every 50 ms boundary; this removes the scheduling gap that
  caused audible jitter while retaining music/SFX mixing. A bounded native verification
  submitted the looping track plus both requested effects with zero drops,
  decode failures, or playback errors.

Visual/render evidence:

- The prior manual Load/Save capture was visibly incorrect upstream of Vulkan:
  captured guest vertices already contained zero-area points at `(320, 0)` and
  displaced UI coordinates. The later frozen-logo capture is a separate
  control-flow failure: Vulkan continued presenting the last valid stream after
  the guest stopped at the null callback described above. No Load/Save
  coordinates, draw bounds, assets, or presenter transforms are overridden.
- Frontier diagnosis is batched by static guest control flow. Each newly reached
  root contributes its direct-call graph, both conditional paths, and branch
  fallthroughs up to the configured bounds before one native rebuild. Indirect
  targets still form new batch roots when runtime state first resolves them.
- `batch-frontier-final.json` must no longer be read as normal guest-thread
  completion. Its `1,714,486`-instruction boundary was the incomplete synthetic
  helper's null callback escaping to address zero. Register/stack audit now
  identifies the exact failing instruction at `0x0010C530`, callback return
  address `0x0010C532`, object `0x10098780`, synthetic descriptor
  `0x31F0C000`, and null dispatch-table base. The replacement bounded proof in
  `reports/local/playability/real-helper-proof-5m.json` reaches its full
  `5,000,000`-instruction budget with the real helper, no unhandled target, no
  dynamic frontier, all `16` subsystem initializers successful, `8` compatible
  registry attaches, `6` successful registry lookups, no subsystem shutdown,
  and every production repair fallback disabled. Native threads that reach an
  intentional bound are now reported as `step_budget` rather than as a false
  `thread_returned_to_guest` playability gap. Manual visual validation remains
  pending.
- Vertex append and render-boundary audits now aggregate producer return addresses,
  coordinate bounds, anomaly classes, object addresses, and their exact render
  write/guest-flip counters. The append probe also reads the quad submitter's
  stable `+0x54` parent-return slot, so reports rank the actual frontend quad
  producer rather than stopping at its five low-level corner append calls.
  A rolling 256-flip geometry timeline breaks those producer bounds/counts down
  by guest flip and joins them to the host's final geometry signature for that
  flip, preserving the title-to-Load/Save transition instead of flattening an
  entire manual run into one coordinate range.
  Text diagnostics retain both the first and latest bounded samples, their
  coordinate arguments/source strings, and the renderer parent's return
  address, so Load/Save text is not displaced from the report by earlier title
  samples.
  Only the first, new-flip, new-caller, and periodic
  malformed samples retain the heavier register, frame-chain, stack-word,
  active frontend-object, dynamic-object, and vertex-control snapshots. This
  keeps the capture bounded and avoids restoring per-vertex JSON logging in the
  native loop. Runtime ABI records likewise retain their guest return address
  and rank failed calls by shim/caller, making the recurring invalid semaphore
  and wait sequence traceable to a concrete XBE call site.
- The Vulkan presenter emits `nv2a_presented_geometry_anomalies` on every reload
  with invalid-range, non-finite, collapsed-axis, zero-area, exact-center-origin,
  offscreen, viewport-overflow, and overall-bound metrics. Detailed draw,
  transform-program, and vertex payloads remain sampled. The automatic render
  debug report joins these host metrics to the bounded guest provenance using
  shared flip and command-write counters.
- Re-analysis of the first July 13 Load/Save capture
  `reports/local/screenshots/b2-recomp-20260713-172800-146-frame-27913-1.bmp`
  found that the old report had incorrectly reused a black readback from host
  flip `52` as evidence for the final host flip `2314`. The corrected report
  correlates guest producer flip `2313` to presented flip `2314`, refuses the
  stale readback, and emits `current_presented_flip_readback_missing` instead of
  a false dominant-black conclusion. The older v2 report then made a second
  overreach: it treated all vertices built by `0x000C20E0` as submitted draws
  and emitted `guest_fullscreen_geometry_missing_on_host`. Those vertices are a
  CPU-side immediate-list staging buffer and may be clipped, discarded, or
  batched before a draw. Report v3 therefore uses only the read-only audit at
  the actual `0x000EE1B0` immediate-array submission wrapper for guest/host
  composition conclusions; builder geometry remains provenance. Old artifacts
  now emit `guest_draw_submission_audit_missing` instead of the false loss
  claim.
- The entry at `0x0021CA20`, previously labeled as a primitive draw, is the D3D
  clear path: its observed arguments include clear flags `0xF3`, color, depth,
  and stencil, and it is called once per frame. Production execution runs its
  recovered guest code and attaches a read-only entry audit. The opt-in
  return-only implementation remains only for its isolated legacy contract test
  and for recognizing old artifacts; report v3 identifies it as an
  accuracy-losing clear replacement rather than a missing draw.
- The fresh July 13 manual capture reaches exact completed guest flip `1013`.
  Its final completed prefix contains `295609` presentable and interpreted
  command records, with `exact_completed_flip=true`; the append-only sidecar
  continued to `295633` records after that publication. Strict offline replay
  resolves all eleven resources, presents nine draws / 1392 vertices, reports
  zero unmatched textures or unsupported primitives/arrays, and produces
  `reports/local/screenshots/fresh-final-flip-1013.bmp` with SHA-256
  `59DDE0E5B5AC5E7C1B2E2A09948F982C89ED1F1266F44E88B2ADDC82897042E5`.
  The replay shows the genuine `Load`, `New`, and `Don't Save` overlay plus the
  `Unable to load Burnout 2 Profile.` message on black. Frames `1003..1013` all
  clear color/depth/stencil and submit the same nine overlay draws; none binds
  any of the six available `512x512` background textures. The remaining black
  background is therefore upstream guest command generation, not a missing
  Vulkan texture upload or host composition draw; report v3 emits
  `black_clear_without_fullscreen_background_draw` with the exact flip, draw,
  texture, and bounds evidence. The manual presenter's only
  automatic readback was still flip `52`, so the report correctly retains
  `current_presented_flip_readback_missing`; the exact offline replay supplies
  the final-frame pixel evidence.
- A configured headless proof with the persistent save, dashboard, and cache
  roots ran both discovered guest threads for their complete three-million-step
  budgets with zero dynamic frontiers. It audited `60` recovered primitive-draw
  calls and captured `7323` render writes. The main guest thread sustained
  `277172` steps/s; its dominant measured costs were page invalidation
  (`5.041` seconds), native dispatch (`3.135` seconds), observer callbacks
  (`1.857` seconds), call handlers (`1.849` seconds), and dirty-page sync
  (`1.461` seconds). The secondary thread sustained `89060` steps/s and spent
  `14.776` seconds in call handlers, making it a separate performance target
  that can be optimized only by preserving the same guest-visible behavior.
  Lifted `DIV`/`IDIV` now reports divisor-zero and quotient-overflow conditions
  as structured guest arithmetic faults with the exact guest EIP, rather than
  allowing a host integer exception. This also made an intentionally
  misconfigured proof pinpoint its missing `u:\\` volume without obscuring the
  recovered rendering progress.
- The July 14 performance pass preserves the recovered instruction stream and
  all guest-visible boundaries while removing host-side work. Native page
  coherence now uses exact changed/dirty page worklists and byte ranges; an
  adjacent A/B with the same generated DLLs reduced main invalidation scans
  from `13,398,525` to `22,238` and dirty scans from `13,401,194` to `16,927`,
  with the same `3,711` invalidations, `16,927` writebacks, guest steps, ABI
  calls, and render writes. A native address hash dispatcher then kept
  cross-module transitions inside C++: the final ten-million-step live proof
  performed the same `582,722` module calls through only `17,253`
  Python-visible dispatches. Exact-range overlay commit, preindexed runtime ABI
  metadata, direct semantic-safe u32 overlay access, and validated PCM caching
  complete the hot path.
- `reports/local/performance/native-60fps-proof-live-10m.json` is the final
  bounded proof. The main guest executes `10,000,000` instructions in `7.564`
  seconds (`1,322,044` steps/s), publishes all `404` completed flips and
  `42,264` render writes, and measures `80.356` guest flips/s over its most
  recent 120 publications. The Vulkan presenter remains paced at 16 ms, strict
  render validation passes, the music cache reports one validated hit and zero
  decode errors, and the run has zero dynamic frontiers. Its accuracy counters
  match the pre-optimization ten-million-step proof exactly: `582,722` module
  calls, `13,237` handlers, `49,513` dirty-page writebacks / `26,333,527` exact
  bytes, `12,318` invalidated pages, and `42,264` render writes. The 60 FPS
  console target therefore has measured steady-state headroom rather than a
  reduced-work approximation.
- The same 448.355-second ledger contains 7,310 reloads and 28,525 presented
  frames. Presenter reload work consumed 75.757 seconds: interpretation was the
  largest measured stage at 40.236 seconds, followed by command recording at
  17.449 seconds, resource update at 14.067 seconds, command loading at 3.761
  seconds, and fence wait at 0.118 seconds. Reload p95 was 14.945 ms and 12.126%
  of frames exceeded 16 ms. Corrected diagnostics also identify `6,070`
  redundant same-flip reloads; flips `1388` and `2314` alone accounted for
  `4,125` reloads and most presenter reload time. Manifest-only completed-flip
  publication eliminates that work instead of skipping guest rendering.
- Live command ingestion keeps geometric capacity slack and advances a
  persistent interpreter through the exact appended range. The former
  exact-size reserve relocated a million-record history after small appends;
  the later heuristic tail replay still rescanned and approximated state. Both
  costs are now removed without reducing the command set. Detailed
  draw/program/vertex diagnostics remain sampled every 120 reloads, while
  validation counters, completed-flip checks, and strict failures remain
  exhaustive.
- The fresh 117.775-second presenter ledger contains 925 reloads. Reload work
  consumed 12.667 seconds (`10.75%` of elapsed time), led by command loading at
  10.628 seconds / 11.490 ms average / 11.665 ms p95; resource update consumed
  1.392 seconds, command recording 0.431 seconds, and interpretation 0.185
  seconds. Five same-flip reloads remained and 5.44% of 7312 frames exceeded the
  16 ms target. The live sidecar was already held open; the measured load cost
  instead included a separate heap allocation for every 16-byte command record.
  `RecoveredD3DCommand` now stores its at-most-eight payload bytes inline while
  preserving every command, byte, and ordering. A rebuilt strict replay of the
  295642-record final snapshot produced the identical
  `59DDE0E5...97042E5` BMP, proving that the storage optimization did not change
  recomp or render semantics.
- Native performance summaries now rank handler hot paths by target address,
  callable name, call count, total time, maximum time, and average time. The
  render report traverses native runs and live-bridge metrics nested under every
  `guest_thread_execution`, so the secondary-thread call-handler cost and the
  bridge publication stages can no longer disappear from the aggregate. The
  one-million-step completed-flip proof recorded 55 handler targets; its leading
  target was `TitleAssetStreamOpenFastPath.read_handler`. Publishing 4,243
  command records and 28 completed flips cost `10.2` ms in append work and
  `20.9` ms in manifest work, with 92 post-flip records correctly excluded from
  the final presentable boundary.
- Resource publication now treats the resource-sidecar timestamp as consumed
  only after the matching manifest generation has been observed and loaded. The
  producer atomically replaces the resource sidecar before publishing its new
  manifest, so the presenter could previously pair the new sidecar timestamp
  with the old generation, retain ten textures, and never revisit the final
  eleven-texture Load/Save generation. The pending timestamp is now retried,
  preventing stale texture bindings without adding screen-specific placement or
  asset substitutions.
- Live presenter reloads now wait only for the single in-flight frame fence and
  retain already-created Vulkan pipelines by guest blend/color-mask state.
  Previously every guest publication drained the whole device, destroyed all
  pipelines, and synchronously recompiled them; local reload diagnostics placed
  that resource rebuild at roughly 18-21 ms before command recording, limiting
  the title/Load-Save presenter to about 20 FPS.

- The native live bridge advances the deterministic Xbox clock by one 60 Hz
  interval for every observed NV2A flip and now materializes the resulting
  `KeTickCount` value at its imported kernel-data address. The title helper at
  `0x000DFBE0` loads the `0x00293CBC` import thunk and dereferences its target;
  it never calls the registered HLE handler. Previously that target remained
  zero, so millions of guest instructions and more than 1,600 flips left the
  frontend entrance/load-save animation near the roughly `0.101` seconds
  accumulated by wait shims. A focused regression executes the same double
  dereference and observes the changing 10 ms tick value. A 30-million-step
  native verification advanced 2,193 flips and materialized tick `3665`
  (`36.65` seconds); the final title frame rendered normally because the held
  pre-title test input intentionally did not create a fresh Start edge.
- The presenter retains each draw's transform constants and executes captured
  NV2A vertex-program instructions for position, diffuse color, and stage-0 UV
  outputs. It also selects pipelines by the guest color-write mask, preserving
  alpha-only passes instead of writing them into RGB.
- The real title composition uses six persistent `512x512` DXT1 background
  resources plus a `256x128` DXT5 `b2logo` and a `256x256` DXT5 font atlas. The
  earlier resource scanner paired texture offsets and formats at individual
  register writes, transiently labeling a background address as the following
  DXT5 resource and truncating its snapshot. It now records complete bindings
  at actual `BEGIN_END` draw boundaries and retains them independently of the
  bounded `65536`-write diagnostic window. All eight title resources therefore
  remain available across later live reloads.
- Native Vulkan texture upload now decodes BC3/DXT5 alpha and color blocks in
  addition to DXT1. The corrected extended replay binds all eight title
  textures and produces
  `reports/local/screenshots/title-live-verify-18m.bmp`: the title background,
  Burnout 2 logo, and `Press START` atlas text all render correctly with no
  white fallback rectangle or full-frame white transition.
- The apparent post-title blocker was correlated with those missing textures
  and the one-shot host track, not stopped guest execution. An 18-million-step
  live verification reaches `1590` guest flips, `1718` native draws, eight
  matched textures, continued mode-`2` music polling, and zero dynamic
  frontiers; its final captured title frame remains valid.
- Corrected NV097 method decoding recovers `421` observed triangle-strip draws,
  `1684` inline vertices, per-vertex ARGB colors and UVs, and two stage-0
  `512x512` DXT1 bindings across the merged early/late capture. Delayed capture
  can now skip an arbitrary write prefix, retain prior state history for resource
  discovery, and merge with the earlier state-setting window.
- The Windows/Vulkan first-frame smoke now compiles dedicated inline-vertex
  shaders, creates a graphics pipeline, vertex buffer, sampled images, samplers,
  descriptors, and records native `vkCmdDraw` work from recovered draws. DXT1
  data is CPU-decoded from the guest upload's observed linear, row-major block
  order before upload. The HLE text rectangles are suppressed whenever native
  draws are present.
- A five-million-step alias-aware capture creates `421` draws, `1684` vertices,
  and two `512x512` DXT1 resources, presents and reads back a `640x480` frame,
  and reports no HLE text rectangles. Surface `0x220C0080` now contains `32397`
  nonzero bytes with SHA-256
  `B571D8BD1D2D871D91B7E81DB697B898B767AB2EBEA8C95860196268EC6561DC`;
  surface `0x22080080` contains `31035` nonzero bytes with SHA-256
  `05E3E9514C7E723E166E5BD3902E52BF67849BD328CFF3B54FAB035700EC199C`.
  They contain more than `2200` distinct DXT1 blocks each, proving that guest
  surface population now reaches the NV2A-visible addresses rather than merely
  exposing allocation metadata.
- Native push-buffer interpretation now preserves MMIO/ring epochs and orders
  locally reordered payload writes by push-buffer address. A packet whose guest
  stores arrive as `B0`, `B4`, `BC`, `B8`, for example, is reconstructed from
  its address layout instead of split into truncated capture-order runs. The
  host and normalized decoder consequently agree at `11462` method packets and
  `26868` interpreted methods; the host sees `341` zero-count words (the
  normalized artifact sees `342`), and the selected draw binds recovered
  texture `0x22080080` instead of the white fallback.
- NV097 flip writes now delimit guest frames. The `421`-draw capture contains
  exactly `421` flips with one draw per completed frame, so native replay selects
  the latest completed frame rather than compositing the full animation history.
  The resulting readback contains `4819` colors and visibly renders the Acclaim
  surface (`reports/local/first-frame/frame-native-address-ordered-5m.bmp`).
  Address-ordered state recovery proves that the textured-draw vertex program
  computes `oPos = v0 * c0 + c1` with
  `c0=(1,1,0,1)`, `c1=(0,0,0,0)`, and passes `v9` directly to `oT0`. The
  Vulkan host now converts the resulting top-left screen-space Y coordinate to
  NDC with `2*y/height-1`, matching NV2A's positive-height Vulkan convention;
  the prior inverse expression rendered the logo upside down. Replaying the
  existing capture produces an upright `640x480` readback at
  `reports/local/first-frame/frame-native-y-fixed-5m.bmp` with SHA-256
  `6BA3FDA6EFA2C5EF2CAB2F16022F86FECAD19E31309E8872FCFC1EE922EB2A7C`.
  The remaining half-logo is present in the guest draw itself: all `368`
  textured splash draws submit `x=0..320` and `u=0.5..1.0`, including `360`
  draws on surface `0x220C0080` and the final `8` on `0x22080080`. The captured
  shader and pixel-stage state contains no position or texture-coordinate
  expansion that the host is omitting. Until the stalled splash/timing path is
  recovered, native presentation applies a narrowly guarded reconstruction
  only to a presented textured triangle strip whose bounds are exactly
  `x=0..width/2`, `u=0.5..1`, and `v=0..1`: X expands to the output width and U
  expands to the full texture. The guard activates for the current final draw
  and is reported as `presented_half_quad_recovered=true`; it does not rewrite
  ordinary geometry or arbitrary partial texture draws. The resulting upright,
  complete logo readback is
  `reports/local/first-frame/frame-native-full-logo-5m.bmp`, has `9698` colors,
  and SHA-256
  `B20063FB49FFB5E928234A6A7F062CCC9358DA50188D822120C8D6348ABE10B0`.
  Normal title/menu composition still needs pixel validation.
- The host supports deterministic swapchain readback with `--screenshot`;
  `tools/host/first_frame_smoke.py` writes and validates a 32-bit BMP and records
  its dimensions and SHA-256 digest. The normalized render artifact now also
  preserves the exact text observed at the title text-draw fast path. A
  recovered-stream run HLE-renders that text into the Vulkan render pass and
  captures a `640x480`, two-color frontend frame with `3608` white foreground
  pixels over the recovered black background
  (`reports/local/first-frame/frame.bmp`). The former dirty-disc stream-provider
  cause is cleared. The current path draws `Loading - please wait` exactly `53`
  times; that count is unchanged between the earlier and later extended
  checkpoints while D3D writes rise from `10739` to `32768`, frontend method
  calls rise from `158` to `299`, and runtime ABI calls rise from `4395` to
  `6087`. The automated `frontend_boot_gate.py` therefore classifies the guest
  path as `title_menu_frontend_loop_reached`. The replay still HLE-renders the
  last recovered text sample, so `frame-5m.bmp` remains a loading-label image
  rather than a pixel-validated native title/menu frame; that is now a render
  translation/capture limitation, not an active guest boot blocker.
- Render-stream decode still emits `visual_gap_inventory` and named/unnamed
  method histograms for the remaining state families. Array-backed/indexed
  geometry, additional primitive modes and texture formats, and fuller fixed
  function state remain deliberately trace-driven follow-ups.

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
- Feed decoded inline vertices, colors, UVs, texture bindings, and resource
  snapshots into native Vulkan buffers, sampled images, descriptors, and draws.
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

1. Run the normal live launcher once with report-v3 instrumentation to the
   stable Load/Save screen. Require the `0x000EE1B0` submission audit to match
   guest flip N to host flip N+1 and show `exact_completed_flip=true`; use its
   submitted bounds, texture-address set, text samples, and host bounds to trace
   why the frontend omits a background draw. Capture F12 on the final screen so
   the live report also has same-flip pixel evidence rather than relying on the
   exact offline replay.
2. Design a versioned save state that restores CPU state, sparse-memory dirty
   pages, runtime handle/object state, fast-path state, audio mode, and the
   live-host bridge atomically; reject mismatched XBE and recompiler versions.
3. Measure end-to-end input latency over a longer live menu run and add the
   main-menu capture to the strict pre-manual replay suite.
4. Compact or replace the 72 MB JSON dynamic-block cache before another large
   frontier audit; loading and deserializing it still dominates bounded-proof
   startup time.
5. Trace the first later frontend music-mode transition and validate that it
   stops or replaces the looping `trk07menu` request; sound-bank construction
   and DSP effect-image setup remain separate, non-playing lifecycle operations.
6. Validate the probe-only D3D context, descriptor, context-list, and
   submission-window models against decoded helpers around `0x00217570`,
   `0x0021AFD0`, `0x0021B1C0`, `0x0021AE80`, `0x00219370`, and `0x00222350`.
7. Add each newly stable gameplay capture to the strict pre-manual replay suite,
   then implement every unsupported array/indexed geometry, primitive, texture
   format, and fixed-function state family it inventories before visual testing.
8. Keep the probe-only heap, D3D, MMIO, list, and compare fast paths constrained
   while validating whether recovered helper semantics can replace them.
9. Keep adding regression tests only for observed title paths and ABI edges.

## Open Questions

- What determinism level is required for long-run gameplay testing and replay?
- Which recovered render packets should graduate from preserved artifacts to
  method-specific Vulkan work once gameplay command streams stabilize?
