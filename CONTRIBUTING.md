# Contributing to b2_recomp

Contributions should preserve guest-visible behavior, remain reproducible, and
be reviewable without proprietary assets.

## Before changing code

1. Read [README.md](README.md), [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md),
   and [docs/ASSET_POLICY.md](docs/ASSET_POLICY.md).
2. Install the exact dependencies with
   `python -m pip install --requirement requirements-dev.lock`.
3. Optionally install the repository hook with `python -m pre_commit install`.

Do not weaken target-provenance, strict-render, or artifact-confinement checks
to make a test pass. A compatibility repair must name its supported target and
include evidence and a focused regression.

## Change and commit discipline

- Keep one coherent behavior change per commit. Separate mechanical cleanup,
  generated metadata, implementation, and documentation when they can be
  reviewed independently.
- Use an imperative subject with an area prefix, such as
  `renderer: retain dirty upload spans`.
- Keep every commit buildable and asset-free-testable. Do not commit temporary
  instrumentation, local paths, performance captures, generated executors, or
  proprietary media.
- Describe the behavior before and after, the evidence for guest semantics, and
  the exact checks run. Performance claims need a run identity and workload;
  one-second samples must not be presented as distributions.
- Update the focused document that owns the changed behavior. Keep README.md to
  project status, first-run commands, and navigation; update it only when those
  entry-point facts change. Keep personal agent instructions and audit work
  logs outside source control.

## Required validation

During development, use the focused selector and review its explanation:

```powershell
python .\tools\dev_check.py --explain
```

Unknown source or build areas deliberately fan out to the complete Python
suite. `--all` bypasses changed-file selection, and `--no-cache` forces every
selected node to execute. Emitter changes run the bounded representative AOT
corpus during iteration; do not regenerate the complete decoded store until an
explicit preflight/full closeout.

Launch the exhaustive matrix before requesting review; it starts only after
the focused changed/subsystem gate passes and does not block further editing:

```powershell
python .\tools\dev_check.py --launch-closeout
```

The background closeout runs bytecode compilation, Ruff, strict type checking,
native build-tool validation, maintenance budgets, the synthetic four-layer
preflight, case-sharded Python tests, debug/release/sanitizer CTest, and the
strict release presenter through a budgeted dependency graph. Add manually
captured `--closeout-replay-capsule` inputs when the change requires long title
replay coverage. Cache reuse is explicit; use `quality_gate.py --full
--no-cache` only when a blocking cold debug result is specifically required.
Presenter changes still require the exact local SDK boundary used by the
closeout:

```powershell
python .\tools\native_build.py --preset release --presenter
```

Use the profiling and sanitizer presets when the change affects performance,
memory ownership, synchronization, or native lifetime. Manual game validation
supplements these gates; it never replaces them.

## Pull requests

Keep pull requests small enough to review by behavior boundary. State any
remaining risk and attach only redistributable evidence. By submitting a
contribution, you confirm that it is your work or that you have permission to
submit it under the repository license and asset policy.
