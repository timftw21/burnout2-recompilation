#!/usr/bin/env python3
"""Build a sanitized analysis database from Xbox XBE metadata."""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

try:
    from tools.loader.xbe_loader import load_xbe_bytes, load_xbe_file
except ModuleNotFoundError:  # pragma: no cover - direct script execution fallback
    import sys

    repo_root = Path(__file__).resolve().parents[2]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    from tools.loader.xbe_loader import load_xbe_bytes, load_xbe_file


SCHEMA_VERSION = 1

CONFIDENCE_LEVELS: dict[str, dict[str, str]] = {
    "confirmed": {
        "meaning": "Directly present in parsed XBE metadata or produced by project tooling.",
        "use_when": "Addresses, sizes, ordinals, section ranges, or library records are read from the executable metadata.",
    },
    "probable": {
        "meaning": "Strongly supported by metadata, but the exact code boundary or role still needs disassembly confirmation.",
        "use_when": "The XBE entry point or TLS callback address gives a function start, but function extent is unknown.",
    },
    "inferred": {
        "meaning": "Derived from section names, import names, library names, or Xbox API conventions.",
        "use_when": "Subsystem assignment or runtime role is likely, but source-level behavior has not been proven.",
    },
    "placeholder": {
        "meaning": "A required analysis target exists in the roadmap, but code analysis has not identified concrete sites yet.",
        "use_when": "Milestone tracking needs an explicit TODO without pretending the symbol is known.",
    },
}

NAMING_CONVENTIONS: dict[str, Any] = {
    "addresses": "Use uppercase eight-digit Xbox virtual addresses, for example 0x000E2555.",
    "functions": {
        "entry_point": "entry_start",
        "tls_callbacks": "tls_callback_<address_hex>",
        "discovered": "func_<subsystem>_<address_hex>",
        "import_thunks": "imp_kernel_<ordinal_decimal>_<export_name> or imp_<library>_<ordinal_decimal>",
    },
    "data": {
        "globals": "g_<subsystem>_<address_hex>",
        "tls": "g_tls_directory_<address_hex>",
        "sections": "section_<xbe_section_name>",
    },
    "vtables": {
        "pattern": "vtbl_<subsystem>_<address_hex>",
        "rule": "Only use vtbl_ after a pointer table has multiple confirmed code references.",
    },
    "subsystems": {
        "pattern": "Use stable lower_snake_case IDs from the subsystem taxonomy.",
        "boundary_rule": "Prefer Xbox API surface, library name, and data ownership evidence before assigning code to a gameplay subsystem.",
    },
    "assets": {
        "pattern": "asset_<kind>_<stable_name_or_address>",
        "rule": "Do not include original asset bytes, filenames from extracted media, or generated copyrighted data in committed databases.",
    },
}

SUBSYSTEM_TAXONOMY: dict[str, str] = {
    "startup": "Executable entry, TLS setup, process initialization, and first-call handoff.",
    "allocator": "Heap, pool, virtual memory, contiguous memory, and GPU memory allocation paths.",
    "filesystem": "File, directory, volume, symbolic-link, and low-level I/O paths.",
    "rendering": "Display mode, graphics memory, Direct3D, and frame presentation setup.",
    "audio": "DirectSound, streamed audio, codec, and speaker/output initialization.",
    "input": "Controller, polling, device state, and XAPI input entry points.",
    "main_loop": "The central tick/render/input loop once recovered from code analysis.",
    "threading": "Thread, queue, event, timer, semaphore, APC, interrupt, and synchronization calls.",
    "runtime": "Compiler/runtime support, string/memory helpers, XAPI, and CRT-like behavior.",
    "code": "Unclassified executable code sections.",
    "data": "Unclassified writable or read-only data sections.",
    "assets": "Embedded image/media/package sections visible only as metadata at this stage.",
    "networking": "Xbox network and online-service APIs.",
    "diagnostics": "Debug print, debugger, and diagnostic imports.",
    "crypto": "Hashing, RC4, HMAC, signing, and key handling imports.",
    "hardware": "HAL, EEPROM, display encoder, and hardware information imports.",
    "object_manager": "Kernel object manager imports.",
    "loader": "Section loading, unload, image, and executable loader behavior.",
    "unknown": "Metadata exists, but subsystem ownership is not known yet.",
}


def build_analysis_database_from_file(path: Path) -> dict[str, Any]:
    """Parse an XBE file and return a JSON-safe project analysis database."""

    loaded = load_xbe_file(path, resolve_imports=False)
    return build_analysis_database(
        loaded.info,
        loader_summary=loaded.summary(),
        source_name=path.name,
    )


def build_analysis_database_from_bytes(
    data: bytes, *, source_name: str | None = None
) -> dict[str, Any]:
    """Parse XBE bytes and return a JSON-safe project analysis database."""

    loaded = load_xbe_bytes(data, resolve_imports=False)
    return build_analysis_database(
        loaded.info,
        loader_summary=loaded.summary(),
        source_name=source_name,
    )


def build_analysis_database(
    info: dict[str, Any],
    *,
    loader_summary: dict[str, Any] | None = None,
    source_name: str | None = None,
) -> dict[str, Any]:
    """Create the first project-owned analysis database from parsed XBE metadata."""

    records_by_id: dict[str, dict[str, Any]] = {}
    _seed_memory_region_records(records_by_id, info, loader_summary)
    _seed_section_records(records_by_id, info)
    _seed_entry_records(records_by_id, info)
    _seed_tls_records(records_by_id, info)
    _seed_library_records(records_by_id, info)
    _seed_import_records(records_by_id, info)
    _seed_focus_area_records(records_by_id)

    records = sorted(records_by_id.values(), key=lambda item: item["id"])
    patterns = _build_compiler_runtime_patterns(info, records)
    subsystems = _build_subsystem_index(records)

    database = {
        "format": "b2-recomp-analysis-db",
        "schema_version": SCHEMA_VERSION,
        "public_safe": False,
        "source": _source_summary(info, source_name),
        "naming_conventions": NAMING_CONVENTIONS,
        "confidence_levels": CONFIDENCE_LEVELS,
        "subsystems": subsystems,
        "records": records,
        "compiler_runtime_patterns": patterns,
    }
    database["summary"] = _summary(database)
    return database


def _seed_memory_region_records(
    records_by_id: dict[str, dict[str, Any]],
    info: dict[str, Any],
    loader_summary: dict[str, Any] | None,
) -> None:
    if loader_summary:
        regions = loader_summary["mapped_regions"]
        source = "loader.summary.mapped_regions"
    else:
        regions = info["memory_map"]["regions"]
        source = "xbe.memory_map.regions"

    for region in regions:
        name = region["name"]
        address = region["virtual_address"]
        subsystem = "startup" if region["kind"] == "headers" else _section_subsystem(name)
        permissions = region.get("permissions")
        if permissions is None:
            permissions = _permissions_from_flags(region.get("flag_names", []))
        record_name = f"mem_{_symbol_token(name)}_{address:08X}".lower()
        _add_record(
            records_by_id,
            _record(
                "memory_region",
                record_name,
                address=address,
                size=region["size"] if "size" in region else region["virtual_size"],
                subsystem=subsystem,
                confidence="confirmed",
                source=source,
                display_name=name,
                tags=["memory_map", "loader_region"],
                notes=[
                    "Host-owned loader mapping for the XBE image; executable bytes are not stored in this database."
                ],
                evidence=[
                    {
                        "kind": region["kind"],
                        "index": region.get("index"),
                        "permissions": permissions,
                        "file_backed_size": region.get("file_backed_size"),
                        "zero_fill_size": region.get("zero_fill_size", 0),
                    }
                ],
            ),
        )


def _seed_section_records(
    records_by_id: dict[str, dict[str, Any]], info: dict[str, Any]
) -> None:
    for section in info["sections"]:
        name = section["name"]
        subsystem = _section_subsystem(name)
        _add_record(
            records_by_id,
            _record(
                "section",
                f"section_{_symbol_token(name)}",
                address=section["virtual_address"],
                size=section["virtual_size"],
                subsystem=subsystem,
                confidence="confirmed",
                source="xbe.sections",
                display_name=name,
                tags=["section", "xbe_metadata"],
                notes=[
                    "Section identity and range come from the XBE section table."
                ],
                evidence=[
                    {
                        "index": section["index"],
                        "flags": section["flag_names"],
                        "raw_size": section["raw_size"],
                        "zero_fill_size": section["zero_fill_size"],
                        "digest_matches": section["digest_verification"]["matches"],
                    }
                ],
            ),
        )


def _seed_entry_records(
    records_by_id: dict[str, dict[str, Any]], info: dict[str, Any]
) -> None:
    selected = info["header"]["entry_point"]["selected"]
    if not selected:
        return

    _add_record(
        records_by_id,
        _record(
            "function",
            "entry_start",
            address=selected["value"],
            subsystem="startup",
            confidence="probable",
            source="xbe.header.entry_point",
            tags=["entry_point", "startup", "needs_function_extent"],
            notes=[
                "The XBE encoded entry point resolves inside the image. Treat the start address as strong evidence, but recover the function extent through disassembly."
            ],
            evidence=[
                {
                    "decode_key": selected["key"],
                    "encoded": info["header"]["entry_point"]["encoded"],
                }
            ],
        ),
    )


def _seed_tls_records(
    records_by_id: dict[str, dict[str, Any]], info: dict[str, Any]
) -> None:
    tls = info["tls"]
    if not tls:
        return

    _add_record(
        records_by_id,
        _record(
            "data",
            f"g_tls_directory_{tls['virtual_address']:08X}".lower(),
            address=tls["virtual_address"],
            size=0x18,
            subsystem="startup",
            confidence="confirmed",
            source="xbe.tls",
            tags=["tls", "startup"],
            notes=["TLS directory metadata is present in the XBE header."],
            evidence=[
                {
                    "raw_data_start_address": _hex32(tls["raw_data_start_address"]),
                    "raw_data_end_address": _hex32(tls["raw_data_end_address"]),
                    "tls_index_address": _hex32(tls["tls_index_address"]),
                    "callback_table_address": _hex32(tls["callback_table_address"])
                    if tls["callback_table_address"]
                    else None,
                    "zero_fill_size": tls["zero_fill_size"],
                }
            ],
        ),
    )

    for address in tls["callback_addresses"]:
        _add_record(
            records_by_id,
            _record(
                "function",
                f"tls_callback_{address:08X}".lower(),
                address=address,
                subsystem="startup",
                confidence="probable",
                source="xbe.tls.callback_table",
                tags=["tls", "startup", "needs_function_extent"],
                notes=[
                    "TLS callback start address is metadata-derived; recover extent and order-specific behavior through disassembly."
                ],
                evidence=[
                    {
                        "tls_directory": _hex32(tls["virtual_address"]),
                        "callback_table_address": _hex32(tls["callback_table_address"]),
                    }
                ],
            ),
        )


def _seed_library_records(
    records_by_id: dict[str, dict[str, Any]], info: dict[str, Any]
) -> None:
    for library in info["libraries"]["versions"]:
        _add_library_record(records_by_id, library, "xbe.libraries.versions")

    for alias in ("kernel", "xapi"):
        library = info["libraries"].get(alias)
        if not isinstance(library, dict) or library.get("error"):
            continue
        record_name = f"libref_{alias}_{_symbol_token(library['name'])}"
        _add_record(
            records_by_id,
            _record(
                "library_reference",
                record_name,
                address=library.get("virtual_address"),
                subsystem=_library_subsystem(library["name"]),
                confidence="confirmed",
                source=f"xbe.libraries.{alias}",
                display_name=library["name"],
                tags=["library", alias],
                notes=[f"Named {alias} library version pointer from XBE metadata."],
                evidence=[_library_evidence(library)],
            ),
        )

    for feature in info["library_features"]:
        _add_record(
            records_by_id,
            _record(
                "library_feature",
                f"feature_{_symbol_token(feature['name'])}",
                subsystem=_library_subsystem(feature["name"]),
                confidence="confirmed",
                source="xbe.library_features",
                display_name=feature["name"],
                tags=["library_feature"],
                notes=["Library feature descriptor from extended XBE header metadata."],
                evidence=[_library_evidence(feature)],
            ),
        )


def _add_library_record(
    records_by_id: dict[str, dict[str, Any]],
    library: dict[str, Any],
    source: str,
) -> None:
    _add_record(
        records_by_id,
        _record(
            "library",
            f"lib_{_symbol_token(library['name'])}",
            subsystem=_library_subsystem(library["name"]),
            confidence="confirmed",
            source=source,
            display_name=library["name"],
            tags=["library"],
            notes=["Linked XDK library version metadata from the XBE header."],
            evidence=[_library_evidence(library)],
        ),
    )


def _seed_import_records(
    records_by_id: dict[str, dict[str, Any]], info: dict[str, Any]
) -> None:
    for import_info in info["kernel_imports"]["imports"]:
        export_name = import_info["name"] or f"ordinal_{import_info['ordinal']:04d}"
        subsystem = _kernel_import_subsystem(export_name)
        _add_record(
            records_by_id,
            _record(
                "import_thunk",
                f"imp_kernel_{import_info['ordinal']:04d}_{_symbol_token(export_name)}",
                address=import_info["thunk_address"],
                size=4,
                ordinal=import_info["ordinal"],
                subsystem=subsystem,
                confidence="confirmed" if import_info["name"] else "inferred",
                source="xbe.kernel_imports",
                display_name=f"xboxkrnl.exe!{export_name}",
                tags=["import", "kernel_import", "thunk"],
                notes=[
                    "Kernel import thunk from the XBE import table. The loader patches this address to a host shim or synthetic stub."
                ],
                evidence=[
                    {
                        "raw_value_hex": import_info["raw_value_hex"],
                        "ordinal": import_info["ordinal"],
                    }
                ],
            ),
        )

    for descriptor in info["non_kernel_imports"]:
        image_name = descriptor["image_name"]
        subsystem = _library_subsystem(image_name)
        for thunk in descriptor["thunks"]:
            _add_record(
                records_by_id,
                _record(
                    "import_thunk",
                    f"imp_{_symbol_token(image_name)}_{thunk['ordinal']:04d}",
                    address=thunk["thunk_address"],
                    size=4,
                    ordinal=thunk["ordinal"],
                    subsystem=subsystem,
                    confidence="confirmed",
                    source="xbe.non_kernel_imports",
                    display_name=f"{image_name}!ordinal_{thunk['ordinal']}",
                    tags=["import", "library_import", "thunk"],
                    notes=[
                        "Non-kernel import thunk from the XBE import descriptor table."
                    ],
                    evidence=[
                        {
                            "descriptor_address": descriptor["descriptor_address_hex"],
                            "image_name": image_name,
                            "raw_value_hex": thunk["raw_value_hex"],
                        }
                    ],
                ),
            )


def _seed_focus_area_records(records_by_id: dict[str, dict[str, Any]]) -> None:
    current_records = list(records_by_id.values())
    focus_specs = [
        (
            "startup",
            "focus_startup_code",
            ["startup"],
            "Entry point, TLS directory, and TLS callbacks are the first concrete startup anchors.",
        ),
        (
            "allocator",
            "focus_allocator_paths",
            ["allocator"],
            "Allocator paths begin at Ex, Mm, and Nt memory-management imports.",
        ),
        (
            "filesystem",
            "focus_filesystem_paths",
            ["filesystem"],
            "Filesystem paths begin at Nt and Io file/directory imports.",
        ),
        (
            "rendering",
            "focus_rendering_setup",
            ["rendering"],
            "Rendering setup begins at graphics-related libraries, sections, and video/display imports.",
        ),
        (
            "audio",
            "focus_audio_setup",
            ["audio"],
            "Audio setup begins at sound, codec, and audio-output libraries or imports.",
        ),
        (
            "input",
            "focus_input_polling",
            ["input"],
            "Input polling sites still need code analysis unless direct XInput/XAPI input imports are recovered.",
        ),
        (
            "main_loop",
            "focus_main_loop",
            ["main_loop"],
            "The main loop cannot be named from metadata alone; use entry/startup CFG recovery next.",
        ),
    ]

    for subsystem, name, evidence_subsystems, note in focus_specs:
        evidence_records = _records_for_subsystems(current_records, evidence_subsystems)
        if subsystem == "input" and not evidence_records:
            evidence_records = [
                record
                for record in current_records
                if record["kind"] in {"library", "library_reference"}
                and "xapi" in record["name"]
            ]

        confidence = "inferred" if evidence_records and subsystem != "main_loop" else "placeholder"
        if subsystem == "startup" and evidence_records:
            confidence = "confirmed"
        _add_record(
            records_by_id,
            _record(
                "analysis_focus",
                name,
                subsystem=subsystem,
                confidence=confidence,
                source="milestone_3.roadmap",
                tags=["milestone_3", "focus_area", "needs_disassembly"],
                notes=[note],
                evidence=[
                    {
                        "record_id": record["id"],
                        "name": record["name"],
                        "kind": record["kind"],
                        "confidence": record["confidence"],
                    }
                    for record in evidence_records[:16]
                ],
            ),
        )


def _build_compiler_runtime_patterns(
    info: dict[str, Any], records: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    patterns: list[dict[str, Any]] = []
    selected = info["header"]["entry_point"]["selected"]
    if selected:
        patterns.append(
            _pattern(
                "xbe_entry_point",
                "XBE encoded entry point resolves to startup code.",
                "startup",
                "probable",
                [
                    {
                        "address": _hex32(selected["value"]),
                        "decode_key": selected["key"],
                    }
                ],
            )
        )

    tls = info["tls"]
    if tls:
        patterns.append(
            _pattern(
                "tls_directory",
                "TLS directory and callback table are present.",
                "startup",
                "confirmed",
                [
                    {
                        "tls_directory": _hex32(tls["virtual_address"]),
                        "callback_count": len(tls["callback_addresses"]),
                    }
                ],
            )
        )

    library_count = len(info["libraries"]["versions"])
    if library_count:
        patterns.append(
            _pattern(
                "xdk_library_versions",
                "XDK library version records fingerprint linked Xbox runtime components.",
                "runtime",
                "confirmed",
                [{"library_count": library_count}],
            )
        )

    kernel_import_count = info["kernel_imports"]["count"]
    if kernel_import_count:
        patterns.append(
            _pattern(
                "kernel_import_thunks",
                "Kernel imports use a metadata-discovered thunk table.",
                "loader",
                "confirmed",
                [
                    {
                        "table_address": info["kernel_imports"]["table_address_hex"],
                        "import_count": kernel_import_count,
                    }
                ],
            )
        )

    non_kernel_import_count = sum(
        descriptor["thunk_count"] for descriptor in info["non_kernel_imports"]
    )
    if non_kernel_import_count:
        patterns.append(
            _pattern(
                "non_kernel_import_descriptors",
                "Non-kernel import descriptors link external XDK DLL-style images by ordinal.",
                "runtime",
                "confirmed",
                [{"import_count": non_kernel_import_count}],
            )
        )

    zero_fill_total = info["memory_map"]["zero_fill_total"]
    if zero_fill_total:
        patterns.append(
            _pattern(
                "section_zero_fill",
                "At least one section has BSS-style zero-fill beyond file-backed bytes.",
                "data",
                "confirmed",
                [{"zero_fill_total": zero_fill_total}],
            )
        )

    mismatches = [
        section["name"]
        for section in info["sections"]
        if not section["digest_verification"]["matches"]
    ]
    if mismatches:
        patterns.append(
            _pattern(
                "section_digest_mismatch",
                "One or more section digests do not match parsed raw section bytes.",
                "loader",
                "confirmed",
                [{"sections": mismatches}],
            )
        )

    patterns.append(
        _pattern(
            "pe_stack_heap_reserves",
            "PE-compatible stack and heap reserve fields are available for runtime planning.",
            "runtime",
            "confirmed",
            [
                {
                    "stack_size": info["header"]["stack_size"],
                    "pe_heap_reserve": info["header"]["pe_heap_reserve"],
                    "pe_heap_commit": info["header"]["pe_heap_commit"],
                }
            ],
        )
    )

    pattern_ids = {pattern["id"] for pattern in patterns}
    if "analysis_focus:focus_main_loop" in {record["id"] for record in records}:
        pattern_id = "runtime_pattern:main_loop_pending"
        if pattern_id not in pattern_ids:
            patterns.append(
                _pattern(
                    "main_loop_pending",
                    "Main loop ownership is explicitly pending control-flow analysis.",
                    "main_loop",
                    "placeholder",
                    [{"next_step": "Recover entry-start control flow and first frame tick path."}],
                )
            )

    return patterns


def _source_summary(info: dict[str, Any], source_name: str | None) -> dict[str, Any]:
    header = info["header"]
    certificate = info["certificate"]
    return {
        "input_file_name": source_name,
        "format": info["format"],
        "title_name": certificate["title_name"],
        "title_id": certificate["title_id"]["hex"],
        "xbe_timestamp_utc": header["timestamp_utc"],
        "certificate_timestamp_utc": certificate["timestamp_utc"],
        "base_address": header["base_address"],
        "base_address_hex": _hex32(header["base_address"]),
        "image_size": header["image_size"],
        "section_count": header["section_count"],
        "entry_point": header["entry_point"]["selected"]["value"]
        if header["entry_point"]["selected"]
        else None,
        "entry_point_hex": header["entry_point"]["selected"]["address"]
        if header["entry_point"]["selected"]
        else None,
    }


def _build_subsystem_index(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts = Counter(record["subsystem"] for record in records)
    return [
        {
            "id": subsystem,
            "description": description,
            "record_count": counts.get(subsystem, 0),
        }
        for subsystem, description in SUBSYSTEM_TAXONOMY.items()
    ]


def _summary(database: dict[str, Any]) -> dict[str, Any]:
    records = database["records"]
    return {
        "record_count": len(records),
        "record_kinds": dict(sorted(Counter(record["kind"] for record in records).items())),
        "confidence_counts": dict(
            sorted(Counter(record["confidence"] for record in records).items())
        ),
        "subsystem_counts": dict(
            sorted(Counter(record["subsystem"] for record in records).items())
        ),
        "compiler_runtime_pattern_count": len(database["compiler_runtime_patterns"]),
        "focus_area_count": sum(1 for record in records if record["kind"] == "analysis_focus"),
    }


def _record(
    kind: str,
    name: str,
    *,
    subsystem: str,
    confidence: str,
    source: str,
    address: int | None = None,
    size: int | None = None,
    ordinal: int | None = None,
    display_name: str | None = None,
    tags: list[str] | None = None,
    notes: list[str] | None = None,
    evidence: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if confidence not in CONFIDENCE_LEVELS:
        raise ValueError(f"unknown confidence level: {confidence}")
    if subsystem not in SUBSYSTEM_TAXONOMY:
        raise ValueError(f"unknown subsystem: {subsystem}")

    merged_tags = sorted({kind, subsystem, *(tags or [])})
    return {
        "id": _record_id(kind, name, address=address, ordinal=ordinal),
        "kind": kind,
        "name": name,
        "display_name": display_name or name,
        "address": address,
        "address_hex": _hex32(address) if address is not None else None,
        "size": size,
        "ordinal": ordinal,
        "subsystem": subsystem,
        "confidence": confidence,
        "source": source,
        "tags": merged_tags,
        "notes": notes or [],
        "evidence": evidence or [],
    }


def _add_record(
    records_by_id: dict[str, dict[str, Any]], record: dict[str, Any]
) -> None:
    existing = records_by_id.get(record["id"])
    if not existing:
        records_by_id[record["id"]] = record
        return

    existing["tags"] = sorted({*existing["tags"], *record["tags"]})
    existing["notes"] = _dedupe_dict_list(existing["notes"], record["notes"])
    existing["evidence"] = _dedupe_dict_list(existing["evidence"], record["evidence"])


def _record_id(
    kind: str, name: str, *, address: int | None = None, ordinal: int | None = None
) -> str:
    parts = [_symbol_token(kind), _symbol_token(name)]
    if address is not None:
        parts.append(f"{address:08x}")
    elif ordinal is not None:
        parts.append(f"ord_{ordinal:04x}")
    return ":".join(parts)


def _pattern(
    name: str,
    description: str,
    subsystem: str,
    confidence: str,
    evidence: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "id": f"runtime_pattern:{_symbol_token(name)}",
        "name": name,
        "description": description,
        "subsystem": subsystem,
        "confidence": confidence,
        "evidence": evidence,
    }


def _library_evidence(library: dict[str, Any]) -> dict[str, Any]:
    evidence = {
        "name": library["name"],
        "version_major": library.get("version_major"),
        "version_minor": library.get("version_minor"),
        "version_build": library.get("version_build"),
        "flags": library.get("flags"),
    }
    if "index" in library:
        evidence["index"] = library["index"]
    if "qfe_version" in library:
        evidence["qfe_version"] = library["qfe_version"]
    if "debug_build" in library:
        evidence["debug_build"] = library["debug_build"]
    return evidence


def _records_for_subsystems(
    records: list[dict[str, Any]], subsystems: list[str]
) -> list[dict[str, Any]]:
    subsystem_set = set(subsystems)
    return [record for record in records if record["subsystem"] in subsystem_set]


def _dedupe_dict_list(left: list[Any], right: list[Any]) -> list[Any]:
    seen = {json.dumps(item, sort_keys=True) for item in left}
    result = list(left)
    for item in right:
        key = json.dumps(item, sort_keys=True)
        if key not in seen:
            result.append(item)
            seen.add(key)
    return result


def _symbol_token(text: str) -> str:
    token = re.sub(r"[^0-9A-Za-z]+", "_", text).strip("_").lower()
    if not token:
        token = "unnamed"
    if token[0].isdigit():
        token = f"n_{token}"
    return token


def _hex32(value: int) -> str:
    return f"0x{value:08X}"


def _permissions_from_flags(flag_names: list[str]) -> list[str]:
    permissions = {"read"}
    if "WRITABLE" in flag_names:
        permissions.add("write")
    if "EXECUTABLE" in flag_names:
        permissions.add("execute")
    return sorted(permissions)


def _section_subsystem(name: str) -> str:
    upper_name = name.upper()
    if any(token in upper_name for token in ("D3D", "D3DX", "XGRPH", "GRAPH", "GPU")):
        return "rendering"
    if any(token in upper_name for token in ("DSOUND", "SOUND", "AUDIO", "WMA", "DOLBY", "XACT")):
        return "audio"
    if any(token in upper_name for token in ("XINPUT", "PAD", "CONTROLLER")):
        return "input"
    if any(token in upper_name for token in ("XONLINE", "XNET", "ONLINE")):
        return "networking"
    if any(token in upper_name for token in ("XTIMAGE", "XSIMAGE", "MEDIA", "ASSET")):
        return "assets"
    if any(token in upper_name for token in ("RDATA", "CRT", "TLS")):
        return "runtime"
    if "TEXT" in upper_name or "CODE" in upper_name:
        return "code"
    if "DATA" in upper_name or "BSS" in upper_name:
        return "data"
    return "unknown"


def _library_subsystem(name: str) -> str:
    upper_name = name.upper()
    if any(token in upper_name for token in ("D3D", "D3DX", "XGRPH", "GRAPH")):
        return "rendering"
    if any(token in upper_name for token in ("DSOUND", "SOUND", "AUDIO", "WMA", "DOLBY", "XACT")):
        return "audio"
    if any(token in upper_name for token in ("XINPUT", "CONTROLLER")):
        return "input"
    if any(token in upper_name for token in ("XONLINE", "XNET", "ONLINE")):
        return "networking"
    if "XBOXKRNL" in upper_name:
        return "loader"
    if "XAPI" in upper_name:
        return "runtime"
    return "runtime"


def _kernel_import_subsystem(name: str) -> str:
    if name.startswith(("ExAllocate", "ExFree", "MmAllocate", "MmFree")):
        return "allocator"
    if name.startswith("Nt") and any(
        token in name
        for token in (
            "AllocateVirtualMemory",
            "FreeVirtualMemory",
            "ProtectVirtualMemory",
            "QueryVirtualMemory",
        )
    ):
        return "allocator"
    if name.startswith(("Io", "Iof")) or (
        name.startswith("Nt")
        and any(
            token in name
            for token in (
                "File",
                "Directory",
                "Volume",
                "IoCompletion",
                "SymbolicLink",
            )
        )
    ):
        return "filesystem"
    if name.startswith(("Av",)) or name in {"MmClaimGpuInstanceMemory"}:
        return "rendering"
    if name.startswith(("Ke", "Kf")) or (
        name.startswith("Nt")
        and any(
            token in name
            for token in (
                "Event",
                "Semaphore",
                "Timer",
                "Thread",
                "Mutant",
                "Wait",
                "Yield",
                "Apc",
            )
        )
    ):
        return "threading"
    if name.startswith("Mm"):
        return "allocator"
    if name.startswith("Rtl") or name.startswith("Interlocked"):
        return "runtime"
    if name.startswith("Dbg"):
        return "diagnostics"
    if name.startswith("Xc"):
        return "crypto"
    if name.startswith("Hal") or name.startswith("Xbox"):
        return "hardware"
    if name.startswith("Ob"):
        return "object_manager"
    if name.startswith("Ps"):
        return "threading"
    if name.startswith("Xe"):
        return "loader"
    return "unknown"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate a sanitized project-owned analysis database from an Xbox XBE."
    )
    parser.add_argument("xbe", type=Path, help="Path to the XBE file to analyze.")
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional output JSON path. Use reports/local/ for local generated data.",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print JSON output.",
    )
    args = parser.parse_args()

    database = build_analysis_database_from_file(args.xbe)
    output = json.dumps(database, indent=2 if args.pretty else None, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output + "\n", encoding="utf-8")
    else:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
