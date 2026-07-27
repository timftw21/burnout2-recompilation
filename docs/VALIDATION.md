# Validation evidence

This document retains operational evidence and known limitations without
turning the project entry point into a run log. Unless a row says otherwise,
the evidence is from July 2026 and applies only to the exact supported XBE and
recorded build/run identities.

## Automated asset-free baseline

The latest July 23 closeout run passed 548 Python tests and 10 native CTest
cases. The native suite covers NV2A vertex-program behavior plus live-transport
seqlock/layout rules, texture layout and Morton unswizzling, dirty-range
ownership/merging, pipeline identity/cache hashing, and completed-flip FPS
sampling. Debug and strict release presenter builds passed locally; Windows CI
runs Python/synthetic validation and debug, release, and sanitizer native
presets.

The four-layer synthetic preflight creates ephemeral redistributable asset,
render-stream, rendered-frame, and guest-health fixtures. It does not read
proprietary local data.

## Rendering and runtime coverage

Lesson One renders the world, HUD, and player car at the intended chase-camera
scale. Scene-record, camera, transform, command, resource, and completed-flip
audits agree on the draw population. Strict frozen replays exercise indexed and
inline geometry, recovered vertex programs, compressed mipmapped textures,
sampler and fragment state, fog, alpha/blend/color-mask behavior, off-screen
feedback, both car-shadow layers, and the styled loading label and fade.

The resumable native executor reaches Lesson One with SDL3 frontend audio,
keyboard and SDL3 gamepad input, local storage, streamed track data, and
cooperative workers. The decoded-store-seeded IA-32 audit contains 66,194
reachable blocks and 392,161 instructions with no decoder gap in the observed
set.

Normal gameplay uses one process with resident native dispatch, native hot
services and worker state, versioned shared-memory control, a bounded command
ring, immutable resource slots, and a presenter loaded into that process.
Bounded/lossless audit paths remain isolated by design.

## Native migration acceptance

The initial July 25 warm normal-play run
`55d3d964-93d0-4494-a25b-c084333f02ff` completed in 23.855 seconds with 96
native cache hits, zero cache misses, and no compilation. The sustained render
acceptance run `e0884dd7-5a27-403f-88c6-a4be1263df12` then completed 2 million
instructions and 53 exact native command/resource publications in 13.725
seconds. The presenter acknowledged all 53 generations and performed 52
incremental reloads. Its final cumulative stream contained 11,349 source
records and 9,894 interpreted methods, with zero unknown or truncated packets;
all 53 render validations passed and the centered textured draw was matched.

The sustained run returned with normal-runtime failure code zero. Python
runtime ABI, handler, memory read/write, observer, and slice-yield callback
counts were all zero. The frontier interpreter recorded zero invocations and
zero steps; native promotion and developer live compilation were disabled.
This is the normal-play acceptance evidence for the completed Python runtime
migration, including high-frequency render transport.

### July 25 title-path regression repair

The manual loading/title captures found that normal native publication omitted
frontend-text metadata and discarded all but the final stage-zero texture
binding. Native focused coverage now verifies the `Loading - please wait`
manifest fields and two sequential texture resources in one frame without a
Python handler. The renderer report also rejects presented textured draws whose
resource is missing. A post-repair title generation published four draws and
four texture resources with zero unmatched presented texture draws.

The title confirm path originally failed on the worker's unregistered
`NtQueryDirectoryFile` import at `0xE0000510`. The service is now native and has
a deterministic directory-enumeration regression test; manual play advanced to
Load/Save after the repair. Repeated volume probes then exposed an append-only
native file-table high-water mark: after 128 closed handles, `NtOpenFile("U:\\")`
failed and guest `idiv` at `0x000D9496` divided by the resulting zero allocation
unit. Native file registration now reuses inactive slots, with a 140-cycle
open/close regression test.

The next manual run passed Load/Save and stopped on the main-menu transition.
The live scheduler header retained phase 41 and target `0xE0000630`, identifying
the offline `PhyInitialize` shim rather than a render failure. Both
`PhyInitialize` and `PhyGetLinkState` now return success through native fixed
services with no Python callback. A diagnostic regression also verifies that
future missing native services report transport code 9 and the exact target
even when a render span is pending. Focused unit coverage passes; manual
validation confirmed the repaired main-menu transition. Rendering transport
and the title-to-Load/Save ABI boundary remain closed.

Run `41b28351-0200-4b33-a442-5a96dad024ea` reached first-level loading and then
remained there until the window was closed. The 1.598-billion-instruction run
had zero native-runtime failures, worker failures, Python callbacks, or asset
open failures and continued publishing valid frames. Its 22 native title-asset
opens transferred 50,296,036 bytes. The native migration had omitted the
existing Track PSS prelinked-image publication: level scene and stream tables
use absolute pointers into the image base encoded in each `.pss`, normally
`0x8381C000`, but native reads populated only the caller's temporary buffer.
The native service now validates the two descriptor tables and three
scene-record tables before publishing the exact PSS image at that encoded guest
base. All 88 native-executor tests pass, including native-only publication with
zero Python handler calls, and all 30 extracted Track PSS images pass structural
validation.

Manual run `c2607c60-4d55-4560-a305-8cc228b5a01c` then progressed past the old
hang and exposed an AOT coverage failure at `0x00111040` after 146,052,791 guest
instructions. The target is a valid address-taken level-loading callback called
indirectly from `0x00110CE2`, not a host service or invalid PSS pointer. Its
decoded block is now present in the supported image's SQLite store. Transport
code 9 handling also persists future executable failure targets after the
native run has stopped and labels the result
`native_normal_runtime_coverage_gap`; normal execution still performs no
interpretation, compilation, promotion, or Python callback. Manual first-level
entry remains the acceptance check.

Run `17e6fef4-d8b6-462c-8765-762681fd9b2c` advanced again and reached a second
address-taken callback, `0x00116DD0`, after 207,854,878 guest instructions. Its
post-run coverage result was `decoded_for_next_aot`, demonstrating that the new
code-9 persistence path worked. The report also retained two successful Track
PSS publications totaling 9,568,256 bytes and zero validation failures. Static
analysis places this callback in the exact table beginning at `0x00347100`.
Deterministic pre-run recovery now includes all 11 callbacks in that family,
and every member is present in the supported-image SQLite store. Manual
first-level entry remains open.

Bounded and audit launches that use separate guest/presenter processes now own
both children through a Windows kill-on-close job. This complements graceful
stop and forced timeout handling: if the launcher itself disappears, Windows
terminates the assigned child set instead of retaining orphaned recomp
processes. The job behavior has been verified with a real disposable process.

Manual run `fc0243ad-2d48-4288-8b02-b3192a859d8d` reached the first level's
How to Play card and then remained in a DirectSound cleanup poll until the user
closed the window. Render publication remained healthy for 1,539 guest flips,
the exact F12 snapshot passed the frozen render-debug suite, and input still
reached the guest. The final worker state identified the loop at `0x00107080`:
status continued to report playing after its stop command. The recovered
GetStatus method has two such internal states: active `0x0003` and
pending/allocated `0x8001`.

Run `84b2268a-9f6a-40af-98d5-3243c59cf15c` rebuilt the affected AOT module and
retained the same poll, while diagnostics showed the active-state-only fast
path was present but invoked zero times. This proves the observed voice was in
the second `0x8001` state rather than using a stale artifact. The native guard
now mirrors both exact GetStatus predicates, clears only pending bit `0x8000`
and playing bit `0x0002`, and resumes at `0x00107086` so the guest performs its
own status query and finalizer. Focused coverage exercises both states and the
already-stopped no-op case. The combined native-executor/playability suite
passes all 321 tests; manual progress beyond the card remains the acceptance
check.

The next first-run failure after How to Play decoded callback `0x00024440` and
then succeeded only after the following preparation generated its module. The
frontend-card constructor stores stack argument 7 as its later callback, while
its callers load that argument through `edi` across a branch family. Declared
register-stack ABI recovery now seeds all three proven callbacks
(`0x00024440`, `0x00025150`, and `0x00025A10`) during the pre-run AOT closure.
An extracted-image check confirms all three have static code boundaries, and
the asset-free branch-family regression recovers the complete set.

Run `4d57d5ae-4785-439b-8b5d-db0a2151792d` later stopped in level three with
transport code 9 at imported `NtReleaseSemaphore` (`0xE0000570`). This was not
a coverage gap: the service had already run natively, but the fixed 64-entry
semaphore table was append-only, `NtClose` did not retire entries, and creation
reported success even when registration overflowed. Native close now retires
semaphores, creation reuses inactive slots and reports allocation failure if
the genuinely-live set is full, and seeded handles advance the shared object
allocator. A 140-cycle create/close regression stays at one table slot with no
overflow; invalid or stale releases return `STATUS_INVALID_HANDLE` inside the
native ABI instead of being misreported as unknown code.

Run `08c4a35b-0ebb-4b67-a5d2-afe13725e117` then stopped while loading level
two with `native direct-write spans have no configured observer`. Normal native
gameplay intentionally has no Python render observer: a native-owned command
span was pending when an exact byte access fell through the native memory
handler into the generic Python callback and drain. Normal-runtime exact byte
reads and writes now have native narrow-access semantics, including PFIFO and
MCPX behavior, so the pending span reaches the next native scheduler service
without any Python memory or span callback. The failed process also remained
resident after its report completed; a full dump placed its sole thread in
`LdrShutdownProcess`, blocked in SDL while unloading `b2_presenter.dll` under
the loader lock. The embedded launcher now frees the presenter library before
Python finalization. A real one-frame SDL/Vulkan lifecycle returned zero and
completed the explicit unload, and the combined native-executor/presenter suite
passes all 164 tests.

The first create-save attempt stopped with transport code 9 at `0xE00007B0`.
Import resolution identifies that target as kernel ordinal 335, `XcSHAInit`:
the persistent `U:`/`T:` filesystem had opened correctly, but the normal native
service table did not own the title's save-authentication exports. The native
dispatcher now implements the exact guest-memory contracts for incremental
SHA-1, two-buffer Xbox HMAC, and stateful in-place RC4, including the Xbox SHA
context's 24-byte prefix and each export's real stdcall cleanup. The native
crypto vector, persistent save-file, and directory-enumeration regressions use
zero Python handlers. All 96 native-executor tests and all 236 playability-probe
tests pass; a manual create, restart, and load run remains the acceptance check.

The follow-up create-save run `c346bcb0-c85d-464c-a0af-69d67f457821`
advanced past the native crypto services and stopped with transport code 9 at
`0xE0000710`. The return address `0x000DFB9A` follows calls to
`KeQuerySystemTime` and kernel ordinal 305, `RtlTimeToTimeFields`; the normal
native table had left that timestamp conversion as a cold Python callback.
`RtlTimeToTimeFields` and its ordinal-304 inverse, `RtlTimeFieldsToTime`, now
run in the native dispatcher with their exact two-argument guest ABI. A native
regression converts a known 64-bit system time into the eight signed 16-bit Xbox
time fields, verifies milliseconds and weekday, round-trips it exactly, and
uses zero Python handlers.

Create-save run `47b70b13-be96-4c20-9c49-c93873500797` advanced through the
time conversion and opened `U:\` plus `SaveImage.xbx`, then stopped with
transport code 9 at `0xE0000670`. Import resolution maps that target to kernel
ordinal 260, `RtlAnsiStringToUnicodeString`; the exact title call at
`0x000E23CB` passes destination and source counted-string descriptors with
allocation disabled and returns at `0x000E23D1`. The adjacent inverse helper
calls ordinal 308, `RtlUnicodeStringToAnsiString`, at `0x000E2493`. Both
conversions now operate on guest `Length`/`MaximumLength`/`Buffer` descriptors
in the normal native runtime, use the USA target's fixed Windows-1252 mapping,
honor preallocated or service-allocated destinations, return NTSTATUS buffer
errors, and clean the exact three-argument ABI. The CP-1252 round-trip
regression uses zero Python handlers.

Create-save run `a5a703db-ed32-410e-9a70-a5d16cd8dd6f` completed without a
runtime failure and wrote a valid 54-byte `SaveMeta.xbx`, but never created the
`Profile 1` payload. Static analysis of the title's XAPI wrappers found that
retail Xbox `NtCreateFile` pushes nine arguments and returns with `ret 0x24`;
the runtime ABI override incorrectly modeled the 11-argument Windows form and
cleaned 44 bytes. The extra eight-byte cleanup made the metadata writer restore
`EBX` from UTF-16 metadata (`0x006D0061`) instead of the caller's creation
disposition, so the following profile open rejected its disposition before it
could reach the filesystem. The override and diagnostic bridge now use the
nine-argument Xbox contract. `STATUS_OBJECT_NAME_COLLISION` also maps to
`ERROR_ALREADY_EXISTS` (183), as required by `XCreateSaveGame` to recover the
valid metadata directory left by the failed attempt. All 622 Python unit tests
pass; manual create, restart, and load remains the acceptance check.

A later clean-profile startup exposed the cache initialization that precedes
title main: XAPI reads and updates the raw partition table through bare
`\Device\Harddisk0\Partition0`, assigns one of the three Xbox cache partitions,
formats the selected raw device, and dismounts it before continuing. The
previous zero-valued `HalDiskCachePartitionCount` export corrupted the XAPI
partition-table move, while bare cache devices and their geometry/control
operations still fell outside the native filesystem service. The normal native
runtime now reports three cache partitions, persists deterministic raw backing
files, supplies drive geometry and partition information, and handles the final
`FSCTL_DISMOUNT_VOLUME` flush. A bounded run against an empty cache root reached
the Vulkan presenter and remained active through the 50-second validation
window with no Python runtime callbacks; its exact Python/presenter process tree
was terminated and verified empty afterward.

Create-save run `fa842966-4bf1-494b-9c77-8606765cec05` wrote the expected
31,744-byte `Profile 1` payload, but the title immediately classified it as
unusable. Static analysis of the XAPI signature path found the remaining
failure: `XCalculateSignatureEnd` uses `XboxHDKey` for the console-bound
`NoCopy=1` layer, while the runtime exposed zero-filled key storage and the ABI
bridge materialized only integer data exports. The zero hard-drive key forced
XAPI into an unsupported whole-EEPROM query and left its temporary key buffer
undefined. The runtime now publishes a stable nonzero emulated hard-drive key
and materializes fixed-size byte exports without overlapping adjacent host
targets. The active XBE certificate supplies the title LAN, signature, and
alternate-signature keys in memory; diagnostics report only their sizes, not
their contents. All 624 Python unit tests pass, and both pre-fix save
directories were moved to recoverable local quarantine for a clean manual
create, restart, load, and overwrite check.

## Performance records

The July 14 presenter pacing regression completed 494 flips for 12,000,000
guest instructions at 59.900 presenter FPS. Its frame-time p50/p95/p99 was
16.647/17.048/17.216 ms; reload p50/p95/p99 was
0.775/1.026/1.305 ms, with 3.73% presenter reload busy time and a clean stop.

The controlled July 23 warm-cache, O2, profiling/diagnostics/overlay-off
gameplay baseline is run `a0e9bb76-365c-4319-a3fc-adbd233f333a`. Its F11
snapshot retained 23 completed guest flips over 1.001 seconds, or 22.986 guest
FPS. At that point the workload needed approximately 2.61x more guest
throughput to reach 60 FPS. A post-P1 warm sample retained 24 flips over 1.006
seconds, or 23.861 guest FPS. The approximately 3.8% difference is directional,
not a controlled distribution, because the one-second gameplay windows were
not workload-locked.

Manual run `70eae22e-991e-4449-81c8-b1a57ec848b9` exposed a pacing mismatch:
the presenter rendered 1,293 frames over 21.549 seconds at an average 16.666 ms
per frame, but consumed 2,435 guest publications. Reloading once at the frame
start and again while waiting for its deadline released almost two guest flips
per displayed frame, producing the observed roughly 120 FPS game rate. Normal
play now consumes and acknowledges only at the 60 Hz frame boundary. A manual
run remains required to accept the corrected guest rate.

The later native-only run `4d57d5ae-4785-439b-8b5d-db0a2151792d` exposed the
opposite edge of that policy: gameplay publications narrowly missed the single
loop-boundary probe and were acknowledged on the following boundary, producing
a stable two-presenter-frames-per-guest-flip cadence. Native normal play now
owns the same explicit 60 Hz completed-flip deadline as the diagnostic bridge,
and the depth-two presenter may consume one publication during the remaining
idle interval. Focused native/presenter regressions pass; a manual gameplay run
remains required to accept the corrected 60 Hz cadence.

The first retained ETW baseline found the guest thread consuming approximately
93% of one logical core while the Vulkan presenter sustained 58 presents per
second. That evidence directed the next CPU investigation toward guest/host
service boundaries rather than GPU presentation; it is not a gameplay FPS
distribution.

Earlier frontier diagnostics separated two costs: synchronous recovery caused
41-50 second frame stalls, while permanent interpretation caused excessive
native/interpreter and host-service crossings. Normal runs now reject unknown
targets as AOT coverage gaps. Explicit developer diagnostics may interpret and
persist a frontier for the next build; background native promotion requires
`--developer-live-compile`. A controlled 2,161-instruction
frontier promoted off-thread in 3.505 seconds and then ran the expected 2,162
base/frontier steps natively.

After the P2 native control migration, a warm diagnostics-off embedded
handshake stopped cleanly in 10.377 seconds without changing the 277-module
cache. A separate 16.176-second diagnostic smoke recorded 1,510 cold calls
through the native service table, two worker entries, five lifecycle
transitions, and one native-recorded completion.

### July 26 native VBlank and audio investigation

The `Loading - please wait` frame stall was traced to a missing host delivery
path for the callback registered through Xbox D3D at `0x00215880`. The title's
callback at `0x000B86C0` copies `D3DVBLANKDATA.VBlank` into `0x005518FC`; without
that write, the asynchronous asset wait loop pumps `0x000D5460` only once and
never reaches its read/finalize passes. The normal scheduler now invokes the
registered callback through a separate native AOT context with the exact
three-DWORD VBlank payload and wall-clock-paced 60 Hz cadence. It preserves the
primary context, has explicit completion/failure counters, and classifies an
uncompiled callback as transport code 9. No Python runtime callback, live
compilation, interpreter, or native promotion is involved.

The newly reachable `0x0010F9F0` target was then proven to be argument 2 of the
title compare/search callback consumer at `0x0010D150`. That ABI is now in the
declared stack-callback map, so deterministic AOT closure recovers it before a
normal run rather than discovering it at runtime.

The native audio boundary now parses the RenderWare `0x80D` header-declared
packet/substream layout, observes the reached DirectSound buffer and stream
methods, mirrors guest PCM16/Xbox ADPCM formats and payloads, mixes in a native
worker, and submits through the presenter's SDL3 C ABI. The earlier hard-coded
title special-effect and menu-track playback shortcuts were removed. PCM
normalization now rejects implausible sample rates and unbounded output sizes,
and the live control header reports the exact buffer `Play` stage and metadata.
Focused parser, buffer-ABI, buffer-create, stream, VBlank, live
transport, and static-callback recovery regressions pass.

Manual normal run `aadc8980-d227-4793-a39b-2d7df3515a86` still displayed a
stalled frame after 120 acknowledged publications. Its read-only live snapshot
recorded 6,264,249 primary steps, 103,821 worker steps, scheduler phase 5, and
EIP `0x0022D896` (`IDirectSoundBuffer::Play`). Audio stage 20 had already
rejected buffer `0x22664A48` at data `0x2266422C`: the payload was 64 bytes and
had sample rate zero. Therefore neither the decoder nor SDL device open was
blocking. The stall begins after the observer forwards into the original Xbox
DirectSound SDK body, while executing an AOT module. The next repair must make
`Play` a complete guest-visible host ABI boundary (including object/voice state,
HRESULT, and stack cleanup) or identify and implement the exact SDK hardware
wait it requires; simply suppressing the call would not be acceptable evidence.

Static recovery of the supported XBE closes the source-level ambiguity. Public
wrapper `0x0022D896` normalizes the interface pointer by `-0x1C`, calls
`0x0022C90E`, and returns with `ret 0x10`. The internal method loads its voice
from object offset `0x20`, which is public-buffer offset `+4`, then calls the
voice `Play` core at `0x00235136`. That core establishes allocated/playing
state through the 16-bit voice word at offset `0x12` before programming MCPX.
The native service now terminates at this proven boundary: it resolves that
voice, preserves unrelated state bits and adjacent bytes, sets allocated and
playing, clears the host-owned hardware-pending bit, returns `S_OK`, and applies
the declared 16-byte cleanup. Null buffers or missing voices return `E_FAIL`.
Host payload/decode/output failures remain bounded diagnostics and do not enter
the Xbox SDK hardware path or overwrite the guest-visible HRESULT.

The focused regressions deliberately place sentinel HRESULTs in compiled guest
SDK bodies and prove they are not observed. They also verify exact ESP cleanup,
the pending/allocated-to-allocated/playing voice transition, adjacent-byte
preservation, native buffer creation/mirror resolution, and zero Python handler
calls. All 103 native-executor tests pass. No gameplay process or automated
input was used; progression beyond retained run
`aadc8980-d227-4793-a39b-2d7df3515a86` remains a manual acceptance check.

## Known limitations

- The native pacing repair still needs a manual gameplay acceptance run and a
  longer workload-locked frame-time distribution.
- There is no complete save-state system.
- Fifteen car draws use a second reflection/cubemap stage that is diagnosed but
  not yet replayed.
- Corrected gameplay and Load/Save captures still need promotion into the
  checked-in strict preflight suite.
- The Xbox DirectSound buffer `Play` forwarding stall is source-closed, but its
  manual gameplay acceptance is pending. Sound-effect timing, gameplay music,
  and long-run stream continuity remain unverified.

Local reports under `reports/local/` are evidence, not source artifacts, and
must not be committed. Performance claims should cite their run identity,
configuration, cache state, sampling duration, and workload.
