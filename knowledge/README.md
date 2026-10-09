# Burnout 2 preserved reverse engineering

This is the research foundation for a fresh Xbox recompilation. It preserves
recovered game addresses, object layouts, service contracts, hardware behavior,
and processor, graphics, texture, and audio rules from the previous project.

The extracted material contains 649 records, 130 service contracts, and 326
focused source excerpts. The old runtime, generator, launcher, build setup,
test framework, and synthetic frontend repairs have been removed from the
working tree. These references are research material, not a runnable game.

| File | Preserved knowledge |
| --- | --- |
| [facts.json](facts.json) | Title addresses and object tables, instruction signatures, device registers, executable constants, audio codec tables, service arguments, and guest structure layouts. |
| [supported_target.json](supported_target.json) | Exact accepted executable identity and section metadata. |
| [target_validation.txt](target_validation.txt) | Checks that tie recovered call targets, object methods, and callbacks to specific instructions and memory layouts. |
| [processor.txt](processor.txt) | Instruction decoding, arithmetic, flags, floating-point/SIMD behavior, and processor state conversion. |
| [executable.txt](executable.txt) | Xbox executable header, section, import, certificate, and thread-local data parsing. |
| [graphics.txt](graphics.txt) | GPU command interpretation, vertex formats/programs, fragment combiners, graphics state, and coordinate conversion. |
| [textures.txt](textures.txt) | Texture layouts, storage order, compression, color formats, and render-target rules. |
| [audio.txt](audio.txt) | RenderWare containers, streamed substreams, Xbox ADPCM, and guest audio formats. |
| [xbox_services.txt](xbox_services.txt) | Guest file information, input/time service models, audio voice state, and Xbox cryptographic layouts. |
| [evidence.txt](evidence.txt) | Selected historical observations and existing corner-case examples, without their test harness. |
| [manifest.json](manifest.json) | Original revision, source locations, and hashes for checking the extraction. |

Each excerpt identifies its original file, symbol, and line range. Old type and
function names provide context; they do not establish the new runtime's design.
Source models record the previous project's understanding and are not independent
proof of original-game correctness. Host-created callback targets and service
dispatch addresses are omitted from the corresponding data subsets.

The complete previous source remains in Git at
`ab1d9a7a83963ffc78aa7ee4748778d1a0abcd91`. The local game image and its original
disc provenance remain under `Burnout 2/`.

The decoded instruction database was local and is absent. Rebuild decoded
blocks from the matching executable and rediscover coverage as needed. The
preserved address maps and validated target families provide starting points.
Replay captures are excluded. The extracted executable must match the accepted
identity before applying these address-specific discoveries.
