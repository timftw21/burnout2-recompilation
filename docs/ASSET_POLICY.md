# Asset and proprietary-material policy

b2_recomp is source code and redistributable synthetic validation data. It is
not a game distribution, asset replacement, or Xbox SDK mirror.

## Never commit or distribute here

- Disc images, XBE files, extracted game files, audio, textures, fonts, video,
  screenshots, or other copyrighted game content.
- Xbox SDK headers, libraries, tools, symbols, documentation, or derived copies.
- Disassembly databases, memory dumps, recovered symbols, proprietary reports,
  generated source that embeds original code/data, or captured render streams.
- Patches or archives whose practical purpose is to reconstruct a substantial
  portion of those materials.

These materials belong only in ignored local paths such as `Burnout 2/`,
`data/local/`, `reports/local/`, and `build/`. The ignore rules are a safety
net, not permission to redistribute an unlisted proprietary format.

## Appropriate repository material

- Independently authored runtime, analysis, build, and test code.
- Small synthetic fixtures created specifically for this project and containing
  no original game bytes or recognizable assets.
- Facts needed for interoperability, including addresses, structure layouts,
  behavior descriptions, and cryptographic hashes used solely as provenance
  gates.
- Minimal test vectors with documented synthetic provenance.

Before committing, inspect staged files and run:

```powershell
git diff --cached --stat
git diff --cached --check
python .\tools\quality_gate.py --full
```

If a contribution may contain third-party material, leave it out until its
origin and license are documented. Suspected accidental inclusion should be
reported privately to the repository maintainer through the hosting platform;
stop distributing the affected revision until it is reviewed.
