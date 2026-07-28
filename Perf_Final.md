# Performance Capture Final

## Capture scope

This report covers capture `05517994-cd61-4ac1-a4a9-848acc990d36` from July 27, 2026 (local time). It correlates the run manifest, performance report, render report, native probe, render event stream, and all four hot-path tables. The run completed cleanly on the supported Burnout 2 target with a warm cache, profiling enabled, and no developer live compilation.

The profile is suitable for ranking work: target and transition counts are exact, no transitions overflowed or went unclassified, all 2,396 render validations passed, the event log has no invalid records, and shutdown was clean. It is also static/native-clean: Python runtime callbacks, frontier-interpreter invocations/steps, native promotion, and runtime compile events were all zero.

The four auxiliary text logs predate the capture. An initial native-executor test run had two module-count assertion failures; the subsequent `test-native-executor-fixed` run passed all 111 tests, so no unresolved auxiliary test failure remains. The capture manifest does not provide a separate runner log.

## Executive result

- Guest production is the primary limit: 716 new guest flips in 39.46 seconds, or **18.14 flips/s**.
- The presenter displayed 2,161 frames in the same window, or **54.76 frames/s**. Its frame-time p50 was 16.712 ms, p95 was 34.499 ms, and 52.3% of displayed frames missed 16.667 ms.
- Sampled guest-module plus host-service work is **27.138 ms per guest flip**, 162.8% of a 60 Hz frame budget. On this model, at least **10.472 ms/flip (38.6%)** must be removed to fit 16.667 ms.
- Profiling bookkeeping itself is estimated at **32.3% of capture wall time**. Therefore 18.14 flips/s is an instrumented result, not the diagnostics-off baseline; use the per-target rankings and paired before/after captures to judge optimizations.
- The workload is broad. The top target accounts for 5.96% of sampled guest-module time, the top 10 for 20.03%, and the top 25 for 35.73%. Optimizing isolated functions will not be enough without systemic AOT/codegen and presenter improvements.

## Guest execution hot paths

The capture executed 1.596 billion guest steps and 38.96 million native-module calls: about **2.229 million steps and 54,410 module calls per guest flip**. The primary lane owns 96.57% of steps; the worker lane is only 3.42% and vblank is negligible.

| Rank | Guest target | Native time/flip | Module-time share | Diagnostic signature |
| ---: | --- | ---: | ---: | --- |
| 1 | `0x000EE140` | 1.608 ms | 5.96% | 888,743 one-step calls; expensive short block |
| 2 | `0x0008D1E3` | 0.525 ms | 1.94% | 40.9M steps; high-fan-out long block |
| 3 | `0x00214ED0` | 0.466 ms | 1.73% | 1.06M calls and 48 exits; dispatch hub |
| 4 | `0x0021F9C0` | 0.448 ms | 1.66% | 44.0M steps; long block |
| 5 | `0x002150C0` | 0.437 ms | 1.62% | 16.4M steps; high cost per step |
| 6 | `0x0021976D` | 0.414 ms | 1.53% | 219,846 near-one-step calls; expensive short block |
| 7 | `0x000ED570` | 0.406 ms | 1.51% | 26.3M steps and 129 exits |
| 8 | `0x00214F30` | 0.385 ms | 1.43% | 396,173 calls; high cost per step |
| 9 | `0x00215100` | 0.375 ms | 1.39% | 839,050 calls and 41 exits; dispatch hub |
| 10 | `0x0022784C` | 0.342 ms | 1.27% | 38.7M steps; long block |

All ten have high timing confidence. A single giant strongly connected component (`cycle 4`) contains 8,950 targets and **95.02% of sampled guest-module time**. Its hottest deterministic edge chain is:

`0x000C5550 -> 0x00215100 -> 0x000C5561 -> 0x000EE140 -> 0x000C5570`

The first three edges each execute about 653,000 times during the capture; `0x000EE140 -> 0x000C5570` executes 443,809 times. This is the clearest evidence for reducing cross-module call/return overhead through ahead-of-time fusion or inlining. `0x0008E540 -> 0x00214ED0` is another high-value edge at 308,785 transitions.

Host ABI services are not the cause: all 897,497 service calls total an estimated 0.148 ms/flip, under 0.9% of the frame budget. Dirty synchronization consumes 0.285% of session time, and there are no Python memory callbacks.

## Presenter hot paths

Presenter reload is the second material path. It averages **19.699 ms per guest reload**, p95 is 24.195 ms, and it is busy for 35.74% of the capture. `live_reload` is the dominant boundary for every one of the 152 frames slower than twice the target budget. The stage values below are nested and must not be added together.

- Command interpretation: 10.018 ms/reload, including 8.976 ms of method interpretation. The presenter interprets 131.4 million methods, about 183,500 per reload; 28.84% still use the scalar path.
- Source residency/command loading: 4.683 ms/reload despite shared-memory transport and complete payload reuse.
- Resource update: 3.660 ms/reload, dominated by 3.561 ms of native resource creation/preparation.
- Push-buffer collection: 3.207 ms/reload; method apply and finalize are 3.476 ms and 2.228 ms respectively.
- GPU submission and present average only 0.041 ms and 0.034 ms per displayed frame. Pipeline creation during the window is negligible. This is a CPU command-processing problem, not a GPU-present bottleneck.

Existing reuse mechanisms are working but incomplete: raw resource uploads avoid 93% of compared bytes, texture binding reuse is 92.46%, and offscreen-target reuse is 99.86%. However, the run still compares 1.32 GB of raw resources and uploads 686.97 MB of index data during the profile window.

## Optimization roadmap

1. **Establish a trustworthy performance gate.** Record a diagnostics-off run of the same fixed scene alongside a short profiled run. Track guest flips/s, displayed frames/s, p50/p95 frame time, sampled native ms/flip, module calls/flip, and reload-stage timings. Require two repeat runs within 5% before accepting a gain. Separately reduce profiler overhead with per-thread preallocated buffers and a two-pass mode (exact counts first, sampled timing second); this improves diagnostics, not gameplay.

2. **Reduce AOT dispatch and guest-state overhead.** Inspect emitted native code for the ten targets above and fuse the nearly deterministic hot edges into AOT superblocks. Inline static call/return pairs, aggregate step accounting per native block, keep guest registers/flags resident across fused blocks, and hoist repeated memory-page/address checks where proven safe. Preserve scheduler yields and explicit host ABI boundaries. The first milestone is a material drop from 54,410 module calls/flip without changing exact guest results.

3. **Optimize both short and long guest blocks.** For `0x000EE140` and `0x0021976D`, remove helper/dispatch work that dominates their one-step bodies. For `0x0008D1E3`, `0x0021F9C0`, `0x002150C0`, `0x000ED570`, `0x00214F30`, and `0x0022784C`, optimize generated loops, registerize context, coalesce memory operations, and use SIMD only where guest semantics are exact. Specialize the high-fan-out hubs `0x00214ED0` and `0x00215100` after measuring their dominant exits.

4. **Cache presenter command work across flips.** Cache decoded command spans by epoch/hash, batch recurring scalar methods, and apply only changed render state. Raise the 71.16% bulk-method ratio and avoid reinterpreting roughly 183,500 methods per reload. Split source-residency timing into wait/copy/validation components, then eliminate full-span copies or validation when immutable shared-memory provenance already proves reuse.

5. **Make resource updates genuinely incremental.** Persist index buffers and native resource descriptors, consume dirty ranges instead of scanning unchanged resources, and cache materialized vertex/state products. Focus on the 686.97 MB of repeated index uploads and the 1.32 GB comparison pass. Keep the existing GPU texture conversion and offscreen-target reuse paths; they are not current priorities.

6. **Defer low-return work.** Do not prioritize host services, GPU submit/present, pipeline creation, vblank, or dirty synchronization until guest and reload costs fall substantially. Together they cannot close the present gap.

7. **Profile guest preparation and compilation separately.** This was a warm run with 1,620 cached native modules (1.17 GB), a populated decoded block store and Vulkan cache, and zero compile events, so it cannot diagnose cold preparation or AOT build time. Add phase timers for decoded-store loading/scanning, provenance validation, lifting/code generation, translation-unit compilation, linking, module-manifest loading, and presenter build checks. Compare cold, warm, and no-op rebuilds; then use content-addressed module/shard reuse, parallel compilation, and incremental manifest queries where the timings justify them. Keep all compilation ahead of normal execution—no runtime/JIT fallback.

## Exit criteria

- Diagnostics-off guest production is at least 60 flips/s and displayed output sustains 60 frames/s for the fixed workload.
- Sampled guest plus service work is below 16.667 ms/flip with headroom; presenter reload p95 is also below 16.667 ms.
- Python callbacks, frontier execution, native promotion, and runtime compilation remain zero.
- Render validation remains clean and output matches the supported target deterministically.
