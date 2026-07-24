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

The first retained ETW baseline found the guest thread consuming approximately
93% of one logical core while the Vulkan presenter sustained 58 presents per
second. That evidence directed the next CPU investigation toward guest/host
service boundaries rather than GPU presentation; it is not a gameplay FPS
distribution.

Earlier frontier diagnostics separated two costs: synchronous recovery caused
41-50 second frame stalls, while permanent interpretation caused excessive
native/interpreter and host-service crossings. Normal runs now interpret and
persist newly decoded frontiers for the next AOT build; background native
promotion requires `--developer-live-compile`. A controlled 2,161-instruction
frontier promoted off-thread in 3.505 seconds and then ran the expected 2,162
base/frontier steps natively.

After the P2 native control migration, a warm diagnostics-off embedded
handshake stopped cleanly in 10.377 seconds without changing the 277-module
cache. A separate 16.176-second diagnostic smoke recorded 1,510 cold calls
through the native service table, two worker entries, five lifecycle
transitions, and one native-recorded completion.

## Known limitations

- Guest simulation remains approximately 22-24 FPS in the sampled gameplay
  window; longer workload-locked frame-time distributions are still needed.
- There is no complete save-state system.
- Fifteen car draws use a second reflection/cubemap stage that is diagnosed but
  not yet replayed.
- Corrected gameplay and Load/Save captures still need promotion into the
  checked-in strict preflight suite.
- Longer un-audited runs are needed to characterize input/audio behavior.

Local reports under `reports/local/` are evidence, not source artifacts, and
must not be committed. Performance claims should cite their run identity,
configuration, cache state, sampling duration, and workload.
