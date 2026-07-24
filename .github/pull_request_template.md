## Change

Describe the behavior boundary and why the change is needed.

## Evidence

State the guest-semantic evidence, supported target, or reproducible issue.

## Validation

- [ ] `python tools/quality_gate.py --full`
- [ ] `python tools/native_build.py --preset release --presenter` when presenter code changed
- [ ] Relevant profiling/sanitizer/manual validation recorded
- [ ] No proprietary assets, SDK content, generated guest code, or local reports included
- [ ] Focused documentation updated; README.md/Audit.md changed only when their claims changed

## Risk

Describe remaining risk, compatibility assumptions, and follow-up work.
