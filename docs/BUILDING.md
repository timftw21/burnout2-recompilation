# Building and validation

The supported native development host is Windows x86-64. Python 3.11 or newer
drives the asset-free tools; all Python packages are exact-version locked in
`requirements.lock` and `requirements-dev.lock`.

## Exact native inputs

`tools/native_toolchain.lock.json` pins CMake 4.4.0, Ninja 1.13.0, Clang
22.1.8 targeting `x86_64-pc-windows-msvc`, Vulkan SDK 1.4.341.1 (header version
341), and SDL3 3.4.0. It also pins SHA-256 identities for the compiler, shader
compiler, Vulkan/SDL headers, import libraries, and SDL runtime.

Validate the installed build tools alone:

```powershell
python .\tools\native_toolchain.py --build-tools-only --pretty
```

Validate the complete presenter toolchain selected by `VULKAN_SDK`:

```powershell
python .\tools\native_toolchain.py --presenter-tools-only --pretty
```

`tools/native_build.py` performs the applicable validation automatically and
fails before configuration on any revision or artifact mismatch. Update the
lock only as an intentional toolchain change, review every new hash, and run
all presets plus the strict presenter build.

## Commands

Install dependencies and run the complete asset-free gate:

```powershell
python -m pip install --requirement .\requirements-dev.lock
python .\tools\quality_gate.py --full
```

Build individual native configurations:

```powershell
python .\tools\native_build.py --preset debug
python .\tools\native_build.py --preset release
python .\tools\native_build.py --preset profiling
python .\tools\native_build.py --preset sanitizer
python .\tools\native_build.py --preset release --presenter
```

Debug, release, and sanitizer CTest presets run in Windows CI. The Python CI
job runs the same default `tools/quality_gate.py` command used by pre-commit.
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
