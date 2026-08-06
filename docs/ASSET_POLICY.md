# Asset and proprietary-material policy

b2_recomp contains independently authored source, documentation, and synthetic
validation data. It is not a game distribution, asset replacement, or Xbox SDK
mirror.

## Never commit or distribute

- Disc images, XBE files, extracted game files, audio, textures, fonts, video,
  save data, or other reusable game assets.
- Xbox SDK headers, libraries, tools, symbols, documentation, or derived copies.
- Disassembly databases, memory dumps, recovered symbols, proprietary local
  reports, generated source that embeds original code/data, or captured render
  streams.
- Patches or archives whose practical purpose is to reconstruct a substantial
  portion of those materials.
- Screenshots or captures outside the narrow documentation exception below.

These materials belong only in ignored local paths such as `Burnout 2/`,
`data/local/`, `reports/local/`, and `build/`. Ignore rules are a safety net,
not permission to redistribute an unlisted proprietary format.

## Documentation screenshot exception

Small screenshots may be committed under `docs/images/` only when all of the
following are true:

- the repository maintainer supplied or explicitly approved the capture;
- it illustrates project progress, compatibility, or a user-facing workflow;
- it is a flattened, reasonably sized image rather than a reusable source asset
  or lossless capture bundle;
- the README or nearby documentation clearly identifies it as a development
  snapshot rather than evidence of a finished release; and
- no claim is made that the screenshot or depicted game content is covered by
  the repository's MIT license.

The approved README images `frontend-single-player.jpg` and
`lesson-one-start.jpg` are maintainer-supplied development captures. They are
included only for project documentation. Original game content, names,
trademarks, and imagery remain the property of their respective rights holders.

Do not add screenshots from local reports automatically. Every new image needs
an explicit review of provenance, purpose, size, and surrounding attribution.

## Appropriate repository material

- Independently authored runtime, analysis, build, documentation, and test code.
- Small synthetic fixtures created specifically for this project and containing
  no original game bytes or recognizable assets.
- Facts needed for interoperability, including addresses, structure layouts,
  behavior descriptions, and cryptographic hashes used as provenance gates.
- Minimal test vectors with documented synthetic provenance.
- The narrowly approved documentation screenshots described above.

## Before committing

Inspect the staged paths and contents, not just the ignore result:

```powershell
git diff --cached --stat
git diff --cached --check
python .\tools\dev_check.py --explain
```

Use the exhaustive closeout when the change affects runtime or release
artifacts:

```powershell
python .\tools\dev_check.py --launch-closeout
```

If material may contain third-party content outside this policy, leave it out
until its origin and repository purpose are documented. Suspected accidental
inclusion should be reported privately to the repository maintainer through the
hosting platform; stop distributing the affected revision until it is reviewed.
