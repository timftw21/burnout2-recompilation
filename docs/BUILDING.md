# Building and validation

The supported native development host is Windows x86-64. Python 3.11 or newer
drives the asset-free tools; all Python packages are exact-version locked in
`requirements.lock` and `requirements-dev.lock`.

The project is a work in progress. A successful build proves artifact and
toolchain consistency; it does not imply that the current Phase 7 static path
can complete the game. The latest normal-live artifact reaches Load/Save and
then fails closed on an unverified new-save indirect call.

## Exact native inputs

`tools/native_toolchain.lock.json` pins CMake 4.4.2, Ninja 1.13.0, Clang
22.1.8 targeting `x86_64-pc-windows-msvc`, Vulkan SDK 1.4.357.0 (header version
357), and SDL3 3.4.14. It also pins SHA-256 identities for the compiler, shader
compiler, Vulkan/SDL headers, import libraries, and SDL runtime.

Validate the installed build tools alone:

```powershell
python .\tools\native_toolchain.py --build-tools-only --pretty
```

Validate the build tools and compiler required by asset-free AOT/native tests
without requiring the presenter SDK:

```powershell
python .\tools\native_toolchain.py --aot-tools-only --pretty
```

Validate the complete presenter toolchain selected by `VULKAN_SDK`:

```powershell
python .\tools\native_toolchain.py --presenter-tools-only --pretty
```

`tools/native_build.py` performs the applicable validation automatically and
fails before configuration on any revision or artifact mismatch. Update the
lock only as an intentional toolchain change, review every new hash, and run
all presets plus the strict presenter build.

The optional compiler launcher is independently locked in
`tools/compiler_cache.lock.json`. Install its exact archive once with:

```powershell
python .\tools\compiler_cache.py install
```

Native builds use that sccache installation automatically when present. Pass
`--compiler-cache required` in reproducible/CI builds or `off` while diagnosing
the launcher. Cache data stays under `build/cache/sccache`; per-run JSON
telemetry under `build/local/compiler-cache/` reports hits, misses, hit rate,
compiled source count, and source bytes.

## Commands

For the ordinary edit/debug loop, run the changed-file-aware gate:

```powershell
python .\tools\dev_check.py --explain
```

It maps the current Git diff to explicit Python test modules, static checks,
CMake targets, and CTest labels. Independent checks run concurrently; Python
tests are distributed by individual test case using retained local durations.
Successful nodes are reused only when their content, command, Python and native
tool identities, declared environment, dependency keys, and output identities
still match. Cache hits and misses are printed. Use `--dry-run --explain` to
inspect selection without running it, `--path <path>` to diagnose a specific
file, and `--all` to select the complete asset-free base gate:

```powershell
python .\tools\dev_check.py --dry-run --explain
python .\tools\dev_check.py --path tools/recomp/x86_lifter.py --explain
python .\tools\dev_check.py --all
python .\tools\dev_check.py --launch-closeout
```

Local cache and timing state is confined to `build/local/dev-check/`.
`--no-cache` forces execution without reading or updating the node cache.
Every node has an enforced wall-time budget; the history stores bounded p50 and
p95 samples and flags material regressions. Python shard stdout is buffered and
shown only for failures.

`--launch-closeout` runs the normal changed/subsystem selection first and, only
after it passes, starts a detached matrix containing the complete asset-free
gate, debug/release/sanitizer native builds, and the strict release presenter.
The native presets run concurrently with bounded parallelism. A new launch
requests cooperative cancellation of an older local closeout on the same
branch. Inspect a run without waiting for it:

```powershell
python .\tools\validation_closeout.py launch --jobs 6
python .\tools\validation_closeout.py status .\reports\local\validation\closeout\<run>
```

Pass one or more manually captured `--replay-capsule` paths to put long,
proprietary deterministic replays in that integration boundary. The tool does
not capture input or create title data. CI hard-cancels superseded revisions,
shards Python cases, restores compiler/representative-AOT/ThinLTO artifacts,
and retains compact timing and failure evidence.

Test budgets and the separate repeat-only quarantine live in
`tools/test_runtime_budgets.json`. Ordinary gates exclude entries in
`repeat_only`; run them deliberately with
`python .\tools\python_test_shards.py --repeat-quarantine <count>`. Declared
`shared_fixture_groups` keep tests using one expensive deterministic fixture in
one worker instead of rebuilding it in several shards.

On the first node or test failure, the gate prints a one-command focused rerun,
the deterministic seed, temporary artifact location, input/cache identity, and
last successful prerequisite. It also writes a bounded capsule under
`reports/local/validation/failures/`; only the newest eight capsules and at
most 64 MiB are retained.

Install dependencies and run the blocking complete asset-free/debug gate when
an immediate foreground result is specifically needed:

```powershell
python -m pip install --requirement .\requirements-dev.lock
python .\tools\quality_gate.py --full
```

`quality_gate.py` is the compatibility entry point for `dev_check.py --all`.
It uses the same exact-key cache and case-level sharding; add `--no-cache` when
a deliberately cold authoritative rerun is required.

Build individual native configurations:

```powershell
python .\tools\native_build.py --preset debug
python .\tools\native_build.py --preset release
python .\tools\native_build.py --preset profiling
python .\tools\native_build.py --preset sanitizer
python .\tools\native_build.py --preset release --presenter
python .\tools\native_build.py --preset debug --target b2r_host_core_tests --test-regex '^native\.host_core\.dirty_range_lifetime$' --parallel 8
python .\tools\native_build.py --preset debug --target b2r_host_core_tests --label texture --label pipeline
python .\tools\native_build.py --preset debug --configure-only
python .\tools\native_build.py --preset debug --target b2r_host_core_tests --build-only
```

`--target`, `--test-regex`, and `--label` are repeatable; repeated filters are
ORed. The feature labels are `renderer`, `transport`, `texture`, `pipeline`,
and `nv2a`. The default path configures, invokes Ninja only for the selected
targets, and runs only the selected CTest cases. `--configure-only` stops after
CMake generation; `--build-only` assumes that generation already happened and
does not run CTest. The validation graph uses these options after its single
pinned-toolchain node, avoiding duplicate toolchain validation.

Emitter/executor edits use the bounded asset-free AOT corpus:

```powershell
python .\tools\recomp\representative_aot.py
```

It covers the major generated-helper and instruction families and reuses the
same content-addressed module, object, sccache, and ThinLTO caches as title AOT
preparation. Use it for iteration. Complete decoded-store preparation belongs
only in explicit preflight/full validation before a gameplay handoff.

## Building the Phase 7 normal-live artifact

Normal-live generation is an ahead-of-time operation over local retained
evidence. It requires:

- the exact supported XBE;
- the matching version-3 decoded block store;
- a manually captured replay capsule used only as deterministic evidence; and
- a closed normal-validation coverage profile.

The retained local development command is:

```powershell
python -m tools.recomp.ia32_native_backend build-normal-live `
  .\reports\local\replay\lesson-one-phase7-observed-run-4.b2rcap `
  --xbe .\data\local\extracted\burnout_2_poi_usa\default.xbe `
  --decoded-block-store .\build\native-guest-loop\decoded-blocks.sqlite3 `
  --coverage-profile .\reports\local\replay\boot-to-lesson-one-coverage-profile.json `
  --build-dir .\build\local\ia32-live `
  --report .\reports\local\replay\boot-to-lesson-one-normal-live-build.json
```

Those capsule/profile paths are proprietary ignored inputs, not repository
fixtures. A successful build writes a content-addressed artifact below
`build/local/ia32-live/<artifact-id>/` and refreshes the stable local index at
`build/local/ia32-live/manifest.json`.

Before launching, inspect the manifest and indirect-target table. A usable
static-clean artifact must report:

- `normal_execution_eligible: true` and `coverage_closed: true`;
- zero unknown targets, pending host services, frontier invocations/steps,
  cross-backend exits, and Python runtime callbacks; and
- runtime compilation, decoding, code patching, native promotion, and raw-XBE
  execution disabled.

Launch the exact artifact explicitly:

```powershell
python .\tools\playability\live_test.py `
  --guest-backend same-isa-ia32 `
  --ia32-artifact .\build\local\ia32-live\manifest.json `
  --skip-host-build
```

Unknown executable targets are coverage gaps. Diagnose the guarded address,
recover a finite evidence-backed family when possible, rebuild, and rerun.
Never make `--developer-live-compile`, an interpreter, or automatic backend
fallback part of the normal build/launch loop.

## Replay-oriented iteration

Create and validate the asset-free replay checkpoint without building the
presenter or starting the title:

```powershell
python .\tools\playability\replay_capsule.py synthetic `
  --output .\build\local\replay\synthetic.b2rcap
python .\tools\playability\differential_replay.py execute `
  .\build\local\replay\synthetic.b2rcap `
  --experimental interpreter `
  --max-steps 4 `
  --report .\build\local\replay\synthetic-differential.json
```

`dev_check.py --all` contains equivalent cached `capsule_synthetic` and
`differential_synthetic` nodes. Real checkpoints are produced only after a
manual gameplay capture and must stay below `reports/local/` or `data/local/`;
the tools never drive controller input. Native differential replay is an
explicit local diagnostic build, preserves its generated PDBs, and reuses the
content-addressed object and ThinLTO caches.

Generated AOT source always receives `native-debug-index.json`. PDB retention
is opt-in for diagnostic/profiling executors so normal title preparation does
not multiply the generated-artifact footprint. The module manifest owns and
prunes the related `.cpp`, `.obj`, `.dll`, `.pdb`, and `.debug.json` files as a
single cache entry.

Debug, release, and sanitizer CTest presets run in Windows CI with the pinned
compiler cache restored by compiler/target/flags/lock/source identity. The
Python CI job runs the same default `tools/quality_gate.py` command used by
pre-commit.
The `--full` local form adds the debug native build and tests.

Generated artifacts belong under `build/` and `reports/local/`. Inspect and
prune them only through the dry-run-first maintenance commands:

```powershell
python .\tools\project_maintenance.py status --pretty
python .\tools\project_maintenance.py prune --pretty
python .\tools\project_maintenance.py clean-reports --pretty
python .\tools\project_maintenance.py clean --pretty
```

`prune`, `clean-reports`, and `clean` preview their changes by default. Add
`--apply` only after reviewing the JSON plan. Retention defaults to 30 days,
2,000 report files, 2 GiB of reports, 2,048 native artifacts, and 4 GiB of
native artifacts. `clean-reports` manages only `reports/local/`; `clean`
manages that directory and `build/`. The proprietary `data/local/` tree is
never a deletion target.

For initial dependency installation, extraction, target verification, and an
ordinary launch, see [GETTING_STARTED.md](GETTING_STARTED.md).
