#!/usr/bin/env python3
"""Build and run deterministic same-ISA IA-32 native artifacts.

The frozen Phase-0 slice, Phase-1 resident worker, Phase-2 decoded-store
artifact, Phase-3 architecture/memory layer, Phase-4 host ABI, Phase-5
resident scheduler, and Phase-6 measured coverage-growth layer share this
implementation.
Every artifact is emitted ahead of execution from verified decoded bytes and
named static rewrites. The runtime contains no interpreter, JIT, decoder,
undecoded-XBE path, native promotion, or Python callback.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import mmap
import os
import shutil
import sqlite3
import struct
import subprocess
import sys
import time
import uuid
import zlib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import AbstractSet, Any, Iterable, Mapping, Sequence, cast


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.native_toolchain import load_toolchain_lock, resolve_clang_cl, sha256_file
from tools.loader.xbe_loader import XbeLoaderError, load_xbe_file
from tools.project_identity import ProjectIdentityError, verify_supported_xbe
from tools.playability.replay_capsule import (
    PAGE_SIZE,
    ReplayCapsule,
    ReplayCapsuleError,
    clone_capsule_state,
    cpu_state_from_record,
    cpu_state_record,
    load_replay_capsule,
)
from tools.recomp.x86_lifter import (
    CpuFlags,
    CpuState,
    LiftedFunction,
    Operand,
    REGISTER_NAMES,
    SparseMemory,
    X86Instruction,
    execute_lifted_function,
    lift_x86_function,
)


ARTIFACT_FORMAT = "b2-recomp-ia32-slice"
ARTIFACT_VERSION = 1
GENERATOR_VERSION = 13
EXECUTION_CONTRACT_FORMAT = "b2-recomp-ia32-execution-contract"
EXECUTION_CONTRACT_VERSION = 1
PHASE0_EXECUTION_CONTRACT_ID = "db1648476e03d88ab1c18a0ad7d04dddc7cd8b388754808c38467b2a6e72aae7"
ENTRY_EXIT_THUNK_ABI_VERSION = 1
HOST_SERVICE_THUNK_ABI_VERSION = 1
TIMING_PROTOCOL_VERSION = 1
OBSERVABLE_STATE_VERSION = 1
PERSISTENT_GENERATOR_VERSION = 14
PERSISTENT_WORKER_CONTRACT_FORMAT = "b2-recomp-ia32-persistent-worker-contract"
PERSISTENT_WORKER_CONTRACT_VERSION = 1
PHASE1_PERSISTENT_WORKER_CONTRACT_ID = (
    "f47d8abe70f7b6c628692c93cccc048c9f5b9fb6795ff7c3bfc346a91798285f"
)
DECODED_STORE_GENERATOR_VERSION = 15
DECODED_STORE_ARTIFACT_CONTRACT_FORMAT = "b2-recomp-ia32-decoded-store-artifact-contract"
DECODED_STORE_ARTIFACT_CONTRACT_VERSION = 1
PHASE2_DECODED_STORE_ARTIFACT_CONTRACT_ID = (
    "5831ed5424f7a3c3541f626d9a710d21ec64368c8ee3b823a9740d14c6be8651"
)
ARCHITECTURE_GENERATOR_VERSION = 16
ARCHITECTURE_CONTRACT_FORMAT = "b2-recomp-ia32-architecture-memory-contract"
ARCHITECTURE_CONTRACT_VERSION = 1
PHASE3_ARCHITECTURE_CONTRACT_ID = "ae0efb70deea9c115cbe4fc78ffaa4d691127738f622c8809f62e0484e1395f6"
HOST_ABI_GENERATOR_VERSION = 20
HOST_ABI_CONTRACT_FORMAT = "b2-recomp-ia32-host-abi-contract"
HOST_ABI_CONTRACT_VERSION = 2
PHASE4_HOST_ABI_CONTRACT_ID = "813b75d708756fb884bb03ea9199cebb458ddfc1908fc038ea841c9b78ac2542"
RESIDENT_SCHEDULER_GENERATOR_VERSION = 21
RESIDENT_SCHEDULER_CONTRACT_FORMAT = "b2-recomp-ia32-resident-scheduler-contract"
RESIDENT_SCHEDULER_CONTRACT_VERSION = 2
PHASE5_RESIDENT_SCHEDULER_CONTRACT_ID = (
    "46d3912dfeb6281b4e86399293f7ec728ecc7b9457bceaf4516ba28f0daa3a62"
)
COVERAGE_GROWTH_GENERATOR_VERSION = 22
COVERAGE_GROWTH_CONTRACT_FORMAT = "b2-recomp-ia32-coverage-growth-contract"
COVERAGE_GROWTH_CONTRACT_VERSION = 2
PHASE6_COVERAGE_GROWTH_CONTRACT_ID = (
    "6a73b7ecac983f0b448e74f59241faf63de961a22ec62e891af19e0aa8f0b50c"
)
CUTOVER_GENERATOR_VERSION = 23
NORMAL_LIVE_GENERATOR_VERSION = 31
CUTOVER_CONTRACT_FORMAT = "b2-recomp-ia32-launcher-cutover-contract"
CUTOVER_CONTRACT_VERSION = 1
PHASE7_CUTOVER_CONTRACT_ID = "805369e7b1de0e116abaddc4ffb4f675d3d89f52a19552ce9d0c151609d591b0"
NORMAL_LIVE_LAUNCH_CONTRACT_FORMAT = "b2-recomp-ia32-normal-live-launch-contract"
NORMAL_LIVE_LAUNCH_CONTRACT_VERSION = 1
NORMAL_LIVE_LAUNCH_PROTOCOL_VERSION = 1
NORMAL_LIVE_TRANSPORT_SCHEMA_VERSION = 1
NORMAL_LIVE_BOOT_IMAGE_FORMAT = "b2-recomp-ia32-normal-live-boot-image"
NORMAL_LIVE_BOOT_IMAGE_VERSION = 1
NORMAL_LIVE_BOOT_STACK_TOP = 0x70000000
# The boot CRT probes the absent Xbox kernel PE header at 0x80010000.  Sparse
# oracle memory returns zero for the header and the preceding derived section
# probe; direct IA-32 execution needs the same two pages resident explicitly.
NORMAL_LIVE_BOOT_KERNEL_PROBE_START = 0x8000F000
NORMAL_LIVE_BOOT_KERNEL_PROBE_SIZE = 0x2000
NORMAL_LIVE_BOOT_TLS_BASE = 0x72000000
NORMAL_LIVE_BOOT_TLS_VECTOR = NORMAL_LIVE_BOOT_TLS_BASE + 0x300
NORMAL_LIVE_BOOT_TLS_DATA = NORMAL_LIVE_BOOT_TLS_BASE + 0x400
NORMAL_LIVE_TLS_INDEX_ADDRESS = 0x005A61F0
NORMAL_LIVE_PRIMARY_STACK_TOP = 0x71000000
NORMAL_LIVE_PRIMARY_TLS_BASE = 0x72010000
NORMAL_LIVE_PRIMARY_THREAD_OBJECT = NORMAL_LIVE_PRIMARY_TLS_BASE + 0x1000
NORMAL_LIVE_PRIMARY_TLS_DATA = NORMAL_LIVE_PRIMARY_TLS_BASE + 0x2000
NORMAL_LIVE_PRIMARY_TLS_VECTOR = NORMAL_LIVE_PRIMARY_TLS_BASE + 0x3000
NORMAL_LIVE_VBLANK_CALLBACK_ADDRESS = 0x000B86C0
NORMAL_LIVE_VBLANK_REGISTER_ADDRESS = 0x00215880
NORMAL_LIVE_VBLANK_CONTEXT_GLOBAL = 0x002256B8
NORMAL_LIVE_VBLANK_CALLBACK_OFFSET = 0x1988
NORMAL_LIVE_VBLANK_DATA_ADDRESS = 0x21B77000
NORMAL_LIVE_VBLANK_STACK_TOP = 0x71FFF000
NORMAL_LIVE_VBLANK_TLS_BASE = 0x72020000
NORMAL_LIVE_VBLANK_GENERATION_ADDRESS = 0x005518FC
NORMAL_LIVE_FLIP_STOP_ADDRESS = 0x00222540
COVERAGE_PROFILE_FORMAT = "b2-recomp-ia32-coverage-profile"
COVERAGE_PROFILE_VERSION = 1
COVERAGE_GROWTH_SLICE_ORDER = (
    "lesson-one-hot-scc",
    "boot-frontend-continuity",
    "worker-vblank-cold",
)
DECODED_BLOCK_STORE_VERSION = 3
WORKER_PROTOCOL_VERSION = 1
TARGET_TRIPLE = "i686-pc-windows-msvc"
WINDOWS_ALLOCATION_GRANULARITY = 0x10000
HELPER_IMAGE_BASE = 0x10000
DETACHED_HELPER_IMAGE_BASE = HELPER_IMAGE_BASE
DETACHED_GUEST_RESERVATION_START = 0x00011000
DETACHED_GUEST_RESERVATION_END = 0x01000000
DETACHED_HELPER_GUARD_END = 0x02000000
WORKER_EXCHANGE_VIEW_CANDIDATES = (0x64000000, 0x68000000, 0x6C000000)
WORKER_EXCHANGE_VIEW_SPAN = 0x02000000
GUEST_SECTION_RVA = 0x40000
GUEST_SECTION_ADDRESS = HELPER_IMAGE_BASE + GUEST_SECTION_RVA
MINIMUM_USER_ADDRESS = 0x10000
LOW_GUEST_IMAGE_LIMIT = 0x10000000
THUNK_REGION = 0x50000000
START_THUNK_ADDRESS = THUNK_REGION
EXIT_THUNK_ADDRESS = THUNK_REGION + 0x1000
SERVICE_DISPATCH_RELAY_ADDRESS = THUNK_REGION + 0x2000
AUDITED_SERVICE_THUNK_BASE = THUNK_REGION + 0x3000
AUDITED_SERVICE_THUNK_STRIDE = 0x100
ARCHITECTURE_REWRITE_THUNK_BASE = THUNK_REGION + 0xE000
NORMAL_LIVE_BOOT_RETURN_THUNK_ADDRESS = THUNK_REGION + 0x2100
NORMAL_LIVE_RESUME_THUNK_ADDRESS = THUNK_REGION + 0x2200
NORMAL_LIVE_VBLANK_START_THUNK_ADDRESS = THUNK_REGION + 0x2300
NORMAL_LIVE_VBLANK_EXIT_THUNK_ADDRESS = THUNK_REGION + 0x2400
ARCHITECTURE_RDTSC_THUNK_ADDRESS = ARCHITECTURE_REWRITE_THUNK_BASE
ARCHITECTURE_SGDT_THUNK_ADDRESS = ARCHITECTURE_REWRITE_THUNK_BASE + 0x100
ARCHITECTURE_DENSE_RDTSC_THUNK_ADDRESS = ARCHITECTURE_REWRITE_THUNK_BASE + 0x200
ARCHITECTURE_MCPX_FRAME_COUNTER_THUNK_BASE = ARCHITECTURE_REWRITE_THUNK_BASE + 0x300
ARCHITECTURE_MCPX_FRAME_COUNTER_THUNK_STRIDE = 0x20
ARCHITECTURE_AUDIO_DSP_READY_THUNK_ADDRESS = ARCHITECTURE_REWRITE_THUNK_BASE + 0x400
ARCHITECTURE_TIMESTAMP_COUNTER_ADDRESS = THUNK_REGION + 0xF000
SCRATCH_ADDRESS = THUNK_REGION + WINDOWS_ALLOCATION_GRANULARITY
NORMAL_LIVE_VBLANK_SCRATCH_ADDRESS = SCRATCH_ADDRESS + 0x1000
HELPER_IMAGE_LIMIT = GUEST_SECTION_ADDRESS

NATIVE_HOST_SERVICE_RETURN_CONSTANT = 1
SUPPORTED_RETURN_CONSTANT_SERVICES = {
    "RtlEnterCriticalSection": 4,
    "RtlLeaveCriticalSection": 4,
    "RtlInitializeCriticalSection": 4,
}

EXCHANGE_MAGIC = 0x49325242  # "B2RI"
EXCHANGE_VERSION = 1
EXCHANGE_STATUS_PENDING = 0
EXCHANGE_STATUS_RUNNING = 1
EXCHANGE_STATUS_COMPLETE = 2
WORKER_STATUS_READY = 3
WORKER_STATUS_STOPPED = 4
WORKER_STATUS_FAULT = 97
WORKER_STATUS_PROTOCOL_ERROR = 98
WORKER_COMMAND_ENTER = 1
WORKER_COMMAND_RESUME = 2
WORKER_COMMAND_SERVICE_RETURN = 3
WORKER_COMMAND_STOP = 4
WORKER_COMMAND_SCHEDULE = 5
WORKER_EXCHANGE_VERSION = 2
ARCHITECTURE_EXCHANGE_VERSION = 3
HOST_ABI_EXCHANGE_VERSION = 4
RESIDENT_SCHEDULER_EXCHANGE_VERSION = 5
CUTOVER_EXCHANGE_VERSION = 6
EXCHANGE_HEADER_SIZE = 592
EXCHANGE_FXSAVE_OFFSET = 80
EXCHANGE_FXSAVE_SIZE = 512

# Intel documents bits 6-7 and 13-15 of the x87 control word, and bits 16-31
# of MXCSR, as reserved. Hardware save/restore may canonicalize those bits.
X87_CONTROL_WORD_OBSERVABLE_MASK = 0x1F3F
MXCSR_DEFINED_MASK = 0x0000FFFF
MXCSR_STICKY_STATUS_MASK = 0x0000003F
MXCSR_PHASE0_COMPARISON_MASK = MXCSR_DEFINED_MASK & ~MXCSR_STICKY_STATUS_MASK
DETERMINISTIC_TSC_STEP = 733_000
PHYSICAL_ALIAS_START = 0xA0000000
PHYSICAL_ALIAS_END = 0xC0000000
PHASE3_MMIO_SHADOW_BASE = 0x51000000
PHASE7_PHYSICAL_ZERO_PAGE = 0x80000000
PHASE7_D3D_MMIO_PAGE = 0xFED00000
PHASE7_AUDIO_SG_MMIO_PAGE = 0xFE804000
PHASE7_MCPX_MMIO_PAGE = 0xFE820000
PHASE7_AUDIO_BUFFER_MMIO_START = 0xFE830000
PHASE7_AUDIO_BUFFER_MMIO_END = 0xFE840000
PHASE7_AUDIO_BUFFER_MMIO_PAGES = frozenset(
    range(PHASE7_AUDIO_BUFFER_MMIO_START, PHASE7_AUDIO_BUFFER_MMIO_END, PAGE_SIZE)
)
PHASE7_CAPTURED_DIRECT_MMIO_PAGES = frozenset({0xFD100000, 0xFD800000, PHASE7_D3D_MMIO_PAGE})
# The XDK D3D initializer stores 0xFD000000 in its device root and reaches
# these pages through register-derived offsets.  They cannot use the absolute
# operand relocation path, so normal-live materializes deterministic zeroed
# identity shadows for the finite decoded startup call tree.
PHASE7_ZEROED_DIRECT_MMIO_PAGES = frozenset(
    {
        0xFD000000,
        0xFD001000,
        0xFD002000,
        0xFD003000,
        0xFD008000,
        0xFD009000,
        0xFD400000,
        0xFD600000,
        0xFD601000,
        0xFD680000,
        0xFD700000,
        0xFD701000,
        0xFD702000,
        0xFD703000,
        0xFD704000,
        0xFE000000,
        PHASE7_AUDIO_SG_MMIO_PAGE,
        PHASE7_MCPX_MMIO_PAGE,
        *PHASE7_AUDIO_BUFFER_MMIO_PAGES,
    }
)
PHASE7_DIRECT_MMIO_PAGES = frozenset(
    PHASE7_CAPTURED_DIRECT_MMIO_PAGES | PHASE7_ZEROED_DIRECT_MMIO_PAGES
)
PHASE7_SELF_CLEARING_MMIO_WRITE_SITES = frozenset({0x0021AE1A, 0x0021AEB5, 0x0021AF95})
PHASE7_SELF_CLEARING_MMIO_ADDRESS = 0xFD100410
PHASE7_SELF_CLEARING_MMIO_MASK = 0x00010000
PHASE7_GPU_COMPLETION_SHADOW_READ_SITES = frozenset({0x0021A7D5, 0x0021A7F3})
PHASE7_GPU_SUBMITTED_CONTEXT_OFFSET = 0x40
PHASE7_GPU_COMPLETED_CONTEXT_OFFSET = 0x44
PHASE7_PFIFO_WRITE_ONE_TO_CLEAR_SITE = 0x0021D664
PHASE7_PFIFO_STATUS_OFFSET = 0x00400100
PHASE7_PFIFO_STATUS_CLEAR_MASK = 0x00001000
PHASE7_MCPX_FRAME_COUNTER_ADDRESS = 0xFE820010
PHASE7_MCPX_FRAME_COUNTER_INCREMENT = 4
# MCPX exposes its mix-bin and scatter/gather tables through register arrays.
# The first writer is shared by a 32-slot initializer and the matching setter;
# the setter carries the same slot through its software table before the
# hardware call.  The other two writers mask their index to 0..63 in the same
# decoded block.  Require every setup/caller/mask/access instruction, every
# direct caller of the shared writer, and every indexed FE820xxx operand before
# making this one page an identity shadow.
PHASE7_MCPX_SLOT_WRITE_TARGET = 0x002311F4
PHASE7_MCPX_SLOT_WRITE_CALL_SITES = frozenset({0x0022C46C, 0x002317B6})
PHASE7_MCPX_INDEXED_MMIO_FAMILY = {
    0x0022C457: "8B4C240C",
    0x0022C464: "885C0A14",
    0x0022C468: "51",
    0x0022C469: "8B480C",
    0x0022C46C: "E8834D0000",
    0x002311F4: "55",
    0x002311F5: "8BEC",
    0x002311F7: "51",
    0x002311F8: "51",
    0x002311FD: "56",
    0x002311FE: "8BF1",
    0x00231215: "8B4E0C",
    0x00231218: "8B4508",
    0x0023121B: "0FB64C0114",
    0x00231220: "83E107",
    0x00231223: "890C85000282FE",
    0x002317AD: "33DB",
    0x002317AF: "85FF",
    0x002317B1: "7C10",
    0x002317B3: "53",
    0x002317B4: "8BCE",
    0x002317B6: "E839FAFFFF",
    0x002317BB: "43",
    0x002317BC: "83FB20",
    0x002317BF: "8BF8",
    0x002317C1: "72EC",
    0x00231B50: "8BD1",
    0x00231B52: "81E1C0FF3F00",
    0x00231B58: "890D900182FE",
    0x00231B5E: "8B4DF0",
    0x00231B61: "83E23F",
    0x00231B64: "890CD5000682FE",
    0x00231B6B: "8B4DDC",
    0x00231B74: "8934D5040682FE",
}
PHASE7_MCPX_INDEXED_MMIO_ACCESS_SITES = frozenset({0x00231223, 0x00231B64, 0x00231B74})
PHASE7_AUDIO_DSP_CONTROL_ADDRESS = 0xFEC0012C
PHASE7_AUDIO_DSP_STATUS_ADDRESS = 0xFEC00130
PHASE7_AUDIO_DSP_RESET_REQUEST = 0x00000002
PHASE7_AUDIO_DSP_RESET_READY = 0x00000100
PHASE7_AUDIO_DSP_CONTROL_WRITE_SITES = frozenset({0x00237095, 0x002370A1})
PHASE7_AUDIO_VOICE_COMMAND_PENDING = 0x02
PHASE7_AUDIO_VOICE_COMMAND_DISPLACEMENT = -0x013FFEF5
PHASE7_AUDIO_VOICE_COMMAND_WRITE_BASES = {
    0x00237359: "eax",
    0x002377F0: "ecx",
}
# The native audio-buffer submitter uses a dword mailbox at buffer state +0x810.
# One producer writes command 3 and immediately polls for the hardware-owned
# word to clear.  The adjacent accessor polls the same word before reuse and
# then submits command 2, so both producers must complete in place when no MCPX
# worker owns this mailbox.  Validate the complete accessor and both direct
# callers before rewriting either command.
PHASE7_AUDIO_BUFFER_COMMAND_VALUE = 3
PHASE7_AUDIO_BUFFER_COMMAND_IMMEDIATE_SITE = 0x002309A8
PHASE7_AUDIO_BUFFER_COMMAND_VALUES = {
    0x002309A8: 3,
    0x00230A3A: 2,
}
PHASE7_AUDIO_BUFFER_COMMAND_CALL_SITES = {
    0x0022C411: "E8D6450000",
    0x0023136D: "E87AF6FFFF",
}
PHASE7_AUDIO_BUFFER_COMMAND_FAMILY = {
    0x002309A8: "6A03",
    0x002309AA: "58",
    0x002309AD: "81C310080000",
    0x002309B5: "8903",
    0x002309B7: "833B00",
    0x002309BA: "75FB",
    0x002309EC: "8B542408",
    0x002309F0: "85D2",
    0x002309F2: "56",
    0x002309F3: "7514",
    0x002309F5: "8B7118",
    0x002309F8: "81FE00800000",
    0x002309FE: "8B511C",
    0x00230A01: "744A",
    0x00230A03: "85D2",
    0x00230A05: "7446",
    0x00230A07: "EB04",
    0x00230A09: "8B742408",
    0x00230A0D: "8B4108",
    0x00230A10: "8B4010",
    0x00230A13: "8B00",
    0x00230A15: "0500080000",
    0x00230A1A: "83781000",
    0x00230A1E: "75FA",
    0x00230A20: "53",
    0x00230A21: "8BDE",
    0x00230A23: "C1EB02",
    0x00230A26: "2B5804",
    0x00230A29: "C1EA02",
    0x00230A2C: "81EB06020000",
    0x00230A32: "8918",
    0x00230A34: "897008",
    0x00230A37: "89500C",
    0x00230A3A: "C7401002000000",
    0x00230A41: "83611C00",
    0x00230A45: "C7411800800000",
    0x00230A4C: "5B",
    0x00230A4D: "5E",
    0x00230A4E: "C20800",
}
# DirectSound exposes one 64 KiB hardware buffer aperture at FE830000-FE83FFFF.
# The decoded family constructs per-buffer pointers from FE836000, funnels all
# reads through one bounds-checked copy helper, and owns the FE83FFxx control
# words. Validate the pointer math, every helper caller, the complete copy, and
# every address-named reference before materializing the finite identity shadow.
PHASE7_AUDIO_BUFFER_COPY_TARGET = 0x00230BFF
PHASE7_AUDIO_BUFFER_COPY_CALL_SITES = frozenset({0x0022C3BD, 0x00231259, 0x00231309})
PHASE7_AUDIO_BUFFER_MMIO_REFERENCE_SITES = frozenset(
    {
        0x00230957,
        0x0023655D,
        0x00236563,
        0x00236622,
        0x0023662D,
        0x00236632,
        0x00236666,
        0x00236FD2,
    }
)
PHASE7_AUDIO_BUFFER_MMIO_FAMILY = {
    # Per-buffer hardware address construction.
    0x0023094E: "8B45EC",
    0x00230951: "8B480C",
    0x00230954: "8B4904",
    0x00230957: "814710006083FE",
    0x0023095E: "8345F808",
    0x00230962: "81E918687C01",
    0x00230968: "010F",
    0x0023096A: "B97ABEA0FF",
    0x0023096F: "2B8B04080000",
    0x00230975: "83C720",
    0x00230978: "C1E102",
    0x0023097B: "014FE8",
    0x0023097E: "8B4008",
    0x00230981: "8B4014",
    0x00230984: "2D00C00000",
    0x00230989: "0147F8",
    # Bounds-checked copy helper, including both REP copy widths.
    0x00230BFF: "55",
    0x00230C00: "8BEC",
    0x00230C02: "8D4508",
    0x00230C05: "50",
    0x00230C06: "FF7508",
    0x00230C09: "E8B7FDFFFF",
    0x00230C0E: "85C0",
    0x00230C10: "7C23",
    0x00230C12: "8B4508",
    0x00230C15: "8B4D14",
    0x00230C18: "56",
    0x00230C19: "8B7008",
    0x00230C1C: "03750C",
    0x00230C1F: "8BC1",
    0x00230C21: "57",
    0x00230C22: "8B7D10",
    0x00230C25: "C1E902",
    0x00230C28: "F3A5",
    0x00230C2A: "8BC8",
    0x00230C2C: "83E103",
    0x00230C2F: "F3A4",
    0x00230C31: "5F",
    0x00230C32: "33C0",
    0x00230C34: "5E",
    0x00230C35: "5D",
    0x00230C36: "C21000",
    # Public copy caller.
    0x0022C3A7: "8B4508",
    0x0022C3AA: "8B400C",
    0x0022C3AD: "8B4814",
    0x0022C3B0: "57",
    0x0022C3B1: "FF7518",
    0x0022C3B4: "FF7514",
    0x0022C3B7: "FF7510",
    0x0022C3BA: "FF750C",
    0x0022C3BD: "E83D480000",
    # Fixed 0x34-byte status copy caller.
    0x00231242: "8B460C",
    0x00231245: "8B4010",
    0x00231248: "83F8FF",
    0x0023124B: "743D",
    0x0023124D: "6A34",
    0x0023124F: "8D4DCC",
    0x00231252: "51",
    0x00231253: "8B4E14",
    0x00231256: "6A00",
    0x00231258: "50",
    0x00231259: "E8A1F9FFFF",
    # Fixed 0x118-byte format copy caller observed at the Phase-7 fault.
    0x002312ED: "8B400C",
    0x002312F0: "83F8FF",
    0x002312F3: "747E",
    0x002312F5: "57",
    0x002312F6: "BF18010000",
    0x002312FB: "57",
    0x002312FC: "8D8DDCFDFFFF",
    0x00231302: "51",
    0x00231303: "8B4E14",
    0x00231306: "6A00",
    0x00231308: "50",
    0x00231309: "E8F1F8FFFF",
    # Address-named aperture control words.
    0x0023655D: "893500FF83FE",
    0x00236563: "893504FF83FE",
    0x00236622: "893DFCFF83FE",
    0x0023662D: "A310FF83FE",
    0x00236632: "C70514FF83FEFF000000",
    0x00236666: "C705FCFF83FE03000000",
    0x00236FD2: "C705FCFF83FE00000000",
}
# The XDK audio scatter/gather initializer seeds EDI with FE804028, writes two
# bounded descriptor pairs through it, then programs the two adjacent absolute
# registers.  Retain the complete setup/bound/access family before allowing the
# single identity-shadow page so an unrelated dynamic high address cannot gain
# a general mapping policy.
PHASE7_AUDIO_SG_MMIO_FAMILY = {
    0x002366EF: "BF284080FE",
    0x002366F9: "C745F402000000",
    0x00236745: "8947FC",
    0x00236752: "8917",
    0x00236757: "83C710",
    0x0023675A: "FF4DF4",
    0x0023675D: "75A1",
    0x00236B36: "890D2C4080FE",
    0x00236B3C: "C7053C4080FE00380000",
}
PHASE7_AUDIO_SG_MMIO_ACCESS_SITES = frozenset({0x00236745, 0x00236752, 0x00236B36, 0x00236B3C})
PHASE7_AUDIO_DSP_MMIO_PAGE = 0xFEC00000
# The normal-live DirectSound startup tree reaches this complete finite family
# before the first hardware voice is created.  Exact bytes keep the direct
# shadow page scoped to the observed MCPX register accessors rather than
# establishing a general dynamic-MMIO policy.
PHASE7_AUDIO_DSP_MMIO_FAMILY = {
    0x0023706C: "F6053401C0FE01",
    0x0023707E: "A12C01C0FE",
    0x00237095: "A32C01C0FE",
    0x002370A1: "A32C01C0FE",
    0x002370BC: "85353001C0FE",
    0x002370F4: "66890C550000C0FE",
    0x00237134: "80A00B01C0FE00",
    0x002371A4: "88980401C0FE",
    0x002371B9: "88880501C0FE",
    0x002371F5: "A37C01C0FE",
    0x00237226: "6689820801C0FE",
    0x00237359: "C6800B01C0FE02",
    0x00237369: "8A880B01C0FE",
    0x00237379: "89880001C0FE",
    0x00237387: "A37C01C0FE",
    0x00237419: "F6800601C0FE01",
    0x00237445: "C6800B01C0FE1D",
    0x002377F0: "C6810B01C0FE02",
}
PHASE7_GPU_PFIFO_RUNOUT_STATUS_ADDRESS = 0xFD002400
PHASE7_GPU_PFIFO_CACHE1_STATUS_ADDRESS = 0xFD003214
PHASE7_GPU_PFIFO_IDLE_BIT = 0x00000010
PHASE7_RESIDENT_INTERRUPT_MASK_SITES = {
    0x000E666A: "cli",
    0x000E6671: "sti",
    0x000E6D1D: "cli",
    0x000E6D24: "sti",
}
PHASE7_ZERO_PORT_READ_PREFIX_SITE = 0x0021D099
PHASE7_ZERO_PORT_READ_SITE = 0x0021D09D
PHASE7_DENSE_RDTSC_PREFIX_SITE = 0x000E24BC
PHASE7_DENSE_RDTSC_SITE = 0x000E24C0
# The strict repeated boot profile can enter overlapping decoded blocks after an
# indirect call has already returned, hiding the exact call instruction from the
# module-edge stream. These finite bindings come from the repeated profile,
# immutable vtables in the accepted Lesson One capsule, and retained bootstrap
# thread diagnostics. The 0x3F7BB targets are the manager's 27 captured slot-C
# methods plus two strict-profile boot variants. The 0xE689B binding is the
# bootstrap thread wrapper calling its captured start_context1 value. The
# 0x12799E binding is installed by the immediately preceding static assignment
# at 0x12798A and retained in the accepted capsule. The 0x28C571 callbacks are
# the three immutable slot-4 values referenced by the fixed descriptor table at
# 0x28BFB4-0x28BFC4.
PHASE7_CAPTURED_INDIRECT_TARGETS = {
    0x0010AA58: (0x000D8890,),
    0x00011086: (0x00015350, 0x00015420),
    0x00014920: (0x000154B0,),
    0x0003F7AF: (
        0x00030290,
        0x00042770,
        0x00042AF0,
        0x0004C9F0,
        0x000D5EC0,
        0x000D9370,
    ),
    0x0003F7BB: (
        0x0003FFD0,
        0x000411D0,
        0x00041860,
        0x00041B90,
        0x00041CF0,
        0x00041EF0,
        0x00042770,
        0x00042AF0,
        0x00042BD0,
        0x00043020,
        0x00044070,
        0x00045950,
        0x00046310,
        0x00046820,
        0x00046C00,
        0x00047AE0,
        0x00047EA0,
        0x00048340,
        0x00049430,
        0x0004A6A0,
        0x0004B020,
        0x0004B4E0,
        0x0004C6E0,
        0x0004C9F0,
        0x0004CC70,
        0x0004D4B0,
        0x000D5EC0,
        0x000D7410,
        0x000D9370,
    ),
    0x0004404E: (0x00032EE0,),
    0x0004475F: (0x0003A030,),
    0x0004CB30: (0x000309F0,),
    0x000E689B: (0x000E24E1,),
    0x0012799E: (0x00127965,),
    0x0028C571: (0x0028C180, 0x0028C32F, 0x0028C9C8),
}
# The ordinary native boot reaches a base-manager initialization loop whose
# module-edge profile resumes only after the virtual call returns.  The live
# first-fault capture and both accepted Lesson One capsules identify the same
# immutable object/vtable pair.  Each tuple names the vtable and exact decoded
# slot target; recovery below also verifies the call's encoded slot offset.
PHASE7_CAPTURED_VTABLE_SLOT_BINDINGS = {
    0x000D865B: ((0x00295D38, 0x000D9590),),
    0x0022FC0E: ((0x002C9450, 0x0022E932),),
}
# The title's Start path installs one fixed global input object into the
# frontend at +0x8C, then calls slot 0x0C.  Validate the pointer cell, object,
# complete four-method vtable, installer, and caller sequence as one family so
# accepting the observed slot cannot become a broad dynamic-vtable allowance.
PHASE7_TITLE_INPUT_OBJECT_POINTER_CELL = 0x0031C958
PHASE7_TITLE_INPUT_OBJECT = 0x0031C984
PHASE7_TITLE_INPUT_OBJECT_VTABLE = 0x002B3E7C
PHASE7_TITLE_INPUT_OBJECT_VTABLE_ENTRIES = (
    0x0002AFA0,
    0x0002AFC0,
    0x0002AFF0,
    0x0002B040,
)
PHASE7_TITLE_INPUT_OBJECT_BINDINGS = {
    0x0002AB5A: (0x0C, 0x0002B040),
}
PHASE7_TITLE_INPUT_OBJECT_INSTRUCTION_BYTES = {
    0x0002A636: "8B1558C93100",
    0x0002A63C: "89968C000000",
    0x0002AB50: "8B8E8C000000",
    0x0002AB56: "8B01",
    0x0002AB58: "6A01",
    0x0002AB5A: "FF500C",
}
# The first post-Start frontend transition tail-calls slot 0x0C on the global
# object at 0x004CB364.  That cell is zero in the boot image and initialized by
# guest startup; a live first-fault capture and the accepted Lesson One capsule
# agree on the resulting exact object/vtable pair.  The adjacent 0x4501F call
# has the same address-named cell and slot, so validate both guarded sites as
# one bounded family while leaving other lifetime-dependent users unresolved.
PHASE7_POST_START_OBJECT_POINTER_CELL = 0x004CB364
PHASE7_POST_START_OBJECT_INITIAL_POINTER = 0x00000000
PHASE7_POST_START_OBJECT = 0x0031E8E0
PHASE7_POST_START_OBJECT_VTABLE = 0x002B4798
PHASE7_POST_START_OBJECT_VTABLE_ENTRIES = (
    0x0002EFD0,
    0x0002EFF0,
    0x0002F050,
    0x0002F1C0,
)
PHASE7_POST_START_OBJECT_BINDINGS = {
    0x00044F92: (0x0C, 0x0002F1C0),
    0x0004501F: (0x0C, 0x0002F1C0),
}
PHASE7_POST_START_OBJECT_INSTRUCTION_BYTES = {
    # Tail-call reached directly by the title transition.
    0x00044F80: "8B0D64B34C00",
    0x00044F86: "8B11",
    0x00044F8E: "89442404",
    0x00044F92: "FF620C",
    # Adjacent ordinary call over the same cell and slot.
    0x00045016: "8B0D64B34C00",
    0x0004501C: "8B11",
    0x0004501E: "50",
    0x0004501F: "FF520C",
}
# New-save creation first makes two consecutive slot-0x0C calls from one
# routine, then enters one handler containing four more calls on the companion
# object.  Live x86 CDB captures at 0x00045141 and 0x00045CEB record the same
# exact companion object/vtable pair.  Validate the complete handler-local
# family atomically while leaving the other lifetime-dependent users of both
# global cells guarded.
PHASE7_NEW_SAVE_COMPANION_OBJECT_POINTER_CELL = 0x004CB350
PHASE7_NEW_SAVE_COMPANION_OBJECT_INITIAL_POINTER = 0x00000000
PHASE7_NEW_SAVE_COMPANION_OBJECT = 0x00322778
PHASE7_NEW_SAVE_COMPANION_OBJECT_VTABLE = 0x002B59A8
PHASE7_NEW_SAVE_COMPANION_OBJECT_VTABLE_ENTRIES = (
    0x000345C0,
    0x000345E0,
    0x000D7410,
    0x00034880,
)
PHASE7_NEW_SAVE_OBJECT_BINDINGS = {
    0x00045141: (PHASE7_POST_START_OBJECT_VTABLE, 0x0C, 0x0002F1C0),
    0x0004514D: (PHASE7_NEW_SAVE_COMPANION_OBJECT_VTABLE, 0x0C, 0x00034880),
    0x00045CEB: (PHASE7_NEW_SAVE_COMPANION_OBJECT_VTABLE, 0x0C, 0x00034880),
    0x00045D57: (PHASE7_NEW_SAVE_COMPANION_OBJECT_VTABLE, 0x0C, 0x00034880),
    0x00045D95: (PHASE7_NEW_SAVE_COMPANION_OBJECT_VTABLE, 0x0C, 0x00034880),
    0x00045E98: (PHASE7_NEW_SAVE_COMPANION_OBJECT_VTABLE, 0x0C, 0x00034880),
}
PHASE7_NEW_SAVE_OBJECT_INSTRUCTION_BYTES = {
    0x00045138: "8B0D64B34C00",
    0x0004513E: "8B11",
    0x00045140: "50",
    0x00045141: "FF520C",
    0x00045144: "8B0D50B34C00",
    0x0004514A: "8B01",
    0x0004514C: "55",
    0x0004514D: "FF500C",
    0x00045CDC: "8B0D50B34C00",
    0x00045CE2: "8B11",
    0x00045CE4: "8D8650060000",
    0x00045CEA: "50",
    0x00045CEB: "FF520C",
    0x00045D4D: "8B0D50B34C00",
    0x00045D53: "8B01",
    0x00045D55: "6A00",
    0x00045D57: "FF500C",
    0x00045D86: "8B0D50B34C00",
    0x00045D8C: "8B11",
    0x00045D8E: "8D8650060000",
    0x00045D94: "50",
    0x00045D95: "FF520C",
    0x00045E8C: "8B0D50B34C00",
    0x00045E92: "8B11",
    0x00045E94: "6A00",
    0x00045E96: "8BD8",
    0x00045E98: "FF520C",
}
# Save/load creation and the paired resource-update methods reach slot 0x0C on
# the global object at 0x004CB370.  The resource methods are adjacent entries in
# the four-method owner vtable at 0x002B7350.  Validate both complete vtables,
# every exact caller sequence, and the cleared boot pointer cell so this finite
# lifetime-dependent family remains fail-closed.
PHASE7_SAVE_SLOT_OBJECT_POINTER_CELL = 0x004CB370
PHASE7_SAVE_SLOT_OBJECT_INITIAL_POINTER = 0x00000000
PHASE7_SAVE_SLOT_OBJECT = 0x00324B78
PHASE7_SAVE_SLOT_OBJECT_VTABLE = 0x002B64F8
PHASE7_SAVE_SLOT_OBJECT_VTABLE_ENTRIES = (
    0x00038410,
    0x000390B0,
    0x00039FF0,
    0x0003A030,
)
PHASE7_SAVE_SLOT_OWNER_VTABLE = 0x002B7350
PHASE7_SAVE_SLOT_OWNER_VTABLE_ENTRIES = (
    0x0003CD40,
    0x0003CD60,
    0x0003CE50,
    0x0003D270,
)
PHASE7_SAVE_SLOT_OBJECT_BINDINGS = {
    0x0003D25C: (0x0C, 0x0003A030),
    0x0003D642: (0x0C, 0x0003A030),
    0x0004475F: (0x0C, 0x0003A030),
    0x0004484F: (0x0C, 0x0003A030),
    0x0004486A: (0x0C, 0x0003A030),
}
PHASE7_SAVE_SLOT_OBJECT_INSTRUCTION_BYTES = {
    0x0003D253: "8B0D70B34C00",
    0x0003D259: "8B11",
    0x0003D25B: "56",
    0x0003D25C: "FF520C",
    0x0003D63A: "8B0D70B34C00",
    0x0003D640: "8B01",
    0x0003D642: "FF500C",
    0x00044754: "8B0D70B34C00",
    0x0004475A: "8B01",
    0x0004475C: "57",
    0x0004475D: "6A00",
    0x0004475F: "FF500C",
    0x00044846: "8B0D70B34C00",
    0x0004484C: "8B11",
    0x0004484E: "57",
    0x0004484F: "FF520C",
    0x00044860: "8B0D70B34C00",
    0x00044866: "8B01",
    0x00044868: "6A00",
    0x0004486A: "FF500C",
    0x0004486D: "C3",
}
# The selection setter at 0x00032EE0 is reached through slot 0x0C of the
# lifetime-installed object at 0x004CB360.  Its 34 callers are a closed family:
# each exact capsule loads that cell, obtains the same four-entry vtable, and
# makes the slot-C call.  Keep every caller byte-for-byte sealed so this does
# not become a general allowance for global-object or slot-C dispatches.
PHASE7_RESOURCE_SELECTION_OBJECT_POINTER_CELL = 0x004CB360
PHASE7_RESOURCE_SELECTION_OBJECT_INITIAL_POINTER = 0x00000000
PHASE7_RESOURCE_SELECTION_OBJECT = 0x00320698
PHASE7_RESOURCE_SELECTION_OBJECT_VTABLE = 0x002B5008
PHASE7_RESOURCE_SELECTION_OBJECT_VTABLE_ENTRIES = (
    0x000A97B0,
    0x00032ED0,
    0x000D7410,
    0x00032EE0,
)
PHASE7_RESOURCE_SELECTION_OBJECT_BINDINGS = {
    0x000387D1: (0x000387C5, 0x0C, 0x00032EE0),
    0x0003FE40: (0x0003FE26, 0x0C, 0x00032EE0),
    0x00040EB4: (0x00040EAA, 0x0C, 0x00032EE0),
    0x000411A7: (0x00041198, 0x0C, 0x00032EE0),
    0x0004183F: (0x00041830, 0x0C, 0x00032EE0),
    0x00041B46: (0x00041B37, 0x0C, 0x00032EE0),
    0x00041E56: (0x00041E46, 0x0C, 0x00032EE0),
    0x000426FD: (0x000426EE, 0x0C, 0x00032EE0),
    0x00042A58: (0x00042A48, 0x0C, 0x00032EE0),
    0x00042EC8: (0x00042EBB, 0x0C, 0x00032EE0),
    0x00042F9F: (0x00042F90, 0x0C, 0x00032EE0),
    0x00042FD6: (0x00042FC7, 0x0C, 0x00032EE0),
    0x00043210: (0x00043201, 0x0C, 0x00032EE0),
    0x0004404E: (0x0004403E, 0x0C, 0x00032EE0),
    0x0004436A: (0x0004435A, 0x0C, 0x00032EE0),
    0x0004572D: (0x00045721, 0x0C, 0x00032EE0),
    0x000461D5: (0x000461C6, 0x0C, 0x00032EE0),
    0x000467A5: (0x00046796, 0x0C, 0x00032EE0),
    0x00046F3E: (0x00046F32, 0x0C, 0x00032EE0),
    0x00046FDD: (0x00046FD1, 0x0C, 0x00032EE0),
    0x0004712F: (0x00047125, 0x0C, 0x00032EE0),
    0x00047E7D: (0x00047E71, 0x0C, 0x00032EE0),
    0x000499E9: (0x000499DF, 0x0C, 0x00032EE0),
    0x0004A8E4: (0x0004A8D5, 0x0C, 0x00032EE0),
    0x0004A9B5: (0x0004A9AB, 0x0C, 0x00032EE0),
    0x0004AA17: (0x0004AA0D, 0x0C, 0x00032EE0),
    0x0004AA6C: (0x0004AA5D, 0x0C, 0x00032EE0),
    0x0004AB4A: (0x0004AB3B, 0x0C, 0x00032EE0),
    0x0004C683: (0x0004C677, 0x0C, 0x00032EE0),
    0x0004CC27: (0x0004CC0C, 0x0C, 0x00032EE0),
    0x0004D6F2: (0x0004D6E8, 0x0C, 0x00032EE0),
    0x0004D767: (0x0004D75E, 0x0C, 0x00032EE0),
    0x0004D803: (0x0004D7F9, 0x0C, 0x00032EE0),
    0x00053E60: (0x00053E56, 0x0C, 0x00032EE0),
}
PHASE7_RESOURCE_SELECTION_OBJECT_CALLER_BYTES = {
    0x000387C5: "8B0D60B34C008B04868B1150FF520C",
    0x0003FE26: "8B0D60B34C008B11568B35043135008B34B5083135008B048650FF520C",
    0x00040EAA: "8B0D60B34C008B116A00FF520C",
    0x00041198: "8B0D60B34C008B923C0100008B0152FF500C",
    0x00041830: "8B0D60B34C008B927C0100008B0152FF500C",
    0x00041B37: "8B0D60B34C008B80640100008B1150FF520C",
    0x00041E46: "8B0D60B34C008B84B8CC0700008B1150FF520C",
    0x000426EE: "8B0D60B34C008B80C40900008B1150FF520C",
    0x00042A48: "8B0D60B34C008B8498AC0900008B1150FF520C",
    0x00042EBB: "8B0D60B34C008B4424208B1150FF520C",
    0x00042F90: "8B0D60B34C008B92AC0500008B0152FF500C",
    0x00042FC7: "8B0D60B34C008B80AC0500008B1150FF520C",
    0x00043201: "8B0D60B34C008B928C0500008B0152FF500C",
    0x0004403E: "8B0D60B34C008B018D14928B54960452FF500C",
    0x0004435A: "8B0D60B34C008B118D04808B44870450FF520C",
    0x00045721: "8B0D60B34C008B40548B1150FF520C",
    0x000461C6: "8B0D60B34C008B92A40D00008B0152FF500C",
    0x00046796: "8B0D60B34C008B80780100008B1150FF520C",
    0x00046F32: "8B0D60B34C008B40148B1150FF520C",
    0x00046FD1: "8B0D60B34C008B04868B1150FF520C",
    0x00047125: "8B0D60B34C008B116A00FF520C",
    0x00047E71: "8B0D60B34C008B52608B0152FF500C",
    0x000499DF: "8B0D60B34C008B116A00FF520C",
    0x0004A8D5: "8B0D60B34C008B92040D00008B0152FF500C",
    0x0004A9AB: "8B0D60B34C008B016A00FF500C",
    0x0004AA0D: "8B0D60B34C008B116A00FF520C",
    0x0004AA5D: "8B0D60B34C008B92100D00008B0152FF500C",
    0x0004AB3B: "8B0D60B34C008B800C0D00008B1150FF520C",
    0x0004C677: "8B0D60B34C008B14AA8B0152FF500C",
    0x0004CC0C: "8B0D60B34C008B14952C27350083F8018B01750B8B92F800000052FF500C",
    0x0004D6E8: "8B0D60B34C008B116A00FF520C",
    0x0004D75E: "8B0D60B34C008B0152FF500C",
    0x0004D7F9: "8B0D60B34C008B116A00FF520C",
    0x00053E56: "8B0D60B34C008B116A00FF520C",
}
# The frontend object at 0x004CB33C owns four slot-C calls around the level
# selection transitions.  The accepted Lesson One capsule and the live x86
# boundary at 0x000428B3 agree on the exact object and four-method vtable.
# Only two callers are active in the current normal-live slice, but validate
# all four address-named caller sequences so the recovery remains one closed
# object family rather than a one-off observed target.
PHASE7_LEVEL_SELECT_OBJECT_POINTER_CELL = 0x004CB33C
PHASE7_LEVEL_SELECT_OBJECT = 0x004DABD8
PHASE7_LEVEL_SELECT_OBJECT_VTABLE = 0x002B54E0
PHASE7_LEVEL_SELECT_OBJECT_VTABLE_ENTRIES = (
    0x000A97B0,
    0x00033800,
    0x000D7410,
    0x00033880,
)
PHASE7_LEVEL_SELECT_OBJECT_BINDINGS = {
    0x00042882: (0x00042879, 0x0C, 0x00033880),
    0x000428B3: (0x000428A9, 0x0C, 0x00033880),
}
PHASE7_LEVEL_SELECT_OBJECT_CALLER_BYTES = {
    0x0004272F: "8B0D3CB34C008B0153FF500C",
    0x00042879: "8B0D3CB34C008B1157FF520C",
    0x000428A9: "8B0D3CB34C008B116A00FF520C",
    0x00042A7E: "8B0D3CB34C008B118D44240C50FF520C",
}
# Five fixed manager entries at 0x00489F70 share this eight-method vtable.
# Startup calls slots 0 and 0x1C on entries returned by the base manager.  The
# level-loading status transition follows the same selected entry through slot
# 0x18, while presenter-close reaches slot 0x14 from the slot-0x1C method
# itself. Admit every reached site only with the complete table, fixed array
# initializer, and exact object/argument setup so lifecycle coverage cannot
# become a collection of one-off targets.
PHASE7_BASE_MANAGER_OBJECT_VTABLE = 0x00295D44
PHASE7_BASE_MANAGER_OBJECT_VTABLE_ENTRIES = (
    0x000D95D0,
    0x000D9710,
    0x000D98C0,
    0x000D9A60,
    0x000D9750,
    0x000D9B60,
    0x000D97D0,
    0x000D95B0,
)
PHASE7_BASE_MANAGER_OBJECT_BINDINGS = {
    0x000D5387: (0x1C, 0x000D95B0),
    0x000D5397: (0x18, 0x000D97D0),
    0x000D869F: (0x1C, 0x000D95B0),
    0x000D86CD: (0x00, 0x000D95D0),
    0x000D95BE: (0x14, 0x000D9B60),
    0x000D9983: (0x10, 0x000D9750),
    0x000D99B4: (0x10, 0x000D9750),
    0x000D99ED: (0x10, 0x000D9750),
    0x000D9AE5: (0x10, 0x000D9750),
    0x000D9BDC: (0x10, 0x000D9750),
    0x000D9BFA: (0x10, 0x000D9750),
    0x000D9C1E: (0x08, 0x000D98C0),
}
PHASE7_BASE_MANAGER_OBJECT_INSTRUCTION_BYTES = {
    # Fixed five-entry initializer at 0x00489F70 with 0x50-byte stride.
    0x00139380: "B8709F4800",
    0x00139385: "B905000000",
    0x0013938A: "8D9B00000000",
    0x00139390: "C700445D2900",
    0x00139396: "83C050",
    0x00139399: "49",
    0x0013939A: "75F4",
    0x0013939C: "C3",
    # Base-manager entry selected by the active panel row.
    0x000D537B: "8B8E40080000",
    0x000D5381: "85C9",
    0x000D5383: "7415",
    0x000D5385: "8B11",
    0x000D5387: "FF521C",
    # The status-2 branch calls slot 0x18 on the same selected entry.
    0x000D538F: "8B8E40080000",
    0x000D5395: "8B01",
    0x000D5397: "FF5018",
    # Returned-object slot 0x1C call.
    0x000D8699: "8BF8",
    0x000D869B: "8B17",
    0x000D869D: "8BCF",
    0x000D869F: "FF521C",
    # Returned-object slot 0 call with its three arguments.
    0x000D86BE: "8B4C2418",
    0x000D86C2: "8B542414",
    0x000D86C6: "8B07",
    0x000D86C8: "51",
    0x000D86C9: "52",
    0x000D86CA: "56",
    0x000D86CB: "8BCF",
    0x000D86CD: "FF10",
    # Slot-0x1C method and its shutdown-time slot-0x14 call.
    0x000D95B0: "56",
    0x000D95B1: "8BF1",
    0x000D95B3: "8B4620",
    0x000D95B6: "85C0",
    0x000D95B8: "7407",
    0x000D95BA: "8B06",
    0x000D95BC: "6A00",
    0x000D95BE: "FF5014",
    0x000D95C1: "8B4620",
    0x000D95C4: "5E",
    0x000D95C5: "C3",
    # Remaining calls made by methods in the same complete vtable.
    0x000D996E: "8B16",
    0x000D9970: "6A01",
    0x000D9979: "53",
    0x000D997A: "50",
    0x000D997B: "8BCE",
    0x000D9983: "FF5210",
    0x000D99AC: "8B16",
    0x000D99AE: "6A01",
    0x000D99B0: "53",
    0x000D99B1: "50",
    0x000D99B2: "8BCE",
    0x000D99B4: "FF5210",
    0x000D99E1: "8B4C240C",
    0x000D99E5: "8B06",
    0x000D99E7: "6A01",
    0x000D99E9: "53",
    0x000D99EA: "51",
    0x000D99EB: "8BCE",
    0x000D99ED: "FF5010",
    0x000D9ADE: "8B16",
    0x000D9AE0: "6A01",
    0x000D9AE2: "6A00",
    0x000D9AE4: "50",
    0x000D9AE5: "FF5210",
    0x000D9BC9: "8B442408",
    0x000D9BCD: "8B16",
    0x000D9BCF: "6A01",
    0x000D9BD1: "57",
    0x000D9BD2: "50",
    0x000D9BD3: "8BCE",
    0x000D9BDC: "FF5210",
    0x000D9BEE: "8B442408",
    0x000D9BF2: "8B16",
    0x000D9BF4: "6A01",
    0x000D9BF6: "57",
    0x000D9BF7: "50",
    0x000D9BF8: "8BCE",
    0x000D9BFA: "FF5210",
    0x000D9C15: "8B16",
    0x000D9C17: "50",
    0x000D9C18: "8B464C",
    0x000D9C1B: "50",
    0x000D9C1C: "8BCE",
    0x000D9C1E: "FF5208",
}
# The active game-state cell is selected from a fixed 16-entry registry.  One
# registry entry is intentionally null; the other 15 entries point at static
# objects whose complete 16-slot virtual interfaces are present in the boot
# image.  State transitions share one reviewed dispatcher, while ordinary
# users load the active object directly from 0x0034AB5C.  Recover the complete
# registry and all locally proven active-state dispatches together so entering
# gameplay does not encounter this finite interface one method at a time.
PHASE7_GAME_STATE_OBJECTS = (
    (
        0x00,
        0x002FE318,
        0x00300268,
        0x00295BBC,
        (
            0x00013060,
            0x00013080,
            0x00013370,
            0x000163B0,
            0x000133B0,
            0x000144F0,
            0x000D7400,
            0x000D7400,
            0x00014160,
            0x00014200,
            0x00014280,
            0x000198F0,
            0x000163E0,
            0x00016420,
            0x000163B0,
            0x000163C0,
        ),
    ),
    (
        0x01,
        0x002FE31C,
        0x003002D0,
        0x00295BE8,
        (
            0x000198F0,
            0x000163E0,
            0x00016420,
            0x000163B0,
            0x000163C0,
            0x000144F0,
            0x00016390,
            0x000163A0,
            0x00014160,
            0x00014200,
            0x00016000,
            0x00015580,
            0x00015590,
            0x000156F0,
            0x00015730,
            0x00015700,
        ),
    ),
    (
        0x02,
        0x002FE320,
        0x00300328,
        0x0029590C,
        (
            0x000198F0,
            0x00015E90,
            0x00015F30,
            0x000163B0,
            0x000163C0,
            0x000144F0,
            0x00016390,
            0x000163A0,
            0x00014160,
            0x00014200,
            0x00016000,
            0x00016500,
            0x00016520,
            0x00016580,
            0x00016650,
            0x00016840,
        ),
    ),
    (
        0x03,
        0x002FE324,
        0x003003D8,
        0x00295C40,
        (
            0x00016140,
            0x00016160,
            0x000162C0,
            0x000163B0,
            0x000163C0,
            0x000144F0,
            0x00016390,
            0x000163A0,
            0x00014160,
            0x00014200,
            0x00016000,
            0x00015580,
            0x00015590,
            0x000156F0,
            0x00015730,
            0x00015700,
        ),
    ),
    (
        0x04,
        0x002FE328,
        0x00300488,
        0x00295C98,
        (
            0x00016140,
            0x00016160,
            0x000162C0,
            0x000163B0,
            0x000163C0,
            0x000144F0,
            0x00016390,
            0x000163A0,
            0x00014160,
            0x00014200,
            0x00016000,
            0x000198F0,
            0x0001A840,
            0x0001A880,
            0x000163B0,
            0x0001A820,
        ),
    ),
    (
        0x05,
        0x002FE32C,
        0x00300380,
        0x00295C14,
        (
            0x00015580,
            0x00015590,
            0x000156F0,
            0x00015730,
            0x00015700,
            0x000144F0,
            0x00015710,
            0x00015720,
            0x00015740,
            0x00015750,
            0x00015760,
            0x00016140,
            0x00016160,
            0x000162C0,
            0x000163B0,
            0x000163C0,
        ),
    ),
    (
        0x06,
        0x002FE330,
        0x00300430,
        0x00295C6C,
        (
            0x00015580,
            0x00015590,
            0x000156F0,
            0x00015730,
            0x00015700,
            0x000144F0,
            0x00015710,
            0x00015720,
            0x00015740,
            0x00015750,
            0x00015760,
            0x00016140,
            0x00016160,
            0x000162C0,
            0x000163B0,
            0x000163C0,
        ),
    ),
    (
        0x07,
        0x002FE334,
        0x00300540,
        0x00295964,
        (
            0x00014C30,
            0x00014C80,
            0x00014D00,
            0x000163B0,
            0x00014DE0,
            0x000144F0,
            0x00015100,
            0x000163A0,
            0x00015740,
            0x00015110,
            0x00015130,
            0x00015190,
            0x000152D0,
            0x000152F0,
            0x00015350,
            0x000163B0,
        ),
    ),
    (
        0x08,
        0x002FE338,
        0x003004E0,
        0x00295938,
        (
            0x00016500,
            0x00016520,
            0x00016580,
            0x00016650,
            0x00016840,
            0x000144F0,
            0x00014130,
            0x00014150,
            0x00014160,
            0x00014200,
            0x00016660,
            0x00014C30,
            0x00014C80,
            0x00014D00,
            0x000163B0,
            0x00014DE0,
        ),
    ),
    (0x09, 0x002FE33C, 0x00000000, 0x00000000, ()),
    (
        0x0A,
        0x002FE340,
        0x003006A0,
        0x00295CC4,
        (
            0x000198F0,
            0x0001A840,
            0x0001A880,
            0x000163B0,
            0x0001A820,
            0x000144F0,
            0x00016390,
            0x000163A0,
            0x00014160,
            0x00014200,
            0x0001A950,
            0x000172D0,
            0x000172D0,
            0x000172E0,
            0x000172F0,
            0x00017310,
        ),
    ),
    (
        0x0B,
        0x002FE344,
        0x003006F8,
        0x002959C0,
        (
            0x000198F0,
            0x00019910,
            0x00019950,
            0x000163B0,
            0x0001A820,
            0x000144F0,
            0x00016390,
            0x000163A0,
            0x00014160,
            0x00014200,
            0x0001A950,
            0x00016140,
            0x0001A700,
            0x0001A740,
            0x000163B0,
            0x0001A820,
        ),
    ),
    (
        0x0C,
        0x002FE348,
        0x00300750,
        0x002959EC,
        (
            0x00016140,
            0x0001A700,
            0x0001A740,
            0x000163B0,
            0x0001A820,
            0x000144F0,
            0x00016390,
            0x000163A0,
            0x00014160,
            0x00014200,
            0x0001A950,
            0x00019140,
            0x00019180,
            0x00019240,
            0x000192E0,
            0x00019290,
        ),
    ),
    (
        0x0D,
        0x002FE34C,
        0x003007A8,
        0x00295A18,
        (
            0x00019140,
            0x00019180,
            0x00019240,
            0x000192E0,
            0x00019290,
            0x00019320,
            0x00019370,
            0x000193B0,
            0x000193F0,
            0x00019430,
            0x00019470,
            0x000194B0,
            0x00019810,
            0x000194C0,
            0x00019530,
            0x00019570,
        ),
    ),
    (
        0x0E,
        0x002FE350,
        0x003021A8,
        0x00295A60,
        (
            0x00018C70,
            0x00018D00,
            0x00018D50,
            0x000192E0,
            0x00018D80,
            0x00019320,
            0x00019370,
            0x000193B0,
            0x00018DC0,
            0x00018DD0,
            0x00018DE0,
            0x000194B0,
            0x00018E30,
            0x00018F00,
            0x00019010,
            0x00019090,
        ),
    ),
    (
        0x0F,
        0x002FE354,
        0x00300648,
        0x00295994,
        (
            0x000152D0,
            0x000152F0,
            0x00015350,
            0x000163B0,
            0x00015420,
            0x000144F0,
            0x000154B0,
            0x000163A0,
            0x00014160,
            0x00014200,
            0x000154C0,
            0x000198F0,
            0x00019910,
            0x00019950,
            0x000163B0,
            0x0001A820,
        ),
    ),
)
PHASE7_GAME_STATE_ACTIVE_OBJECT_CELL = 0x0034AB5C
PHASE7_GAME_STATE_TRANSITION_CODE_START = 0x00011000
PHASE7_GAME_STATE_TRANSITION_CODE_END = 0x00011172
PHASE7_GAME_STATE_TRANSITION_CODE_SHA256 = (
    "21b3ebf8179c4532ab7b6e4d5b9f09dba4cbf3cc04b794c9282245245d733326"
)
PHASE7_GAME_STATE_REFERENCE_COUNT = 93
PHASE7_GAME_STATE_REFERENCE_SHA256 = (
    "f06a773c9493dddf10bdaa3fd09bf9e72bcf0b91c9032ef924bd85cc3a230553"
)
PHASE7_GAME_STATE_DERIVED_SITE_COUNT = 29
PHASE7_GAME_STATE_DERIVED_SITE_SHA256 = (
    "4bf7b8a9889f9484bc4776e29794412f68b55bbb4b633ceb021992c0573c1034"
)
PHASE7_GAME_STATE_SPECIAL_BINDINGS = {
    0x00011093: (0x04, None),
    0x0001114B: (0x10, None),
    0x00013910: (0x04, 0x00),
}
PHASE7_GAME_STATE_SPECIAL_SITE_BYTES = {
    0x00011093: "FF5004",
    0x0001114B: "FF5010",
    0x00013910: "FF5204",
}
# The shared update helper is entered only through four concrete game-state
# methods.  It retains ``this`` in ESI and performs two late self-dispatches,
# so the ordinary active-object-cell backtrace cannot discover them.  Freeze
# the complete helper body, its exact direct-caller census, and the caller
# prefixes rooted at known game-state vtable entries before admitting both
# dispatches as one finite family.
PHASE7_GAME_STATE_HELPER_START = 0x000157E0
PHASE7_GAME_STATE_HELPER_END = 0x00015DF5
PHASE7_GAME_STATE_HELPER_SHA256 = (
    "35ca5777a7d7d208a778f68dfda542c1d79ba0c088524339fc65e9cadbba235c"
)
PHASE7_GAME_STATE_HELPER_CALLERS = {
    0x00014E5D: (
        0x00014DE0,
        "ecf467ffb080adb984a7e77126674e7c4856f2b7fad78e304dd403bbb56d8173",
    ),
    0x00015423: (
        0x00015420,
        "3e2923dbb4c02bd3f6d554afaab65762183cfb3337f4a2548bbad8258b311ef2",
    ),
    0x000163C3: (
        0x000163C0,
        "773a02784df8368bb918a981443231601300053f4ad5dc179102309cd1631e87",
    ),
    0x00016843: (
        0x00016840,
        "e74d5f35bff5be7ab6670f135e43c2060e734e7b907eb914be37533cbd669c2d",
    ),
}
PHASE7_GAME_STATE_HELPER_STATE_INDEXES = (
    0x00,
    0x01,
    0x02,
    0x03,
    0x04,
    0x05,
    0x06,
    0x07,
    0x08,
    0x0F,
)
PHASE7_GAME_STATE_HELPER_BINDINGS = {
    0x00015D7D: (0x18, "FF5018"),
    0x00015DC3: (0x1C, "FF521C"),
}
PHASE7_GAME_STATE_SITE_COUNT = 34
PHASE7_GAME_STATE_SITE_SHA256 = (
    "46b1d6ddc548251c302bbbe49d2cb3ef3c540286a7df6cc08b5effb96594b20a"
)
PHASE7_GAME_STATE_OBSERVED_BOUNDARY = (0x000127E7, 0x0F, 0x14, 0x000144F0)
# Level loading selects one of five immutable strategy objects through the
# common owner field at +0x18.  The boot image contains every object and its
# complete nine-entry vtable; static startup and the five owner initializers
# are also the only decoded references to these object addresses.  Seal that
# complete reference census and every class-owned caller capsule as one batch
# so level loading does not encounter this finite virtual family one slot at a
# time.  A zero vtable in a binding denotes a common-owner call that may see
# any of the five strategies; the remaining bindings name the exact strategy
# installed by that owner's initializer.
PHASE7_LEVEL_LOAD_STRATEGY_OBJECTS = (
    (
        0x00303BC4,
        0x00295CF0,
        (
            0x000172D0,
            0x000172D0,
            0x000172E0,
            0x000172F0,
            0x00017310,
            0x00017320,
            0x000D7400,
            0x000D7400,
            0x000D7400,
        ),
    ),
    (
        0x00303BD4,
        0x00295D14,
        (
            0x00011910,
            0x00011960,
            0x00011980,
            0x000119D0,
            0x00011A60,
            0x00017320,
            0x000D7400,
            0x00011F50,
            0x00011F50,
        ),
    ),
    (
        0x00303C0C,
        0x00295AA8,
        (
            0x00012790,
            0x00012790,
            0x000148E0,
            0x00014970,
            0x000127C0,
            0x00014940,
            0x000D7400,
            0x000D7400,
            0x000D7400,
        ),
    ),
    (
        0x00303C20,
        0x00295ACC,
        (
            0x00012790,
            0x00012790,
            0x000127A0,
            0x00012860,
            0x000127C0,
            0x00012800,
            0x000D7400,
            0x00012850,
            0x000D7400,
        ),
    ),
    (
        0x00303C34,
        0x00295AF0,
        (
            0x00018560,
            0x00018590,
            0x000186F0,
            0x000185F0,
            0x00018A30,
            0x000186D0,
            0x000D7400,
            0x00018840,
            0x00018870,
        ),
    ),
)
PHASE7_LEVEL_LOAD_STRATEGY_BINDINGS = {
    # Common-owner reset/update calls can hold any installed strategy.
    0x00013F7D: (0x00013F51, 0x00000000, 0x04),
    0x00014022: (0x0001401D, 0x00000000, 0x04),
    0x000143CE: (0x000143B6, 0x00000000, 0x08),
    # Owner initialized with the object at 0x00303C20.
    0x00014D5B: (0x00014D56, 0x00295ACC, 0x1C),
    0x00014E0E: (0x00014E01, 0x00295ACC, 0x0C),
    0x00014E89: (0x00014E7C, 0x00295ACC, 0x0C),
    # Owner initialized with the object at 0x00303C0C.
    0x000153AB: (0x000153A6, 0x00295AA8, 0x1C),
    0x00015563: (0x0001555E, 0x00295AA8, 0x04),
    0x00015962: (0x00015959, 0x00295AA8, 0x14),
    0x000159E3: (0x000159DA, 0x00295AA8, 0x14),
    # Owner initialized with the object at 0x00303BD4.
    0x00015F95: (0x00015F90, 0x00295D14, 0x1C),
    0x0001631B: (0x00016316, 0x00295D14, 0x1C),
    0x0001647B: (0x00016476, 0x00295D14, 0x1C),
    # Owner initialized with the object at 0x00303C34.
    0x000165DB: (0x000165D6, 0x00295AF0, 0x1C),
    # Owner initialized with the object at 0x00303BC4.
    0x000199AB: (0x000199A6, 0x00295CF0, 0x1C),
    0x00019B4B: (0x00019B45, 0x00295CF0, 0x10),
    0x00019DFF: (0x00019DF6, 0x00295CF0, 0x14),
    0x0001A349: (0x0001A33D, 0x00295CF0, 0x0C),
    0x0001A376: (0x0001A365, 0x00295CF0, 0x0C),
    0x0001A386: (0x0001A365, 0x00295CF0, 0x0C),
    0x0001A79B: (0x0001A796, 0x00295CF0, 0x1C),
    0x0001A8DB: (0x0001A8D6, 0x00295CF0, 0x1C),
}
PHASE7_LEVEL_LOAD_STRATEGY_CALLER_BYTES = {
    0x00013F51: "8B4E18897E08897E0C897E10897E14897E1C897E20897E48897E4C897E28897E243BCF5F89462C5E74058B11FF6204",
    0x0001401D: "8B4E188B01FF5004",
    0x000143B6: "8B4E183BCB897E44895E08895E0C897E1C897E2074058B11FF5208",
    0x00014D56: "8B4E188B11FF521C",
    0x00014E01: "8B4E188B016A006A016A006A00FF500C",
    0x00014E7C: "8B4E188B016A006A006A006A00FF500C",
    0x000153A6: "8B4E188B11FF521C",
    0x0001555E: "8B4E188B11FF5204",
    0x00015959: "8B44240C8B48188B11FF5214",
    0x000159DA: "8B44240C8B48188B11FF5214",
    0x00015F90: "8B4E188B11FF521C",
    0x00016316: "8B4E188B11FF521C",
    0x00016476: "8B4E188B11FF521C",
    0x000165D6: "8B4E188B11FF521C",
    0x000199A6: "8B4E188B11FF521C",
    0x00019B45: "8B4E188B1157FF5210",
    0x00019DF6: "8B44240C8B48188B11FF5214",
    0x0001A33D: "8BD18B4A188B116A006A0050FF520C",
    0x0001A365: "8B74240C8B4E188B016A006A006A006A00FF500C8B4E186A006A006A016A018B11FF520C",
    0x0001A796: "8B4E188B11FF521C",
    0x0001A8D6: "8B4E188B11FF521C",
}
PHASE7_LEVEL_LOAD_STRATEGY_SITE_BYTES = {
    0x00013F7D: "FF6204",
    0x00014022: "FF5004",
    0x000143CE: "FF5208",
    0x00014D5B: "FF521C",
    0x00014E0E: "FF500C",
    0x00014E89: "FF500C",
    0x000153AB: "FF521C",
    0x00015563: "FF5204",
    0x00015962: "FF5214",
    0x000159E3: "FF5214",
    0x00015F95: "FF521C",
    0x0001631B: "FF521C",
    0x0001647B: "FF521C",
    0x000165DB: "FF521C",
    0x000199AB: "FF521C",
    0x00019B4B: "FF5210",
    0x00019DFF: "FF5214",
    0x0001A349: "FF520C",
    0x0001A376: "FF500C",
    0x0001A386: "FF520C",
    0x0001A79B: "FF521C",
    0x0001A8DB: "FF521C",
}
PHASE7_LEVEL_LOAD_STRATEGY_INSTALLER_BYTES = {
    0x00013839: "B9C43B3000E88D3A0000B9D43B3000E8C3E0FFFFB9343C3000E8094D0000B90C3C3000E82FEFFFFFB9203C3000E825EFFFFF",
    0x00014C30: "53568BF1E8370B000033DB8D4E58899EE4000000C74618203C3000E8D0D9FFFF",
    0x000152D0: "568BF1E898040000C746180C3C30005EC3",
    0x00016140: "568BF1E828F6FFFFC74618D43B30005EC3",
    0x00016500: "568BF1E868F2FFFFB9343C3000894E18A1343C30005EFF20",
    0x000198F0: "568BF1E878BEFFFFC74618C43B30005EC3",
    0x00042960: "B90C3C3000E826FEFCFF",
}
PHASE7_LEVEL_LOAD_STRATEGY_REFERENCE_BYTES = {
    0x00013839: "B9C43B3000",
    0x00013843: "B9D43B3000",
    0x0001384D: "B9343C3000",
    0x00013857: "B90C3C3000",
    0x00013861: "B9203C3000",
    0x00014C44: "C74618203C3000",
    0x000152D8: "C746180C3C3000",
    0x00016148: "C74618D43B3000",
    0x00016508: "B9343C3000",
    0x00016510: "A1343C3000",
    0x000198F8: "C74618C43B3000",
    0x00042960: "B90C3C3000",
}
PHASE7_LEVEL_LOAD_STRATEGY_REFERENCES = {
    0x00303BC4: frozenset((0x00013839, 0x000198F8)),
    0x00303BD4: frozenset((0x00013843, 0x00016148)),
    0x00303C0C: frozenset((0x00013857, 0x000152D8, 0x00042960)),
    0x00303C20: frozenset((0x00013861, 0x00014C44)),
    0x00303C34: frozenset((0x0001384D, 0x00016508, 0x00016510)),
}
# The level-load parameter owner installs a fixed bank of 32 statically
# initialized objects, one at every dword field from +0x10 through +0x8C.
# Three generic walkers consume all slot-0/slot-4/slot-8 entries, followed by
# 142 direct slot-4 setters in the same closed code region.  The complete code
# hash and derived direct-site census keep this large family exact without a
# brittle one-boundary-at-a-time target list.
PHASE7_LEVEL_PARAMETER_BANK_OBJECTS = (
    (0x10, 0x0030F61C, 0x002B08B8, (0x00021570, 0x00026700, 0x00026740)),
    (0x14, 0x0030F840, 0x002B08B8, (0x00021570, 0x00026700, 0x00026740)),
    (0x18, 0x0030F784, 0x002B0818, (0x0001E450, 0x0001E460, 0x0001E540)),
    (0x1C, 0x0030F9A4, 0x002B0818, (0x0001E450, 0x0001E460, 0x0001E540)),
    (0x20, 0x0030F664, 0x002B07DC, (0x00021570, 0x00023850, 0x00023880)),
    (0x24, 0x0030F888, 0x002B07DC, (0x00021570, 0x00023850, 0x00023880)),
    (0x28, 0x0030F6D8, 0x002B0800, (0x00021570, 0x000231A0, 0x00023290)),
    (0x2C, 0x0030F8F8, 0x002B0800, (0x00021570, 0x000231A0, 0x00023290)),
    (0x30, 0x0030F64C, 0x002B07D0, (0x0001E450, 0x00023EB0, 0x00024050)),
    (0x34, 0x0030F870, 0x002B07D0, (0x0001E450, 0x00023EB0, 0x00024050)),
    (0x38, 0x0030F684, 0x002B07E8, (0x00023990, 0x000239A0, 0x00023B30)),
    (0x3C, 0x0030F8A8, 0x002B07E8, (0x00023990, 0x000239A0, 0x00023B30)),
    (0x40, 0x0030F6A4, 0x002B07F4, (0x000371B0, 0x0001EA50, 0x0001EBA0)),
    (0x44, 0x0030F8C8, 0x002B07F4, (0x000371B0, 0x0001EA50, 0x0001EBA0)),
    (0x48, 0x0030F740, 0x002B080C, (0x0001E450, 0x00027990, 0x00027A20)),
    (0x4C, 0x0030F960, 0x002B080C, (0x0001E450, 0x00027990, 0x00027A20)),
    (0x50, 0x0030F7A8, 0x002B0824, (0x0001D810, 0x0001D830, 0x0001D980)),
    (0x54, 0x0030F9C8, 0x002B0824, (0x0001D810, 0x0001D830, 0x0001D980)),
    (0x58, 0x0030F82C, 0x002B0830, (0x00021570, 0x00021580, 0x00021650)),
    (0x5C, 0x0030FA4C, 0x002B0830, (0x00021570, 0x00021580, 0x00021650)),
    (0x60, 0x00310180, 0x002B088C, (0x0001FE90, 0x0001FF20, 0x00020040)),
    (0x64, 0x003102B0, 0x002B088C, (0x0001FE90, 0x0001FF20, 0x00020040)),
    (0x68, 0x003103E0, 0x002B089C, (0x0001E450, 0x000202E0, 0x00020420)),
    (0x6C, 0x00310400, 0x002B089C, (0x0001E450, 0x000202E0, 0x00020420)),
    (0x70, 0x00310420, 0x002B08A8, (0x0001E450, 0x00028340, 0x00028430)),
    (0x74, 0x00310540, 0x002B08A8, (0x0001E450, 0x00028340, 0x00028430)),
    (0x78, 0x0030FA88, 0x002B0854, (0x0001F080, 0x0001F200, 0x0001F3B0)),
    (0x7C, 0x0030FFC8, 0x002B0864, (0x0001E450, 0x0001F790, 0x0001F870)),
    (0x80, 0x00310100, 0x002B0874, (0x00021EA0, 0x00021F50, 0x000222E0)),
    (0x84, 0x00310164, 0x002B0880, (0x00021570, 0x00021CC0, 0x00021E40)),
    (0x88, 0x0030FA60, 0x002B083C, (0x00021570, 0x00027F00, 0x00027FA0)),
    (0x8C, 0x0030FA74, 0x002B0848, (0x00021570, 0x00024080, 0x000D7410)),
)
PHASE7_LEVEL_PARAMETER_BANK_CODE_START = 0x0001AA10
PHASE7_LEVEL_PARAMETER_BANK_CODE_END = 0x0001CFD6
PHASE7_LEVEL_PARAMETER_BANK_CODE_SHA256 = (
    "b2143380234d2c2d73fd07da2b3699b8517e52074140d48cf8e2f789ecb531a2"
)
PHASE7_LEVEL_PARAMETER_BANK_GENERIC_BINDINGS = {
    0x0001AB3F: 0x00,
    0x0001ABEB: 0x08,
    0x0001AC27: 0x04,
}
PHASE7_LEVEL_PARAMETER_BANK_SETTER_START = 0x0001AC60
PHASE7_LEVEL_PARAMETER_BANK_SETTER_SITE_COUNT = 142
PHASE7_LEVEL_PARAMETER_BANK_SETTER_SITE_SHA256 = (
    "43c4f4c8280b909b7fbcb5f3323d35a5221717aac585a69bba44e9caf20322bf"
)
PHASE7_LEVEL_PARAMETER_BANK_OBSERVED_BOUNDARY = (0x0001B200, 0x10, 0x00026700)
# The loading screen then operates on two four-entry fixed manager arrays.
# Their startup loops install three related 20-entry vtables, one 11-entry
# primary table, and an adjacent 11-entry secondary table.  The retained
# Lesson One capsule contains all 20 installed object-table cells exactly as
# produced by those loops.  The common tables have identical targets in every
# self-dispatched slot below; the primary and embedded-secondary sites name
# their exact installed table.
PHASE7_LEVEL_LOAD_MANAGER_COMMON_VTABLES = (
    0x00295D68,
    0x00295DB8,
    0x00295E08,
)
PHASE7_LEVEL_LOAD_MANAGER_VTABLES = (
    (
        0x00295D68,
        (
            0x0005E360,
            0x00064C60,
            0x0005E740,
            0x0005EB40,
            0x0005EB50,
            0x00066410,
            0x0005FF60,
            0x0005FFC0,
            0x000604E0,
            0x000605B0,
            0x0005B670,
            0x00058F00,
            0x0005A6B0,
            0x00059120,
            0x00059380,
            0x0005B950,
            0x0005BB80,
            0x00059770,
            0x00059DB0,
            0x00060820,
        ),
    ),
    (
        0x00295DB8,
        (
            0x0005D860,
            0x00064C60,
            0x0005D8E0,
            0x0005DA00,
            0x0005DA20,
            0x00066410,
            0x0005FF60,
            0x0005FFC0,
            0x000604E0,
            0x000605B0,
            0x0005B670,
            0x00058F00,
            0x0005A6B0,
            0x00059120,
            0x00059380,
            0x0005B950,
            0x0005BB80,
            0x00059770,
            0x00059DB0,
            0x0005DD00,
        ),
    ),
    (
        0x00295E08,
        (
            0x000558D0,
            0x00064C60,
            0x00055970,
            0x0005EB40,
            0x0005EB50,
            0x00066410,
            0x0005FF60,
            0x0005FFC0,
            0x000604E0,
            0x000605B0,
            0x0005B670,
            0x00058F00,
            0x0005A6B0,
            0x00059120,
            0x00059380,
            0x0005B950,
            0x0005BB80,
            0x00059770,
            0x00059DB0,
            0x00055B50,
        ),
    ),
    (
        0x00295E58,
        (
            0x000706C0,
            0x00073150,
            0x00070740,
            0x00070800,
            0x00074000,
            0x00073AD0,
            0x00072E10,
            0x000732D0,
            0x00070C80,
            0x00070CC0,
            0x00070D10,
        ),
    ),
    (
        0x00295E84,
        (
            0x0007E7C0,
            0x0007F9F0,
            0x0007ECD0,
            0x0007EDF0,
            0x0007F4E0,
            0x000D7410,
            0x0007EE80,
            0x000732D0,
            0x00070C80,
            0x00070CC0,
            0x00070D10,
        ),
    ),
)
PHASE7_LEVEL_LOAD_MANAGER_OBJECT_BINDINGS = tuple(
    binding
    for index in range(4)
    for binding in (
        (0x004BCCE0 + index * 0x4570 - 0x2100, 0x00295E08),
        (0x004BCCE0 + index * 0x4570 - 0x3090, 0x00295E84),
        (0x004BCCE0 + index * 0x4570, 0x00295DB8),
        (0x00464220 + index * 0x3090, 0x00295E58),
        (0x00464220 + index * 0x3090 + 0x0F90, 0x00295E08),
    )
)
PHASE7_LEVEL_LOAD_MANAGER_BINDINGS = {
    # Self-dispatches shared by all three 20-entry manager tables.
    0x00058F31: (0x00058F2F, 0x00000000, 0x34),
    0x00058F3D: (0x00058F3B, 0x00000000, 0x38),
    0x00058F44: (0x00058F42, 0x00000000, 0x3C),
    0x00058F5D: (0x00058F58, 0x00000000, 0x38),
    0x00058F6C: (0x00058F65, 0x00000000, 0x40),
    0x00058F73: (0x00058F71, 0x00000000, 0x44),
    0x00058F81: (0x00058F7C, 0x00000000, 0x3C),
    0x00058F90: (0x00058F8B, 0x00000000, 0x44),
    0x00058F9A: (0x00058F95, 0x00000000, 0x48),
    0x000590CC: (0x000590C7, 0x00000000, 0x20),
    0x000590D4: (0x000590CF, 0x00000000, 0x20),
    0x00059772: (0x00059770, 0x00000000, 0x40),
    0x0005B90E: (0x0005B8DA, 0x00000000, 0x24),
    0x0005D127: (0x0005D11D, 0x00000000, 0x18),
    0x0005D60E: (0x0005D609, 0x00000000, 0x1C),
    0x0005EAD3: (0x0005EACF, 0x00000000, 0x18),
    # Self-dispatches on the fixed primary level-loader table.
    0x000707EF: (0x000707EB, 0x00295E58, 0x18),
    0x00072F27: (0x00072F0B, 0x00295E58, 0x08),
    0x00073997: (0x00073991, 0x00295E58, 0x24),
    0x000739A3: (0x000739A1, 0x00295E58, 0x20),
    0x00074210: (0x0007420C, 0x00295E58, 0x1C),
    0x00074437: (0x00074433, 0x00295E58, 0x28),
    0x00074451: (0x00074446, 0x00295E58, 0x08),
    # The primary object owns the embedded 20-entry object at +0x0F90.
    0x000743B5: (0x000743A7, 0x00295E08, 0x4C),
    # Fixed four-object lifecycle sweeps rooted at cell array 0x002FE370.
    0x00082222: (0x0008221C, 0x00295E84, 0x08),
    0x00082319: (0x00082310, 0x00295E84, 0x10),
    0x00082485: (0x0008247B, 0x00295DB8, 0x28),
    0x00082709: (0x00082700, 0x00295E84, 0x10),
    0x00082727: (0x0008270C, 0x00295DB8, 0x14),
}
PHASE7_LEVEL_LOAD_MANAGER_CALLER_BYTES = {
    0x00058F2F: "8B06FF5034",
    0x00058F3B: "8B16FF5238",
    0x00058F42: "8B06FF503C",
    0x00058F58: "8B17568BCFFF5238",
    0x00058F65: "8BCE5775078B06FF5040",
    0x00058F71: "8B16FF5244",
    0x00058F7C: "8B07568BCFFF503C",
    0x00058F8B: "8B17568BCFFF5244",
    0x00058F95: "8B06578BCEFF5048",
    0x000590C7: "8B16578BCEFF5220",
    0x000590CF: "8B07568BCFFF5020",
    0x00059770: "8B01FF6040",
    0x0005B8DA: "8B16D88EE00000008BCED99EE0000000D90524BE2B00D88EE4000000D99EE4000000D90524BE2B00D88EE8000000D99EE8000000FF5224",
    0x0005D11D: "8B038BCB89932C020000FF5018",
    0x0005D609: "8B13558BCBFF521C",
    0x0005EACF: "8B038BCBFF5018",
    0x000707EB: "8B038BCBFF5018",
    0x00072F0B: "8B16DDD86A048BCEC7867430000000000000C786680D000001000000FF5208",
    0x00073991: "8BCE740C8B06FF5024",
    0x000739A1: "8B16FF5220",
    0x0007420C: "8B168BCEFF521C",
    0x000743A7: "8B96900F00008D9E900F00008BCBFF524C",
    0x00074433: "8B068BCEFF5028",
    0x00074446: "8B168BCE5789BE600D0000FF5208",
    0x0008221C: "8B066A048BCEFF5008",
    0x00082310: "8B0CBE3BCB74058B01FF5010",
    0x0008247B: "8B04BE8B58088B138BCBFF5228",
    0x00082700: "8B0CBE85C974238B01FF5010",
    0x0008270C: (
        "8B04BE8B88D00C000085C974118B0D888159008B40088B10518BC8FF5214"
    ),
}
PHASE7_LEVEL_LOAD_MANAGER_SITE_BYTES = {
    site: PHASE7_LEVEL_LOAD_MANAGER_CALLER_BYTES[caller_start][
        -6:
    ]
    for site, (caller_start, _vtable, _slot) in PHASE7_LEVEL_LOAD_MANAGER_BINDINGS.items()
}
PHASE7_LEVEL_LOAD_MANAGER_INSTALLER_BYTES = {
    0x00073CC0: "53558B6C240C5657558BD9E8E0FCFFFFC703685D2900",
    0x001392B0: "B8E0CC4B00B904000000BA845E290090C78000DFFFFF085E2900899070CFFFFFC700B85D290005704500004975E2C390B820424600B904000000BA085E290090C700585E29008990900F000005903000004975ECC3",
}
PHASE7_LEVEL_LOAD_MANAGER_REFERENCE_BYTES = {
    0x00073CD0: "C703685D2900",
    0x001392BA: "BA845E2900",
    0x001392C0: "C78000DFFFFF085E2900",
    0x001392D0: "C700B85D2900",
    0x001392EA: "BA085E2900",
    0x001392F0: "C700585E2900",
}
PHASE7_LEVEL_LOAD_MANAGER_REFERENCES = {
    0x00295D68: frozenset((0x00073CD0,)),
    0x00295DB8: frozenset((0x001392D0,)),
    0x00295E08: frozenset((0x001392C0, 0x001392EA)),
    0x00295E58: frozenset((0x001392F0,)),
    0x00295E84: frozenset((0x001392BA,)),
}
PHASE7_LEVEL_LOAD_MANAGER_SWEEP_DIRECT_CALLERS = {
    0x00015B5F: (0x00082100, 0x00015B59, "50B970E32F00E89CC50600"),
    0x00015BBC: (0x00082100, 0x00015BB6, "52B970E32F00E83FC50600"),
    0x0001A0CE: (0x00082100, 0x0001A0C8, "50B970E32F00E82D800600"),
    0x0001A2D6: (0x00082100, 0x0001A2D0, "51B970E32F00E8257E0600"),
    0x0001395E: (0x000822B0, 0x00013959, "B970E32F00E84DE90600"),
}
PHASE7_LEVEL_LOAD_MANAGER_SWEEP_PROOF_BYTES = {
    # 0x00082100 retains ECX in EDI and loads each non-null object into ESI.
    0x00082100: "558BEC83EC0853568B750857568BF9",
    0x00082125: "8B349F",
    # 0x000822B0 retains the fixed cell-array base directly in ESI.
    0x000822B0: "83EC1853568BF1",
}
PHASE7_LEVEL_LOAD_MANAGER_SWEEP_OBSERVED_BOUNDARY = (
    0x00082709,
    0x00295E84,
    0x10,
    0x0007F4E0,
)
# The gameplay update dispatcher at 0x000ADCA0 selects one of nine fixed
# helpers from a 20-state byte selector and ten-entry jump table.  Every helper
# begins by invoking slot 0x18 on the primary level-view object.  The retained
# cell names the first of four statically constructed objects sharing vtable
# 0x00295E84; later race slots use the other three objects with that same
# complete table.  Recover the nine primary calls as one family, but leave the
# two optional 0x002FE374 calls in helpers 0x000AC360/0x000AC9F0 guarded: that
# secondary cell is null in the accepted capsule and has no finite target yet.
PHASE7_LEVEL_UPDATE_PRIMARY_CELL = 0x002FE370
PHASE7_LEVEL_UPDATE_PRIMARY_VTABLE = 0x00295E84
PHASE7_LEVEL_UPDATE_PRIMARY_OBJECTS = tuple(
    object_address
    for object_address, vtable in PHASE7_LEVEL_LOAD_MANAGER_OBJECT_BINDINGS
    if vtable == PHASE7_LEVEL_UPDATE_PRIMARY_VTABLE
)
PHASE7_LEVEL_UPDATE_SELECTOR_ADDRESS = 0x000ADE08
PHASE7_LEVEL_UPDATE_SELECTOR_BYTES = "0001020209090309090909090909040905060708"
PHASE7_LEVEL_UPDATE_JUMP_TABLE_ADDRESS = 0x000ADDE0
PHASE7_LEVEL_UPDATE_JUMP_TABLE = (
    0x000ADCED,
    0x000ADDBF,
    0x000ADCCF,
    0x000ADDA1,
    0x000ADD47,
    0x000ADD0B,
    0x000ADD29,
    0x000ADD65,
    0x000ADD83,
    0x000ADDBF,
)
PHASE7_LEVEL_UPDATE_SWITCH_INSTRUCTION_BYTES = {
    0x000ADCC1: "0FB69008DE0A00",
    0x000ADCC8: "FF2495E0DD0A00",
    0x000ADCDA: "E881C6FFFF",
    0x000ADCF8: "E883D3FFFF",
    0x000ADD16: "E835DCFFFF",
    0x000ADD34: "E837E1FFFF",
    0x000ADD52: "E8C9D6FFFF",
    0x000ADD70: "E8EBE5FFFF",
    0x000ADD8E: "E85DECFFFF",
    0x000ADDAC: "E83FF3FFFF",
    0x000ADDCA: "E841C8FFFF",
}
PHASE7_LEVEL_UPDATE_HELPER_CALLS = {
    0x000ADCDA: (0x000ADCCF, 0x000AA360),
    0x000ADCF8: (0x000ADCED, 0x000AB080),
    0x000ADD16: (0x000ADD0B, 0x000AB950),
    0x000ADD34: (0x000ADD29, 0x000ABE70),
    0x000ADD52: (0x000ADD47, 0x000AB420),
    0x000ADD70: (0x000ADD65, 0x000AC360),
    0x000ADD8E: (0x000ADD83, 0x000AC9F0),
    0x000ADDAC: (0x000ADDA1, 0x000AD0F0),
    0x000ADDCA: (0x000ADDBF, 0x000AA610),
}
PHASE7_LEVEL_UPDATE_PRIMARY_CALLER_BYTES = {
    0x000AA3A3: "8B40088B5104568944242C8B0157895C241C89542414FF5018",
    0x000AA621: "8B4D088B41048945F88B450C89570489078B11FF5218",
    0x000AB0C3: "8B40088B5104568944242C8B015789542418FF5018",
    0x000AB438: "8B08578B79048B4D088B11897C24388D5F10FF5218",
    0x000AB9BA: (
        "8B40088B1156578944242CC74424480000803F"
        "C744244C00000000C744245000000000FF5218"
    ),
    0x000ABED4: (
        "8B4D088B01565789542428C74424480000803F"
        "C744244C00000000C744245000000000FF5018"
    ),
    0x000AC3BB: (
        "8B50088B01578954243CC744242000000000"
        "C74424240000803FC744242800000000FF5018"
    ),
    0x000ACA4B: (
        "8B50088B015789542440C744242000000000"
        "C74424240000803FC744242800000000FF5018"
    ),
    0x000AD106: "8B4D088B118975DC8945FC83C610FF5218",
}
PHASE7_LEVEL_UPDATE_PRIMARY_BINDINGS = {
    0x000AA3B9: (0x000AA360, 0x000AA3A3),
    0x000AA634: (0x000AA610, 0x000AA621),
    0x000AB0D5: (0x000AB080, 0x000AB0C3),
    0x000AB44A: (0x000AB420, 0x000AB438),
    0x000AB9DD: (0x000AB950, 0x000AB9BA),
    0x000ABEF7: (0x000ABE70, 0x000ABED4),
    0x000AC3DD: (0x000AC360, 0x000AC3BB),
    0x000ACA6D: (0x000AC9F0, 0x000ACA4B),
    0x000AD114: (0x000AD0F0, 0x000AD106),
}
PHASE7_LEVEL_UPDATE_SECONDARY_GUARDED_SITES = frozenset((0x000AC3F8, 0x000ACA85))
PHASE7_LEVEL_UPDATE_OBSERVED_BOUNDARIES = frozenset(
    ((0x000AA3B9, 0x0007EE80), (0x000AB0D5, 0x0007EE80))
)
# The per-racer sample routine at 0x0009D160 walks the same four fixed primary
# cells and queries the slot-0x18 position object three times per non-null
# cell.  Its only direct caller is the racer loop at 0x00097AB4.  Recover all
# three calls together, freezing the loop setup/tail and complete caller census
# so an observed coordinate query cannot become a general vtable allowance.
PHASE7_LEVEL_SAMPLE_PRIMARY_BINDINGS = {
    0x0009D679: "edx",
    0x0009D697: "eax",
    0x0009D6B5: "edx",
}
PHASE7_LEVEL_SAMPLE_DIRECT_CALLS = {0x00097AB4: 0x0009D160}
PHASE7_LEVEL_SAMPLE_PROOF_BYTES = {
    0x00097AA2: "8574242474158B8B9801000085C9740B8BCBE8A7560000",
    0x0009D160: "83EC48538BD98B83A001000085C00F8594060000",
    0x0009D652: (
        "33F6BFCC2E4B0089742410897C24148B0CB570E32F0085C90F8474010000"
        "D943308B11D95C240CFF5218D940308B0CB570E32F00D86C240CD95C243C"
        "D943348B01D95C240CFF5018D940348B0CB570E32F00D86C240CD95C2440"
        "D943388B11D95C240CFF5218"
    ),
    0x0009D7E4: (
        "8B54241083C71081C2FE0000004681FF0C2F4B0089542410897C2414"
        "0F8C5BFEFFFF5F5E5B83C448C3"
    ),
}
PHASE7_LEVEL_SAMPLE_OBSERVED_BOUNDARY = (
    0x0009D679,
    0x004B9C50,
    PHASE7_LEVEL_UPDATE_PRIMARY_VTABLE,
    0x0007EE80,
)
# The fixed level-lifecycle sweep calls one query owner rooted at
# 0x002FE370 + 0x1A44.  Its sole worker walks the same four primary cells and
# calls slot 0x18 on each non-null object.  A later call in that query routine
# uses slot 4 on one of twenty fixed records constructed at 0x0050D890.  Seal
# the complete call chain and both finite object families so the later sibling
# is recovered ahead of gameplay rather than after a second guard.
PHASE7_LEVEL_QUERY_OWNER = 0x002FFDB4
PHASE7_LEVEL_QUERY_RECORD_BASE_CELL = 0x004B8464
PHASE7_LEVEL_QUERY_RECORD_COUNT_CELL = 0x004B8468
PHASE7_LEVEL_QUERY_RECORD_BASE = 0x0050D890
PHASE7_LEVEL_QUERY_RECORD_CAPACITY = 20
PHASE7_LEVEL_QUERY_RECORD_STRIDE = 0x1C
PHASE7_LEVEL_QUERY_RECORD_VTABLE = 0x002BF3A0
PHASE7_LEVEL_QUERY_RECORD_VTABLE_ENTRIES = (
    0x0009F640,
    0x0009F660,
    0x0007C7B0,
    0x000943D0,
)
PHASE7_LEVEL_QUERY_POOL_BASE_CELL = 0x004B846C
PHASE7_LEVEL_QUERY_POOL_BASE = 0x0050DAC0
PHASE7_LEVEL_QUERY_POOL_CAPACITY = 0x100
PHASE7_LEVEL_QUERY_POOL_STRIDE = 0x18
PHASE7_LEVEL_QUERY_POOL_VTABLE = 0x002BF3EC
PHASE7_LEVEL_QUERY_POOL_VTABLE_ENTRIES = (
    0x0009F550,
    0x0009F570,
    0x0009F550,
    0x0009F570,
)
PHASE7_LEVEL_QUERY_BINDINGS = {
    0x000798B5: (PHASE7_LEVEL_UPDATE_PRIMARY_VTABLE, 0x18, 0x0007EE80),
    0x0007994D: (PHASE7_LEVEL_QUERY_RECORD_VTABLE, 0x04, 0x0009F660),
    0x0009F562: (PHASE7_LEVEL_QUERY_POOL_VTABLE, 0x04, 0x0009F570),
    0x0009F650: (PHASE7_LEVEL_QUERY_RECORD_VTABLE, 0x04, 0x0009F660),
    0x0009F6AC: (PHASE7_LEVEL_QUERY_POOL_VTABLE, 0x04, 0x0009F570),
}
PHASE7_LEVEL_QUERY_SITE_BASES = {
    0x000798B5: "edx",
    0x0007994D: "edx",
    0x0009F562: "eax",
    0x0009F650: "eax",
    0x0009F6AC: "eax",
}
PHASE7_LEVEL_QUERY_DIRECT_CALLS = {
    0x0001395E: 0x000822B0,
    0x00082741: 0x0009F9B0,
    0x0009FA60: 0x00079880,
}
PHASE7_LEVEL_QUERY_PROOF_BYTES = {
    0x00013959: "B970E32F00E84DE90600",
    0x000822B0: "83EC1853568BF1",
    0x0008273B: "8D8E441A0000E86AD20100",
    0x0009F9B0: (
        "83EC1053568BF18B46045733FF85C07E438D5E088B0BE815FCFFFF85C0742A"
        "8B4E0433C04985C97E158D4E088D6424008B560483C00283C1084A3BC27CF2"
        "8B56044A4F89560483EB048B46044783C3043BF87CC0A168844B0085C00F84"
        "DE01000033DB8B068B3C0385FF747E8B473C85C074268B87D00C000085C075"
        "6DB988FD2F00E809790300D905081F2C00D84714DED9DFE0F6C44175518B15"
        "64844B008D4C2418518B8F840300006BC91C5703CAE81B9EFDFF"
    ),
    0x00079880: (
        "83EC0C568BF1578B7C24188B874406000033C985C00F9EC157897E184923C1"
        "8BCE894614E8175E020085C00F84D60000008B178BCFFF521883C0308B088B"
        "50048B4008894C24088B4E04D9018954240CD864240889442410D94104D864"
        "240CD94108D8642410D9411CD9C1D8CAD9C3D8CCDEC1D9C4D8CDDEC1D9C1"
        "D8CADED9DFE0DDD8F6C4017579D94114D8CAD94110D8CCDEC1D94118D8CA"
        "DEC1D81D9C432900DDD8DFE0DDD8F6C405DDD87A598B46143B41207D518B"
        "4C812485C98B44241C741B8B49048B1189108B168BCEFF5204"
    ),
    0x0007A20C: "C70564844B0090D85000",
    0x0007A278: (
        "8D4304EB038D49008B0883F9FF740E8D0C498D0CCDC0DA50008908EB06C700"
        "000000008B0B4283C0043BD17CDB"
    ),
    0x0007A2D1: "C7056C844B00C0DA5000",
    0x0009F550: "8B44240C3B4110750C8B5424088B01895114FF5004",
    0x0009F640: "8B44240C8B5424088941148B01895118FF5004",
    0x0009F660: (
        "568BF1B988FD2F00E8237D03008B4E048946088B46148B4C812485C974318B"
        "56188B41048951148B500485D27E1C8B560889510851B9B4FD2F00E8F10200"
        "008B4E188B512089560C5EC38B01FF5004"
    ),
    0x001F4910: (
        "B8C0DA5000B9000100008D9B00000000C700ECF32B0083C0184975F4C3"
    ),
    0x001F4950: (
        "B890D85000B9140000008D9B00000000C700A0F32B0083C01C4975F4C3"
    ),
}
PHASE7_LEVEL_QUERY_RECORD_BASE_REFERENCES = frozenset((0x0007A20C, 0x001F4950))
PHASE7_LEVEL_QUERY_RECORD_VTABLE_REFERENCES = frozenset((0x001F4960,))
PHASE7_LEVEL_QUERY_POOL_BASE_REFERENCES = frozenset((0x0007A2D1, 0x001F4910))
PHASE7_LEVEL_QUERY_POOL_VTABLE_REFERENCES = frozenset((0x001F4920,))
PHASE7_LEVEL_QUERY_OBSERVED_BOUNDARY = (
    0x000798B5,
    0x004B9C50,
    PHASE7_LEVEL_UPDATE_PRIMARY_VTABLE,
    0x0007EE80,
)
PHASE7_LEVEL_QUERY_PREDICTED_BOUNDARY = (
    0x0007994D,
    PHASE7_LEVEL_QUERY_RECORD_BASE,
    PHASE7_LEVEL_QUERY_RECORD_VTABLE,
    0x0009F660,
)
# A late level-load initializer installs six fixed callbacks in an adjacent
# global vector.  Two worker paths consume the same three cells, while a third
# path consumes the fourth.  Validate the complete installer, all six cells,
# and the exact seven-consumer reference census before recovering any site.
PHASE7_RUNTIME_CALLBACK_VECTOR = (
    (0x00347EB4, 0x0012186B),
    (0x00347EB8, 0x00121516),
    (0x00347EBC, 0x0012157B),
    (0x00347EC0, 0x001214BE),
    (0x00347EC4, 0x00121561),
    (0x00347EC8, 0x0012186B),
)
PHASE7_RUNTIME_CALLBACK_BINDINGS = {
    0x00121EAA: (0x00121E87, 0x00347EB4, 0x0012186B),
    0x00121EC5: (0x00121E87, 0x00347EC0, 0x001214BE),
    0x00121ED6: (0x00121E87, 0x00347EB8, 0x00121516),
    0x00124A48: (0x00124A25, 0x00347EB4, 0x0012186B),
    0x00124A63: (0x00124A25, 0x00347EC0, 0x001214BE),
    0x00124A75: (0x00124A25, 0x00347EB8, 0x00121516),
    0x00126034: (0x00126024, 0x00347EBC, 0x0012157B),
}
PHASE7_RUNTIME_CALLBACK_CALLER_BYTES = {
    0x00121E87: "8B45108B08FF75C883C008FF75F88945108B40FC8945BC0FBEC3508D45B85750894DB8FF15B47E34008B75FC83C41481E680000000740E837DF800750857FF15C07E34005980FB67750C85F6750857FF15B87E3400",
    0x00124A25: "8B45108B08FF75C083C008FF75F88945108B40FC8945B00FBEC3508D45AC5750894DACFF15B47E34008B75FC83C41481E680000000740E837DF800750857FF15C07E3400596683FB67750C85F6750857FF15B87E3400",
    0x00126024: "8D853CFEFFFF50FF75A00FBE45B54850FF15BC7E3400",
}
PHASE7_RUNTIME_CALLBACK_SITE_BYTES = {
    0x00121EAA: "FF15B47E3400",
    0x00121EC5: "FF15C07E3400",
    0x00121ED6: "FF15B87E3400",
    0x00124A48: "FF15B47E3400",
    0x00124A63: "FF15C07E3400",
    0x00124A75: "FF15B87E3400",
    0x00126034: "FF15BC7E3400",
}
PHASE7_RUNTIME_CALLBACK_INSTALLER_BYTES = {
    0x0011E251: "B86B181200A3B47E3400C705B87E340016151200C705BC7E34007B151200C705C07E3400BE141200C705C47E340061151200A3C87E3400C3",
}
PHASE7_RUNTIME_CALLBACK_REFERENCE_BYTES = {
    0x0011E256: "A3B47E3400",
    0x0011E25B: "C705B87E340016151200",
    0x0011E265: "C705BC7E34007B151200",
    0x0011E26F: "C705C07E3400BE141200",
    0x0011E279: "C705C47E340061151200",
    0x0011E283: "A3C87E3400",
    **PHASE7_RUNTIME_CALLBACK_SITE_BYTES,
}
PHASE7_RUNTIME_CALLBACK_REFERENCES = {
    0x00347EB4: frozenset((0x0011E256, 0x00121EAA, 0x00124A48)),
    0x00347EB8: frozenset((0x0011E25B, 0x00121ED6, 0x00124A75)),
    0x00347EBC: frozenset((0x0011E265, 0x00126034)),
    0x00347EC0: frozenset((0x0011E26F, 0x00121EC5, 0x00124A63)),
    0x00347EC4: frozenset((0x0011E279,)),
    0x00347EC8: frozenset((0x0011E283,)),
}
# DirectSound's three-entry reference-counted helper vtable is installed by two
# adjacent constructors.  The startup cleanup and release walkers call its two
# shared IUnknown-style slots through the same EDI object shape.  Recover both
# sites atomically so neither becomes a one-off register-indirect allowance.
PHASE7_DIRECTSOUND_REFCOUNT_VTABLE = 0x002C9574
PHASE7_DIRECTSOUND_REFCOUNT_BINDINGS = {
    0x00232AC1: (4, 0x0022BF1D),
    0x00232CA1: (8, 0x0022BF2A),
}
PHASE7_DIRECTSOUND_REFCOUNT_INSTRUCTION_BYTES = {
    0x002328FF: "C70074952C00",
    0x00232928: "C70774952C00",
    0x00232ABE: "8B07",
    0x00232AC0: "57",
    0x00232AC1: "FF5004",
    0x00232C9E: "8B07",
    0x00232CA0: "57",
    0x00232CA1: "FF5008",
}
# The hardware-voice object installs one nine-entry vtable in its constructor
# and restores the same table before the adjacent destruction path. Voice
# initialization reaches slot +0x10 only after the allocation/constructor and
# direct initialization chain below. Recover that complete finite object
# family as one unit so the guarded call cannot degrade into a broad vtable
# allowance.
PHASE7_DIRECTSOUND_VOICE_VTABLE = 0x002C95A4
PHASE7_DIRECTSOUND_VOICE_VTABLE_ENTRIES = (
    0x002355C9,
    0x0022BF1D,
    0x0022BF2A,
    0x00232D56,
    0x00235460,
    0x00234E0B,
    0x00234EED,
    0x00234F1C,
    0x0023557A,
)
PHASE7_DIRECTSOUND_VOICE_BINDINGS = {
    0x00232D9B: (0x10, 0x00235460),
    0x00234AF4: (0x1C, 0x00234F1C),
    0x002351DA: (0x18, 0x00234EED),
}
PHASE7_DIRECTSOUND_VOICE_INSTRUCTION_BYTES = {
    0x0022F28A: "6858010000",
    0x0022F2A2: "8BC8",
    0x0022F2A4: "E88D5C0000",
    0x0022F2B7: "894720",
    0x0022F2BC: "8BC8",
    0x0022F2BE: "E812610000",
    0x002318BE: "6A01",
    0x002318C0: "8BF1",
    0x002318C2: "E88F140000",
    0x00232D99: "8B01",
    0x00232D9B: "FF5010",
    0x00234AEE: "8B06",
    0x00234AF0: "6A01",
    0x00234AF2: "8BCE",
    0x00234AF4: "FF501C",
    0x00234BE1: "FF750C",
    0x00234BE4: "8BCE",
    0x00234BE6: "E8D2FDFFFF",
    0x00234F3B: "8BF1",
    0x00234F4A: "C706A4952C00",
    0x002353D8: "8BF1",
    0x002353DA: "E877D9FFFF",
    0x002351BE: "E8FAF7FFFF",
    0x002351D1: "8B06",
    0x002351D3: "57",
    0x002351D4: "57",
    0x002351D5: "57",
    0x002351D6: "6A02",
    0x002351D8: "8BCE",
    0x002351DA: "FF5018",
    0x0023550E: "8BF9",
    0x00235510: "C707A4952C00",
    0x002355CA: "8BF1",
    0x002355CC: "E83BFFFFFF",
}
# The title installs one four-instruction vblank callback through the D3D
# device's fixed callback setter.  The normal-live IA-32 scheduler invokes
# exactly this decoded leaf on its native vblank lane; validating the complete
# installer, setter, and leaf prevents the asynchronous boundary from becoming
# a general host-driven guest entry point.
PHASE7_VBLANK_CALLBACK_INSTRUCTION_BYTES = {
    0x000B86C0: "8B442404",
    0x000B86C4: "8B08",
    0x000B86C6: "890DFC185500",
    0x000B86CC: "C3",
    0x000B8D3D: "68C0860B00",
    0x000B8D42: "889E47B90600",
    0x000B8D48: "889E44B90600",
    0x000B8D4E: "889E45B90600",
    0x000B8D54: "889E46B90600",
    0x000B8D5A: "E821CB1500",
    0x00215880: "8B442404",
    0x00215884: "8B0DB8562200",
    0x0021588A: "898188190000",
    0x00215890: "C20400",
}
# The title installs this immutable 16-entry function-pair table before calling
# the adjacent sort/cleanup helpers.  The two helpers use an eight-byte row and
# call the second-column function through [edi+4] or [esi+4].  Their register
# targets are installed by the two address-named callers below; in particular,
# the cleanup helper at 0x10A280 receives 0x10A2F0 in EBX.
PHASE7_FUNCTION_PAIR_TABLE_BASE = 0x00345398
PHASE7_FUNCTION_PAIR_TABLE = (
    (0x0010A190, 0x0010A1F0),
    (0x000CFDD0, 0x000D7400),
    (0x0010EC40, 0x0010EAE0),
    (0x00113770, 0x00113A60),
    (0x00106C40, 0x00106C70),
    (0x00112A70, 0x00112AA0),
    (0x0010F6E0, 0x0010F250),
    (0x0010D8A0, 0x0010D6B0),
    (0x0010A110, 0x00109E80),
    (0x0010BC30, 0x0010C080),
    (0x0010F7C0, 0x00110250),
    (0x0010CAC0, 0x00112DE0),
    (0x00112DB0, 0x0010CEF0),
    (0x0010E240, 0x0010E280),
    (0x0010C400, 0x0010C600),
    (0x00112D60, 0x00112DA0),
)
PHASE7_FUNCTION_PAIR_HELPER_TARGETS = {
    0x0010A22D: (0x0010A2B0,),
    0x0010A253: tuple(sorted(pair[1] for pair in PHASE7_FUNCTION_PAIR_TABLE)),
    0x0010A263: (0x0010A2F0,),
    0x0010A290: tuple(sorted(pair[1] for pair in PHASE7_FUNCTION_PAIR_TABLE)),
    0x0010A29C: (0x0010A2F0,),
}
# Collision helper 0x6A6A0 indexes this immutable symmetric four-by-four
# function table with the two primitive-kind fields supplied by its callers.
# Seven entries are executable and four invalid combinations are explicit
# nulls.  Preserve the complete helper/table/caller family so the register
# call remains fail-closed without waiting to observe each primitive pairing.
PHASE7_COLLISION_DISPATCH_TABLE_BASE = 0x00336E48
PHASE7_COLLISION_DISPATCH_TABLE = (
    (0x0006AAA0, 0x0006A210, 0x0006A550, 0x0006A5C0),
    (0x0006A210, 0x0006A420, 0x0006A440, 0x0006A630),
    (0x0006A550, 0x0006A440, 0x00000000, 0x00000000),
    (0x0006A5C0, 0x0006A630, 0x00000000, 0x00000000),
)
PHASE7_COLLISION_DISPATCH_HELPER_START = 0x0006A6A0
PHASE7_COLLISION_DISPATCH_HELPER_END = 0x0006A6F8
PHASE7_COLLISION_DISPATCH_HELPER_SHA256 = (
    "3e681731b40ace659f83f49d0ac675f11b02fb092e1e0d7396cad5bf64507653"
)
PHASE7_COLLISION_DISPATCH_SITE = 0x0006A6E6
PHASE7_COLLISION_DISPATCH_CALLERS = frozenset(
    {
        0x00063B85,
        0x00063E08,
        0x00063E7C,
        0x00063EEC,
        0x00063F59,
        0x00063FC6,
        0x0006404E,
        0x000640C3,
        0x00064130,
        0x000641A4,
    }
)
PHASE7_COLLISION_DISPATCH_OBSERVED_BOUNDARY = (
    PHASE7_COLLISION_DISPATCH_SITE,
    0x0006A550,
)
# Helper 0x10D150 receives its EBP callback as the third stack argument.  Five
# decoded callers install exact code addresses and the sixth direct caller is
# the helper's recursive path, which preserves EBP.  Both callback sites inside
# the helper share the complete finite target set.
PHASE7_D150_CALLBACK_CALLERS = {
    0x0010D466: (0x0010D452, 0x0010D200),
    0x0010D4C5: (0x0010D4B2, 0x0010D260),
    0x0010FF43: (0x0010FF2E, 0x0010FAF0),
    0x0011025D: (0x00110254, 0x0010FE60),
    0x0011330C: (0x001132FD, 0x0010F9F0),
}
PHASE7_D150_CALLBACK_TARGETS = {
    site: tuple(sorted(binding[1] for binding in PHASE7_D150_CALLBACK_CALLERS.values()))
    for site in (0x0010D177, 0x0010D1AA)
}
PHASE7_BB0_CALLBACK_SITE = 0x00112BCE
PHASE7_BB0_CALLBACK_TARGET = 0x00109BB0
PHASE7_BB0_CALLBACK_CALLER = 0x00109BDF
PHASE7_BB0_CALLBACK_INSTALL = 0x00109BD5
# One four-slot asset-stream callback record is initialized together before the
# frontend opens its streamed audio banks.  The first slot is invoked from the
# decoded stream-construction helper after the record is attached at object
# offset +0xC.  Keep the complete record and both direct helper callers in the
# proof even though this startup path currently reaches only the first slot.
PHASE7_ASSET_STREAM_CALLBACK_RECORD_SLOTS = (
    (0x0C, 0x00110380, 0x0011052D),
    (0x10, 0x00110440, 0x00110534),
    (0x14, 0x00110450, 0x0011053B),
    (0x18, 0x00110490, 0x00110542),
)
PHASE7_ASSET_STREAM_CALLBACK_BINDINGS = {
    0x00112E50: (0x10, 0x00110440),
    0x001130C6: (0x0C, 0x00110380),
    0x001131BD: (0x14, 0x00110450),
}
PHASE7_ASSET_STREAM_ENTRY_REFERENCES = {
    0x00112E40: {
        0x0010CBA2,
        0x0010CBCC,
        0x0010CE83,
        0x001102B0,
        0x00113750,
    },
    0x00112EA0: {0x0011029E, 0x001134B6},
    0x00113100: {0x0011370C, 0x00115938},
}
# One resource plug-in descriptor installs a fixed four-entry callback vector
# at object offset +0x14.  The plug-in consumes all four entries directly; its
# resource converter receives slot zero as an explicit function argument and
# invokes it at the fifth guarded site.  Recover the entire descriptor/vector
# family together so loading new resource types cannot expose one callback at
# a time.
PHASE7_RESOURCE_CONVERTER_DESCRIPTOR = 0x00344D90
PHASE7_RESOURCE_CONVERTER_DESCRIPTOR_VALUES = (
    0x002CACA4,
    0x00105920,
    0x00000000,
    0x00000000,
    0x00105BE0,
)
PHASE7_RESOURCE_CONVERTER_CALLBACK_VECTOR = (
    (0x00, 0x00105830),
    (0x04, 0x001058C0),
    (0x08, 0x00105450),
    (0x0C, 0x001061A0),
)
PHASE7_RESOURCE_CONVERTER_INSTALLER_BINDINGS = {
    0x00105BE7: (0x00, 0x00105830),
    0x00105BED: (0x04, 0x001058C0),
    0x00105BF4: (0x08, 0x00105450),
    0x00105BFB: (0x0C, 0x001061A0),
}
PHASE7_RESOURCE_CONVERTER_CALLBACK_BINDINGS = {
    0x001059BC: (0x04, 0x001058C0),
    0x00105A6A: (0x04, 0x001058C0),
    0x00105A89: (0x08, 0x00105450),
    0x00105BC0: (0x0C, 0x001061A0),
    0x00105E72: (0x00, 0x00105830),
}
PHASE7_RESOURCE_CONVERTER_INSTALLER_BYTES = (
    "8B4424048B4014C70030581000C74004C0581000C7400850541000"
    "C7400CA0611000B801000000C3"
)
PHASE7_RESOURCE_CONVERTER_INSTRUCTION_BYTES = {
    # The adjacent plug-in shares the generic slot-C callback.  Keep its one
    # immediate reference in the exact target-reference census.
    0x0010501B: "C7400CA0611000",
    0x00105923: "8B44245C",
    0x00105927: "8B4C2458",
    0x0010592C: "8B5914",
    0x00105943: "895C2414",
    0x001059B0: "8B4304",
    0x001059BC: "FFD0",
    0x00105A31: "8B03",
    0x00105A33: "50",
    0x00105A3C: "E84F020000",
    0x00105A5E: "8B4304",
    0x00105A6A: "FFD0",
    0x00105A81: "8B4308",
    0x00105A89: "FFD0",
    0x00105BA9: "8B542414",
    0x00105BAD: "8B420C",
    0x00105BC0: "FFD0",
    0x00105C10: "B8904D3400",
    0x00105E5F: "8B442434",
    0x00105E72: "FFD0",
}
PHASE7_RESOURCE_CONVERTER_OBSERVED_BOUNDARY = (0x00105E72, 0x00105830)
# The accepted capsule retains the six installed callback values used by the
# title's CRT wrapper table. Every decoded indirect site that names one of
# these exact cells shares its finite target instead of growing site by site.
PHASE7_CAPTURED_CALLBACK_CELLS = {
    0x005ADCB4: (0x000F4780,),
    0x005ADCC0: (0x000F0AF0,),
    0x005ADCC4: (0x000EFAC0,),
    0x005ADCFC: (0x000EF800,),
    0x005ADD00: (0x000F8500,),
    0x005ADD04: (0x000F8A60,),
    0x005ADD0C: (0x000F8CB0,),
    0x005ADD18: (0x000EF990,),
    0x005ADD24: (0x000EE810,),
    0x005ADD28: (0x000EEB90,),
    0x005ADD44: (0x000EED10,),
    0x005ADD48: (0x000EEDA0,),
    0x005ADD50: (0x000F99D0,),
    0x005ADD68: (0x000D86F0,),
    0x005ADD6C: (0x000D8770,),
    0x005ADD70: (0x000D8780,),
    0x005ADD74: (0x000D87B0,),
    0x005ADD80: (0x000D87F0,),
    0x005ADD84: (0x000D8810,),
    0x005ADD8C: (0x000D8890,),
    0x005ADD98: (0x00121080,),
    0x005ADD9C: (0x0011E470,),
    0x005ADDA0: (0x00121090,),
    0x005ADDA8: (0x000F4B30,),
    0x005ADDAC: (0x000F4B10,),
    0x005ADDB4: (0x00120FF0,),
    0x005ADDB8: (0x000F4A60,),
    0x005ADDBC: (0x00120F60,),
    0x005ADDD0: (0x0011F398,),
    0x005ADDD4: (0x0011F2ED,),
    0x005ADDD8: (0x001206BF,),
    0x005ADDDC: (0x000F1FE0,),
    0x005ADDE0: (0x000E8720,),
    0x005ADDE4: (0x000E8730,),
}
# Some title routines retain a kernel import in a callee-saved register across
# a short loop. These tuples name the load site, import cell, and exact resolved
# service target so the whole reviewed family can be validated fail-closed.
PHASE7_CAPTURED_REGISTER_IMPORT_BINDINGS = {
    0x000DFDB7: (0x000DFD76, 0x00293CC0, 0xE0000700),
    0x000E0701: (0x000E06E3, 0x00293CD4, 0xE0000590),
    0x000E1E2E: (0x000E1E1A, 0x00293CD0, 0xE00006B0),
    0x000E264E: (0x000E263B, 0x00293CD0, 0xE00006B0),
    0x000E268E: (0x000E2687, 0x00293CE8, 0xE00006E0),
    0x000E328F: (0x000E326F, 0x00293DA8, 0xE0000450),
    0x000E3FDF: (0x000E3FC7, 0x00293DA8, 0xE0000450),
    0x000E44FF: (0x000E44F9, 0x00293DB4, 0xE0000540),
    0x000E5F62: (0x000E5EFF, 0x00293D10, 0xE00004B0),
    0x000E6233: (0x000E6216, 0x00293D10, 0xE00004B0),
    0x000E6668: (0x000E665B, 0x00293CA8, 0xE0000440),
    0x000E6A61: (0x000E6A42, 0x00293CF8, 0xE0000560),
    0x000E6B3A: (0x000E6B2B, 0x00293CD0, 0xE00006B0),
    0x0021049A: (0x00210490, 0x00293D70, 0xE0000690),
    0x00210C46: (0x00210C02, 0x00293D6C, 0xE00006D0),
    0x00219E60: (0x00219E49, 0x00293CAC, 0xE00003C0),
    0x0021AAA7: (0x0021AA8F, 0x00293CAC, 0xE00003C0),
    0x0021AABA: (0x0021AA8F, 0x00293CAC, 0xE00003C0),
    0x0021B1A9: (0x0021B191, 0x00293D5C, 0xE0000350),
    0x0021E063: (0x0021E049, 0x00293E5C, 0xE00000E0),
    0x00221BFA: (0x00221BE5, 0x00293E50, 0xE0000010),
    0x002313F9: (0x002313E6, 0x00293E70, 0xE00002A0),
    0x0023171E: (0x0023170B, 0x00293D64, 0xE0000220),
    0x00236295: (0x00236289, 0x00293E38, 0xE00003E0),
    0x00264ED6: (0x00264EC7, 0x00293CD0, 0xE00006B0),
    0x00264F04: (0x00264ED8, 0x00293CD8, 0xE00004F0),
    0x00272493: (0x0027248C, 0x00293CE8, 0xE00006E0),
    0x002728B2: (0x0027289F, 0x00293CD0, 0xE00006B0),
    0x002728DE: (0x00272890, 0x00293CCC, 0xE0000480),
    0x0027295F: (0x00272958, 0x00293CE8, 0xE00006E0),
    0x00278CD7: (0x00278CD0, 0x00293CE8, 0xE00006E0),
    0x00280754: (0x00280713, 0x00293E00, 0xE0000310),
    0x002873AD: (0x0028739B, 0x00293DE4, 0xE0000050),
    0x00287631: (0x00287617, 0x00293DE0, 0xE0000070),
    0x002896D2: (0x002896C0, 0x00293D58, 0xE00002E0),
    0x0028989B: (0x0028988C, 0x00293D58, 0xE00002E0),
    0x0028CFB5: (0x0028CFAD, 0x00293D50, 0xE0000290),
    0x0028D872: (0x0028D859, 0x00293D78, 0xE0000190),
    0x0028EC38: (0x0028EC19, 0x00293D5C, 0xE0000350),
    0x00293417: (0x00293404, 0x00293D50, 0xE0000290),
}
# Two fused boot paths hide their kernel calls from the module-edge stream.
# Admit the services only while the immutable accepted-capsule import cells
# still contain the exact profiled targets.
PHASE7_CAPTURED_SERVICE_IMPORT_CELLS = {
    0x00293CDC: 0xE0000400,
    0x00293E58: 0xE0000030,
}
# The title's optimized memmove implementation contains seven logical jump
# tables and 16 consumers.  Four tables deliberately use non-zero or negative
# selector ranges, allowing their unused entries to overlap adjacent machine
# instructions.  Model the complete routine explicitly rather than weakening
# the generic zero-based jump-table recognizer.
PHASE7_MEMMOVE_JUMP_TABLES = {
    0x0011F010: ((1, 0x0011F020), (2, 0x0011F04C), (3, 0x0011F070)),
    0x0011F10C: (
        (-4, 0x0011F10C),
        (-3, 0x0011F114),
        (-2, 0x0011F120),
        (-1, 0x0011F134),
    ),
    0x0011F090: (
        (0, 0x0011F0F3),
        (1, 0x0011F0E0),
        (2, 0x0011F0D8),
        (3, 0x0011F0D0),
        (4, 0x0011F0C8),
        (5, 0x0011F0C0),
        (6, 0x0011F0B8),
        (7, 0x0011F0B0),
    ),
    0x0011F0FC: (
        (0, 0x0011F10C),
        (1, 0x0011F114),
        (2, 0x0011F120),
        (3, 0x0011F134),
    ),
    0x0011F19C: ((1, 0x0011F1AC), (2, 0x0011F1D0), (3, 0x0011F1F8)),
    0x0011F248: (
        (-7, 0x0011F24C),
        (-6, 0x0011F254),
        (-5, 0x0011F25C),
        (-4, 0x0011F264),
        (-3, 0x0011F26C),
        (-2, 0x0011F274),
        (-1, 0x0011F27C),
        (0, 0x0011F28F),
    ),
    0x0011F298: (
        (0, 0x0011F2A8),
        (1, 0x0011F2B0),
        (2, 0x0011F2C0),
        (3, 0x0011F2D4),
    ),
}
PHASE7_MEMMOVE_JUMP_BINDINGS = {
    0x0011EFE5: (0x0011F0FC, "edx"),
    0x0011EFFD: (0x0011F010, "eax"),
    0x0011F004: (0x0011F10C, "ecx"),
    0x0011F00C: (0x0011F090, "ecx"),
    0x0011F042: (0x0011F0FC, "edx"),
    0x0011F068: (0x0011F0FC, "edx"),
    0x0011F086: (0x0011F0FC, "edx"),
    0x0011F0F3: (0x0011F0FC, "edx"),
    0x0011F16B: (0x0011F298, "edx"),
    0x0011F176: (0x0011F248, "ecx"),
    0x0011F191: (0x0011F19C, "eax"),
    0x0011F198: (0x0011F298, "ecx"),
    0x0011F1C6: (0x0011F298, "edx"),
    0x0011F1F0: (0x0011F298, "edx"),
    0x0011F222: (0x0011F298, "edx"),
    0x0011F28F: (0x0011F298, "edx"),
}
PHASE7_MEMMOVE_JUMP_SITE_BYTES = {
    0x0011EFE5: "FF2495FCF01100",
    0x0011EFFD: "FF248510F01100",
    0x0011F004: "FF248D0CF11100",
    0x0011F00C: "FF248D90F01100",
    0x0011F042: "FF2495FCF01100",
    0x0011F068: "FF2495FCF01100",
    0x0011F086: "FF2495FCF01100",
    0x0011F0F3: "FF2495FCF01100",
    0x0011F16B: "FF249598F21100",
    0x0011F176: "FF248D48F21100",
    0x0011F191: "FF24859CF11100",
    0x0011F198: "FF248D98F21100",
    0x0011F1C6: "FF249598F21100",
    0x0011F1F0: "FF249598F21100",
    0x0011F222: "FF249598F21100",
    0x0011F28F: "FF249598F21100",
}
PHASE7_MEMMOVE_JUMP_CONTROL_BYTES = {
    0x0011EFB0: "558BEC57568B750C8B4D108B7D088BC18BD103C63BFE76083BF80F827C010000F7C7030000007514C1E90283E20383F9087229F3A5FF2495FCF011008BC7BA0300000083E904720C83E00303C8FF248510F01100FF248D0CF1110090FF248D90F011009020F011004CF0110070F01100",
    0x0011F14C: "8D7431FC8D7C39FCF7C7030000007524C1E90283E20383F908720DFDF3A5FCFF249598F211008BFFF7D9FF248D48F211008D49008BC7BA0300000083F904720C83E0032BC8FF24859CF11100FF248D98F2110090ACF11100D0F11100F8F11100",
}
PHASE7_MEMMOVE_JUMP_REFERENCES = {
    0x0011F010: frozenset((0x0011EFFD,)),
    0x0011F10C: frozenset((0x0011F004,)),
    0x0011F090: frozenset((0x0011F00C,)),
    0x0011F0FC: frozenset(
        (0x0011EFE5, 0x0011F042, 0x0011F068, 0x0011F086, 0x0011F0F3)
    ),
    0x0011F19C: frozenset((0x0011F191,)),
    0x0011F248: frozenset((0x0011F176,)),
    0x0011F298: frozenset(
        (0x0011F16B, 0x0011F198, 0x0011F1C6, 0x0011F1F0, 0x0011F222, 0x0011F28F)
    ),
}
# The linked runtime's aligned-copy tail masks EBX to four bits and branches
# around the indexed jump when the result is zero.  The physical table therefore
# has one deliberate null followed by all 15 executable selector targets, which
# the generic zero-based recognizer cannot safely infer.
PHASE7_ALIGNED_COPY_TAIL_SITE = 0x0028B630
PHASE7_ALIGNED_COPY_TAIL_TABLE = 0x0028B6D4
PHASE7_ALIGNED_COPY_TAIL_TARGETS = (
    0x0028B689,
    0x0028B684,
    0x0028B67F,
    0x0028B67A,
    0x0028B675,
    0x0028B670,
    0x0028B66B,
    0x0028B666,
    0x0028B661,
    0x0028B65C,
    0x0028B657,
    0x0028B652,
    0x0028B64D,
    0x0028B648,
    0x0028B643,
)
PHASE7_ALIGNED_COPY_TAIL_PROOF_START = 0x0028B61F
PHASE7_ALIGNED_COPY_TAIL_PROOF_BYTES = (
    "8BD983C10FC1E90483E30F740B8D749EC0FF249DD4B62800"
)
PHASE6_MAX_STATIC_JUMP_TABLE_ENTRIES = 256
PHASE3_REWRITE_ISLAND_OFFSET = 0x40
NATIVE_HOST_SERVICE_WRITE_U32 = 2
NATIVE_HOST_SERVICE_CALLBACK = 3
NATIVE_HOST_SERVICE_WORKLOAD_ABI = 4
WORKLOAD_SERVICE_SYSTEM_TIME = 3
WORKLOAD_SERVICE_XGETDEVICES = 4
WORKLOAD_SERVICE_XINPUT_OPEN = 5
WORKLOAD_SERVICE_XINPUT_CAPABILITIES = 6
WORKLOAD_SERVICE_XINPUT_STATE = 7
WORKLOAD_SERVICE_COLD_CALLBACK = 8
WORKLOAD_SERVICE_IRQL = 9
WORKLOAD_SERVICE_SEMAPHORE = 10
WORKLOAD_SERVICE_RUNTIME = 11
WORKLOAD_SERVICE_MEMORY = 12
WORKLOAD_SERVICE_AUDIO = 13
WORKLOAD_SERVICE_TITLE = 14
WORKLOAD_SERVICE_BOOTSTRAP = 15
WORKLOAD_SERVICE_XINPUT_CLOSE = 16
WORKLOAD_SERVICE_XINPUT_SET_STATE = 17
WORKLOAD_SERVICE_OFFLINE_KERNEL = 18
IA32_VBLANK_RETURN_SENTINEL = 0xB2D3D000
HOST_SERVICE_EXECUTION_NATIVE32 = 1
HOST_SERVICE_EXECUTION_NATIVE64_BROKER = 2
HOST_SERVICE_CALL_STDCALL = 1
HOST_SERVICE_CALL_CDECL = 2
HOST_SERVICE_BOUNDARIES = {
    "synchronization": 1,
    "files": 2,
    "timing": 3,
    "input": 4,
    "audio": 5,
    "render": 6,
    "memory": 7,
    "title": 8,
}
HOST_SERVICE_TRACE_CAPACITY = 64
HOST_SERVICE_TRACE_HEADER_SIZE = 16
HOST_SERVICE_TRACE_RECORD_WORDS = 24
HOST_SERVICE_TRACE_RECORD_SIZE = HOST_SERVICE_TRACE_RECORD_WORDS * 4
NORMAL_LIVE_NATIVE32_MAX_ARGUMENTS = 10
HOST_SERVICE_BROKER_CONTROL_SIZE = 64
HOST_SERVICE_BROKER_MAGIC = 0x42325242  # "BR2B"
HOST_SERVICE_BROKER_VERSION = 1
HOST_SERVICE_BROKER_STATUS_IDLE = 0
HOST_SERVICE_BROKER_STATUS_READY = 1
HOST_SERVICE_BROKER_STATUS_REQUEST = 2
HOST_SERVICE_BROKER_STATUS_RESPONSE = 3
HOST_SERVICE_BROKER_STATUS_STOP = 4
HOST_SERVICE_BROKER_STATUS_ERROR = 5
RESIDENT_SCHEDULER_MAGIC = 0x35533242  # "B2S5"
RESIDENT_SCHEDULER_VERSION = 1
RESIDENT_SCHEDULER_LANES = ("primary", "worker", "vblank")
RESIDENT_SCHEDULER_LANE_COUNT = len(RESIDENT_SCHEDULER_LANES)
RESIDENT_SCHEDULER_TRACE_CAPACITY = 32
RESIDENT_SCHEDULER_HEADER_SIZE = 64
RESIDENT_SCHEDULER_LANE_RECORD_SIZE = 592
RESIDENT_SCHEDULER_TRACE_RECORD_WORDS = 16
RESIDENT_SCHEDULER_TRACE_RECORD_SIZE = RESIDENT_SCHEDULER_TRACE_RECORD_WORDS * 4
RESIDENT_LANE_READY = 1
RESIDENT_LANE_RUNNING = 2
RESIDENT_LANE_WAITING = 3
RESIDENT_LANE_COMPLETED = 4
RESIDENT_LANE_FAULTED = 5
RESIDENT_EXIT_YIELD = 1
RESIDENT_EXIT_WAIT = 2
RESIDENT_EXIT_COMPLETE = 3
RESIDENT_EXIT_FLIP = 4
RESIDENT_EXIT_FAULT = 5
RESIDENT_EXIT_KINDS = {
    "yield": RESIDENT_EXIT_YIELD,
    "wait": RESIDENT_EXIT_WAIT,
    "complete": RESIDENT_EXIT_COMPLETE,
    "flip": RESIDENT_EXIT_FLIP,
}
CUTOVER_MEMORY_MAGIC = 0x374D3242  # "B2M7"
CUTOVER_MEMORY_VERSION = 1
CUTOVER_MEMORY_HEADER_WORDS = 16
CUTOVER_MEMORY_HEADER_SIZE = CUTOVER_MEMORY_HEADER_WORDS * 4
CUTOVER_PAGE_CLEAN = 0
CUTOVER_PAGE_NATIVE_DIRTY = 1
CUTOVER_PAGE_HOST_PUBLICATION = 2
FULL_FLIP_ORACLE_FORMAT = "b2-recomp-phase7-full-flip-oracle"
FULL_FLIP_ORACLE_RESOURCE_MAGIC = b"B2F7"
FULL_FLIP_ORACLE_RESOURCE_VERSION = 1

# Exact ABI declarations for the supported workload's service targets that do
# not live in the XBE-backed decoded block store.  The operation kind/value
# pairs are the accepted native executor's frozen service-dispatch contract;
# they are deliberately separate from Phase 4's generic thunk kind.
SUPPORTED_WORKLOAD_SERVICE_SPECS: dict[int, dict[str, Any]] = {
    0x31F10300: {
        "shim_name": "TitleAssetStreamStatus",
        "runtime_kind": WORKLOAD_SERVICE_TITLE,
        "runtime_value": 7,
        "stack_cleanup_bytes": 0,
        "boundary": "title",
    },
    0x31F10700: {
        "shim_name": "TitleAssetStreamRead",
        "runtime_kind": WORKLOAD_SERVICE_TITLE,
        "runtime_value": 8,
        "stack_cleanup_bytes": 8,
        "boundary": "files",
    },
    0x31F10800: {
        "shim_name": "TitleAssetStreamSeek",
        "runtime_kind": WORKLOAD_SERVICE_TITLE,
        "runtime_value": 9,
        "stack_cleanup_bytes": 12,
        "boundary": "files",
    },
    0xE0000050: {
        "shim_name": "ExAllocatePoolWithTag",
        "runtime_kind": WORKLOAD_SERVICE_MEMORY,
        "runtime_value": 8,
        "stack_cleanup_bytes": 8,
        "boundary": "memory",
    },
    0xE0000070: {
        "shim_name": "ExFreePool",
        "runtime_kind": WORKLOAD_SERVICE_MEMORY,
        "runtime_value": 6,
        "stack_cleanup_bytes": 4,
        "boundary": "memory",
    },
    0xE0000080: {
        "shim_name": "ExQueryPoolBlockSize",
        "runtime_kind": WORKLOAD_SERVICE_MEMORY,
        "runtime_value": 5,
        "stack_cleanup_bytes": 4,
        "boundary": "memory",
    },
    0xE0000200: {
        "shim_name": "KeDelayExecutionThread",
        "runtime_kind": WORKLOAD_SERVICE_RUNTIME,
        "runtime_value": 6,
        "stack_cleanup_bytes": 12,
        "boundary": "timing",
    },
    0xE0000280: {
        "shim_name": "KeQuerySystemTime",
        "runtime_kind": WORKLOAD_SERVICE_SYSTEM_TIME,
        "runtime_value": 0,
        "stack_cleanup_bytes": 4,
        "boundary": "timing",
    },
    0xE0000360: {
        "shim_name": "KfRaiseIrql",
        "runtime_kind": WORKLOAD_SERVICE_IRQL,
        "runtime_value": 1,
        "stack_cleanup_bytes": 4,
        "boundary": "synchronization",
    },
    0xE0000370: {
        "shim_name": "KfLowerIrql",
        "runtime_kind": WORKLOAD_SERVICE_IRQL,
        "runtime_value": 2,
        "stack_cleanup_bytes": 0,
        "boundary": "synchronization",
    },
    0xE00003A0: {
        "shim_name": "MmAllocateContiguousMemoryEx",
        "runtime_kind": WORKLOAD_SERVICE_MEMORY,
        "runtime_value": 1,
        "stack_cleanup_bytes": 20,
        "boundary": "memory",
    },
    0xE0000570: {
        "shim_name": "NtReleaseSemaphore",
        "runtime_kind": WORKLOAD_SERVICE_SEMAPHORE,
        "runtime_value": 1,
        "stack_cleanup_bytes": 12,
        "boundary": "synchronization",
    },
    0xE00005C0: {
        "shim_name": "NtWaitForSingleObjectEx",
        "runtime_kind": WORKLOAD_SERVICE_SEMAPHORE,
        "runtime_value": 2,
        "stack_cleanup_bytes": 16,
        "boundary": "synchronization",
    },
}

# Exact additional ABI rows observed by the complete XBE-entry-to-Lesson-One
# native profile.  Kind/value pairs match the accepted native executor's
# service dispatch contract.  Their bodies remain fail-closed until the same
# behavior is owned by the normal-live native runtime.
#
# The allocation wrapper at 0x000CB690 remains guest code.  Music wrapper
# 0x000CC2F0 is the title's narrow high-level audio ABI until the guest music
# manager reaches the low-level stream path: mode 2 selects menu music, mode 3
# selects the current gameplay track, mode 4 selects credits, and all other
# modes stop the active track.  The service is native and never compiles guest
# code at runtime.
PHASE7_PROFILED_BOOT_SERVICE_ROWS = (
    (0x000CC2F0, "TitleMusicModeSet", 13, 19, 4, "audio"),
    (0x0022C11B, "DirectSoundBufferRelease", 13, 29, 4, "audio"),
    (0x0022CC0B, "DirectSoundStreamRelease", 13, 30, 4, "audio"),
    (0x0022CDA9, "DirectSoundStreamProcess", 13, 8, 12, "audio"),
    (0x0022CD0D, "DirectSoundStreamFlush", 13, 9, 4, "audio"),
    (0x0022D7D2, "DirectSoundBufferSetVolume", 13, 13, 8, "audio"),
    (0x0022D85E, "DirectSoundBufferSetHeadroom", 13, 20, 8, "audio"),
    (0x0022D87A, "DirectSoundBufferSetMixBinVolumes", 13, 21, 8, "audio"),
    (0x0022D896, "DirectSoundBufferPlay", 13, 4, 16, "audio"),
    (0x0022D8BA, "DirectSoundBufferStop", 13, 5, 4, "audio"),
    (0x0022D8D2, "DirectSoundBufferStopEx", 13, 6, 16, "audio"),
    (0x0022D8F6, "DirectSoundBufferSetLoopRegion", 13, 22, 12, "audio"),
    (0x0022D916, "DirectSoundBufferGetStatus", 13, 17, 8, "audio"),
    (0x0022D932, "DirectSoundBufferGetPosition", 13, 15, 12, "audio"),
    (0x0022D952, "DirectSoundBufferSetPosition", 13, 16, 8, "audio"),
    (0x0022E738, "DirectSoundBufferSetFrequency", 13, 14, 8, "audio"),
    (0x0022E754, "DirectSoundBufferSetOutputBuffer", 13, 23, 8, "audio"),
    (0x0022E770, "DirectSoundBufferSetMixBins", 13, 24, 8, "audio"),
    (0x0022E78C, "DirectSoundBufferSetAllParameters", 13, 25, 12, "audio"),
    (0x0022E8C4, "DirectSoundBufferSetRolloffCurve", 13, 26, 16, "audio"),
    (0x0022E8E8, "DirectSoundBufferSetI3DL2Source", 13, 27, 12, "audio"),
    (0x0022E908, "DirectSoundBufferSetPlayRegion", 13, 28, 12, "audio"),
    (0x0022EF2F, "DirectSoundBufferSetFormat", 13, 3, 8, "audio"),
    (0x0022EF4B, "DirectSoundBufferSetData", 13, 2, 12, "audio"),
    (0x0022EF6B, "DirectSoundStreamSetFormat", 13, 10, 8, "audio"),
    (0x0022F8EA, "DirectSoundBufferCreate", 13, 11, 16, "audio"),
    (0x0022F90E, "DirectSoundStreamCreate", 13, 7, 16, "audio"),
    (0x00230ABD, "DirectSoundEffectImage", 13, 1, 12, "audio"),
    (0x0028CF40, "XInputOpen", 5, 0, 16, "input"),
    (0x0028CF96, "XInputClose", 16, 0, 4, "input"),
    (0x0028CFA2, "XInputGetCapabilities", 6, 0, 8, "input"),
    (0x0028D180, "XInputGetState", 7, 0, 8, "input"),
    (0x0028D1EC, "XInputSetState", 17, 0, 8, "input"),
    (0x0028D282, "XGetDevices", 4, 0, 4, "input"),
    (0x31F10200, "TitleAssetStreamClose", 14, 6, 0, "title"),
    (0xE0000000, "AvGetSavedDataAddress", 15, 17, 0, "render"),
    (0xE0000010, "AvSendTVEncoderOption", 15, 1, 16, "render"),
    (0xE0000020, "AvSetDisplayMode", 15, 18, 24, "render"),
    (0xE0000030, "AvSetSavedDataAddress", 15, 19, 4, "render"),
    (0xE0000090, "ExQueryNonVolatileSetting", 11, 3, 20, "synchronization"),
    (0xE00000D0, "HalGetInterruptVector", 15, 2, 8, "synchronization"),
    (0xE00000E0, "HalReadWritePCISpace", 15, 3, 24, "synchronization"),
    (0xE00000F0, "HalRegisterShutdownNotification", 1, 0, 8, "synchronization"),
    (0xE0000120, "IoCreateDevice", 15, 4, 24, "files"),
    (0xE0000130, "IoCreateSymbolicLink", 1, 0, 8, "files"),
    (0xE00001F0, "KeConnectInterrupt", 15, 5, 4, "synchronization"),
    (0xE0000220, "KeInitializeDpc", 11, 5, 12, "synchronization"),
    (0xE0000230, "KeInitializeInterrupt", 15, 6, 28, "synchronization"),
    (0xE0000240, "KeInitializeTimerEx", 11, 5, 8, "synchronization"),
    (0xE0000290, "KeRaiseIrqlToDpcLevel", 9, 3, 0, "synchronization"),
    (0xE00002D0, "KeSetBasePriorityThread", 8, 7, 8, "synchronization"),
    (0xE00002F0, "KeSetTimer", 1, 0, 16, "timing"),
    (0xE0000310, "KeStallExecutionProcessor", 15, 7, 4, "timing"),
    (0xE0000350, "KeWaitForSingleObject", 10, 3, 20, "synchronization"),
    (0xE0000390, "MmAllocateContiguousMemory", 12, 3, 4, "memory"),
    (0xE00003B0, "MmClaimGpuInstanceMemory", 15, 8, 8, "render"),
    (0xE00003C0, "MmFreeContiguousMemory", 12, 6, 4, "memory"),
    (0xE00003D0, "MmGetPhysicalAddress", 12, 4, 4, "memory"),
    (0xE00003E0, "MmLockUnlockBufferPages", 12, 7, 12, "memory"),
    (0xE0000400, "MmPersistContiguousMemory", 12, 7, 12, "memory"),
    (0xE0000420, "MmQueryAllocationSize", 12, 5, 4, "memory"),
    (0xE0000450, "NtAllocateVirtualMemory", 12, 2, 20, "memory"),
    (0xE0000460, "NtClose", 15, 9, 4, "files"),
    (0xE0000480, "NtCreateFile", 15, 11, 36, "files"),
    (0xE0000490, "NtCreateSemaphore", 8, 5, 16, "synchronization"),
    (0xE00004D0, "NtFreeVirtualMemory", 12, 6, 4, "memory"),
    (0xE00004F0, "NtOpenFile", 15, 12, 24, "files"),
    (0xE0000500, "NtOpenSymbolicLinkObject", 15, 13, 8, "files"),
    (0xE0000510, "NtQueryDirectoryFile", 15, 23, 40, "files"),
    (0xE0000520, "NtQueryInformationFile", 15, 14, 20, "files"),
    (0xE0000530, "NtQuerySymbolicLinkObject", 15, 15, 12, "files"),
    (0xE0000550, "NtQueryVolumeInformationFile", 15, 16, 20, "files"),
    (0xE0000560, "NtReadFile", 15, 20, 32, "files"),
    (0xE0000590, "NtSetInformationFile", 15, 21, 20, "files"),
    (0xE00005D0, "NtWriteFile", 15, 22, 32, "files"),
    (0xE00005F0, "ObReferenceObjectByHandle", 8, 6, 12, "synchronization"),
    (0xE0000610, "ObfDereferenceObject", 1, 0, 0, "synchronization"),
    (0xE0000620, "PhyGetLinkState", 1, 0, 4, "input"),
    (0xE0000630, "PhyInitialize", 1, 0, 8, "input"),
    (0xE0000640, "PsCreateSystemThreadEx", 8, 1, 40, "synchronization"),
    (0xE0000670, "RtlAnsiStringToUnicodeString", 11, 7, 12, "memory"),
    (0xE0000690, "RtlEnterCriticalSection", 1, 0, 4, "synchronization"),
    (0xE00006A0, "RtlEqualString", 11, 2, 12, "memory"),
    (0xE00006B0, "RtlInitAnsiString", 11, 1, 8, "memory"),
    (0xE00006C0, "RtlInitializeCriticalSection", 1, 0, 4, "synchronization"),
    (0xE00006D0, "RtlLeaveCriticalSection", 1, 0, 4, "synchronization"),
    (0xE00006E0, "RtlNtStatusToDosError", 11, 4, 4, "synchronization"),
    (0xE0000700, "RtlTimeFieldsToTime", 15, 30, 8, "timing"),
    (0xE0000710, "RtlTimeToTimeFields", 15, 31, 8, "timing"),
    (0xE0000720, "RtlUnicodeStringToAnsiString", 11, 8, 12, "memory"),
    (0xE00007B0, "XcSHAInit", 15, 24, 4, "synchronization"),
    (0xE00007C0, "XcSHAUpdate", 15, 25, 12, "synchronization"),
    (0xE00007D0, "XcSHAFinal", 15, 26, 8, "synchronization"),
)
PHASE7_PROFILED_BOOT_SERVICE_SPECS = {
    target: {
        "shim_name": shim_name,
        "runtime_kind": runtime_kind,
        "runtime_value": runtime_value,
        "stack_cleanup_bytes": stack_cleanup_bytes,
        "boundary": boundary,
        "normal_live_body": "pending-native-port",
    }
    for (
        target,
        shim_name,
        runtime_kind,
        runtime_value,
        stack_cleanup_bytes,
        boundary,
    ) in PHASE7_PROFILED_BOOT_SERVICE_ROWS
}
PHASE7_PROFILED_BOOT_SERVICE_SPECS[0x31F10200]["normal_live_body"] = "implemented-native32"

# These imports were not reached by the retained boot-to-Lesson-One profile,
# but every call site names the loader's immutable kernel thunk table.  Their
# native bodies are complete and can therefore be admitted by the offline
# boundary audit without waiting for another manual gameplay fault.
PHASE7_OFFLINE_KERNEL_SERVICE_SPECS: dict[int, dict[str, Any]] = {
    0xE00002E0: {
        "shim_name": "KeSetEvent",
        "runtime_kind": WORKLOAD_SERVICE_BOOTSTRAP,
        "runtime_value": 10,
        "stack_cleanup_bytes": 12,
        "boundary": "synchronization",
        "normal_live_body": "implemented-native32",
    },
    0xE0000680: {
        "shim_name": "RtlCompareMemoryUlong",
        "runtime_kind": WORKLOAD_SERVICE_RUNTIME,
        "runtime_value": 9,
        "stack_cleanup_bytes": 12,
        "boundary": "memory",
        "normal_live_body": "implemented-native32",
    },
}

# The complete import-table walk exposes a second, finite group of kernel
# exports that the retained gameplay profile did not invoke.  Values in this
# table are consumed by the native IA-32 worker's offline-kernel dispatcher;
# none of them route through Python or permit runtime guest-code discovery.
PHASE7_OFFLINE_KERNEL_SERVICE_ROWS = (
    (0xE0000040, "DbgPrint", 1, 4, "synchronization"),
    (0xE0000100, "HalReturnToFirmware", 2, 4, "synchronization"),
    (0xE0000140, "IoDeleteSymbolicLink", 3, 4, "files"),
    (0xE00001A0, "IoSynchronousFsdRequest", 4, 8, "files"),
    (0xE00001D0, "KeBugCheck", 5, 4, "synchronization"),
    (0xE00001E0, "KeCancelTimer", 6, 4, "synchronization"),
    (0xE0000210, "KeDisconnectInterrupt", 7, 4, "synchronization"),
    (0xE0000250, "KeInsertQueueDpc", 8, 4, "synchronization"),
    (0xE0000260, "KeQueryPerformanceCounter", 9, 0, "timing"),
    (0xE00002A0, "KeRemoveQueueDpc", 10, 4, "synchronization"),
    (0xE00002B0, "KeRestoreFloatingPointState", 11, 4, "synchronization"),
    (0xE00002C0, "KeSaveFloatingPointState", 12, 0, "synchronization"),
    (0xE0000410, "MmQueryAddressProtect", 13, 4, "memory"),
    (0xE0000470, "NtCreateEvent", 14, 8, "synchronization"),
    (0xE00004A0, "NtDeleteFile", 15, 4, "files"),
    (0xE00004E0, "NtFsControlFile", 16, 40, "files"),
    (0xE0000580, "NtSetEvent", 17, 4, "synchronization"),
    (0xE00005B0, "NtWaitForSingleObject", 18, 12, "synchronization"),
    (0xE0000650, "PsTerminateSystemThread", 19, 4, "synchronization"),
    (0xE00006F0, "RtlRaiseException", 20, 4, "synchronization"),
    (0xE00007E0, "XcRC4Key", 21, 12, "synchronization"),
    (0xE00007F0, "XcRC4Crypt", 22, 12, "synchronization"),
    (0xE0000800, "XcHMAC", 23, 28, "synchronization"),
    (0xE0000810, "XcVerifyPKCS1Signature", 24, 12, "synchronization"),
    (0xE0000820, "XcModExp", 25, 16, "synchronization"),
    (0xE0000830, "XcDESKeyParity", 26, 4, "synchronization"),
    (0xE0000840, "XcKeyTable", 27, 8, "synchronization"),
    (0xE0000850, "XcBlockCryptCBC", 28, 16, "synchronization"),
)
PHASE7_OFFLINE_KERNEL_SERVICE_SPECS.update(
    {
        target: {
            "shim_name": shim_name,
            "runtime_kind": WORKLOAD_SERVICE_OFFLINE_KERNEL,
            "runtime_value": runtime_value,
            "stack_cleanup_bytes": stack_cleanup_bytes,
            "boundary": boundary,
            "normal_live_body": "implemented-native32",
        }
        for target, shim_name, runtime_value, stack_cleanup_bytes, boundary in (
            PHASE7_OFFLINE_KERNEL_SERVICE_ROWS
        )
    }
)

PHASE7_OFFLINE_BOUNDARY_OPTIMIZATION_INTERVAL = 20
PHASE7_OFFLINE_VTABLE_RDATA_START = 0x00293CA0
PHASE7_OFFLINE_VTABLE_RDATA_END = 0x002CD660
PHASE7_OFFLINE_VTABLE_SLOT_LIMIT = 0x00000400
PHASE7_OFFLINE_BOUNDARY_OPTIMIZATIONS = (
    {
        "id": "phase7-dirty-publication-code-page-filter-v1",
        "kind": "runtime",
        "boundary_credit": PHASE7_OFFLINE_BOUNDARY_OPTIMIZATION_INTERVAL,
        "summary": (
            "skip canonical-page construction for pages without detached guest code and "
            "compare/copy fixed pages eight words per iteration"
        ),
    },
    {
        "id": "phase7-dirty-publication-first-difference-suffix-v1",
        "kind": "runtime",
        "boundary_credit": PHASE7_OFFLINE_BOUNDARY_OPTIMIZATION_INTERVAL,
        "summary": "combine dirty detection and publication after the first differing word",
    },
    {
        "id": "phase7-code-page-sparse-publication-v1",
        "kind": "runtime",
        "boundary_credit": PHASE7_OFFLINE_BOUNDARY_OPTIMIZATION_INTERVAL,
        "summary": "publish mutable code-page gaps without constructing a 4 KiB scratch page",
    },
    {
        "id": "phase7-workload-allocation-last-hit-cache-v1",
        "kind": "runtime",
        "boundary_credit": PHASE7_OFFLINE_BOUNDARY_OPTIMIZATION_INTERVAL,
        "summary": "cache the last native allocation interval used by memory services",
    },
    {
        "id": "phase7-semaphore-handle-direct-cache-v1",
        "kind": "runtime",
        "boundary_credit": PHASE7_OFFLINE_BOUNDARY_OPTIMIZATION_INTERVAL,
        "summary": "cache native semaphore lookups by guest handle",
    },
    {
        "id": "phase7-audio-buffer-open-addressing-v1",
        "kind": "runtime",
        "boundary_credit": PHASE7_OFFLINE_BOUNDARY_OPTIMIZATION_INTERVAL,
        "summary": "replace full audio-buffer scans with key-seeded open addressing",
    },
    {
        "id": "phase7-audio-stream-open-addressing-v1",
        "kind": "runtime",
        "boundary_credit": PHASE7_OFFLINE_BOUNDARY_OPTIMIZATION_INTERVAL,
        "summary": "replace full audio-stream scans with key-seeded open addressing",
    },
    {
        "id": "phase7-file-handle-direct-cache-v1",
        "kind": "runtime",
        "boundary_credit": PHASE7_OFFLINE_BOUNDARY_OPTIMIZATION_INTERVAL,
        "summary": "serve repeated native file operations from a handle-indexed cache",
    },
    {
        "id": "phase7-audio-packet-stream-fifo-v1",
        "kind": "runtime",
        "boundary_credit": PHASE7_OFFLINE_BOUNDARY_OPTIMIZATION_INTERVAL,
        "summary": "replace per-quantum packet minimum scans with per-stream FIFO links",
    },
    {
        "id": "phase7-audio-playback-direct-cache-v1",
        "kind": "runtime",
        "boundary_credit": PHASE7_OFFLINE_BOUNDARY_OPTIMIZATION_INTERVAL,
        "summary": "serve repeated active-playback controls from a key-indexed cache",
    },
    {
        "id": "phase7-texture-binding-direct-cache-v1",
        "kind": "runtime",
        "boundary_credit": PHASE7_OFFLINE_BOUNDARY_OPTIMIZATION_INTERVAL,
        "summary": "cache retained texture bindings by guest resource address",
    },
    {
        "id": "phase7-resource-dedupe-open-addressing-v1",
        "kind": "runtime",
        "boundary_credit": PHASE7_OFFLINE_BOUNDARY_OPTIMIZATION_INTERVAL,
        "summary": "replace quadratic per-frame texture dedupe with generation-stamped hashing",
    },
    {
        "id": "phase7-resource-source-last-hit-cache-v1",
        "kind": "runtime",
        "boundary_credit": PHASE7_OFFLINE_BOUNDARY_OPTIMIZATION_INTERVAL,
        "summary": "cache the last contiguous allocation used for render resource translation",
    },
    {
        "id": "phase7-resource-word-compare-v1",
        "kind": "runtime",
        "boundary_credit": PHASE7_OFFLINE_BOUNDARY_OPTIMIZATION_INTERVAL,
        "summary": "compare retained resource payloads eight words per iteration",
    },
    {
        "id": "phase7-audio-active-playback-mask-v1",
        "kind": "runtime",
        "boundary_credit": PHASE7_OFFLINE_BOUNDARY_OPTIMIZATION_INTERVAL,
        "summary": "mix only active playback slots using two native bit masks",
    },
    {
        "id": "phase7-audio-active-stream-mask-v1",
        "kind": "runtime",
        "boundary_credit": PHASE7_OFFLINE_BOUNDARY_OPTIMIZATION_INTERVAL,
        "summary": "visit only live DirectSound streams in each audio quantum",
    },
    {
        "id": "phase7-audio-packet-free-hint-v1",
        "kind": "runtime",
        "boundary_credit": PHASE7_OFFLINE_BOUNDARY_OPTIMIZATION_INTERVAL,
        "summary": "allocate packet slots from a rotating free-slot hint",
    },
    {
        "id": "phase7-audio-playback-free-mask-v1",
        "kind": "runtime",
        "boundary_credit": PHASE7_OFFLINE_BOUNDARY_OPTIMIZATION_INTERVAL,
        "summary": "reserve free playback slots from complemented active masks",
    },
    {
        "id": "phase7-sha256-direct-block-transform-v1",
        "kind": "runtime",
        "boundary_credit": PHASE7_OFFLINE_BOUNDARY_OPTIMIZATION_INTERVAL,
        "summary": "transform aligned SHA-256 input blocks without staging them through the tail buffer",
    },
    {
        "id": "phase7-sha256-rolling-schedule-v1",
        "kind": "runtime",
        "boundary_credit": PHASE7_OFFLINE_BOUNDARY_OPTIMIZATION_INTERVAL,
        "summary": "reduce the SHA-256 message schedule from 64 stack words to a 16-word rolling ring",
    },
    {
        "id": "phase7-vertex-range-sorted-merge-v1",
        "kind": "runtime",
        "boundary_credit": PHASE7_OFFLINE_BOUNDARY_OPTIMIZATION_INTERVAL,
        "summary": "retain sorted vertex intervals and merge only the contiguous overlap window",
    },
    {
        "id": "phase7-audio-gain-memoization-v1",
        "kind": "runtime",
        "boundary_credit": PHASE7_OFFLINE_BOUNDARY_OPTIMIZATION_INTERVAL,
        "summary": "memoize fixed-point centibel gains in a direct-mapped native table",
    },
    {
        "id": "phase7-audio-idle-quantum-bypass-v1",
        "kind": "runtime",
        "boundary_credit": PHASE7_OFFLINE_BOUNDARY_OPTIMIZATION_INTERVAL,
        "summary": "skip accumulator clearing and mixer locking when playback and stream masks are empty",
    },
    {
        "id": "phase7-audio-accumulator-wide-clear-v1",
        "kind": "runtime",
        "boundary_credit": PHASE7_OFFLINE_BOUNDARY_OPTIMIZATION_INTERVAL,
        "summary": "clear eight native mix accumulators per loop iteration",
    },
    {
        "id": "phase7-audio-unity-resample-fast-path-v1",
        "kind": "runtime",
        "boundary_credit": PHASE7_OFFLINE_BOUNDARY_OPTIMIZATION_INTERVAL,
        "summary": "bypass interpolation multiplies for integer-position 48 kHz playback",
    },
    {
        "id": "phase7-allocation-page-direct-cache-v1",
        "kind": "runtime",
        "boundary_credit": PHASE7_OFFLINE_BOUNDARY_OPTIMIZATION_INTERVAL,
        "summary": "cache workload allocation intervals by guest page before the linear fallback",
    },
)

PHASE7_NORMAL_LIVE_INPUT_AUDIO_SERVICE_TARGETS = frozenset(
    target
    for target, _name, runtime_kind, _value, _cleanup, _boundary in PHASE7_PROFILED_BOOT_SERVICE_ROWS
    if runtime_kind
    in {
        WORKLOAD_SERVICE_XGETDEVICES,
        WORKLOAD_SERVICE_XINPUT_OPEN,
        WORKLOAD_SERVICE_XINPUT_CAPABILITIES,
        WORKLOAD_SERVICE_XINPUT_STATE,
        WORKLOAD_SERVICE_XINPUT_CLOSE,
        WORKLOAD_SERVICE_XINPUT_SET_STATE,
        WORKLOAD_SERVICE_AUDIO,
    }
)
for _target in PHASE7_NORMAL_LIVE_INPUT_AUDIO_SERVICE_TARGETS:
    PHASE7_PROFILED_BOOT_SERVICE_SPECS[_target]["normal_live_body"] = "implemented-native32"

# Native-created DirectSound buffers intentionally omit the private DSOUND
# object graph.  These public configuration entry points therefore form one
# indivisible static host-ABI family: admitting only the entries seen by an
# older coverage profile can expose a guest implementation that dereferences
# the omitted graph when a different frontend/level path configures a buffer.
PHASE7_NATIVE_BUFFER_CONFIGURATION_SERVICE_TARGETS = frozenset(
    target
    for (
        target,
        _name,
        runtime_kind,
        runtime_value,
        _cleanup,
        _boundary,
    ) in PHASE7_PROFILED_BOOT_SERVICE_ROWS
    if runtime_kind == WORKLOAD_SERVICE_AUDIO and 20 <= runtime_value <= 28
)

# Native-created buffer and stream shells retain only the public interface
# shape needed by the title.  Their two reached release paths must therefore
# stay at the same native boundary as creation: entering the private DSOUND
# destructors would dereference an object graph the native mixer does not own.
PHASE7_NATIVE_AUDIO_LIFETIME_SERVICE_TARGETS = frozenset(
    target
    for (
        target,
        _name,
        runtime_kind,
        runtime_value,
        _cleanup,
        _boundary,
    ) in PHASE7_PROFILED_BOOT_SERVICE_ROWS
    if runtime_kind == WORKLOAD_SERVICE_AUDIO and runtime_value in {29, 30}
)

# XInputOpen returns a native-owned synthetic handle rather than the private
# Xbox device object used by the title's linked XInput implementation.  Every
# operation that consumes or destroys that handle must consequently remain on
# the same explicit host ABI boundary.  Keep the complete finite pair even
# when a capture reaches only rumble or only hot-plug teardown.
PHASE7_NATIVE_XINPUT_CONTROL_SERVICE_TARGETS = frozenset(
    target
    for (
        target,
        _name,
        runtime_kind,
        _runtime_value,
        _cleanup,
        _boundary,
    ) in PHASE7_PROFILED_BOOT_SERVICE_ROWS
    if runtime_kind
    in {WORKLOAD_SERVICE_XINPUT_CLOSE, WORKLOAD_SERVICE_XINPUT_SET_STATE}
)

# Phase-7 normal-live ports are promoted in subsystem-sized groups.  Keeping
# this list next to the measured service table makes the build manifest a
# fail-closed inventory: a service is not launchable merely because a generic
# Phase-4 descriptor exists for it.
PHASE7_NORMAL_LIVE_CORE_SERVICE_TARGETS = frozenset(
    {
        0xE0000000,  # AvGetSavedDataAddress
        0xE0000010,  # AvSendTVEncoderOption
        0xE0000020,  # AvSetDisplayMode
        0xE0000030,  # AvSetSavedDataAddress
        0xE0000090,  # ExQueryNonVolatileSetting
        0xE00000D0,  # HalGetInterruptVector
        0xE00000E0,  # HalReadWritePCISpace
        0xE0000120,  # IoCreateDevice
        0xE00001F0,  # KeConnectInterrupt
        0xE0000220,  # KeInitializeDpc
        0xE0000230,  # KeInitializeInterrupt
        0xE0000240,  # KeInitializeTimerEx
        0xE0000290,  # KeRaiseIrqlToDpcLevel
        0xE00002D0,  # KeSetBasePriorityThread
        0xE0000310,  # KeStallExecutionProcessor
        0xE0000350,  # KeWaitForSingleObject
        0xE0000390,  # MmAllocateContiguousMemory
        0xE00003B0,  # MmClaimGpuInstanceMemory
        0xE00003C0,  # MmFreeContiguousMemory
        0xE00003D0,  # MmGetPhysicalAddress
        0xE00003E0,  # MmLockUnlockBufferPages
        0xE0000400,  # MmPersistContiguousMemory
        0xE0000420,  # MmQueryAllocationSize
        0xE0000450,  # NtAllocateVirtualMemory
        0xE0000460,  # NtClose
        0xE0000490,  # NtCreateSemaphore
        0xE00004D0,  # NtFreeVirtualMemory
        0xE00005F0,  # ObReferenceObjectByHandle
        0xE0000640,  # PsCreateSystemThreadEx
        0xE0000670,  # RtlAnsiStringToUnicodeString
        0xE00006A0,  # RtlEqualString
        0xE00006B0,  # RtlInitAnsiString
        0xE00006E0,  # RtlNtStatusToDosError
        0xE0000720,  # RtlUnicodeStringToAnsiString
    }
)
for _target in PHASE7_NORMAL_LIVE_CORE_SERVICE_TARGETS:
    PHASE7_PROFILED_BOOT_SERVICE_SPECS[_target]["normal_live_body"] = "implemented-native32"

PHASE7_NORMAL_LIVE_FILE_CRYPTO_TIME_TARGETS = frozenset(
    {
        0xE0000480,  # NtCreateFile
        0xE00004F0,  # NtOpenFile
        0xE0000500,  # NtOpenSymbolicLinkObject
        0xE0000510,  # NtQueryDirectoryFile
        0xE0000520,  # NtQueryInformationFile
        0xE0000530,  # NtQuerySymbolicLinkObject
        0xE0000550,  # NtQueryVolumeInformationFile
        0xE0000560,  # NtReadFile
        0xE0000590,  # NtSetInformationFile
        0xE00005D0,  # NtWriteFile
        0xE0000700,  # RtlTimeFieldsToTime
        0xE0000710,  # RtlTimeToTimeFields
        0xE00007B0,  # XcSHAInit
        0xE00007C0,  # XcSHAUpdate
        0xE00007D0,  # XcSHAFinal
    }
)
for _target in PHASE7_NORMAL_LIVE_FILE_CRYPTO_TIME_TARGETS:
    PHASE7_PROFILED_BOOT_SERVICE_SPECS[_target]["normal_live_body"] = "implemented-native32"

SCRATCH_REGISTER_OFFSETS = {
    "eax": 0,
    "ecx": 4,
    "edx": 8,
    "ebx": 12,
    "esp": 16,
    "ebp": 20,
    "esi": 24,
    "edi": 28,
}
SCRATCH_EFLAGS_OFFSET = 32
SCRATCH_ENTRY_EIP_OFFSET = 36
SCRATCH_HOST_ESP_OFFSET = 40
SCRATCH_FXSAVE_OFFSET = 48
SCRATCH_HOST_EBX_OFFSET = SCRATCH_FXSAVE_OFFSET + EXCHANGE_FXSAVE_SIZE
SCRATCH_HOST_ESI_OFFSET = SCRATCH_HOST_EBX_OFFSET + 4
SCRATCH_HOST_EDI_OFFSET = SCRATCH_HOST_ESI_OFFSET + 4
SCRATCH_HOST_EBP_OFFSET = SCRATCH_HOST_EDI_OFFSET + 4
SCRATCH_SERVICE_DISPATCH_OFFSET = SCRATCH_HOST_EBP_OFFSET + 4
SCRATCH_SERVICE_FXSAVE_OFFSET = 0x400

REJECTED_MNEMONICS = frozenset(
    {
        "cli",
        "cpuid",
        "hlt",
        "in",
        "int",
        "int3",
        "invd",
        "invlpg",
        "lgdt",
        "lidt",
        "lldt",
        "lmsw",
        "ltr",
        "out",
        "rdmsr",
        "rdpmc",
        "rdtsc",
        "rdtscp",
        "sidt",
        "sgdt",
        "sldt",
        "smsw",
        "sti",
        "str",
        "syscall",
        "sysenter",
        "sysexit",
        "sysret",
        "ud2",
        "wbinvd",
        "wrmsr",
    }
)


class Ia32BackendError(RuntimeError):
    """Raised when a slice is not statically safe or its helper fails."""


class Ia32GuestFault(Ia32BackendError):
    """A recoverable guest exception published by a resident Phase-3 worker."""

    def __init__(
        self,
        message: str,
        *,
        code: int,
        guest_eip: int,
        access_address: int | None = None,
        access_kind: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.guest_eip = guest_eip
        self.access_address = access_address
        self.access_kind = access_kind


@dataclass(frozen=True)
class Ia32SliceArtifact:
    root: Path
    executable: Path
    manifest_path: Path
    manifest: dict[str, Any]

    @property
    def artifact_id(self) -> str:
        return str(self.manifest["artifact_id"])


@dataclass(frozen=True)
class _FixedSlicePreflight:
    instructions: dict[int, X86Instruction]
    page_addresses: tuple[int, ...]
    memory_ranges: tuple[tuple[int, int], ...]
    write_ranges: tuple[tuple[int, int], ...]
    function_symbols: tuple[str, ...]
    indirect_targets: dict[int, tuple[int, ...]]
    host_service_thunks: tuple[_HostServiceThunk, ...]
    host_service_calls: tuple[dict[str, Any], ...]
    steps: int


@dataclass(frozen=True)
class _HostServiceThunk:
    target: int
    shim_name: str
    kind: int
    value: int
    stack_cleanup_bytes: int
    calling_convention: str = "stdcall"
    argument_count: int = 0
    boundary: str = "synchronization"
    execution: str = "native32"
    memory_write_argument: int = -1
    memory_write_value: int = 0
    callback_argument: int = -1
    callback_context_argument: int = -1
    callback_value: int = 0
    callback_stack_cleanup_bytes: int = 0
    runtime_kind: int = 0
    runtime_value: int = 0
    normal_live_body: str = "implemented-native32"


@dataclass(frozen=True)
class Ia32SliceResult:
    state: CpuState
    memory: SparseMemory
    elapsed_ns: int
    process_elapsed_ns: int
    artifact_id: str
    capsule_id: str
    execution_contract_id: str
    toolchain_id: str
    stop_eip: int
    resident_worker: bool = False
    worker_process_id: int | None = None
    worker_dispatch_index: int = 0
    worker_startup_ns: int = 0
    resident_dispatch_ns: int = 0
    dirty_publications: tuple[dict[str, Any], ...] = ()
    host_service_calls: tuple[dict[str, Any], ...] = ()
    resident_scheduler: dict[str, Any] | None = None
    memory_transport: dict[str, Any] | None = None

    def summary(self) -> dict[str, Any]:
        return {
            "backend": "same-isa-ia32-prototype",
            "artifact_id": self.artifact_id,
            "capsule_id": self.capsule_id,
            "execution_contract_id": self.execution_contract_id,
            "toolchain_id": self.toolchain_id,
            "timing_protocol_version": TIMING_PROTOCOL_VERSION,
            "resident_worker": self.resident_worker,
            "worker_process_id": self.worker_process_id,
            "worker_dispatch_index": self.worker_dispatch_index,
            "worker_startup_ns": self.worker_startup_ns,
            "resident_dispatch_ns": self.resident_dispatch_ns,
            "stop_eip": self.stop_eip,
            "stop_eip_hex": f"0x{self.stop_eip:08X}",
            "elapsed_ns": self.elapsed_ns,
            "process_elapsed_ns": self.process_elapsed_ns,
            "guest_eip": self.state.eip,
            "guest_eip_hex": f"0x{self.state.eip:08X}",
            "cpu_state": self.state.to_dict(),
            "memory_page_count": self.memory.allocated_page_count,
            "dirty_publications": list(self.dirty_publications),
            "host_service_calls": list(self.host_service_calls),
            "resident_scheduler": self.resident_scheduler,
            "memory_transport": self.memory_transport,
        }


@dataclass(frozen=True)
class Ia32SliceReferenceResult:
    state: CpuState
    memory: SparseMemory
    steps: int
    stop_eip: int
    host_service_thunks: tuple[_HostServiceThunk, ...]
    host_service_calls: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class Ia32SchedulerReferenceResult:
    state: CpuState
    memory: SparseMemory
    lane_states: tuple[dict[str, Any], ...]
    scheduler_trace: tuple[dict[str, Any], ...]
    host_service_calls: tuple[dict[str, Any], ...]
    worker_wakeups: int
    completed_flips: int
    render_hash: int
    audio_hash: int


@dataclass(frozen=True)
class Ia32DecodedStorePlan:
    """Canonical, XBE-bound view of decoded blocks consumed by Phase 2."""

    xbe_sha256: str
    store_snapshot_id: str
    inputs: dict[str, Any]
    functions: tuple[LiftedFunction, ...]
    coverage_map: dict[str, Any]
    section_map: dict[str, Any]
    direct_edge_relocations: dict[str, Any]
    indirect_target_table: dict[str, Any]
    rewrite_manifest: dict[str, Any]


@dataclass(frozen=True)
class Ia32ArchitectureProfile:
    """Build-time Phase-3 ownership and exceptional-memory declarations."""

    dirty_page_owners: Mapping[int, str] | None = None
    recoverable_fault_pages: tuple[int, ...] = ()
    mmio_shadow_pages: Mapping[int, int] | None = None


@dataclass(frozen=True)
class Ia32CoverageProfile:
    """Measured executable targets and transitions for one supported workload."""

    workload: str
    mode: str
    targets: tuple[Mapping[str, Any], ...]
    transitions: tuple[Mapping[str, Any], ...] = ()
    boundary_exits: tuple[Mapping[str, Any], ...] = ()
    frontier_interpreter_invocations: int = 0
    frontier_interpreter_steps: int = 0


@dataclass(frozen=True)
class Ia32CoverageGrowthPlan:
    """Frozen Phase-6 ranking, closure, and promotion decision."""

    coverage_growth_map: dict[str, Any]


@dataclass(frozen=True)
class _Ia32ArchitecturePlan:
    instructions: dict[int, X86Instruction]
    extra_code_spans: tuple[tuple[int, bytes], ...]
    page_bindings: tuple[dict[str, Any], ...]
    rewrites: tuple[dict[str, Any], ...]
    dirty_ownership: tuple[dict[str, Any], ...]
    fault_pages: tuple[int, ...]
    timestamp_step_count: int
    architecture_map: dict[str, Any]


@dataclass(frozen=True)
class _Ia32SchedulerLane:
    lane_id: int
    name: str
    initial_eip: int
    tls_base: int
    initial_state: int
    registers: tuple[int, ...]
    eflags: int
    wait_object: int


@dataclass(frozen=True)
class _Ia32SchedulerStep:
    sequence: int
    lane_id: int
    entry_eip: int
    next_eip: int
    exit_kind: int
    wake_lane_id: int
    wait_object: int


@dataclass(frozen=True)
class _Ia32SchedulerPlan:
    lanes: tuple[_Ia32SchedulerLane, ...]
    steps: tuple[_Ia32SchedulerStep, ...]
    active_tls_base: int
    render_address: int
    render_size: int
    audio_address: int
    audio_size: int
    required_pages: tuple[int, ...]
    scheduler_map: dict[str, Any]


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode("utf-8")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _u32(value: int) -> int:
    return value & 0xFFFFFFFF


def _allocation_region(address: int) -> int:
    return address & ~(WINDOWS_ALLOCATION_GRANULARITY - 1)


def _address_range_collides(start: int, size: int, other_start: int, other_end: int) -> bool:
    end = start + size
    return start < other_end and end > other_start


def _instruction_map(functions: Iterable[LiftedFunction]) -> dict[int, X86Instruction]:
    instructions: dict[int, X86Instruction] = {}
    occupied_bytes: dict[int, int] = {}
    for function in functions:
        for instruction in function.instructions:
            previous_instruction = instructions.get(instruction.address)
            if previous_instruction is not None:
                if previous_instruction != instruction:
                    raise Ia32BackendError(
                        f"conflicting decoded instructions begin at 0x{instruction.address:08X}"
                    )
                continue
            try:
                payload = bytes.fromhex(instruction.bytes_hex)
            except ValueError as exc:
                raise Ia32BackendError(
                    f"instruction 0x{instruction.address:08X} has invalid byte provenance"
                ) from exc
            if len(payload) != instruction.size:
                raise Ia32BackendError(
                    f"instruction 0x{instruction.address:08X} has {len(payload)} verified "
                    f"bytes but decoded size {instruction.size}"
                )
            for offset, value in enumerate(payload):
                address = instruction.address + offset
                previous = occupied_bytes.get(address)
                if previous is not None and previous != value:
                    raise Ia32BackendError(f"conflicting decoded bytes overlap at 0x{address:08X}")
                occupied_bytes[address] = value
            instructions[instruction.address] = instruction
    if not instructions:
        raise Ia32BackendError("IA-32 slice contains no decoded instructions")
    return instructions


class _PreflightMemory:
    """Record exact fixed-slice memory dependencies without copying pages."""

    def __init__(self, memory: SparseMemory) -> None:
        self._memory = memory
        self.pages: set[int] = set()
        self.ranges: list[tuple[int, int]] = []
        self.write_ranges: list[tuple[int, int]] = []
        self._synthetic_u32_reads: dict[int, list[int]] = {}

    def _record(self, address: int, size: int, *, write: bool = False) -> None:
        if size <= 0:
            return
        address = _u32(address)
        self.pages.add(address >> 12)
        self.pages.add(_u32(address + size - 1) >> 12)
        end = address + size
        if end <= 0x100000000:
            self.ranges.append((address, size))
            if write:
                self.write_ranges.append((address, size))
        else:
            self.ranges.append((address, 0x100000000 - address))
            self.ranges.append((0, end - 0x100000000))
            if write:
                self.write_ranges.append((address, 0x100000000 - address))
                self.write_ranges.append((0, end - 0x100000000))

    def read(self, address: int, size: int) -> bytes:
        self._record(address, size)
        return bytes(self._memory.read(address, size))

    def read_u32(self, address: int) -> int:
        synthetic = self._synthetic_u32_reads.get(_u32(address))
        if synthetic:
            value = synthetic.pop(0)
            if not synthetic:
                del self._synthetic_u32_reads[_u32(address)]
            return value
        self._record(address, 4)
        return int(self._memory.read_u32(address))

    def write(self, address: int, payload: bytes) -> None:
        self._record(address, len(payload), write=True)
        self._memory.write(address, payload)

    def write_u32(self, address: int, value: int) -> None:
        self._record(address, 4, write=True)
        self._memory.write_u32(address, value)

    def prepare_stdcall_return(self, state: CpuState, stack_cleanup_bytes: int) -> None:
        """Model RET N around the lifter's mandatory external-call pop."""

        stack_pointer = state.get_register("esp")
        return_address = self.read_u32(stack_pointer)
        synthetic_address = _u32(stack_pointer + stack_cleanup_bytes)
        self._synthetic_u32_reads.setdefault(synthetic_address, []).append(return_address)
        state.set_register("esp", synthetic_address)


def _merged_memory_ranges(
    ranges: Iterable[tuple[int, int]],
) -> tuple[tuple[int, int], ...]:
    merged: list[list[int]] = []
    for address, size in sorted(ranges):
        end = address + size
        if merged and address <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([address, end])
    return tuple((start, end - start) for start, end in merged)


def _capsule_host_service_thunks(
    capsule: ReplayCapsule,
) -> dict[int, _HostServiceThunk]:
    registry = capsule.service_state.get("service_registry")
    if registry is None:
        registry = capsule.service_state.get("service_trace", [])
    if not isinstance(registry, list):
        raise Ia32BackendError("capsule host-service registry must be a list")
    services: dict[int, _HostServiceThunk] = {}
    for record in registry:
        if not isinstance(record, dict):
            raise Ia32BackendError("capsule host-service registry contains a non-record")
        shim_name = record.get("shim_name")
        if not isinstance(shim_name, str) or not shim_name:
            raise Ia32BackendError("capsule host-service registry contains an unnamed service")
        try:
            target = _u32(int(record["target"]))
            kind = int(record["kind"])
            value = _u32(int(record["value"]))
        except (KeyError, TypeError, ValueError):
            raise Ia32BackendError(f"capsule service {shim_name} has malformed core fields")
        legacy = (
            shim_name in SUPPORTED_RETURN_CONSTANT_SERVICES
            and "calling_convention" not in record
            and "argument_count" not in record
        )
        calling_convention = str(record.get("calling_convention", "stdcall"))
        try:
            argument_count = int(record.get("argument_count", 1 if legacy else 0))
            stack_cleanup_bytes = int(
                record.get(
                    "stack_cleanup_bytes",
                    SUPPORTED_RETURN_CONSTANT_SERVICES.get(shim_name, 0),
                )
            )
            memory_write_argument = int(record.get("memory_write_argument", -1))
            memory_write_value = _u32(int(record.get("memory_write_value", 0)))
            callback_argument = int(record.get("callback_argument", -1))
            callback_context_argument = int(record.get("callback_context_argument", -1))
            callback_value = _u32(int(record.get("callback_value", 0)))
            callback_stack_cleanup_bytes = int(record.get("callback_stack_cleanup_bytes", 0))
            runtime_kind = int(record.get("runtime_kind", 0))
            runtime_value = int(record.get("runtime_value", 0))
        except (TypeError, ValueError) as exc:
            raise Ia32BackendError(f"capsule service {shim_name} has malformed ABI fields") from exc
        boundary = str(record.get("boundary", "synchronization"))
        execution = str(record.get("execution", "native32"))
        normal_live_body = str(record.get("normal_live_body", "implemented-native32"))
        if kind not in {
            NATIVE_HOST_SERVICE_RETURN_CONSTANT,
            NATIVE_HOST_SERVICE_WRITE_U32,
            NATIVE_HOST_SERVICE_CALLBACK,
            NATIVE_HOST_SERVICE_WORKLOAD_ABI,
        }:
            raise Ia32BackendError(
                f"capsule service {shim_name} at 0x{target:08X} has unsupported kind {kind}"
            )
        title_service_target = (
            0x31F10000 <= target < 0x31F20000
            or target in PHASE7_NORMAL_LIVE_INPUT_AUDIO_SERVICE_TARGETS
        )
        if (target < 0xC0000000 and not title_service_target) or (
            target & 0xF and target not in PHASE7_NORMAL_LIVE_INPUT_AUDIO_SERVICE_TARGETS
        ):
            raise Ia32BackendError(
                f"capsule service {shim_name} has invalid thunk address 0x{target:08X}"
            )
        if calling_convention not in {"stdcall", "cdecl"}:
            raise Ia32BackendError(
                f"capsule service {shim_name} has unsupported calling convention "
                f"{calling_convention!r}"
            )
        maximum_arguments = (
            NORMAL_LIVE_NATIVE32_MAX_ARGUMENTS
            if target in PHASE7_PROFILED_BOOT_SERVICE_SPECS and execution == "native32"
            else 5
        )
        if argument_count < 0 or argument_count > maximum_arguments:
            raise Ia32BackendError(
                f"capsule service {shim_name} argument count must be between zero "
                f"and {maximum_arguments}"
            )
        expected_cleanup = argument_count * 4 if calling_convention == "stdcall" else 0
        if stack_cleanup_bytes != expected_cleanup:
            raise Ia32BackendError(
                f"capsule service {shim_name} stack cleanup {stack_cleanup_bytes} does not "
                f"match {calling_convention} argument count {argument_count}"
            )
        if boundary not in HOST_SERVICE_BOUNDARIES:
            raise Ia32BackendError(
                f"capsule service {shim_name} has unsupported boundary {boundary!r}"
            )
        if execution not in {"native32", "native64-broker"}:
            raise Ia32BackendError(
                f"capsule service {shim_name} has unsupported execution owner {execution!r}"
            )
        if execution == "native64-broker" and argument_count > 4:
            raise Ia32BackendError(
                f"capsule service {shim_name} exceeds the scalar broker argument capacity"
            )
        if execution == "native64-broker" and kind == NATIVE_HOST_SERVICE_CALLBACK:
            raise Ia32BackendError(
                f"capsule service {shim_name} cannot invoke IA-32 callbacks from the broker"
            )
        if memory_write_argument >= argument_count:
            raise Ia32BackendError(
                f"capsule service {shim_name} memory-write argument is out of range"
            )
        if kind == NATIVE_HOST_SERVICE_WRITE_U32 and memory_write_argument < 0:
            raise Ia32BackendError(
                f"capsule service {shim_name} write kind has no memory-write argument"
            )
        if kind == NATIVE_HOST_SERVICE_CALLBACK:
            if not (0 <= callback_argument < argument_count):
                raise Ia32BackendError(
                    f"capsule service {shim_name} callback argument is out of range"
                )
            if not (0 <= callback_context_argument < argument_count):
                raise Ia32BackendError(
                    f"capsule service {shim_name} callback context argument is out of range"
                )
            if callback_stack_cleanup_bytes not in {0, 8}:
                raise Ia32BackendError(
                    f"capsule service {shim_name} callback cleanup must be cdecl or stdcall"
                )
        if kind == NATIVE_HOST_SERVICE_WORKLOAD_ABI:
            if runtime_kind not in {
                WORKLOAD_SERVICE_SYSTEM_TIME,
                WORKLOAD_SERVICE_XGETDEVICES,
                WORKLOAD_SERVICE_XINPUT_OPEN,
                WORKLOAD_SERVICE_XINPUT_CAPABILITIES,
                WORKLOAD_SERVICE_XINPUT_STATE,
                WORKLOAD_SERVICE_COLD_CALLBACK,
                WORKLOAD_SERVICE_IRQL,
                WORKLOAD_SERVICE_SEMAPHORE,
                WORKLOAD_SERVICE_RUNTIME,
                WORKLOAD_SERVICE_MEMORY,
                WORKLOAD_SERVICE_AUDIO,
                WORKLOAD_SERVICE_TITLE,
                WORKLOAD_SERVICE_BOOTSTRAP,
                WORKLOAD_SERVICE_XINPUT_CLOSE,
                WORKLOAD_SERVICE_XINPUT_SET_STATE,
            }:
                raise Ia32BackendError(
                    f"capsule service {shim_name} has unsupported workload kind {runtime_kind}"
                )
        elif runtime_kind or runtime_value:
            raise Ia32BackendError(
                f"capsule service {shim_name} has workload fields on generic kind {kind}"
            )
        service = _HostServiceThunk(
            target=target,
            shim_name=str(shim_name),
            kind=kind,
            value=value,
            stack_cleanup_bytes=stack_cleanup_bytes,
            calling_convention=calling_convention,
            argument_count=argument_count,
            boundary=boundary,
            execution=execution,
            memory_write_argument=memory_write_argument,
            memory_write_value=memory_write_value,
            callback_argument=callback_argument,
            callback_context_argument=callback_context_argument,
            callback_value=callback_value,
            callback_stack_cleanup_bytes=callback_stack_cleanup_bytes,
            runtime_kind=runtime_kind,
            runtime_value=runtime_value,
            normal_live_body=normal_live_body,
        )
        previous = services.get(target)
        if previous is not None and previous != service:
            raise Ia32BackendError(f"capsule service trace conflicts at 0x{target:08X}")
        services[target] = service
    return services


def _host_service_record(service: _HostServiceThunk) -> dict[str, Any]:
    return {
        "target": service.target,
        "shim_name": service.shim_name,
        "kind": service.kind,
        "value": service.value,
        "stack_cleanup_bytes": service.stack_cleanup_bytes,
        "calling_convention": service.calling_convention,
        "argument_count": service.argument_count,
        "boundary": service.boundary,
        "execution": service.execution,
        "memory_write_argument": service.memory_write_argument,
        "memory_write_value": service.memory_write_value,
        "callback_argument": service.callback_argument,
        "callback_context_argument": service.callback_context_argument,
        "callback_value": service.callback_value,
        "callback_stack_cleanup_bytes": service.callback_stack_cleanup_bytes,
        "runtime_kind": service.runtime_kind,
        "runtime_value": service.runtime_value,
        "normal_live_body": service.normal_live_body,
    }


def _phase6_host_service_thunks(
    capsule: ReplayCapsule,
    profile: Ia32CoverageProfile,
    *,
    complete_offline_registry: bool = False,
) -> dict[int, _HostServiceThunk]:
    """Merge exact supported-workload ABI descriptors into a capture registry."""

    services = _capsule_host_service_thunks(capsule)
    service_specs = {
        **SUPPORTED_WORKLOAD_SERVICE_SPECS,
        **PHASE7_PROFILED_BOOT_SERVICE_SPECS,
        **PHASE7_OFFLINE_KERNEL_SERVICE_SPECS,
    }
    profile_record = _normalized_coverage_profile(profile)
    observed_external_targets = {
        int(record["target"]) for record in profile_record["boundary_exits"] if "target" in record
    }
    observed_external_targets.update(
        int(record["target"])
        for record in profile_record["transitions"]
        if int(record["target"]) in service_specs
    )
    observed_external_targets.update(
        target
        for cell, target in PHASE7_CAPTURED_SERVICE_IMPORT_CELLS.items()
        if capsule.memory.read_u32(cell) == target
    )
    observed_audio_boundary = any(
        target in PHASE7_NORMAL_LIVE_INPUT_AUDIO_SERVICE_TARGETS
        and int(
            cast(int, PHASE7_PROFILED_BOOT_SERVICE_SPECS[target]["runtime_kind"])
        )
        == WORKLOAD_SERVICE_AUDIO
        for target in observed_external_targets
    )
    observed_xinput_boundary = any(
        target in PHASE7_NORMAL_LIVE_INPUT_AUDIO_SERVICE_TARGETS
        and int(
            cast(int, PHASE7_PROFILED_BOOT_SERVICE_SPECS[target]["runtime_kind"])
        )
        in {
            WORKLOAD_SERVICE_XGETDEVICES,
            WORKLOAD_SERVICE_XINPUT_OPEN,
            WORKLOAD_SERVICE_XINPUT_CAPABILITIES,
            WORKLOAD_SERVICE_XINPUT_STATE,
            WORKLOAD_SERVICE_XINPUT_CLOSE,
            WORKLOAD_SERVICE_XINPUT_SET_STATE,
        }
        for target in observed_external_targets
    )
    if observed_audio_boundary:
        observed_external_targets.update(PHASE7_NATIVE_BUFFER_CONFIGURATION_SERVICE_TARGETS)
        observed_external_targets.update(PHASE7_NATIVE_AUDIO_LIFETIME_SERVICE_TARGETS)
    if observed_xinput_boundary:
        observed_external_targets.update(PHASE7_NATIVE_XINPUT_CONTROL_SERVICE_TARGETS)
    if complete_offline_registry:
        observed_external_targets.update(
            target
            for target, spec in service_specs.items()
            if spec.get("normal_live_body", "implemented-native32")
            == "implemented-native32"
        )
    for target in sorted(observed_external_targets):
        if target in services:
            continue
        spec = service_specs.get(target)
        if spec is None:
            continue
        cleanup = int(spec["stack_cleanup_bytes"])
        services[target] = _HostServiceThunk(
            target=target,
            shim_name=str(spec["shim_name"]),
            kind=NATIVE_HOST_SERVICE_WORKLOAD_ABI,
            value=0,
            stack_cleanup_bytes=cleanup,
            argument_count=cleanup // 4,
            boundary=str(spec["boundary"]),
            runtime_kind=int(spec["runtime_kind"]),
            runtime_value=int(spec["runtime_value"]),
            normal_live_body=str(spec.get("normal_live_body", "implemented-native32")),
        )
    return services


def _capsule_with_host_services(
    capsule: ReplayCapsule,
    services: Mapping[int, _HostServiceThunk],
) -> ReplayCapsule:
    service_state = dict(capsule.service_state)
    service_state["service_registry"] = [
        _host_service_record(services[target]) for target in sorted(services)
    ]
    return replace(capsule, service_state=service_state)


def _trace_fixed_slice(
    capsule: ReplayCapsule,
    stop_eip: int,
) -> tuple[_FixedSlicePreflight, CpuState, SparseMemory]:
    """Trace one decoded capsule slice before emitting its static artifact."""

    entry_eip = _u32(capsule.state.eip)
    stop_eip = _u32(stop_eip)
    owner_by_address: dict[int, LiftedFunction] = {}
    for function in capsule.functions:
        for instruction in function.instructions:
            owner_by_address.setdefault(instruction.address, function)
    entry_function = owner_by_address.get(entry_eip)
    if entry_function is None:
        raise Ia32BackendError(f"entry address 0x{entry_eip:08X} is not decoded")
    if stop_eip not in owner_by_address:
        raise Ia32BackendError(f"stop address 0x{stop_eip:08X} is not decoded")

    state, memory = clone_capsule_state(capsule)
    traced_memory = _PreflightMemory(memory)
    selected_functions = [entry_function]
    selected_function_ids = {id(entry_function)}
    indirect_targets: dict[int, set[int]] = {}
    available_host_services = _capsule_host_service_thunks(capsule)
    invoked_host_services: dict[int, _HostServiceThunk] = {}
    host_service_calls: list[dict[str, Any]] = []

    def call_host_service(
        active_state: CpuState,
        active_memory: SparseMemory,
        target: int,
        _trace: Any,
    ) -> None:
        service = available_host_services[target]
        if not isinstance(active_memory, _PreflightMemory):
            raise Ia32BackendError("host-service preflight requires traced memory")
        entry_esp = active_state.get_register("esp")
        preserved_ecx = active_state.get_register("ecx")
        preserved_edx = active_state.get_register("edx")
        preserved_flags = CpuFlags(**active_state.flags.to_dict())
        preserved_mxcsr = active_state.mxcsr
        preserved_fpu_control_word = active_state.fpu_control_word
        preserved_fpu_status_word = active_state.fpu_status_word
        preserved_fpu_stack = list(active_state.fpu_stack)
        preserved_xmm = dict(active_state.xmm_registers)
        preserved_mmx = dict(active_state.mmx_registers)
        arguments = tuple(
            active_memory.read_u32(_u32(entry_esp + 4 + index * 4))
            for index in range(service.argument_count)
        )
        memory_address = 0
        memory_before = 0
        memory_after = 0
        if service.memory_write_argument >= 0:
            memory_address = arguments[service.memory_write_argument]
            memory_before = active_memory.read_u32(memory_address)
            active_memory.write_u32(memory_address, service.memory_write_value)
            memory_after = active_memory.read_u32(memory_address)
        callback_target = 0
        callback_return = 0
        callback_count = 0
        if service.kind == NATIVE_HOST_SERVICE_CALLBACK:
            callback_target = arguments[service.callback_argument]
            callback_context = arguments[service.callback_context_argument]
            callback_function = load_block(callback_target)
            if callback_function is None:
                raise Ia32BackendError(
                    f"capsule service {service.shim_name} callback target "
                    f"0x{callback_target:08X} is not decoded"
                )
            callback_sentinel = 0xEFFF0000
            callback_esp = _u32(entry_esp - 12)
            active_memory.write_u32(callback_esp, callback_sentinel)
            active_memory.write_u32(callback_esp + 4, callback_context)
            active_memory.write_u32(callback_esp + 8, service.callback_value)
            active_state.set_register("esp", callback_esp)
            active_state.eip = callback_target
            callback_result = execute_lifted_function(
                callback_function,
                state=active_state,
                memory=active_memory,
                call_handlers={target: call_host_service for target in available_host_services},
                block_loader=load_block,
                max_steps=10_000,
                record_instruction_trace=False,
                record_trace=False,
                return_on_missing_instruction=True,
                preserve_initial_eip=True,
            )
            if callback_result.return_address != callback_sentinel:
                raise Ia32BackendError(
                    f"capsule service {service.shim_name} callback returned to "
                    f"0x{callback_result.return_address:08X}"
                )
            if service.callback_stack_cleanup_bytes == 0:
                active_state.set_register("esp", active_state.get_register("esp") + 8)
            if active_state.get_register("esp") != entry_esp:
                raise Ia32BackendError(
                    f"capsule service {service.shim_name} callback stack delta is incorrect"
                )
            callback_return = active_state.get_register("eax")
            callback_count = 1
        active_state.set_register("ecx", preserved_ecx)
        active_state.set_register("edx", preserved_edx)
        active_state.flags = preserved_flags
        active_state.mxcsr = preserved_mxcsr
        active_state.fpu_control_word = preserved_fpu_control_word
        active_state.fpu_status_word = preserved_fpu_status_word
        active_state.fpu_stack = preserved_fpu_stack
        active_state.xmm_registers = preserved_xmm
        active_state.mmx_registers = preserved_mmx
        active_state.set_register("eax", service.value)
        active_memory.prepare_stdcall_return(
            active_state,
            service.stack_cleanup_bytes,
        )
        invoked_host_services[target] = service
        host_service_calls.append(
            {
                "sequence": len(host_service_calls),
                "target": target,
                "shim_name": service.shim_name,
                "kind": service.kind,
                "boundary": service.boundary,
                "execution": service.execution,
                "calling_convention": service.calling_convention,
                "argument_count": service.argument_count,
                "stack_cleanup_bytes": service.stack_cleanup_bytes,
                "entry_esp": entry_esp,
                "return_esp": _u32(entry_esp + 4 + service.stack_cleanup_bytes),
                "arguments": list(arguments),
                "return_value": service.value,
                "memory_address": memory_address,
                "memory_before": memory_before,
                "memory_after": memory_after,
                "callback_target": callback_target,
                "callback_return": callback_return,
                "callback_count": callback_count,
                "runtime_kind": service.runtime_kind,
                "runtime_value": service.runtime_value,
                "error": 0,
            }
        )

    def resolve_indirect_target(instruction: X86Instruction) -> int:
        if len(instruction.operands) != 1:
            raise Ia32BackendError(
                f"indirect transfer at 0x{instruction.address:08X} has no target operand"
            )
        operand = instruction.operands[0]
        if operand.kind == "reg" and operand.reg is not None:
            return int(state.get_register(operand.reg))
        if operand.kind != "mem":
            raise Ia32BackendError(
                f"indirect transfer at 0x{instruction.address:08X} has an unsupported "
                f"{operand.kind} target"
            )
        address = operand.absolute if operand.absolute is not None else operand.displacement
        if operand.base is not None:
            address += state.get_register(operand.base)
        if operand.index is not None:
            address += state.get_register(operand.index) * operand.scale
        if operand.segment is not None:
            address += state.fs_base
        return traced_memory.read_u32(_u32(address))

    def observe_preflight(
        _state: CpuState,
        _memory: SparseMemory,
        _trace: Any,
        _steps: int,
    ) -> None:
        instruction = owner_by_address.get(state.eip)
        if instruction is None:
            return
        current = next(
            (candidate for candidate in instruction.instructions if candidate.address == state.eip),
            None,
        )
        if (
            current is not None
            and current.mnemonic.casefold() in {"call", "jmp"}
            and current.target is None
        ):
            indirect_targets.setdefault(current.address, set()).add(
                resolve_indirect_target(current)
            )

    def load_block(target: int) -> LiftedFunction | None:
        function = owner_by_address.get(_u32(target))
        if function is not None and id(function) not in selected_function_ids:
            selected_functions.append(function)
            selected_function_ids.add(id(function))
        return function

    result = execute_lifted_function(
        entry_function,
        state=state,
        memory=traced_memory,
        block_loader=load_block,
        call_handlers={target: call_host_service for target in available_host_services},
        step_observer=observe_preflight,
        max_steps=1_000_000,
        record_instruction_trace=False,
        record_trace=False,
        return_on_missing_instruction=True,
        execution_yield_predicate=lambda _steps: state.eip == stop_eip,
        preserve_initial_eip=True,
    )
    if state.eip != stop_eip:
        raise Ia32BackendError(
            "decoded fixed-slice preflight reached unverified target "
            f"0x{state.eip:08X} after {result.steps} steps"
        )
    preflight = _FixedSlicePreflight(
        instructions=_instruction_map(selected_functions),
        page_addresses=tuple(sorted(page << 12 for page in traced_memory.pages)),
        memory_ranges=_merged_memory_ranges(traced_memory.ranges),
        write_ranges=_merged_memory_ranges(traced_memory.write_ranges),
        function_symbols=tuple(function.symbol for function in selected_functions),
        indirect_targets={
            address: tuple(sorted(targets)) for address, targets in sorted(indirect_targets.items())
        },
        host_service_thunks=tuple(
            invoked_host_services[target] for target in sorted(invoked_host_services)
        ),
        host_service_calls=tuple(host_service_calls),
        steps=result.steps,
    )
    return preflight, state, memory


def _fixed_slice_preflight(
    capsule: ReplayCapsule,
    stop_eip: int,
) -> _FixedSlicePreflight:
    preflight, _state, _memory = _trace_fixed_slice(capsule, stop_eip)
    return preflight


def execute_ia32_slice_reference(
    capsule: ReplayCapsule,
    *,
    stop_eip: int,
) -> Ia32SliceReferenceResult:
    """Execute the accepted decoded reference for one fixed IA-32 proof slice."""

    preflight, state, memory = _trace_fixed_slice(capsule, _u32(stop_eip))
    return Ia32SliceReferenceResult(
        state=state,
        memory=memory,
        steps=preflight.steps,
        stop_eip=_u32(stop_eip),
        host_service_thunks=preflight.host_service_thunks,
        host_service_calls=preflight.host_service_calls,
    )


def _execution_island_instruction_map(
    capsule: ReplayCapsule,
    stop_eip: int,
) -> dict[int, X86Instruction]:
    return _fixed_slice_preflight(capsule, stop_eip).instructions


def _resident_fail_closed_trap(instruction: X86Instruction) -> bool:
    """Identify observed debug traps that remain terminal in resident execution."""

    mnemonic = instruction.mnemonic.casefold()
    payload = bytes.fromhex(instruction.bytes_hex)
    return (mnemonic == "int3" and payload == b"\xcc") or (
        mnemonic == "int" and payload == b"\xcd\x2d"
    )


def _guarded_static_boundaries(
    instructions: Mapping[int, X86Instruction],
    stop_eip: int,
    verified_indirect_targets: Mapping[int, Sequence[int]] | None = None,
    host_service_targets: Iterable[int] = (),
) -> dict[int, str]:
    """Return unsupported island boundaries that must trap if reached."""

    decoded_addresses = set(instructions)
    verified_indirect_targets = verified_indirect_targets or {}
    allowed_targets = decoded_addresses | {int(target) for target in host_service_targets}
    guarded: dict[int, str] = {}
    for instruction in instructions.values():
        if instruction.address == stop_eip:
            continue
        mnemonic = instruction.mnemonic.casefold()
        reason: str | None = None
        if _resident_fail_closed_trap(instruction):
            reason = "software debug interrupt is a fail-closed terminal trap"
        elif mnemonic in REJECTED_MNEMONICS:
            reason = f"{instruction.mnemonic} requires a privileged rewrite"
        elif any(operand.segment is not None for operand in instruction.operands):
            reason = "segment-relative memory requires a TLS/segment rewrite"
        elif mnemonic in {"call_indirect", "jmp_indirect"} or (
            mnemonic in {"call", "jmp"} and instruction.target is None
        ):
            targets = verified_indirect_targets.get(instruction.address, ())
            if not targets or any(target not in allowed_targets for target in targets):
                reason = f"{instruction.mnemonic} requires verified indirect-target dispatch"
        elif mnemonic in {"call", "jmp", "jcc"} and instruction.target is not None:
            if instruction.target not in allowed_targets and instruction.target != stop_eip:
                reason = "control transfer exits the verified execution island"
        if reason is None and mnemonic not in {"jmp", "ret"}:
            if (
                instruction.next_address not in decoded_addresses
                and instruction.next_address != stop_eip
            ):
                reason = "fallthrough exits the verified execution island"
        if reason is not None:
            guarded[instruction.address] = reason
    return guarded


def _static_rewrite_gaps(
    capsule: ReplayCapsule,
    instructions: Mapping[int, X86Instruction],
    stop_eip: int,
    *,
    verified_indirect_targets: Mapping[int, Sequence[int]] | None = None,
    host_service_targets: Iterable[int] = (),
) -> list[str]:
    gaps: set[str] = set()
    decoded_addresses = set(instructions)
    verified_indirect_targets = verified_indirect_targets or {}
    allowed_targets = decoded_addresses | {int(target) for target in host_service_targets}
    if stop_eip not in decoded_addresses:
        gaps.add(f"stop address 0x{stop_eip:08X} is not decoded")
    if capsule.state.eip not in decoded_addresses:
        gaps.add(f"entry address 0x{capsule.state.eip:08X} is not decoded")
    if capsule.state.fs_base:
        gaps.add("nonzero FS base requires a TLS rewrite thunk")
    if not capsule.state.flags.interrupt_enabled:
        gaps.add("ring-3 POPFD cannot restore a cleared interrupt-enable flag")
    if capsule.state.fpu_stack:
        gaps.add("nonempty x87 input state is not encoded by the prototype thunk")
    if any(capsule.state.mmx_registers.values()):
        gaps.add("nonzero MMX input state is not encoded by the prototype thunk")

    mapped_page_addresses = {page * PAGE_SIZE for page, _payload in capsule.memory.export_pages()}
    checked_addresses = {
        *mapped_page_addresses,
        *(instruction.address for instruction in instructions.values()),
        stop_eip,
    }
    for address in checked_addresses:
        if address < MINIMUM_USER_ADDRESS:
            gaps.add(f"guest address 0x{address:08X} is below the Win32 user mapping floor")
        if _address_range_collides(
            address,
            PAGE_SIZE,
            THUNK_REGION,
            SCRATCH_ADDRESS + WINDOWS_ALLOCATION_GRANULARITY,
        ):
            gaps.add(f"guest page 0x{address & ~(PAGE_SIZE - 1):08X} collides with thunks")
        if _address_range_collides(address, PAGE_SIZE, HELPER_IMAGE_BASE, HELPER_IMAGE_LIMIT):
            gaps.add(f"guest page 0x{address & ~(PAGE_SIZE - 1):08X} collides with helper image")

    for instruction in instructions.values():
        if instruction.address == stop_eip:
            continue
        mnemonic = instruction.mnemonic.casefold()
        if mnemonic in REJECTED_MNEMONICS:
            gaps.add(
                f"0x{instruction.address:08X} {instruction.mnemonic} requires a privileged rewrite"
            )
        if mnemonic in {"call_indirect", "jmp_indirect"} or (
            mnemonic in {"call", "jmp"} and instruction.target is None
        ):
            targets = verified_indirect_targets.get(instruction.address, ())
            if not targets or any(target not in allowed_targets for target in targets):
                gaps.add(
                    f"0x{instruction.address:08X} {instruction.mnemonic} requires verified "
                    "indirect-target dispatch"
                )
        for operand in instruction.operands:
            if operand.segment is not None:
                gaps.add(
                    f"0x{instruction.address:08X} uses {operand.segment} and requires a TLS/segment "
                    "rewrite"
                )
            if operand.absolute is not None and operand.absolute >= 0x80000000:
                gaps.add(
                    f"0x{instruction.address:08X} touches high/MMIO address "
                    f"0x{operand.absolute:08X}"
                )
        if mnemonic in {"call", "jmp", "jcc"} and instruction.target is not None:
            if instruction.target not in allowed_targets and instruction.target != stop_eip:
                gaps.add(
                    f"0x{instruction.address:08X} exits to unverified target "
                    f"0x{instruction.target:08X}"
                )
        if mnemonic == "ret":
            gaps.add(
                f"0x{instruction.address:08X} has an unverified stack return; select it as the "
                "slice stop or add a return thunk"
            )
        if mnemonic not in {"jmp", "ret"}:
            next_address = instruction.next_address
            if next_address not in decoded_addresses and next_address != stop_eip:
                gaps.add(
                    f"0x{instruction.address:08X} falls through to unverified address "
                    f"0x{next_address:08X}"
                )
    return sorted(gaps)


def validate_ia32_slice(capsule: ReplayCapsule, *, stop_eip: int) -> dict[int, X86Instruction]:
    preflight = _fixed_slice_preflight(capsule, stop_eip)
    instructions = preflight.instructions
    gaps = _static_rewrite_gaps(
        capsule,
        instructions,
        _u32(stop_eip),
        verified_indirect_targets=preflight.indirect_targets,
        host_service_targets=(service.target for service in preflight.host_service_thunks),
    )
    if gaps:
        formatted = "\n".join(f"- {gap}" for gap in gaps)
        raise Ia32BackendError(f"same-ISA slice has static rewrite gaps:\n{formatted}")
    return instructions


def _contiguous_code_spans(
    instructions: Mapping[int, X86Instruction],
    *,
    guarded_addresses: Iterable[int] = (),
) -> tuple[tuple[int, bytes], ...]:
    guarded = {int(address) for address in guarded_addresses}
    code_bytes: dict[int, int] = {}
    for instruction in instructions.values():
        payload = bytearray.fromhex(instruction.bytes_hex)
        if instruction.address in guarded:
            payload[0] = 0xCC
        for offset, value in enumerate(payload):
            code_bytes[instruction.address + offset] = value
    spans: list[tuple[int, bytes]] = []
    start: int | None = None
    previous: int | None = None
    span_payload = bytearray()
    for address, value in sorted(code_bytes.items()):
        if previous is None or address != previous + 1:
            if start is not None:
                spans.append((start, bytes(span_payload)))
            start = address
            span_payload = bytearray()
        span_payload.append(value)
        previous = address
    if start is not None:
        spans.append((start, bytes(span_payload)))
    return tuple(spans)


def _replace_static_u32(
    instruction: X86Instruction,
    original: int,
    replacement: int,
    *,
    remove_fs_prefix: bool = False,
) -> X86Instruction:
    payload = bytearray.fromhex(instruction.bytes_hex)
    needle = struct.pack("<I", _u32(original))
    offset = payload.rfind(needle)
    if offset < 0:
        raise Ia32BackendError(
            f"0x{instruction.address:08X} {instruction.mnemonic} has no statically "
            "patchable absolute-address field"
        )
    payload[offset : offset + 4] = struct.pack("<I", _u32(replacement))
    if remove_fs_prefix:
        try:
            prefix = payload.index(0x64, 0, offset)
        except ValueError as exc:
            raise Ia32BackendError(
                f"0x{instruction.address:08X} names FS without an encoded FS prefix"
            ) from exc
        payload[prefix] = 0x90
    return replace(instruction, bytes_hex=payload.hex().upper())


def _phase3_mmio_shadow_pages(
    instructions: Mapping[int, X86Instruction],
    profile: Ia32ArchitectureProfile,
) -> dict[int, int]:
    declared = {
        _u32(int(logical)) & ~(PAGE_SIZE - 1): _u32(int(mapped)) & ~(PAGE_SIZE - 1)
        for logical, mapped in (profile.mmio_shadow_pages or {}).items()
    }
    candidates = sorted(
        {
            int(operand.absolute) & ~(PAGE_SIZE - 1)
            for instruction in instructions.values()
            for operand in instruction.operands
            if operand.absolute is not None
            and (
                int(operand.absolute) & ~(PAGE_SIZE - 1) == PHASE7_PHYSICAL_ZERO_PAGE
                or int(operand.absolute) >= PHYSICAL_ALIAS_END
            )
        }
    )
    next_shadow = PHASE3_MMIO_SHADOW_BASE
    used = set(declared.values())
    for logical in candidates:
        if logical in declared:
            continue
        if logical == PHASE7_PHYSICAL_ZERO_PAGE:
            declared[logical] = logical
            used.add(logical)
            continue
        while next_shadow in used:
            next_shadow += PAGE_SIZE
        declared[logical] = next_shadow
        used.add(next_shadow)
        next_shadow += PAGE_SIZE
    return declared


def _phase3_mapped_address(address: int, mmio_pages: Mapping[int, int]) -> int:
    address = _u32(address)
    page = address & ~(PAGE_SIZE - 1)
    if PHYSICAL_ALIAS_START <= address < PHYSICAL_ALIAS_END:
        return address & 0x7FFFFFFF
    shadow = mmio_pages.get(page)
    if shadow is not None:
        return _u32(shadow + (address & (PAGE_SIZE - 1)))
    return address


def _phase3_rewrite_island(
    instruction: X86Instruction,
    *,
    timestamp_counter: int,
    occupied: set[int],
    stop_eip: int,
) -> tuple[X86Instruction, tuple[int, bytes]]:
    if instruction.size != 2 or bytes.fromhex(instruction.bytes_hex) != b"\x0f\x31":
        raise Ia32BackendError(
            f"0x{instruction.address:08X} rdtsc has no supported two-byte rewrite form"
        )
    for target in range(instruction.next_address + 5, instruction.address + 129):
        island_range = set(range(target, target + 15))
        stop_range = set(range(stop_eip, stop_eip + 5))
        if island_range & occupied or island_range & stop_range:
            continue
        short_delta = target - instruction.next_address
        if not -128 <= short_delta <= 127:
            continue
        return_address = instruction.next_address
        tail = target + 15
        return_delta = _u32(return_address - tail)
        value = timestamp_counter & 0xFFFFFFFFFFFFFFFF
        island = (
            b"\xb8"
            + struct.pack("<I", value & 0xFFFFFFFF)
            + b"\xba"
            + struct.pack("<I", value >> 32)
            + b"\xe9"
            + struct.pack("<I", return_delta)
        )
        rewritten = replace(
            instruction,
            bytes_hex=(b"\xeb" + bytes((short_delta & 0xFF,))).hex().upper(),
        )
        return rewritten, (target, island)
    raise Ia32BackendError(
        f"0x{instruction.address:08X} rdtsc has no 15-byte verified rewrite island "
        "within short-jump range"
    )


def _relative_i32(target: int, next_address: int) -> bytes:
    delta = _u32(target - next_address)
    signed = delta - 0x1_0000_0000 if delta & 0x80000000 else delta
    return struct.pack("<i", signed)


def _phase3_shared_timestamp_thunk(timestamp_counter: int) -> tuple[bytes, bytes]:
    """Return a flag-preserving resident RDTSC thunk and its persistent counter."""

    counter = ARCHITECTURE_TIMESTAMP_COUNTER_ADDRESS
    code = bytearray(b"\x9c")  # pushfd -- RDTSC does not change arithmetic flags.
    code += b"\xa1" + struct.pack("<I", counter)
    code += b"\x8b\x15" + struct.pack("<I", counter + 4)
    code += b"\x81\x05" + struct.pack("<I", counter) + struct.pack("<I", DETERMINISTIC_TSC_STEP)
    code += b"\x83\x15" + struct.pack("<I", counter + 4) + b"\x00"
    code += b"\x9d\xc3"  # popfd; ret
    return bytes(code), struct.pack("<Q", timestamp_counter & 0xFFFFFFFFFFFFFFFF)


def _phase3_shared_timestamp_rewrite(
    instruction: X86Instruction,
    *,
    occupied: set[int],
    stop_eip: int,
) -> tuple[X86Instruction, tuple[int, bytes]]:
    """Route a two-byte RDTSC through a shared deterministic resident counter."""

    if instruction.size != 2 or bytes.fromhex(instruction.bytes_hex) != b"\x0f\x31":
        raise Ia32BackendError(
            f"0x{instruction.address:08X} rdtsc has no supported two-byte rewrite form"
        )
    local_size = 10
    for target in range(instruction.next_address + 5, instruction.address + 129):
        island_range = set(range(target, target + local_size))
        stop_range = set(range(stop_eip, stop_eip + 5))
        if island_range & occupied or island_range & stop_range:
            continue
        short_delta = target - instruction.next_address
        if not -128 <= short_delta <= 127:
            continue
        island = (
            b"\xe8"
            + _relative_i32(ARCHITECTURE_RDTSC_THUNK_ADDRESS, target + 5)
            + b"\xe9"
            + _relative_i32(instruction.next_address, target + local_size)
        )
        rewritten = replace(
            instruction,
            bytes_hex=(b"\xeb" + bytes((short_delta & 0xFF,))).hex().upper(),
        )
        return rewritten, (target, island)
    raise Ia32BackendError(
        f"0x{instruction.address:08X} rdtsc has no 10-byte shared-counter rewrite "
        "island within short-jump range"
    )


def _phase3_dense_timestamp_rewrite(
    instructions: Mapping[int, X86Instruction],
    instruction: X86Instruction,
) -> tuple[dict[int, X86Instruction], tuple[int, bytes]]:
    """Route the observed compact timer helper through the shared RDTSC thunk."""

    prefix = instructions.get(PHASE7_DENSE_RDTSC_PREFIX_SITE)
    if (
        instruction.address != PHASE7_DENSE_RDTSC_SITE
        or instruction.size != 2
        or bytes.fromhex(instruction.bytes_hex) != b"\x0f\x31"
        or prefix is None
        or prefix.next_address != instruction.address
        or prefix.size != 4
        or bytes.fromhex(prefix.bytes_hex) != b"\x8b\x4c\x24\x04"
    ):
        raise Ia32BackendError(
            f"0x{instruction.address:08X} rdtsc has no verified dense helper rewrite"
        )
    call = b"\xe8" + _relative_i32(
        ARCHITECTURE_DENSE_RDTSC_THUNK_ADDRESS,
        PHASE7_DENSE_RDTSC_PREFIX_SITE + 5,
    )
    # CALL adds its own return address. The original helper argument therefore
    # moves from [esp+4] to [esp+8] while the thunk obtains the shared EDX:EAX
    # timestamp. The original stores at 0xE24C2 and 0xE24C4 remain unchanged.
    thunk = (
        b"\x8b\x4c\x24\x08"
        + b"\xe8"
        + _relative_i32(
            ARCHITECTURE_RDTSC_THUNK_ADDRESS,
            ARCHITECTURE_DENSE_RDTSC_THUNK_ADDRESS + 9,
        )
        + b"\xc3"
    )
    return (
        {
            prefix.address: replace(prefix, bytes_hex=call[:4].hex().upper()),
            instruction.address: replace(
                instruction,
                bytes_hex=(call[4:] + b"\x90").hex().upper(),
            ),
        },
        (ARCHITECTURE_DENSE_RDTSC_THUNK_ADDRESS, thunk),
    )


def _resident_sgdt_rewrite(
    instruction: X86Instruction,
    *,
    gdtr_base: int,
    gdtr_limit: int,
) -> tuple[X86Instruction, tuple[int, bytes]]:
    """Replace the observed stack SGDT with a fixed native thunk call."""

    if (
        instruction.mnemonic.casefold() != "sgdt"
        or instruction.size != 5
        or bytes.fromhex(instruction.bytes_hex) != b"\x0f\x01\x44\x24\x06"
    ):
        raise Ia32BackendError(
            f"0x{instruction.address:08X} {instruction.text()} has no supported "
            "resident SGDT rewrite form"
        )
    rewritten = replace(
        instruction,
        bytes_hex=(
            b"\xe8"
            + _relative_i32(
                ARCHITECTURE_SGDT_THUNK_ADDRESS,
                instruction.next_address,
            )
        )
        .hex()
        .upper(),
    )
    # CALL pushes its return address, so the original [esp+6] destination is
    # [esp+10] inside the thunk.
    thunk = (
        b"\x66\xc7\x44\x24\x0a"
        + struct.pack("<H", gdtr_limit & 0xFFFF)
        + b"\xc7\x44\x24\x0c"
        + struct.pack("<I", _u32(gdtr_base))
        + b"\xc3"
    )
    return rewritten, (ARCHITECTURE_SGDT_THUNK_ADDRESS, thunk)


def _resident_zero_port_read_rewrite(
    instructions: Mapping[int, X86Instruction],
    instruction: X86Instruction,
) -> dict[int, X86Instruction]:
    """Implement the observed port read as flag-preserving AL=0 semantics."""

    prefix = instructions.get(PHASE7_ZERO_PORT_READ_PREFIX_SITE)
    if (
        instruction.address != PHASE7_ZERO_PORT_READ_SITE
        or instruction.mnemonic.casefold() != "in"
        or instruction.size != 1
        or bytes.fromhex(instruction.bytes_hex) != b"\xec"
        or prefix is None
        or prefix.next_address != instruction.address
        or prefix.size != 4
        or bytes.fromhex(prefix.bytes_hex) != b"\x66\xba\xc0\x80"
    ):
        raise Ia32BackendError(
            f"0x{instruction.address:08X} {instruction.text()} has no supported "
            "resident port-read rewrite form"
        )
    # The decoded oracle discards port 0x80C0 and returns zero in AL. The next
    # instruction replaces EDX, so the five-byte pair can preserve flags and
    # clear AL without exposing a ring-3 IN instruction.
    return {
        prefix.address: replace(prefix, bytes_hex="9C24009D"),  # pushfd; and al,0; popfd
        instruction.address: replace(instruction, bytes_hex="90"),
    }


def _phase3_inline_timestamp_wrapper(
    instructions: Mapping[int, X86Instruction],
    instruction: X86Instruction,
    *,
    timestamp_counter: int,
) -> dict[int, X86Instruction]:
    """Rewrite the supported dense RDTSC wrapper without an external island."""

    expected = bytes.fromhex(
        "0F31"  # rdtsc
        "8D5C2404"  # lea ebx,[esp+4]
        "8903"  # mov [ebx],eax
        "895304"  # mov [ebx+4],edx
        "8B442404"  # mov eax,[esp+4]
        "5B"  # pop ebx
        "83C408"  # add esp,8
        "C3"  # ret
    )
    addresses = []
    payload = bytearray()
    address = instruction.address
    while len(payload) < len(expected):
        current = instructions.get(address)
        if current is None:
            break
        addresses.append(address)
        payload.extend(bytes.fromhex(current.bytes_hex))
        address = current.next_address
    if bytes(payload) != expected:
        raise Ia32BackendError(
            f"0x{instruction.address:08X} rdtsc has no verified inline wrapper rewrite"
        )
    value = timestamp_counter & 0xFFFFFFFFFFFFFFFF
    replacement = bytearray(
        b"\xb8"
        + struct.pack("<I", value & 0xFFFFFFFF)
        + b"\xba"
        + struct.pack("<I", value >> 32)
        + b"\x5b\x83\xc4\x08\xc3"
    )
    replacement.extend(b"\x90" * (len(expected) - len(replacement)))
    rewritten = {}
    offset = 0
    for current_address in addresses:
        current = instructions[current_address]
        rewritten[current_address] = replace(
            current,
            bytes_hex=replacement[offset : offset + current.size].hex().upper(),
        )
        offset += current.size
    return rewritten


def _resident_privileged_noop_rewrite(
    instruction: X86Instruction,
) -> X86Instruction:
    """Replace accepted no-effect hardware operations with equal-size NOPs."""

    payload = bytes.fromhex(instruction.bytes_hex)
    mnemonic = instruction.mnemonic.casefold()
    interrupt_mask = PHASE7_RESIDENT_INTERRUPT_MASK_SITES.get(instruction.address)
    supported = (
        (interrupt_mask == mnemonic and payload == (b"\xfa" if mnemonic == "cli" else b"\xfb"))
        or (mnemonic == "wbinvd" and payload == b"\x0f\x09")
        or (mnemonic == "out" and payload and payload[0] in {0xE6, 0xE7, 0xEE, 0xEF})
    )
    if not supported or len(payload) != instruction.size:
        raise Ia32BackendError(
            f"0x{instruction.address:08X} {instruction.mnemonic} has no supported "
            "resident no-op rewrite form"
        )
    return replace(instruction, bytes_hex=(b"\x90" * instruction.size).hex().upper())


def _resident_self_clearing_mmio_rewrite(
    instruction: X86Instruction,
) -> X86Instruction:
    """Discard the observed GPU command write whose status bit clears immediately."""

    operands = instruction.operands
    supported = bool(
        instruction.address in PHASE7_SELF_CLEARING_MMIO_WRITE_SITES
        and instruction.mnemonic.casefold() == "mov"
        and instruction.size == 6
        and instruction.bytes_hex.upper() == "898810041000"
        and len(operands) == 2
        and operands[0].kind == "mem"
        and operands[0].size == 32
        and operands[0].base == "eax"
        and operands[0].index is None
        and operands[0].scale == 1
        and operands[0].displacement == 0x00100410
        and operands[0].absolute is None
        and operands[0].segment is None
        and operands[1].kind == "reg"
        and operands[1].size == 32
        and operands[1].reg == "ecx"
    )
    if not supported:
        raise Ia32BackendError(
            f"0x{instruction.address:08X} {instruction.text()} does not match the "
            "observed self-clearing MMIO write"
        )
    return replace(instruction, bytes_hex=(b"\x90" * instruction.size).hex().upper())


def _resident_gpu_completion_shadow_read_rewrite(
    instruction: X86Instruction,
) -> X86Instruction:
    """Read the submitted GPU context where hardware would publish completion."""

    operands = instruction.operands
    supported = bool(
        instruction.address in PHASE7_GPU_COMPLETION_SHADOW_READ_SITES
        and instruction.mnemonic.casefold() == "mov"
        and instruction.size == 3
        and instruction.bytes_hex.upper() == "8B4944"
        and len(operands) == 2
        and operands[0].kind == "reg"
        and operands[0].size == 32
        and operands[0].reg == "ecx"
        and operands[1].kind == "mem"
        and operands[1].size == 32
        and operands[1].base == "ecx"
        and operands[1].index is None
        and operands[1].scale == 1
        and operands[1].displacement == PHASE7_GPU_COMPLETED_CONTEXT_OFFSET
        and operands[1].absolute is None
        and operands[1].segment is None
    )
    if not supported:
        raise Ia32BackendError(
            f"0x{instruction.address:08X} {instruction.text()} does not match the "
            "observed GPU completion shadow read"
        )
    return replace(instruction, bytes_hex="8B4940")


def _resident_mcpx_frame_counter_read_rewrite(
    instruction: X86Instruction,
    *,
    mapped_address: int,
) -> tuple[X86Instruction, tuple[int, bytes], str]:
    """Advance the finite MCPX counter shadow and return its new value."""

    register_codes = {
        "eax": 0,
        "ecx": 1,
        "edx": 2,
        "ebx": 3,
        "ebp": 5,
        "esi": 6,
        "edi": 7,
    }
    operands = instruction.operands
    register = (
        operands[0].reg
        if len(operands) == 2 and operands[0].kind == "reg" and operands[0].size == 32
        else None
    )
    source = operands[1] if len(operands) == 2 else None
    register_code = register_codes.get(register or "")
    expected = (
        b"\xa1" + struct.pack("<I", PHASE7_MCPX_FRAME_COUNTER_ADDRESS)
        if register == "eax"
        else (
            b"\x8b"
            + bytes((0x05 | (int(register_code) << 3),))
            + struct.pack("<I", PHASE7_MCPX_FRAME_COUNTER_ADDRESS)
            if register_code is not None
            else b""
        )
    )
    supported = bool(
        instruction.mnemonic.casefold() == "mov"
        and register_code is not None
        and source is not None
        and source.kind == "mem"
        and source.size == 32
        and source.absolute == PHASE7_MCPX_FRAME_COUNTER_ADDRESS
        and source.base is None
        and source.index is None
        and source.scale == 1
        and source.displacement == 0
        and source.segment is None
        and bytes.fromhex(instruction.bytes_hex) == expected
        and instruction.size == len(expected)
    )
    if not supported:
        raise Ia32BackendError(
            f"0x{instruction.address:08X} {instruction.text()} does not match the "
            "observed MCPX frame-counter read family"
        )
    assert register is not None and register_code is not None
    thunk_address = (
        ARCHITECTURE_MCPX_FRAME_COUNTER_THUNK_BASE
        + register_code * ARCHITECTURE_MCPX_FRAME_COUNTER_THUNK_STRIDE
    )
    replacement = bytearray(b"\xe8")
    replacement += _relative_i32(thunk_address, instruction.address + 5)
    replacement += b"\x90" * (instruction.size - len(replacement))
    thunk = bytearray(b"\x9c")  # Preserve flags across the synthetic read side effect.
    thunk += b"\x83\x05" + struct.pack("<I", mapped_address)
    thunk += bytes((PHASE7_MCPX_FRAME_COUNTER_INCREMENT,))
    thunk += b"\x8b" + bytes((0x05 | (register_code << 3),))
    thunk += struct.pack("<I", mapped_address)
    thunk += b"\x9d\xc3"
    if len(thunk) > ARCHITECTURE_MCPX_FRAME_COUNTER_THUNK_STRIDE:
        raise Ia32BackendError("MCPX frame-counter thunk exceeded its reserved slot")
    return (
        replace(instruction, bytes_hex=replacement.hex().upper()),
        (thunk_address, bytes(thunk)),
        str(register),
    )


def _resident_audio_dsp_control_write_rewrite(
    instruction: X86Instruction,
    *,
    mapped_control_address: int,
    mapped_status_address: int,
) -> tuple[X86Instruction, tuple[int, bytes]]:
    """Publish the finite Xbox DSP reset-ready handshake in the MMIO shadow."""

    operands = instruction.operands
    destination = operands[0] if len(operands) == 2 else None
    source = operands[1] if len(operands) == 2 else None
    expected = b"\xa3" + struct.pack("<I", PHASE7_AUDIO_DSP_CONTROL_ADDRESS)
    supported = bool(
        instruction.address in PHASE7_AUDIO_DSP_CONTROL_WRITE_SITES
        and instruction.mnemonic.casefold() == "mov"
        and destination is not None
        and destination.kind == "mem"
        and destination.size == 32
        and destination.absolute == PHASE7_AUDIO_DSP_CONTROL_ADDRESS
        and destination.base is None
        and destination.index is None
        and destination.scale == 1
        and destination.displacement == 0
        and destination.segment is None
        and source is not None
        and source.kind == "reg"
        and source.size == 32
        and source.reg == "eax"
        and bytes.fromhex(instruction.bytes_hex) == expected
        and instruction.size == len(expected)
    )
    if not supported:
        raise Ia32BackendError(
            f"0x{instruction.address:08X} {instruction.text()} does not match the "
            "observed audio DSP control-write family"
        )

    replacement = b"\xe8" + _relative_i32(
        ARCHITECTURE_AUDIO_DSP_READY_THUNK_ADDRESS,
        instruction.next_address,
    )
    thunk = bytearray(b"\x9c")  # A raw MMIO write does not change guest flags.
    thunk += b"\xa3" + struct.pack("<I", mapped_control_address)
    thunk += b"\xa9" + struct.pack("<I", PHASE7_AUDIO_DSP_RESET_REQUEST)
    thunk += b"\x74\x0a"
    thunk += b"\x81\x0d" + struct.pack("<I", mapped_status_address)
    thunk += struct.pack("<I", PHASE7_AUDIO_DSP_RESET_READY)
    thunk += b"\x9d\xc3"
    return (
        replace(instruction, bytes_hex=replacement.hex().upper()),
        (ARCHITECTURE_AUDIO_DSP_READY_THUNK_ADDRESS, bytes(thunk)),
    )


def _resident_audio_voice_command_write_rewrite(
    instruction: X86Instruction,
) -> X86Instruction:
    """Complete the finite self-clearing MCPX voice-command writes in place."""

    operands = instruction.operands
    destination = operands[0] if len(operands) == 2 else None
    source = operands[1] if len(operands) == 2 else None
    expected_base = PHASE7_AUDIO_VOICE_COMMAND_WRITE_BASES.get(instruction.address)
    expected_bytes = PHASE7_AUDIO_DSP_MMIO_FAMILY.get(instruction.address)
    supported = bool(
        expected_base is not None
        and expected_bytes is not None
        and instruction.mnemonic.casefold() == "mov"
        and destination is not None
        and destination.kind == "mem"
        and destination.size == 8
        and destination.base == expected_base
        and destination.index is None
        and destination.scale == 1
        and destination.displacement == PHASE7_AUDIO_VOICE_COMMAND_DISPLACEMENT
        and destination.absolute is None
        and destination.segment is None
        and source is not None
        and source.kind == "imm"
        and source.immediate == PHASE7_AUDIO_VOICE_COMMAND_PENDING
        and instruction.bytes_hex.upper() == expected_bytes
    )
    if not supported:
        raise Ia32BackendError(
            f"0x{instruction.address:08X} {instruction.text()} does not match the "
            "finite audio voice-command self-clear family"
        )

    replacement = bytearray(bytes.fromhex(instruction.bytes_hex))
    replacement[-1] &= ~PHASE7_AUDIO_VOICE_COMMAND_PENDING
    return replace(instruction, bytes_hex=replacement.hex().upper())


def _resident_audio_buffer_command_rewrite(
    instruction: X86Instruction,
    instructions: Mapping[int, X86Instruction],
) -> X86Instruction:
    """Complete the finite audio-buffer command mailbox before its two polls."""

    for address, expected in PHASE7_AUDIO_BUFFER_COMMAND_FAMILY.items():
        family_instruction = instructions.get(address)
        if family_instruction is None or family_instruction.bytes_hex.upper() != expected:
            raise Ia32BackendError(
                f"finite audio buffer-command self-clear family drifted: 0x{address:08X}"
            )
    for address, expected in PHASE7_AUDIO_BUFFER_COMMAND_CALL_SITES.items():
        caller = instructions.get(address)
        if (
            caller is None
            or caller.bytes_hex.upper() != expected
            or caller.mnemonic.casefold() != "call"
            or caller.target != 0x002309EC
        ):
            raise Ia32BackendError(
                f"finite audio buffer-command caller family drifted: 0x{address:08X}"
            )
    observed_callers = {
        address
        for address, candidate in instructions.items()
        if candidate.mnemonic.casefold() == "call" and candidate.target == 0x002309EC
    }
    if observed_callers != set(PHASE7_AUDIO_BUFFER_COMMAND_CALL_SITES):
        raise Ia32BackendError("finite audio buffer-command caller census drifted")

    operands = instruction.operands
    supported = False
    if instruction.address == PHASE7_AUDIO_BUFFER_COMMAND_IMMEDIATE_SITE:
        supported = bool(
            instruction.mnemonic.casefold() == "push"
            and len(operands) == 1
            and operands[0].kind == "imm"
            and operands[0].immediate == PHASE7_AUDIO_BUFFER_COMMAND_VALUES[instruction.address]
        )
    elif instruction.address == 0x00230A3A:
        destination = operands[0] if len(operands) == 2 else None
        source = operands[1] if len(operands) == 2 else None
        supported = bool(
            instruction.mnemonic.casefold() == "mov"
            and destination is not None
            and destination.kind == "mem"
            and destination.size == 32
            and destination.base == "eax"
            and destination.index is None
            and destination.scale == 1
            and destination.displacement == 0x10
            and destination.absolute is None
            and destination.segment is None
            and source is not None
            and source.kind == "imm"
            and source.size == 32
            and source.immediate == PHASE7_AUDIO_BUFFER_COMMAND_VALUES[instruction.address]
        )
    supported = bool(
        supported
        and instruction.address in PHASE7_AUDIO_BUFFER_COMMAND_VALUES
        and instruction.bytes_hex.upper()
        == PHASE7_AUDIO_BUFFER_COMMAND_FAMILY[instruction.address]
    )
    if not supported:
        raise Ia32BackendError(
            f"0x{instruction.address:08X} {instruction.text()} does not match the "
            "finite audio buffer-command self-clear family"
        )

    replacement = bytearray(bytes.fromhex(instruction.bytes_hex))
    if instruction.address == PHASE7_AUDIO_BUFFER_COMMAND_IMMEDIATE_SITE:
        replacement[-1] = 0
    else:
        replacement[-4:] = bytes(4)
    return replace(instruction, bytes_hex=replacement.hex().upper())


def _validate_phase7_audio_dsp_mmio_instruction(
    instruction: X86Instruction,
) -> None:
    expected = PHASE7_AUDIO_DSP_MMIO_FAMILY.get(instruction.address)
    if expected is None or instruction.bytes_hex.upper() != expected:
        raise Ia32BackendError(
            f"0x{instruction.address:08X} {instruction.text()} does not match the "
            "finite normal-live audio DSP MMIO family"
        )


def _validate_phase7_audio_dsp_mmio_family(
    instructions: Mapping[int, X86Instruction],
) -> None:
    missing = sorted(set(PHASE7_AUDIO_DSP_MMIO_FAMILY) - set(instructions))
    if missing:
        raise Ia32BackendError(
            "normal-live audio DSP MMIO family is incomplete: "
            + ", ".join(f"0x{address:08X}" for address in missing)
        )
    for address in sorted(PHASE7_AUDIO_DSP_MMIO_FAMILY):
        _validate_phase7_audio_dsp_mmio_instruction(instructions[address])


def _validate_phase7_vblank_callback_family(
    instructions: Mapping[int, X86Instruction],
) -> None:
    """Validate the one finite D3D vblank installer/setter/callback family."""

    missing = sorted(set(PHASE7_VBLANK_CALLBACK_INSTRUCTION_BYTES) - set(instructions))
    if missing:
        raise Ia32BackendError(
            "normal-live vblank callback family is incomplete: "
            + ", ".join(f"0x{address:08X}" for address in missing)
        )
    for address, expected in PHASE7_VBLANK_CALLBACK_INSTRUCTION_BYTES.items():
        if instructions[address].bytes_hex.upper() != expected:
            raise Ia32BackendError(f"normal-live vblank callback family drifted at 0x{address:08X}")

    register_callers = {
        address
        for address, instruction in instructions.items()
        if instruction.mnemonic.casefold() == "call"
        and instruction.target == NORMAL_LIVE_VBLANK_REGISTER_ADDRESS
    }
    if register_callers != {0x000B8D5A}:
        raise Ia32BackendError(
            "normal-live vblank register-caller family drifted: expected 0x000B8D5A"
        )
    callback_installers = {
        address
        for address, instruction in instructions.items()
        if any(
            operand.kind == "imm" and operand.immediate == NORMAL_LIVE_VBLANK_CALLBACK_ADDRESS
            for operand in instruction.operands
        )
    }
    if callback_installers != {0x000B8D3D}:
        raise Ia32BackendError(
            "normal-live vblank callback-installer family drifted: expected 0x000B8D3D"
        )
    direct_callback_callers = {
        address
        for address, instruction in instructions.items()
        if instruction.mnemonic.casefold() == "call"
        and instruction.target == NORMAL_LIVE_VBLANK_CALLBACK_ADDRESS
    }
    if direct_callback_callers:
        raise Ia32BackendError("normal-live vblank callback gained an unexpected direct caller")


def _validate_phase7_audio_buffer_mmio_family(
    instructions: Mapping[int, X86Instruction],
) -> None:
    missing = sorted(set(PHASE7_AUDIO_BUFFER_MMIO_FAMILY) - set(instructions))
    if missing:
        raise Ia32BackendError(
            "normal-live audio buffer MMIO family is incomplete: "
            + ", ".join(f"0x{address:08X}" for address in missing)
        )
    for address, expected in PHASE7_AUDIO_BUFFER_MMIO_FAMILY.items():
        instruction = instructions[address]
        if instruction.bytes_hex.upper() != expected:
            raise Ia32BackendError(
                f"0x{address:08X} {instruction.text()} does not match the "
                "finite normal-live audio buffer MMIO family"
            )

    direct_callers = {
        instruction.address
        for instruction in instructions.values()
        if instruction.mnemonic.casefold() == "call"
        and instruction.target == PHASE7_AUDIO_BUFFER_COPY_TARGET
    }
    if direct_callers != PHASE7_AUDIO_BUFFER_COPY_CALL_SITES:
        raise Ia32BackendError(
            "normal-live audio buffer copy-caller family drifted: expected "
            + ", ".join(
                f"0x{address:08X}" for address in sorted(PHASE7_AUDIO_BUFFER_COPY_CALL_SITES)
            )
            + "; observed "
            + (", ".join(f"0x{address:08X}" for address in sorted(direct_callers)) or "none")
        )

    reference_sites = {
        instruction.address
        for instruction in instructions.values()
        if any(
            value is not None
            and PHASE7_AUDIO_BUFFER_MMIO_START <= _u32(int(value)) < PHASE7_AUDIO_BUFFER_MMIO_END
            for operand in instruction.operands
            for value in (
                operand.absolute,
                operand.immediate,
                (
                    operand.displacement
                    if operand.kind == "mem"
                    and operand.absolute is None
                    and (operand.base is not None or operand.index is not None)
                    else None
                ),
            )
        )
    }
    if reference_sites != PHASE7_AUDIO_BUFFER_MMIO_REFERENCE_SITES:
        raise Ia32BackendError(
            "normal-live FE83xxxx audio buffer reference family drifted: expected "
            + ", ".join(
                f"0x{address:08X}" for address in sorted(PHASE7_AUDIO_BUFFER_MMIO_REFERENCE_SITES)
            )
            + "; observed "
            + (", ".join(f"0x{address:08X}" for address in sorted(reference_sites)) or "none")
        )


def _validate_phase7_audio_sg_mmio_family(
    instructions: Mapping[int, X86Instruction],
) -> None:
    missing = sorted(set(PHASE7_AUDIO_SG_MMIO_FAMILY) - set(instructions))
    if missing:
        raise Ia32BackendError(
            "normal-live audio SG MMIO family is incomplete: "
            + ", ".join(f"0x{address:08X}" for address in missing)
        )
    for address, expected in PHASE7_AUDIO_SG_MMIO_FAMILY.items():
        instruction = instructions[address]
        if instruction.bytes_hex.upper() != expected:
            raise Ia32BackendError(
                f"0x{address:08X} {instruction.text()} does not match the "
                "finite normal-live audio SG MMIO family"
            )


def _validate_phase7_mcpx_indexed_mmio_family(
    instructions: Mapping[int, X86Instruction],
) -> None:
    missing = sorted(set(PHASE7_MCPX_INDEXED_MMIO_FAMILY) - set(instructions))
    if missing:
        raise Ia32BackendError(
            "normal-live MCPX indexed MMIO family is incomplete: "
            + ", ".join(f"0x{address:08X}" for address in missing)
        )
    for address, expected in PHASE7_MCPX_INDEXED_MMIO_FAMILY.items():
        instruction = instructions[address]
        if instruction.bytes_hex.upper() != expected:
            raise Ia32BackendError(
                f"0x{address:08X} {instruction.text()} does not match the "
                "finite normal-live MCPX indexed MMIO family"
            )

    direct_callers = {
        instruction.address
        for instruction in instructions.values()
        if instruction.mnemonic.casefold() == "call"
        and instruction.target == PHASE7_MCPX_SLOT_WRITE_TARGET
    }
    if direct_callers != PHASE7_MCPX_SLOT_WRITE_CALL_SITES:
        unexpected = sorted(direct_callers - PHASE7_MCPX_SLOT_WRITE_CALL_SITES)
        missing_callers = sorted(PHASE7_MCPX_SLOT_WRITE_CALL_SITES - direct_callers)
        details = []
        if unexpected:
            details.append("unexpected " + ", ".join(f"0x{address:08X}" for address in unexpected))
        if missing_callers:
            details.append(
                "missing " + ", ".join(f"0x{address:08X}" for address in missing_callers)
            )
        raise Ia32BackendError(
            "normal-live MCPX slot-writer caller family drifted: " + "; ".join(details)
        )

    indexed_sites = {
        instruction.address
        for instruction in instructions.values()
        for operand in instruction.operands
        if operand.kind == "mem"
        and operand.absolute is None
        and (operand.base is not None or operand.index is not None)
        and (_u32(operand.displacement) & ~(PAGE_SIZE - 1)) == PHASE7_MCPX_MMIO_PAGE
    }
    if indexed_sites != PHASE7_MCPX_INDEXED_MMIO_ACCESS_SITES:
        unexpected = sorted(indexed_sites - PHASE7_MCPX_INDEXED_MMIO_ACCESS_SITES)
        missing_sites = sorted(PHASE7_MCPX_INDEXED_MMIO_ACCESS_SITES - indexed_sites)
        details = []
        if unexpected:
            details.append("unexpected " + ", ".join(f"0x{address:08X}" for address in unexpected))
        if missing_sites:
            details.append("missing " + ", ".join(f"0x{address:08X}" for address in missing_sites))
        raise Ia32BackendError(
            "normal-live indexed FE820xxx access family drifted: " + "; ".join(details)
        )


def _resident_pfifo_write_one_to_clear_rewrite(
    instruction: X86Instruction,
) -> X86Instruction:
    """Record the acknowledged PFIFO status bit as cleared in the RAM shadow."""

    operands = instruction.operands
    supported = bool(
        instruction.address == PHASE7_PFIFO_WRITE_ONE_TO_CLEAR_SITE
        and instruction.mnemonic.casefold() == "mov"
        and instruction.size == 10
        and instruction.bytes_hex.upper() == "C7860001400000100000"
        and len(operands) == 2
        and operands[0].kind == "mem"
        and operands[0].size == 32
        and operands[0].base == "esi"
        and operands[0].index is None
        and operands[0].scale == 1
        and operands[0].displacement == PHASE7_PFIFO_STATUS_OFFSET
        and operands[0].absolute is None
        and operands[0].segment is None
        and operands[1].kind == "imm"
        and operands[1].size == 32
        and operands[1].immediate == PHASE7_PFIFO_STATUS_CLEAR_MASK
    )
    if not supported:
        raise Ia32BackendError(
            f"0x{instruction.address:08X} {instruction.text()} does not match the "
            "observed PFIFO write-one-to-clear status write"
        )
    replacement = bytearray(bytes.fromhex(instruction.bytes_hex))
    replacement[-4:] = bytes(4)
    return replace(instruction, bytes_hex=replacement.hex().upper())


def _phase3_architecture_plan(
    capsule: ReplayCapsule,
    preflight: _FixedSlicePreflight,
    *,
    stop_eip: int,
    profile: Ia32ArchitectureProfile,
    resident_privileged_boundary: bool = False,
    occupied_instruction_bytes: Iterable[int] = (),
) -> _Ia32ArchitecturePlan:
    if capsule.state.fpu_stack and any(capsule.state.mmx_registers.values()):
        raise Ia32BackendError(
            "Phase-3 input cannot claim independent x87 and MMX values in their aliased "
            "physical register file"
        )
    instruction_bytes = {
        address
        for instruction in preflight.instructions.values()
        for address in range(instruction.address, instruction.next_address)
    }
    for address, size in preflight.write_ranges:
        overlap = next(
            (current for current in range(address, address + size) if current in instruction_bytes),
            None,
        )
        if overlap is not None:
            raise Ia32BackendError(
                f"0x{overlap:08X} is written by the guest and is unsupported "
                "self-modifying executable memory"
            )

    rewritten = dict(preflight.instructions)
    rewrites: list[dict[str, Any]] = []
    extra_code_spans: list[tuple[int, bytes]] = []
    rewritten_logical_pages: set[int] = set()
    mmio_pages = _phase3_mmio_shadow_pages(preflight.instructions, profile)
    occupied = set(instruction_bytes) | {int(address) for address in occupied_instruction_bytes}
    timestamp_step_count = 0
    mcpx_frame_counter_thunks: set[int] = set()
    audio_dsp_ready_thunk_added = False
    for address in sorted(preflight.instructions):
        instruction = rewritten[address]
        mnemonic = instruction.mnemonic.casefold()
        if address in PHASE7_AUDIO_DSP_CONTROL_WRITE_SITES and resident_privileged_boundary:
            mapped_control = _phase3_mapped_address(
                PHASE7_AUDIO_DSP_CONTROL_ADDRESS,
                mmio_pages,
            )
            mapped_status = _phase3_mapped_address(
                PHASE7_AUDIO_DSP_STATUS_ADDRESS,
                mmio_pages,
            )
            rewritten_instruction, thunk = _resident_audio_dsp_control_write_rewrite(
                instruction,
                mapped_control_address=mapped_control,
                mapped_status_address=mapped_status,
            )
            rewritten[address] = rewritten_instruction
            if not audio_dsp_ready_thunk_added:
                extra_code_spans.append(thunk)
                audio_dsp_ready_thunk_added = True
            rewritten_logical_pages.add(PHASE7_AUDIO_DSP_CONTROL_ADDRESS & ~(PAGE_SIZE - 1))
            rewrites.append(
                {
                    "address": address,
                    "address_hex": f"0x{address:08X}",
                    "kind": "mmio-audio-dsp-reset-handshake",
                    "status": "implemented-resident-reset-ready-shadow-thunk",
                    "logical_control_address": PHASE7_AUDIO_DSP_CONTROL_ADDRESS,
                    "mapped_control_address": mapped_control,
                    "logical_status_address": PHASE7_AUDIO_DSP_STATUS_ADDRESS,
                    "mapped_status_address": mapped_status,
                    "reset_request_mask": PHASE7_AUDIO_DSP_RESET_REQUEST,
                    "reset_ready_mask": PHASE7_AUDIO_DSP_RESET_READY,
                    "thunk_address": thunk[0],
                    "preserved_eflags": True,
                    "original_bytes": instruction.bytes_hex,
                    "replacement_bytes": rewritten_instruction.bytes_hex,
                    "write_semantics": "control-write-asserts-ready-on-reset-request",
                }
            )
            continue
        if address in PHASE7_AUDIO_BUFFER_COMMAND_VALUES and resident_privileged_boundary:
            rewritten_instruction = _resident_audio_buffer_command_rewrite(
                instruction,
                preflight.instructions,
            )
            rewritten[address] = rewritten_instruction
            rewrites.append(
                {
                    "address": address,
                    "address_hex": f"0x{address:08X}",
                    "kind": "audio-buffer-command-self-clear",
                    "status": "implemented-resident-finite-self-clear",
                    "family_sites": sorted(PHASE7_AUDIO_BUFFER_COMMAND_FAMILY),
                    "producer_sites": sorted(PHASE7_AUDIO_BUFFER_COMMAND_VALUES),
                    "caller_sites": sorted(PHASE7_AUDIO_BUFFER_COMMAND_CALL_SITES),
                    "command_value": PHASE7_AUDIO_BUFFER_COMMAND_VALUES[address],
                    "mailbox_displacement": 0x810,
                    "preserved_eflags": True,
                    "original_bytes": instruction.bytes_hex,
                    "replacement_bytes": rewritten_instruction.bytes_hex,
                    "write_semantics": "complete-self-clearing-buffer-command",
                }
            )
            continue
        if address in PHASE7_AUDIO_VOICE_COMMAND_WRITE_BASES and resident_privileged_boundary:
            rewritten_instruction = _resident_audio_voice_command_write_rewrite(instruction)
            rewritten[address] = rewritten_instruction
            rewritten_logical_pages.add(PHASE7_AUDIO_DSP_MMIO_PAGE)
            rewrites.append(
                {
                    "address": address,
                    "address_hex": f"0x{address:08X}",
                    "kind": "mmio-audio-voice-command-self-clear",
                    "status": "implemented-resident-finite-self-clear",
                    "logical_page": PHASE7_AUDIO_DSP_MMIO_PAGE,
                    "mapped_page": PHASE7_AUDIO_DSP_MMIO_PAGE,
                    "register_base": PHASE7_AUDIO_VOICE_COMMAND_WRITE_BASES[address],
                    "command_displacement": PHASE7_AUDIO_VOICE_COMMAND_DISPLACEMENT,
                    "pending_mask": PHASE7_AUDIO_VOICE_COMMAND_PENDING,
                    "preserved_eflags": True,
                    "original_bytes": instruction.bytes_hex,
                    "replacement_bytes": rewritten_instruction.bytes_hex,
                    "write_semantics": "complete-self-clearing-voice-command",
                }
            )
            continue
        if (
            address in PHASE7_AUDIO_BUFFER_MMIO_REFERENCE_SITES
            and resident_privileged_boundary
            and all(mmio_pages.get(page) == page for page in PHASE7_AUDIO_BUFFER_MMIO_PAGES)
        ):
            expected = PHASE7_AUDIO_BUFFER_MMIO_FAMILY[address]
            if instruction.bytes_hex.upper() != expected:
                raise Ia32BackendError(
                    f"0x{address:08X} {instruction.text()} does not match the "
                    "finite normal-live audio buffer MMIO family"
                )
            rewritten_logical_pages.update(PHASE7_AUDIO_BUFFER_MMIO_PAGES)
            rewrites.append(
                {
                    "address": address,
                    "address_hex": f"0x{address:08X}",
                    "kind": "mmio-audio-buffer-aperture-direct-shadow-reference",
                    "status": "implemented-resident-finite-direct-shadow",
                    "logical_start": PHASE7_AUDIO_BUFFER_MMIO_START,
                    "logical_end": PHASE7_AUDIO_BUFFER_MMIO_END,
                    "page_count": len(PHASE7_AUDIO_BUFFER_MMIO_PAGES),
                    "family_site_count": len(PHASE7_AUDIO_BUFFER_MMIO_FAMILY),
                    "copy_target": PHASE7_AUDIO_BUFFER_COPY_TARGET,
                    "copy_call_site_count": len(PHASE7_AUDIO_BUFFER_COPY_CALL_SITES),
                    "original_bytes": instruction.bytes_hex,
                    "replacement_bytes": instruction.bytes_hex,
                    "access_semantics": (
                        "native-pointer-and-control-access-against-zeroed-identity-aperture"
                    ),
                }
            )
            continue
        if (
            address in PHASE7_AUDIO_SG_MMIO_ACCESS_SITES
            and resident_privileged_boundary
            and mmio_pages.get(PHASE7_AUDIO_SG_MMIO_PAGE) == PHASE7_AUDIO_SG_MMIO_PAGE
        ):
            expected = PHASE7_AUDIO_SG_MMIO_FAMILY[address]
            if instruction.bytes_hex.upper() != expected:
                raise Ia32BackendError(
                    f"0x{address:08X} {instruction.text()} does not match the "
                    "finite normal-live audio SG MMIO family"
                )
            rewritten_logical_pages.add(PHASE7_AUDIO_SG_MMIO_PAGE)
            rewrites.append(
                {
                    "address": address,
                    "address_hex": f"0x{address:08X}",
                    "kind": "mmio-audio-sg-direct-shadow-access",
                    "status": "implemented-resident-finite-direct-shadow",
                    "logical_page": PHASE7_AUDIO_SG_MMIO_PAGE,
                    "mapped_page": PHASE7_AUDIO_SG_MMIO_PAGE,
                    "family_site_count": len(PHASE7_AUDIO_SG_MMIO_FAMILY),
                    "original_bytes": instruction.bytes_hex,
                    "replacement_bytes": instruction.bytes_hex,
                    "access_semantics": "native-instruction-against-zeroed-shadow-page",
                }
            )
            continue
        if (
            address in PHASE7_MCPX_INDEXED_MMIO_ACCESS_SITES
            and resident_privileged_boundary
            and mmio_pages.get(PHASE7_MCPX_MMIO_PAGE) == PHASE7_MCPX_MMIO_PAGE
        ):
            expected = PHASE7_MCPX_INDEXED_MMIO_FAMILY[address]
            if instruction.bytes_hex.upper() != expected:
                raise Ia32BackendError(
                    f"0x{address:08X} {instruction.text()} does not match the "
                    "finite normal-live MCPX indexed MMIO family"
                )
            rewritten_logical_pages.add(PHASE7_MCPX_MMIO_PAGE)
            rewrites.append(
                {
                    "address": address,
                    "address_hex": f"0x{address:08X}",
                    "kind": "mmio-mcpx-indexed-direct-shadow-access",
                    "status": "implemented-resident-finite-direct-shadow",
                    "logical_page": PHASE7_MCPX_MMIO_PAGE,
                    "mapped_page": PHASE7_MCPX_MMIO_PAGE,
                    "family_site_count": len(PHASE7_MCPX_INDEXED_MMIO_FAMILY),
                    "original_bytes": instruction.bytes_hex,
                    "replacement_bytes": instruction.bytes_hex,
                    "access_semantics": ("native-instruction-against-zeroed-shadow-page"),
                }
            )
            continue
        if (
            address in PHASE7_AUDIO_DSP_MMIO_FAMILY
            and resident_privileged_boundary
            and mmio_pages.get(PHASE7_AUDIO_DSP_MMIO_PAGE) == PHASE7_AUDIO_DSP_MMIO_PAGE
        ):
            _validate_phase7_audio_dsp_mmio_instruction(instruction)
            rewritten_logical_pages.add(PHASE7_AUDIO_DSP_MMIO_PAGE)
            rewrites.append(
                {
                    "address": address,
                    "address_hex": f"0x{address:08X}",
                    "kind": "mmio-audio-dsp-direct-shadow-access",
                    "status": "implemented-resident-finite-direct-shadow",
                    "logical_page": PHASE7_AUDIO_DSP_MMIO_PAGE,
                    "mapped_page": PHASE7_AUDIO_DSP_MMIO_PAGE,
                    "family_site_count": len(PHASE7_AUDIO_DSP_MMIO_FAMILY),
                    "original_bytes": instruction.bytes_hex,
                    "replacement_bytes": instruction.bytes_hex,
                    "access_semantics": "native-instruction-against-zeroed-shadow-page",
                }
            )
            continue
        if resident_privileged_boundary and any(
            operand.absolute == PHASE7_MCPX_FRAME_COUNTER_ADDRESS
            for operand in instruction.operands
        ):
            mapped = _phase3_mapped_address(
                PHASE7_MCPX_FRAME_COUNTER_ADDRESS,
                mmio_pages,
            )
            rewritten_instruction, thunk, register = _resident_mcpx_frame_counter_read_rewrite(
                instruction,
                mapped_address=mapped,
            )
            rewritten[address] = rewritten_instruction
            if thunk[0] not in mcpx_frame_counter_thunks:
                extra_code_spans.append(thunk)
                mcpx_frame_counter_thunks.add(thunk[0])
            rewritten_logical_pages.add(PHASE7_MCPX_FRAME_COUNTER_ADDRESS & ~(PAGE_SIZE - 1))
            rewrites.append(
                {
                    "address": address,
                    "address_hex": f"0x{address:08X}",
                    "kind": "mmio-mcpx-frame-counter-read",
                    "status": "implemented-resident-incrementing-shadow-thunk",
                    "logical_address": PHASE7_MCPX_FRAME_COUNTER_ADDRESS,
                    "mapped_address": mapped,
                    "increment": PHASE7_MCPX_FRAME_COUNTER_INCREMENT,
                    "destination_register": register,
                    "thunk_address": thunk[0],
                    "preserved_eflags": True,
                    "original_bytes": instruction.bytes_hex,
                    "replacement_bytes": rewritten_instruction.bytes_hex,
                }
            )
            continue
        if address in PHASE7_GPU_COMPLETION_SHADOW_READ_SITES and resident_privileged_boundary:
            rewritten_instruction = _resident_gpu_completion_shadow_read_rewrite(instruction)
            rewritten[address] = rewritten_instruction
            rewrites.append(
                {
                    "address": address,
                    "address_hex": f"0x{address:08X}",
                    "kind": "mmio-gpu-completion-shadow-read",
                    "status": "implemented-resident-submitted-value-mirror",
                    "original_bytes": instruction.bytes_hex,
                    "replacement_bytes": rewritten_instruction.bytes_hex,
                    "submitted_offset": PHASE7_GPU_SUBMITTED_CONTEXT_OFFSET,
                    "completed_offset": PHASE7_GPU_COMPLETED_CONTEXT_OFFSET,
                    "read_semantics": "submitted-context-is-completed-context",
                }
            )
            continue
        if address == PHASE7_PFIFO_WRITE_ONE_TO_CLEAR_SITE and resident_privileged_boundary:
            rewritten_instruction = _resident_pfifo_write_one_to_clear_rewrite(instruction)
            rewritten[address] = rewritten_instruction
            rewrites.append(
                {
                    "address": address,
                    "address_hex": f"0x{address:08X}",
                    "kind": "mmio-pfifo-write-one-to-clear",
                    "status": "implemented-resident-status-acknowledge",
                    "original_bytes": instruction.bytes_hex,
                    "replacement_bytes": rewritten_instruction.bytes_hex,
                    "register_base": "esi",
                    "status_offset": PHASE7_PFIFO_STATUS_OFFSET,
                    "clear_mask": PHASE7_PFIFO_STATUS_CLEAR_MASK,
                    "write_semantics": "acknowledged-status-is-zero",
                }
            )
            continue
        if address in PHASE7_SELF_CLEARING_MMIO_WRITE_SITES and resident_privileged_boundary:
            rewritten_instruction = _resident_self_clearing_mmio_rewrite(instruction)
            rewritten[address] = rewritten_instruction
            rewrites.append(
                {
                    "address": address,
                    "address_hex": f"0x{address:08X}",
                    "kind": "mmio-self-clearing-command-write",
                    "status": "implemented-resident-observed-discard",
                    "logical_address": PHASE7_SELF_CLEARING_MMIO_ADDRESS,
                    "logical_page": PHASE7_SELF_CLEARING_MMIO_ADDRESS & ~(PAGE_SIZE - 1),
                    "register_base": "eax",
                    "observed_base": PHASE7_SELF_CLEARING_MMIO_ADDRESS - 0x00100410,
                    "command_mask": PHASE7_SELF_CLEARING_MMIO_MASK,
                    "original_bytes": instruction.bytes_hex,
                    "replacement_bytes": rewritten_instruction.bytes_hex,
                    "write_semantics": "discard-self-clearing-command",
                }
            )
            continue
        if mnemonic in REJECTED_MNEMONICS:
            if _resident_fail_closed_trap(instruction) and resident_privileged_boundary:
                # This is the title's explicit assertion/no-return instruction, not an
                # injected unknown-boundary guard. Preserve the native terminal opcode
                # and record its semantics so the artifact inventory does not conflate
                # a resolved guest debug exit with an unresolved recompilation seam.
                rewritten[address] = instruction
                rewrites.append(
                    {
                        "address": address,
                        "address_hex": f"0x{address:08X}",
                        "kind": "software-debug-terminal",
                        "status": "implemented-native-terminal",
                        "original_bytes": instruction.bytes_hex,
                        "replacement_bytes": instruction.bytes_hex,
                        "terminal_semantics": "resident-exception-containment",
                    }
                )
                continue
            if mnemonic in {"cli", "sti", "wbinvd", "out"} and resident_privileged_boundary:
                rewritten_instruction = _resident_privileged_noop_rewrite(instruction)
                rewritten[address] = rewritten_instruction
                interrupt_mask = mnemonic in {"cli", "sti"}
                rewrites.append(
                    {
                        "address": address,
                        "address_hex": f"0x{address:08X}",
                        "kind": f"privileged-{mnemonic}",
                        "status": (
                            "implemented-cooperative-scheduler-mask"
                            if interrupt_mask
                            else "implemented-resident-observed-noop"
                        ),
                        "original_bytes": instruction.bytes_hex,
                        "replacement_bytes": rewritten_instruction.bytes_hex,
                        "publication_boundary": "resident-scheduler-safe-point",
                        "interrupt_delivery": (
                            "no-asynchronous-injection-inside-native-lane-dispatch"
                            if interrupt_mask
                            else "unchanged"
                        ),
                    }
                )
                continue
            if mnemonic == "sgdt" and resident_privileged_boundary:
                rewritten_instruction, thunk = _resident_sgdt_rewrite(
                    instruction,
                    gdtr_base=capsule.state.gdtr_base,
                    gdtr_limit=capsule.state.gdtr_limit,
                )
                rewritten[address] = rewritten_instruction
                extra_code_spans.append(thunk)
                rewrites.append(
                    {
                        "address": address,
                        "address_hex": f"0x{address:08X}",
                        "kind": "privileged-sgdt",
                        "status": "implemented-fixed-architectural-state-thunk",
                        "original_bytes": instruction.bytes_hex,
                        "replacement_bytes": rewritten_instruction.bytes_hex,
                        "thunk_address": thunk[0],
                        "gdtr_base": _u32(capsule.state.gdtr_base),
                        "gdtr_limit": capsule.state.gdtr_limit & 0xFFFF,
                    }
                )
                continue
            if mnemonic == "in" and resident_privileged_boundary:
                inline = _resident_zero_port_read_rewrite(rewritten, instruction)
                rewritten.update(inline)
                rewrites.append(
                    {
                        "address": address,
                        "address_hex": f"0x{address:08X}",
                        "kind": "privileged-in-port-zero",
                        "status": "implemented-observed-port-value-inline-wrapper",
                        "port": 0x80C0,
                        "result": 0,
                        "prefix_address": PHASE7_ZERO_PORT_READ_PREFIX_SITE,
                        "original_bytes": "66BAC080EC",
                        "replacement_bytes": "9C24009D90",
                        "preserved_eflags": True,
                    }
                )
                continue
            if mnemonic != "rdtsc":
                raise Ia32BackendError(
                    f"0x{address:08X} {instruction.mnemonic} has no Phase-3 privileged rewrite"
                )
            if resident_privileged_boundary:
                if address == PHASE7_DENSE_RDTSC_SITE:
                    inline, dense_thunk = _phase3_dense_timestamp_rewrite(
                        rewritten,
                        instruction,
                    )
                    if not timestamp_step_count:
                        timestamp_thunk, timestamp_data = _phase3_shared_timestamp_thunk(
                            capsule.state.timestamp_counter
                        )
                        extra_code_spans.extend(
                            (
                                (ARCHITECTURE_RDTSC_THUNK_ADDRESS, timestamp_thunk),
                                (ARCHITECTURE_TIMESTAMP_COUNTER_ADDRESS, timestamp_data),
                            )
                        )
                    rewritten.update(inline)
                    extra_code_spans.append(dense_thunk)
                    timestamp_step_count = 1
                    rewrites.append(
                        {
                            "address": address,
                            "address_hex": f"0x{address:08X}",
                            "kind": "privileged-rdtsc",
                            "status": "implemented-deterministic-dense-helper-thunk",
                            "prefix_address": PHASE7_DENSE_RDTSC_PREFIX_SITE,
                            "timestamp_counter": capsule.state.timestamp_counter,
                            "counter_address": ARCHITECTURE_TIMESTAMP_COUNTER_ADDRESS,
                            "thunk_address": ARCHITECTURE_RDTSC_THUNK_ADDRESS,
                            "dense_thunk_address": ARCHITECTURE_DENSE_RDTSC_THUNK_ADDRESS,
                            "step": DETERMINISTIC_TSC_STEP,
                        }
                    )
                    continue
                try:
                    rewritten_instruction, island = _phase3_shared_timestamp_rewrite(
                        instruction,
                        occupied=occupied,
                        stop_eip=stop_eip,
                    )
                except Ia32BackendError:
                    # A decoded but unobserved dense timer path remains an explicit
                    # terminal guard. Normal-validation closure will reject it if its
                    # containing profiled function requires an executable rewrite.
                    continue
                if not timestamp_step_count:
                    timestamp_thunk, timestamp_data = _phase3_shared_timestamp_thunk(
                        capsule.state.timestamp_counter
                    )
                    extra_code_spans.extend(
                        (
                            (ARCHITECTURE_RDTSC_THUNK_ADDRESS, timestamp_thunk),
                            (ARCHITECTURE_TIMESTAMP_COUNTER_ADDRESS, timestamp_data),
                        )
                    )
                rewritten[address] = rewritten_instruction
                extra_code_spans.append(island)
                occupied.update(range(island[0], island[0] + len(island[1])))
                timestamp_step_count = 1
                rewrites.append(
                    {
                        "address": address,
                        "address_hex": f"0x{address:08X}",
                        "kind": "privileged-rdtsc",
                        "status": "implemented-deterministic-shared-counter-island",
                        "timestamp_counter": capsule.state.timestamp_counter,
                        "counter_address": ARCHITECTURE_TIMESTAMP_COUNTER_ADDRESS,
                        "thunk_address": ARCHITECTURE_RDTSC_THUNK_ADDRESS,
                        "island_address": island[0],
                        "step": DETERMINISTIC_TSC_STEP,
                    }
                )
                continue
            if timestamp_step_count:
                raise Ia32BackendError(
                    f"0x{address:08X} rdtsc requires path-sensitive counter versioning; "
                    "only one statically reached site is currently supported"
                )
            try:
                rewritten_instruction, island = _phase3_rewrite_island(
                    instruction,
                    timestamp_counter=capsule.state.timestamp_counter,
                    occupied=occupied,
                    stop_eip=stop_eip,
                )
            except Ia32BackendError:
                try:
                    inline = _phase3_inline_timestamp_wrapper(
                        rewritten,
                        instruction,
                        timestamp_counter=capsule.state.timestamp_counter,
                    )
                except Ia32BackendError:
                    raise
                rewritten.update(inline)
                rewrite_kind = "implemented-deterministic-inline-wrapper"
                island_address = None
            else:
                rewritten[address] = rewritten_instruction
                extra_code_spans.append(island)
                occupied.update(range(island[0], island[0] + len(island[1])))
                rewrite_kind = "implemented-deterministic-island"
                island_address = island[0]
            timestamp_step_count = 1
            record = {
                "address": address,
                "address_hex": f"0x{address:08X}",
                "kind": "privileged-rdtsc",
                "status": rewrite_kind,
                "timestamp_counter": capsule.state.timestamp_counter,
                "step": DETERMINISTIC_TSC_STEP,
            }
            if island_address is not None:
                record["island_address"] = island_address
            rewrites.append(record)
            continue

        active = instruction
        for operand in instruction.operands:
            if operand.segment is not None:
                if operand.segment.casefold() != "fs" or operand.absolute is None:
                    raise Ia32BackendError(
                        f"0x{address:08X} uses dynamic {operand.segment or 'segment'} memory; "
                        "Phase-3 TLS rewriting requires an absolute FS displacement"
                    )
                logical = _u32(capsule.state.fs_base + int(operand.absolute))
                active = _replace_static_u32(
                    active,
                    int(operand.absolute),
                    logical,
                    remove_fs_prefix=True,
                )
                rewritten_logical_pages.add(logical & ~(PAGE_SIZE - 1))
                rewrites.append(
                    {
                        "address": address,
                        "address_hex": f"0x{address:08X}",
                        "kind": "fs-tls-absolute",
                        "logical_address": logical,
                        "mapped_address": logical,
                        "status": "implemented-static-address",
                    }
                )
            elif operand.absolute is not None:
                logical = _u32(int(operand.absolute))
                mapped = _phase3_mapped_address(logical, mmio_pages)
                logical_page = logical & ~(PAGE_SIZE - 1)
                if logical_page == PHASE7_PHYSICAL_ZERO_PAGE and mapped == logical:
                    rewritten_logical_pages.add(logical_page)
                    rewrites.append(
                        {
                            "address": address,
                            "address_hex": f"0x{address:08X}",
                            "kind": "physical-zero-direct-page",
                            "logical_address": logical,
                            "mapped_address": mapped,
                            "status": "implemented-direct-page",
                        }
                    )
                elif logical_page == PHASE7_MCPX_MMIO_PAGE and mapped == logical:
                    rewritten_logical_pages.add(logical_page)
                    rewrites.append(
                        {
                            "address": address,
                            "address_hex": f"0x{address:08X}",
                            "kind": "mmio-mcpx-direct-shadow-access",
                            "logical_address": logical,
                            "mapped_address": mapped,
                            "status": "implemented-resident-direct-page",
                        }
                    )
                if mapped != logical:
                    active = _replace_static_u32(active, logical, mapped)
                    rewritten_logical_pages.add(logical_page)
                    rewrites.append(
                        {
                            "address": address,
                            "address_hex": f"0x{address:08X}",
                            "kind": (
                                "physical-zero-shadow-page"
                                if logical_page == PHASE7_PHYSICAL_ZERO_PAGE
                                else (
                                    "physical-page-alias"
                                    if logical < PHYSICAL_ALIAS_END
                                    else "mmio-shadow-page"
                                )
                            ),
                            "logical_address": logical,
                            "mapped_address": mapped,
                            "status": "implemented-static-address",
                        }
                    )
        rewritten[address] = active

    fault_pages = tuple(
        sorted({_u32(value) & ~(PAGE_SIZE - 1) for value in profile.recoverable_fault_pages})
    )
    fault_sites: list[dict[str, Any]] = []
    for instruction in preflight.instructions.values():
        for operand_index, operand in enumerate(instruction.operands):
            if operand.kind != "mem" or operand.absolute is None:
                continue
            logical = _u32(
                int(operand.absolute) + (capsule.state.fs_base if operand.segment == "fs" else 0)
            )
            if logical & ~(PAGE_SIZE - 1) not in fault_pages:
                continue
            write = operand_index == 0 and instruction.mnemonic.casefold() not in {
                "cmp",
                "test",
            }
            fault_sites.append(
                {
                    "address": instruction.address,
                    "address_hex": f"0x{instruction.address:08X}",
                    "exception_code": 0xC0000005,
                    "access_kind": "write" if write else "read",
                    "access_type": 1 if write else 0,
                    "access_address": logical,
                }
            )
    missing_fault_pages = [
        page
        for page in fault_pages
        if not any(int(site["access_address"]) & ~(PAGE_SIZE - 1) == page for site in fault_sites)
    ]
    if missing_fault_pages:
        formatted = ", ".join(f"0x{page:08X}" for page in missing_fault_pages)
        raise Ia32BackendError(
            "recoverable fault pages require an address-named static access: " + formatted
        )
    for page in preflight.page_addresses:
        if (
            page != PHASE7_PHYSICAL_ZERO_PAGE and page < PHYSICAL_ALIAS_START
        ) or page in fault_pages:
            continue
        if mmio_pages.get(page) == page:
            continue
        if page not in rewritten_logical_pages:
            raise Ia32BackendError(
                f"0x{page:08X} is reached through a dynamic high/MMIO/alias address; "
                "no Phase-3 static rewrite names the access"
            )

    owners = {
        _u32(int(page)) & ~(PAGE_SIZE - 1): str(owner)
        for page, owner in (profile.dirty_page_owners or {}).items()
    }
    logical_pages = sorted(set(preflight.page_addresses))
    bindings_by_mapped: dict[int, list[int]] = {}
    for logical in logical_pages:
        if logical in fault_pages:
            continue
        mapped = _phase3_mapped_address(logical, mmio_pages)
        bindings_by_mapped.setdefault(mapped, []).append(logical)
    page_bindings = []
    ownership = []
    for mapped, logical_values in sorted(bindings_by_mapped.items()):
        logical_values = sorted(set(logical_values))
        owner_values = sorted(
            {
                owners.get(
                    logical,
                    "renderer" if logical >= 0x80000000 else "guest",
                )
                for logical in logical_values
            }
        )
        owner = owner_values[0] if len(owner_values) == 1 else "+".join(owner_values)
        page_bindings.append(
            {
                "mapped_address": mapped,
                "logical_addresses": logical_values,
                "owner": owner,
            }
        )
        ownership.append(
            {
                "mapped_address": mapped,
                "logical_addresses": logical_values,
                "owner": owner,
                "publication": "native-dirty-page-bit",
            }
        )
    architecture_map = {
        "format": "b2-recomp-ia32-architecture-map",
        "version": 1,
        "contract_id": ia32_architecture_contract_id(),
        "exchange_version": ARCHITECTURE_EXCHANGE_VERSION,
        "fpu_mmx_mode": (
            "x87"
            if capsule.state.fpu_stack
            else "mmx"
            if any(capsule.state.mmx_registers.values())
            else "empty"
        ),
        "rewrites": rewrites,
        "page_bindings": page_bindings,
        "dirty_ownership": ownership,
        "recoverable_fault_pages": list(fault_pages),
        "recoverable_fault_sites": fault_sites,
        "cross_page_ranges": [
            {"address": address, "size": size}
            for address, size in preflight.memory_ranges
            if (address & (PAGE_SIZE - 1)) + size > PAGE_SIZE
        ],
        "stack": {
            "entry_esp": capsule.state.get_register("esp"),
            "native_call_ret_semantics": True,
            "host_service_stack_cleanup": [
                {
                    "target": service.target,
                    "bytes": service.stack_cleanup_bytes,
                }
                for service in preflight.host_service_thunks
            ],
        },
        "self_modifying_executable_memory": False,
        "new_executable_memory": False,
        "unsupported_sites": [],
    }
    return _Ia32ArchitecturePlan(
        instructions=rewritten,
        extra_code_spans=tuple(extra_code_spans),
        page_bindings=tuple(page_bindings),
        rewrites=tuple(rewrites),
        dirty_ownership=tuple(ownership),
        fault_pages=fault_pages,
        timestamp_step_count=timestamp_step_count,
        architecture_map=architecture_map,
    )


def _mixed_code_page_data_spans(
    capsule: ReplayCapsule,
    memory_ranges: Iterable[tuple[int, int]],
    code_spans: Sequence[tuple[int, bytes]],
    *,
    verified_code_spans: Sequence[tuple[int, bytes]] | None = None,
) -> tuple[tuple[int, bytes], ...]:
    """Restore only traced data bytes that share executable code pages."""

    code_pages: set[int] = set()
    code_bytes: dict[int, int] = {}
    for address, payload in code_spans:
        for offset, value in enumerate(payload):
            code_bytes[address + offset] = value
        code_pages.update(
            range(
                address & ~(PAGE_SIZE - 1),
                (address + len(payload) + PAGE_SIZE - 1) & ~(PAGE_SIZE - 1),
                PAGE_SIZE,
            )
        )
    verified_code_bytes: dict[int, int] = {}
    for address, payload in verified_code_spans or code_spans:
        for offset, value in enumerate(payload):
            verified_code_bytes[address + offset] = value
    data_bytes: dict[int, int] = {}
    for address, size in memory_ranges:
        cursor = 0
        while cursor < size:
            current = address + cursor
            page = current & ~(PAGE_SIZE - 1)
            chunk_size = min(size - cursor, PAGE_SIZE - (current & (PAGE_SIZE - 1)))
            if page in code_pages:
                payload = capsule.memory.read(current, chunk_size)
                for offset, value in enumerate(payload):
                    byte_address = current + offset
                    code_value = code_bytes.get(byte_address)
                    verified_value = verified_code_bytes.get(byte_address)
                    if verified_value is not None and verified_value != value:
                        raise Ia32BackendError(
                            "traced data conflicts with verified instruction byte at "
                            f"0x{byte_address:08X}"
                        )
                    if code_value is None:
                        data_bytes[byte_address] = value
            cursor += chunk_size
    spans: list[tuple[int, bytes]] = []
    start: int | None = None
    previous: int | None = None
    data_payload = bytearray()
    for address, value in sorted(data_bytes.items()):
        if previous is None or address != previous + 1:
            if start is not None:
                spans.append((start, bytes(data_payload)))
            start = address
            data_payload = bytearray()
        data_payload.append(value)
        previous = address
    if start is not None:
        spans.append((start, bytes(data_payload)))
    return tuple(spans)


def _mappable_data_page(address: int, *, detached_guest: bool = False) -> bool:
    page = _u32(address) & ~(PAGE_SIZE - 1)
    if detached_guest and page == DETACHED_HELPER_IMAGE_BASE:
        # The image loader needs the PE header only until mainCRTStartup has
        # installed the native runtime.  The XBE header occupies the same
        # logical page and is restored in place before guest entry so both
        # static and dynamic guest accesses observe the captured bytes.
        return True
    helper_base = DETACHED_HELPER_IMAGE_BASE if detached_guest else HELPER_IMAGE_BASE
    helper_limit = helper_base + WINDOWS_ALLOCATION_GRANULARITY * 4
    return bool(
        (
            (MINIMUM_USER_ADDRESS if detached_guest else GUEST_SECTION_ADDRESS) <= page < 0xC0000000
            or page in PHASE7_DIRECT_MMIO_PAGES
            or page == PHASE7_AUDIO_DSP_MMIO_PAGE
        )
        and not _address_range_collides(
            page,
            PAGE_SIZE,
            helper_base,
            helper_limit,
        )
        and not _address_range_collides(
            page,
            PAGE_SIZE,
            THUNK_REGION,
            SCRATCH_ADDRESS + WINDOWS_ALLOCATION_GRANULARITY,
        )
    )


def _artifact_page_addresses(
    capsule: ReplayCapsule,
    instructions: Mapping[int, X86Instruction],
    code_spans: Sequence[tuple[int, bytes]],
    dependency_page_addresses: Iterable[int],
    *,
    detached_guest: bool = False,
) -> tuple[int, ...]:
    """Select captured/data dependency pages without exposing undecoded code bytes."""

    code_pages: set[int] = set()
    for address, payload in code_spans:
        code_pages.update(
            range(
                address & ~(PAGE_SIZE - 1),
                (address + len(payload) + PAGE_SIZE - 1) & ~(PAGE_SIZE - 1),
                PAGE_SIZE,
            )
        )
    pages = {
        _u32(address) & ~(PAGE_SIZE - 1)
        for address in dependency_page_addresses
        if _mappable_data_page(address, detached_guest=detached_guest)
    }
    rejected_pages = {
        _u32(address) & ~(PAGE_SIZE - 1)
        for address in dependency_page_addresses
        if not _mappable_data_page(address, detached_guest=detached_guest)
    }
    if rejected_pages:
        formatted = ", ".join(f"0x{address:08X}" for address in sorted(rejected_pages))
        raise Ia32BackendError(f"fixed slice depends on unmappable Win32 guest pages: {formatted}")
    if detached_guest:
        pages.update(code_pages)
    else:
        pages.difference_update(code_pages)
    return tuple(sorted(pages))


def _mov_absolute_store(opcode: bytes, address: int) -> bytes:
    return opcode + struct.pack("<I", address)


def _build_start_thunk(*, scratch: int = SCRATCH_ADDRESS) -> bytes:
    code = bytearray()
    code += _mov_absolute_store(b"\x89\x25", scratch + SCRATCH_HOST_ESP_OFFSET)
    code += _mov_absolute_store(b"\x89\x1d", scratch + SCRATCH_HOST_EBX_OFFSET)
    code += _mov_absolute_store(b"\x89\x35", scratch + SCRATCH_HOST_ESI_OFFSET)
    code += _mov_absolute_store(b"\x89\x3d", scratch + SCRATCH_HOST_EDI_OFFSET)
    code += _mov_absolute_store(b"\x89\x2d", scratch + SCRATCH_HOST_EBP_OFFSET)
    code += _mov_absolute_store(b"\x0f\xae\x0d", scratch + SCRATCH_FXSAVE_OFFSET)
    code += _mov_absolute_store(b"\xff\x35", scratch + SCRATCH_EFLAGS_OFFSET)
    code += b"\x9d"
    register_loads = {
        "eax": b"\xa1",
        "ecx": b"\x8b\x0d",
        "edx": b"\x8b\x15",
        "ebx": b"\x8b\x1d",
        "ebp": b"\x8b\x2d",
        "esi": b"\x8b\x35",
        "edi": b"\x8b\x3d",
        "esp": b"\x8b\x25",
    }
    for name in ("eax", "ecx", "edx", "ebx", "ebp", "esi", "edi", "esp"):
        code += _mov_absolute_store(register_loads[name], scratch + SCRATCH_REGISTER_OFFSETS[name])
    code += _mov_absolute_store(b"\xff\x25", scratch + SCRATCH_ENTRY_EIP_OFFSET)
    return bytes(code)


def _build_exit_thunk(*, scratch: int = SCRATCH_ADDRESS) -> bytes:
    register_stores = {
        "eax": b"\xa3",
        "ecx": b"\x89\x0d",
        "edx": b"\x89\x15",
        "ebx": b"\x89\x1d",
        "esp": b"\x89\x25",
        "ebp": b"\x89\x2d",
        "esi": b"\x89\x35",
        "edi": b"\x89\x3d",
    }
    code = bytearray()
    for name in REGISTER_NAMES:
        code += _mov_absolute_store(register_stores[name], scratch + SCRATCH_REGISTER_OFFSETS[name])
    code += _mov_absolute_store(b"\x0f\xae\x05", scratch + SCRATCH_FXSAVE_OFFSET)
    code += _mov_absolute_store(b"\x8b\x25", scratch + SCRATCH_HOST_ESP_OFFSET)
    code += b"\x9c"
    code += _mov_absolute_store(b"\x8f\x05", scratch + SCRATCH_EFLAGS_OFFSET)
    code += _mov_absolute_store(b"\x8b\x1d", scratch + SCRATCH_HOST_EBX_OFFSET)
    code += _mov_absolute_store(b"\x8b\x35", scratch + SCRATCH_HOST_ESI_OFFSET)
    code += _mov_absolute_store(b"\x8b\x3d", scratch + SCRATCH_HOST_EDI_OFFSET)
    code += _mov_absolute_store(b"\x8b\x2d", scratch + SCRATCH_HOST_EBP_OFFSET)
    code += b"\xc3"
    return bytes(code)


def _bytes_initializer(payload: bytes) -> str:
    return ",".join(f"0x{value:02X}" for value in payload)


def _jump_patch(address: int, target: int) -> bytes:
    delta = _u32(target) - (_u32(address) + 5)
    return b"\xe9" + struct.pack("<i", delta)


def _stop_patch(stop_eip: int) -> bytes:
    return _jump_patch(stop_eip, EXIT_THUNK_ADDRESS)


def _return_constant_thunk(service: _HostServiceThunk) -> bytes:
    if service.kind != NATIVE_HOST_SERVICE_RETURN_CONSTANT:
        raise Ia32BackendError(
            f"service {service.shim_name} requires the Phase-4 audited dispatcher thunk"
        )
    code = bytearray(b"\xb8" + struct.pack("<I", service.value))
    if service.stack_cleanup_bytes:
        code += b"\xc2" + struct.pack("<H", service.stack_cleanup_bytes)
    else:
        code += b"\xc3"
    return bytes(code)


def _audited_service_body(index: int, service: _HostServiceThunk) -> bytes:
    """Preserve guest state and call the fixed native service dispatcher."""

    if index < 0:
        raise Ia32BackendError("host-service thunk index cannot be negative")
    code = bytearray(
        b"\x0f\xae\x05"
        + struct.pack("<I", SCRATCH_ADDRESS + SCRATCH_SERVICE_FXSAVE_OFFSET)
        + b"\x9c"  # pushfd
        b"\x51"  # push ecx
        b"\x52"  # push edx
        b"\x8d\x44\x24\x10"  # lea eax,[esp+16] -- first stack argument
        b"\x50"  # push eax
        b"\xff\x74\x24\x08"  # push saved ecx -- this pointer
        b"\x68" + struct.pack("<I", index)
    )
    body_address = AUDITED_SERVICE_THUNK_BASE + index * AUDITED_SERVICE_THUNK_STRIDE
    call_next = _u32(body_address + len(code) + 5)
    call_delta = _u32(SERVICE_DISPATCH_RELAY_ADDRESS - call_next)
    code += b"\xe8" + struct.pack(
        "<i", call_delta - 0x1_0000_0000 if call_delta & 0x80000000 else call_delta
    )
    code += bytearray(
        b"\x83\xc4\x0c"  # discard dispatcher arguments
        b"\x0f\xae\x0d"
        + struct.pack("<I", SCRATCH_ADDRESS + SCRATCH_SERVICE_FXSAVE_OFFSET)
        + b"\x5a\x59\x9d"  # restore edx/ecx/eflags
    )
    if service.stack_cleanup_bytes:
        code += b"\xc2" + struct.pack("<H", service.stack_cleanup_bytes)
    else:
        code += b"\xc3"
    return bytes(code)


def _audited_service_thunk(index: int, service: _HostServiceThunk) -> bytes:
    body_address = AUDITED_SERVICE_THUNK_BASE + index * AUDITED_SERVICE_THUNK_STRIDE
    delta = _u32(body_address - _u32(service.target + 5))
    return b"\xe9" + struct.pack("<i", delta - 0x1_0000_0000 if delta & 0x80000000 else delta)


def _host_service_thunk_bytes(
    index: int,
    service: _HostServiceThunk,
    *,
    audited: bool,
) -> bytes:
    return _audited_service_thunk(index, service) if audited else _return_constant_thunk(service)


def ia32_execution_contract() -> dict[str, Any]:
    """Describe the frozen Phase-0 exchange, thunk, timing, and state contract."""

    timing = {
        "version": TIMING_PROTOCOL_VERSION,
        "internal_clock": "QueryPerformanceCounter",
        "internal_interval": "before-entry-thunk-through-after-exit-thunk",
        "internal_includes": [
            "host-nonvolatile-register-save-restore",
            "guest-register-and-flags-restore",
            "FXRSTOR",
            "guest-execution",
            "host-service-thunks",
            "FXSAVE",
            "final-state-capture",
        ],
        "internal_field": "elapsed_ns",
        "process_clock": "host-perf-counter-ns",
        "process_interval": "helper-create-through-process-exit",
        "process_field": "process_elapsed_ns",
        "unit": "nanoseconds",
    }
    observable_state = {
        "version": OBSERVABLE_STATE_VERSION,
        "x87_control_word_mask": X87_CONTROL_WORD_OBSERVABLE_MASK,
        "x87_control_word_mask_hex": f"0x{X87_CONTROL_WORD_OBSERVABLE_MASK:04X}",
        "mxcsr_defined_mask": MXCSR_DEFINED_MASK,
        "mxcsr_defined_mask_hex": f"0x{MXCSR_DEFINED_MASK:08X}",
        "mxcsr_exact_control_mask": MXCSR_PHASE0_COMPARISON_MASK,
        "mxcsr_exact_control_mask_hex": f"0x{MXCSR_PHASE0_COMPARISON_MASK:08X}",
        "mxcsr_sticky_status_mask": MXCSR_STICKY_STATUS_MASK,
        "mxcsr_sticky_status_mask_hex": f"0x{MXCSR_STICKY_STATUS_MASK:08X}",
        "mxcsr_status_policy": "reported-separately-until-decoded-reference-models-sticky-flags",
        "logical_state": [
            "eip",
            "gprs",
            "observable-eflags",
            "segments",
            "timestamp-counter",
            "normalized-x87-control",
            "x87-status-and-stack",
            "exact-mxcsr-control-and-reported-sticky-status",
            "xmm",
            "mmx",
            "selected-memory-pages",
        ],
        "phase0_input_limit": "empty-x87-stack-and-zero-mmx-registers",
        "historical_proof_alias_policy": (
            "report-but-exclude-mmx-x87-physical-alias-view-from-old-compute-proof"
        ),
    }
    contract = {
        "format": EXECUTION_CONTRACT_FORMAT,
        "version": EXECUTION_CONTRACT_VERSION,
        "artifact": {
            "format": ARTIFACT_FORMAT,
            "version": ARTIFACT_VERSION,
            "target": TARGET_TRIPLE,
        },
        "exchange": {
            "magic": EXCHANGE_MAGIC,
            "version": EXCHANGE_VERSION,
            "header_size": EXCHANGE_HEADER_SIZE,
            "fxsave_offset": EXCHANGE_FXSAVE_OFFSET,
            "fxsave_size": EXCHANGE_FXSAVE_SIZE,
        },
        "entry_exit_thunk": {
            "abi_version": ENTRY_EXIT_THUNK_ABI_VERSION,
            "start_address": START_THUNK_ADDRESS,
            "exit_address": EXIT_THUNK_ADDRESS,
            "start_code_sha256": _sha256(_build_start_thunk()),
            "exit_code_sha256": _sha256(_build_exit_thunk()),
        },
        "host_service_thunk": {
            "abi_version": HOST_SERVICE_THUNK_ABI_VERSION,
            "return_constant_kind": NATIVE_HOST_SERVICE_RETURN_CONSTANT,
            "encoding": "mov-eax-imm32-then-ret-imm16-or-ret",
            "stack_cleanup_bytes": dict(sorted(SUPPORTED_RETURN_CONSTANT_SERVICES.items())),
        },
        "observable_state": observable_state,
        "timing": timing,
    }
    contract["observable_state_id"] = _sha256(_canonical_json(observable_state))
    contract["timing_protocol_id"] = _sha256(_canonical_json(timing))
    return contract


def ia32_execution_contract_id() -> str:
    identity = _sha256(_canonical_json(ia32_execution_contract()))
    if EXECUTION_CONTRACT_VERSION == 1 and identity != PHASE0_EXECUTION_CONTRACT_ID:
        raise Ia32BackendError(
            "the frozen Phase-0 IA-32 execution contract changed without a version bump"
        )
    return identity


def ia32_persistent_worker_contract() -> dict[str, Any]:
    """Describe the Phase-1 resident worker lifecycle and command protocol."""

    return {
        "format": PERSISTENT_WORKER_CONTRACT_FORMAT,
        "version": PERSISTENT_WORKER_CONTRACT_VERSION,
        "base_execution_contract_id": PHASE0_EXECUTION_CONTRACT_ID,
        "exchange": {
            "magic": EXCHANGE_MAGIC,
            "version": WORKER_EXCHANGE_VERSION,
            "header_size": EXCHANGE_HEADER_SIZE,
            "protocol_version": WORKER_PROTOCOL_VERSION,
            "request_event_suffix": "-request",
            "response_event_suffix": "-response",
        },
        "commands": {
            "enter": WORKER_COMMAND_ENTER,
            "resume": WORKER_COMMAND_RESUME,
            "service_return": WORKER_COMMAND_SERVICE_RETURN,
            "stop": WORKER_COMMAND_STOP,
        },
        "statuses": {
            "pending": EXCHANGE_STATUS_PENDING,
            "running": EXCHANGE_STATUS_RUNNING,
            "complete": EXCHANGE_STATUS_COMPLETE,
            "ready": WORKER_STATUS_READY,
            "stopped": WORKER_STATUS_STOPPED,
            "fault": WORKER_STATUS_FAULT,
            "protocol_error": WORKER_STATUS_PROTOCOL_ERROR,
        },
        "lifecycle": [
            "launch-once",
            "map-title-ram-thunks-and-control-once",
            "signal-ready",
            "reset-state-and-pages-before-each-command",
            "execute-entry-through-fixed-stop",
            "publish-state-pages-and-timing",
            "wait-for-next-command",
            "acknowledge-stop-and-exit",
        ],
        "steady_state_excludes": ["process-creation", "PE-mapping", "runtime-code-generation"],
        "normal_runtime_python_callbacks": 0,
    }


def ia32_persistent_worker_contract_id() -> str:
    identity = _sha256(_canonical_json(ia32_persistent_worker_contract()))
    if PERSISTENT_WORKER_CONTRACT_VERSION == 1 and identity != PHASE1_PERSISTENT_WORKER_CONTRACT_ID:
        raise Ia32BackendError(
            "the frozen Phase-1 IA-32 persistent-worker contract changed without a version bump"
        )
    return identity


def ia32_decoded_store_artifact_contract() -> dict[str, Any]:
    """Describe the Phase-2 offline decoded-store artifact boundary."""

    return {
        "format": DECODED_STORE_ARTIFACT_CONTRACT_FORMAT,
        "version": DECODED_STORE_ARTIFACT_CONTRACT_VERSION,
        "base_execution_contract_id": PHASE0_EXECUTION_CONTRACT_ID,
        "persistent_worker_contract_id": PHASE1_PERSISTENT_WORKER_CONTRACT_ID,
        "decoded_block_store": {
            "format": "sqlite-zlib-json-blob",
            "version": DECODED_BLOCK_STORE_VERSION,
            "identity": "canonical-image-record-snapshot",
            "instruction_bytes": "bound-from-matching-xbe-section",
        },
        "required_outputs": [
            "section-map.json",
            "coverage-map.json",
            "direct-edge-relocations.json",
            "indirect-target-table.json",
            "rewrite-manifest.json",
        ],
        "direct_edge_policy": "preserve-original-fixed-va-encoding",
        "unknown_target_policy": "named-static-coverage-gap-no-runtime-fallback",
        "loader_rejects": [
            "xbe-content-mismatch",
            "decoded-store-snapshot-mismatch",
            "toolchain-content-mismatch",
            "sidecar-content-mismatch",
        ],
        "normal_execution": {
            "runtime_decoding": False,
            "runtime_compilation": False,
            "runtime_code_patching": False,
            "native_promotion": False,
            "raw_xbe_execution": False,
            "python_callbacks": 0,
        },
    }


def ia32_decoded_store_artifact_contract_id() -> str:
    identity = _sha256(_canonical_json(ia32_decoded_store_artifact_contract()))
    if (
        DECODED_STORE_ARTIFACT_CONTRACT_VERSION == 1
        and identity != PHASE2_DECODED_STORE_ARTIFACT_CONTRACT_ID
    ):
        raise Ia32BackendError(
            "the frozen Phase-2 IA-32 decoded-store contract changed without a version bump"
        )
    return identity


def ia32_architecture_contract() -> dict[str, Any]:
    """Describe the Phase-3 architectural, memory, and fault boundary."""

    return {
        "format": ARCHITECTURE_CONTRACT_FORMAT,
        "version": ARCHITECTURE_CONTRACT_VERSION,
        "base_execution_contract_id": PHASE0_EXECUTION_CONTRACT_ID,
        "persistent_worker_contract_id": PHASE1_PERSISTENT_WORKER_CONTRACT_ID,
        "decoded_store_artifact_contract_id": PHASE2_DECODED_STORE_ARTIFACT_CONTRACT_ID,
        "exchange": {
            "version": ARCHITECTURE_EXCHANGE_VERSION,
            "fxsave": "complete-x87-mmx-sse-image",
            "dirty_page_bits": "one-native-computed-byte-per-exchange-page",
        },
        "architectural_state": {
            "x87_mmx_alias": "one-physical-register-file-explicit-active-mode",
            "x87_values": "logical-stack-normalized-from-80-bit-registers",
            "sse": "exact-xmm-bits-and-defined-mxcsr-bits",
            "fs_tls": "absolute-build-time-rewrite-for-static-fs-memory-only",
            "stack": "native-esp-ret-and-stdcall-semantics",
            "timestamp_counter": {
                "policy": "deterministic-build-time-rdtsc-island",
                "step": DETERMINISTIC_TSC_STEP,
            },
        },
        "memory": {
            "physical_alias": "0xA0000000-0xBFFFFFFF-to-address-and-0x7FFFFFFF",
            "cross_page": "native-access-with-every-touched-page-prebound",
            "mmio": "static-absolute-page-to-deterministic-shadow-page",
            "dirty_ownership": "native-page-change-bit-plus-build-time-owner",
            "self_modifying_code": "unsupported-address-named-static-coverage-gap",
            "new_executable_memory": "unsupported-without-aot-form",
        },
        "faults": {
            "containment": "native-static-fault-command-boundary-with-veh-last-resort",
            "identity": ["exception-code", "guest-eip", "access-kind", "access-address"],
            "worker_lifetime": "recoverable-fault-does-not-terminate-worker",
        },
        "unsupported_policy": "fail-closed-with-address-named-rewrite-or-coverage-diagnostic",
        "normal_runtime_python_callbacks": 0,
    }


def ia32_architecture_contract_id() -> str:
    identity = _sha256(_canonical_json(ia32_architecture_contract()))
    if (
        ARCHITECTURE_CONTRACT_VERSION == 1
        and PHASE3_ARCHITECTURE_CONTRACT_ID
        and identity != PHASE3_ARCHITECTURE_CONTRACT_ID
    ):
        raise Ia32BackendError(
            "the frozen Phase-3 IA-32 architecture contract changed without a version bump"
        )
    return identity


def ia32_host_abi_contract() -> dict[str, Any]:
    """Describe the Phase-4 registered service and native-broker boundary."""

    return {
        "format": HOST_ABI_CONTRACT_FORMAT,
        "version": HOST_ABI_CONTRACT_VERSION,
        "generator_version": HOST_ABI_GENERATOR_VERSION,
        "architecture_contract_id": PHASE3_ARCHITECTURE_CONTRACT_ID,
        "exchange": {
            "version": HOST_ABI_EXCHANGE_VERSION,
            "trace_capacity": HOST_SERVICE_TRACE_CAPACITY,
            "trace_record_words": HOST_SERVICE_TRACE_RECORD_WORDS,
            "overflow_policy": "fail-closed",
        },
        "calling_conventions": {
            "supported": ["cdecl", "stdcall"],
            "maximum_native32_arguments": 5,
            "maximum_native64_broker_arguments": 4,
            "stack_cleanup": "descriptor-exact",
            "preserved_registers": ["ebx", "esi", "edi", "ebp", "ecx", "edx"],
            "preserved_eflags": True,
            "return_register": "eax",
        },
        "service_kinds": {
            "return_constant": NATIVE_HOST_SERVICE_RETURN_CONSTANT,
            "write_u32": NATIVE_HOST_SERVICE_WRITE_U32,
            "callback": NATIVE_HOST_SERVICE_CALLBACK,
            "workload_abi": NATIVE_HOST_SERVICE_WORKLOAD_ABI,
        },
        "workload_abi": {
            "operation_fields": ["runtime_kind", "runtime_value"],
            "native32_services": [
                "system-time",
                "irql",
                "semaphore",
                "delay-thread",
                "memory-allocation",
                "title-stream-status-read-seek",
            ],
            "supported_target_count": len(SUPPORTED_WORKLOAD_SERVICE_SPECS),
            "this_pointer": "preserved-ecx-explicit-dispatch-argument",
        },
        "execution_owners": {
            "native32": HOST_SERVICE_EXECUTION_NATIVE32,
            "native64-broker": HOST_SERVICE_EXECUTION_NATIVE64_BROKER,
        },
        "boundaries": dict(sorted(HOST_SERVICE_BOUNDARIES.items())),
        "broker": {
            "magic": HOST_SERVICE_BROKER_MAGIC,
            "version": HOST_SERVICE_BROKER_VERSION,
            "transport": "named-shared-memory-and-native-events",
            "pointer_policy": "scalar-copy-no-cross-process-guest-pointer-dereference",
            "lifecycle": ["ready", "request", "response", "stop", "error"],
        },
        "audit": [
            "service-order",
            "arguments",
            "return-value",
            "stack-entry-and-return",
            "memory-before-and-after",
            "callback-target-return-and-count",
            "boundary-and-execution-owner",
        ],
        "normal_runtime_python_callbacks": 0,
        "runtime_decoding": False,
        "runtime_compilation": False,
        "runtime_code_patching": False,
        "native_promotion": False,
        "raw_xbe_execution": False,
    }


def ia32_host_abi_contract_id() -> str:
    identity = _sha256(_canonical_json(ia32_host_abi_contract()))
    if HOST_ABI_CONTRACT_VERSION == 1 and identity != PHASE4_HOST_ABI_CONTRACT_ID:
        raise Ia32BackendError(
            "the frozen Phase-4 IA-32 host ABI contract changed without a version bump"
        )
    return identity


def ia32_resident_scheduler_contract() -> dict[str, Any]:
    """Describe the Phase-5 persistent lane and scheduler boundary."""

    return {
        "format": RESIDENT_SCHEDULER_CONTRACT_FORMAT,
        "version": RESIDENT_SCHEDULER_CONTRACT_VERSION,
        "generator_version": RESIDENT_SCHEDULER_GENERATOR_VERSION,
        "host_abi_contract_id": PHASE4_HOST_ABI_CONTRACT_ID,
        "exchange": {
            "version": RESIDENT_SCHEDULER_EXCHANGE_VERSION,
            "lane_record_size": RESIDENT_SCHEDULER_LANE_RECORD_SIZE,
            "trace_capacity": RESIDENT_SCHEDULER_TRACE_CAPACITY,
            "trace_record_words": RESIDENT_SCHEDULER_TRACE_RECORD_WORDS,
            "overflow_policy": "fail-closed",
        },
        "lanes": list(RESIDENT_SCHEDULER_LANES),
        "ordering": "primary-safe-point-then-vblank-then-runnable-worker",
        "contexts": {
            "state": "persistent-gpr-eflags-and-complete-fxsave-per-lane",
            "tls": "per-lane-page-swapped-through-static-fs-window-in-native32",
            "resume": "build-time-entry-map-at-classified-safe-points",
        },
        "synchronization": {
            "wait": "explicit-wait-object-parks-lane",
            "wake": "ready-transition-before-next-selection",
            "terminal_states": ["completed", "faulted"],
        },
        "callback_return": {
            "vblank_sentinel": IA32_VBLANK_RETURN_SENTINEL,
            "classification": "explicit-resident-scheduler-exit",
        },
        "fault_containment": {
            "boundary": "vectored-exception-to-current-lane-context",
            "identity": ["lane", "exception-code", "guest-eip", "access-address"],
            "other_lanes": "remain-resident-and-classified",
        },
        "products": {
            "completed_flips": "native-scheduler-count",
            "render_hash": "fnv1a32-declared-range",
            "audio_hash": "fnv1a32-declared-range",
        },
        "normal_runtime_python_callbacks": 0,
        "runtime_decoding": False,
        "runtime_compilation": False,
        "runtime_code_patching": False,
        "native_promotion": False,
        "raw_xbe_execution": False,
    }


def ia32_resident_scheduler_contract_id() -> str:
    identity = _sha256(_canonical_json(ia32_resident_scheduler_contract()))
    if (
        RESIDENT_SCHEDULER_CONTRACT_VERSION == 1
        and PHASE5_RESIDENT_SCHEDULER_CONTRACT_ID
        and identity != PHASE5_RESIDENT_SCHEDULER_CONTRACT_ID
    ):
        raise Ia32BackendError(
            "the frozen Phase-5 IA-32 resident scheduler contract changed without a version bump"
        )
    return identity


def ia32_coverage_growth_contract() -> dict[str, Any]:
    """Describe the Phase-6 measured vertical-slice promotion boundary."""

    return {
        "format": COVERAGE_GROWTH_CONTRACT_FORMAT,
        "version": COVERAGE_GROWTH_CONTRACT_VERSION,
        "generator_version": COVERAGE_GROWTH_GENERATOR_VERSION,
        "resident_scheduler_contract_id": PHASE5_RESIDENT_SCHEDULER_CONTRACT_ID,
        "profile": {
            "format": COVERAGE_PROFILE_FORMAT,
            "version": COVERAGE_PROFILE_VERSION,
            "required_metrics": [
                "estimated_native_time_ns",
                "guest_steps",
                "module_calls",
            ],
            "ranking": {
                "profile_heat": ("estimated_native_time_ns-plus-guest_steps-plus-module_calls"),
                "priority_score": "profile-heat-plus-unique-outgoing-frontier",
                "tie_break": "lowest-scc-entry-address",
            },
            "explicit_boundaries": ["service", "render", "scheduler"],
        },
        "slice_order": list(COVERAGE_GROWTH_SLICE_ORDER),
        "first_slice": {
            "name": "lesson-one-hot-scc",
            "required_exits": ["service", "render"],
        },
        "discovery": {
            "allowed_mode": "diagnostic-discovery",
            "unknown_target_result": "required-next-decoded-store-target",
            "promotion_eligible": False,
        },
        "normal_validation": {
            "mode": "normal-validation",
            "unknown_targets": 0,
            "unresolved_static_rewrite_sites": 0,
            "frontier_interpreter_invocations": 0,
            "frontier_interpreter_steps": 0,
            "python_callbacks": 0,
            "runtime_decoding": False,
            "runtime_compilation": False,
            "runtime_code_patching": False,
            "native_promotion": False,
            "raw_xbe_execution": False,
        },
        "required_output": "coverage-growth-map.json",
        "final_manifest": "closed-decoded-coverage-and-rewrite-identities",
        "guest_mapping": "detached-fixed-va-verified-active-spans",
    }


def ia32_coverage_growth_contract_id() -> str:
    identity = _sha256(_canonical_json(ia32_coverage_growth_contract()))
    if (
        COVERAGE_GROWTH_CONTRACT_VERSION == 1
        and PHASE6_COVERAGE_GROWTH_CONTRACT_ID
        and identity != PHASE6_COVERAGE_GROWTH_CONTRACT_ID
    ):
        raise Ia32BackendError(
            "the frozen Phase-6 IA-32 coverage-growth contract changed without a version bump"
        )
    return identity


def ia32_launcher_cutover_contract() -> dict[str, Any]:
    """Describe the Phase-7 normal-launch and persistent-memory boundary."""

    return {
        "format": CUTOVER_CONTRACT_FORMAT,
        "version": CUTOVER_CONTRACT_VERSION,
        "generator_version": CUTOVER_GENERATOR_VERSION,
        "coverage_growth_contract_id": PHASE6_COVERAGE_GROWTH_CONTRACT_ID,
        "launcher": {
            "default_backend": "same-isa-ia32",
            "diagnostic_oracle": "fusion-only-64-bit",
            "diagnostic_oracle_requires_explicit_selection": True,
            "missing_or_ineligible_artifact": "preflight-failure",
            "automatic_live_fallback": False,
        },
        "memory": {
            "owner_after_initialize": "resident-native32-worker",
            "initialization": "one-full-page-seed-before-ready",
            "command_input": "control-plus-explicit-host-page-publications",
            "command_output": "native-dirty-pages-only",
            "page_state_values": {
                "clean": CUTOVER_PAGE_CLEAN,
                "native-dirty": CUTOVER_PAGE_NATIVE_DIRTY,
                "host-publication": CUTOVER_PAGE_HOST_PUBLICATION,
            },
            "guest_pages_remain_resident_between_commands": True,
            "renderer_audio_publication_uses_build_time_ownership": True,
        },
        "normal_validation": {
            "repeat_warm_dispatches": 2,
            "cross_backend_exits": 0,
            "frontier_interpreter_invocations": 0,
            "frontier_interpreter_steps": 0,
            "python_callbacks": 0,
            "runtime_decoding": False,
            "runtime_compilation": False,
            "runtime_code_patching": False,
            "native_promotion": False,
            "raw_xbe_execution": False,
        },
        "required_output": "launcher-cutover-map.json",
    }


def ia32_launcher_cutover_contract_id() -> str:
    identity = _sha256(_canonical_json(ia32_launcher_cutover_contract()))
    if (
        CUTOVER_CONTRACT_VERSION == 1
        and PHASE7_CUTOVER_CONTRACT_ID
        and identity != PHASE7_CUTOVER_CONTRACT_ID
    ):
        raise Ia32BackendError(
            "the frozen Phase-7 IA-32 launcher-cutover contract changed without a version bump"
        )
    return identity


def ia32_normal_live_launch_contract() -> dict[str, Any]:
    """Describe the native process accepted by the ordinary live launcher."""

    return {
        "format": NORMAL_LIVE_LAUNCH_CONTRACT_FORMAT,
        "version": NORMAL_LIVE_LAUNCH_CONTRACT_VERSION,
        "launcher_cutover_contract_id": PHASE7_CUTOVER_CONTRACT_ID,
        "command_line_protocol_version": NORMAL_LIVE_LAUNCH_PROTOCOL_VERSION,
        "workload": {
            "entry_state": "verified-xbe-entry",
            "supported_path": "boot-frontend-lesson-one",
            "scheduler": "dynamic-native-resident",
            "fixed_replay_plan": False,
            "vblank_lane": {
                "owner": "native-control-watcher-thread",
                "frequency_hz": 60,
                "callback_address": NORMAL_LIVE_VBLANK_CALLBACK_ADDRESS,
                "callback_data_address": NORMAL_LIVE_VBLANK_DATA_ADDRESS,
                "stack_top": NORMAL_LIVE_VBLANK_STACK_TOP,
                "tls_base": NORMAL_LIVE_VBLANK_TLS_BASE,
                "return_sentinel": IA32_VBLANK_RETURN_SENTINEL,
                "runtime_target_policy": "exact-registered-decoded-callback",
            },
        },
        "live_host": {
            "transport_schema_version": NORMAL_LIVE_TRANSPORT_SCHEMA_VERSION,
            "control_manifest_publication": "native",
            "command_span_publication": "native",
            "resource_slot_publication": "native",
            "controller_consumption": "native",
            "audio_decode_mix_submission": "native",
            "filesystem_services": "native",
            "stop_request_consumption": "native",
        },
        "normal_validation": {
            "cross_backend_exits": 0,
            "frontier_interpreter_invocations": 0,
            "frontier_interpreter_steps": 0,
            "python_callbacks": 0,
            "runtime_decoding": False,
            "runtime_compilation": False,
            "runtime_code_patching": False,
            "native_promotion": False,
            "raw_xbe_execution": False,
        },
    }


def ia32_normal_live_launch_contract_id() -> str:
    return _sha256(_canonical_json(ia32_normal_live_launch_contract()))


def _launcher_cutover_map(plan: Ia32CoverageGrowthPlan) -> dict[str, Any]:
    coverage_map = plan.coverage_growth_map
    if coverage_map.get("final_cutover_candidate") is not True:
        raise Ia32BackendError("Phase-7 cutover requires a final closed Phase-6 coverage map")
    return {
        "format": "b2-recomp-ia32-launcher-cutover-map",
        "version": 1,
        "contract_id": ia32_launcher_cutover_contract_id(),
        "coverage_profile_id": coverage_map["profile_id"],
        "coverage_growth_map_id": _sha256(_canonical_json(coverage_map)),
        "default_backend": "same-isa-ia32",
        "diagnostic_oracle": "fusion-only-64-bit",
        "automatic_cross_backend_fallback": False,
        "memory_owner": "resident-native32-worker",
        "initial_page_seed": "once-before-ready",
        "command_input": "control-and-explicit-host-publications-only",
        "command_output": "native-dirty-pages-only",
    }


def _coverage_nonnegative_int(value: object, *, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise Ia32BackendError(f"Phase-6 {context} must be a nonnegative integer")
    return value


def _coverage_address(value: object, *, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 0xFFFFFFFF:
        raise Ia32BackendError(f"Phase-6 {context} must be a 32-bit integer address")
    return value


def _normalized_coverage_profile(profile: Ia32CoverageProfile) -> dict[str, Any]:
    if not profile.workload.strip():
        raise Ia32BackendError("Phase-6 coverage profile requires a workload name")
    if profile.mode not in {"diagnostic-discovery", "normal-validation"}:
        raise Ia32BackendError(
            "Phase-6 coverage profile mode must be diagnostic-discovery or normal-validation"
        )
    if not profile.targets:
        raise Ia32BackendError("Phase-6 coverage profile contains no executable targets")
    targets: list[dict[str, Any]] = []
    seen_targets: set[int] = set()
    for index, value in enumerate(profile.targets):
        if not isinstance(value, Mapping):
            raise Ia32BackendError(f"Phase-6 target {index} is not a record")
        address = _coverage_address(value.get("address"), context=f"target {index} address")
        if address in seen_targets:
            raise Ia32BackendError(f"Phase-6 target 0x{address:08X} is duplicated")
        seen_targets.add(address)
        slice_name = value.get("slice")
        if slice_name not in COVERAGE_GROWTH_SLICE_ORDER:
            raise Ia32BackendError(f"Phase-6 target 0x{address:08X} uses an unknown vertical slice")
        lane = value.get("lane", "primary")
        if lane not in {*RESIDENT_SCHEDULER_LANES, "shared"}:
            raise Ia32BackendError(f"Phase-6 target 0x{address:08X} uses an unknown scheduler lane")
        targets.append(
            {
                "address": address,
                "address_hex": f"0x{address:08X}",
                "slice": slice_name,
                "lane": lane,
                "role": str(value.get("role", "guest")),
                "estimated_native_time_ns": _coverage_nonnegative_int(
                    value.get("estimated_native_time_ns", 0),
                    context=f"target 0x{address:08X} estimated native time",
                ),
                "guest_steps": _coverage_nonnegative_int(
                    value.get("guest_steps", 0),
                    context=f"target 0x{address:08X} guest steps",
                ),
                "module_calls": _coverage_nonnegative_int(
                    value.get("module_calls", 0),
                    context=f"target 0x{address:08X} module calls",
                ),
            }
        )
    transitions: list[dict[str, Any]] = []
    for index, value in enumerate(profile.transitions):
        if not isinstance(value, Mapping):
            raise Ia32BackendError(f"Phase-6 transition {index} is not a record")
        source = _coverage_address(value.get("source"), context=f"transition {index} source")
        target = _coverage_address(value.get("target"), context=f"transition {index} target")
        if source not in seen_targets:
            raise Ia32BackendError(
                f"Phase-6 transition source 0x{source:08X} has no target profile"
            )
        transitions.append(
            {
                "source": source,
                "source_hex": f"0x{source:08X}",
                "target": target,
                "target_hex": f"0x{target:08X}",
                "count": _coverage_nonnegative_int(
                    value.get("count", 0), context=f"transition {index} count"
                ),
            }
        )
    boundary_exits: list[dict[str, Any]] = []
    for index, value in enumerate(profile.boundary_exits):
        if not isinstance(value, Mapping):
            raise Ia32BackendError(f"Phase-6 boundary exit {index} is not a record")
        source = _coverage_address(value.get("source"), context=f"boundary exit {index} source")
        if source not in seen_targets:
            raise Ia32BackendError(f"Phase-6 boundary source 0x{source:08X} has no target profile")
        boundary = value.get("boundary")
        if boundary not in {*HOST_SERVICE_BOUNDARIES, "service", "scheduler"}:
            raise Ia32BackendError(
                f"Phase-6 boundary exit {index} uses unknown boundary {boundary!r}"
            )
        record: dict[str, Any] = {
            "source": source,
            "source_hex": f"0x{source:08X}",
            "boundary": boundary,
            "count": _coverage_nonnegative_int(
                value.get("count", 0), context=f"boundary exit {index} count"
            ),
        }
        if "target" in value:
            target = _coverage_address(value.get("target"), context=f"boundary exit {index} target")
            record.update({"target": target, "target_hex": f"0x{target:08X}"})
        boundary_exits.append(record)
    return {
        "format": COVERAGE_PROFILE_FORMAT,
        "version": COVERAGE_PROFILE_VERSION,
        "workload": profile.workload,
        "mode": profile.mode,
        "targets": sorted(targets, key=lambda item: int(item["address"])),
        "transitions": sorted(
            transitions, key=lambda item: (int(item["source"]), int(item["target"]))
        ),
        "boundary_exits": sorted(
            boundary_exits,
            key=lambda item: (
                int(item["source"]),
                str(item["boundary"]),
                int(item.get("target", 0)),
            ),
        ),
        "frontier_interpreter_invocations": _coverage_nonnegative_int(
            profile.frontier_interpreter_invocations,
            context="frontier interpreter invocation count",
        ),
        "frontier_interpreter_steps": _coverage_nonnegative_int(
            profile.frontier_interpreter_steps,
            context="frontier interpreter step count",
        ),
    }


def _phase6_profile_scheduler_boundaries(
    profile: Ia32CoverageProfile,
) -> Ia32CoverageProfile:
    """Classify the accepted vblank callback sentinel as a scheduler exit."""

    transitions = []
    boundary_exits = list(profile.boundary_exits)
    for record in profile.transitions:
        if int(record["target"]) != IA32_VBLANK_RETURN_SENTINEL:
            transitions.append(record)
            continue
        boundary_exits.append(
            {
                "source": int(record["source"]),
                "target": IA32_VBLANK_RETURN_SENTINEL,
                "boundary": "scheduler",
                "count": int(record.get("count", 0)),
            }
        )
    normalized = Ia32CoverageProfile(
        workload=profile.workload,
        mode=profile.mode,
        targets=profile.targets,
        transitions=tuple(transitions),
        boundary_exits=tuple(boundary_exits),
        frontier_interpreter_invocations=profile.frontier_interpreter_invocations,
        frontier_interpreter_steps=profile.frontier_interpreter_steps,
    )
    _normalized_coverage_profile(normalized)
    return normalized


def load_ia32_coverage_profile(path: Path) -> Ia32CoverageProfile:
    """Load and validate a Phase-6 measured coverage profile."""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise Ia32BackendError(f"could not load Phase-6 coverage profile: {path}") from exc
    if (
        not isinstance(value, dict)
        or value.get("format") != COVERAGE_PROFILE_FORMAT
        or value.get("version") != COVERAGE_PROFILE_VERSION
    ):
        raise Ia32BackendError("unsupported Phase-6 coverage profile")
    targets = value.get("targets")
    transitions = value.get("transitions", [])
    boundary_exits = value.get("boundary_exits", [])
    if (
        not isinstance(targets, list)
        or not isinstance(transitions, list)
        or not isinstance(boundary_exits, list)
    ):
        raise Ia32BackendError("Phase-6 coverage profile arrays are malformed")
    profile = Ia32CoverageProfile(
        workload=str(value.get("workload", "")),
        mode=str(value.get("mode", "")),
        targets=tuple(targets),
        transitions=tuple(transitions),
        boundary_exits=tuple(boundary_exits),
        frontier_interpreter_invocations=value.get("frontier_interpreter_invocations", 0),
        frontier_interpreter_steps=value.get("frontier_interpreter_steps", 0),
    )
    _normalized_coverage_profile(profile)
    return profile


def coverage_profile_from_performance_debug_report(
    path: Path,
    *,
    mode: str = "normal-validation",
) -> Ia32CoverageProfile:
    """Convert the upgraded performance-debug report into a Phase-6 profile."""

    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise Ia32BackendError(f"could not load performance-debug report: {path}") from exc
    if not isinstance(report, dict):
        raise Ia32BackendError("performance-debug report root is not an object")
    analysis = report.get("hot_path_analysis")
    profiling = report.get("profiling")
    if not isinstance(analysis, dict) or not isinstance(profiling, dict):
        raise Ia32BackendError(
            "performance-debug report omits hot-path analysis or profiling state"
        )
    static_runtime = profiling.get("static_runtime")
    if not isinstance(static_runtime, Mapping):
        static_runtime = {}
    if mode == "normal-validation" and analysis.get("status") != "complete":
        raise Ia32BackendError(
            "Phase-6 normal profile conversion requires complete exact transition data"
        )
    if mode == "normal-validation" and (
        profiling.get("accepted") is not True
        or static_runtime.get("clean") is not True
        or static_runtime.get("developer_live_compilation_enabled") is not False
        or static_runtime.get("native_promotion_enabled") is not False
    ):
        raise Ia32BackendError("Phase-6 normal profile conversion requires a static-clean capture")
    raw_targets = analysis.get("targets")
    transition_root = analysis.get("transitions")
    raw_edges = transition_root.get("edges") if isinstance(transition_root, dict) else None
    cycles_root = analysis.get("cycles")
    hot_cycles = cycles_root.get("hot_cycles") if isinstance(cycles_root, dict) else None
    if not isinstance(raw_targets, list) or not raw_targets:
        raise Ia32BackendError("performance-debug report has no measured native targets")
    if not isinstance(raw_edges, list) or not isinstance(hot_cycles, list) or not hot_cycles:
        raise Ia32BackendError("performance-debug report has no transition graph or hot SCC")
    hottest_cycle = hot_cycles[0]
    cycle_targets = hottest_cycle.get("targets") if isinstance(hottest_cycle, dict) else None
    if not isinstance(cycle_targets, list) or len(cycle_targets) < 2:
        raise Ia32BackendError("Phase-6 Lesson One selection requires a multi-target hot SCC")
    try:
        lesson_targets = {
            int(value, 0) if isinstance(value, str) else int(value) for value in cycle_targets
        }
    except (TypeError, ValueError) as exc:
        raise Ia32BackendError("performance-debug hot SCC target list is malformed") from exc
    target_by_address: dict[int, Mapping[str, Any]] = {}
    for record in raw_targets:
        if not isinstance(record, Mapping):
            continue
        address = _coverage_address(record.get("target"), context="debug target")
        target_by_address[address] = record
    if not lesson_targets.issubset(target_by_address):
        raise Ia32BackendError("performance-debug hot SCC refers to an unmeasured target")
    service_by_target = {
        int(record["target"]): record
        for record in analysis.get("native_host_services", [])
        if isinstance(record, Mapping) and isinstance(record.get("target"), int)
    }
    transitions: list[dict[str, Any]] = []
    reverse_edges: dict[int, set[int]] = {}
    boundary_exits: list[dict[str, Any]] = []
    for record in raw_edges:
        if not isinstance(record, Mapping):
            continue
        source = record.get("entry_target")
        target = record.get("exit_target")
        count = record.get("module_calls", 0)
        if (
            not isinstance(source, int)
            or source not in target_by_address
            or not isinstance(target, int)
        ):
            continue
        if target in target_by_address:
            transitions.append({"source": source, "target": target, "count": int(count)})
            reverse_edges.setdefault(target, set()).add(source)
            continue
        if target == IA32_VBLANK_RETURN_SENTINEL:
            boundary_exits.append(
                {
                    "source": source,
                    "target": target,
                    "boundary": "scheduler",
                    "count": int(count),
                }
            )
            continue
        service = service_by_target.get(target)
        if service is None:
            transitions.append({"source": source, "target": target, "count": int(count)})
            continue
        boundary_exits.append(
            {
                "source": source,
                "target": target,
                "boundary": "service",
                "count": int(count),
            }
        )
        if service.get("boundary") == "render":
            boundary_exits.append(
                {
                    "source": source,
                    "target": target,
                    "boundary": "render",
                    "count": int(count),
                }
            )
    render_exit_sources = {
        int(record["source"]) for record in boundary_exits if record["boundary"] == "render"
    }
    for source, record in target_by_address.items():
        exit_reasons = record.get("module_exit_reasons")
        if not isinstance(exit_reasons, Mapping):
            continue
        yield_count = exit_reasons.get("yield", 0)
        if isinstance(yield_count, int) and yield_count > 0 and source not in render_exit_sources:
            boundary_exits.append(
                {
                    "source": source,
                    "boundary": "render",
                    "count": yield_count,
                }
            )
    reaches_lesson = set(lesson_targets)
    pending = list(lesson_targets)
    while pending:
        target = pending.pop()
        for source in reverse_edges.get(target, set()):
            if source not in reaches_lesson:
                reaches_lesson.add(source)
                pending.append(source)
    targets: list[dict[str, Any]] = []
    for address, record in sorted(target_by_address.items()):
        lane = record.get("dominant_execution_lane") or "primary"
        if lane not in RESIDENT_SCHEDULER_LANES:
            lane = "primary"
        if address in lesson_targets:
            slice_name = COVERAGE_GROWTH_SLICE_ORDER[0]
        elif address in reaches_lesson and lane == "primary":
            slice_name = COVERAGE_GROWTH_SLICE_ORDER[1]
        else:
            slice_name = COVERAGE_GROWTH_SLICE_ORDER[2]
        estimated_us = record.get("estimated_native_time_us", 0)
        if isinstance(estimated_us, bool) or not isinstance(estimated_us, (int, float)):
            estimated_us = 0
        targets.append(
            {
                "address": address,
                "slice": slice_name,
                "lane": lane,
                "role": "profiled-native-dispatch-target",
                "estimated_native_time_ns": max(0, round(float(estimated_us) * 1000)),
                "guest_steps": max(0, int(record.get("guest_steps", 0))),
                "module_calls": max(0, int(record.get("module_calls", 0))),
            }
        )
    profile = Ia32CoverageProfile(
        workload=str(report.get("workload") or path.stem),
        mode=mode,
        targets=tuple(targets),
        transitions=tuple(transitions),
        boundary_exits=tuple(boundary_exits),
        frontier_interpreter_invocations=int(
            static_runtime.get("frontier_interpreter_invocation_count", 0)
        ),
        frontier_interpreter_steps=int(static_runtime.get("frontier_interpreter_steps", 0)),
    )
    _normalized_coverage_profile(profile)
    return profile


def _coverage_sccs(nodes: set[int], edges: Mapping[int, set[int]]) -> list[tuple[int, ...]]:
    index = 0
    indexes: dict[int, int] = {}
    lowlinks: dict[int, int] = {}
    stack: list[int] = []
    active: set[int] = set()
    components: list[tuple[int, ...]] = []

    def visit(node: int) -> None:
        nonlocal index
        indexes[node] = index
        lowlinks[node] = index
        index += 1
        stack.append(node)
        active.add(node)
        for target in sorted(edges.get(node, set())):
            if target not in nodes:
                continue
            if target not in indexes:
                visit(target)
                lowlinks[node] = min(lowlinks[node], lowlinks[target])
            elif target in active:
                lowlinks[node] = min(lowlinks[node], indexes[target])
        if lowlinks[node] != indexes[node]:
            return
        component: list[int] = []
        while stack:
            target = stack.pop()
            active.remove(target)
            component.append(target)
            if target == node:
                break
        components.append(tuple(sorted(component)))

    for node in sorted(nodes):
        if node not in indexes:
            visit(node)
    return components


def plan_ia32_coverage_growth(
    decoded_store_plan: Ia32DecodedStorePlan,
    profile: Ia32CoverageProfile,
    *,
    registered_service_targets: Iterable[int] = (),
    implemented_rewrite_addresses: Iterable[int] = (),
) -> Ia32CoverageGrowthPlan:
    """Rank profile SCCs and enforce the Phase-6 static promotion gate."""

    profile_record = _normalized_coverage_profile(profile)
    profile_id = _sha256(_canonical_json(profile_record))
    decoded_addresses = {
        instruction.address
        for function in decoded_store_plan.functions
        for instruction in function.instructions
    }
    service_targets = {_u32(value) for value in registered_service_targets}
    observed_targets = {int(record["address"]) for record in profile_record["targets"]}
    observed_targets.update(int(record["target"]) for record in profile_record["transitions"])
    observed_targets.update(
        int(record["target"]) for record in profile_record["boundary_exits"] if "target" in record
    )
    scheduler_boundary_targets = {
        int(record["target"])
        for record in profile_record["boundary_exits"]
        if record["boundary"] == "scheduler" and "target" in record
    }
    unknown_targets = tuple(
        sorted(observed_targets - decoded_addresses - service_targets - scheduler_boundary_targets)
    )
    implemented = {_u32(value) for value in implemented_rewrite_addresses}
    target_records = {int(record["address"]): record for record in profile_record["targets"]}
    instruction_owner: dict[int, int] = {}
    active_function_instructions: set[int] = set()
    for function in decoded_store_plan.functions:
        function_addresses = {instruction.address for instruction in function.instructions}
        profiled_entries = sorted(function_addresses & set(target_records))
        if not profiled_entries:
            continue
        owner = (
            function.base_address
            if function.base_address in profiled_entries
            else profiled_entries[0]
        )
        active_function_instructions.update(function_addresses)
        instruction_owner.update({address: owner for address in function_addresses})
    unresolved_rewrites = tuple(
        sorted(
            int(record["address"])
            for record in decoded_store_plan.coverage_map["unsupported_sites"]
            if int(record["address"]) in active_function_instructions
            and int(record["address"]) not in implemented
        )
    )
    static_outgoing: dict[int, set[int]] = {address: set() for address in target_records}
    for record in decoded_store_plan.direct_edge_relocations.get("edges", []):
        if not isinstance(record, Mapping):
            continue
        owner = instruction_owner.get(int(record.get("source", -1)))
        target = record.get("target")
        if owner is not None and isinstance(target, int):
            static_outgoing[owner].add(target)
    edges: dict[int, set[int]] = {address: set() for address in target_records}
    for record in profile_record["transitions"]:
        source = int(record["source"])
        target = int(record["target"])
        if target in target_records:
            edges[source].add(target)
    components = _coverage_sccs(set(target_records), edges)
    rankings: list[dict[str, Any]] = []
    for component in components:
        component_set = set(component)
        profile_heat = sum(
            int(target_records[address]["estimated_native_time_ns"])
            + int(target_records[address]["guest_steps"])
            + int(target_records[address]["module_calls"])
            for address in component
        )
        outgoing = {
            int(record["target"])
            for record in profile_record["transitions"]
            if int(record["source"]) in component_set and int(record["target"]) not in component_set
        }
        for address in component:
            outgoing.update(static_outgoing.get(address, set()) - component_set)
        outgoing.update(
            -index - 1
            for index, record in enumerate(profile_record["boundary_exits"])
            if int(record["source"]) in component_set
        )
        component_slices = sorted({str(target_records[address]["slice"]) for address in component})
        rankings.append(
            {
                "scc_entry": component[0],
                "scc_entry_hex": f"0x{component[0]:08X}",
                "targets": list(component),
                "target_hex": [f"0x{address:08X}" for address in component],
                "slices": component_slices,
                "profile_heat": profile_heat,
                "outgoing_frontier": len(outgoing),
                "priority_score": profile_heat + len(outgoing),
            }
        )
    rankings.sort(key=lambda record: (-int(record["priority_score"]), int(record["scc_entry"])))
    for rank, record in enumerate(rankings, 1):
        record["rank"] = rank
    lesson_targets = {
        address
        for address, record in target_records.items()
        if record["slice"] == COVERAGE_GROWTH_SLICE_ORDER[0]
    }
    required_lesson_boundaries = {
        str(record["boundary"])
        for record in profile_record["boundary_exits"]
        if int(record["source"]) in lesson_targets
    }
    lesson_scc_rank = next(
        (
            int(record["rank"])
            for record in rankings
            if len(set(record["targets"]) & lesson_targets) >= 2
        ),
        0,
    )
    counters_zero = (
        int(profile_record["frontier_interpreter_invocations"]) == 0
        and int(profile_record["frontier_interpreter_steps"]) == 0
    )
    closed = not unknown_targets and not unresolved_rewrites and counters_zero
    slices: list[dict[str, Any]] = []
    cumulative: set[int] = set()
    for index, name in enumerate(COVERAGE_GROWTH_SLICE_ORDER):
        selected = {
            address for address, record in target_records.items() if record["slice"] == name
        }
        if not selected:
            raise Ia32BackendError(f"Phase-6 coverage profile omits vertical slice {name}")
        cumulative.update(selected)
        exits = [
            record
            for record in profile_record["boundary_exits"]
            if int(record["source"]) in cumulative
        ]
        stage_outgoing = sorted(
            {
                int(record["target"])
                for record in profile_record["transitions"]
                if int(record["source"]) in cumulative and int(record["target"]) not in cumulative
            }
            | {
                target
                for address in cumulative
                for target in static_outgoing.get(address, set())
                if target not in cumulative
            }
        )
        slices.append(
            {
                "sequence": index,
                "name": name,
                "added_targets": sorted(selected),
                "cumulative_targets": sorted(cumulative),
                "lanes": sorted({str(target_records[address]["lane"]) for address in cumulative}),
                "boundary_exits": exits,
                "outgoing_frontier_targets": stage_outgoing,
                "unknown_targets": list(unknown_targets),
                "unresolved_static_rewrite_sites": list(unresolved_rewrites),
                "frontier_interpreter_invocations": int(
                    profile_record["frontier_interpreter_invocations"]
                ),
                "frontier_interpreter_steps": int(profile_record["frontier_interpreter_steps"]),
                "closed": closed,
            }
        )
    lesson_requirements_met = (
        bool(lesson_targets)
        and lesson_scc_rank > 0
        and {"service", "render"}.issubset(required_lesson_boundaries)
    )
    if not lesson_requirements_met:
        raise Ia32BackendError(
            "Phase-6 Lesson One slice requires a multi-target SCC and explicit service/render exits"
        )
    mode = str(profile_record["mode"])
    if mode == "normal-validation" and unknown_targets:
        sample = ", ".join(f"0x{address:08X}" for address in unknown_targets[:8])
        raise Ia32BackendError(
            "Phase-6 normal validation found unknown executable targets: " + sample
        )
    if mode == "normal-validation" and unresolved_rewrites:
        sample = ", ".join(f"0x{address:08X}" for address in unresolved_rewrites[:8])
        raise Ia32BackendError(
            "Phase-6 normal validation found unresolved static rewrite sites: " + sample
        )
    if mode == "normal-validation" and not counters_zero:
        raise Ia32BackendError(
            "Phase-6 normal validation requires zero frontier-interpreter invocations and steps"
        )
    coverage_growth_map = {
        "format": "b2-recomp-ia32-coverage-growth-map",
        "version": 1,
        "contract_id": ia32_coverage_growth_contract_id(),
        "profile_id": profile_id,
        "profile": profile_record,
        "decoded_store": {
            "store_snapshot_id": decoded_store_plan.store_snapshot_id,
            "coverage_map_id": decoded_store_plan.inputs["coverage_map_id"],
            "rewrite_manifest_id": decoded_store_plan.inputs["rewrite_manifest_id"],
            "global_unsupported_site_count": len(
                decoded_store_plan.coverage_map["unsupported_sites"]
            ),
            "closure_scope": "profiled-supported-workload",
        },
        "ranking": rankings,
        "lesson_one_scc_rank": lesson_scc_rank,
        "slices": slices,
        "unknown_targets": list(unknown_targets),
        "unknown_target_hex": [f"0x{address:08X}" for address in unknown_targets],
        "required_next_decoded_store_targets": list(unknown_targets),
        "unresolved_static_rewrite_sites": list(unresolved_rewrites),
        "closed": closed,
        "promotion_eligible": mode == "normal-validation" and closed,
        "final_cutover_candidate": (
            mode == "normal-validation" and closed and cumulative == set(target_records)
        ),
        "normal_runtime_python_callbacks": 0,
        "runtime_decoding": False,
        "runtime_compilation": False,
        "runtime_code_patching": False,
        "native_promotion": False,
        "raw_xbe_execution": False,
    }
    return Ia32CoverageGrowthPlan(coverage_growth_map=coverage_growth_map)


def _scheduler_u32(record: Mapping[str, Any], key: str, *, context: str) -> int:
    value = record.get(key)
    if not isinstance(value, int):
        raise Ia32BackendError(f"Phase-5 {context} requires integer {key}")
    return _u32(value)


def _resident_scheduler_plan(
    capsule: ReplayCapsule,
    *,
    stop_eip: int,
) -> _Ia32SchedulerPlan:
    root = capsule.scheduler_state.get("resident_scheduler")
    if not isinstance(root, dict) or root.get("version") != 1:
        raise Ia32BackendError("Phase-5 capsule omits scheduler_state.resident_scheduler version 1")
    lane_values = root.get("lanes")
    step_values = root.get("steps")
    if not isinstance(lane_values, list) or len(lane_values) != RESIDENT_SCHEDULER_LANE_COUNT:
        raise Ia32BackendError("Phase-5 scheduler requires primary, worker, and vblank lanes")
    if (
        not isinstance(step_values, list)
        or not step_values
        or len(step_values) > RESIDENT_SCHEDULER_TRACE_CAPACITY
    ):
        raise Ia32BackendError("Phase-5 scheduler step count is empty or exceeds trace capacity")
    active_tls_base = _scheduler_u32(root, "active_tls_base", context="scheduler")
    if active_tls_base & (PAGE_SIZE - 1):
        raise Ia32BackendError("Phase-5 active TLS window must be page aligned")
    products = root.get("products")
    if not isinstance(products, dict):
        raise Ia32BackendError("Phase-5 scheduler omits render/audio product ranges")
    render = products.get("render")
    audio = products.get("audio")
    if not isinstance(render, dict) or not isinstance(audio, dict):
        raise Ia32BackendError("Phase-5 scheduler product ranges are malformed")
    render_address = _scheduler_u32(render, "address", context="render product")
    render_size = _scheduler_u32(render, "size", context="render product")
    audio_address = _scheduler_u32(audio, "address", context="audio product")
    audio_size = _scheduler_u32(audio, "size", context="audio product")
    if not 0 < render_size <= PAGE_SIZE or not 0 < audio_size <= PAGE_SIZE:
        raise Ia32BackendError("Phase-5 product ranges must contain 1..4096 bytes")
    if (render_address & (PAGE_SIZE - 1)) + render_size > PAGE_SIZE or (
        audio_address & (PAGE_SIZE - 1)
    ) + audio_size > PAGE_SIZE:
        raise Ia32BackendError("Phase-5 product ranges may not cross a page")

    state_names = {
        "ready": RESIDENT_LANE_READY,
        "waiting": RESIDENT_LANE_WAITING,
        "completed": RESIDENT_LANE_COMPLETED,
    }
    lanes: list[_Ia32SchedulerLane] = []
    lane_ids: dict[str, int] = {}
    required_pages = {
        active_tls_base,
        render_address & ~(PAGE_SIZE - 1),
        audio_address & ~(PAGE_SIZE - 1),
    }
    for lane_id, (expected_name, value) in enumerate(zip(RESIDENT_SCHEDULER_LANES, lane_values)):
        if not isinstance(value, dict) or value.get("name") != expected_name:
            raise Ia32BackendError(
                "Phase-5 lanes must be declared in primary, worker, vblank order"
            )
        lane_ids[expected_name] = lane_id
        initial_eip = _scheduler_u32(value, "initial_eip", context=expected_name)
        tls_base = _scheduler_u32(value, "tls_base", context=expected_name)
        if tls_base & (PAGE_SIZE - 1) or tls_base == active_tls_base:
            raise Ia32BackendError(
                f"Phase-5 {expected_name} TLS base must be a distinct aligned page"
            )
        initial_state_name = value.get("initial_state")
        if initial_state_name not in state_names:
            raise Ia32BackendError(
                f"Phase-5 {expected_name} initial state must be ready, waiting, or completed"
            )
        register_values = value.get("registers")
        if not isinstance(register_values, dict):
            raise Ia32BackendError(f"Phase-5 {expected_name} omits initial registers")
        registers = tuple(
            _u32(register_values.get(name, capsule.state.get_register(name)))
            for name in REGISTER_NAMES
        )
        eflags_value = value.get("eflags", _eflags_from_state(capsule.state))
        if not isinstance(eflags_value, int):
            raise Ia32BackendError(f"Phase-5 {expected_name} eflags must be an integer")
        wait_object = _u32(value.get("wait_object", 0))
        if initial_state_name == "waiting" and wait_object == 0:
            raise Ia32BackendError(f"Phase-5 waiting {expected_name} lane requires a wait object")
        required_pages.add(tls_base)
        required_pages.add(registers[REGISTER_NAMES.index("esp")] & ~(PAGE_SIZE - 1))
        lanes.append(
            _Ia32SchedulerLane(
                lane_id=lane_id,
                name=expected_name,
                initial_eip=initial_eip,
                tls_base=tls_base,
                initial_state=state_names[str(initial_state_name)],
                registers=registers,
                eflags=_u32(eflags_value),
                wait_object=wait_object,
            )
        )

    instructions = _instruction_map(capsule.functions)
    steps: list[_Ia32SchedulerStep] = []
    simulated_states = [lane.initial_state for lane in lanes]
    simulated_waits = [lane.wait_object for lane in lanes]
    expected_eips = [lane.initial_eip for lane in lanes]
    for sequence, value in enumerate(step_values):
        if not isinstance(value, dict) or value.get("sequence", sequence) != sequence:
            raise Ia32BackendError("Phase-5 scheduler steps require contiguous sequence numbers")
        lane_name = value.get("lane")
        if lane_name not in lane_ids:
            raise Ia32BackendError(f"Phase-5 step {sequence} names an unknown lane")
        lane_id = lane_ids[str(lane_name)]
        entry_eip = _scheduler_u32(value, "entry_eip", context=f"step {sequence}")
        next_eip = _scheduler_u32(value, "next_eip", context=f"step {sequence}")
        if entry_eip not in instructions:
            raise Ia32BackendError(
                f"Phase-5 step {sequence} entry 0x{entry_eip:08X} is not decoded"
            )
        if expected_eips[lane_id] != entry_eip:
            raise Ia32BackendError(
                f"Phase-5 step {sequence} does not resume {lane_name} at its declared EIP"
            )
        if simulated_states[lane_id] not in {RESIDENT_LANE_READY, RESIDENT_LANE_RUNNING}:
            raise Ia32BackendError(
                f"Phase-5 step {sequence} schedules non-runnable {lane_name} lane"
            )
        exit_name = value.get("exit")
        if exit_name not in RESIDENT_EXIT_KINDS:
            raise Ia32BackendError(f"Phase-5 step {sequence} has an unclassified exit")
        exit_kind = RESIDENT_EXIT_KINDS[str(exit_name)]
        wake_name = value.get("wake_lane")
        wake_lane_id = 0xFFFFFFFF
        if wake_name is not None:
            if wake_name not in lane_ids:
                raise Ia32BackendError(f"Phase-5 step {sequence} wakes an unknown lane")
            wake_lane_id = lane_ids[str(wake_name)]
            if simulated_states[wake_lane_id] != RESIDENT_LANE_WAITING:
                raise Ia32BackendError(f"Phase-5 step {sequence} wakes a lane that is not waiting")
            simulated_states[wake_lane_id] = RESIDENT_LANE_READY
            simulated_waits[wake_lane_id] = 0
        wait_object = _u32(value.get("wait_object", 0))
        if exit_kind == RESIDENT_EXIT_WAIT:
            if wait_object == 0:
                raise Ia32BackendError(f"Phase-5 step {sequence} wait omits its object")
            simulated_states[lane_id] = RESIDENT_LANE_WAITING
            simulated_waits[lane_id] = wait_object
        elif exit_kind == RESIDENT_EXIT_COMPLETE:
            simulated_states[lane_id] = RESIDENT_LANE_COMPLETED
            simulated_waits[lane_id] = 0
        else:
            simulated_states[lane_id] = RESIDENT_LANE_READY
        expected_eips[lane_id] = next_eip
        steps.append(
            _Ia32SchedulerStep(
                sequence=sequence,
                lane_id=lane_id,
                entry_eip=entry_eip,
                next_eip=next_eip,
                exit_kind=exit_kind,
                wake_lane_id=wake_lane_id,
                wait_object=wait_object,
            )
        )
    stranded = [
        lanes[index].name
        for index, state in enumerate(simulated_states)
        if state != RESIDENT_LANE_COMPLETED
    ]
    if stranded:
        raise Ia32BackendError("Phase-5 scheduler leaves stranded contexts: " + ", ".join(stranded))
    scheduler_map = {
        "format": "b2-recomp-ia32-resident-scheduler-map",
        "version": 1,
        "contract_id": ia32_resident_scheduler_contract_id(),
        "active_tls_base": active_tls_base,
        "lanes": [
            {
                "id": lane.lane_id,
                "name": lane.name,
                "initial_eip": lane.initial_eip,
                "tls_base": lane.tls_base,
                "initial_state": lane.initial_state,
                "registers": list(lane.registers),
                "eflags": lane.eflags,
                "wait_object": lane.wait_object,
            }
            for lane in lanes
        ],
        "steps": [
            {
                "sequence": step.sequence,
                "lane_id": step.lane_id,
                "entry_eip": step.entry_eip,
                "next_eip": step.next_eip,
                "exit_kind": step.exit_kind,
                "wake_lane_id": step.wake_lane_id,
                "wait_object": step.wait_object,
            }
            for step in steps
        ],
        "products": {
            "algorithm": "fnv1a32",
            "render": {"address": render_address, "size": render_size},
            "audio": {"address": audio_address, "size": audio_size},
        },
        "stop_eip": _u32(stop_eip),
        "final_states": list(simulated_states),
    }
    return _Ia32SchedulerPlan(
        lanes=tuple(lanes),
        steps=tuple(steps),
        active_tls_base=active_tls_base,
        render_address=render_address,
        render_size=render_size,
        audio_address=audio_address,
        audio_size=audio_size,
        required_pages=tuple(sorted(required_pages)),
        scheduler_map=scheduler_map,
    )


def _fnv1a32(payload: bytes) -> int:
    value = 2166136261
    for byte in payload:
        value = _u32((value ^ byte) * 16777619)
    return value


def execute_ia32_scheduler_reference(
    capsule: ReplayCapsule,
    *,
    stop_eip: int,
) -> Ia32SchedulerReferenceResult:
    """Run the accepted decoded scheduler semantics for a Phase-5 capsule."""

    plan = _resident_scheduler_plan(capsule, stop_eip=_u32(stop_eip))
    _unused_state, memory = clone_capsule_state(capsule)
    lane_states: list[CpuState] = []
    states = [lane.initial_state for lane in plan.lanes]
    wait_objects = [lane.wait_object for lane in plan.lanes]
    activations = [0 for _lane in plan.lanes]
    last_exits = [0 for _lane in plan.lanes]
    for lane in plan.lanes:
        state = cpu_state_from_record(cpu_state_record(capsule.state))
        for index, name in enumerate(REGISTER_NAMES):
            state.set_register(name, lane.registers[index])
        state.eip = lane.initial_eip
        state.flags = _flags_from_eflags(lane.eflags)
        state.fs_base = lane.tls_base
        lane_states.append(state)

    trace: list[dict[str, Any]] = []
    service_calls: list[dict[str, Any]] = []
    wakeups = 0
    completed_flips = 0
    state_names = {
        RESIDENT_LANE_READY: "ready",
        RESIDENT_LANE_RUNNING: "running",
        RESIDENT_LANE_WAITING: "waiting",
        RESIDENT_LANE_COMPLETED: "completed",
        RESIDENT_LANE_FAULTED: "faulted",
    }
    exit_names = {value: name for name, value in RESIDENT_EXIT_KINDS.items()}
    for step in plan.steps:
        lane = plan.lanes[step.lane_id]
        state = lane_states[step.lane_id]
        before_state = states[step.lane_id]
        before_tls = memory.read_u32(lane.tls_base)
        service_begin = len(service_calls)
        step_capsule = ReplayCapsule(
            path=capsule.path,
            manifest=capsule.manifest,
            state=state,
            memory=memory,
            functions=capsule.functions,
            scheduler_state=capsule.scheduler_state,
            service_state=capsule.service_state,
            events=capsule.events,
            resources=capsule.resources,
        )
        result = execute_ia32_slice_reference(step_capsule, stop_eip=stop_eip)
        state = result.state
        state.eip = step.next_eip
        state.fs_base = lane.tls_base
        lane_states[step.lane_id] = state
        memory = result.memory
        memory.write(
            plan.active_tls_base,
            memory.read(lane.tls_base, PAGE_SIZE),
        )
        for call in result.host_service_calls:
            service_calls.append({**call, "sequence": len(service_calls)})
        if step.exit_kind == RESIDENT_EXIT_WAIT:
            states[step.lane_id] = RESIDENT_LANE_WAITING
            wait_objects[step.lane_id] = step.wait_object
        elif step.exit_kind == RESIDENT_EXIT_COMPLETE:
            states[step.lane_id] = RESIDENT_LANE_COMPLETED
            wait_objects[step.lane_id] = 0
        else:
            states[step.lane_id] = RESIDENT_LANE_READY
            wait_objects[step.lane_id] = 0
            if step.exit_kind == RESIDENT_EXIT_FLIP:
                completed_flips += 1
        if step.wake_lane_id != 0xFFFFFFFF:
            states[step.wake_lane_id] = RESIDENT_LANE_READY
            wait_objects[step.wake_lane_id] = 0
            wakeups += 1
        activations[step.lane_id] += 1
        last_exits[step.lane_id] = step.exit_kind
        trace.append(
            {
                "sequence": step.sequence,
                "lane": lane.name,
                "entry_eip": step.entry_eip,
                "next_eip": step.next_eip,
                "exit": exit_names[step.exit_kind],
                "wake_lane": (
                    None if step.wake_lane_id == 0xFFFFFFFF else plan.lanes[step.wake_lane_id].name
                ),
                "state_before": state_names[before_state],
                "state_after": state_names[states[step.lane_id]],
                "service_begin": service_begin,
                "service_end": len(service_calls),
                "tls_base": lane.tls_base,
                "tls_before": before_tls,
                "tls_after": memory.read_u32(lane.tls_base),
                "render_hash": _fnv1a32(memory.read(plan.render_address, plan.render_size)),
                "audio_hash": _fnv1a32(memory.read(plan.audio_address, plan.audio_size)),
                "fault_code": 0,
            }
        )
    if any(state != RESIDENT_LANE_COMPLETED for state in states):
        raise Ia32BackendError("decoded Phase-5 scheduler left a stranded reference lane")
    lane_records = tuple(
        {
            "id": lane.lane_id,
            "name": lane.name,
            "state": state_names[states[lane.lane_id]],
            "tls_base": lane.tls_base,
            "activation_count": activations[lane.lane_id],
            "eip": lane_states[lane.lane_id].eip,
            "eflags": _eflags_from_state(lane_states[lane.lane_id]),
            "registers": {
                name: lane_states[lane.lane_id].get_register(name) for name in REGISTER_NAMES
            },
            "last_exit": exit_names.get(
                last_exits[lane.lane_id],
                f"unknown-{last_exits[lane.lane_id]}",
            ),
            "fault_code": 0,
            "wait_object": wait_objects[lane.lane_id],
            "fault_eip": 0,
            "fault_address": 0,
        }
        for lane in plan.lanes
    )
    primary_state = lane_states[0]
    return Ia32SchedulerReferenceResult(
        state=primary_state,
        memory=memory,
        lane_states=lane_records,
        scheduler_trace=tuple(trace),
        host_service_calls=tuple(service_calls),
        worker_wakeups=wakeups,
        completed_flips=completed_flips,
        render_hash=_fnv1a32(memory.read(plan.render_address, plan.render_size)),
        audio_hash=_fnv1a32(memory.read(plan.audio_address, plan.audio_size)),
    )


def _operand_from_decoded_store(record: Mapping[str, Any]) -> Operand:
    return Operand(
        kind=str(record["kind"]),
        size=int(record.get("size", 32)),
        reg=record.get("reg"),
        immediate=record.get("immediate"),
        base=record.get("base"),
        index=record.get("index"),
        scale=int(record.get("scale", 1)),
        displacement=int(record.get("displacement", 0)),
        absolute=record.get("absolute"),
        segment=record.get("segment"),
    )


def _instruction_from_decoded_store(
    record: Mapping[str, Any],
    *,
    payload: bytes,
) -> X86Instruction:
    return X86Instruction(
        address=int(record["address"]),
        size=int(record["size"]),
        mnemonic=str(record["mnemonic"]),
        operands=tuple(
            _operand_from_decoded_store(operand)
            for operand in record.get("operands", [])
            if isinstance(operand, dict)
        ),
        bytes_hex=payload.hex().upper(),
        target=(int(record["target"]) if record.get("target") is not None else None),
        condition=(str(record["condition"]) if record.get("condition") is not None else None),
        ret_stack_adjust=int(record.get("ret_stack_adjust", 0)),
    )


def _decoded_instruction_signature(instruction: X86Instruction) -> tuple[Any, ...]:
    return (
        instruction.address,
        instruction.size,
        instruction.mnemonic,
        instruction.operands,
        instruction.target,
        instruction.condition,
        instruction.ret_stack_adjust,
    )


def _decoded_store_sections(
    xbe_payload: bytes,
    info: Mapping[str, Any],
) -> tuple[dict[str, Any], ...]:
    sections = info.get("sections")
    if not isinstance(sections, list):
        raise Ia32BackendError("XBE metadata omits its section table")
    records = []
    for section in sections:
        if not isinstance(section, dict):
            raise Ia32BackendError("XBE section metadata is malformed")
        raw_address = int(section["raw_address"])
        raw_size = int(section["raw_size"])
        raw_payload = xbe_payload[raw_address : raw_address + raw_size]
        if len(raw_payload) != raw_size:
            raise Ia32BackendError(f"XBE section {section.get('name')} exceeds its source file")
        records.append(
            {
                "index": int(section["index"]),
                "name": str(section["name"]),
                "virtual_address": int(section["virtual_address"]),
                "virtual_size": int(section["virtual_size"]),
                "raw_address": raw_address,
                "raw_size": raw_size,
                "flags": int(section["flags"]),
                "flag_names": sorted(str(value) for value in section["flag_names"]),
                "raw_sha256": _sha256(raw_payload),
            }
        )
    return tuple(sorted(records, key=lambda item: int(item["index"])))


def _section_for_instruction(
    sections: Sequence[Mapping[str, Any]],
    address: int,
    size: int,
) -> Mapping[str, Any] | None:
    for section in sections:
        start = int(section["virtual_address"])
        end = start + int(section["raw_size"])
        if start <= address and address + size <= end:
            return section
    return None


def inspect_ia32_decoded_store(
    xbe_path: Path,
    decoded_block_store_path: Path,
    *,
    host_services: Mapping[int, _HostServiceThunk] | None = None,
    verified_indirect_targets: Mapping[int, Sequence[int]] | None = None,
) -> Ia32DecodedStorePlan:
    """Load a read-only store snapshot and bind every decoded byte to its XBE."""

    xbe_path = xbe_path.resolve()
    decoded_block_store_path = decoded_block_store_path.resolve()
    if not xbe_path.is_file():
        raise Ia32BackendError(f"XBE source is missing: {xbe_path}")
    if not decoded_block_store_path.is_file():
        raise Ia32BackendError(f"decoded block store is missing: {decoded_block_store_path}")
    xbe_payload = xbe_path.read_bytes()
    xbe_sha256 = _sha256(xbe_payload)
    try:
        loaded = load_xbe_file(
            xbe_path,
            resolve_imports=False,
            require_valid_digests=True,
        )
    except XbeLoaderError as strict_error:
        try:
            supported = verify_supported_xbe(xbe_path)
        except (OSError, ValueError, ProjectIdentityError):
            raise Ia32BackendError(
                f"could not load Phase-2 XBE source: {strict_error}"
            ) from strict_error
        if supported.get("supported") is not True:
            raise Ia32BackendError(
                f"could not load Phase-2 XBE source: {strict_error}"
            ) from strict_error
        try:
            loaded = load_xbe_file(
                xbe_path,
                resolve_imports=False,
                require_valid_digests=False,
            )
        except (OSError, ValueError, XbeLoaderError) as exc:
            raise Ia32BackendError(f"could not load Phase-2 XBE source: {exc}") from exc
    except (OSError, ValueError) as exc:
        raise Ia32BackendError(f"could not load Phase-2 XBE source: {exc}") from exc
    sections = _decoded_store_sections(xbe_payload, loaded.info)
    executable_sections = tuple(
        section for section in sections if "EXECUTABLE" in section["flag_names"]
    )
    if not executable_sections:
        raise Ia32BackendError("XBE has no file-backed executable section")

    try:
        connection = sqlite3.connect(
            f"file:{decoded_block_store_path.as_posix()}?mode=ro",
            uri=True,
        )
        try:
            version_row = connection.execute(
                "SELECT value FROM cache_metadata WHERE key='version'"
            ).fetchone()
            if version_row is None or int(version_row[0]) != DECODED_BLOCK_STORE_VERSION:
                observed = None if version_row is None else version_row[0]
                raise Ia32BackendError(
                    "unsupported decoded-block store version "
                    f"{observed}; expected {DECODED_BLOCK_STORE_VERSION}"
                )
            image_rows = connection.execute(
                "SELECT DISTINCT image_sha256 FROM decoded_blocks ORDER BY image_sha256"
            ).fetchall()
            available_images = tuple(str(row[0]).casefold() for row in image_rows)
            rows = connection.execute(
                """
                SELECT cache_key, target, entry_bytes, max_block_instructions, payload
                FROM decoded_blocks
                WHERE LOWER(image_sha256)=?
                ORDER BY target, cache_key
                """,
                (xbe_sha256.casefold(),),
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise Ia32BackendError(f"could not read decoded block store: {exc}") from exc
    if not rows:
        available = ", ".join(available_images[:4]) or "none"
        raise Ia32BackendError(
            "decoded block store has no records for XBE "
            f"{xbe_sha256}; available image identities: {available}"
        )

    snapshot_records = []
    functions_by_base: dict[int, LiftedFunction] = {}
    source_keys_by_base: dict[int, list[str]] = {}
    for cache_key, target, entry_bytes, max_block_instructions, compressed in rows:
        try:
            record = json.loads(zlib.decompress(compressed).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, zlib.error) as exc:
            raise Ia32BackendError(f"decoded store record {cache_key} is invalid: {exc}") from exc
        if not isinstance(record, dict):
            raise Ia32BackendError(f"decoded store record {cache_key} is not an object")
        record_payload = _canonical_json(record)
        snapshot_records.append(
            {
                "cache_key": str(cache_key),
                "target": int(target),
                "entry_bytes": int(entry_bytes),
                "max_block_instructions": int(max_block_instructions),
                "record_sha256": _sha256(record_payload),
            }
        )
        try:
            base_address = int(record["base_address"])
            code_size = int(record["code_size"])
            instruction_records = record["instructions"]
        except (KeyError, TypeError, ValueError) as exc:
            raise Ia32BackendError(f"decoded store record {cache_key} is malformed") from exc
        if (
            base_address != int(target)
            or code_size <= 0
            or not isinstance(instruction_records, list)
        ):
            raise Ia32BackendError(f"decoded store record {cache_key} has inconsistent range")
        function_instructions = []
        for instruction_record in instruction_records:
            if not isinstance(instruction_record, dict):
                raise Ia32BackendError(
                    f"decoded store record {cache_key} has malformed instruction metadata"
                )
            address = int(instruction_record["address"])
            size = int(instruction_record["size"])
            section = _section_for_instruction(executable_sections, address, size)
            if section is None:
                raise Ia32BackendError(
                    f"decoded instruction 0x{address:08X} is not backed by an executable "
                    "XBE section"
                )
            payload = loaded.arena.read(address, size)
            instruction = _instruction_from_decoded_store(
                instruction_record,
                payload=payload,
            )
            try:
                refreshed = lift_x86_function(
                    payload,
                    base_address=address,
                    symbol="phase2_byte_validation",
                    max_instructions=1,
                ).instructions[0]
            except (IndexError, ValueError) as exc:
                raise Ia32BackendError(
                    f"could not validate decoded instruction bytes at 0x{address:08X}"
                ) from exc
            if _decoded_instruction_signature(instruction) != _decoded_instruction_signature(
                refreshed
            ):
                raise Ia32BackendError(
                    f"decoded metadata does not match XBE bytes at 0x{address:08X}"
                )
            function_instructions.append(instruction)
        function = LiftedFunction(
            symbol=str(record["symbol"]),
            base_address=base_address,
            code_size=code_size,
            instructions=tuple(function_instructions),
            target_platform=str(record.get("target_platform", "windows")),
            generated_language=str(record.get("generated_language", "c++17")),
            renderer_backend=str(record.get("renderer_backend", "vulkan")),
        )
        previous = functions_by_base.get(base_address)
        if previous is not None and previous != function:
            raise Ia32BackendError(
                f"decoded store has conflicting records for 0x{base_address:08X}"
            )
        functions_by_base[base_address] = function
        source_keys_by_base.setdefault(base_address, []).append(str(cache_key))

    functions = tuple(functions_by_base[address] for address in sorted(functions_by_base))
    instructions = _instruction_map(functions)
    decoded_addresses = set(instructions)
    services = host_services or {}
    verified_targets = verified_indirect_targets or {}
    coverage_blocks = []
    for function in functions:
        coverage_blocks.append(
            {
                "base_address": function.base_address,
                "size": function.code_size,
                "symbol": function.symbol,
                "source_cache_keys": sorted(source_keys_by_base[function.base_address]),
                "instruction_count": function.instruction_count,
                "instruction_bytes_sha256": _sha256(
                    b"".join(
                        bytes.fromhex(instruction.bytes_hex)
                        for instruction in function.instructions
                    )
                ),
                "instructions": [
                    {
                        "address": instruction.address,
                        "size": instruction.size,
                        "bytes_sha256": _sha256(bytes.fromhex(instruction.bytes_hex)),
                    }
                    for instruction in function.instructions
                ],
            }
        )

    direct_edges = []
    indirect_sites = []
    rewrites = []
    unsupported_sites = []
    for instruction in (instructions[address] for address in sorted(instructions)):
        mnemonic = instruction.mnemonic.casefold()
        if mnemonic in {"call", "jmp", "jcc"} and instruction.target is not None:
            target = int(instruction.target)
            if target in decoded_addresses:
                target_kind = "decoded-block"
                status = "preserved-fixed-va"
            elif target in services:
                target_kind = "registered-host-service"
                status = "native-thunk"
                rewrites.append(
                    {
                        "address": instruction.address,
                        "kind": "host-service-thunk",
                        "target": target,
                        "shim_name": services[target].shim_name,
                        "status": "implemented",
                    }
                )
            else:
                target_kind = "coverage-gap"
                status = "unsupported"
                unsupported_sites.append(
                    {
                        "address": instruction.address,
                        "kind": "direct-target-coverage-gap",
                        "target": target,
                    }
                )
            direct_edges.append(
                {
                    "source": instruction.address,
                    "target": target,
                    "kind": mnemonic,
                    "target_kind": target_kind,
                    "relocation": status,
                    "source_bytes_sha256": _sha256(bytes.fromhex(instruction.bytes_hex)),
                }
            )
        if mnemonic in {"call_indirect", "jmp_indirect"} or (
            mnemonic in {"call", "jmp"} and instruction.target is None
        ):
            targets = tuple(
                sorted(int(target) for target in verified_targets.get(instruction.address, ()))
            )
            targets_valid = bool(targets) and all(
                target in decoded_addresses or target in services for target in targets
            )
            record = {
                "source": instruction.address,
                "kind": mnemonic,
                "targets": list(targets),
                "status": (
                    "verified-fixed-target-set" if targets_valid else "requires-verified-target-set"
                ),
            }
            indirect_sites.append(record)
            if not targets_valid:
                unsupported_sites.append(
                    {
                        "address": instruction.address,
                        "kind": "indirect-target-set-missing",
                    }
                )
        if mnemonic in REJECTED_MNEMONICS:
            rewrites.append(
                {
                    "address": instruction.address,
                    "kind": "privileged-instruction",
                    "mnemonic": instruction.mnemonic,
                    "status": "required-unimplemented",
                }
            )
            unsupported_sites.append(
                {"address": instruction.address, "kind": "privileged-rewrite-missing"}
            )
        for operand in instruction.operands:
            if operand.segment is not None:
                rewrites.append(
                    {
                        "address": instruction.address,
                        "kind": "tls-segment-access",
                        "segment": operand.segment,
                        "status": "required-unimplemented",
                    }
                )
                unsupported_sites.append(
                    {"address": instruction.address, "kind": "tls-rewrite-missing"}
                )
            if operand.absolute is not None and operand.absolute >= 0x80000000:
                rewrites.append(
                    {
                        "address": instruction.address,
                        "kind": "mmio-access",
                        "target": int(operand.absolute),
                        "status": "required-unimplemented",
                    }
                )
                unsupported_sites.append(
                    {"address": instruction.address, "kind": "mmio-rewrite-missing"}
                )

    snapshot_identity = {
        "format": "b2-recomp-decoded-block-store-snapshot",
        "version": 1,
        "store_schema_version": DECODED_BLOCK_STORE_VERSION,
        "xbe_sha256": xbe_sha256,
        "records": snapshot_records,
    }
    store_snapshot_id = _sha256(_canonical_json(snapshot_identity))
    section_map = {
        "format": "b2-recomp-ia32-section-map",
        "version": 1,
        "xbe_sha256": xbe_sha256,
        "sections": list(sections),
    }
    coverage_map = {
        "format": "b2-recomp-ia32-coverage-map",
        "version": 1,
        "store_snapshot_id": store_snapshot_id,
        "block_count": len(functions),
        "instruction_count": len(instructions),
        "decoded_byte_count": sum(instruction.size for instruction in instructions.values()),
        "complete": not unsupported_sites,
        "blocks": coverage_blocks,
        "unsupported_sites": unsupported_sites,
    }
    direct_edge_relocations = {
        "format": "b2-recomp-ia32-direct-edge-relocations",
        "version": 1,
        "policy": "preserve-original-fixed-va-encoding",
        "edges": direct_edges,
    }
    indirect_target_table = {
        "format": "b2-recomp-ia32-indirect-target-table",
        "version": 1,
        "sites": indirect_sites,
    }
    rewrite_manifest = {
        "format": "b2-recomp-ia32-rewrite-manifest",
        "version": 1,
        "rewrites": rewrites,
        "unsupported_sites": unsupported_sites,
    }
    inputs = {
        "contract_id": ia32_decoded_store_artifact_contract_id(),
        "xbe_sha256": xbe_sha256,
        "store_snapshot_id": store_snapshot_id,
        "store_schema_version": DECODED_BLOCK_STORE_VERSION,
        "section_map_id": _sha256(_canonical_json(section_map)),
        "coverage_map_id": _sha256(_canonical_json(coverage_map)),
        "direct_edge_relocations_id": _sha256(_canonical_json(direct_edge_relocations)),
        "indirect_target_table_id": _sha256(_canonical_json(indirect_target_table)),
        "rewrite_manifest_id": _sha256(_canonical_json(rewrite_manifest)),
    }
    return Ia32DecodedStorePlan(
        xbe_sha256=xbe_sha256,
        store_snapshot_id=store_snapshot_id,
        inputs=inputs,
        functions=functions,
        coverage_map=coverage_map,
        section_map=section_map,
        direct_edge_relocations=direct_edge_relocations,
        indirect_target_table=indirect_target_table,
        rewrite_manifest=rewrite_manifest,
    )


def _guest_assembly(
    code_spans: Sequence[tuple[int, bytes]],
    *,
    stop_eip: int,
    data_spans: Sequence[tuple[int, bytes]] = (),
) -> str:
    image: dict[int, int] = {}
    for address, payload in (*code_spans, *data_spans):
        for offset, value in enumerate(payload):
            byte_address = address + offset
            previous_value = image.get(byte_address)
            if previous_value is not None and previous_value != value:
                raise Ia32BackendError(f"guest code/data bytes conflict at 0x{byte_address:08X}")
            image[byte_address] = value
    for offset, value in enumerate(_stop_patch(stop_eip)):
        image[stop_eip + offset] = value
    if min(image) < GUEST_SECTION_ADDRESS:
        raise Ia32BackendError(f"guest PE cannot place code below 0x{GUEST_SECTION_ADDRESS:08X}")
    grouped: list[tuple[int, bytes]] = []
    span_start: int | None = None
    span = bytearray()
    previous: int | None = None
    for address, value in sorted(image.items()):
        if previous is None or address != previous + 1:
            if span_start is not None:
                grouped.append((span_start, bytes(span)))
            span_start = address
            span = bytearray()
        span.append(value)
        previous = address
    if span_start is not None:
        grouped.append((span_start, bytes(span)))
    lines = [
        "/* Deterministic verified IA-32 title slice. */",
        '.section .guest,"wx"',
        ".globl _b2r_ia32_guest_image",
        "_b2r_ia32_guest_image:",
    ]
    cursor = GUEST_SECTION_ADDRESS
    for address, payload in grouped:
        gap = address - cursor
        if gap < 0:
            raise Ia32BackendError("guest PE code spans overlap")
        if gap:
            lines.append(f".fill {gap},1,0xCC")
        lines.append(".byte " + _bytes_initializer(payload))
        cursor = address + len(payload)
    return "\n".join(lines) + "\n"


def _detached_guest_reservation_assembly() -> str:
    reservation_size = DETACHED_GUEST_RESERVATION_END - DETACHED_GUEST_RESERVATION_START
    return "\n".join(
        (
            "/* Deterministic low-title reservation; worker code follows in .text$z. */",
            '.section .text$a,"xr"',
            ".globl _b2r_ia32_guest_image",
            "_b2r_ia32_guest_image:",
            f".fill {reservation_size},1,0xCC",
            "",
        )
    )


def _detached_guest_code_spans(
    code_spans: Sequence[tuple[int, bytes]],
    *,
    stop_eip: int,
) -> tuple[tuple[int, bytes], ...]:
    image: dict[int, int] = {}
    for address, payload in code_spans:
        for offset, value in enumerate(payload):
            byte_address = address + offset
            previous = image.get(byte_address)
            if previous is not None and previous != value:
                raise Ia32BackendError(
                    f"detached guest code bytes conflict at 0x{byte_address:08X}"
                )
            image[byte_address] = value
    for offset, value in enumerate(_stop_patch(stop_eip)):
        image[stop_eip + offset] = value
    grouped: list[tuple[int, bytes]] = []
    start: int | None = None
    previous_address: int | None = None
    grouped_payload = bytearray()
    for address, value in sorted(image.items()):
        if previous_address is None or address != previous_address + 1:
            if start is not None:
                grouped.append((start, bytes(grouped_payload)))
            start = address
            grouped_payload = bytearray()
        grouped_payload.append(value)
        previous_address = address
    if start is not None:
        grouped.append((start, bytes(grouped_payload)))
    return tuple(grouped)


def _extend_guest_pe_virtual_size(path: Path, *, required_end: int) -> None:
    payload = bytearray(path.read_bytes())
    if len(payload) < 0x40 or payload[:2] != b"MZ":
        raise Ia32BackendError("rebuilt guest artifact is not a PE image")
    pe_offset = struct.unpack_from("<I", payload, 0x3C)[0]
    if payload[pe_offset : pe_offset + 4] != b"PE\0\0":
        raise Ia32BackendError("rebuilt guest artifact has no PE signature")
    section_count = struct.unpack_from("<H", payload, pe_offset + 6)[0]
    optional_size = struct.unpack_from("<H", payload, pe_offset + 20)[0]
    optional_offset = pe_offset + 24
    image_base = struct.unpack_from("<I", payload, optional_offset + 28)[0]
    section_alignment = struct.unpack_from("<I", payload, optional_offset + 32)[0]
    if image_base != HELPER_IMAGE_BASE or section_alignment == 0:
        raise Ia32BackendError("rebuilt guest PE has an unexpected image layout")
    section_offset = optional_offset + optional_size
    guest_header: int | None = None
    guest_rva = 0
    for index in range(section_count):
        current = section_offset + index * 40
        name = bytes(payload[current : current + 8]).rstrip(b"\0")
        if name == b".guest":
            guest_header = current
            guest_rva = struct.unpack_from("<I", payload, current + 12)[0]
            break
    if guest_header is None or guest_rva != GUEST_SECTION_RVA:
        raise Ia32BackendError("rebuilt IA-32 PE contains no .guest section")
    virtual_size = max(
        required_end - (HELPER_IMAGE_BASE + guest_rva),
        struct.unpack_from("<I", payload, guest_header + 16)[0],
    )
    if virtual_size <= 0:
        raise Ia32BackendError("rebuilt guest PE has an empty virtual address range")
    characteristics = struct.unpack_from("<I", payload, guest_header + 36)[0]
    struct.pack_into("<I", payload, guest_header + 8, virtual_size)
    struct.pack_into("<I", payload, guest_header + 36, characteristics | 0xE0000000)
    aligned_image_size = (guest_rva + virtual_size + section_alignment - 1) & ~(
        section_alignment - 1
    )
    struct.pack_into("<I", payload, optional_offset + 56, aligned_image_size)
    path.write_bytes(payload)


def _host_service_region_addresses(
    host_service_thunks: Sequence[_HostServiceThunk],
) -> tuple[int, ...]:
    return tuple(
        sorted(
            {
                service.target & ~(WINDOWS_ALLOCATION_GRANULARITY - 1)
                for service in host_service_thunks
            }
        )
    )


def _host_service_allocation_region_addresses(
    host_service_thunks: Sequence[_HostServiceThunk],
    *,
    guest_virtual_size: int,
    detached_guest: bool,
) -> tuple[int, ...]:
    """Return service regions not already owned by the helper's guest image."""

    guest_start = DETACHED_GUEST_RESERVATION_START if detached_guest else GUEST_SECTION_ADDRESS
    guest_end = (
        DETACHED_GUEST_RESERVATION_END
        if detached_guest
        else GUEST_SECTION_ADDRESS + guest_virtual_size
    )
    return tuple(
        region
        for region in _host_service_region_addresses(host_service_thunks)
        if not _address_range_collides(
            region,
            WINDOWS_ALLOCATION_GRANULARITY,
            guest_start,
            guest_end,
        )
    )


def _loaded_host_service_page_addresses(
    host_service_thunks: Sequence[_HostServiceThunk],
    *,
    guest_virtual_size: int,
    detached_guest: bool,
    audited_host_services: bool,
) -> tuple[int, ...]:
    """Return loaded guest-image pages patched with native service thunks."""

    guest_start = DETACHED_GUEST_RESERVATION_START if detached_guest else GUEST_SECTION_ADDRESS
    guest_end = (
        DETACHED_GUEST_RESERVATION_END
        if detached_guest
        else GUEST_SECTION_ADDRESS + guest_virtual_size
    )
    pages: set[int] = set()
    for index, service in enumerate(host_service_thunks):
        payload_size = len(
            _host_service_thunk_bytes(
                index,
                service,
                audited=audited_host_services,
            )
        )
        first_page = service.target & ~(PAGE_SIZE - 1)
        final_page = (service.target + payload_size - 1) & ~(PAGE_SIZE - 1)
        for page in range(first_page, final_page + PAGE_SIZE, PAGE_SIZE):
            if _address_range_collides(page, PAGE_SIZE, guest_start, guest_end):
                pages.add(page)
    return tuple(sorted(pages))


def _normal_live_host_data_pages(
    memory: SparseMemory,
    host_service_thunks: Sequence[_HostServiceThunk],
) -> tuple[tuple[int, bytes], ...]:
    """Retain materialized kernel data exports beside native service thunks."""

    service_regions = set(_host_service_region_addresses(host_service_thunks))
    pages = []
    for page, payload in memory.export_pages():
        address = page * PAGE_SIZE
        if (
            address >= 0xC0000000
            and address & ~(WINDOWS_ALLOCATION_GRANULARITY - 1) in service_regions
            and any(payload)
        ):
            pages.append((address, payload))
    return tuple(sorted(pages))


def _allocation_region_addresses(
    page_addresses: Sequence[int],
    *,
    guest_virtual_size: int,
    detached_guest: bool,
    host_service_thunks: Sequence[_HostServiceThunk],
) -> tuple[int, ...]:
    service_regions = set(_host_service_region_addresses(host_service_thunks))
    runtime_regions = {
        THUNK_REGION & ~(WINDOWS_ALLOCATION_GRANULARITY - 1),
        SCRATCH_ADDRESS & ~(WINDOWS_ALLOCATION_GRANULARITY - 1),
    }
    regions: set[int] = set()
    for address in page_addresses:
        if detached_guest and HELPER_IMAGE_BASE <= address < DETACHED_GUEST_RESERVATION_END:
            continue
        if (
            not detached_guest
            and GUEST_SECTION_ADDRESS <= address < GUEST_SECTION_ADDRESS + guest_virtual_size
        ):
            continue
        region = address & ~(WINDOWS_ALLOCATION_GRANULARITY - 1)
        if region not in service_regions and region not in runtime_regions:
            regions.add(region)
    return tuple(sorted(regions))


def _c_source(
    *,
    page_addresses: Sequence[int],
    mapped_page_addresses: Sequence[int],
    guest_virtual_size: int,
    code_spans: Sequence[tuple[int, bytes]],
    stop_eip: int,
    host_service_thunks: Sequence[_HostServiceThunk] = (),
    audited_host_services: bool = False,
) -> str:
    data_definitions: list[str] = []
    start_thunk = _build_start_thunk()
    exit_thunk = _build_exit_thunk()
    stop_patch = _stop_patch(stop_eip)
    data_definitions.extend(
        (
            f"static const U8 kStartThunk[] = {{{_bytes_initializer(start_thunk)}}};",
            f"static const U8 kExitThunk[] = {{{_bytes_initializer(exit_thunk)}}};",
            f"static const U8 kStopPatch[] = {{{_bytes_initializer(stop_patch)}}};",
        )
    )
    host_service_records = []
    for index, service in enumerate(host_service_thunks):
        payload = _host_service_thunk_bytes(
            index,
            service,
            audited=audited_host_services,
        )
        data_definitions.append(
            f"static const U8 kHostServiceThunk{index}[] = {{{_bytes_initializer(payload)}}};"
        )
        host_service_records.append(
            f"{{0x{service.target:08X}u,kHostServiceThunk{index},sizeof(kHostServiceThunk{index})}}"
        )
        if audited_host_services:
            body = _audited_service_body(index, service)
            data_definitions.append(
                f"static const U8 kHostServiceBody{index}[] = {{{_bytes_initializer(body)}}};"
            )
            host_service_records.append(
                f"{{0x{AUDITED_SERVICE_THUNK_BASE + index * AUDITED_SERVICE_THUNK_STRIDE:08X}u,"
                f"kHostServiceBody{index},sizeof(kHostServiceBody{index})}}"
            )
    pages = ",".join(f"0x{address:08X}u" for address in page_addresses)
    service_regions = _host_service_allocation_region_addresses(
        host_service_thunks,
        guest_virtual_size=guest_virtual_size,
        detached_guest=False,
    )
    allocation_regions = ",".join(
        f"0x{address:08X}u"
        for address in _allocation_region_addresses(
            page_addresses,
            guest_virtual_size=guest_virtual_size,
            detached_guest=False,
            host_service_thunks=host_service_thunks,
        )
    )
    mapped_pages = ",".join(f"0x{address:08X}u" for address in mapped_page_addresses)
    loaded_host_service_pages = ",".join(
        f"0x{address:08X}u"
        for address in _loaded_host_service_page_addresses(
            host_service_thunks,
            guest_virtual_size=guest_virtual_size,
            detached_guest=False,
            audited_host_services=audited_host_services,
        )
    )
    host_service_regions = ",".join(f"0x{address:08X}u" for address in service_regions)
    host_services = ",".join(host_service_records)
    definitions = "\n".join(data_definitions)
    return f"""/* Deterministic generated IA-32 fixed-slice helper. */
typedef unsigned char U8;
typedef unsigned short U16;
typedef unsigned long U32;
typedef unsigned long long U64;
typedef int BOOL;
typedef void* HANDLE;
typedef struct Exchange {{
    U32 magic;
    U32 version;
    volatile U32 status;
    U32 error;
    U32 page_count;
    U32 reserved;
    U64 elapsed_ticks;
    U64 qpc_frequency;
    U32 eip;
    U32 eflags;
    U32 registers[8];
    U8 fxsave[512];
    U8 pages[1];
}} Exchange;
typedef struct ExceptionRecord {{
    U32 code;
    U32 flags;
    struct ExceptionRecord* nested;
    void* address;
    U32 parameter_count;
    U32 information[15];
}} ExceptionRecord;
typedef struct ExceptionPointers {{ ExceptionRecord* record; void* context; }} ExceptionPointers;
typedef struct HostServiceThunk {{
    U32 address;
    const U8* code;
    U32 size;
}} HostServiceThunk;
__declspec(dllimport) char* __stdcall GetCommandLineA(void);
__declspec(dllimport) HANDLE __stdcall OpenFileMappingA(U32, BOOL, const char*);
__declspec(dllimport) void* __stdcall MapViewOfFile(HANDLE, U32, U32, U32, unsigned long);
__declspec(dllimport) BOOL __stdcall CloseHandle(HANDLE);
__declspec(dllimport) void* __stdcall VirtualAlloc(void*, unsigned long, U32, U32);
__declspec(dllimport) BOOL __stdcall VirtualProtect(void*, unsigned long, U32, U32*);
__declspec(dllimport) BOOL __stdcall FlushInstructionCache(HANDLE, const void*, unsigned long);
__declspec(dllimport) BOOL __stdcall QueryPerformanceCounter(U64*);
__declspec(dllimport) BOOL __stdcall QueryPerformanceFrequency(U64*);
__declspec(dllimport) void* __stdcall SetUnhandledExceptionFilter(
    long (__stdcall *)(ExceptionPointers*));
__declspec(dllimport) __declspec(noreturn) void __stdcall ExitProcess(U32);
typedef char ExchangeOffsetCheck[
    (__builtin_offsetof(Exchange, pages) == {EXCHANGE_HEADER_SIZE}) ? 1 : -1];
{definitions}
static const U32 kPages[] = {{{pages}}};
static const U32 kAllocationRegions[] = {{{allocation_regions}}};
static const U32 kMappedPages[] = {{{mapped_pages}}};
static const U32 kHostServiceRegions[] = {{{host_service_regions}}};
static const U32 kLoadedHostServicePages[] = {{{loaded_host_service_pages}}};
static const HostServiceThunk kHostServiceThunks[] = {{{host_services}}};
static Exchange* g_exchange;
static long __stdcall record_exception(ExceptionPointers* pointers) {{
    if (g_exchange && pointers && pointers->record) {{
        g_exchange->status = 97u;
        g_exchange->error = pointers->record->code;
        g_exchange->reserved = (U32)pointers->record->address;
        if (pointers->record->parameter_count >= 2u) {{
            g_exchange->elapsed_ticks = pointers->record->information[0];
            g_exchange->qpc_frequency = pointers->record->information[1];
        }}
    }}
    return 1;
}}
static void copy_bytes(void* target_value, const void* source_value, U32 size) {{
    U8* target = (U8*)target_value;
    const U8* source = (const U8*)source_value;
    U32 index;
    for (index = 0; index < size; ++index) target[index] = source[index];
}}
static void install_host_service_thunks(void) {{
    U32 index;
    for (index = 0u;
         index < sizeof(kHostServiceThunks) / sizeof(kHostServiceThunks[0]);
         ++index)
        copy_bytes((void*)kHostServiceThunks[index].address,
                   kHostServiceThunks[index].code,
                   kHostServiceThunks[index].size);
}}
static const char* mapping_name(void) {{
    const char* value = GetCommandLineA();
    static char name[160];
    U32 index = 0;
    if (*value == '\"') {{
        ++value;
        while (*value && *value != '\"') ++value;
        if (*value == '\"') ++value;
    }} else {{
        while (*value && *value != ' ' && *value != '\t') ++value;
    }}
    while (*value == ' ' || *value == '\t') ++value;
    while (*value && *value != ' ' && *value != '\t' && index + 1 < sizeof(name))
        name[index++] = *value++;
    name[index] = 0;
    return name;
}}
void __cdecl mainCRTStartup(void) {{
    const U32 FILE_MAP_ALL_ACCESS = 0x000F001Fu;
    const U32 MEM_COMMIT = 0x00001000u;
    const U32 MEM_RESERVE = 0x00002000u;
    const U32 MEM_COMMIT_RESERVE = MEM_COMMIT | MEM_RESERVE;
    const U32 PAGE_NOACCESS = 0x01u;
    const U32 PAGE_EXECUTE_READWRITE = 0x40u;
    HANDLE mapping;
    Exchange* exchange;
    U32 index;
    U8 original_stop[5];
    U64 started = 0;
    U64 finished = 0;
    U32 previous_protection = 0;
    if (VirtualAlloc((void*)0x{THUNK_REGION:08X}u, 0x10000u,
                     MEM_COMMIT_RESERVE, PAGE_EXECUTE_READWRITE) !=
        (void*)0x{THUNK_REGION:08X}u) ExitProcess(14);
    if (VirtualAlloc((void*)0x{SCRATCH_ADDRESS:08X}u, 0x10000u,
                     MEM_COMMIT_RESERVE, PAGE_EXECUTE_READWRITE) !=
        (void*)0x{SCRATCH_ADDRESS:08X}u) ExitProcess(15);
    mapping = OpenFileMappingA(FILE_MAP_ALL_ACCESS, 0, mapping_name());
    if (!mapping) ExitProcess(10);
    exchange = (Exchange*)MapViewOfFile(mapping, FILE_MAP_ALL_ACCESS, 0, 0, 0);
    if (!exchange) ExitProcess(11);
    g_exchange = exchange;
    SetUnhandledExceptionFilter(record_exception);
    if (exchange->magic != 0x{EXCHANGE_MAGIC:08X}u ||
        exchange->version != {EXCHANGE_VERSION}u ||
        exchange->page_count != {len(page_addresses)}u) {{
        exchange->error = 12;
        ExitProcess(12);
    }}
    exchange->status = {EXCHANGE_STATUS_RUNNING}u;
    for (index = 0;
         index < sizeof(kAllocationRegions) / sizeof(kAllocationRegions[0]);
         ++index) {{
        if (VirtualAlloc((void*)kAllocationRegions[index], 0x10000u,
                         MEM_RESERVE, PAGE_NOACCESS) !=
            (void*)kAllocationRegions[index]) {{
            exchange->error = 13u;
            exchange->reserved = kAllocationRegions[index];
            ExitProcess(13);
        }}
    }}
    for (index = 0;
         index < sizeof(kHostServiceRegions) / sizeof(kHostServiceRegions[0]);
         ++index) {{
        if (VirtualAlloc((void*)kHostServiceRegions[index], 0x10000u,
                         MEM_COMMIT_RESERVE, PAGE_EXECUTE_READWRITE) !=
            (void*)kHostServiceRegions[index]) {{
            exchange->error = 19u;
            exchange->reserved = kHostServiceRegions[index];
            ExitProcess(19);
        }}
    }}
    for (index = 0; index < sizeof(kPages) / sizeof(kPages[0]); ++index) {{
        if (kPages[index] >= 0x{GUEST_SECTION_ADDRESS:08X}u &&
            kPages[index] < 0x{GUEST_SECTION_ADDRESS + guest_virtual_size:08X}u)
            continue;
        if (VirtualAlloc((void*)kPages[index], 4096u, MEM_COMMIT,
                         PAGE_EXECUTE_READWRITE) != (void*)kPages[index]) {{
            exchange->error = 18u;
            exchange->reserved = kPages[index];
            ExitProcess(18);
        }}
    }}
    for (index = 0;
         index < sizeof(kLoadedHostServicePages) /
                     sizeof(kLoadedHostServicePages[0]);
         ++index) {{
        if (!VirtualProtect((void*)kLoadedHostServicePages[index], 4096u,
                            PAGE_EXECUTE_READWRITE, &previous_protection)) {{
            exchange->error = 17u;
            exchange->reserved = kLoadedHostServicePages[index];
            ExitProcess(17);
        }}
    }}
    for (index = 0; index < sizeof(kPages) / sizeof(kPages[0]); ++index)
        copy_bytes((void*)kPages[index], exchange->pages + index * 4096u, 4096u);
    copy_bytes((void*)0x{START_THUNK_ADDRESS:08X}u, kStartThunk, sizeof(kStartThunk));
    copy_bytes((void*)0x{EXIT_THUNK_ADDRESS:08X}u, kExitThunk, sizeof(kExitThunk));
    for (index = 0;
         index < sizeof(kHostServiceThunks) / sizeof(kHostServiceThunks[0]);
         ++index)
        copy_bytes((void*)kHostServiceThunks[index].address,
                   kHostServiceThunks[index].code,
                   kHostServiceThunks[index].size);
    copy_bytes(original_stop, (void*)0x{stop_eip:08X}u, sizeof(original_stop));
    copy_bytes((void*)0x{stop_eip:08X}u, kStopPatch, sizeof(kStopPatch));
    if (!VirtualProtect((void*)0x{GUEST_SECTION_ADDRESS:08X}u,
                        0x{guest_virtual_size:X}u, PAGE_NOACCESS,
                        &previous_protection)) ExitProcess(16);
    for (index = 0; index < sizeof(kMappedPages) / sizeof(kMappedPages[0]); ++index)
        if (!VirtualProtect((void*)kMappedPages[index], 4096u,
                            PAGE_EXECUTE_READWRITE, &previous_protection))
            ExitProcess(17);
    FlushInstructionCache((HANDLE)-1, (void*)0x{GUEST_SECTION_ADDRESS:08X}u,
                          0x{guest_virtual_size:X}u);
    for (index = 0; index < 8u; ++index)
        *(U32*)(0x{SCRATCH_ADDRESS:08X}u + index * 4u) = exchange->registers[index];
    *(U32*)0x{SCRATCH_ADDRESS + SCRATCH_EFLAGS_OFFSET:08X}u = exchange->eflags;
    *(U32*)0x{SCRATCH_ADDRESS + SCRATCH_ENTRY_EIP_OFFSET:08X}u = exchange->eip;
    copy_bytes((void*)0x{SCRATCH_ADDRESS + SCRATCH_FXSAVE_OFFSET:08X}u,
               exchange->fxsave, 512u);
    if (!QueryPerformanceFrequency(&exchange->qpc_frequency) ||
        !QueryPerformanceCounter(&started)) {{
        exchange->error = 30;
        ExitProcess(30);
    }}
    ((void (__cdecl *)(void))0x{START_THUNK_ADDRESS:08X}u)();
    QueryPerformanceCounter(&finished);
    exchange->elapsed_ticks = finished - started;
    for (index = 0; index < 8u; ++index)
        exchange->registers[index] = *(U32*)(0x{SCRATCH_ADDRESS:08X}u + index * 4u);
    exchange->eflags = *(U32*)0x{SCRATCH_ADDRESS + SCRATCH_EFLAGS_OFFSET:08X}u;
    exchange->eip = 0x{stop_eip:08X}u;
    copy_bytes(exchange->fxsave,
               (void*)0x{SCRATCH_ADDRESS + SCRATCH_FXSAVE_OFFSET:08X}u, 512u);
    copy_bytes((void*)0x{stop_eip:08X}u, original_stop, sizeof(original_stop));
    for (index = 0; index < sizeof(kPages) / sizeof(kPages[0]); ++index)
        copy_bytes(exchange->pages + index * 4096u, (void*)kPages[index], 4096u);
    exchange->status = {EXCHANGE_STATUS_COMPLETE}u;
    CloseHandle(mapping);
    ExitProcess(0);
}}
"""


def _persistent_c_source(
    *,
    page_addresses: Sequence[int],
    mapped_page_addresses: Sequence[int],
    guest_virtual_size: int,
    code_spans: Sequence[tuple[int, bytes]],
    stop_eip: int,
    host_service_thunks: Sequence[_HostServiceThunk] = (),
    audited_host_services: bool = False,
    detached_guest: bool = False,
) -> str:
    data_definitions: list[str] = []
    start_thunk = _build_start_thunk()
    exit_thunk = _build_exit_thunk()
    stop_patch = _stop_patch(stop_eip)
    data_definitions.extend(
        (
            f"static const U8 kStartThunk[] = {{{_bytes_initializer(start_thunk)}}};",
            f"static const U8 kExitThunk[] = {{{_bytes_initializer(exit_thunk)}}};",
            f"static const U8 kStopPatch[] = {{{_bytes_initializer(stop_patch)}}};",
        )
    )
    host_service_records = []
    detached_code_records = []
    if detached_guest:
        for index, (address, payload) in enumerate(
            _detached_guest_code_spans(code_spans, stop_eip=stop_eip)
        ):
            data_definitions.append(
                f"static const U8 kDetachedGuestCode{index}[] = {{{_bytes_initializer(payload)}}};"
            )
            detached_code_records.append(
                f"{{0x{address:08X}u,kDetachedGuestCode{index},sizeof(kDetachedGuestCode{index})}}"
            )
    for index, service in enumerate(host_service_thunks):
        payload = _host_service_thunk_bytes(
            index,
            service,
            audited=audited_host_services,
        )
        data_definitions.append(
            f"static const U8 kHostServiceThunk{index}[] = {{{_bytes_initializer(payload)}}};"
        )
        host_service_records.append(
            f"{{0x{service.target:08X}u,kHostServiceThunk{index},sizeof(kHostServiceThunk{index})}}"
        )
        if audited_host_services:
            body = _audited_service_body(index, service)
            data_definitions.append(
                f"static const U8 kHostServiceBody{index}[] = {{{_bytes_initializer(body)}}};"
            )
            host_service_records.append(
                f"{{0x{AUDITED_SERVICE_THUNK_BASE + index * AUDITED_SERVICE_THUNK_STRIDE:08X}u,"
                f"kHostServiceBody{index},sizeof(kHostServiceBody{index})}}"
            )
    pages = ",".join(f"0x{address:08X}u" for address in page_addresses)
    service_regions = _host_service_allocation_region_addresses(
        host_service_thunks,
        guest_virtual_size=guest_virtual_size,
        detached_guest=detached_guest,
    )
    allocation_regions = ",".join(
        f"0x{address:08X}u"
        for address in _allocation_region_addresses(
            page_addresses,
            guest_virtual_size=guest_virtual_size,
            detached_guest=detached_guest,
            host_service_thunks=host_service_thunks,
        )
    )
    mapped_pages = ",".join(f"0x{address:08X}u" for address in mapped_page_addresses)
    loaded_host_service_pages = ",".join(
        f"0x{address:08X}u"
        for address in _loaded_host_service_page_addresses(
            host_service_thunks,
            guest_virtual_size=guest_virtual_size,
            detached_guest=detached_guest,
            audited_host_services=audited_host_services,
        )
    )
    host_service_regions = ",".join(f"0x{address:08X}u" for address in service_regions)
    host_services = ",".join(host_service_records)
    occupied_addresses = {
        *page_addresses,
        *(service.target for service in host_service_thunks),
        THUNK_REGION,
        SCRATCH_ADDRESS,
        DETACHED_HELPER_IMAGE_BASE if detached_guest else HELPER_IMAGE_BASE,
    }
    exchange_view_candidates = tuple(
        candidate
        for candidate in WORKER_EXCHANGE_VIEW_CANDIDATES
        if not any(
            candidate <= address < candidate + WORKER_EXCHANGE_VIEW_SPAN
            for address in occupied_addresses
        )
    )
    if not exchange_view_candidates:
        raise Ia32BackendError("IA-32 worker has no collision-free fixed exchange-view candidate")
    exchange_views = ",".join(f"0x{address:08X}u" for address in exchange_view_candidates)
    detached_codes = ",".join(detached_code_records) or "{0u,0,0u}"
    detached_code_count = (
        "sizeof(kDetachedGuestSpans) / sizeof(kDetachedGuestSpans[0])"
        if detached_code_records
        else "0u"
    )
    guest_page_skip = (
        f"""        if (kPages[index] >= 0x{HELPER_IMAGE_BASE:08X}u &&
            kPages[index] < 0x{DETACHED_GUEST_RESERVATION_END:08X}u)
            continue;"""
        if detached_guest
        else f"""        if (kPages[index] >= 0x{GUEST_SECTION_ADDRESS:08X}u &&
            kPages[index] < 0x{GUEST_SECTION_ADDRESS + guest_virtual_size:08X}u)
            continue;"""
    )
    guest_protection = (
        f"""    if (!VirtualProtect((void*)0x{DETACHED_GUEST_RESERVATION_START:08X}u,
                        0x{DETACHED_GUEST_RESERVATION_END - DETACHED_GUEST_RESERVATION_START:X}u,
                        PAGE_NOACCESS, &previous_protection))
        protocol_failure(16u, 0x{DETACHED_GUEST_RESERVATION_START:08X}u);
"""
        if detached_guest
        else f"""    if (!VirtualProtect((void*)0x{GUEST_SECTION_ADDRESS:08X}u,
                        0x{guest_virtual_size:X}u, PAGE_NOACCESS,
                        &previous_protection)) protocol_failure(16u, 0x{GUEST_SECTION_ADDRESS:08X}u);
"""
    )
    detached_copy = (
        f"""        for (index = 0u; index < {detached_code_count}; ++index)
            copy_bytes((void*)kDetachedGuestSpans[index].address,
                       kDetachedGuestSpans[index].code,
                       kDetachedGuestSpans[index].size);
"""
        if detached_guest
        else ""
    )
    detached_flush = (
        f"""        for (index = 0u; index < {detached_code_count}; ++index)
            FlushInstructionCache((HANDLE)-1,
                                  (void*)kDetachedGuestSpans[index].address,
                                  kDetachedGuestSpans[index].size);
"""
        if detached_guest
        else ""
    )
    code_segment = '#pragma code_seg(".text$z")\n' if detached_guest else ""
    memory_query_type = (
        """typedef struct MemoryBasicInformation {
    void* base_address;
    void* allocation_base;
    U32 allocation_protect;
    U32 region_size;
    U32 state;
    U32 protect;
    U32 type;
} MemoryBasicInformation;
"""
        if detached_guest
        else ""
    )
    map_view_declaration = (
        """__declspec(dllimport) void* __stdcall MapViewOfFileEx(
    HANDLE, U32, U32, U32, unsigned long, void*);"""
        if detached_guest
        else "__declspec(dllimport) void* __stdcall MapViewOfFile(HANDLE, U32, U32, U32, unsigned long);"
    )
    virtual_query_declaration = (
        """__declspec(dllimport) unsigned long __stdcall VirtualQuery(
    const void*, MemoryBasicInformation*, unsigned long);
"""
        if detached_guest
        else ""
    )
    detached_definitions = (
        f"""static const U32 kExchangeViewAddresses[] = {{{exchange_views}}};
static const HostServiceThunk kDetachedGuestSpans[] = {{{detached_codes}}};
"""
        if detached_guest
        else ""
    )
    allocation_failure_definition = (
        """static __declspec(noreturn) void allocation_failure(U32 error, U32 detail) {
    MemoryBasicInformation information;
    if (g_exchange &&
        VirtualQuery((void*)detail, &information, sizeof(information)) ==
            sizeof(information)) {
        g_exchange->elapsed_ticks = (U32)information.allocation_base;
        g_exchange->qpc_frequency =
            ((U64)information.state << 32u) | information.region_size;
    }
    protocol_failure(error, detail);
}
"""
        if detached_guest
        else ""
    )
    exchange_mapping = (
        """    exchange = 0;
    for (index = 0;
         index < sizeof(kExchangeViewAddresses) / sizeof(kExchangeViewAddresses[0]);
         ++index) {
        exchange = (Exchange*)MapViewOfFileEx(
            mapping, FILE_MAP_ALL_ACCESS, 0, 0, 0,
            (void*)kExchangeViewAddresses[index]);
        if (exchange) break;
    }
"""
        if detached_guest
        else "    exchange = (Exchange*)MapViewOfFile(mapping, FILE_MAP_ALL_ACCESS, 0, 0, 0);\n"
    )
    allocation_failure_call = "allocation_failure" if detached_guest else "protocol_failure"
    definitions = "\n".join(data_definitions)
    return f"""/* Deterministic generated persistent IA-32 fixed-slice worker. */
{code_segment}typedef unsigned char U8;
typedef unsigned short U16;
typedef unsigned long U32;
typedef unsigned long long U64;
typedef int BOOL;
typedef void* HANDLE;
typedef struct Exchange {{
    U32 magic;
    U32 version;
    volatile U32 status;
    U32 error;
    U32 page_count;
    U32 reserved;
    U64 elapsed_ticks;
    U64 qpc_frequency;
    U32 eip;
    U32 eflags;
    U32 registers[8];
    U8 fxsave[512];
    U8 pages[1];
}} Exchange;
typedef struct ExceptionRecord {{
    U32 code;
    U32 flags;
    struct ExceptionRecord* nested;
    void* address;
    U32 parameter_count;
    U32 information[15];
}} ExceptionRecord;
typedef struct ExceptionPointers {{ ExceptionRecord* record; void* context; }} ExceptionPointers;
{memory_query_type}typedef struct HostServiceThunk {{
    U32 address;
    const U8* code;
    U32 size;
}} HostServiceThunk;
__declspec(dllimport) char* __stdcall GetCommandLineA(void);
__declspec(dllimport) HANDLE __stdcall OpenFileMappingA(U32, BOOL, const char*);
{map_view_declaration}
__declspec(dllimport) HANDLE __stdcall OpenEventA(U32, BOOL, const char*);
__declspec(dllimport) BOOL __stdcall SetEvent(HANDLE);
__declspec(dllimport) U32 __stdcall WaitForSingleObject(HANDLE, U32);
__declspec(dllimport) BOOL __stdcall CloseHandle(HANDLE);
__declspec(dllimport) void* __stdcall VirtualAlloc(void*, unsigned long, U32, U32);
{virtual_query_declaration}__declspec(dllimport) BOOL __stdcall VirtualProtect(void*, unsigned long, U32, U32*);
__declspec(dllimport) BOOL __stdcall FlushInstructionCache(HANDLE, const void*, unsigned long);
__declspec(dllimport) BOOL __stdcall QueryPerformanceCounter(U64*);
__declspec(dllimport) BOOL __stdcall QueryPerformanceFrequency(U64*);
__declspec(dllimport) void* __stdcall SetUnhandledExceptionFilter(
    long (__stdcall *)(ExceptionPointers*));
__declspec(dllimport) __declspec(noreturn) void __stdcall ExitProcess(U32);
typedef char ExchangeOffsetCheck[
    (__builtin_offsetof(Exchange, pages) == {EXCHANGE_HEADER_SIZE}) ? 1 : -1];
{definitions}
static const U32 kPages[] = {{{pages}}};
static const U32 kAllocationRegions[] = {{{allocation_regions}}};
static const U32 kMappedPages[] = {{{mapped_pages}}};
static const U32 kHostServiceRegions[] = {{{host_service_regions}}};
static const U32 kLoadedHostServicePages[] = {{{loaded_host_service_pages}}};
static const HostServiceThunk kHostServiceThunks[] = {{{host_services}}};
{detached_definitions}static Exchange* g_exchange;
static HANDLE g_response_event;
static long __stdcall record_exception(ExceptionPointers* pointers) {{
    if (g_exchange && pointers && pointers->record) {{
        g_exchange->status = {WORKER_STATUS_FAULT}u;
        g_exchange->error = pointers->record->code;
        g_exchange->reserved = (U32)pointers->record->address;
        if (pointers->record->parameter_count >= 2u) {{
            g_exchange->elapsed_ticks = pointers->record->information[0];
            g_exchange->qpc_frequency = pointers->record->information[1];
        }}
        if (g_response_event) SetEvent(g_response_event);
    }}
    return 1;
}}
static void copy_bytes(void* target_value, const void* source_value, U32 size) {{
    U8* target = (U8*)target_value;
    const U8* source = (const U8*)source_value;
    U32 index;
    for (index = 0; index < size; ++index) target[index] = source[index];
}}
static void install_host_service_thunks(void) {{
    U32 index;
    for (index = 0u;
         index < sizeof(kHostServiceThunks) / sizeof(kHostServiceThunks[0]);
         ++index)
        copy_bytes((void*)kHostServiceThunks[index].address,
                   kHostServiceThunks[index].code,
                   kHostServiceThunks[index].size);
}}
static const char* mapping_name(void) {{
    const char* value = GetCommandLineA();
    static char name[160];
    U32 index = 0;
    if (*value == '\"') {{
        ++value;
        while (*value && *value != '\"') ++value;
        if (*value == '\"') ++value;
    }} else {{
        while (*value && *value != ' ' && *value != '\t') ++value;
    }}
    while (*value == ' ' || *value == '\t') ++value;
    while (*value && *value != ' ' && *value != '\t' && index + 1 < sizeof(name))
        name[index++] = *value++;
    name[index] = 0;
    return name;
}}
static void event_name(char* target, const char* suffix) {{
    const char* source = mapping_name();
    U32 index = 0;
    while (*source && index < 190u) target[index++] = *source++;
    while (*suffix && index < 190u) target[index++] = *suffix++;
    target[index] = 0;
}}
static __declspec(noreturn) void protocol_failure(U32 error, U32 detail) {{
    if (g_exchange) {{
        g_exchange->status = {WORKER_STATUS_PROTOCOL_ERROR}u;
        g_exchange->error = error;
        g_exchange->reserved = detail;
    }}
    if (g_response_event) SetEvent(g_response_event);
    ExitProcess(error);
}}
{allocation_failure_definition}void __cdecl mainCRTStartup(void) {{
    const U32 FILE_MAP_ALL_ACCESS = 0x000F001Fu;
    const U32 EVENT_MODIFY_STATE = 0x00000002u;
    const U32 SYNCHRONIZE = 0x00100000u;
    const U32 INFINITE = 0xFFFFFFFFu;
    const U32 WAIT_OBJECT_0 = 0u;
    const U32 MEM_COMMIT = 0x00001000u;
    const U32 MEM_RESERVE = 0x00002000u;
    const U32 MEM_COMMIT_RESERVE = MEM_COMMIT | MEM_RESERVE;
    const U32 PAGE_NOACCESS = 0x01u;
    const U32 PAGE_EXECUTE_READWRITE = 0x40u;
    HANDLE mapping;
    HANDLE request_event;
    Exchange* exchange;
    U32 index;
    U32 command;
    U8 original_stop[5];
    U64 started = 0;
    U64 finished = 0;
    U64 frequency = 0;
    U32 previous_protection = 0;
    char request_name[192];
    char response_name[192];
    mapping = OpenFileMappingA(FILE_MAP_ALL_ACCESS, 0, mapping_name());
    if (!mapping) ExitProcess(10);
{exchange_mapping}    if (!exchange) ExitProcess(11);
    event_name(request_name, "-request");
    event_name(response_name, "-response");
    request_event = OpenEventA(SYNCHRONIZE, 0, request_name);
    g_response_event = OpenEventA(EVENT_MODIFY_STATE, 0, response_name);
    if (!request_event || !g_response_event) ExitProcess(20);
    g_exchange = exchange;
    SetUnhandledExceptionFilter(record_exception);
    if (exchange->magic != 0x{EXCHANGE_MAGIC:08X}u ||
        exchange->version != {WORKER_EXCHANGE_VERSION}u ||
        exchange->error != {WORKER_PROTOCOL_VERSION}u ||
        exchange->page_count != {len(page_addresses)}u)
        protocol_failure(21u, exchange->eip);
    if (VirtualAlloc((void*)0x{THUNK_REGION:08X}u, 0x10000u,
                     MEM_COMMIT_RESERVE, PAGE_EXECUTE_READWRITE) !=
        (void*)0x{THUNK_REGION:08X}u) protocol_failure(14u, 0x{THUNK_REGION:08X}u);
    if (VirtualAlloc((void*)0x{SCRATCH_ADDRESS:08X}u, 0x10000u,
                     MEM_COMMIT_RESERVE, PAGE_EXECUTE_READWRITE) !=
        (void*)0x{SCRATCH_ADDRESS:08X}u) protocol_failure(15u, 0x{SCRATCH_ADDRESS:08X}u);
    for (index = 0;
         index < sizeof(kAllocationRegions) / sizeof(kAllocationRegions[0]);
         ++index) {{
        if (VirtualAlloc((void*)kAllocationRegions[index], 0x10000u,
                         MEM_RESERVE, PAGE_NOACCESS) !=
            (void*)kAllocationRegions[index])
            {allocation_failure_call}(13u, kAllocationRegions[index]);
    }}
    for (index = 0;
         index < sizeof(kHostServiceRegions) / sizeof(kHostServiceRegions[0]);
         ++index) {{
        if (VirtualAlloc((void*)kHostServiceRegions[index], 0x10000u,
                         MEM_COMMIT_RESERVE, PAGE_EXECUTE_READWRITE) !=
            (void*)kHostServiceRegions[index])
            protocol_failure(19u, kHostServiceRegions[index]);
    }}
    for (index = 0; index < sizeof(kPages) / sizeof(kPages[0]); ++index) {{
{guest_page_skip}
        if (VirtualAlloc((void*)kPages[index], 4096u, MEM_COMMIT,
                         PAGE_EXECUTE_READWRITE) != (void*)kPages[index])
            protocol_failure(18u, kPages[index]);
    }}
    for (index = 0;
         index < sizeof(kLoadedHostServicePages) /
                     sizeof(kLoadedHostServicePages[0]);
         ++index)
        if (!VirtualProtect((void*)kLoadedHostServicePages[index], 4096u,
                            PAGE_EXECUTE_READWRITE, &previous_protection))
            protocol_failure(17u, kLoadedHostServicePages[index]);
    copy_bytes((void*)0x{START_THUNK_ADDRESS:08X}u, kStartThunk, sizeof(kStartThunk));
    copy_bytes((void*)0x{EXIT_THUNK_ADDRESS:08X}u, kExitThunk, sizeof(kExitThunk));
    for (index = 0;
         index < sizeof(kHostServiceThunks) / sizeof(kHostServiceThunks[0]);
         ++index)
        copy_bytes((void*)kHostServiceThunks[index].address,
                   kHostServiceThunks[index].code,
                   kHostServiceThunks[index].size);
    copy_bytes(original_stop, (void*)0x{stop_eip:08X}u, sizeof(original_stop));
{guest_protection}    for (index = 0; index < sizeof(kMappedPages) / sizeof(kMappedPages[0]); ++index)
        if (!VirtualProtect((void*)kMappedPages[index], 4096u,
                            PAGE_EXECUTE_READWRITE, &previous_protection))
            protocol_failure(17u, kMappedPages[index]);
    if (!QueryPerformanceFrequency(&frequency)) protocol_failure(30u, exchange->eip);
    exchange->error = 0u;
    exchange->reserved = {WORKER_PROTOCOL_VERSION}u;
    exchange->status = {WORKER_STATUS_READY}u;
    if (!SetEvent(g_response_event)) ExitProcess(22);
    for (;;) {{
        if (WaitForSingleObject(request_event, INFINITE) != WAIT_OBJECT_0)
            protocol_failure(23u, exchange->eip);
        command = exchange->reserved;
        if (exchange->magic != 0x{EXCHANGE_MAGIC:08X}u ||
            exchange->version != {WORKER_EXCHANGE_VERSION}u ||
            exchange->error != {WORKER_PROTOCOL_VERSION}u ||
            exchange->page_count != {len(page_addresses)}u ||
            exchange->status != {EXCHANGE_STATUS_PENDING}u)
            protocol_failure(24u, exchange->eip);
        if (command == {WORKER_COMMAND_STOP}u) {{
            exchange->error = 0u;
            exchange->reserved = command;
            exchange->status = {WORKER_STATUS_STOPPED}u;
            SetEvent(g_response_event);
            CloseHandle(request_event);
            CloseHandle(g_response_event);
            CloseHandle(mapping);
            ExitProcess(0);
        }}
        if (command != {WORKER_COMMAND_ENTER}u &&
            command != {WORKER_COMMAND_RESUME}u &&
            command != {WORKER_COMMAND_SERVICE_RETURN}u)
            protocol_failure(25u, exchange->eip);
        exchange->status = {EXCHANGE_STATUS_RUNNING}u;
        exchange->error = 0u;
        for (index = 0; index < sizeof(kPages) / sizeof(kPages[0]); ++index)
            copy_bytes((void*)kPages[index], exchange->pages + index * 4096u, 4096u);
{detached_copy}        install_host_service_thunks();
        copy_bytes((void*)0x{stop_eip:08X}u, kStopPatch, sizeof(kStopPatch));
        FlushInstructionCache((HANDLE)-1, (void*)0x{GUEST_SECTION_ADDRESS:08X}u,
                              0x{guest_virtual_size:X}u);
{detached_flush}        for (index = 0; index < 8u; ++index)
            *(U32*)(0x{SCRATCH_ADDRESS:08X}u + index * 4u) = exchange->registers[index];
        *(U32*)0x{SCRATCH_ADDRESS + SCRATCH_EFLAGS_OFFSET:08X}u = exchange->eflags;
        *(U32*)0x{SCRATCH_ADDRESS + SCRATCH_ENTRY_EIP_OFFSET:08X}u = exchange->eip;
        copy_bytes((void*)0x{SCRATCH_ADDRESS + SCRATCH_FXSAVE_OFFSET:08X}u,
                   exchange->fxsave, 512u);
        if (!QueryPerformanceCounter(&started)) protocol_failure(30u, exchange->eip);
        ((void (__cdecl *)(void))0x{START_THUNK_ADDRESS:08X}u)();
        QueryPerformanceCounter(&finished);
        exchange->elapsed_ticks = finished - started;
        exchange->qpc_frequency = frequency;
        for (index = 0; index < 8u; ++index)
            exchange->registers[index] = *(U32*)(0x{SCRATCH_ADDRESS:08X}u + index * 4u);
        exchange->eflags = *(U32*)0x{SCRATCH_ADDRESS + SCRATCH_EFLAGS_OFFSET:08X}u;
        exchange->eip = 0x{stop_eip:08X}u;
        copy_bytes(exchange->fxsave,
                   (void*)0x{SCRATCH_ADDRESS + SCRATCH_FXSAVE_OFFSET:08X}u, 512u);
        copy_bytes((void*)0x{stop_eip:08X}u, original_stop, sizeof(original_stop));
        for (index = 0; index < sizeof(kPages) / sizeof(kPages[0]); ++index)
            copy_bytes(exchange->pages + index * 4096u, (void*)kPages[index], 4096u);
        exchange->error = 0u;
        exchange->reserved = command;
        exchange->status = {EXCHANGE_STATUS_COMPLETE}u;
        if (!SetEvent(g_response_event)) ExitProcess(26);
    }}
}}
"""


def _decoded_store_c_source(
    *,
    page_addresses: Sequence[int],
    mapped_page_addresses: Sequence[int],
    guest_virtual_size: int,
    code_spans: Sequence[tuple[int, bytes]],
    stop_eip: int,
    host_service_thunks: Sequence[_HostServiceThunk] = (),
    audited_host_services: bool = False,
    detached_guest: bool = False,
) -> str:
    """Emit the resident worker with its stop edge fixed at build time."""

    source = _persistent_c_source(
        page_addresses=page_addresses,
        mapped_page_addresses=mapped_page_addresses,
        guest_virtual_size=guest_virtual_size,
        code_spans=code_spans,
        stop_eip=stop_eip,
        host_service_thunks=host_service_thunks,
        audited_host_services=audited_host_services,
        detached_guest=detached_guest,
    )
    runtime_patch_fragments = (
        "    U8 original_stop[5];\n",
        f"    copy_bytes(original_stop, (void*)0x{stop_eip:08X}u, sizeof(original_stop));\n",
        f"        copy_bytes((void*)0x{stop_eip:08X}u, kStopPatch, sizeof(kStopPatch));\n",
        f"        copy_bytes((void*)0x{stop_eip:08X}u, original_stop, sizeof(original_stop));\n",
    )
    for fragment in runtime_patch_fragments:
        if fragment not in source:
            raise Ia32BackendError("Phase-2 worker generator lost its build-time stop invariant")
        source = source.replace(fragment, "", 1)
    return source


def _phase3_c_source(
    *,
    page_addresses: Sequence[int],
    mapped_page_addresses: Sequence[int],
    guest_virtual_size: int,
    code_spans: Sequence[tuple[int, bytes]],
    stop_eip: int,
    host_service_thunks: Sequence[_HostServiceThunk] = (),
    audited_host_services: bool = False,
    detached_guest: bool = False,
) -> str:
    """Emit a v3 worker with native dirty bits and recoverable SEH commands."""

    source = _decoded_store_c_source(
        page_addresses=page_addresses,
        mapped_page_addresses=mapped_page_addresses,
        guest_virtual_size=guest_virtual_size,
        code_spans=code_spans,
        stop_eip=stop_eip,
        host_service_thunks=host_service_thunks,
        audited_host_services=audited_host_services,
        detached_guest=detached_guest,
    )
    source = source.replace(
        f"exchange->version != {WORKER_EXCHANGE_VERSION}u",
        f"exchange->version != {ARCHITECTURE_EXCHANGE_VERSION}u",
    )
    declaration = """__declspec(dllimport) void* __stdcall SetUnhandledExceptionFilter(
    long (__stdcall *)(ExceptionPointers*));
__declspec(dllimport) void* __stdcall AddVectoredExceptionHandler(
    U32, long (__stdcall *)(ExceptionPointers*));
"""
    old_declaration = """__declspec(dllimport) void* __stdcall SetUnhandledExceptionFilter(
    long (__stdcall *)(ExceptionPointers*));
"""
    if old_declaration not in source:
        raise Ia32BackendError("Phase-3 worker lost its exception declaration seam")
    source = source.replace(old_declaration, declaration, 1)
    old_handler = f"""static long __stdcall record_exception(ExceptionPointers* pointers) {{
    if (g_exchange && pointers && pointers->record) {{
        g_exchange->status = {WORKER_STATUS_FAULT}u;
        g_exchange->error = pointers->record->code;
        g_exchange->reserved = (U32)pointers->record->address;
        if (pointers->record->parameter_count >= 2u) {{
            g_exchange->elapsed_ticks = pointers->record->information[0];
            g_exchange->qpc_frequency = pointers->record->information[1];
        }}
        if (g_response_event) SetEvent(g_response_event);
    }}
    return 1;
}}
"""
    new_handler = f"""static U32 g_recovery_eip;
static BOOL g_guest_faulted;
static long __stdcall record_exception(ExceptionPointers* pointers) {{
    if (g_exchange && g_recovery_eip && pointers && pointers->record) {{
        U8* context = (U8*)pointers->context;
        g_exchange->status = {WORKER_STATUS_FAULT}u;
        g_exchange->error = pointers->record->code;
        g_exchange->reserved = (U32)pointers->record->address;
        if (pointers->record->parameter_count >= 2u) {{
            g_exchange->elapsed_ticks = pointers->record->information[0];
            g_exchange->qpc_frequency = pointers->record->information[1];
        }}
        if (context) {{
            g_exchange->registers[0] = *(U32*)(context + 176u);
            g_exchange->registers[1] = *(U32*)(context + 172u);
            g_exchange->registers[2] = *(U32*)(context + 168u);
            g_exchange->registers[3] = *(U32*)(context + 164u);
            g_exchange->registers[4] = *(U32*)(context + 196u);
            g_exchange->registers[5] = *(U32*)(context + 180u);
            g_exchange->registers[6] = *(U32*)(context + 160u);
            g_exchange->registers[7] = *(U32*)(context + 156u);
            g_exchange->eip = *(U32*)(context + 184u);
            g_exchange->eflags = *(U32*)(context + 192u);
            copy_bytes(g_exchange->fxsave, context + 204u, 512u);
            *(U32*)(context + 184u) = g_recovery_eip;
            *(U32*)(context + 196u) =
                *(U32*)0x{SCRATCH_ADDRESS + SCRATCH_HOST_ESP_OFFSET:08X}u + 4u;
            *(U32*)(context + 164u) =
                *(U32*)0x{SCRATCH_ADDRESS + SCRATCH_HOST_EBX_OFFSET:08X}u;
            *(U32*)(context + 160u) =
                *(U32*)0x{SCRATCH_ADDRESS + SCRATCH_HOST_ESI_OFFSET:08X}u;
            *(U32*)(context + 156u) =
                *(U32*)0x{SCRATCH_ADDRESS + SCRATCH_HOST_EDI_OFFSET:08X}u;
            *(U32*)(context + 180u) =
                *(U32*)0x{SCRATCH_ADDRESS + SCRATCH_HOST_EBP_OFFSET:08X}u;
        }}
        g_guest_faulted = 1;
        return -1;
    }}
    return 0;
}}
"""
    # The handler now uses copy_bytes, so place it after that helper.
    if old_handler not in source:
        raise Ia32BackendError("Phase-3 worker lost its exception-handler seam")
    source = source.replace(old_handler, "", 1)
    copy_helper_end = """    for (index = 0; index < size; ++index) target[index] = source[index];
}
"""
    native_publication = f"""    for (index = 0; index < size; ++index) target[index] = source[index];
}}
{new_handler}static BOOL page_changed(const U8* before, const U8* after) {{
    const U32* left = (const U32*)before;
    const U32* right = (const U32*)after;
    U32 index;
    for (index = 0; index < 1024u; index += 8u) {{
        U32 changed = (left[index] ^ right[index]) |
            (left[index + 1u] ^ right[index + 1u]) |
            (left[index + 2u] ^ right[index + 2u]) |
            (left[index + 3u] ^ right[index + 3u]) |
            (left[index + 4u] ^ right[index + 4u]) |
            (left[index + 5u] ^ right[index + 5u]) |
            (left[index + 6u] ^ right[index + 6u]) |
            (left[index + 7u] ^ right[index + 7u]);
        if (changed != 0u) return 1;
    }}
    return 0;
}}
static void copy_page(U8* target, const U8* source) {{
    U32* output = (U32*)target;
    const U32* input = (const U32*)source;
    U32 index;
    for (index = 0; index < 1024u; index += 8u) {{
        output[index] = input[index];
        output[index + 1u] = input[index + 1u];
        output[index + 2u] = input[index + 2u];
        output[index + 3u] = input[index + 3u];
        output[index + 4u] = input[index + 4u];
        output[index + 5u] = input[index + 5u];
        output[index + 6u] = input[index + 6u];
        output[index + 7u] = input[index + 7u];
    }}
}}
static void publish_pages_and_dirty(void) {{
    U32 index;
    U8* dirty = g_exchange->pages + {len(page_addresses)}u * 4096u;
    for (index = 0; index < sizeof(kPages) / sizeof(kPages[0]); ++index) {{
        U8* input = g_exchange->pages + index * 4096u;
        U8* output = (U8*)kPages[index];
        dirty[index] = (U8)page_changed(input, output);
        copy_page(input, output);
    }}
}}
"""
    if copy_helper_end not in source:
        raise Ia32BackendError("Phase-3 worker lost its memory-publication seam")
    source = source.replace(copy_helper_end, native_publication, 1)
    guest_call = f"""        if (!QueryPerformanceCounter(&started)) protocol_failure(30u, exchange->eip);
        ((void (__cdecl *)(void))0x{START_THUNK_ADDRESS:08X}u)();
        QueryPerformanceCounter(&finished);
"""
    guarded_call = f"""        if (!QueryPerformanceCounter(&started)) protocol_failure(30u, exchange->eip);
        g_guest_faulted = 0;
        g_recovery_eip = (U32)&&guest_fault_recovery;
        ((void (__cdecl *)(void))0x{START_THUNK_ADDRESS:08X}u)();
guest_fault_recovery:
        g_recovery_eip = 0u;
        QueryPerformanceCounter(&finished);
        if (g_guest_faulted) {{
            exchange->elapsed_ticks = finished - started;
            publish_pages_and_dirty();
            if (!SetEvent(g_response_event)) ExitProcess(26);
            continue;
        }}
"""
    if guest_call not in source:
        raise Ia32BackendError("Phase-3 worker lost its guest-command SEH seam")
    source = source.replace(guest_call, guarded_call, 1)
    setup = "    SetUnhandledExceptionFilter(record_exception);\n"
    if setup not in source:
        raise Ia32BackendError("Phase-3 worker lost its exception setup seam")
    source = source.replace(
        setup,
        "    if (!AddVectoredExceptionHandler(1u, record_exception)) ExitProcess(27);\n",
        1,
    )
    output_copy = """        for (index = 0; index < sizeof(kPages) / sizeof(kPages[0]); ++index)
            copy_bytes(exchange->pages + index * 4096u, (void*)kPages[index], 4096u);
"""
    if output_copy not in source:
        raise Ia32BackendError("Phase-3 worker lost its output-page seam")
    source = source.replace(output_copy, "        publish_pages_and_dirty();\n", 1)
    return source


def _phase3_fault_preflight_source(
    source: str,
    architecture_plan: _Ia32ArchitecturePlan,
) -> str:
    sites = architecture_plan.architecture_map.get("recoverable_fault_sites", [])
    if not isinstance(sites, list) or not sites:
        return source
    if len(sites) != 1 or not isinstance(sites[0], dict):
        raise Ia32BackendError(
            "Phase-3 recoverable fault preflight currently requires one statically reached site"
        )
    site = sites[0]
    seam = f"""        exchange->status = {EXCHANGE_STATUS_RUNNING}u;
        exchange->error = 0u;
"""
    preflight = f"""        exchange->status = {EXCHANGE_STATUS_RUNNING}u;
        exchange->error = 0u;
        exchange->status = {WORKER_STATUS_FAULT}u;
        exchange->error = 0x{int(site["exception_code"]):08X}u;
        exchange->reserved = 0x{int(site["address"]):08X}u;
        exchange->elapsed_ticks = {int(site["access_type"])}u;
        exchange->qpc_frequency = 0x{int(site["access_address"]):08X}u;
        exchange->eip = 0x{int(site["address"]):08X}u;
        if (!SetEvent(g_response_event)) ExitProcess(26);
        continue;
"""
    if seam not in source:
        raise Ia32BackendError("Phase-3 worker lost its recoverable-fault preflight seam")
    return source.replace(seam, preflight, 1)


def _service_descriptor_initializer(service: _HostServiceThunk) -> str:
    calling_convention = (
        HOST_SERVICE_CALL_STDCALL
        if service.calling_convention == "stdcall"
        else HOST_SERVICE_CALL_CDECL
    )
    execution = (
        HOST_SERVICE_EXECUTION_NATIVE32
        if service.execution == "native32"
        else HOST_SERVICE_EXECUTION_NATIVE64_BROKER
    )
    values = (
        service.target,
        service.kind,
        HOST_SERVICE_BOUNDARIES[service.boundary],
        execution,
        calling_convention,
        service.argument_count,
        service.stack_cleanup_bytes,
        service.value,
        _u32(service.memory_write_argument),
        service.memory_write_value,
        _u32(service.callback_argument),
        _u32(service.callback_context_argument),
        service.callback_value,
        service.callback_stack_cleanup_bytes,
        service.runtime_kind,
        service.runtime_value,
    )
    return "{" + ",".join(f"0x{value:08X}u" for value in values) + "}"


def _phase4_service_offsets(page_count: int) -> tuple[int, int]:
    broker_offset = EXCHANGE_HEADER_SIZE + page_count * PAGE_SIZE + page_count
    trace_offset = broker_offset + HOST_SERVICE_BROKER_CONTROL_SIZE
    return broker_offset, trace_offset


def _phase4_c_source(
    *,
    page_addresses: Sequence[int],
    mapped_page_addresses: Sequence[int],
    guest_virtual_size: int,
    code_spans: Sequence[tuple[int, bytes]],
    stop_eip: int,
    host_service_thunks: Sequence[_HostServiceThunk] = (),
    audited_host_services: bool = True,
    service_state: Mapping[str, Any] | None = None,
    detached_guest: bool = False,
) -> str:
    """Emit the Phase-4 audited service dispatcher and native broker client."""

    if (
        AUDITED_SERVICE_THUNK_BASE + len(host_service_thunks) * AUDITED_SERVICE_THUNK_STRIDE
        > ARCHITECTURE_REWRITE_THUNK_BASE
    ):
        raise Ia32BackendError(
            "Phase-4 registered-service bodies overlap the architecture rewrite thunks"
        )
    ordered_targets = sorted(service.target for service in host_service_thunks)
    for previous, current in zip(ordered_targets, ordered_targets[1:]):
        if current < previous + 5:
            raise Ia32BackendError(
                f"Phase-4 service entry thunks overlap at 0x{previous:08X} and 0x{current:08X}"
            )
    for index, service in enumerate(host_service_thunks):
        if len(_audited_service_body(index, service)) > AUDITED_SERVICE_THUNK_STRIDE:
            raise Ia32BackendError(
                f"Phase-4 service body for {service.shim_name} exceeds its static slot"
            )
    source = _phase3_c_source(
        page_addresses=page_addresses,
        mapped_page_addresses=mapped_page_addresses,
        guest_virtual_size=guest_virtual_size,
        code_spans=code_spans,
        stop_eip=stop_eip,
        host_service_thunks=host_service_thunks,
        audited_host_services=audited_host_services,
        detached_guest=detached_guest,
    )
    source = source.replace(
        f"exchange->version != {ARCHITECTURE_EXCHANGE_VERSION}u",
        f"exchange->version != {HOST_ABI_EXCHANGE_VERSION}u",
    )
    broker_offset, trace_offset = _phase4_service_offsets(len(page_addresses))
    descriptors = ",".join(
        _service_descriptor_initializer(service) for service in host_service_thunks
    )
    descriptor_count = "sizeof(kServiceDescriptors) / sizeof(kServiceDescriptors[0])"
    if not descriptors:
        descriptors = "{0u,0u,0u,0u,0u,0u,0u,0u,0u,0u,0u,0u,0u,0u,0u,0u}"
        descriptor_count = "0u"
    runtime_state = service_state or {}
    semaphore_records = runtime_state.get("native_semaphores", [])
    if not isinstance(semaphore_records, list):
        semaphore_records = []
    semaphore_initializers = []
    for record in semaphore_records:
        if not isinstance(record, Mapping):
            continue
        semaphore_initializers.append(
            "{"
            + ",".join(
                f"0x{_u32(int(value)):08X}u"
                for value in (
                    record.get("handle", 0),
                    record.get("count", 0),
                    record.get("limit", 0),
                )
            )
            + "}"
        )
    if not semaphore_initializers:
        semaphore_initializers.append("{0u,0u,0u}")
    semaphore_count = sum(1 for record in semaphore_records if isinstance(record, Mapping))
    next_pool_address = _u32(int(runtime_state.get("next_pool_address", 0x10000000)))
    type_seam = """typedef struct HostServiceThunk {
    U32 address;
    const U8* code;
    U32 size;
} HostServiceThunk;
"""
    type_block = (
        type_seam
        + f"""typedef struct ServiceDescriptor {{
    U32 target, kind, boundary, execution, calling_convention;
    U32 argument_count, stack_cleanup_bytes, return_value;
    U32 memory_write_argument, memory_write_value;
    U32 callback_argument, callback_context_argument, callback_value;
    U32 callback_stack_cleanup_bytes, runtime_kind, runtime_value;
}} ServiceDescriptor;
typedef struct BrokerControl {{
    U32 magic, version;
    volatile U32 status;
    U32 sequence, service_index, argument_count;
    U32 arguments[4];
    U32 return_value, memory_value, error, reserved[3];
}} BrokerControl;
typedef struct ServiceTraceRecord {{ U32 words[{HOST_SERVICE_TRACE_RECORD_WORDS}]; }} ServiceTraceRecord;
typedef struct WorkloadSemaphore {{ U32 handle, count, limit; }} WorkloadSemaphore;
typedef struct WorkloadAllocation {{ U32 address, size, active; }} WorkloadAllocation;
static const ServiceDescriptor kServiceDescriptors[] = {{{descriptors}}};
static WorkloadSemaphore g_workload_semaphores[1024] = {{{",".join(semaphore_initializers)}}};
static U32 g_workload_semaphore_count = {semaphore_count}u;
static WorkloadAllocation g_workload_allocations[1024];
static U32 g_workload_allocation_count;
static U32 g_workload_next_pool = 0x{next_pool_address:08X}u;
static U32 g_workload_next_contiguous = 0x83000000u;
static U32 g_workload_current_irql;
static U32 g_workload_next_object_handle = 0xB2401000u;
static U32 g_workload_av_saved_data_address;
__declspec(dllimport) void __stdcall GetSystemTimeAsFileTime(U64*);
__declspec(dllimport) int __stdcall MultiByteToWideChar(
    U32, U32, const char*, int, U16*, int);
__declspec(dllimport) int __stdcall WideCharToMultiByte(
    U32, U32, const U16*, int, char*, int, const char*, BOOL*);
"""
    )
    if type_seam not in source:
        raise Ia32BackendError("Phase-4 worker lost its host-service type seam")
    source = source.replace(type_seam, type_block, 1)
    global_seam = """static Exchange* g_exchange;
static HANDLE g_response_event;
"""
    global_block = (
        global_seam
        + """static HANDLE g_broker_request_event;
static HANDLE g_broker_response_event;
"""
    )
    if global_seam not in source:
        raise Ia32BackendError("Phase-4 worker lost its native broker global seam")
    source = source.replace(global_seam, global_block, 1)
    main_seam = "void __cdecl mainCRTStartup(void) {\n"
    workload_dispatcher = f"""static U32 workload_align_up(U32 value, U32 alignment) {{
    return (value + alignment - 1u) & ~(alignment - 1u);
}}
static WorkloadAllocation* workload_find_allocation(U32 address) {{
    U32 index;
    for (index = 0u; index < g_workload_allocation_count; ++index) {{
        WorkloadAllocation* allocation = &g_workload_allocations[index];
        if (allocation->active && address >= allocation->address &&
            address - allocation->address < allocation->size)
            return allocation;
    }}
    return 0;
}}
static U32 workload_allocate(U32 size, U32 alignment, U32* cursor) {{
    U32 address;
    U32 commit_address;
    U32 commit_size;
    U32 region_address;
    U32 region_size;
    U32 reserved_size;
    WorkloadAllocation* allocation;
    if (size == 0u) size = 1u;
    if (alignment < 16u) alignment = 16u;
    if (g_workload_allocation_count >= 1024u) return 0u;
    address = workload_align_up(*cursor, alignment);
    reserved_size = workload_align_up(size, 4096u);
    if ((U64)address + reserved_size > 0x100000000ull) return 0u;
    commit_address = address & ~4095u;
    commit_size = workload_align_up(address - commit_address + reserved_size, 4096u);
    if (VirtualAlloc((void*)commit_address, commit_size, 0x00001000u, 0x04u) !=
        (void*)commit_address) {{
        region_address = address & ~0xFFFFu;
        region_size = workload_align_up(
            address - region_address + reserved_size, 0x10000u);
        if (VirtualAlloc((void*)region_address, region_size, 0x00002000u, 0x01u) !=
            (void*)region_address) return 0u;
        if (VirtualAlloc((void*)commit_address, commit_size, 0x00001000u, 0x04u) !=
            (void*)commit_address) return 0u;
    }}
    allocation = &g_workload_allocations[g_workload_allocation_count++];
    allocation->address = address;
    allocation->size = size;
    allocation->active = 1u;
    *cursor = address + reserved_size;
    return address;
}}
static U32 dispatch_workload_service(
    const ServiceDescriptor* service,
    U32 this_pointer,
    U32* arguments
) {{
    U32 index;
    if (service->runtime_kind == {WORKLOAD_SERVICE_SYSTEM_TIME}u) {{
        U64 filetime = 0u;
        GetSystemTimeAsFileTime(&filetime);
        if (arguments[0] != 0u) {{
            *(U32*)arguments[0] = (U32)filetime;
            *(U32*)(arguments[0] + 4u) = (U32)(filetime >> 32u);
        }}
        return (U32)filetime;
    }}
    if (service->runtime_kind == {WORKLOAD_SERVICE_IRQL}u) {{
        U32 previous = g_workload_current_irql;
        if (service->runtime_value == 1u) {{
            if (arguments[0] > g_workload_current_irql)
                g_workload_current_irql = arguments[0];
            return previous;
        }}
        if (service->runtime_value == 2u) {{
            g_workload_current_irql = service->argument_count ? arguments[0] : 0u;
            return 0u;
        }}
        if (service->runtime_value == 3u) {{
            if (g_workload_current_irql < 2u) g_workload_current_irql = 2u;
            return previous;
        }}
    }}
    if (service->runtime_kind == {WORKLOAD_SERVICE_SEMAPHORE}u) {{
        WorkloadSemaphore* semaphore = 0;
        if (service->runtime_value == 3u) return 0u;
        for (index = 0u; index < g_workload_semaphore_count; ++index) {{
            if (g_workload_semaphores[index].handle == arguments[0]) {{
                semaphore = &g_workload_semaphores[index];
                break;
            }}
        }}
        if (!semaphore) return 0xC0000008u;
        if (service->runtime_value == 1u) {{
            U32 previous = semaphore->count;
            U64 released = (U64)semaphore->count + arguments[1];
            semaphore->count = released < semaphore->limit
                ? (U32)released : semaphore->limit;
            if (arguments[2] != 0u) *(U32*)arguments[2] = previous;
            return 0u;
        }}
        if (service->runtime_value == 2u) {{
            if (semaphore->count != 0u) {{
                --semaphore->count;
                return 0u;
            }}
            return 0x00000102u;
        }}
    }}
    if (service->runtime_kind == {WORKLOAD_SERVICE_RUNTIME}u) {{
        if (service->runtime_value == 1u) {{
            U32 length = 0u;
            U32 source = arguments[1];
            if (source != 0u)
                while (length < 0xFFFEu && *(U8*)(source + length) != 0u) ++length;
            if (arguments[0] != 0u) {{
                *(U32*)arguments[0] = length | ((source ? length + 1u : 0u) << 16u);
                *(U32*)(arguments[0] + 4u) = source;
            }}
            return 0u;
        }}
        if (service->runtime_value == 2u) {{
            U32 left = arguments[0], right = arguments[1];
            U32 left_length, right_length, left_buffer, right_buffer;
            BOOL equal;
            if (!left || !right) return 0u;
            left_length = *(U16*)left;
            right_length = *(U16*)right;
            left_buffer = *(U32*)(left + 4u);
            right_buffer = *(U32*)(right + 4u);
            equal = left_length == right_length;
            for (index = 0u; equal && index < left_length; ++index) {{
                U8 left_value = *(U8*)(left_buffer + index);
                U8 right_value = *(U8*)(right_buffer + index);
                if (arguments[2]) {{
                    if (left_value >= 'A' && left_value <= 'Z') left_value += 'a' - 'A';
                    if (right_value >= 'A' && right_value <= 'Z') right_value += 'a' - 'A';
                }}
                equal = left_value == right_value;
            }}
            return equal ? 1u : 0u;
        }}
        if (service->runtime_value == 9u) {{
            U32 source = arguments[0], length = arguments[1];
            U32 pattern = arguments[2], matched = 0u;
            while (source != 0u && matched + 4u <= length &&
                   *(const U32*)(source + matched) == pattern)
                matched += 4u;
            return matched;
        }}
        if (service->runtime_value == 3u) {{
            if (arguments[4] != 0u) *(U32*)arguments[4] = 0u;
            return 0xC0000034u;
        }}
        if (service->runtime_value == 4u) {{
            switch (arguments[0]) {{
            case 0x00000000u: return 0u;
            case 0xC000000Fu:
            case 0xC0000034u: return 2u;
            case 0xC000003Au: return 3u;
            case 0xC0000035u: return 183u;
            case 0xC0000022u: return 5u;
            case 0xC0000008u: return 6u;
            case 0xC000000Du: return 87u;
            case 0xC0000002u: return 120u;
            default: return 1u;
            }}
        }}
        if (service->runtime_value == 5u || service->runtime_value == 6u)
            return 0u;
        if (service->runtime_value == 7u) {{
            U32 destination = arguments[0], source = arguments[1];
            U32 source_length, source_buffer, output_length, required_capacity;
            U32 destination_word, destination_maximum, destination_buffer;
            int converted;
            if (!destination || !source) return 0xC000000Du;
            source_length = *(U16*)source;
            source_buffer = *(U32*)(source + 4u);
            if (source_length && !source_buffer) return 0xC000000Du;
            converted = source_length ? MultiByteToWideChar(
                1252u, 0u, (const char*)source_buffer, (int)source_length, 0, 0) : 0;
            if (source_length && converted <= 0) return 0xC000000Du;
            output_length = (U32)converted * 2u;
            required_capacity = output_length + 2u;
            if (required_capacity > 0xFFFFu) return 0xC00000F0u;
            destination_word = *(U32*)destination;
            destination_maximum = destination_word >> 16u;
            destination_buffer = *(U32*)(destination + 4u);
            if (arguments[2]) {{
                destination_buffer = workload_allocate(
                    required_capacity, 2u, &g_workload_next_pool);
                destination_maximum = required_capacity;
                *(U32*)(destination + 4u) = destination_buffer;
                if (!destination_buffer) {{
                    *(U32*)destination = output_length | (destination_maximum << 16u);
                    return 0xC0000017u;
                }}
            }} else if (!destination_buffer || output_length >= destination_maximum) {{
                *(U32*)destination = (destination_word & 0xFFFF0000u) | output_length;
                return destination_buffer ? 0x80000005u : 0xC000000Du;
            }}
            *(U32*)destination = output_length | (destination_maximum << 16u);
            if (converted) MultiByteToWideChar(
                1252u, 0u, (const char*)source_buffer, (int)source_length,
                (U16*)destination_buffer, converted);
            *(U16*)(destination_buffer + output_length) = 0u;
            return 0u;
        }}
        if (service->runtime_value == 8u) {{
            U32 destination = arguments[0], source = arguments[1];
            U32 source_length, source_buffer, source_count, required_length;
            U32 destination_word, destination_maximum, destination_buffer, output_length;
            U32 status = 0u;
            int converted;
            if (!destination || !source) return 0xC000000Du;
            source_length = *(U16*)source;
            source_buffer = *(U32*)(source + 4u);
            if ((source_length & 1u) || (source_length && !source_buffer))
                return 0xC000000Du;
            source_count = source_length / 2u;
            converted = source_count ? WideCharToMultiByte(
                1252u, 0u, (const U16*)source_buffer, (int)source_count,
                0, 0, "?", 0) : 0;
            if (source_count && converted <= 0) return 0xC000000Du;
            required_length = (U32)converted;
            destination_word = *(U32*)destination;
            destination_maximum = destination_word >> 16u;
            destination_buffer = *(U32*)(destination + 4u);
            if (arguments[2]) {{
                destination_buffer = workload_allocate(
                    required_length + 1u, 1u, &g_workload_next_pool);
                destination_maximum = required_length + 1u;
                *(U32*)(destination + 4u) = destination_buffer;
                if (!destination_buffer) {{
                    *(U32*)destination = required_length | (destination_maximum << 16u);
                    return 0xC0000017u;
                }}
            }} else if (!destination_buffer) {{
                return 0xC000000Du;
            }}
            output_length = required_length;
            if (output_length >= destination_maximum) {{
                if (!destination_maximum) {{
                    *(U32*)destination = destination_word & 0xFFFF0000u;
                    return 0x80000005u;
                }}
                output_length = destination_maximum - 1u;
                status = 0x80000005u;
            }}
            *(U32*)destination = output_length | (destination_maximum << 16u);
            if (output_length) WideCharToMultiByte(
                1252u, 0u, (const U16*)source_buffer, (int)source_count,
                (char*)destination_buffer, (int)output_length, "?", 0);
            *(U8*)(destination_buffer + output_length) = 0u;
            return status;
        }}
    }}
    if (service->runtime_kind == {WORKLOAD_SERVICE_MEMORY}u) {{
        WorkloadAllocation* allocation;
        if (service->runtime_value == 1u) {{
            U32 alignment = arguments[3] ? arguments[3] : 4096u;
            return workload_allocate(arguments[0], alignment,
                                     &g_workload_next_contiguous);
        }}
        if (service->runtime_value == 2u) {{
            U32 base_pointer = arguments[0];
            U32 size_pointer = arguments[2];
            U32 requested_size = size_pointer ? *(U32*)size_pointer : 0u;
            U32 requested_base = base_pointer ? *(U32*)base_pointer : 0u;
            U32 address = 0u;
            if (!requested_size) return 0xC000000Du;
            allocation = requested_base ? workload_find_allocation(requested_base) : 0;
            if (allocation && (arguments[3] & 0x1000u)) address = requested_base;
            else address = workload_allocate(
                requested_size, 4096u, &g_workload_next_pool);
            if (!address) return 0xC000000Du;
            if (base_pointer) *(U32*)base_pointer = address;
            if (size_pointer) *(U32*)size_pointer = requested_size;
            return 0u;
        }}
        if (service->runtime_value == 3u)
            return workload_allocate(arguments[0], 4096u,
                                     &g_workload_next_contiguous);
        if (service->runtime_value == 8u)
            return workload_allocate(arguments[0], 16u, &g_workload_next_pool);
        allocation = workload_find_allocation(arguments[0]);
        if (service->runtime_value == 4u) {{
            if (!allocation) return 0u;
            return arguments[0] >= 0x80000000u && arguments[0] < 0x84000000u
                ? arguments[0] & 0x7FFFFFFFu : arguments[0];
        }}
        if (service->runtime_value == 5u)
            return allocation ? allocation->size : 0u;
        if (service->runtime_value == 6u) {{
            if (allocation) allocation->active = 0u;
            return allocation ? 0u : 0xC000000Du;
        }}
        if (service->runtime_value == 7u) {{
            U64 end = (U64)arguments[0] + arguments[1];
            U64 allocation_end = allocation
                ? (U64)allocation->address + allocation->size : 0u;
            return allocation && end <= allocation_end ? 0u : 0xC000000Du;
        }}
    }}
    if (service->runtime_kind == {WORKLOAD_SERVICE_OFFLINE_KERNEL}u) {{
        U32 value = service->runtime_value;
        if (value == 1u || value == 3u || value == 4u || value == 7u ||
            value == 8u || value == 10u || value == 11u || value == 12u ||
            value == 15u || value == 19u || value == 20u)
            return value == 7u || value == 8u || value == 10u ? 1u : 0u;
        if (value == 2u || value == 5u) {{
            ExitProcess(arguments[0]);
            return 0u;
        }}
        if (value == 6u) {{
            U32 timer = arguments[0];
            U32 previous = timer ? *(U32*)(timer + 4u) : 0u;
            if (timer) *(U32*)(timer + 4u) = 0u;
            return previous != 0u;
        }}
        if (value == 9u) {{
            U64 counter = 0u;
            if (!QueryPerformanceCounter(&counter)) return 0u;
            return (U32)counter;
        }}
        if (value == 13u)
            return workload_find_allocation(arguments[0]) ? 4u : 0u;
        if (value == 14u) {{
            U32 event = workload_allocate(8u, 4u, &g_workload_next_pool);
            if (!event) return 0u;
            *(U32*)event = arguments[0] != 0u;
            *(U32*)(event + 4u) = arguments[1] != 0u;
            return event;
        }}
        if (value == 16u) {{
            if (arguments[4]) {{
                *(U32*)arguments[4] = 0u;
                *(U32*)(arguments[4] + 4u) = 0u;
            }}
            return 0u;
        }}
        if (value == 17u || value == 18u) {{
            WorkloadSemaphore* semaphore = 0;
            for (index = 0u; index < g_workload_semaphore_count; ++index)
                if (g_workload_semaphores[index].handle == arguments[0]) {{
                    semaphore = &g_workload_semaphores[index];
                    break;
                }}
            if (!semaphore) return 0xC0000008u;
            if (value == 17u) {{
                U32 previous = semaphore->count;
                semaphore->count = semaphore->limit ? 1u : 0u;
                return previous;
            }}
            if (semaphore->count) {{
                --semaphore->count;
                return 0u;
            }}
            return 0x00000102u;
        }}
        if (value == 21u) {{
            U8* permutation = (U8*)arguments[0];
            U32 key_size = arguments[1], key = arguments[2], j = 0u;
            if (!permutation) return 0u;
            for (index = 0u; index < 256u; ++index) permutation[index] = (U8)index;
            for (index = 0u; index < 256u; ++index) {{
                U8 temporary;
                j = (j + permutation[index] +
                     (key_size ? *(U8*)(key + index % key_size) : 0u)) & 255u;
                temporary = permutation[index];
                permutation[index] = permutation[j];
                permutation[j] = temporary;
            }}
            *(U32*)(arguments[0] + 256u) = 0u;
            *(U32*)(arguments[0] + 260u) = 0u;
            return 0u;
        }}
        if (value == 22u) {{
            U8* permutation = (U8*)arguments[0];
            U32 size = arguments[1], payload = arguments[2], i, j;
            if (!permutation) return 0u;
            i = *(U32*)(arguments[0] + 256u) & 255u;
            j = *(U32*)(arguments[0] + 260u) & 255u;
            for (index = 0u; index < size; ++index) {{
                U8 temporary;
                i = (i + 1u) & 255u;
                j = (j + permutation[i]) & 255u;
                temporary = permutation[i];
                permutation[i] = permutation[j];
                permutation[j] = temporary;
                *(U8*)(payload + index) ^=
                    permutation[(permutation[i] + permutation[j]) & 255u];
            }}
            *(U32*)(arguments[0] + 256u) = i;
            *(U32*)(arguments[0] + 260u) = j;
            return 0u;
        }}
        if (value == 26u) {{
            U8* key = (U8*)arguments[0];
            if (key) for (index = 0u; index < 8u; ++index) {{
                U8 byte = key[index] & 0xFEu;
                U8 bits = byte;
                bits ^= bits >> 4u; bits ^= bits >> 2u; bits ^= bits >> 1u;
                key[index] = byte | ((bits & 1u) ^ 1u);
            }}
            return 0u;
        }}
        if (value == 27u) {{
            if (arguments[0] && arguments[1])
                copy_bytes((void*)arguments[0], (void*)arguments[1], 8u);
            return 0u;
        }}
        if (value == 23u || value == 24u || value == 25u || value == 28u)
            return 0u;
    }}
    if (service->runtime_kind == {WORKLOAD_SERVICE_BOOTSTRAP}u) {{
        if (service->runtime_value == 1u) {{
            if (arguments[3]) *(U32*)arguments[3] = 0x00400101u;
            return 0u;
        }}
        if (service->runtime_value == 2u)
            return 0x20u + ((arguments[0] ? arguments[0] : arguments[1]) & 0x1Fu);
        if (service->runtime_value == 3u) {{
            if (!arguments[5] && arguments[3])
                for (index = 0u; index < arguments[4]; ++index)
                    *(U8*)(arguments[3] + index) = 0u;
            return 0u;
        }}
        if (service->runtime_value == 4u) {{
            const U32 device_size = 0x30u;
            U32 device = workload_allocate(
                device_size + arguments[1], 16u, &g_workload_next_pool);
            U32 extension;
            if (!device) return 0xC000009Au;
            extension = device + device_size;
            for (index = 0u; index < device_size + arguments[1]; ++index)
                *(U8*)(device + index) = 0u;
            *(U16*)device = 3u;
            *(U16*)(device + 2u) = (U16)device_size;
            *(U32*)(device + 4u) = 1u;
            *(U32*)(device + 8u) = arguments[0];
            *(U32*)(device + 0xCu) = device;
            *(U32*)(device + 0x14u) = arguments[4] ? 8u : 0u;
            *(U32*)(device + 0x18u) = extension;
            *(U8*)(device + 0x1Cu) = (U8)arguments[3];
            *(U8*)(device + 0x1Eu) = 1u;
            if (arguments[5]) *(U32*)arguments[5] = device;
            return 0u;
        }}
        if (service->runtime_value == 5u) return 1u;
        if (service->runtime_value == 6u || service->runtime_value == 7u)
            return 0u;
        if (service->runtime_value == 8u)
            return workload_allocate(arguments[0], 4096u,
                                     &g_workload_next_contiguous);
        if (service->runtime_value == 9u) {{
            for (index = 0u; index < g_workload_semaphore_count; ++index)
                if (g_workload_semaphores[index].handle == arguments[0]) {{
                    g_workload_semaphores[index].handle = 0u;
                    g_workload_semaphores[index].count = 0u;
                    g_workload_semaphores[index].limit = 0u;
            }}
            return 0u;
        }}
        if (service->runtime_value == 10u) {{
            U32 event = arguments[0];
            U32 previous = event != 0u ? *(U32*)(event + 4u) : 0u;
            if (event != 0u) *(U32*)(event + 4u) = 1u;
            return previous;
        }}
        if (service->runtime_value == 17u) return g_workload_av_saved_data_address;
        if (service->runtime_value == 18u) return 0u;
        if (service->runtime_value == 19u) {{
            g_workload_av_saved_data_address = arguments[0];
            return 0u;
        }}
    }}
    if (service->runtime_kind == {WORKLOAD_SERVICE_TITLE}u) {{
        U32 payload = this_pointer ? *(U32*)(this_pointer + 0x30u) : 0u;
        U32 payload_size = this_pointer ? *(U32*)(this_pointer + 0x34u) : 0u;
        U32 position = this_pointer ? *(U32*)(this_pointer + 0x38u) : 0u;
        if (service->runtime_value == 6u) {{
            if (this_pointer != 0u) *(U32*)(this_pointer + 0x2Cu) = 0u;
            return 1u;
        }}
        if (service->runtime_value == 7u) {{
            if (payload != 0u) *(U32*)(this_pointer + 0x2Cu) = 1u;
            return payload != 0u ? 1u : 0u;
        }}
        if (service->runtime_value == 8u) {{
            U32 available = position < payload_size ? payload_size - position : 0u;
            U32 copied = arguments[1] < available ? arguments[1] : available;
            U8* destination = (U8*)arguments[0];
            U8* source_bytes = (U8*)(payload + position);
            if (destination != 0 && payload != 0u) {{
                for (index = 0u; index < copied; ++index)
                    destination[index] = source_bytes[index];
            }} else {{
                copied = 0u;
            }}
            position += copied;
            if (this_pointer != 0u) {{
                *(U32*)(this_pointer + 0x18u) = position;
                *(U32*)(this_pointer + 0x1Cu) = 0u;
                *(U32*)(this_pointer + 0x38u) = position;
                *(U32*)(this_pointer + 0x2Cu) = 1u;
            }}
            return copied;
        }}
        if (service->runtime_value == 9u) {{
            U64 bits = ((U64)arguments[1] << 32u) | arguments[0];
            long long offset = (long long)bits;
            long long base = arguments[2] == 0u ? 0ll
                : arguments[2] == 1u ? (long long)position
                : (long long)payload_size;
            long long requested = base + offset;
            if (requested < 0ll) requested = 0ll;
            if ((U64)requested > payload_size) requested = payload_size;
            position = (U32)requested;
            if (this_pointer != 0u) {{
                *(U32*)(this_pointer + 0x18u) = position;
                *(U32*)(this_pointer + 0x1Cu) = 0u;
                *(U32*)(this_pointer + 0x38u) = position;
                *(U32*)(this_pointer + 0x2Cu) = 1u;
            }}
            return position;
        }}
    }}
    protocol_failure(47u, service->target);
    return 0u;
}}
"""
    dispatcher = f"""static U32 __cdecl dispatch_host_service(
    U32 service_index, U32 this_pointer, U32* arguments
) {{
    U32 index;
    U32 result;
    U32 memory_value;
    U32* trace_header = (U32*)((U8*)g_exchange + {trace_offset}u);
    ServiceTraceRecord* records = (ServiceTraceRecord*)(trace_header + 4u);
    ServiceTraceRecord* record;
    const ServiceDescriptor* service;
    BrokerControl* broker = (BrokerControl*)((U8*)g_exchange + {broker_offset}u);
    if (service_index >= {descriptor_count})
        protocol_failure(40u, service_index);
    if (trace_header[2] != {HOST_SERVICE_TRACE_CAPACITY}u || trace_header[3] != 1u)
        protocol_failure(41u, service_index);
    if (trace_header[0] >= {HOST_SERVICE_TRACE_CAPACITY}u) {{
        trace_header[1] = 1u;
        protocol_failure(42u, service_index);
    }}
    service = &kServiceDescriptors[service_index];
    record = &records[trace_header[0]];
    for (index = 0; index < {HOST_SERVICE_TRACE_RECORD_WORDS}u; ++index)
        record->words[index] = 0u;
    record->words[0] = trace_header[0];
    record->words[1] = service->target;
    record->words[2] = service->kind;
    record->words[3] = service->boundary;
    record->words[4] = service->execution;
    record->words[5] = service->calling_convention;
    record->words[6] = service->argument_count;
    record->words[7] = service->stack_cleanup_bytes;
    record->words[8] = (U32)arguments - 4u;
    record->words[9] = (U32)arguments + service->stack_cleanup_bytes;
    for (index = 0; index < service->argument_count && index < 5u; ++index)
        record->words[10u + index] = arguments[index];
    result = service->return_value;
    memory_value = service->memory_write_value;
    if (service->kind == {NATIVE_HOST_SERVICE_WORKLOAD_ABI}u)
        result = dispatch_workload_service(service, this_pointer, arguments);
    if (service->execution == {HOST_SERVICE_EXECUTION_NATIVE64_BROKER}u) {{
        if (broker->magic != 0x{HOST_SERVICE_BROKER_MAGIC:08X}u ||
            broker->version != {HOST_SERVICE_BROKER_VERSION}u)
            protocol_failure(43u, service_index);
        broker->service_index = service_index;
        broker->argument_count = service->argument_count;
        for (index = 0; index < 4u; ++index)
            broker->arguments[index] = index < service->argument_count ? arguments[index] : 0u;
        broker->error = 0u;
        broker->sequence += 1u;
        broker->status = {HOST_SERVICE_BROKER_STATUS_REQUEST}u;
        if (!SetEvent(g_broker_request_event) ||
            WaitForSingleObject(g_broker_response_event, 0xFFFFFFFFu) != 0u)
            protocol_failure(44u, service_index);
        if (broker->status != {HOST_SERVICE_BROKER_STATUS_RESPONSE}u || broker->error)
            protocol_failure(45u, service_index);
        result = broker->return_value;
        memory_value = broker->memory_value;
    }}
    if (service->memory_write_argument != 0xFFFFFFFFu) {{
        U32* target = (U32*)arguments[service->memory_write_argument];
        record->words[16] = (U32)target;
        record->words[17] = *target;
        *target = memory_value;
        record->words[18] = *target;
    }}
    if (service->kind == {NATIVE_HOST_SERVICE_CALLBACK}u) {{
        U32 callback_target = arguments[service->callback_argument];
        U32 callback_context = arguments[service->callback_context_argument];
        U32 callback_return;
        if (service->callback_stack_cleanup_bytes == 8u)
            callback_return = ((U32 (__stdcall *)(U32, U32))callback_target)(
                callback_context, service->callback_value);
        else
            callback_return = ((U32 (__cdecl *)(U32, U32))callback_target)(
                callback_context, service->callback_value);
        record->words[19] = callback_target;
        record->words[20] = callback_return;
        record->words[21] = 1u;
    }}
    record->words[15] = result;
    record->words[22] = (service->runtime_kind << 16u) |
        (service->runtime_value & 0xFFFFu);
    record->words[23] = 0u;
    trace_header[0] += 1u;
    return result;
}}
"""
    if main_seam not in source:
        raise Ia32BackendError("Phase-4 worker lost its main-entry seam")
    source = source.replace(main_seam, workload_dispatcher + dispatcher + main_seam, 1)
    local_seam = """    char request_name[192];
    char response_name[192];
"""
    local_block = (
        local_seam
        + """    char broker_request_name[192];
    char broker_response_name[192];
"""
    )
    if local_seam not in source:
        raise Ia32BackendError("Phase-4 worker lost its event-name local seam")
    source = source.replace(local_seam, local_block, 1)
    event_seam = """    event_name(request_name, "-request");
    event_name(response_name, "-response");
    request_event = OpenEventA(SYNCHRONIZE, 0, request_name);
    g_response_event = OpenEventA(EVENT_MODIFY_STATE, 0, response_name);
    if (!request_event || !g_response_event) ExitProcess(20);
"""
    event_block = """    event_name(request_name, "-request");
    event_name(response_name, "-response");
    event_name(broker_request_name, "-broker-request");
    event_name(broker_response_name, "-broker-response");
    request_event = OpenEventA(SYNCHRONIZE, 0, request_name);
    g_response_event = OpenEventA(EVENT_MODIFY_STATE, 0, response_name);
    g_broker_request_event = OpenEventA(EVENT_MODIFY_STATE, 0, broker_request_name);
    g_broker_response_event = OpenEventA(SYNCHRONIZE, 0, broker_response_name);
    if (!request_event || !g_response_event ||
        !g_broker_request_event || !g_broker_response_event) ExitProcess(20);
"""
    if event_seam not in source:
        raise Ia32BackendError("Phase-4 worker lost its event-open seam")
    source = source.replace(event_seam, event_block, 1)
    dispatch_pointer_seam = f"""    copy_bytes((void*)0x{EXIT_THUNK_ADDRESS:08X}u, kExitThunk, sizeof(kExitThunk));
"""
    dispatch_pointer_block = (
        dispatch_pointer_seam
        + f"""    *(U32*)0x{SCRATCH_ADDRESS + SCRATCH_SERVICE_DISPATCH_OFFSET:08X}u =
        (U32)&dispatch_host_service;
    *(U8*)0x{SERVICE_DISPATCH_RELAY_ADDRESS:08X}u = 0xE9u;
    *(U32*)0x{SERVICE_DISPATCH_RELAY_ADDRESS + 1:08X}u =
        (U32)&dispatch_host_service - 0x{SERVICE_DISPATCH_RELAY_ADDRESS + 5:08X}u;
"""
    )
    if dispatch_pointer_seam not in source:
        raise Ia32BackendError("Phase-4 worker lost its dispatcher-pointer seam")
    source = source.replace(dispatch_pointer_seam, dispatch_pointer_block, 1)
    stop_seam = f"""        if (command == {WORKER_COMMAND_STOP}u) {{
            exchange->error = 0u;
"""
    stop_block = f"""        if (command == {WORKER_COMMAND_STOP}u) {{
            BrokerControl* broker = (BrokerControl*)((U8*)exchange + {broker_offset}u);
            broker->status = {HOST_SERVICE_BROKER_STATUS_STOP}u;
            if (!SetEvent(g_broker_request_event) ||
                WaitForSingleObject(g_broker_response_event, 0xFFFFFFFFu) != 0u)
                protocol_failure(46u, exchange->eip);
            exchange->error = 0u;
"""
    if stop_seam not in source:
        raise Ia32BackendError("Phase-4 worker lost its broker-stop seam")
    return source.replace(stop_seam, stop_block, 1)


def _phase5_scheduler_offsets(page_count: int) -> tuple[int, int, int]:
    _broker_offset, trace_offset = _phase4_service_offsets(page_count)
    scheduler_offset = (
        trace_offset
        + HOST_SERVICE_TRACE_HEADER_SIZE
        + HOST_SERVICE_TRACE_CAPACITY * HOST_SERVICE_TRACE_RECORD_SIZE
    )
    lanes_offset = scheduler_offset + RESIDENT_SCHEDULER_HEADER_SIZE
    traces_offset = (
        lanes_offset + RESIDENT_SCHEDULER_LANE_COUNT * RESIDENT_SCHEDULER_LANE_RECORD_SIZE
    )
    return scheduler_offset, lanes_offset, traces_offset


def _phase7_memory_offset(page_count: int) -> int:
    _scheduler_offset, _lanes_offset, traces_offset = _phase5_scheduler_offsets(page_count)
    return traces_offset + (
        RESIDENT_SCHEDULER_TRACE_CAPACITY * RESIDENT_SCHEDULER_TRACE_RECORD_SIZE
    )


def _phase5_c_source(
    *,
    page_addresses: Sequence[int],
    mapped_page_addresses: Sequence[int],
    guest_virtual_size: int,
    code_spans: Sequence[tuple[int, bytes]],
    stop_eip: int,
    host_service_thunks: Sequence[_HostServiceThunk] = (),
    audited_host_services: bool = True,
    scheduler_plan: _Ia32SchedulerPlan,
    service_state: Mapping[str, Any] | None = None,
    detached_guest: bool = False,
) -> str:
    """Emit the Phase-5 three-lane resident scheduler inside the IA-32 worker."""

    source = _phase4_c_source(
        page_addresses=page_addresses,
        mapped_page_addresses=mapped_page_addresses,
        guest_virtual_size=guest_virtual_size,
        code_spans=code_spans,
        stop_eip=stop_eip,
        host_service_thunks=host_service_thunks,
        audited_host_services=audited_host_services,
        service_state=service_state,
        detached_guest=detached_guest,
    )
    source = source.replace(
        f"exchange->version != {HOST_ABI_EXCHANGE_VERSION}u",
        f"exchange->version != {RESIDENT_SCHEDULER_EXCHANGE_VERSION}u",
    )
    scheduler_offset, lanes_offset, traces_offset = _phase5_scheduler_offsets(len(page_addresses))
    lane_initializers = ",".join(
        "{"
        + ",".join(
            f"0x{value:08X}u"
            for value in (
                lane.lane_id,
                lane.initial_eip,
                lane.tls_base,
                lane.initial_state,
                *lane.registers,
                lane.eflags,
                lane.wait_object,
            )
        )
        + "}"
        for lane in scheduler_plan.lanes
    )
    step_initializers = ",".join(
        "{"
        + ",".join(
            f"0x{value:08X}u"
            for value in (
                step.sequence,
                step.lane_id,
                step.entry_eip,
                step.next_eip,
                step.exit_kind,
                step.wake_lane_id,
                step.wait_object,
            )
        )
        + "}"
        for step in scheduler_plan.steps
    )
    type_seam = (
        f"typedef struct ServiceTraceRecord {{ U32 words["
        f"{HOST_SERVICE_TRACE_RECORD_WORDS}]; }} ServiceTraceRecord;\n"
    )
    type_block = (
        type_seam
        + f"""typedef struct SchedulerLaneInitial {{
    U32 id, eip, tls_base, state, registers[8], eflags, wait_object;
}} SchedulerLaneInitial;
typedef struct SchedulerLane {{
    U32 id, state, tls_base, activation_count, eip, eflags, registers[8];
    U32 last_exit, fault_code, wait_object, fault_eip, fault_address, reserved;
    U8 fxsave[512];
}} SchedulerLane;
typedef struct SchedulerStep {{
    U32 sequence, lane_id, entry_eip, next_eip, exit_kind, wake_lane_id, wait_object;
}} SchedulerStep;
typedef struct SchedulerTrace {{ U32 words[{RESIDENT_SCHEDULER_TRACE_RECORD_WORDS}]; }} SchedulerTrace;
typedef char SchedulerLaneSizeCheck[
    (sizeof(SchedulerLane) == {RESIDENT_SCHEDULER_LANE_RECORD_SIZE}u) ? 1 : -1];
static const SchedulerLaneInitial kSchedulerLanes[] = {{{lane_initializers}}};
static const SchedulerStep kSchedulerSteps[] = {{{step_initializers}}};
"""
    )
    if type_seam not in source:
        raise Ia32BackendError("Phase-5 worker lost its scheduler type seam")
    source = source.replace(type_seam, type_block, 1)
    main_seam = "void __cdecl mainCRTStartup(void) {\n"
    scheduler_helpers = f"""static U32 scheduler_fnv1a(const U8* payload, U32 size) {{
    U32 index;
    U32 value = 2166136261u;
    for (index = 0u; index < size; ++index) {{
        value ^= payload[index];
        value *= 16777619u;
    }}
    return value;
}}
static void initialize_scheduler_lanes(SchedulerLane* lanes, Exchange* exchange) {{
    U32 lane_index;
    U32 register_index;
    for (lane_index = 0u; lane_index < {RESIDENT_SCHEDULER_LANE_COUNT}u;
         ++lane_index) {{
        SchedulerLane* lane = &lanes[lane_index];
        const SchedulerLaneInitial* initial = &kSchedulerLanes[lane_index];
        lane->id = initial->id;
        lane->state = initial->state;
        lane->tls_base = initial->tls_base;
        lane->activation_count = 0u;
        lane->eip = initial->eip;
        lane->eflags = initial->eflags;
        for (register_index = 0u; register_index < 8u; ++register_index)
            lane->registers[register_index] = initial->registers[register_index];
        lane->last_exit = 0u;
        lane->fault_code = 0u;
        lane->wait_object = initial->wait_object;
        lane->fault_eip = 0u;
        lane->fault_address = 0u;
        lane->reserved = 0u;
        copy_bytes(lane->fxsave, exchange->fxsave, 512u);
    }}
}}
static void publish_scheduler_lanes(const SchedulerLane* lanes) {{
    U32 lane_index;
    for (lane_index = 0u; lane_index < {RESIDENT_SCHEDULER_LANE_COUNT}u;
         ++lane_index)
        copy_bytes(
            (U8*)g_exchange + {lanes_offset}u
                + lane_index * {RESIDENT_SCHEDULER_LANE_RECORD_SIZE}u,
            &lanes[lane_index], {RESIDENT_SCHEDULER_LANE_RECORD_SIZE}u);
}}
"""
    if main_seam not in source:
        raise Ia32BackendError("Phase-5 worker lost its main-entry seam")
    source = source.replace(main_seam, scheduler_helpers + main_seam, 1)
    validation_seam = f"""        if (command != {WORKER_COMMAND_ENTER}u &&
            command != {WORKER_COMMAND_RESUME}u &&
            command != {WORKER_COMMAND_SERVICE_RETURN}u)
            protocol_failure(25u, exchange->eip);
"""
    validation_block = f"""        if (command != {WORKER_COMMAND_ENTER}u &&
            command != {WORKER_COMMAND_RESUME}u &&
            command != {WORKER_COMMAND_SERVICE_RETURN}u &&
            command != {WORKER_COMMAND_SCHEDULE}u)
            protocol_failure(25u, exchange->eip);
"""
    if validation_seam not in source:
        raise Ia32BackendError("Phase-5 worker lost its command-validation seam")
    source = source.replace(validation_seam, validation_block, 1)
    scheduler_detached_copy = (
        """            for (index = 0u;
                 index < sizeof(kDetachedGuestSpans) / sizeof(kDetachedGuestSpans[0]);
                 ++index)
                copy_bytes((void*)kDetachedGuestSpans[index].address,
                           kDetachedGuestSpans[index].code,
                           kDetachedGuestSpans[index].size);
"""
        if detached_guest
        else ""
    )
    scheduler_detached_flush = (
        """            for (index = 0u;
                 index < sizeof(kDetachedGuestSpans) / sizeof(kDetachedGuestSpans[0]);
                 ++index)
                FlushInstructionCache((HANDLE)-1,
                                      (void*)kDetachedGuestSpans[index].address,
                                      kDetachedGuestSpans[index].size);
"""
        if detached_guest
        else ""
    )
    dispatch_seam = f"""        exchange->status = {EXCHANGE_STATUS_RUNNING}u;
        exchange->error = 0u;
"""
    dispatch_block = f"""        if (command == {WORKER_COMMAND_SCHEDULE}u) {{
            U32* scheduler_header = (U32*)((U8*)exchange + {scheduler_offset}u);
            U32* service_header = (U32*)((U8*)exchange +
                {_phase4_service_offsets(len(page_addresses))[1]}u);
            SchedulerLane lanes[{RESIDENT_SCHEDULER_LANE_COUNT}];
            SchedulerTrace* traces = (SchedulerTrace*)((U8*)exchange + {traces_offset}u);
            U32 step_index;
            U32 previous_lane = 0xFFFFFFFFu;
            if (scheduler_header[0] != 0x{RESIDENT_SCHEDULER_MAGIC:08X}u ||
                scheduler_header[1] != {RESIDENT_SCHEDULER_VERSION}u ||
                scheduler_header[2] != {RESIDENT_SCHEDULER_LANE_COUNT}u ||
                scheduler_header[3] != {len(scheduler_plan.steps)}u ||
                scheduler_header[5] != {RESIDENT_SCHEDULER_TRACE_CAPACITY}u)
                protocol_failure(50u, scheduler_header[0]);
            exchange->status = {EXCHANGE_STATUS_RUNNING}u;
            exchange->error = 0u;
            for (index = 0; index < sizeof(kPages) / sizeof(kPages[0]); ++index)
                copy_bytes((void*)kPages[index], exchange->pages + index * 4096u, 4096u);
{scheduler_detached_copy}            install_host_service_thunks();

            FlushInstructionCache((HANDLE)-1, (void*)0x{GUEST_SECTION_ADDRESS:08X}u,
                                  0x{guest_virtual_size:X}u);
{scheduler_detached_flush}
            for (index = 4u; index < 16u; ++index) scheduler_header[index] = 0u;
            scheduler_header[5] = {RESIDENT_SCHEDULER_TRACE_CAPACITY}u;
            initialize_scheduler_lanes(lanes, exchange);
            if (!QueryPerformanceCounter(&started)) protocol_failure(30u, exchange->eip);
            for (step_index = 0u; step_index < {len(scheduler_plan.steps)}u; ++step_index) {{
                const SchedulerStep* step = &kSchedulerSteps[step_index];
                SchedulerLane* lane;
                SchedulerTrace* trace = &traces[step_index];
                U32 before_state;
                U32 before_tls;
                U32 register_index;
                U32 service_begin;
                U32 service_end;
                U32 actual_exit;
                if (step->sequence != step_index ||
                    step->lane_id >= {RESIDENT_SCHEDULER_LANE_COUNT}u)
                    protocol_failure(51u, step_index);
                lane = &lanes[step->lane_id];
                if ((lane->state != {RESIDENT_LANE_READY}u &&
                     lane->state != {RESIDENT_LANE_RUNNING}u) ||
                    lane->eip != step->entry_eip)
                    protocol_failure(52u, lane->eip);
                before_state = lane->state;
                before_tls = *(U32*)lane->tls_base;
                service_begin = service_header[0];
                lane->state = {RESIDENT_LANE_RUNNING}u;
                copy_bytes((void*)0x{scheduler_plan.active_tls_base:08X}u,
                           (void*)lane->tls_base, 4096u);
                for (register_index = 0u; register_index < 8u; ++register_index)
                    *(U32*)(0x{SCRATCH_ADDRESS:08X}u + register_index * 4u) =
                        lane->registers[register_index];
                *(U32*)0x{SCRATCH_ADDRESS + SCRATCH_EFLAGS_OFFSET:08X}u = lane->eflags;
                *(U32*)0x{SCRATCH_ADDRESS + SCRATCH_ENTRY_EIP_OFFSET:08X}u = lane->eip;
                copy_bytes((void*)0x{SCRATCH_ADDRESS + SCRATCH_FXSAVE_OFFSET:08X}u,
                           lane->fxsave, 512u);
                exchange->eip = lane->eip;
                g_guest_faulted = 0;
                g_recovery_eip = (U32)&&scheduler_lane_recovery;
                ((void (__cdecl *)(void))0x{START_THUNK_ADDRESS:08X}u)();
scheduler_lane_recovery:
                g_recovery_eip = 0u;
                if (g_guest_faulted) {{
                    for (register_index = 0u; register_index < 8u; ++register_index)
                        lane->registers[register_index] = exchange->registers[register_index];
                    lane->eflags = exchange->eflags;
                    lane->eip = exchange->eip;
                    copy_bytes(lane->fxsave, exchange->fxsave, 512u);
                    lane->fault_code = exchange->error;
                    lane->fault_eip = exchange->eip;
                    lane->fault_address = (U32)exchange->qpc_frequency;
                    lane->state = {RESIDENT_LANE_FAULTED}u;
                    lane->last_exit = {RESIDENT_EXIT_FAULT}u;
                    scheduler_header[10] += 1u;
                    actual_exit = {RESIDENT_EXIT_FAULT}u;
                    exchange->status = {EXCHANGE_STATUS_RUNNING}u;
                    exchange->error = 0u;
                }} else {{
                    for (register_index = 0u; register_index < 8u; ++register_index)
                        lane->registers[register_index] =
                            *(U32*)(0x{SCRATCH_ADDRESS:08X}u + register_index * 4u);
                    lane->eflags = *(U32*)0x{SCRATCH_ADDRESS + SCRATCH_EFLAGS_OFFSET:08X}u;
                    lane->eip = step->next_eip;
                    copy_bytes(lane->fxsave,
                               (void*)0x{SCRATCH_ADDRESS + SCRATCH_FXSAVE_OFFSET:08X}u,
                               512u);
                    lane->last_exit = step->exit_kind;
                    lane->wait_object = 0u;
                    actual_exit = step->exit_kind;
                    if (step->exit_kind == {RESIDENT_EXIT_WAIT}u) {{
                        lane->state = {RESIDENT_LANE_WAITING}u;
                        lane->wait_object = step->wait_object;
                    }} else if (step->exit_kind == {RESIDENT_EXIT_COMPLETE}u) {{
                        lane->state = {RESIDENT_LANE_COMPLETED}u;
                    }} else if (step->exit_kind == {RESIDENT_EXIT_FLIP}u) {{
                        lane->state = {RESIDENT_LANE_READY}u;
                        scheduler_header[7] += 1u;
                    }} else if (step->exit_kind == {RESIDENT_EXIT_YIELD}u) {{
                        lane->state = {RESIDENT_LANE_READY}u;
                    }} else {{
                        lane->state = {RESIDENT_LANE_FAULTED}u;
                        scheduler_header[9] += 1u;
                    }}
                }}
                copy_bytes((void*)lane->tls_base,
                           (void*)0x{scheduler_plan.active_tls_base:08X}u, 4096u);
                lane->activation_count += 1u;
                if (step->wake_lane_id != 0xFFFFFFFFu) {{
                    SchedulerLane* wake_lane;
                    if (step->wake_lane_id >= {RESIDENT_SCHEDULER_LANE_COUNT}u)
                        protocol_failure(53u, step->wake_lane_id);
                    wake_lane = &lanes[step->wake_lane_id];
                    if (wake_lane->state != {RESIDENT_LANE_WAITING}u) {{
                        exchange->elapsed_ticks = wake_lane->fault_eip;
                        exchange->qpc_frequency = wake_lane->fault_address;
                        protocol_failure(54u, wake_lane->state);
                    }}
                    wake_lane->state = {RESIDENT_LANE_READY}u;
                    wake_lane->wait_object = 0u;
                    scheduler_header[6] += 1u;
                }}
                service_end = service_header[0];
                if (previous_lane != 0xFFFFFFFFu && previous_lane != step->lane_id)
                    scheduler_header[14] += 1u;
                previous_lane = step->lane_id;
                for (register_index = 0u;
                     register_index < {RESIDENT_SCHEDULER_TRACE_RECORD_WORDS}u;
                     ++register_index)
                    trace->words[register_index] = 0u;
                trace->words[0] = step_index;
                trace->words[1] = step->lane_id;
                trace->words[2] = step->entry_eip;
                trace->words[3] = lane->eip;
                trace->words[4] = actual_exit;
                trace->words[5] = step->wake_lane_id;
                trace->words[6] = before_state;
                trace->words[7] = lane->state;
                trace->words[8] = service_begin;
                trace->words[9] = service_end;
                trace->words[10] = lane->tls_base;
                trace->words[11] = before_tls;
                trace->words[12] = *(U32*)lane->tls_base;
                trace->words[13] = scheduler_fnv1a(
                    (const U8*)0x{scheduler_plan.render_address:08X}u,
                    {scheduler_plan.render_size}u);
                trace->words[14] = scheduler_fnv1a(
                    (const U8*)0x{scheduler_plan.audio_address:08X}u,
                    {scheduler_plan.audio_size}u);
                trace->words[15] = lane->fault_code;
                scheduler_header[4] += 1u;
            }}
            QueryPerformanceCounter(&finished);
            scheduler_header[11] = service_header[0];
            scheduler_header[12] = scheduler_fnv1a(
                (const U8*)0x{scheduler_plan.render_address:08X}u,
                {scheduler_plan.render_size}u);
            scheduler_header[13] = scheduler_fnv1a(
                (const U8*)0x{scheduler_plan.audio_address:08X}u,
                {scheduler_plan.audio_size}u);
            for (index = 0u; index < {RESIDENT_SCHEDULER_LANE_COUNT}u; ++index)
                if (lanes[index].state != {RESIDENT_LANE_COMPLETED}u &&
                    lanes[index].state != {RESIDENT_LANE_FAULTED}u)
                    scheduler_header[8] += 1u;
            publish_scheduler_lanes(lanes);
            exchange->elapsed_ticks = finished - started;
            exchange->qpc_frequency = frequency;
            exchange->eip = lanes[0].eip;
            exchange->eflags = lanes[0].eflags;
            for (index = 0u; index < 8u; ++index)
                exchange->registers[index] = lanes[0].registers[index];
            copy_bytes(exchange->fxsave, lanes[0].fxsave, 512u);
            publish_pages_and_dirty();
            exchange->error = 0u;
            exchange->reserved = command;
            exchange->status = {EXCHANGE_STATUS_COMPLETE}u;
            if (!SetEvent(g_response_event)) ExitProcess(26);
            continue;
        }}
        exchange->status = {EXCHANGE_STATUS_RUNNING}u;
        exchange->error = 0u;
"""
    if dispatch_seam not in source:
        raise Ia32BackendError("Phase-5 worker lost its scheduler-command seam")
    return source.replace(dispatch_seam, dispatch_block, 1)


def _phase7_native_workload_source(
    source: str,
    *,
    safe_object_paths_only: bool = False,
) -> str:
    """Port normal-live allocator, GPU, and safe path semantics into Phase 7."""

    if "typedef struct MemoryBasicInformation" not in source:
        type_seam = "typedef unsigned long long U64;\n"
        if source.count(type_seam) != 1:
            raise Ia32BackendError("Phase-7 worker lost its memory-query type seam")
        source = source.replace(
            type_seam,
            type_seam
            + """typedef struct MemoryBasicInformation {
    void* base_address;
    void* allocation_base;
    U32 allocation_protect;
    U32 region_size;
    U32 state;
    U32 protect;
    U32 type;
} MemoryBasicInformation;
__declspec(dllimport) unsigned long __stdcall VirtualQuery(
    const void*, MemoryBasicInformation*, unsigned long);
""",
            1,
        )

    if not safe_object_paths_only:
        declaration_seam = """__declspec(dllimport) int __stdcall WideCharToMultiByte(
    U32, U32, const U16*, int, char*, int, const char*, BOOL*);
"""
        declaration_block = (
            declaration_seam
            + """__declspec(dllimport) HANDLE __stdcall CreateFileMappingA(
    HANDLE, void*, U32, U32, U32, const char*);
__declspec(dllimport) BOOL __stdcall UnmapViewOfFile(const void*);
"""
        )
        if "__stdcall CreateFileMappingA(" not in source:
            if source.count(declaration_seam) != 1:
                raise Ia32BackendError(
                    "Phase-7 worker lost its contiguous-mapping declaration seam"
                )
            source = source.replace(declaration_seam, declaration_block, 1)
        if "__stdcall MapViewOfFileEx(" not in source:
            mapping_seam = """__declspec(dllimport) void* __stdcall MapViewOfFile(HANDLE, U32, U32, U32, unsigned long);"""
            mapping_block = (
                mapping_seam
                + """
__declspec(dllimport) void* __stdcall MapViewOfFileEx(
    HANDLE, U32, U32, U32, unsigned long, void*);"""
            )
            if source.count(mapping_seam) != 1:
                raise Ia32BackendError(
                    "Phase-7 worker lost its fixed-view mapping declaration seam"
                )
            source = source.replace(mapping_seam, mapping_block, 1)

    replacements = (
        (
            """typedef struct WorkloadAllocation { U32 address, size, active; } WorkloadAllocation;
""",
            """typedef struct WorkloadAllocation {
    U32 address, size, active, contiguous;
} WorkloadAllocation;
""",
            "allocation ownership type",
        ),
        (
            """static U32 g_workload_next_contiguous = 0x83000000u;
""",
            """static U32 g_workload_next_contiguous = 0x83000000u;
static U32 g_workload_requested_contiguous;
static HANDLE g_workload_contiguous_mapping;
""",
            "contiguous mapping global",
        ),
        (
            """static WorkloadAllocation* workload_find_allocation(U32 address) {
    U32 index;
    for (index = 0u; index < g_workload_allocation_count; ++index) {
        WorkloadAllocation* allocation = &g_workload_allocations[index];
        if (allocation->active && address >= allocation->address &&
            address - allocation->address < allocation->size)
            return allocation;
    }
    return 0;
}
""",
            """static BOOL workload_allocation_offset(
    const WorkloadAllocation* allocation, U32 address, U32* offset
) {
    U32 base;
    if (!allocation || !allocation->active) return 0;
    if (address >= allocation->address &&
        address - allocation->address < allocation->size) {
        if (offset) *offset = address - allocation->address;
        return 1;
    }
    if (!allocation->contiguous) return 0;
    base = 0xF0000000u | (allocation->address & 0x0FFFFFFFu);
    if (address < base || address - base >= allocation->size) return 0;
    if (offset) *offset = address - base;
    return 1;
}
static WorkloadAllocation* workload_find_allocation(U32 address) {
    U32 index;
    for (index = 0u; index < g_workload_allocation_count; ++index) {
        WorkloadAllocation* allocation = &g_workload_allocations[index];
        if (workload_allocation_offset(allocation, address, 0)) return allocation;
    }
    return 0;
}
static BOOL workload_open_contiguous_arena(void) {
    const U32 FILE_MAP_ALL_ACCESS = 0x000F001Fu;
    const U32 PAGE_READWRITE = 0x04u;
    const U32 ARENA_SIZE = 0x0B000000u;
    HANDLE mapping;
    void* kernel_view;
    void* uncached_view;
    if (g_workload_contiguous_mapping) return 1;
    mapping = CreateFileMappingA(
        (HANDLE)-1, 0, PAGE_READWRITE, 0u, ARENA_SIZE, 0);
    if (!mapping) return 0;
    kernel_view = MapViewOfFileEx(
        mapping, FILE_MAP_ALL_ACCESS, 0u, 0u, ARENA_SIZE,
        (void*)0x82000000u);
    if (kernel_view != (void*)0x82000000u) {
        if (kernel_view) UnmapViewOfFile(kernel_view);
        CloseHandle(mapping);
        return 0;
    }
    uncached_view = MapViewOfFileEx(
        mapping, FILE_MAP_ALL_ACCESS, 0u, 0u, ARENA_SIZE,
        (void*)0xF2000000u);
    if (uncached_view != (void*)0xF2000000u) {
        if (uncached_view) UnmapViewOfFile(uncached_view);
        UnmapViewOfFile(kernel_view);
        CloseHandle(mapping);
        return 0;
    }
    g_workload_contiguous_mapping = mapping;
    return 1;
}
static U32 workload_choose_contiguous_address(
    U32 size, U32 lowest, U32 highest, U32 alignment
) {
    U32 pass;
    U32 reserved_size;
    U32 cursor_physical = g_workload_next_contiguous & 0x7FFFFFFFu;
    if (!size) size = 1u;
    if (alignment < 4096u) alignment = 4096u;
    if ((alignment & (alignment - 1u)) != 0u || highest < lowest) return 0u;
    reserved_size = workload_align_up(size, 4096u);
    if (reserved_size < size) return 0u;
    for (pass = 0u; pass < 2u; ++pass) {
        U32 candidate = lowest;
        if (!pass && candidate < cursor_physical) candidate = cursor_physical;
        if (candidate < 0x02000000u) candidate = 0x02000000u;
        candidate = workload_align_up(candidate, alignment);
        for (;;) {
            U32 index;
            U64 candidate_end = (U64)candidate + reserved_size;
            BOOL overlap = 0;
            if (candidate_end > 0x0D000000ull ||
                candidate_end > (U64)highest + 1ull)
                break;
            for (index = 0u; index < g_workload_allocation_count; ++index) {
                WorkloadAllocation* allocation = &g_workload_allocations[index];
                U32 allocation_start;
                U64 allocation_end;
                if (!allocation->active || !allocation->contiguous) continue;
                allocation_start = allocation->address & 0x7FFFFFFFu;
                allocation_end = (U64)allocation_start +
                    workload_align_up(allocation->size, 4096u);
                if (candidate_end <= allocation_start ||
                    candidate >= allocation_end)
                    continue;
                if (allocation_end > 0xFFFFFFFFull) return 0u;
                candidate = workload_align_up((U32)allocation_end, alignment);
                overlap = 1;
                break;
            }
            if (!overlap) return 0x80000000u + candidate;
        }
        if (lowest >= cursor_physical) break;
    }
    return 0u;
}
""",
            "contiguous alias helpers",
        ),
        (
            """    address = workload_align_up(*cursor, alignment);
    reserved_size = workload_align_up(size, 4096u);
""",
            """    address = workload_align_up(*cursor, alignment);
    if (cursor == &g_workload_next_contiguous &&
        g_workload_requested_contiguous)
        address = g_workload_requested_contiguous;
    reserved_size = workload_align_up(size, 4096u);
""",
            "constrained contiguous allocation address",
        ),
        (
            """    U32 region_address;
    U32 region_size;
    U32 reserved_size;
    WorkloadAllocation* allocation;
""",
            """    U32 region_address;
    U32 reserved_size;
    BOOL contiguous;
    U64 page_address;
    U64 commit_end;
    WorkloadAllocation* allocation;
""",
            "allocator declarations",
        ),
        (
            """    if (VirtualAlloc((void*)commit_address, commit_size, 0x00001000u, 0x04u) !=
        (void*)commit_address) {
        region_address = address & ~0xFFFFu;
        region_size = workload_align_up(
            address - region_address + reserved_size, 0x10000u);
        if (VirtualAlloc((void*)region_address, region_size, 0x00002000u, 0x01u) !=
            (void*)region_address) return 0u;
        if (VirtualAlloc((void*)commit_address, commit_size, 0x00001000u, 0x04u) !=
            (void*)commit_address) return 0u;
    }
""",
            """    contiguous = cursor == &g_workload_next_contiguous;
    if (reserved_size < size) return 0u;
    if (contiguous) {
        if ((U64)address + reserved_size > 0x8D000000ull ||
            !workload_open_contiguous_arena())
            return 0u;
    } else {
        commit_end = (U64)commit_address + commit_size;
        for (page_address = commit_address; page_address < commit_end;
             page_address += 4096u) {
            if (VirtualAlloc((void*)(U32)page_address, 4096u, 0x00001000u, 0x04u) ==
                (void*)(U32)page_address) continue;
            region_address = (U32)page_address & ~0xFFFFu;
            if (VirtualAlloc((void*)region_address, 0x10000u, 0x00002000u, 0x01u) !=
                (void*)region_address) return 0u;
            if (VirtualAlloc((void*)(U32)page_address, 4096u, 0x00001000u, 0x04u) !=
                (void*)(U32)page_address) return 0u;
        }
    }
""",
            "allocator commit loop",
        ),
        (
            """    allocation->active = 1u;
    *cursor = address + reserved_size;
""",
            """    allocation->active = 1u;
    allocation->contiguous = contiguous;
    if (*cursor < address + reserved_size) *cursor = address + reserved_size;
""",
            "allocation ownership assignment",
        ),
        (
            """        if (service->runtime_value == 1u) {
            U32 alignment = arguments[3] ? arguments[3] : 4096u;
            return workload_allocate(arguments[0], alignment,
                                     &g_workload_next_contiguous);
        }
""",
            """        if (service->runtime_value == 1u) {
            U32 alignment = arguments[3] ? arguments[3] : 4096u;
            U32 address;
            g_workload_requested_contiguous = workload_choose_contiguous_address(
                arguments[0], arguments[1], arguments[2], alignment);
            if (!g_workload_requested_contiguous) return 0u;
            address = workload_allocate(
                arguments[0], alignment, &g_workload_next_contiguous);
            g_workload_requested_contiguous = 0u;
            return address;
        }
""",
            "MmAllocateContiguousMemoryEx constraints",
        ),
        (
            """        allocation = workload_find_allocation(arguments[0]);
        if (service->runtime_value == 4u) {
            if (!allocation) return 0u;
            return arguments[0] >= 0x80000000u && arguments[0] < 0x84000000u
                ? arguments[0] & 0x7FFFFFFFu : arguments[0];
        }
""",
            """        allocation = workload_find_allocation(arguments[0]);
        if (service->runtime_value == 4u) {
            U32 offset = 0u;
            if (!workload_allocation_offset(allocation, arguments[0], &offset))
                return 0u;
            return allocation->contiguous
                ? (allocation->address & 0x7FFFFFFFu) + offset
                : arguments[0];
        }
""",
            "physical-address translation",
        ),
        (
            """        if (service->runtime_value == 7u) {
            U64 end = (U64)arguments[0] + arguments[1];
            U64 allocation_end = allocation
                ? (U64)allocation->address + allocation->size : 0u;
            return allocation && end <= allocation_end ? 0u : 0xC000000Du;
        }
""",
            """        if (service->runtime_value == 7u) {
            U32 offset = 0u;
            return workload_allocation_offset(allocation, arguments[0], &offset) &&
                    arguments[1] <= allocation->size - offset
                ? 0u : 0xC000000Du;
        }
""",
            "aliased allocation range validation",
        ),
        (
            """        if (service->runtime_value == 8u)
            return workload_allocate(arguments[0], 4096u,
                                     &g_workload_next_contiguous);
""",
            """        if (service->runtime_value == 8u) {
            U32 address = workload_allocate(
                arguments[0], 4096u, &g_workload_next_contiguous);
            if (arguments[1]) *(U32*)arguments[1] = 0u;
            return address;
        }
""",
            "GPU instance-memory claim",
        ),
        (
            """static BOOL live_decode_object_path(
    U32 object_attributes, char* output, U32 capacity
) {
    U32 candidates[6];
    U32 candidate, index;
    if (!object_attributes || capacity < 2u) return 0;
    candidates[0] = object_attributes;
    for (index = 0u; index < 5u; ++index)
        candidates[index + 1u] = *(U32*)(object_attributes + index * 4u);
    for (candidate = 0u; candidate < 6u; ++candidate) {
        U32 descriptor = candidates[candidate];
        U32 length, maximum, buffer;
        BOOL path_like = 0;
        if (!descriptor) continue;
        length = *(U16*)descriptor;
        maximum = *(U16*)(descriptor + 2u);
        buffer = *(U32*)(descriptor + 4u);
        if (!length || length > maximum || length >= capacity || !buffer) continue;
        for (index = 0u; index < length; ++index) {
            U8 value = *(U8*)(buffer + index);
            if (value < 0x20u || value > 0x7Eu) break;
            output[index] = (char)value;
            if (value == '\\\\' || value == '/' || value == ':') path_like = 1;
        }
        if (index == length && path_like) {
            output[length] = 0;
            return 1;
        }
    }
    return 0;
}
""",
            """static BOOL live_readable_range(U32 address, U32 size) {
    U64 cursor = address;
    U64 end = cursor + size;
    if (!address || !size || end > 0x100000000ull) return 0;
    while (cursor < end) {
        MemoryBasicInformation information;
        U32 access;
        U64 region_end;
        if (VirtualQuery((void*)(U32)cursor, &information, sizeof(information)) !=
                sizeof(information) ||
            information.state != 0x00001000u || !information.base_address)
            return 0;
        access = information.protect & 0xFFu;
        if ((information.protect & 0x00000100u) || access == 0u ||
            access == 0x01u || access == 0x10u)
            return 0;
        region_end = (U64)(U32)information.base_address + information.region_size;
        if (region_end <= cursor) return 0;
        cursor = region_end;
    }
    return 1;
}
static BOOL live_decode_object_path(
    U32 object_attributes, char* output, U32 capacity
) {
    U32 candidates[6];
    U32 candidate, index;
    if (!object_attributes || capacity < 2u) return 0;
    candidates[0] = object_attributes;
    for (index = 0u; index < 5u; ++index) {
        U64 candidate_address = (U64)object_attributes + index * 4u;
        candidates[index + 1u] =
            candidate_address <= 0xFFFFFFFFull &&
                    live_readable_range((U32)candidate_address, 4u)
                ? *(U32*)(U32)candidate_address
                : 0u;
    }
    for (candidate = 0u; candidate < 6u; ++candidate) {
        U32 descriptor = candidates[candidate];
        U32 length, maximum, buffer;
        BOOL path_like = 0;
        if (!live_readable_range(descriptor, 8u)) continue;
        length = *(U16*)descriptor;
        maximum = *(U16*)(descriptor + 2u);
        buffer = *(U32*)(descriptor + 4u);
        if (!length || length > maximum || length >= capacity ||
            !live_readable_range(buffer, length))
            continue;
        for (index = 0u; index < length; ++index) {
            U8 value = *(U8*)(buffer + index);
            if (value < 0x20u || value > 0x7Eu) break;
            output[index] = (char)value;
            if (value == '\\\\' || value == '/' || value == ':') path_like = 1;
        }
        if (index == length && path_like) {
            output[length] = 0;
            return 1;
        }
    }
    return 0;
}
""",
            "object-path decoder",
        ),
    )
    for old, new, label in replacements:
        if label == "object-path decoder":
            if old not in source:
                if "static BOOL live_decode_object_path(" in source:
                    raise Ia32BackendError("Phase-7 worker changed its object-path decoder seam")
                if safe_object_paths_only:
                    raise Ia32BackendError("normal-live worker lost its object-path decoder")
                continue
        elif safe_object_paths_only:
            continue
        if source.count(old) != 1:
            raise Ia32BackendError(f"Phase-7 worker lost its {label} seam")
        source = source.replace(old, new, 1)
    return source


def _phase7_c_source(
    *,
    page_addresses: Sequence[int],
    mapped_page_addresses: Sequence[int],
    guest_virtual_size: int,
    code_spans: Sequence[tuple[int, bytes]],
    stop_eip: int,
    host_service_thunks: Sequence[_HostServiceThunk] = (),
    audited_host_services: bool = True,
    scheduler_plan: _Ia32SchedulerPlan,
    service_state: Mapping[str, Any] | None = None,
    detached_guest: bool = False,
) -> str:
    """Emit the Phase-7 worker-owned memory and dirty-publication protocol."""

    source = _phase5_c_source(
        page_addresses=page_addresses,
        mapped_page_addresses=mapped_page_addresses,
        guest_virtual_size=guest_virtual_size,
        code_spans=code_spans,
        stop_eip=stop_eip,
        host_service_thunks=host_service_thunks,
        audited_host_services=audited_host_services,
        scheduler_plan=scheduler_plan,
        service_state=service_state,
        detached_guest=detached_guest,
    )
    source = _phase7_native_workload_source(source)
    source = source.replace(
        f"exchange->version != {RESIDENT_SCHEDULER_EXCHANGE_VERSION}u",
        f"exchange->version != {CUTOVER_EXCHANGE_VERSION}u",
    )
    memory_offset = _phase7_memory_offset(len(page_addresses))
    detached_code_pages: set[int] = set()
    if detached_guest:
        for address, payload in code_spans:
            if not payload:
                continue
            first_page = address & ~(PAGE_SIZE - 1)
            final_page = (address + len(payload) - 1) & ~(PAGE_SIZE - 1)
            detached_code_pages.update(range(first_page, final_page + PAGE_SIZE, PAGE_SIZE))
    detached_code_page_flags = ",".join(
        "1u" if address in detached_code_pages else "0u" for address in page_addresses
    )
    old_publication = f"""static void publish_pages_and_dirty(void) {{
    U32 index;
    U8* dirty = g_exchange->pages + {len(page_addresses)}u * 4096u;
    for (index = 0; index < sizeof(kPages) / sizeof(kPages[0]); ++index) {{
        U8* input = g_exchange->pages + index * 4096u;
        U8* output = (U8*)kPages[index];
        dirty[index] = (U8)page_changed(input, output);
        copy_page(input, output);
    }}
}}
"""
    canonical_code_helper = (
        "static const U8 kDetachedGuestCodePages[] = {"
        + detached_code_page_flags
        + "};\n"
        + """static BOOL publish_changed_bytes(
    U8* published, const U8* resident, U32 size
) {
    U32 index = 0u;
    while (index + 4u <= size &&
           *(const U32*)(published + index) == *(const U32*)(resident + index))
        index += 4u;
    while (index < size && published[index] == resident[index]) ++index;
    if (index == size) return 0;
    copy_bytes(published + index, resident + index, size - index);
    return 1;
}
static BOOL publish_page_without_code(
    U32 page_address, U8* published, const U8* resident, U32* cursor
) {
    BOOL changed = 0;
    U32 index;
    U32 position = 0u;
    U32 page_end = page_address + 4096u;
    U32 count = sizeof(kDetachedGuestSpans) / sizeof(kDetachedGuestSpans[0]);
    while (*cursor < count &&
           kDetachedGuestSpans[*cursor].address +
               kDetachedGuestSpans[*cursor].size <= page_address)
        ++*cursor;
    for (index = *cursor; index < count; ++index) {
        U32 span_start = kDetachedGuestSpans[index].address;
        U32 span_end = span_start + kDetachedGuestSpans[index].size;
        U32 code_start;
        U32 code_end;
        if (span_start >= page_end) break;
        code_start = span_start > page_address ? span_start - page_address : 0u;
        code_end = span_end < page_end ? span_end - page_address : 4096u;
        if (position < code_start)
            changed |= publish_changed_bytes(
                published + position, resident + position, code_start - position);
        if (position < code_end) position = code_end;
    }
    if (position < 4096u)
        changed |= publish_changed_bytes(
            published + position, resident + position, 4096u - position);
    return changed;
}
"""
        if detached_guest
        else ""
    )
    publication_locals = "    U32 code_cursor = 0u;\n" if detached_guest else ""
    publication_compare = (
        """        BOOL changed = kDetachedGuestCodePages[index] != 0u
            ? publish_page_without_code(
                kPages[index], published, resident, &code_cursor)
            : publish_changed_page(published, resident);
        if (changed) {"""
        if detached_guest
        else """        if (publish_changed_page(published, resident)) {"""
    )
    new_publication = f"""static BOOL publish_changed_page(U8* published, const U8* resident) {{
    U32* output = (U32*)published;
    const U32* input = (const U32*)resident;
    U32 index;
    for (index = 0u; index < 1024u; index += 8u) {{
        if (output[index] != input[index] ||
            output[index + 1u] != input[index + 1u] ||
            output[index + 2u] != input[index + 2u] ||
            output[index + 3u] != input[index + 3u] ||
            output[index + 4u] != input[index + 4u] ||
            output[index + 5u] != input[index + 5u] ||
            output[index + 6u] != input[index + 6u] ||
            output[index + 7u] != input[index + 7u]) {{
            for (; index < 1024u; index += 8u) {{
                output[index] = input[index];
                output[index + 1u] = input[index + 1u];
                output[index + 2u] = input[index + 2u];
                output[index + 3u] = input[index + 3u];
                output[index + 4u] = input[index + 4u];
                output[index + 5u] = input[index + 5u];
                output[index + 6u] = input[index + 6u];
                output[index + 7u] = input[index + 7u];
            }}
            return 1;
        }}
    }}
    return 0;
}}
{canonical_code_helper}static __declspec(noreturn) void protocol_failure(U32 error, U32 detail);
static U32* cutover_memory_header(void) {{
    return (U32*)((U8*)g_exchange + {memory_offset}u);
}}
static U8* cutover_page_states(void) {{
    return g_exchange->pages + {len(page_addresses)}u * 4096u;
}}
static void validate_cutover_memory_header(void) {{
    U32* header = cutover_memory_header();
    if (header[0] != 0x{CUTOVER_MEMORY_MAGIC:08X}u ||
        header[1] != {CUTOVER_MEMORY_VERSION}u)
        protocol_failure(60u, header[0]);
}}
static void initialize_resident_memory(void) {{
    U32 index;
    U32* header = cutover_memory_header();
    U8* states = cutover_page_states();
    validate_cutover_memory_header();
    for (index = 0; index < sizeof(kPages) / sizeof(kPages[0]); ++index) {{
        copy_page((U8*)kPages[index],
                  g_exchange->pages + index * 4096u);
        states[index] = {CUTOVER_PAGE_CLEAN}u;
    }}
    header[2] = {len(page_addresses)}u;
    header[3] = {len(page_addresses) * PAGE_SIZE}u;
    header[4] = 0u;
    header[5] = 0u;
    header[6] = 0u;
    header[7] = 0u;
    header[8] = 0u;
    header[9] = {len(page_addresses)}u;
    header[10] = {len(page_addresses)}u;
    header[11] = 0u;
    header[12] = 0u;
    header[13] = 0u;
}}
static void apply_host_publications(void) {{
    U32 index;
    U32 count = 0u;
    U32* header = cutover_memory_header();
    U8* states = cutover_page_states();
    validate_cutover_memory_header();
    for (index = 0; index < sizeof(kPages) / sizeof(kPages[0]); ++index) {{
        if (states[index] == {CUTOVER_PAGE_HOST_PUBLICATION}u) {{
            copy_page((U8*)kPages[index],
                      g_exchange->pages + index * 4096u);
            states[index] = {CUTOVER_PAGE_CLEAN}u;
            ++count;
        }} else if (states[index] != {CUTOVER_PAGE_CLEAN}u) {{
            protocol_failure(61u, kPages[index]);
        }}
    }}
    header[4] += 1u;
    header[5] = count;
    header[6] = count * 4096u;
    header[9] = {len(page_addresses)}u - count;
    header[12] += 1u;
}}
static void publish_pages_and_dirty(void) {{
    U32 index;
    U32 count = 0u;
{publication_locals}    U32* header = cutover_memory_header();
    U8* states = cutover_page_states();
    validate_cutover_memory_header();
    for (index = 0; index < sizeof(kPages) / sizeof(kPages[0]); ++index) {{
        U8* published = g_exchange->pages + index * 4096u;
        U8* resident = (U8*)kPages[index];
{publication_compare}
            states[index] = {CUTOVER_PAGE_NATIVE_DIRTY}u;
            ++count;
        }} else {{
            states[index] = {CUTOVER_PAGE_CLEAN}u;
        }}
    }}
    header[7] = count;
    header[8] = count * 4096u;
    header[10] = {len(page_addresses)}u - count;
    header[13] += 1u;
}}
"""
    if old_publication not in source:
        raise Ia32BackendError("Phase-7 worker lost its dirty-publication seam")
    source = source.replace(old_publication, new_publication, 1)
    normal_input_copy = """        for (index = 0; index < sizeof(kPages) / sizeof(kPages[0]); ++index)
            copy_bytes((void*)kPages[index], exchange->pages + index * 4096u, 4096u);
"""
    scheduler_input_copy = """            for (index = 0; index < sizeof(kPages) / sizeof(kPages[0]); ++index)
                copy_bytes((void*)kPages[index], exchange->pages + index * 4096u, 4096u);
"""
    if normal_input_copy not in source or scheduler_input_copy not in source:
        raise Ia32BackendError("Phase-7 worker lost a full-input-copy seam")
    source = source.replace(scheduler_input_copy, "            apply_host_publications();\n", 1)
    source = source.replace(normal_input_copy, "        apply_host_publications();\n", 1)
    ready_seam = """    if (!QueryPerformanceFrequency(&frequency)) protocol_failure(30u, exchange->eip);
"""
    if ready_seam not in source:
        raise Ia32BackendError("Phase-7 worker lost its initialization seam")
    return source.replace(
        ready_seam,
        "    initialize_resident_memory();\n" + ready_seam,
        1,
    )


def _normal_live_resume_thunk(instruction: X86Instruction) -> bytes:
    """Execute the instruction hidden by the fixed stop patch, then continue."""

    if instruction.address + instruction.size != instruction.next_address:
        raise Ia32BackendError("normal-live resume instruction has an invalid extent")
    payload = bytes.fromhex(instruction.bytes_hex)
    if len(payload) != instruction.size or instruction.size < 5:
        raise Ia32BackendError(
            "normal-live flip stop requires at least five verified instruction bytes"
        )
    jump_address = NORMAL_LIVE_RESUME_THUNK_ADDRESS + len(payload)
    displacement = _u32(instruction.next_address - (jump_address + 5))
    return payload + b"\xe9" + struct.pack("<I", displacement)


def _normal_live_resource_publication_c_source() -> str:
    """Return the native NV2A texture snapshot publisher used by normal-live."""

    return r"""#define LIVE_RESOURCE_BINDING_CAPACITY 256u
#define LIVE_VERTEX_RANGE_CAPACITY 1024u
#define LIVE_RESOURCE_CAPACITY \
    (LIVE_RESOURCE_BINDING_CAPACITY + LIVE_VERTEX_RANGE_CAPACITY)
typedef struct LiveResourceScanState {
    U32 next_address, run_active, pending_long_header, pending_packet;
    U32 non_increasing, first_method, remaining, method_index;
    U32 texture_offsets[4], texture_formats[4], texture_image_rects[4];
    U32 texture_offset_mask, texture_format_mask;
    U32 vertex_array_offsets[16], vertex_array_formats[16];
    U32 vertex_array_offset_mask, vertex_array_format_mask;
    U32 active_vertex_primitive, active_vertex_max_index;
    U32 active_vertex_has_index, retained_vertex_range_count;
    U32 retained_vertex_range_starts[LIVE_VERTEX_RANGE_CAPACITY];
    U32 retained_vertex_range_ends[LIVE_VERTEX_RANGE_CAPACITY];
    U32 retained_binding_count, retained_binding_evict_cursor;
    U32 frame_binding_stages[LIVE_RESOURCE_BINDING_CAPACITY];
    U32 frame_binding_addresses[LIVE_RESOURCE_BINDING_CAPACITY];
    U32 frame_binding_formats[LIVE_RESOURCE_BINDING_CAPACITY];
    U32 frame_binding_image_rects[LIVE_RESOURCE_BINDING_CAPACITY];
} LiveResourceScanState;
typedef struct LiveSha256 {
    U32 hash[8];
    U8 block[64];
    U64 byte_count;
    U32 block_size;
} LiveSha256;
typedef struct LiveTextureResource {
    U32 stage, address, source, width, height, byte_count;
    const char* format;
    U8 hash[32];
} LiveTextureResource;
static LiveResourceScanState g_live_resource_scan;
static LiveTextureResource g_live_texture_resources[
    LIVE_RESOURCE_CAPACITY];
static U32 g_live_resource_payload_size = 12u;
static U32 g_live_texture_binding_cache[LIVE_RESOURCE_BINDING_CAPACITY];
#define LIVE_RESOURCE_DEDUPE_CAPACITY 2048u
static U32 g_live_resource_dedupe_generation;
static U32 g_live_resource_dedupe_stamps[LIVE_RESOURCE_DEDUPE_CAPACITY];
static U32 g_live_resource_dedupe_addresses[LIVE_RESOURCE_DEDUPE_CAPACITY];
static U32 g_live_resource_dedupe_widths[LIVE_RESOURCE_DEDUPE_CAPACITY];
static U32 g_live_resource_dedupe_heights[LIVE_RESOURCE_DEDUPE_CAPACITY];
static const char* g_live_resource_dedupe_formats[
    LIVE_RESOURCE_DEDUPE_CAPACITY];
static WorkloadAllocation* g_live_resource_last_allocation;
static BOOL live_readable_range(U32, U32);
static BOOL live_resource_source(U32, U32, U32*);
static U32 live_sha256_rotate(U32 value, U32 count) {
    return (value >> count) | (value << (32u - count));
}
static void live_sha256_transform(LiveSha256* state, const U8* input) {
    static const U32 constants[64] = {
        0x428a2f98u,0x71374491u,0xb5c0fbcfu,0xe9b5dba5u,
        0x3956c25bu,0x59f111f1u,0x923f82a4u,0xab1c5ed5u,
        0xd807aa98u,0x12835b01u,0x243185beu,0x550c7dc3u,
        0x72be5d74u,0x80deb1feu,0x9bdc06a7u,0xc19bf174u,
        0xe49b69c1u,0xefbe4786u,0x0fc19dc6u,0x240ca1ccu,
        0x2de92c6fu,0x4a7484aau,0x5cb0a9dcu,0x76f988dau,
        0x983e5152u,0xa831c66du,0xb00327c8u,0xbf597fc7u,
        0xc6e00bf3u,0xd5a79147u,0x06ca6351u,0x14292967u,
        0x27b70a85u,0x2e1b2138u,0x4d2c6dfcu,0x53380d13u,
        0x650a7354u,0x766a0abbu,0x81c2c92eu,0x92722c85u,
        0xa2bfe8a1u,0xa81a664bu,0xc24b8b70u,0xc76c51a3u,
        0xd192e819u,0xd6990624u,0xf40e3585u,0x106aa070u,
        0x19a4c116u,0x1e376c08u,0x2748774cu,0x34b0bcb5u,
        0x391c0cb3u,0x4ed8aa4au,0x5b9cca4fu,0x682e6ff3u,
        0x748f82eeu,0x78a5636fu,0x84c87814u,0x8cc70208u,
        0x90befffau,0xa4506cebu,0xbef9a3f7u,0xc67178f2u
    };
    U32 words[16];
    U32 a, b, c, d, e, f, g, h, index;
    for (index = 0u; index < 16u; ++index) {
        words[index] = (U32)input[index * 4u] << 24u |
            (U32)input[index * 4u + 1u] << 16u |
            (U32)input[index * 4u + 2u] << 8u |
            (U32)input[index * 4u + 3u];
    }
    a = state->hash[0]; b = state->hash[1];
    c = state->hash[2]; d = state->hash[3];
    e = state->hash[4]; f = state->hash[5];
    g = state->hash[6]; h = state->hash[7];
    for (index = 0u; index < 64u; ++index) {
        U32 word;
        if (index >= 16u) {
            U32 first_word = words[(index + 1u) & 15u];
            U32 second_word = words[(index + 14u) & 15u];
            U32 first = live_sha256_rotate(first_word, 7u) ^
                live_sha256_rotate(first_word, 18u) ^ (first_word >> 3u);
            U32 second = live_sha256_rotate(second_word, 17u) ^
                live_sha256_rotate(second_word, 19u) ^ (second_word >> 10u);
            words[index & 15u] += first + words[(index + 9u) & 15u] + second;
        }
        word = words[index & 15u];
        U32 sum1 = live_sha256_rotate(e, 6u) ^
            live_sha256_rotate(e, 11u) ^ live_sha256_rotate(e, 25u);
        U32 choice = (e & f) ^ (~e & g);
        U32 temporary1 = h + sum1 + choice + constants[index] + word;
        U32 sum0 = live_sha256_rotate(a, 2u) ^
            live_sha256_rotate(a, 13u) ^ live_sha256_rotate(a, 22u);
        U32 majority = (a & b) ^ (a & c) ^ (b & c);
        U32 temporary2 = sum0 + majority;
        h = g; g = f; f = e; e = d + temporary1;
        d = c; c = b; b = a; a = temporary1 + temporary2;
    }
    state->hash[0] += a; state->hash[1] += b;
    state->hash[2] += c; state->hash[3] += d;
    state->hash[4] += e; state->hash[5] += f;
    state->hash[6] += g; state->hash[7] += h;
}
static void live_sha256_init(LiveSha256* state) {
    U32 index;
    state->hash[0] = 0x6a09e667u; state->hash[1] = 0xbb67ae85u;
    state->hash[2] = 0x3c6ef372u; state->hash[3] = 0xa54ff53au;
    state->hash[4] = 0x510e527fu; state->hash[5] = 0x9b05688cu;
    state->hash[6] = 0x1f83d9abu; state->hash[7] = 0x5be0cd19u;
    for (index = 0u; index < 64u; ++index) state->block[index] = 0u;
    state->byte_count = 0u;
    state->block_size = 0u;
}
static void live_sha256_update(LiveSha256* state, const U8* input, U32 size) {
    state->byte_count += size;
    if (state->block_size == 0u) {
        while (size >= 64u) {
            live_sha256_transform(state, input);
            input += 64u;
            size -= 64u;
        }
    }
    while (size != 0u) {
        U32 chunk = 64u - state->block_size;
        if (chunk > size) chunk = size;
        copy_bytes(state->block + state->block_size, input, chunk);
        state->block_size += chunk;
        input += chunk;
        size -= chunk;
        if (state->block_size == 64u) {
            live_sha256_transform(state, state->block);
            state->block_size = 0u;
        }
    }
}
static void live_sha256_finish(LiveSha256* state, U8 output[32]) {
    U64 bit_count = state->byte_count * 8u;
    U32 index;
    state->block[state->block_size++] = 0x80u;
    if (state->block_size > 56u) {
        while (state->block_size < 64u) state->block[state->block_size++] = 0u;
        live_sha256_transform(state, state->block);
        state->block_size = 0u;
    }
    while (state->block_size < 56u) state->block[state->block_size++] = 0u;
    for (index = 0u; index < 8u; ++index)
        state->block[63u - index] = (U8)(bit_count >> (index * 8u));
    live_sha256_transform(state, state->block);
    for (index = 0u; index < 8u; ++index) {
        output[index * 4u] = (U8)(state->hash[index] >> 24u);
        output[index * 4u + 1u] = (U8)(state->hash[index] >> 16u);
        output[index * 4u + 2u] = (U8)(state->hash[index] >> 8u);
        output[index * 4u + 3u] = (U8)state->hash[index];
    }
}
static BOOL live_retain_texture_binding(U32 stage) {
    LiveResourceScanState* state = &g_live_resource_scan;
    U32 stage_mask = 1u << stage;
    U32 address, cache_slot, cached, index;
    if ((state->texture_offset_mask & stage_mask) == 0u ||
        (state->texture_format_mask & stage_mask) == 0u) return 1;
    address = state->texture_offsets[stage];
    cache_slot = (address >> 4u) & (LIVE_RESOURCE_BINDING_CAPACITY - 1u);
    cached = g_live_texture_binding_cache[cache_slot];
    if (cached) {
        index = cached - 1u;
        if (index < state->retained_binding_count &&
            state->frame_binding_addresses[index] == address)
            goto retained;
    }
    for (index = 0u; index < state->retained_binding_count; ++index) {
        if (state->frame_binding_addresses[index] == address) goto retained;
    }
    if (state->retained_binding_count < LIVE_RESOURCE_BINDING_CAPACITY) {
        index = state->retained_binding_count++;
    } else {
        index = state->retained_binding_evict_cursor++ %
            LIVE_RESOURCE_BINDING_CAPACITY;
    }
    state->frame_binding_addresses[index] = address;
retained:
    g_live_texture_binding_cache[cache_slot] = index + 1u;
    state->frame_binding_stages[index] = stage;
    state->frame_binding_formats[index] = state->texture_formats[stage];
    state->frame_binding_image_rects[index] = state->texture_image_rects[stage];
    return 1;
}
static U32 live_vertex_element_size(U32 format) {
    U32 type = format & 0xFu;
    U32 components = (format >> 4u) & 0xFu;
    if (components == 0u) return 0u;
    if (type == 2u) return components * 4u;
    if (type == 0u || type == 4u || type == 6u) return 4u;
    return components * 2u;
}
static BOOL live_retain_vertex_range(U32 start, U32 end) {
    LiveResourceScanState* state = &g_live_resource_scan;
    U32 index = 0u;
    if (!start || end <= start || end - start > 16u * 1024u * 1024u)
        return 1;
    while (index < state->retained_vertex_range_count &&
           state->retained_vertex_range_ends[index] < start) ++index;
    while (index < state->retained_vertex_range_count &&
           state->retained_vertex_range_starts[index] <= end) {
        U32 existing_start = state->retained_vertex_range_starts[index];
        U32 existing_end = state->retained_vertex_range_ends[index];
        U32 candidate_start = existing_start < start ? existing_start : start;
        U32 candidate_end = existing_end > end ? existing_end : end;
        U32 move;
        if (candidate_end - candidate_start > 16u * 1024u * 1024u) break;
        start = candidate_start;
        end = candidate_end;
        --state->retained_vertex_range_count;
        for (move = index; move < state->retained_vertex_range_count; ++move) {
            state->retained_vertex_range_starts[move] =
                state->retained_vertex_range_starts[move + 1u];
            state->retained_vertex_range_ends[move] =
                state->retained_vertex_range_ends[move + 1u];
        }
    }
    if (state->retained_vertex_range_count >= LIVE_VERTEX_RANGE_CAPACITY) {
        U32 read, write = 0u;
        for (read = 0u; read < state->retained_vertex_range_count; ++read) {
            U32 retained_start = state->retained_vertex_range_starts[read];
            U32 retained_end = state->retained_vertex_range_ends[read];
            U32 source;
            if (retained_end <= retained_start ||
                !live_resource_source(
                    retained_start, retained_end - retained_start, &source))
                continue;
            state->retained_vertex_range_starts[write] = retained_start;
            state->retained_vertex_range_ends[write++] = retained_end;
        }
        state->retained_vertex_range_count = write;
        index = 0u;
        while (index < state->retained_vertex_range_count &&
               state->retained_vertex_range_ends[index] < start) ++index;
    }
    if (state->retained_vertex_range_count >= LIVE_VERTEX_RANGE_CAPACITY) {
        g_live_render_publication_failure_code = 12u;
        return 0;
    }
    {
        U32 move = state->retained_vertex_range_count;
        while (move > index) {
            state->retained_vertex_range_starts[move] =
                state->retained_vertex_range_starts[move - 1u];
            state->retained_vertex_range_ends[move] =
                state->retained_vertex_range_ends[move - 1u];
            --move;
        }
    }
    ++state->retained_vertex_range_count;
    state->retained_vertex_range_starts[index] = start;
    state->retained_vertex_range_ends[index] = end;
    return 1;
}
static BOOL live_retain_active_vertex_ranges(void) {
    LiveResourceScanState* state = &g_live_resource_scan;
    U32 populated_mask, slot;
    if (!state->active_vertex_has_index) return 1;
    populated_mask = state->vertex_array_offset_mask &
        state->vertex_array_format_mask;
    for (slot = 0u; slot < 16u; ++slot) {
        U32 address, format, stride, element_size;
        U64 end;
        if ((populated_mask & (1u << slot)) == 0u) continue;
        address = state->vertex_array_offsets[slot];
        format = state->vertex_array_formats[slot];
        stride = format >> 8u;
        element_size = live_vertex_element_size(format);
        end = (U64)address + (U64)state->active_vertex_max_index * stride +
            element_size;
        if (!address || !stride || !element_size || end > 0xFFFFFFFFull)
            continue;
        if (!live_retain_vertex_range(address, (U32)end)) return 0;
    }
    state->active_vertex_max_index = 0u;
    state->active_vertex_has_index = 0u;
    return 1;
}
static BOOL live_apply_resource_method(U32 method, U32 value) {
    LiveResourceScanState* state = &g_live_resource_scan;
    if (method == 0x012Cu) {
        if (!live_retain_active_vertex_ranges()) return 0;
    } else if (method == 0x17FCu) {
        U32 stage;
        if (!live_retain_active_vertex_ranges()) return 0;
        state->active_vertex_primitive = value;
        if (value != 0u)
            for (stage = 0u; stage < 4u; ++stage)
                if (!live_retain_texture_binding(stage)) return 0;
    } else if ((method == 0x1800u || method == 0x1808u) &&
               state->active_vertex_primitive != 0u) {
        if (method == 0x1800u) {
            U32 second = value >> 16u;
            value &= 0xFFFFu;
            if (second > value) value = second;
        }
        if (!state->active_vertex_has_index ||
            value > state->active_vertex_max_index)
            state->active_vertex_max_index = value;
        state->active_vertex_has_index = 1u;
    } else if (method == 0x1810u && state->active_vertex_primitive != 0u) {
        U32 first = value & 0x00FFFFFFu;
        U32 count = (value >> 24u) + 1u;
        U64 maximum = (U64)first + count - 1u;
        if (maximum <= 0xFFFFFFFFull) {
            if (!state->active_vertex_has_index ||
                (U32)maximum > state->active_vertex_max_index)
                state->active_vertex_max_index = (U32)maximum;
            state->active_vertex_has_index = 1u;
        }
    } else if (method >= 0x1720u && method <= 0x175Cu &&
               ((method - 0x1720u) & 3u) == 0u) {
        U32 slot = (method - 0x1720u) / 4u;
        state->vertex_array_offsets[slot] = value;
        state->vertex_array_offset_mask |= 1u << slot;
    } else if (method >= 0x1760u && method <= 0x179Cu &&
               ((method - 0x1760u) & 3u) == 0u) {
        U32 slot = (method - 0x1760u) / 4u;
        state->vertex_array_formats[slot] = value;
        state->vertex_array_format_mask |= 1u << slot;
    } else if (method >= 0x1B00u && method < 0x1C00u) {
        U32 stage = (method - 0x1B00u) / 0x40u;
        U32 stage_register = (method - 0x1B00u) & 0x3Fu;
        U32 stage_mask = 1u << stage;
        if (stage_register == 0u) {
            state->texture_offsets[stage] = value;
            state->texture_offset_mask |= stage_mask;
        } else if (stage_register == 4u) {
            state->texture_formats[stage] = value;
            state->texture_format_mask |= stage_mask;
        } else if (stage_register == 0x1Cu) {
            state->texture_image_rects[stage] = value;
        }
    }
    return 1;
}
static void live_reset_resource_packet(void) {
    LiveResourceScanState* state = &g_live_resource_scan;
    state->run_active = 0u;
    state->pending_long_header = 0u;
    state->pending_packet = 0u;
    state->non_increasing = 0u;
    state->first_method = 0u;
    state->remaining = 0u;
    state->method_index = 0u;
}
static BOOL live_consume_resource_word(U32 word) {
    LiveResourceScanState* state = &g_live_resource_scan;
    U32 count;
    if (state->pending_long_header) {
        state->pending_long_header = 0u;
        state->remaining = word & 0x00FFFFFFu;
        state->method_index = 0u;
        state->non_increasing = 1u;
        state->pending_packet = state->remaining != 0u;
        return 1;
    }
    if (state->pending_packet) {
        U32 method = state->non_increasing ? state->first_method :
            state->first_method + state->method_index * 4u;
        if (!live_apply_resource_method(method, word)) return 0;
        ++state->method_index;
        if (--state->remaining == 0u) state->pending_packet = 0u;
        return 1;
    }
    if ((word & 0xE0030003u) == 0u) {
        count = (word >> 18u) & 0x7FFu;
        state->non_increasing = 0u;
    } else if ((word & 0xE0030003u) == 0x40000000u) {
        count = (word >> 18u) & 0x7FFu;
        state->non_increasing = 1u;
    } else if ((word & 0xFFFF0003u) == 0x00030000u) {
        state->first_method = ((word >> 2u) & 0x7FFu) * 4u;
        state->pending_long_header = 1u;
        return 1;
    } else {
        return 1;
    }
    state->first_method = ((word >> 2u) & 0x7FFu) * 4u;
    state->remaining = count;
    state->method_index = 0u;
    state->pending_packet = count != 0u;
    return 1;
}
static BOOL live_scan_resource_span(
    U32 address, const U8* payload, U32 size, BOOL completes_flip
) {
    LiveResourceScanState* state = &g_live_resource_scan;
    U32 offset;
    if ((address & 3u) || (size & 3u)) {
        g_live_render_publication_failure_code = 10u;
        return 0;
    }
    if (state->run_active && address != state->next_address)
        live_reset_resource_packet();
    state->run_active = 1u;
    for (offset = 0u; offset < size; offset += 4u) {
        U32 word;
        copy_bytes(&word, payload + offset, 4u);
        if (!live_consume_resource_word(word)) {
            if (!g_live_render_publication_failure_code)
                g_live_render_publication_failure_code = 11u;
            return 0;
        }
    }
    state->next_address = address + size;
    if (completes_flip) live_reset_resource_packet();
    return 1;
}
static BOOL live_texture_layout(
    U32 format_raw, U32 image_rect, U32* width_output, U32* height_output,
    U32* byte_count_output, const char** format_output
) {
    U32 color_format = (format_raw >> 8u) & 0xFFu;
    U32 linear = color_format == 0x12u || color_format == 0x1Eu;
    U32 width, height, levels, maximum, maximum_levels = 0u;
    U32 mip_width, mip_height, level;
    U64 byte_count = 0u;
    const char* format;
    switch (color_format) {
    case 0x05u: format = "R5G6B5"; break;
    case 0x06u: format = "A8R8G8B8"; break;
    case 0x07u: format = "X8R8G8B8"; break;
    case 0x0Cu: format = "DXT1"; break;
    case 0x0Eu: format = "DXT3"; break;
    case 0x0Fu: format = "DXT5"; break;
    case 0x12u: format = "A8R8G8B8_LINEAR"; break;
    case 0x1Eu: format = "X8R8G8B8_LINEAR"; break;
    default: return 0;
    }
    width = linear && image_rect ? image_rect >> 16u :
        1u << ((format_raw >> 20u) & 0xFu);
    height = linear && image_rect ? image_rect & 0xFFFFu :
        1u << ((format_raw >> 24u) & 0xFu);
    levels = (format_raw >> 16u) & 0xFu;
    if (levels == 0u) levels = 1u;
    if (linear) {
        levels = 1u;
    } else {
        maximum = width > height ? width : height;
        do { ++maximum_levels; maximum >>= 1u; } while (maximum != 0u);
        if (levels > maximum_levels) levels = maximum_levels;
    }
    mip_width = width;
    mip_height = height;
    for (level = 0u; level < levels; ++level) {
        if (color_format == 0x0Cu) {
            U64 size = (U64)((mip_width + 3u) / 4u) *
                ((mip_height + 3u) / 4u) * 8u;
            byte_count += size < 8u ? 8u : size;
        } else if (color_format == 0x0Eu || color_format == 0x0Fu) {
            U64 size = (U64)((mip_width + 3u) / 4u) *
                ((mip_height + 3u) / 4u) * 16u;
            byte_count += size < 16u ? 16u : size;
        } else if (color_format == 0x05u) {
            byte_count += (U64)mip_width * mip_height * 2u;
        } else {
            byte_count += (U64)mip_width * mip_height * 4u;
        }
        if (mip_width > 1u) mip_width /= 2u;
        if (mip_height > 1u) mip_height /= 2u;
    }
    if (format_raw & (1u << 2u)) byte_count = ((byte_count + 127u) & ~127ull) * 6u;
    if (!width || !height || !byte_count || byte_count > 16u * 1024u * 1024u)
        return 0;
    *width_output = width;
    *height_output = height;
    *byte_count_output = (U32)byte_count;
    *format_output = format;
    return 1;
}
static BOOL live_resource_allocation_source(
    WorkloadAllocation* allocation, U32 address, U32 size, U32* source_output
) {
    U32 physical, offset;
    if (!allocation || !allocation->active || !allocation->contiguous) return 0;
    physical = allocation->address & 0x7FFFFFFFu;
    if (address < physical) return 0;
    offset = address - physical;
    if (offset >= allocation->size || size > allocation->size - offset ||
        !live_readable_range(allocation->address + offset, size)) return 0;
    *source_output = allocation->address + offset;
    return 1;
}
static BOOL live_resource_source(U32 address, U32 size, U32* source_output) {
    U32 index;
    if (live_resource_allocation_source(
            g_live_resource_last_allocation, address, size, source_output))
        return 1;
    for (index = 0u; index < g_workload_allocation_count; ++index) {
        WorkloadAllocation* allocation = &g_workload_allocations[index];
        if (allocation == g_live_resource_last_allocation) continue;
        if (live_resource_allocation_source(
                allocation, address, size, source_output)) {
            g_live_resource_last_allocation = allocation;
            return 1;
        }
    }
    if (address < 64u * 1024u * 1024u &&
        live_readable_range(address | 0x80000000u, size)) {
        *source_output = address | 0x80000000u;
        return 1;
    }
    if (live_readable_range(address, size)) {
        *source_output = address;
        return 1;
    }
    return 0;
}
static BOOL live_resource_dedupe_insert(
    U32 address, U32 width, U32 height, const char* format
) {
    U32 probe;
    U32 slot = (address ^ width * 0x9E3779B1u ^ height * 0x85EBCA77u) &
        (LIVE_RESOURCE_DEDUPE_CAPACITY - 1u);
    for (probe = 0u; probe < LIVE_RESOURCE_DEDUPE_CAPACITY; ++probe) {
        U32 index = (slot + probe) & (LIVE_RESOURCE_DEDUPE_CAPACITY - 1u);
        if (g_live_resource_dedupe_stamps[index] !=
            g_live_resource_dedupe_generation) {
            g_live_resource_dedupe_stamps[index] =
                g_live_resource_dedupe_generation;
            g_live_resource_dedupe_addresses[index] = address;
            g_live_resource_dedupe_widths[index] = width;
            g_live_resource_dedupe_heights[index] = height;
            g_live_resource_dedupe_formats[index] = format;
            return 1;
        }
        if (g_live_resource_dedupe_addresses[index] == address &&
            g_live_resource_dedupe_widths[index] == width &&
            g_live_resource_dedupe_heights[index] == height &&
            live_string_equal(g_live_resource_dedupe_formats[index], format))
            return 0;
    }
    return 1;
}
static U32 live_collect_resources(
    LiveTextureResource resources[LIVE_RESOURCE_CAPACITY]
) {
    LiveResourceScanState* state = &g_live_resource_scan;
    U32 resource_count = 0u;
    U32 index;
    if (++g_live_resource_dedupe_generation == 0u) {
        for (index = 0u; index < LIVE_RESOURCE_DEDUPE_CAPACITY; ++index)
            g_live_resource_dedupe_stamps[index] = 0u;
        g_live_resource_dedupe_generation = 1u;
    }
    for (index = 0u; index < state->retained_binding_count; ++index) {
        LiveTextureResource candidate;
        candidate.stage = state->frame_binding_stages[index];
        candidate.address = state->frame_binding_addresses[index];
        if (!candidate.address ||
            !live_texture_layout(
                state->frame_binding_formats[index],
                state->frame_binding_image_rects[index],
                &candidate.width, &candidate.height, &candidate.byte_count,
                &candidate.format) ||
            !live_resource_source(
                candidate.address, candidate.byte_count, &candidate.source))
            continue;
        if (live_resource_dedupe_insert(
                candidate.address, candidate.width, candidate.height,
                candidate.format) && resource_count < LIVE_RESOURCE_CAPACITY)
            resources[resource_count++] = candidate;
    }
    for (index = 0u; index < state->retained_vertex_range_count; ++index) {
        LiveTextureResource candidate;
        U32 start = state->retained_vertex_range_starts[index];
        U32 end = state->retained_vertex_range_ends[index];
        if (!start || end <= start || resource_count >= LIVE_RESOURCE_CAPACITY)
            continue;
        candidate.stage = 0u;
        candidate.address = start;
        candidate.width = end - start;
        candidate.height = 1u;
        candidate.byte_count = end - start;
        candidate.format = "VERTEX_BUFFER";
        if (!live_resource_source(
                candidate.address, candidate.byte_count, &candidate.source))
            continue;
        resources[resource_count++] = candidate;
    }
    return resource_count;
}
static BOOL live_bytes_equal(const void* left, const void* right, U32 size) {
    const U8* a = (const U8*)left;
    const U8* b = (const U8*)right;
    U32 index = 0u;
    while (size - index >= 32u) {
        const U32* words_a = (const U32*)(a + index);
        const U32* words_b = (const U32*)(b + index);
        if (words_a[0] != words_b[0] || words_a[1] != words_b[1] ||
            words_a[2] != words_b[2] || words_a[3] != words_b[3] ||
            words_a[4] != words_b[4] || words_a[5] != words_b[5] ||
            words_a[6] != words_b[6] || words_a[7] != words_b[7]) return 0;
        index += 32u;
    }
    for (; index < size; ++index)
        if (a[index] != b[index]) return 0;
    return 1;
}
static void live_hash_resource(LiveTextureResource* resource) {
    LiveSha256 content;
    live_sha256_init(&content);
    live_sha256_update(&content, (const U8*)resource->source,
                       resource->byte_count);
    live_sha256_finish(&content, resource->hash);
}
static BOOL live_prepare_resource_snapshot(
    LiveTextureResource resources[LIVE_RESOURCE_CAPACITY], U32 count
) {
    U64 slot_capacity = (64u * 1024u * 1024u - 64u) / 2u;
    U32 index;
    if (g_live_resource_generation) {
        U32 slot = (U32)(g_live_resource_generation & 1u);
        U32 metadata = slot ? 40u : 24u;
        U32 sequence = live_read_u32(g_live_resources, metadata);
        U32 payload_size = live_read_u32(g_live_resources, metadata + 4u);
        U64 generation = live_read_u64(g_live_resources, metadata + 8u);
        U8* input = g_live_resources + 64u + slot * slot_capacity;
        U32 cursor = 12u;
        BOOL compatible = sequence && !(sequence & 1u) &&
            generation == g_live_resource_generation &&
            payload_size >= cursor && payload_size <= slot_capacity &&
            live_bytes_equal(input, "B2TEX001", 8u) &&
            live_read_u32(input, 8u) == count;
        BOOL changed = 0;
        for (index = 0u; compatible && index < count; ++index) {
            LiveTextureResource* resource = &resources[index];
            U32 format_size = live_string_size(resource->format);
            U8* header;
            U64 required = 56u + format_size + resource->byte_count;
            if ((U64)cursor + required > payload_size) {
                compatible = 0;
                break;
            }
            header = input + cursor;
            if (live_read_u32(header, 0u) != resource->stage ||
                live_read_u32(header, 4u) != resource->address ||
                live_read_u32(header, 8u) != resource->width ||
                live_read_u32(header, 12u) != resource->height ||
                live_read_u32(header, 16u) != format_size ||
                live_read_u32(header, 20u) != resource->byte_count ||
                !live_bytes_equal(header + 56u, resource->format, format_size)) {
                compatible = 0;
                break;
            }
            if (live_bytes_equal(
                    header + 56u + format_size,
                    (const void*)resource->source, resource->byte_count)) {
                copy_bytes(resource->hash, header + 24u, sizeof(resource->hash));
            } else {
                live_hash_resource(resource);
                changed = 1;
            }
            cursor += (U32)required;
        }
        if (compatible && cursor == payload_size) return changed;
    }
    for (index = 0u; index < count; ++index)
        live_hash_resource(&resources[index]);
    return 1;
}
static BOOL live_publish_resources(void) {
    U64 slot_capacity = (64u * 1024u * 1024u - 64u) / 2u;
    U32 resource_count = live_collect_resources(
        g_live_texture_resources);
    if (!live_prepare_resource_snapshot(
            g_live_texture_resources, resource_count)) return 1;
    ++g_live_resource_generation;
    U32 slot = (U32)(g_live_resource_generation & 1u);
    U32 metadata = slot ? 40u : 24u;
    U32 sequence = live_read_u32(g_live_resources, metadata);
    U32 publishing = (sequence + 1u) | 1u;
    U32 published = publishing + 1u;
    U32 payload_size = 12u;
    U32 index;
    U8* output = g_live_resources + 64u + slot * slot_capacity;
    live_write_u32(g_live_resources, metadata, publishing);
    live_write_u64(g_live_resources, metadata + 8u, g_live_resource_generation);
    copy_bytes(output, "B2TEX001", 8u);
    live_write_u32(output, 8u, resource_count);
    for (index = 0u; index < resource_count; ++index) {
        LiveTextureResource* resource = &g_live_texture_resources[index];
        U32 format_size = live_string_size(resource->format);
        U64 required = 56u + format_size + resource->byte_count;
        U8* header;
        if ((U64)payload_size + required > slot_capacity) {
            g_live_render_publication_failure_code = 20u;
            return 0;
        }
        header = output + payload_size;
        live_write_u32(header, 0u, resource->stage);
        live_write_u32(header, 4u, resource->address);
        live_write_u32(header, 8u, resource->width);
        live_write_u32(header, 12u, resource->height);
        live_write_u32(header, 16u, format_size);
        live_write_u32(header, 20u, resource->byte_count);
        copy_bytes(header + 24u, resource->hash, sizeof(resource->hash));
        copy_bytes(header + 56u, resource->format, format_size);
        copy_bytes(header + 56u + format_size, (const U8*)resource->source,
                   resource->byte_count);
        payload_size += (U32)required;
    }
    live_write_u32(g_live_resources, metadata + 4u, payload_size);
    live_barrier();
    live_write_u32(g_live_resources, metadata, published);
    g_live_resource_payload_size = payload_size;
    return 1;
}
"""


def _normal_live_input_audio_c_source() -> str:
    """Emit the native title input/audio bridge used by ordinary live runs."""

    return r"""
static BOOL live_readable_range(U32, U32);
typedef struct LiveControllerState {
    U16 buttons;
    U8 left_trigger, right_trigger;
    short thumb_lx, thumb_ly, thumb_rx, thumb_ry;
    U32 connected, generation;
} LiveControllerState;
typedef struct LiveAudioFormat {
    U16 tag, channels;
    U32 sample_rate;
    U16 block_align, bits_per_sample, samples_per_block;
} LiveAudioFormat;
typedef struct LiveAudioBuffer {
    U32 key, descriptor, voice, data, size, current_position, frequency;
    long volume, left_mixbin_volume, right_mixbin_volume;
    LiveAudioFormat format;
    U32 playing;
} LiveAudioBuffer;
typedef struct LiveAudioPlayback {
    U32 key, active, loop, sample_count;
    short* samples;
    U64 cursor_frame_q32, step_q32;
    U32 loop_start_frame, loop_end_frame;
    long left_gain_q16, right_gain_q16;
} LiveAudioPlayback;
typedef struct LiveAudioStream {
    U32 key;
    LiveAudioFormat format;
    U32 packet_head, packet_tail;
} LiveAudioStream;
typedef struct LiveAudioPacket {
    U32 stream, active, sample_count, cursor, byte_count;
    U32 completed_size, status, completion_event;
    U32 next;
    U64 sequence;
    short* samples;
} LiveAudioPacket;
static LiveControllerState g_live_controller = {
    0u, 0u, 0u, 0, 0, 0, 0, 1u, 0u};
static U32 g_live_controller_sequence;
static U32 g_live_controller_packets[4];
static U32 g_live_controller_last_generation[4];
static U32 g_live_controller_open_mask;
static U64 g_live_controller_refresh_count;
static U64 g_live_controller_poll_count;
static U64 g_live_controller_service_counts[18];
static U32 g_live_controller_observed_buttons;
static U16 g_live_controller_last_left_motor;
static U16 g_live_controller_last_right_motor;
static LiveAudioBuffer g_live_audio_buffers[256];
static LiveAudioPlayback g_live_audio_playbacks[64];
static LiveAudioPlayback* g_live_audio_playback_cache[64];
static U32 g_live_audio_active_playback_masks[2];
static U32 g_live_audio_gain_cache_keys[64];
static long g_live_audio_gain_cache_values[64];
static LiveAudioStream g_live_audio_streams[32];
static volatile U32 g_live_audio_active_stream_mask;
static LiveAudioPacket g_live_audio_packets[64];
static U32 g_live_audio_packet_free_hint;
static U64 g_live_audio_packet_sequence;
static U64 g_live_audio_packet_completion_count;
static U64 g_live_audio_packet_flush_count;
static volatile U32 g_live_audio_lock;
static volatile U32 g_live_audio_lane_started;
static U64 g_live_audio_lane_start_count;
static U64 g_live_audio_lane_start_failure_count;
static U64 g_live_audio_lane_entry_count;
static U64 g_live_audio_lane_active_mix_count;
static U32 g_live_audio_lane_start_error;
static U64 g_live_audio_service_count;
static U64 g_live_audio_service_value_counts[31];
static U64 g_live_audio_effect_image_count;
static U64 g_live_audio_effect_image_failure_count;
static U64 g_live_audio_decoded_buffer_count;
static U64 g_live_audio_decoded_packet_count;
static U64 g_live_audio_decode_failure_count;
static U64 g_live_audio_published_buffer_count;
static U64 g_live_audio_published_byte_count;
static U64 g_live_audio_dropped_buffer_count;
static U64 g_live_audio_music_mode_count;
static U64 g_live_audio_music_load_count;
static U64 g_live_audio_music_failure_count;
static U32 g_live_audio_music_mode;
static U32 g_live_audio_music_active;
static U32 g_live_audio_music_kind;
static U32 g_live_audio_music_track_index = 0xFFFFFFFFu;
static U32 g_live_audio_music_stream_count;
static U32 g_live_audio_last_buffer;
static U32 g_live_audio_last_data;
static U32 g_live_audio_last_size;
static U32 g_live_audio_last_sample_rate;
static U32 g_live_audio_effect_workspace;
static short g_live_audio_mixed[4800];
static long g_live_audio_accumulator[4800];

static void live_audio_lock(void) {
    while (__atomic_exchange_n(&g_live_audio_lock, 1u, 5) != 0u) Sleep(0u);
}
static void live_audio_unlock(void) {
    __atomic_store_n(&g_live_audio_lock, 0u, 5);
}
static void live_refresh_controller(void) {
    U32 sequence, confirmed;
    LiveControllerState next;
    if (!g_live_control) return;
    sequence = live_read_u32(g_live_control, 64u);
    if (!sequence || (sequence & 1u) || sequence == g_live_controller_sequence)
        return;
    copy_bytes(&next.buttons, g_live_control + 68u, 2u);
    next.left_trigger = g_live_control[70u];
    next.right_trigger = g_live_control[71u];
    copy_bytes(&next.thumb_lx, g_live_control + 72u, 2u);
    copy_bytes(&next.thumb_ly, g_live_control + 74u, 2u);
    copy_bytes(&next.thumb_rx, g_live_control + 76u, 2u);
    copy_bytes(&next.thumb_ry, g_live_control + 78u, 2u);
    next.connected = g_live_control[80u] ? 1u : 0u;
    next.generation = sequence;
    live_barrier();
    confirmed = live_read_u32(g_live_control, 64u);
    if (confirmed != sequence || (confirmed & 1u)) return;
    g_live_controller = next;
    g_live_controller_sequence = sequence;
    ++g_live_controller_refresh_count;
}
static U32 live_dispatch_input_service(
    const ServiceDescriptor* service, U32* arguments
) {
    const U32 disconnected = 0x48Fu;
    U32 port, output, index;
    live_refresh_controller();
    ++g_live_controller_poll_count;
    if (service->runtime_kind < 18u)
        ++g_live_controller_service_counts[service->runtime_kind];
    if (service->runtime_kind == 4u)
        return g_live_controller.connected ? 1u : 0u;
    if (service->runtime_kind == 5u) {
        port = arguments[1];
        if (port != 0u || !g_live_controller.connected) return 0u;
        g_live_controller_open_mask |= 1u << port;
        return 0xB2401000u + port;
    }
    port = arguments[0] - 0xB2401000u;
    if (service->runtime_kind == 16u) {
        if (port == 0u) {
            g_live_controller_open_mask &= ~(1u << port);
            g_live_controller_last_generation[port] = 0u;
        }
        return 0u;
    }
    output = arguments[1];
    if (port != 0u || !g_live_controller.connected ||
        !(g_live_controller_open_mask & (1u << port)))
        return disconnected;
    if (service->runtime_kind == 6u) {
        if (output) {
            *(U8*)output = 1u;
            *(U8*)(output + 1u) = 1u;
            for (index = 2u; index < 20u; ++index) *(U8*)(output + index) = 0u;
        }
        return 0u;
    }
    if (service->runtime_kind == 7u) {
        static const U16 analog_masks[6] = {
            0x1000u, 0x2000u, 0x4000u, 0x8000u, 0x0100u, 0x0200u};
        U16 digital;
        short sticks[4];
        if (!output) return 0u;
        if (g_live_controller_last_generation[0] !=
            g_live_controller.generation) {
            ++g_live_controller_packets[0];
            g_live_controller_last_generation[0] =
                g_live_controller.generation;
        }
        g_live_controller_observed_buttons |= g_live_controller.buttons;
        *(U32*)output = g_live_controller_packets[0];
        digital = g_live_controller.buttons & 0x00FFu;
        *(U16*)(output + 4u) = digital;
        for (index = 0u; index < 6u; ++index)
            *(U8*)(output + 6u + index) =
                (g_live_controller.buttons & analog_masks[index]) ? 0xFFu : 0u;
        *(U8*)(output + 12u) = g_live_controller.left_trigger;
        *(U8*)(output + 13u) = g_live_controller.right_trigger;
        sticks[0] = g_live_controller.thumb_lx;
        sticks[1] = g_live_controller.thumb_ly;
        sticks[2] = g_live_controller.thumb_rx;
        sticks[3] = g_live_controller.thumb_ry;
        for (index = 0u; index < 4u; ++index)
            *(short*)(output + 14u + index * 2u) = sticks[index];
        return 0u;
    }
    if (service->runtime_kind == 17u) {
        if (!output) return 0x57u;
        *(U32*)output = 0u;
        *(U8*)(output + 0x40u) = 0u;
        *(U8*)(output + 0x41u) = (U8)(port + 2u);
        g_live_controller_last_left_motor = *(U16*)(output + 0x42u);
        g_live_controller_last_right_motor = *(U16*)(output + 0x44u);
        return 0u;
    }
    return disconnected;
}
static LiveAudioFormat live_audio_format(U32 address) {
    LiveAudioFormat format = {0};
    if (!address) return format;
    format.tag = *(U16*)address;
    format.channels = *(U16*)(address + 2u);
    format.sample_rate = *(U32*)(address + 4u);
    format.block_align = *(U16*)(address + 0xCu);
    format.bits_per_sample = *(U16*)(address + 0xEu);
    format.samples_per_block = *(U16*)(address + 0x12u);
    return format;
}
static LiveAudioBuffer* live_audio_buffer(U32 key, BOOL create) {
    U32 index, probe, start;
    LiveAudioBuffer* free_slot = 0;
    if (!key) return 0;
    start = (key >> 4u) & 255u;
    for (probe = 0u; probe < 256u; ++probe) {
        index = (start + probe) & 255u;
        if (g_live_audio_buffers[index].key == key)
            return &g_live_audio_buffers[index];
        if (!g_live_audio_buffers[index].key && !free_slot)
            free_slot = &g_live_audio_buffers[index];
    }
    if (!create || !free_slot) return 0;
    free_slot->key = key;
    return free_slot;
}
static LiveAudioStream* live_audio_stream(U32 key, BOOL create) {
    U32 index, probe, start;
    LiveAudioStream* free_slot = 0;
    if (!key) return 0;
    start = (key >> 4u) & 31u;
    for (probe = 0u; probe < 32u; ++probe) {
        index = (start + probe) & 31u;
        if (g_live_audio_streams[index].key == key)
            return &g_live_audio_streams[index];
        if (!g_live_audio_streams[index].key && !free_slot)
            free_slot = &g_live_audio_streams[index];
    }
    if (!create || !free_slot) return 0;
    free_slot->key = key;
    __atomic_fetch_or(
        &g_live_audio_active_stream_mask,
        1u << (U32)(free_slot - g_live_audio_streams), 5);
    return free_slot;
}
static void live_audio_complete_packet(LiveAudioPacket* packet, U32 status) {
    if (!packet || !packet->active) return;
    if (packet->completed_size)
        *(U32*)packet->completed_size = status ? 0u : packet->byte_count;
    if (packet->status) *(U32*)packet->status = status;
    if (packet->completion_event) SetEvent((HANDLE)packet->completion_event);
    packet->active = 0u;
    g_live_audio_packet_free_hint = (U32)(packet - g_live_audio_packets);
    if (packet->samples) VirtualFree(packet->samples, 0u, 0x8000u);
    packet->samples = 0;
    packet->next = 0u;
    if (status == 0x8000000Bu) ++g_live_audio_packet_flush_count;
    else ++g_live_audio_packet_completion_count;
}
static long live_audio_clamp(long value) {
    if (value < -32768) return -32768;
    if (value > 32767) return 32767;
    return value;
}
static long live_audio_gain(long volume) {
    U32 attenuation, cache_slot, cache_key;
    U64 factor = 0xFFB497A2ull;
    U64 gain = 1ull << 32u;
    long result;
    if (volume <= -10000) return 0;
    if (volume >= 0) return 65536;
    attenuation = (U32)(-volume);
    cache_slot = attenuation & 63u;
    cache_key = attenuation + 1u;
    if (g_live_audio_gain_cache_keys[cache_slot] == cache_key)
        return g_live_audio_gain_cache_values[cache_slot];
    while (attenuation) {
        if (attenuation & 1u)
            gain = (gain * factor + 0x80000000ull) >> 32u;
        factor = (factor * factor + 0x80000000ull) >> 32u;
        attenuation >>= 1u;
    }
    result = (long)((gain + 0x8000u) >> 16u);
    g_live_audio_gain_cache_values[cache_slot] = result;
    g_live_audio_gain_cache_keys[cache_slot] = cache_key;
    return result;
}
static long live_audio_combined_gain(long volume, long mixbin_volume) {
    if (volume > 0) volume = 0;
    if (mixbin_volume > 0) mixbin_volume = 0;
    if (volume <= -10000 || mixbin_volume <= -10000 ||
        volume + mixbin_volume <= -10000) return 0;
    return live_audio_gain(volume + mixbin_volume);
}
static void live_audio_refresh_playback_gain(
    LiveAudioPlayback* playback, LiveAudioBuffer* buffer
) {
    playback->left_gain_q16 = live_audio_combined_gain(
        buffer->volume, buffer->left_mixbin_volume);
    playback->right_gain_q16 = live_audio_combined_gain(
        buffer->volume, buffer->right_mixbin_volume);
}
static LiveAudioPlayback* live_audio_playback(U32 key) {
    U32 index, slot = (key >> 4u) & 63u;
    LiveAudioPlayback* cached = g_live_audio_playback_cache[slot];
    if (cached && cached->active && cached->key == key) return cached;
    for (index = 0u; index < 64u; ++index) {
        LiveAudioPlayback* playback = &g_live_audio_playbacks[index];
        if (playback->active && playback->key == key) {
            g_live_audio_playback_cache[slot] = playback;
            return playback;
        }
    }
    return 0;
}
static void live_audio_mark_playback(LiveAudioPlayback* playback) {
    U32 index;
    if (!playback) return;
    index = (U32)(playback - g_live_audio_playbacks);
    g_live_audio_active_playback_masks[index >> 5u] |=
        1u << (index & 31u);
}
static LiveAudioPlayback* live_audio_reserve_playback(void) {
    U32 mask_word;
    for (mask_word = 0u; mask_word < 2u; ++mask_word) {
        U32 available = ~g_live_audio_active_playback_masks[mask_word];
        while (available) {
            U32 bit = (U32)__builtin_ctz(available);
            LiveAudioPlayback* playback =
                &g_live_audio_playbacks[mask_word * 32u + bit];
            available &= available - 1u;
            if (playback->active) continue;
            playback->active = 2u;
            live_audio_mark_playback(playback);
            return playback;
        }
    }
    return 0;
}
static void live_audio_forget_playback(LiveAudioPlayback* playback) {
    U32 index, slot;
    if (!playback) return;
    index = (U32)(playback - g_live_audio_playbacks);
    g_live_audio_active_playback_masks[index >> 5u] &=
        ~(1u << (index & 31u));
    slot = (playback->key >> 4u) & 63u;
    if (g_live_audio_playback_cache[slot] == playback)
        g_live_audio_playback_cache[slot] = 0;
}
static U64 live_audio_div_u64_u32(U64 numerator, U32 denominator) {
    U64 quotient = 0u, remainder = 0u;
    int bit;
    if (!denominator) return 0u;
    for (bit = 63; bit >= 0; --bit) {
        remainder = (remainder << 1u) | ((numerator >> bit) & 1u);
        if (remainder >= denominator) {
            remainder -= denominator;
            quotient |= 1ull << bit;
        }
    }
    return quotient;
}
static U64 live_audio_step(U32 frequency, U32 original) {
    U64 effective;
    if (!original) return 1ull << 32u;
    effective = frequency ? frequency : original;
    return live_audio_div_u64_u32(
        effective * (1ull << 32u) + original / 2u, original);
}
static BOOL live_audio_normalized_frames(
    LiveAudioFormat format, U32 byte_count, U32* frame_count
) {
    U64 source_frames, normalized;
    if (!frame_count || !format.sample_rate) return 0;
    if (format.tag == 1u && format.bits_per_sample == 16u &&
        (format.channels == 1u || format.channels == 2u)) {
        U32 bytes_per_frame = format.channels * 2u;
        if (byte_count % bytes_per_frame) return 0;
        source_frames = byte_count / bytes_per_frame;
    } else if (format.tag == 0x69u &&
               (format.channels == 1u || format.channels == 2u)) {
        U32 block_align = format.channels * 36u;
        if (byte_count % block_align) return 0;
        source_frames = (U64)(byte_count / block_align) * 64u;
    } else return 0;
    normalized = live_audio_div_u64_u32(
        source_frames * 48000u + format.sample_rate / 2u,
        format.sample_rate);
    if (normalized > 0xFFFFFFFFull) return 0;
    *frame_count = (U32)normalized;
    return 1;
}
static BOOL live_audio_frame_byte_position(
    LiveAudioFormat format, U32 normalized_frame, U32* byte_position
) {
    U64 source_frames, position;
    if (!byte_position || !format.sample_rate) return 0;
    source_frames = live_audio_div_u64_u32(
        (U64)normalized_frame * format.sample_rate, 48000u);
    if (format.tag == 1u && format.bits_per_sample == 16u &&
        (format.channels == 1u || format.channels == 2u)) {
        position = source_frames * format.channels * 2u;
    } else if (format.tag == 0x69u &&
               (format.channels == 1u || format.channels == 2u)) {
        position = source_frames / 64u * format.channels * 36u;
    } else return 0;
    if (position > 0xFFFFFFFFull) return 0;
    *byte_position = (U32)position;
    return 1;
}
static BOOL live_audio_decode_adpcm_channel(const U8* block, short* output) {
    static const int index_table[16] = {
        -1,-1,-1,-1,2,4,6,8,-1,-1,-1,-1,2,4,6,8};
    static const int step_table[89] = {
        7,8,9,10,11,12,13,14,16,17,19,21,23,25,28,31,34,37,41,45,50,
        55,60,66,73,80,88,97,107,118,130,143,157,173,190,209,230,253,
        279,307,337,371,408,449,494,544,598,658,724,796,876,963,1060,
        1166,1282,1411,1552,1707,1878,2066,2272,2499,2749,3024,3327,
        3660,4026,4428,4871,5358,5894,6484,7132,7845,8630,9493,10442,
        11487,12635,13899,15289,16818,18500,20350,22385,24623,27086,
        29794,32767};
    int predictor = *(const short*)block;
    int step_index = block[2];
    U32 byte_index, half, sample = 1u;
    if (step_index >= 89) return 0;
    output[0] = (short)predictor;
    for (byte_index = 4u; byte_index < 36u && sample < 64u; ++byte_index) {
        U8 packed = block[byte_index];
        for (half = 0u; half < 2u && sample < 64u; ++half) {
            U8 nibble = half ? packed >> 4u : packed & 15u;
            int step = step_table[step_index];
            int difference = step >> 3;
            if (nibble & 1u) difference += step >> 2;
            if (nibble & 2u) difference += step >> 1;
            if (nibble & 4u) difference += step;
            predictor += (nibble & 8u) ? -difference : difference;
            predictor = live_audio_clamp(predictor);
            step_index += index_table[nibble];
            if (step_index < 0) step_index = 0;
            if (step_index > 88) step_index = 88;
            output[sample++] = (short)predictor;
        }
    }
    return sample == 64u;
}
static BOOL live_audio_decode(
    U32 data, U32 size, LiveAudioFormat format,
    short** output, U32* output_samples
) {
    U64 source_frames, frames, bytes;
    short* decoded;
    U32 frame;
    if (!data || !size || !output || !output_samples ||
        (format.channels != 1u && format.channels != 2u) ||
        format.sample_rate < 4000u || format.sample_rate > 192000u)
        return 0;
    source_frames = 0u;
    if (format.tag == 1u && format.bits_per_sample == 16u &&
        !(size % (format.channels * 2u))) {
        source_frames = size / (format.channels * 2u);
    } else if (format.tag == 0x69u && !(size % (format.channels * 36u))) {
        source_frames = (U64)(size / (format.channels * 36u)) * 64u;
    } else return 0;
    frames = live_audio_div_u64_u32(
        source_frames * 48000u + format.sample_rate / 2u,
        format.sample_rate);
    bytes = frames * 4u;
    if (!frames || frames > 32u * 1024u * 1024u || bytes > 0x7FFFFFFFu)
        return 0;
    decoded = (short*)VirtualAlloc(0, (U32)bytes, 0x3000u, 0x04u);
    if (!decoded) return 0;
    if (format.tag == 1u) {
        const short* input = (const short*)data;
        for (frame = 0u; frame < (U32)frames; ++frame) {
            U64 position = (U64)frame * format.sample_rate;
            U32 source = (U32)live_audio_div_u64_u32(position, 48000u);
            U32 next = source + 1u < source_frames ? source + 1u : source;
            U64 fraction = live_audio_div_u64_u32(
                (position - (U64)source * 48000u) * (1ull << 32u),
                48000u);
            long left0 = input[source * format.channels];
            long left1 = input[next * format.channels];
            long right0 = format.channels == 2u
                ? input[source * 2u + 1u] : left0;
            long right1 = format.channels == 2u
                ? input[next * 2u + 1u] : left1;
            decoded[frame * 2u] = (short)(
                left0 + ((long long)(left1 - left0) * fraction >> 32u));
            decoded[frame * 2u + 1u] = (short)(
                right0 + ((long long)(right1 - right0) * fraction >> 32u));
        }
    } else {
        U32 block_align = format.channels * 36u;
        U32 block_offset, decoded_frame = 0u;
        short channels[2][64];
        U8 block[36];
        for (block_offset = 0u; block_offset < size;
             block_offset += block_align) {
            U32 channel, group, sample;
            for (channel = 0u; channel < format.channels; ++channel) {
                copy_bytes(block, (const void*)(data + block_offset + channel * 4u), 4u);
                for (group = 0u; group < 8u; ++group)
                    copy_bytes(
                        block + 4u + group * 4u,
                        (const void*)(data + block_offset + format.channels * 4u +
                            group * format.channels * 4u + channel * 4u), 4u);
                if (!live_audio_decode_adpcm_channel(block, channels[channel])) {
                    VirtualFree(decoded, 0u, 0x8000u);
                    return 0;
                }
            }
            for (sample = 0u; sample < 64u; ++sample) {
                if (format.sample_rate == 48000u) {
                    decoded[decoded_frame * 2u] = channels[0][sample];
                    decoded[decoded_frame * 2u + 1u] = format.channels == 2u
                        ? channels[1][sample] : channels[0][sample];
                } else {
                    U32 normalized = (U32)live_audio_div_u64_u32(
                        (U64)decoded_frame * 48000u + format.sample_rate / 2u,
                        format.sample_rate);
                    U32 normalized_end = (U32)live_audio_div_u64_u32(
                        (U64)(decoded_frame + 1u) * 48000u +
                            format.sample_rate / 2u,
                        format.sample_rate);
                    while (normalized < normalized_end && normalized < frames) {
                        decoded[normalized * 2u] = channels[0][sample];
                        decoded[normalized * 2u + 1u] = format.channels == 2u
                            ? channels[1][sample] : channels[0][sample];
                        ++normalized;
                    }
                }
                ++decoded_frame;
            }
        }
    }
    *output = decoded;
    *output_samples = (U32)frames * 2u;
    return 1;
}
static BOOL live_audio_publish(const short* samples, U32 sample_count) {
    U32 waited = 0u, sequence, publishing, published, byte_count;
    if (!g_live_control || !samples || !sample_count) return 0;
    byte_count = sample_count * 2u;
    if (byte_count > 16384u || byte_count & 3u) return 0;
    for (;;) {
        sequence = live_read_u32(g_live_control, 8192u);
        if ((!sequence || live_read_u32(g_live_control, 8196u) == sequence) &&
            !(sequence & 1u)) break;
        if (live_read_u32(g_live_control, 32u) || ++waited >= 500u) return 0;
        Sleep(1u);
    }
    publishing = (sequence + 1u) | 1u;
    published = publishing + 1u;
    live_write_u32(g_live_control, 8192u, publishing);
    live_write_u32(g_live_control, 8200u, byte_count);
    copy_bytes(g_live_control + 8224u, samples, byte_count);
    live_barrier();
    live_write_u32(g_live_control, 8192u, published);
    if (!SetEvent(g_live_publication_event)) return 0;
    ++g_live_audio_published_buffer_count;
    g_live_audio_published_byte_count += byte_count;
    return 1;
}
static U32 __stdcall live_audio_lane(void* ignored) {
    U64 frequency = 0u;
    U64 interval;
    U64 next_deadline = 0u;
    (void)ignored;
    ++g_live_audio_lane_entry_count;
    if (!QueryPerformanceFrequency(&frequency) || !frequency) {
        g_live_audio_lane_start_error = 1u;
        ++g_live_audio_lane_start_failure_count;
        live_write_summary();
        ExitProcess(73u);
    }
    interval = (frequency + 10u) / 20u;
    if (!interval) interval = 1u;
    for (;;) {
        U32 index, mask_word, active = 0u;
        if (live_read_u32(g_live_control, 32u)) return 0u;
        if ((g_live_audio_active_playback_masks[0] |
             g_live_audio_active_playback_masks[1] |
             __atomic_load_n(&g_live_audio_active_stream_mask, 5)) == 0u) {
            next_deadline = 0u;
            Sleep(5u);
            continue;
        }
        for (index = 0u; index < 4800u; index += 8u) {
            g_live_audio_accumulator[index] = 0;
            g_live_audio_accumulator[index + 1u] = 0;
            g_live_audio_accumulator[index + 2u] = 0;
            g_live_audio_accumulator[index + 3u] = 0;
            g_live_audio_accumulator[index + 4u] = 0;
            g_live_audio_accumulator[index + 5u] = 0;
            g_live_audio_accumulator[index + 6u] = 0;
            g_live_audio_accumulator[index + 7u] = 0;
        }
        live_audio_lock();
        for (mask_word = 0u; mask_word < 2u; ++mask_word) {
            U32 mask = g_live_audio_active_playback_masks[mask_word];
            while (mask) {
            U32 bit = (U32)__builtin_ctz(mask);
            index = mask_word * 32u + bit;
            mask &= mask - 1u;
            LiveAudioPlayback* playback = &g_live_audio_playbacks[index];
            U32 sample;
            if (!playback->active) continue;
            active = 1u;
            for (sample = 0u; sample < 4800u; sample += 2u) {
                U32 frame = (U32)(playback->cursor_frame_q32 >> 32u);
                U32 end = playback->loop ? playback->loop_end_frame
                                         : playback->sample_count / 2u;
                U32 next;
                U64 fraction;
                long left, right;
                if (frame >= end) {
                    if (!playback->loop ||
                        playback->loop_start_frame >= playback->loop_end_frame) {
                        LiveAudioBuffer* buffer =
                            live_audio_buffer(playback->key, 0);
                        if (buffer) buffer->playing = 0u;
                        live_audio_forget_playback(playback);
                        playback->active = 0u;
                        VirtualFree(playback->samples, 0u, 0x8000u);
                        playback->samples = 0;
                        break;
                    }
                    frame = playback->loop_start_frame;
                    playback->cursor_frame_q32 = (U64)frame << 32u;
                }
                next = frame + 1u < end ? frame + 1u :
                    (playback->loop ? playback->loop_start_frame : frame);
                fraction = playback->cursor_frame_q32 & 0xFFFFFFFFull;
                if (playback->step_q32 == (1ull << 32u) && fraction == 0u) {
                    left = playback->samples[frame * 2u];
                    right = playback->samples[frame * 2u + 1u];
                } else {
                    left = playback->samples[frame * 2u] +
                        (long)(((long long)(playback->samples[next * 2u] -
                            playback->samples[frame * 2u]) * fraction) >> 32u);
                    right = playback->samples[frame * 2u + 1u] +
                        (long)(((long long)(playback->samples[next * 2u + 1u] -
                            playback->samples[frame * 2u + 1u]) * fraction) >> 32u);
                }
                g_live_audio_accumulator[sample] +=
                    left * playback->left_gain_q16 / 65536;
                g_live_audio_accumulator[sample + 1u] +=
                    right * playback->right_gain_q16 / 65536;
                playback->cursor_frame_q32 += playback->step_q32;
            }
            }
        }
        {
        U32 stream_mask = __atomic_load_n(&g_live_audio_active_stream_mask, 5);
        while (stream_mask) {
            U32 output = 0u;
            U32 bit = (U32)__builtin_ctz(stream_mask);
            index = bit;
            stream_mask &= stream_mask - 1u;
            LiveAudioStream* stream = &g_live_audio_streams[index];
            if (!stream->key) continue;
            while (output < 4800u) {
                U32 packet_index = stream->packet_head;
                LiveAudioPacket* packet = packet_index
                    ? &g_live_audio_packets[packet_index - 1u] : 0;
                if (!packet) break;
                active = 1u;
                while (output < 4800u && packet->cursor < packet->sample_count)
                    g_live_audio_accumulator[output++] +=
                        packet->samples[packet->cursor++];
                if (packet->cursor < packet->sample_count) break;
                stream->packet_head = packet->next;
                if (!stream->packet_head) stream->packet_tail = 0u;
                live_audio_complete_packet(packet, 0u);
            }
        }
        }
        for (index = 0u; index < 4800u; ++index)
            g_live_audio_mixed[index] = (short)live_audio_clamp(
                g_live_audio_accumulator[index]);
        live_audio_unlock();
        if (active) {
            U64 now = 0u;
            ++g_live_audio_lane_active_mix_count;
            if (!live_audio_publish(g_live_audio_mixed, 4800u))
                ++g_live_audio_dropped_buffer_count;
            if (!QueryPerformanceCounter(&now)) {
                g_live_audio_lane_start_error = 2u;
                ++g_live_audio_lane_start_failure_count;
                live_write_summary();
                ExitProcess(73u);
            }
            if (!next_deadline) next_deadline = now + interval;
            while (now < next_deadline) {
                if (live_read_u32(g_live_control, 32u)) return 0u;
                Sleep(1u);
                if (!QueryPerformanceCounter(&now)) {
                    g_live_audio_lane_start_error = 2u;
                    ++g_live_audio_lane_start_failure_count;
                    live_write_summary();
                    ExitProcess(73u);
                }
            }
            next_deadline += interval;
            if (next_deadline <= now) next_deadline = now + interval;
        } else {
            next_deadline = 0u;
            Sleep(5u);
        }
    }
}
static BOOL live_start_audio_lane(void) {
    HANDLE thread;
    if (g_live_audio_lane_started) return 1;
    ++g_live_audio_lane_start_count;
    thread = CreateThread(
        0, 64u * 1024u, live_audio_lane, 0, 0x00010000u, 0);
    if (!thread) {
        g_live_audio_lane_start_error = GetLastError();
        ++g_live_audio_lane_start_failure_count;
        return 0;
    }
    g_live_audio_lane_started = 1u;
    CloseHandle(thread);
    return 1;
}
static void live_audio_stop_buffer(U32 key) {
    LiveAudioBuffer* buffer = live_audio_buffer(key, 0);
    LiveAudioPlayback* playback;
    live_audio_lock();
    playback = live_audio_playback(key);
    if (playback) {
        live_audio_forget_playback(playback);
        playback->active = 0u;
        VirtualFree(playback->samples, 0u, 0x8000u);
        playback->samples = 0;
    }
    if (buffer) buffer->playing = 0u;
    live_audio_unlock();
}
static BOOL live_audio_asset_path(
    const char* relative, char* output, U32 capacity
) {
    U32 cursor = 0u, index = 0u;
    while (g_live_extracted_root[cursor]) {
        if (cursor + 2u >= capacity) return 0;
        output[cursor] = g_live_extracted_root[cursor];
        ++cursor;
    }
    if (cursor && output[cursor - 1u] != '\\' && output[cursor - 1u] != '/')
        output[cursor++] = '\\';
    while (relative[index]) {
        if (cursor + 1u >= capacity) return 0;
        output[cursor++] = relative[index] == '/' ? '\\' : relative[index];
        ++index;
    }
    output[cursor] = 0;
    return cursor != 0u;
}
static BOOL live_audio_read_asset(
    const char* relative, U8** payload_output, U32* size_output
) {
    const U32 generic_read = 0x80000000u;
    HANDLE file;
    U8* payload;
    U32 size, high = 0u, offset = 0u;
    char path[1536];
    if (!payload_output || !size_output ||
        !live_audio_asset_path(relative, path, sizeof(path))) return 0;
    file = CreateFileA(path, generic_read, 1u, 0, 3u, 0x80u, 0);
    if (file == (HANDLE)-1) return 0;
    size = GetFileSize(file, &high);
    if (high || !size || size > 128u * 1024u * 1024u) {
        CloseHandle(file);
        return 0;
    }
    payload = (U8*)VirtualAlloc(0, size, 0x3000u, 0x04u);
    if (!payload) {
        CloseHandle(file);
        return 0;
    }
    while (offset < size) {
        U32 request = size - offset, read = 0u;
        if (request > 0x00100000u) request = 0x00100000u;
        if (!ReadFile(file, payload + offset, request, &read, 0) ||
            read != request) {
            CloseHandle(file);
            VirtualFree(payload, 0u, 0x8000u);
            return 0;
        }
        offset += read;
    }
    CloseHandle(file);
    *payload_output = payload;
    *size_output = size;
    return 1;
}
static void live_audio_stop_music(void) {
    const U32 music_key = 0x31F10600u;
    U32 index;
    for (index = 0u; index < 2u; ++index)
        live_audio_stop_buffer(music_key + index);
    g_live_audio_music_active = 0u;
    g_live_audio_music_kind = 0u;
    g_live_audio_music_track_index = 0xFFFFFFFFu;
    g_live_audio_music_stream_count = 0u;
}
static BOOL live_audio_rws_track(
    const char* relative, U32 music_kind, U32 track_index
) {
    const U32 music_key = 0x31F10600u;
    U8* payload = 0;
    U8* encoded = 0;
    short* samples[2] = {0, 0};
    LiveAudioPlayback* playbacks[2] = {0, 0};
    LiveAudioFormat format = {0};
    U32 size = 0u, header_size, data_offset, data_size;
    U32 substream_count, packet_size;
    U32 declared_sizes[2] = {0u, 0u};
    U32 descriptor_offsets[2] = {0u, 0u};
    U32 descriptor_audio[2] = {0u, 0u};
    U32 sample_counts[2] = {0u, 0u};
    U32 descriptor_count = 0u;
    U32 sample_rate = 0u, offset, stream, index;
    if (!live_audio_read_asset(relative, &payload, &size)) goto failure;
    if (size < 0x9Cu || *(U32*)payload != 0x80Du ||
        (U64)*(U32*)(payload + 4u) + 12ull > size ||
        *(U32*)(payload + 12u) != 0x80Eu) goto failure;
    header_size = *(U32*)(payload + 16u);
    if ((U64)header_size + 24ull > size) goto failure;
    data_offset = header_size + 24u;
    if ((U64)data_offset + 12ull > size ||
        *(U32*)(payload + data_offset) != 0x80Fu) goto failure;
    data_size = *(U32*)(payload + data_offset + 4u);
    if ((U64)data_offset + 12ull + data_size > size) goto failure;
    substream_count = *(U32*)(payload + 0x40u);
    packet_size = *(U32*)(payload + 0x4Cu);
    if (!substream_count || substream_count > 2u || !packet_size ||
        0x98u + substream_count * 4u > data_offset) goto failure;
    for (stream = 0u; stream < substream_count; ++stream) {
        declared_sizes[stream] = *(U32*)(payload + 0x98u + stream * 4u);
        if (!declared_sizes[stream] || declared_sizes[stream] > data_size)
            goto failure;
    }
    for (offset = 12u; offset + 4u <= data_offset; ++offset) {
        U32 candidate = *(U32*)(payload + offset);
        if (candidate == 48000u || candidate == 44100u ||
            candidate == 32000u || candidate == 22050u) {
            sample_rate = candidate;
            break;
        }
    }
    if (!sample_rate) goto failure;
    for (offset = 0x80u; offset + 0x1Cu <= data_offset; offset += 4u) {
        U32 flags = *(U32*)(payload + offset);
        U32 span = *(U32*)(payload + offset + 4u);
        U32 format_flags = *(U32*)(payload + offset + 0xCu);
        U32 audio_size = *(U32*)(payload + offset + 0x14u);
        U32 packet_offset = *(U32*)(payload + offset + 0x18u);
        if (flags == 7u && format_flags == 0x00040004u && span &&
            span <= packet_size && audio_size && audio_size <= span &&
            packet_offset < packet_size &&
            (U64)packet_offset + span <= packet_size) {
            if (descriptor_count >= substream_count) goto failure;
            descriptor_offsets[descriptor_count] = packet_offset;
            descriptor_audio[descriptor_count] = audio_size;
            ++descriptor_count;
        }
    }
    if (descriptor_count != substream_count) goto failure;
    format.tag = 0x69u;
    format.channels = 2u;
    format.sample_rate = sample_rate;
    format.block_align = 72u;
    format.bits_per_sample = 4u;
    format.samples_per_block = 64u;
    for (stream = 0u; stream < substream_count; ++stream) {
        U32 encoded_size = 0u;
        encoded = (U8*)VirtualAlloc(
            0, declared_sizes[stream], 0x3000u, 0x04u);
        if (!encoded) goto failure;
        for (offset = 0u;
             offset < data_size && encoded_size < declared_sizes[stream];
             offset += packet_size) {
            U32 source = offset + descriptor_offsets[stream];
            U32 copied;
            if (source >= data_size) break;
            copied = descriptor_audio[stream];
            if (copied > data_size - source) copied = data_size - source;
            if (copied > declared_sizes[stream] - encoded_size)
                copied = declared_sizes[stream] - encoded_size;
            copy_bytes(
                encoded + encoded_size,
                payload + data_offset + 12u + source,
                copied);
            encoded_size += copied;
        }
        if (encoded_size != declared_sizes[stream] || encoded_size % 72u ||
            !live_audio_decode(
                (U32)encoded, encoded_size, format,
                &samples[stream], &sample_counts[stream])) goto failure;
        VirtualFree(encoded, 0u, 0x8000u);
        encoded = 0;
    }
    if (!live_start_audio_lane()) goto failure;
    live_audio_stop_music();
    live_audio_lock();
    for (stream = 0u; stream < substream_count; ++stream) {
        playbacks[stream] = live_audio_reserve_playback();
        if (!playbacks[stream]) {
            for (index = 0u; index < stream; ++index) {
                live_audio_forget_playback(playbacks[index]);
                playbacks[index]->active = 0u;
            }
            live_audio_unlock();
            goto failure;
        }
    }
    for (stream = 0u; stream < substream_count; ++stream) {
        LiveAudioPlayback* playback = playbacks[stream];
        playback->key = music_key + stream;
        g_live_audio_playback_cache[(playback->key >> 4u) & 63u] = playback;
        playback->active = 1u;
        playback->loop = 1u;
        playback->sample_count = sample_counts[stream];
        playback->samples = samples[stream];
        playback->cursor_frame_q32 = 0u;
        playback->step_q32 = 1ull << 32u;
        playback->loop_start_frame = 0u;
        playback->loop_end_frame = sample_counts[stream] / 2u;
        playback->left_gain_q16 = 32768;
        playback->right_gain_q16 = 32768;
        samples[stream] = 0;
    }
    live_audio_unlock();
    VirtualFree(payload, 0u, 0x8000u);
    ++g_live_audio_music_load_count;
    g_live_audio_music_active = 1u;
    g_live_audio_music_kind = music_kind;
    g_live_audio_music_track_index = track_index;
    g_live_audio_music_stream_count = substream_count;
    return 1;
failure:
    for (index = 0u; index < 2u; ++index)
        if (samples[index]) VirtualFree(samples[index], 0u, 0x8000u);
    if (encoded) VirtualFree(encoded, 0u, 0x8000u);
    if (payload) VirtualFree(payload, 0u, 0x8000u);
    ++g_live_audio_music_failure_count;
    return 0;
}
static BOOL live_audio_gameplay_track(
    char* output, U32 capacity, U32* track_index_output
) {
    static const char suffix[] = "mgst.rws";
    U32 track_index = *(volatile U32*)0x0048A150u;
    const char* base;
    U32 cursor = 0u, index;
    if (!output || !track_index_output || track_index >= 30u) return 0;
    base = *(const char**)(0x0033FDD8u + track_index * 4u);
    if ((U32)base < 0x002C54D4u || (U32)base > 0x002C5564u ||
        !live_readable_range((U32)base, 16u) ||
        base[0] != 'm' || base[1] != 'u' || base[2] != 's' ||
        base[3] != 'i' || base[4] != 'c' ||
        (base[5] != '0' && base[5] != '1') || base[6] != '\\') return 0;
    while (base[cursor]) {
        if (cursor + sizeof(suffix) >= capacity) return 0;
        output[cursor] = base[cursor];
        ++cursor;
    }
    for (index = 0u; index < sizeof(suffix); ++index)
        output[cursor + index] = suffix[index];
    *track_index_output = track_index;
    return 1;
}
static U32 live_audio_set_music_mode(U32 holder, U32 mode) {
    U32 object = holder ? *(U32*)holder : 0u;
    U32 track_index = 0xFFFFFFFFu;
    BOOL has_gameplay_track = 0;
    char track[96];
    ++g_live_audio_music_mode_count;
    if (object) *(U32*)(object + 0x38u) = mode;
    if (mode == 3u)
        has_gameplay_track = live_audio_gameplay_track(
            track, sizeof(track), &track_index);
    if ((mode == 2u && g_live_audio_music_active &&
         g_live_audio_music_kind == 1u) ||
        (mode == 3u && has_gameplay_track && g_live_audio_music_active &&
         g_live_audio_music_kind == 2u &&
         g_live_audio_music_track_index == track_index) ||
        (mode == 4u && g_live_audio_music_active &&
         g_live_audio_music_kind == 3u)) {
        g_live_audio_music_mode = mode;
        return 1u;
    }
    live_audio_stop_music();
    if (mode == 2u)
        live_audio_rws_track("music0/trk07menust.rws", 1u, 0xFFFFFFFFu);
    else if (mode == 3u && has_gameplay_track)
        live_audio_rws_track(track, 2u, track_index);
    else if (mode == 4u)
        live_audio_rws_track("music0/creditsst.rws", 3u, 0xFFFFFFFFu);
    g_live_audio_music_mode = mode;
    return 1u;
}
static U32 live_audio_play(U32 key, U32 flags) {
    LiveAudioBuffer* buffer = live_audio_buffer(key, 0);
    LiveAudioPlayback* playback = 0;
    short* samples = 0;
    U32 sample_count = 0u, play_start, play_length, loop_start, loop_length;
    U32 current_frame = 0u, loop_start_frame = 0u, loop_end_frame = 0u;
    if (!buffer || !buffer->data || !buffer->size) return 0u;
    play_start = buffer->descriptor ? *(U32*)(buffer->descriptor + 0xC0u) : 0u;
    play_length = buffer->descriptor ? *(U32*)(buffer->descriptor + 0xC4u) : 0u;
    if (!play_length) play_length = buffer->size;
    loop_start = buffer->descriptor ? *(U32*)(buffer->descriptor + 0xC8u) : 0u;
    loop_length = buffer->descriptor ? *(U32*)(buffer->descriptor + 0xCCu) : 0u;
    if (!loop_length) loop_length = play_length - loop_start;
    if ((U64)play_start + play_length > buffer->size ||
        (U64)loop_start + loop_length > play_length) return 0u;
    if (!live_audio_normalized_frames(
            buffer->format, buffer->current_position, &current_frame) ||
        !live_audio_normalized_frames(
            buffer->format, loop_start, &loop_start_frame) ||
        !live_audio_normalized_frames(
            buffer->format, loop_start + loop_length, &loop_end_frame))
        return 0u;
    g_live_audio_last_buffer = key;
    g_live_audio_last_data = buffer->data + play_start;
    g_live_audio_last_size = play_length;
    g_live_audio_last_sample_rate = buffer->format.sample_rate;
    live_write_u32(g_live_control, 236u, 3u);
    live_write_u32(g_live_control, 240u, key);
    live_write_u32(g_live_control, 244u, g_live_audio_last_data);
    live_write_u32(g_live_control, 248u, play_length);
    live_write_u32(g_live_control, 252u, buffer->format.sample_rate);
    if (!live_audio_decode(
            g_live_audio_last_data, play_length, buffer->format,
            &samples, &sample_count)) {
        ++g_live_audio_decode_failure_count;
        live_write_u32(g_live_control, 236u, 20u);
        return 0u;
    }
    ++g_live_audio_decoded_buffer_count;
    live_audio_stop_buffer(key);
    live_audio_lock();
    playback = live_audio_reserve_playback();
    if (!playback) {
        live_audio_unlock();
        VirtualFree(samples, 0u, 0x8000u);
        ++g_live_audio_dropped_buffer_count;
        return 0u;
    }
    playback->key = key;
    g_live_audio_playback_cache[(key >> 4u) & 63u] = playback;
    playback->active = 1u;
    playback->loop = flags & 1u;
    playback->samples = samples;
    playback->sample_count = sample_count;
    playback->cursor_frame_q32 = (U64)current_frame << 32u;
    playback->step_q32 = live_audio_step(
        buffer->frequency, buffer->format.sample_rate);
    playback->loop_start_frame = loop_start_frame;
    playback->loop_end_frame = loop_end_frame;
    if (!playback->loop || playback->loop_end_frame > sample_count / 2u) {
        playback->loop_start_frame = 0u;
        playback->loop_end_frame = sample_count / 2u;
    }
    live_audio_refresh_playback_gain(playback, buffer);
    buffer->playing = 1u;
    live_audio_unlock();
    live_write_u32(g_live_control, 236u, 6u);
    live_start_audio_lane();
    return 0u;
}
static U32 live_audio_effect_image(
    U32 this_pointer, U32 image, U32 image_size, U32 workspace_output
) {
    const U32 workspace_address = 0x31FF0000u;
    const U32 invalid_call = 0x88780032u;
    U32 hardware_owner, hardware_root, hardware_image, descriptor;
    U32 table_source, table_size, count, index;
    U32 image_table_a, image_table_b;
    U32 relocation0, relocation8, relocation10, relocation18;
    U64 table_offset;
    if (!live_readable_range(this_pointer, 0x24u) ||
        (image_size != 0x54FCu && image_size != 0x5800u) ||
        !live_readable_range(image, image_size) ||
        (workspace_output != 0u &&
         !live_readable_range(workspace_output, 4u)))
        goto invalid;
    image_table_a = *(U32*)(image + 0x804u);
    image_table_b = *(U32*)(image + 0x80Cu);
    if (*(U32*)(image + 0x800u) != 0u || image_table_a != 0x9EFu ||
        *(U32*)(image + 0x808u) != 0x2FD4u || image_table_b != 0x916u ||
        *(U32*)(image + 0x810u) != 3u)
        goto invalid;
    hardware_owner = *(U32*)(this_pointer + 8u);
    descriptor = *(U32*)(this_pointer + 0xCu);
    if (!live_readable_range(hardware_owner, 0x18u) ||
        !live_readable_range(descriptor, 8u) ||
        image_size > *(U32*)(this_pointer + 4u))
        goto invalid;
    hardware_root = *(U32*)(hardware_owner + 0x10u);
    if (!live_readable_range(hardware_root, 4u)) goto invalid;
    hardware_image = *(U32*)hardware_root;
    if (!live_readable_range(hardware_image, *(U32*)(this_pointer + 4u)))
        goto invalid;
    for (index = 0u; index < 6u; ++index)
        *(U32*)(hardware_image + 0x800u + index * 4u) = 0u;
    *(U32*)(hardware_image + 0x800u) = *(U32*)(image + 0x800u);
    *(U32*)(hardware_image + 0x804u) = image_table_a;
    *(U32*)(hardware_image + 0x808u) = *(U32*)(image + 0x808u);
    *(U32*)(hardware_image + 0x80Cu) = image_table_b;
    *(U32*)(hardware_image + 0x814u) = *(U32*)(image + 0x814u);
    copy_bytes(
        (void*)(hardware_image + 0x818u), (void*)(image + 0x818u),
        image_size - 0x818u);
    table_offset = 0x818ull + ((U64)image_table_a + image_table_b) * 4ull;
    if (table_offset != 0x542Cull || table_offset + 8ull > image_size)
        goto invalid;
    table_source = hardware_image + (U32)table_offset;
    count = *(U32*)table_source;
    if (count != 5u) goto invalid;
    table_size = 8u + count * 0x20u;
    if (table_offset + table_size > image_size) goto invalid;
    if (!g_live_audio_effect_workspace) {
        void* workspace = VirtualAlloc(
            (void*)workspace_address, 0x10000u, 0x3000u, 0x04u);
        if (workspace != (void*)workspace_address) {
            ++g_live_audio_effect_image_failure_count;
            return 0x8007000Eu;
        }
        g_live_audio_effect_workspace = workspace_address;
    }
    copy_bytes((void*)workspace_address, (void*)table_source, table_size);
    relocation0 = *(U32*)(descriptor + 4u) - 0x017C6818u;
    relocation8 = (0xFFA0BE7Au - image_table_a) * 4u;
    relocation10 = 0xFE836000u;
    relocation18 = *(U32*)(hardware_owner + 0x14u) - 0x0000C000u;
    for (index = 0u; index < count; ++index) {
        U32 entry = workspace_address + 8u + index * 0x20u;
        U32 source0 = *(U32*)entry;
        U32 size0 = *(U32*)(entry + 4u);
        U32 source8 = *(U32*)(entry + 8u);
        U32 size8 = *(U32*)(entry + 0xCu);
        U32 target0 = source0 + relocation0;
        U32 target8 = source8 + relocation8;
        if ((U64)source0 + size0 > image_size ||
            (U64)source8 + size8 > image_size ||
            target0 < 0xFE830000u || target0 + size0 < target0 ||
            target0 + size0 > 0xFE840000u ||
            target8 < 0xFE830000u || target8 + size8 < target8 ||
            target8 + size8 > 0xFE840000u)
            goto invalid;
        copy_bytes((void*)target0, (void*)(hardware_image + source0), size0);
        copy_bytes((void*)target8, (void*)(hardware_image + source8), size8);
        *(U32*)entry = target0;
        *(U32*)(entry + 8u) = target8;
        *(U32*)(entry + 0x10u) += relocation10;
        *(U32*)(entry + 0x18u) += relocation18;
    }
    *(U32*)(hardware_image + 0x810u) = 0u;
    *(U32*)(this_pointer + 0x20u) = workspace_address;
    if (workspace_output) *(U32*)workspace_output = workspace_address;
    ++g_live_audio_effect_image_count;
    return 0u;
invalid:
    ++g_live_audio_effect_image_failure_count;
    return invalid_call;
}
static U32 live_dispatch_audio_service(
    const ServiceDescriptor* service, U32 this_pointer, U32* arguments
) {
    U32 value = service->runtime_value;
    LiveAudioBuffer* buffer;
    LiveAudioStream* stream;
    U32 index;
    ++g_live_audio_service_count;
    if (value < 31u) ++g_live_audio_service_value_counts[value];
    if (value == 19u)
        return live_audio_set_music_mode(this_pointer, arguments[0]);
    if (value == 1u)
        return live_audio_effect_image(
            this_pointer, arguments[0], arguments[1], arguments[2]);
    if (value == 11u) {
        U32 descriptor = arguments[1], output = arguments[2];
        U32 base = workload_allocate(0x200u, 16u, &g_workload_next_pool);
        U32 key = base ? base + 0x1Cu : 0u;
        if (!key || !output) return 0x80004005u;
        for (index = 0u; index < 0x200u; ++index) *(U8*)(base + index) = 0u;
        buffer = live_audio_buffer(key, 1);
        if (!buffer) return 0x80004005u;
        *(U32*)base = 0x002C9474u;
        *(U32*)(base + 4u) = 1u;
        buffer->descriptor = base + 0x40u;
        buffer->voice = base + 0x160u;
        *(U32*)key = buffer->descriptor;
        *(U32*)(key + 4u) = buffer->voice;
        if (descriptor) {
            buffer->data = *(U32*)(descriptor + 8u);
            buffer->size = *(U32*)(descriptor + 0xCu);
            buffer->format = live_audio_format(*(U32*)(descriptor + 0x10u));
        }
        *(U32*)(buffer->descriptor + 0xC4u) = buffer->size;
        *(U32*)(buffer->descriptor + 0xCCu) = buffer->size;
        *(U32*)output = key;
        return 0u;
    }
    if (value == 7u) {
        U32 descriptor = arguments[1], output = arguments[2];
        U32 base = workload_allocate(0x80u, 16u, &g_workload_next_pool);
        U32 key = base;
        if (!key || !output) return 0x80004005u;
        for (index = 0u; index < 0x80u; ++index) *(U8*)(base + index) = 0u;
        stream = live_audio_stream(key, 1);
        if (!stream) return 0x80004005u;
        *(U32*)base = 0x002C948Cu;
        *(U32*)(base + 4u) = 0x002C9480u;
        *(U32*)(base + 8u) = 1u;
        if (descriptor) stream->format = live_audio_format(*(U32*)(descriptor + 8u));
        *(U32*)output = key;
        return 0u;
    }
    if (value == 29u) {
        U32 key = arguments[0];
        live_audio_stop_buffer(key);
        buffer = live_audio_buffer(key, 0);
        if (buffer)
            for (index = 0u; index < sizeof(*buffer); ++index)
                ((U8*)buffer)[index] = 0u;
        return 0u;
    }
    if (value == 30u) {
        U32 key = arguments[0];
        stream = live_audio_stream(key, 0);
        live_audio_lock();
        for (index = 0u; index < 64u; ++index) {
            if (g_live_audio_packets[index].active &&
                g_live_audio_packets[index].stream == key)
                live_audio_complete_packet(
                    &g_live_audio_packets[index], 0x8000000Bu);
        }
        if (stream) stream->packet_head = stream->packet_tail = 0u;
        if (stream)
            __atomic_fetch_and(
                &g_live_audio_active_stream_mask,
                ~(1u << (U32)(stream - g_live_audio_streams)), 5);
        live_audio_unlock();
        if (stream)
            for (index = 0u; index < sizeof(*stream); ++index)
                ((U8*)stream)[index] = 0u;
        return 0u;
    }
    if (value == 2u) {
        buffer = live_audio_buffer(arguments[0], 1);
        if (buffer) {
            buffer->data = arguments[1];
            buffer->size = arguments[2];
            if (buffer->descriptor) {
                *(U32*)(buffer->descriptor + 0xC0u) = 0u;
                *(U32*)(buffer->descriptor + 0xC4u) = buffer->size;
                *(U32*)(buffer->descriptor + 0xC8u) = 0u;
                *(U32*)(buffer->descriptor + 0xCCu) = buffer->size;
            }
        }
        return 0u;
    }
    if (value == 3u) {
        buffer = live_audio_buffer(arguments[0], 1);
        if (buffer) buffer->format = live_audio_format(arguments[1]);
        return 0u;
    }
    if (value == 13u) {
        buffer = live_audio_buffer(arguments[0], 1);
        if (buffer) {
            LiveAudioPlayback* playback;
            buffer->volume = (long)arguments[1];
            live_audio_lock();
            playback = live_audio_playback(arguments[0]);
            if (playback) live_audio_refresh_playback_gain(playback, buffer);
            live_audio_unlock();
        }
        return 0u;
    }
    if (value == 14u) {
        buffer = live_audio_buffer(arguments[0], 1);
        if (buffer) {
            LiveAudioPlayback* playback;
            buffer->frequency = arguments[1];
            live_audio_lock();
            playback = live_audio_playback(arguments[0]);
            if (playback) playback->step_q32 = live_audio_step(
                buffer->frequency, buffer->format.sample_rate);
            live_audio_unlock();
        }
        return 0u;
    }
    /*
     * Native-created buffers deliberately do not expose the private DSOUND
     * object graph.  Keep every reached public configuration call at the
     * host ABI boundary: routing and 3D parameters are accepted by the
     * current stereo mixer, while play/loop regions update the descriptor
     * state consumed by live_audio_play().
     */
    if (value == 21u) {
        U32 mixbins = arguments[1], count, pairs;
        LiveAudioPlayback* playback;
        buffer = live_audio_buffer(arguments[0], 0);
        if (!buffer || !mixbins || !live_readable_range(mixbins, 8u))
            return 0x80004005u;
        count = *(U32*)mixbins;
        pairs = *(U32*)(mixbins + 4u);
        if (count > 32u ||
            (count && (!pairs || !live_readable_range(pairs, count * 8u))))
            return 0x88780032u;
        for (index = 0u; index < count; ++index) {
            U32 mixbin = *(U32*)(pairs + index * 8u);
            long volume = *(long*)(pairs + index * 8u + 4u);
            if (mixbin == 0u) buffer->left_mixbin_volume = volume;
            else if (mixbin == 1u) buffer->right_mixbin_volume = volume;
        }
        live_audio_lock();
        playback = live_audio_playback(arguments[0]);
        if (playback) live_audio_refresh_playback_gain(playback, buffer);
        live_audio_unlock();
        return 0u;
    }
    if (value == 20u || value == 23u || value == 24u ||
        value == 25u || value == 26u || value == 27u) {
        buffer = live_audio_buffer(arguments[0], 0);
        return buffer ? 0u : 0x80004005u;
    }
    if (value == 28u) {
        U32 start = arguments[1], length = arguments[2];
        buffer = live_audio_buffer(arguments[0], 0);
        if (!buffer || !buffer->descriptor || start > buffer->size)
            return 0x88780032u;
        if (!length) length = buffer->size - start;
        if ((U64)start + length > buffer->size) return 0x88780032u;
        *(U32*)(buffer->descriptor + 0xC0u) = start;
        *(U32*)(buffer->descriptor + 0xC4u) = length;
        return 0u;
    }
    if (value == 22u) {
        U32 start = arguments[1], length = arguments[2], play_length;
        buffer = live_audio_buffer(arguments[0], 0);
        if (!buffer || !buffer->descriptor) return 0x88780032u;
        play_length = *(U32*)(buffer->descriptor + 0xC4u);
        if (start > play_length) return 0x88780032u;
        if (!length) length = play_length - start;
        if ((U64)start + length > play_length) return 0x88780032u;
        *(U32*)(buffer->descriptor + 0xC8u) = start;
        *(U32*)(buffer->descriptor + 0xCCu) = length;
        return 0u;
    }
    if (value == 4u) return live_audio_play(arguments[0], arguments[3]);
    if (value == 5u || value == 6u) {
        live_audio_stop_buffer(arguments[0]);
        return 0u;
    }
    if (value == 17u) {
        buffer = live_audio_buffer(arguments[0], 0);
        if (!buffer || !arguments[1]) return 0x80004005u;
        *(U32*)arguments[1] = buffer->playing ? 1u : 0u;
        return 0u;
    }
    if (value == 15u) {
        LiveAudioPlayback* playback;
        buffer = live_audio_buffer(arguments[0], 0);
        if (!buffer) return 0x80004005u;
        live_audio_lock();
        playback = live_audio_playback(arguments[0]);
        if (playback) {
            U32 position;
            if (live_audio_frame_byte_position(
                    buffer->format,
                    (U32)(playback->cursor_frame_q32 >> 32u), &position))
                buffer->current_position = position;
        }
        live_audio_unlock();
        if (arguments[1]) *(U32*)arguments[1] = buffer->current_position;
        if (arguments[2]) *(U32*)arguments[2] = buffer->current_position;
        return 0u;
    }
    if (value == 16u) {
        LiveAudioPlayback* playback;
        U32 frame;
        buffer = live_audio_buffer(arguments[0], 0);
        if (!buffer || arguments[1] >= buffer->size) return 0x80004005u;
        buffer->current_position = arguments[1];
        live_audio_lock();
        playback = live_audio_playback(arguments[0]);
        if (playback && live_audio_normalized_frames(
                buffer->format, arguments[1], &frame))
            playback->cursor_frame_q32 = (U64)frame << 32u;
        live_audio_unlock();
        return 0u;
    }
    if (value == 10u) {
        stream = live_audio_stream(arguments[0], 1);
        if (stream) stream->format = live_audio_format(arguments[1]);
        return 0u;
    }
    if (value == 8u) {
        U32 packet = arguments[1], data, size, completed, status, event;
        short* samples = 0;
        U32 sample_count = 0u;
        LiveAudioPacket* slot = 0;
        stream = live_audio_stream(arguments[0], 0);
        if (!stream || !packet) return 0u;
        data = *(U32*)packet;
        size = *(U32*)(packet + 4u);
        completed = *(U32*)(packet + 8u);
        status = *(U32*)(packet + 12u);
        event = *(U32*)(packet + 16u);
        if (completed) *(U32*)completed = 0u;
        if (status) *(U32*)status = 0x8000000Au;
        if (!live_audio_decode(data, size, stream->format, &samples, &sample_count)) {
            ++g_live_audio_decode_failure_count;
            if (status) *(U32*)status = 0x80004005u;
            if (event) SetEvent((HANDLE)event);
            return 0u;
        }
        ++g_live_audio_decoded_packet_count;
        live_audio_lock();
        for (index = 0u; index < 64u; ++index) {
            U32 candidate = (g_live_audio_packet_free_hint + index) & 63u;
            if (!g_live_audio_packets[candidate].active) {
                slot = &g_live_audio_packets[candidate];
                g_live_audio_packet_free_hint = (candidate + 1u) & 63u;
                break;
            }
        }
        if (slot) {
            U32 packet_index = (U32)(slot - g_live_audio_packets) + 1u;
            slot->stream = arguments[0];
            slot->active = 1u;
            slot->sample_count = sample_count;
            slot->cursor = 0u;
            slot->byte_count = size;
            slot->completed_size = completed;
            slot->status = status;
            slot->completion_event = event;
            slot->next = 0u;
            slot->sequence = ++g_live_audio_packet_sequence;
            slot->samples = samples;
            if (stream->packet_tail)
                g_live_audio_packets[stream->packet_tail - 1u].next = packet_index;
            else
                stream->packet_head = packet_index;
            stream->packet_tail = packet_index;
        }
        live_audio_unlock();
        if (!slot) {
            VirtualFree(samples, 0u, 0x8000u);
            ++g_live_audio_dropped_buffer_count;
            if (status) *(U32*)status = 0x80004005u;
            if (event) SetEvent((HANDLE)event);
        }
        live_start_audio_lane();
        return 0u;
    }
    if (value == 9u) {
        stream = live_audio_stream(arguments[0], 0);
        live_audio_lock();
        for (index = 0u; index < 64u; ++index) {
            if (g_live_audio_packets[index].active &&
                g_live_audio_packets[index].stream == arguments[0])
                live_audio_complete_packet(
                    &g_live_audio_packets[index], 0x8000000Bu);
        }
        if (stream) stream->packet_head = stream->packet_tail = 0u;
        live_audio_unlock();
        return 0u;
    }
    return 0u;
}
"""


def _phase7_normal_live_c_source(
    source: str,
    *,
    page_count: int,
    boot_image_size: int,
    stop_instruction: X86Instruction,
    host_data_pages: Sequence[tuple[int, bytes]] = (),
) -> str:
    """Turn the Phase-7 resident proof worker into its standalone live mode.

    The proof protocol stays byte-for-byte available when the executable is
    launched with a positional exchange name.  ``--normal-live`` instead loads
    the content-identified XBE-entry exchange once, retains all guest memory in
    the IA-32 process, and publishes completed flip spans directly to the
    ordinary presenter transports.
    """

    boot_return_thunk = _stop_patch(NORMAL_LIVE_BOOT_RETURN_THUNK_ADDRESS)
    resume_thunk = _normal_live_resume_thunk(stop_instruction)
    vblank_start_thunk = _build_start_thunk(scratch=NORMAL_LIVE_VBLANK_SCRATCH_ADDRESS)
    vblank_exit_thunk = _build_exit_thunk(scratch=NORMAL_LIVE_VBLANK_SCRATCH_ADDRESS)
    vblank_return_patch = _jump_patch(
        IA32_VBLANK_RETURN_SENTINEL,
        NORMAL_LIVE_VBLANK_EXIT_THUNK_ADDRESS,
    )
    declaration_seam = """__declspec(dllimport) char* __stdcall GetCommandLineA(void);
__declspec(dllimport) HANDLE __stdcall OpenFileMappingA(U32, BOOL, const char*);
"""
    declaration_block = """__declspec(dllimport) char* __stdcall GetCommandLineA(void);
__declspec(dllimport) U32 __stdcall GetModuleFileNameA(HANDLE, char*, U32);
__declspec(dllimport) HANDLE __stdcall CreateFileA(
    const char*, U32, U32, void*, U32, U32, HANDLE);
__declspec(dllimport) BOOL __stdcall ReadFile(HANDLE, void*, U32, U32*, void*);
__declspec(dllimport) BOOL __stdcall WriteFile(HANDLE, const void*, U32, U32*, void*);
__declspec(dllimport) U32 __stdcall GetFileAttributesA(const char*);
__declspec(dllimport) BOOL __stdcall CreateDirectoryA(const char*, void*);
__declspec(dllimport) U32 __stdcall GetFileSize(HANDLE, U32*);
__declspec(dllimport) U32 __stdcall SetFilePointer(HANDLE, long, long*, U32);
__declspec(dllimport) BOOL __stdcall SetEndOfFile(HANDLE);
__declspec(dllimport) BOOL __stdcall DeleteFileA(const char*);
__declspec(dllimport) U32 __stdcall GetLastError(void);
__declspec(dllimport) HANDLE __stdcall FindFirstFileA(const char*, void*);
__declspec(dllimport) BOOL __stdcall FindNextFileA(HANDLE, void*);
__declspec(dllimport) BOOL __stdcall FindClose(HANDLE);
__declspec(dllimport) BOOL __stdcall SystemTimeToFileTime(const void*, void*);
__declspec(dllimport) BOOL __stdcall FileTimeToSystemTime(const void*, void*);
__declspec(dllimport) void __stdcall Sleep(U32);
__declspec(dllimport) HANDLE __stdcall CreateEventA(void*, BOOL, BOOL, const char*);
__declspec(dllimport) HANDLE __stdcall CreateThread(
    void*, U32, U32 (__stdcall*)(void*), void*, U32, U32*);
__declspec(dllimport) BOOL __stdcall SetEvent(HANDLE);
__declspec(dllimport) BOOL __stdcall VirtualFree(void*, U32, U32);
__declspec(dllimport) HANDLE __stdcall OpenFileMappingA(U32, BOOL, const char*);
"""
    if declaration_seam not in source:
        raise Ia32BackendError("normal-live worker lost its Win32 declaration seam")
    source = source.replace(declaration_seam, declaration_block, 1)

    global_seam = """static HANDLE g_broker_request_event;
static HANDLE g_broker_response_event;
"""
    global_block = (
        global_seam
        + """static BOOL g_normal_live;
static U8* g_live_control;
static U8* g_live_commands;
static U8* g_live_resources;
static U64 g_live_command_cursor;
static U64 g_live_command_write_count;
static U32 g_live_command_offset;
static U32 g_live_push_ring_start;
static U32 g_live_push_ring_end;
static U32 g_live_push_ring_cursor;
static U64 g_live_resource_generation;
static U64 g_live_flip_count;
static HANDLE g_live_presentation_event;
static HANDLE g_live_publication_event;
static HANDLE g_live_control_thread;
static volatile U32 g_live_summary_claimed;
static volatile U32 g_live_vblank_lane_ready;
static volatile U32 g_live_vblank_sequence;
static volatile U32 g_live_vblank_schedule_count;
static volatile U32 g_live_vblank_run_count;
static volatile U32 g_live_vblank_completion_count;
static volatile U32 g_live_vblank_failure_count;
static volatile U32 g_live_vblank_callback_address;
static volatile U32 g_live_vblank_failure_code;
static U32 g_live_render_publication_failure_code;
static U64 g_live_vblank_next_deadline_qpc;
static char g_live_presentation_event_name[160];
static char g_live_publication_event_name[160];
static char g_live_summary_path[1024];
static char g_live_run_id[96];
static char g_live_extracted_root[1024];
static char g_live_save_root[1024];
static char g_live_dashboard_root[1024];
static char g_live_cache_root[1024];
typedef struct LiveWorker {
    U32 handle, start_address, start_context1, start_context2, suspended;
    U32 kernel_stack_size, tls_data_size;
} LiveWorker;
typedef struct LiveFileTime { U32 low, high; } LiveFileTime;
typedef struct LiveSystemTime {
    U16 year, month, day_of_week, day, hour, minute, second, milliseconds;
} LiveSystemTime;
typedef struct LiveFindData {
    U32 attributes;
    LiveFileTime creation, access, write;
    U32 size_high, size_low, reserved0, reserved1;
    char name[260], alternate_name[14];
} LiveFindData;
typedef struct LiveFile {
    U32 handle, flags, active, delete_pending, directory_index;
    U64 size, position;
    HANDLE host;
    char guest_path[512], host_path[1024], directory_pattern[260];
} LiveFile;
typedef struct LiveSha1Context {
    U32 state[5], count[2];
    U8 buffer[64];
} LiveSha1Context;
static LiveWorker g_live_workers[64];
static U32 g_live_worker_count;
static U32 g_live_next_worker_handle = 0xB2402000u;
static U32 g_live_bootstrap_phase = 1u;
static U32 g_live_startup_phase;
static void live_write_fault_summary(ExceptionRecord*, void*);
static void live_write_summary(void);
static LiveFile g_live_files[128];
static U32 g_live_file_count;
static LiveFile* g_live_file_cache[256];
static LiveFindData g_live_find_entries[128];
static U32 live_dispatch_bootstrap_service(
    const ServiceDescriptor*, U32*, U32);
static U32 live_dispatch_input_service(const ServiceDescriptor*, U32*);
static U32 live_dispatch_audio_service(const ServiceDescriptor*, U32, U32*);
"""
    )
    if global_seam not in source:
        raise Ia32BackendError("normal-live worker lost its native-service global seam")
    source = source.replace(global_seam, global_block, 1)

    main_seam = "void __cdecl mainCRTStartup(void) {\n"
    host_data_definitions = []
    host_data_records = []
    for index, (address, payload) in enumerate(host_data_pages):
        if address & (PAGE_SIZE - 1) or len(payload) != PAGE_SIZE:
            raise Ia32BackendError("normal-live host data must contain complete aligned pages")
        host_data_definitions.append(
            f"static const U8 kNormalLiveHostData{index}[] = {{{_bytes_initializer(payload)}}};"
        )
        host_data_records.append(f"{{0x{address:08X}u,kNormalLiveHostData{index}}}")
    host_data_records.append("{0u,0}")
    host_data_block = "\n".join(host_data_definitions)
    host_data_table = ",".join(host_data_records)
    helpers = f"""static const U8 kNormalLiveBootReturnThunk[] = {{{_bytes_initializer(boot_return_thunk)}}};
static const U8 kNormalLiveResumeThunk[] = {{{_bytes_initializer(resume_thunk)}}};
static const U8 kNormalLiveVblankStartThunk[] = {{{_bytes_initializer(vblank_start_thunk)}}};
static const U8 kNormalLiveVblankExitThunk[] = {{{_bytes_initializer(vblank_exit_thunk)}}};
static const U8 kNormalLiveVblankReturnPatch[] = {{{_bytes_initializer(vblank_return_patch)}}};
typedef struct LiveHostDataPage {{ U32 address; const U8* payload; }} LiveHostDataPage;
{host_data_block}
static const LiveHostDataPage kNormalLiveHostDataPages[] = {{{host_data_table}}};
static const U32 kNormalLiveHostDataPageCount = {len(host_data_pages)}u;
static U32 live_read_u32(const U8* value, U32 offset) {{
    U32 result;
    copy_bytes(&result, value + offset, 4u);
    return result;
}}
static U64 live_read_u64(const U8* value, U32 offset) {{
    U64 result;
    copy_bytes(&result, value + offset, 8u);
    return result;
}}
static void live_write_u16(U8* value, U32 offset, U16 input) {{
    copy_bytes(value + offset, &input, 2u);
}}
static void live_write_u32(U8* value, U32 offset, U32 input) {{
    copy_bytes(value + offset, &input, 4u);
}}
static void live_write_u64(U8* value, U32 offset, U64 input) {{
    copy_bytes(value + offset, &input, 8u);
}}
static void live_barrier(void) {{
    __atomic_thread_fence(5);
}}
{_normal_live_input_audio_c_source()}
static U32 __stdcall live_vblank_lane(void* ignored) {{
    U64 frequency = 0u;
    U64 interval;
    (void)ignored;
    if (!QueryPerformanceFrequency(&frequency) || !frequency) {{
        g_live_vblank_failure_code = 1u;
        ++g_live_vblank_failure_count;
        live_write_summary();
        ExitProcess(72u);
    }}
    interval = (frequency + 30u) / 60u;
    if (!interval) interval = 1u;
    for (;;) {{
        U32 context;
        U32 callback;
        U64 now = 0u;
        U32 index;
        if (live_read_u32(g_live_control, 32u) != 0u) return 0u;
        context = *(volatile U32*)0x{NORMAL_LIVE_VBLANK_CONTEXT_GLOBAL:08X}u;
        if (!context) {{
            g_live_vblank_next_deadline_qpc = 0u;
            Sleep(1u);
            continue;
        }}
        callback = *(volatile U32*)(
            context + 0x{NORMAL_LIVE_VBLANK_CALLBACK_OFFSET:X}u);
        if (!callback) {{
            g_live_vblank_next_deadline_qpc = 0u;
            Sleep(1u);
            continue;
        }}
        g_live_vblank_callback_address = callback;
        if (callback != 0x{NORMAL_LIVE_VBLANK_CALLBACK_ADDRESS:08X}u) {{
            g_live_vblank_failure_code = 2u;
            ++g_live_vblank_failure_count;
            live_write_summary();
            ExitProcess(72u);
        }}
        if (!QueryPerformanceCounter(&now)) {{
            g_live_vblank_failure_code = 3u;
            ++g_live_vblank_failure_count;
            live_write_summary();
            ExitProcess(72u);
        }}
        if (!g_live_vblank_next_deadline_qpc) {{
            g_live_vblank_next_deadline_qpc = now + interval;
            Sleep(1u);
            continue;
        }}
        if (now < g_live_vblank_next_deadline_qpc) {{
            Sleep(1u);
            continue;
        }}
        g_live_vblank_next_deadline_qpc = now + interval;
        ++g_live_vblank_sequence;
        ++g_live_vblank_schedule_count;
        *(volatile U32*)0x{NORMAL_LIVE_VBLANK_DATA_ADDRESS:08X}u =
            g_live_vblank_sequence;
        *(volatile U32*)0x{NORMAL_LIVE_VBLANK_DATA_ADDRESS + 4:08X}u =
            (U32)g_live_flip_count;
        *(volatile U32*)0x{NORMAL_LIVE_VBLANK_DATA_ADDRESS + 8:08X}u = 0u;
        *(volatile U32*)0x{NORMAL_LIVE_VBLANK_STACK_TOP:08X}u =
            0x{IA32_VBLANK_RETURN_SENTINEL:08X}u;
        *(volatile U32*)0x{NORMAL_LIVE_VBLANK_STACK_TOP + 4:08X}u =
            0x{NORMAL_LIVE_VBLANK_DATA_ADDRESS:08X}u;
        for (index = 0u; index < 8u; ++index)
            *(U32*)(0x{NORMAL_LIVE_VBLANK_SCRATCH_ADDRESS:08X}u + index * 4u) = 0u;
        *(U32*)0x{NORMAL_LIVE_VBLANK_SCRATCH_ADDRESS + SCRATCH_REGISTER_OFFSETS["esp"]:08X}u =
            0x{NORMAL_LIVE_VBLANK_STACK_TOP:08X}u;
        *(U32*)0x{NORMAL_LIVE_VBLANK_SCRATCH_ADDRESS + SCRATCH_EFLAGS_OFFSET:08X}u =
            0x00000202u;
        *(U32*)0x{NORMAL_LIVE_VBLANK_SCRATCH_ADDRESS + SCRATCH_ENTRY_EIP_OFFSET:08X}u =
            callback;
        copy_bytes(
            (void*)0x{NORMAL_LIVE_VBLANK_SCRATCH_ADDRESS + SCRATCH_FXSAVE_OFFSET:08X}u,
            (void*)0x{SCRATCH_ADDRESS + SCRATCH_FXSAVE_OFFSET:08X}u, 512u);
        live_barrier();
        ++g_live_vblank_run_count;
        ((void (__cdecl *)(void))0x{NORMAL_LIVE_VBLANK_START_THUNK_ADDRESS:08X}u)();
        ++g_live_vblank_completion_count;
    }}
}}
static BOOL live_start_vblank_lane(void) {{
    const U32 MEM_COMMIT_RESERVE = 0x3000u;
    const U32 PAGE_READWRITE = 0x04u;
    const U32 stack_region = 0x{NORMAL_LIVE_VBLANK_STACK_TOP & ~0xFFFF:08X}u;
    const U32 data_region = 0x{NORMAL_LIVE_VBLANK_DATA_ADDRESS & ~0xFFFF:08X}u;
    if (g_live_vblank_lane_ready) return 1;
    if (VirtualAlloc((void*)stack_region, 0x10000u,
                     MEM_COMMIT_RESERVE, PAGE_READWRITE) !=
        (void*)stack_region) {{
        g_live_vblank_failure_code = 10u;
        ++g_live_vblank_failure_count;
        return 0;
    }}
    if (VirtualAlloc((void*)data_region, 0x10000u,
                     MEM_COMMIT_RESERVE, PAGE_READWRITE) !=
        (void*)data_region) {{
        g_live_vblank_failure_code = 11u;
        ++g_live_vblank_failure_count;
        return 0;
    }}
    if (VirtualAlloc((void*)0x{NORMAL_LIVE_VBLANK_TLS_BASE:08X}u, 0x10000u,
                     MEM_COMMIT_RESERVE, PAGE_READWRITE) !=
        (void*)0x{NORMAL_LIVE_VBLANK_TLS_BASE:08X}u) {{
        g_live_vblank_failure_code = 12u;
        ++g_live_vblank_failure_count;
        return 0;
    }}
    copy_bytes((void*)0x{NORMAL_LIVE_VBLANK_TLS_BASE:08X}u,
               (void*)0x{NORMAL_LIVE_BOOT_TLS_BASE:08X}u, 4096u);
    *(U32*)0x{NORMAL_LIVE_VBLANK_TLS_BASE + 0x20:08X}u =
        0x{NORMAL_LIVE_VBLANK_TLS_BASE:08X}u;
    live_barrier();
    g_live_vblank_lane_ready = 1u;
    return 1;
}}
static U32 __stdcall live_control_watcher(void* ignored) {{
    (void)ignored;
    while (live_read_u32(g_live_control, 32u) == 0u) {{
        if (g_live_vblank_lane_ready) {{
            live_vblank_lane(0);
            break;
        }}
        Sleep(1u);
    }}
    live_write_summary();
    ExitProcess(0);
    return 0u;
}}
static U32 live_string_size(const char* value) {{
    U32 size = 0u;
    while (value[size]) ++size;
    return size;
}}
static BOOL live_string_equal(const char* left, const char* right) {{
    U32 index = 0u;
    while (left[index] && right[index] && left[index] == right[index]) ++index;
    return left[index] == 0 && right[index] == 0;
}}
static BOOL live_join_name(
    const char* prefix, const char* suffix, char* output, U32 capacity
) {{
    U32 prefix_size = live_string_size(prefix);
    U32 suffix_size = live_string_size(suffix);
    if (prefix_size + suffix_size + 1u > capacity) return 0;
    copy_bytes(output, prefix, prefix_size);
    copy_bytes(output + prefix_size, suffix, suffix_size);
    output[prefix_size + suffix_size] = 0;
    return 1;
}}
static const char* live_next_token(const char* cursor, char* output, U32 capacity) {{
    U32 size = 0u;
    BOOL quoted = 0;
    while (*cursor == ' ' || *cursor == '\\t') ++cursor;
    if (*cursor == '\"') {{ quoted = 1; ++cursor; }}
    while (*cursor && ((quoted && *cursor != '\"') ||
           (!quoted && *cursor != ' ' && *cursor != '\\t'))) {{
        if (size + 1u < capacity) output[size++] = *cursor;
        ++cursor;
    }}
    if (quoted && *cursor == '\"') ++cursor;
    output[size] = 0;
    return cursor;
}}
static BOOL live_argument(const char* name, char* output, U32 capacity) {{
    const char* cursor = GetCommandLineA();
    char token[1024];
    cursor = live_next_token(cursor, token, sizeof(token));
    while (*cursor) {{
        cursor = live_next_token(cursor, token, sizeof(token));
        if (live_string_equal(token, name)) {{
            live_next_token(cursor, output, capacity);
            return output[0] != 0;
        }}
    }}
    return 0;
}}
static BOOL live_has_argument(const char* name) {{
    char value[1024];
    const char* cursor = GetCommandLineA();
    cursor = live_next_token(cursor, value, sizeof(value));
    while (*cursor) {{
        cursor = live_next_token(cursor, value, sizeof(value));
        if (live_string_equal(value, name)) return 1;
    }}
    return 0;
}}
static BOOL live_sibling_path(const char* name, char* output, U32 capacity) {{
    U32 size = GetModuleFileNameA(0, output, capacity);
    U32 slash = 0u;
    U32 index;
    if (size == 0u || size >= capacity) return 0;
    for (index = 0u; index < size; ++index)
        if (output[index] == '\\\\' || output[index] == '/') slash = index + 1u;
    for (index = 0u; name[index]; ++index) {{
        if (slash + index + 1u >= capacity) return 0;
        output[slash + index] = name[index];
    }}
    output[slash + index] = 0;
    return 1;
}}
static Exchange* live_load_boot_exchange(void) {{
    const U32 GENERIC_READ = 0x80000000u;
    const U32 FILE_SHARE_READ = 1u;
    const U32 OPEN_EXISTING = 3u;
    const U32 FILE_ATTRIBUTE_NORMAL = 0x80u;
    const U32 MEM_COMMIT_RESERVE = 0x3000u;
    const U32 PAGE_READWRITE = 0x04u;
    HANDLE file;
    Exchange* exchange = 0;
    U32 index;
    U32 offset = 0u;
    U32 read = 0u;
    char path[1024];
    if (!live_sibling_path("boot-image.bin", path, sizeof(path))) return 0;
    file = CreateFileA(path, GENERIC_READ, FILE_SHARE_READ, 0,
                       OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, 0);
    if (file == (HANDLE)-1) return 0;
    for (index = 0u;
         index < sizeof(kExchangeViewAddresses) / sizeof(kExchangeViewAddresses[0]);
         ++index) {{
        exchange = (Exchange*)VirtualAlloc(
            (void*)kExchangeViewAddresses[index], {boot_image_size}u,
            MEM_COMMIT_RESERVE, PAGE_READWRITE);
        if (exchange) break;
    }}
    if (!exchange) {{ CloseHandle(file); return 0; }}
    while (offset < {boot_image_size}u) {{
        U32 request = {boot_image_size}u - offset;
        if (request > 0x01000000u) request = 0x01000000u;
        if (!ReadFile(file, (U8*)exchange + offset, request, &read, 0) ||
            read != request) {{
            CloseHandle(file);
            return 0;
        }}
        offset += read;
    }}
    CloseHandle(file);
    return exchange;
}}
static BOOL live_open_transports(void) {{
    const U32 FILE_MAP_ALL_ACCESS = 0x000F001Fu;
    HANDLE mapping;
    char name[256];
    char derived[280];
    U32 size;
    if (!live_argument("--live-control-transport", name, sizeof(name))) return 0;
    mapping = OpenFileMappingA(FILE_MAP_ALL_ACCESS, 0, name);
    if (!mapping) return 0;
    g_live_control = (U8*)MapViewOfFileEx(
        mapping, FILE_MAP_ALL_ACCESS, 0, 0, 0, 0);
    size = live_string_size(name);
    if (size + 11u >= sizeof(derived)) return 0;
    copy_bytes(derived, name, size);
    copy_bytes(derived + size, "_commands", 10u);
    mapping = OpenFileMappingA(FILE_MAP_ALL_ACCESS, 0, derived);
    if (!mapping) return 0;
    g_live_commands = (U8*)MapViewOfFileEx(
        mapping, FILE_MAP_ALL_ACCESS, 0, 0, 0, 0);
    copy_bytes(derived + size, "_resources", 11u);
    mapping = OpenFileMappingA(FILE_MAP_ALL_ACCESS, 0, derived);
    if (!mapping) return 0;
    g_live_resources = (U8*)MapViewOfFileEx(
        mapping, FILE_MAP_ALL_ACCESS, 0, 0, 0, 0);
    if (!g_live_control || !g_live_commands || !g_live_resources ||
        live_read_u32(g_live_control, 8u) != 1u ||
        live_read_u32(g_live_control, 12u) != 65536u ||
        live_read_u32(g_live_commands, 8u) != 1u ||
        live_read_u32(g_live_commands, 12u) != 64u * 1024u * 1024u ||
        live_read_u32(g_live_resources, 8u) != 1u ||
        live_read_u32(g_live_resources, 12u) != 64u * 1024u * 1024u)
        return 0;
    g_live_command_cursor = live_read_u64(g_live_commands, 24u);
    if (g_live_command_cursor >= 64u * 1024u * 1024u - 64u) return 0;
    g_live_command_offset = (U32)g_live_command_cursor;
    live_argument("--summary-output", g_live_summary_path,
                  sizeof(g_live_summary_path));
    if (!live_argument("--run-id", g_live_run_id, sizeof(g_live_run_id)) ||
        !live_join_name("b2_recomp_presented_", g_live_run_id,
                        g_live_presentation_event_name,
                        sizeof(g_live_presentation_event_name)) ||
        !live_join_name("b2_recomp_published_", g_live_run_id,
                        g_live_publication_event_name,
                        sizeof(g_live_publication_event_name)))
        return 0;
    g_live_presentation_event = CreateEventA(
        0, 0, 0, g_live_presentation_event_name);
    g_live_publication_event = CreateEventA(
        0, 0, 0, g_live_publication_event_name);
    if (!g_live_presentation_event || !g_live_publication_event) return 0;
    if (!live_argument("--extracted-root", g_live_extracted_root,
                       sizeof(g_live_extracted_root)) ||
        !live_argument("--save-data-root", g_live_save_root,
                       sizeof(g_live_save_root)) ||
        !live_argument("--dashboard-root", g_live_dashboard_root,
                       sizeof(g_live_dashboard_root)) ||
        !live_argument("--cache-root", g_live_cache_root,
                       sizeof(g_live_cache_root)))
        return 0;
    return 1;
}}
static BOOL live_ring_write_two(
    const U8* first_payload, U32 first_size,
    const U8* second_payload, U32 second_size
) {{
    const U64 data_offset = 64u;
    const U64 capacity = 64u * 1024u * 1024u - data_offset;
    U64 read_cursor = live_read_u64(g_live_commands, 32u);
    U64 total = (U64)first_size + second_size;
    U64 cursor = g_live_command_cursor;
    U32 offset = g_live_command_offset;
    U32 part;
    if (read_cursor > cursor || total > capacity - (cursor - read_cursor)) {{
        g_live_render_publication_failure_code = 30u;
        return 0;
    }}
    part = (U32)capacity - offset;
    if (part > first_size) part = first_size;
    copy_bytes(g_live_commands + data_offset + offset, first_payload, part);
    if (part < first_size)
        copy_bytes(g_live_commands + data_offset, first_payload + part, first_size - part);
    cursor += first_size;
    offset += first_size;
    if (offset >= (U32)capacity) offset -= (U32)capacity;
    part = (U32)capacity - offset;
    if (part > second_size) part = second_size;
    copy_bytes(g_live_commands + data_offset + offset, second_payload, part);
    if (part < second_size)
        copy_bytes(g_live_commands + data_offset, second_payload + part, second_size - part);
    cursor += second_size;
    offset += second_size;
    if (offset >= (U32)capacity) offset -= (U32)capacity;
    g_live_command_cursor = cursor;
    g_live_command_offset = offset;
    live_barrier();
    live_write_u64(g_live_commands, 24u, cursor);
    return 1;
}}
static U32 live_manifest_append(
    U8* payload, U32 size, U16 field, U8 kind,
    const void* value, U32 value_size
) {{
    live_write_u16(payload, size, field);
    payload[size + 2u] = kind;
    payload[size + 3u] = 0u;
    live_write_u32(payload, size + 4u, value_size);
    copy_bytes(payload + size + 8u, value, value_size);
    return size + 8u + value_size;
}}
static U32 live_manifest_u64(U8* payload, U32 size, U16 field, U64 value) {{
    return live_manifest_append(payload, size, field, 1u, &value, 8u);
}}
static U32 live_manifest_bool(U8* payload, U32 size, U16 field, BOOL value) {{
    U8 encoded = value ? 1u : 0u;
    return live_manifest_append(payload, size, field, 2u, &encoded, 1u);
}}
static U32 live_manifest_string(
    U8* payload, U32 size, U16 field, const char* value
) {{
    return live_manifest_append(
        payload, size, field, 3u, value, live_string_size(value));
}}
{_normal_live_resource_publication_c_source()}
static BOOL live_publish_manifest(void) {{
    U8 payload[1024];
    U32 records = 0u;
    U32 size = 16u;
    U32 sequence;
    U32 publishing;
    U32 published;
    copy_bytes(payload, "B2MAN001", 8u);
    live_write_u32(payload, 8u, 1u);
    size = live_manifest_bool(payload, size, 1u, 0); ++records;
    size = live_manifest_string(payload, size, 2u,
                                "shared-memory-resource-slot"); ++records;
    size = live_manifest_string(payload, size, 3u,
                                "shared-memory-command-spans"); ++records;
    size = live_manifest_u64(payload, size, 10u,
                             g_live_command_write_count); ++records;
    size = live_manifest_u64(payload, size, 17u, g_live_flip_count); ++records;
    size = live_manifest_u64(payload, size, 18u, g_live_flip_count); ++records;
    size = live_manifest_u64(payload, size, 21u,
                             g_live_command_write_count); ++records;
    size = live_manifest_u64(payload, size, 22u,
                             g_live_command_write_count); ++records;
    size = live_manifest_u64(payload, size, 23u, 0u); ++records;
    size = live_manifest_string(payload, size, 24u,
                                "shared_memory_span_v1"); ++records;
    size = live_manifest_u64(payload, size, 25u,
                             g_live_command_cursor); ++records;
    size = live_manifest_u64(payload, size, 26u, 0u); ++records;
    size = live_manifest_u64(payload, size, 27u,
                             g_live_resource_generation); ++records;
    size = live_manifest_u64(payload, size, 28u, 1u); ++records;
    size = live_manifest_string(payload, size, 30u,
                                g_live_presentation_event_name); ++records;
    size = live_manifest_string(payload, size, 31u,
                                g_live_publication_event_name); ++records;
    size = live_manifest_u64(payload, size, 32u,
                             g_live_resource_generation & 1u); ++records;
    size = live_manifest_u64(payload, size, 33u,
                             g_live_resource_payload_size); ++records;
    size = live_manifest_string(payload, size, 34u,
                                "shared_memory_slot_v1"); ++records;
    live_write_u32(payload, 12u, records);
    sequence = live_read_u32(g_live_control, 256u);
    publishing = (sequence + 1u) | 1u;
    published = publishing + 1u;
    live_write_u32(g_live_control, 256u, publishing);
    live_write_u32(g_live_control, 260u, size);
    copy_bytes(g_live_control + 264u, payload, size);
    live_barrier();
    live_write_u32(g_live_control, 256u, published);
    if (!SetEvent(g_live_publication_event)) {{
        g_live_render_publication_failure_code = 50u;
        return 0;
    }}
    return 1;
}}
static BOOL live_wait_for_presentation(void) {{
    U32 waited = 0u;
    for (;;) {{
        U32 sequence;
        U32 confirmed;
        if (live_read_u32(g_live_control, 32u) != 0u) return 0;
        sequence = live_read_u32(g_live_control, 128u);
        if (sequence && !(sequence & 1u) &&
            live_read_u64(g_live_control, 136u) == 1u &&
            live_read_u64(g_live_control, 144u) >= g_live_flip_count) {{
            confirmed = live_read_u32(g_live_control, 128u);
            if (confirmed == sequence && !(confirmed & 1u)) return 1;
        }}
        if (++waited >= 120000u) {{
            g_live_render_publication_failure_code = 60u;
            return 0;
        }}
        Sleep(1u);
    }}
}}
static BOOL live_publish_command_span(
    U32 ring_start, U32 span_start, U32 span_end, BOOL completes_flip
) {{
    U32 payload_size;
    U32 write_count;
    U8 header[16];
    if (span_end <= span_start || (span_start & 3u) || (span_end & 3u)) {{
        g_live_render_publication_failure_code = 40u;
        return 0;
    }}
    payload_size = span_end - span_start;
    write_count = payload_size / 4u;
    for (U32 index = 0u; index < sizeof(header); ++index) header[index] = 0u;
    header[0] = 1u;
    header[1] = completes_flip ? 1u : 0u;
    live_write_u32(header, 4u, 0x80000000u + span_start - ring_start);
    live_write_u32(header, 8u, payload_size);
    live_write_u32(header, 12u, write_count);
    if (!live_scan_resource_span(
            0x80000000u + span_start - ring_start,
            (const U8*)span_start, payload_size, completes_flip)) return 0;
    if (!live_ring_write_two(
            header, sizeof(header), (const U8*)span_start, payload_size)) return 0;
    g_live_command_write_count += write_count;
    return 1;
}}
static BOOL live_publish_frame(void) {{
    U32 context = *(volatile U32*)0x002256B8u;
    U32 ring_start;
    U32 ring_end;
    U32 ring_cursor;
    U32 put_value;
    U32 get_pointer;
    BOOL initialized = 0;
    BOOL published_span = 0;
    g_live_render_publication_failure_code = 0u;
    if (!context) {{
        g_live_render_publication_failure_code = 70u;
        return 0;
    }}
    ring_start = *(volatile U32*)(context + 0x24u);
    ring_end = *(volatile U32*)(context + 0x28u);
    ring_cursor = *(volatile U32*)context;
    put_value = *(volatile U32*)(context + 0x2Cu);
    get_pointer = *(volatile U32*)(context + 0x30u);
    if (!ring_start || ring_end <= ring_start || ring_end - ring_start > 0x01000000u ||
        ring_cursor < ring_start || ring_cursor > ring_end ||
        (ring_start & 3u) || (ring_end & 3u) || (ring_cursor & 3u)) {{
        g_live_render_publication_failure_code = 71u;
        return 0;
    }}
    if (g_live_push_ring_start != ring_start || g_live_push_ring_end != ring_end ||
        g_live_push_ring_cursor < ring_start || g_live_push_ring_cursor > ring_end) {{
        g_live_push_ring_start = ring_start;
        g_live_push_ring_end = ring_end;
        g_live_push_ring_cursor = ring_start;
        initialized = 1;
    }}
    if (ring_cursor > g_live_push_ring_cursor) {{
        if (!live_publish_command_span(
                ring_start, g_live_push_ring_cursor, ring_cursor,
                !initialized)) return 0;
        published_span = 1;
    }} else if (ring_cursor < g_live_push_ring_cursor) {{
        if (g_live_push_ring_cursor < ring_end &&
            !live_publish_command_span(
                ring_start, g_live_push_ring_cursor, ring_end,
                ring_cursor == ring_start)) return 0;
        published_span = 1;
        if (ring_cursor > ring_start &&
            !live_publish_command_span(
                ring_start, ring_start, ring_cursor, 1)) return 0;
    }}
    g_live_push_ring_cursor = ring_cursor == ring_end ? ring_start : ring_cursor;
    if (published_span && !initialized) ++g_live_flip_count;
    if (!live_publish_resources() || !live_publish_manifest()) return 0;
    if (get_pointer) *(volatile U32*)get_pointer = put_value;
    if (initialized) return 1;
    return live_wait_for_presentation();
}}
static BOOL live_start_primary_worker(Exchange* exchange) {{
    const U32 MEM_COMMIT_RESERVE = 0x3000u;
    const U32 PAGE_READWRITE = 0x04u;
    const U32 stack_top = 0x{NORMAL_LIVE_PRIMARY_STACK_TOP:08X}u;
    const U32 tls_region = 0x{NORMAL_LIVE_PRIMARY_TLS_BASE:08X}u;
    LiveWorker* worker = 0;
    U32 index, stack_size, stack_region;
    for (index = 0u; index < g_live_worker_count; ++index) {{
        if (g_live_workers[index].start_address &&
            !g_live_workers[index].suspended) {{
            worker = &g_live_workers[index];
            break;
        }}
    }}
    if (!worker) return 0;
    stack_size = workload_align_up(
        worker->kernel_stack_size ? worker->kernel_stack_size : 0x00010000u,
        0x00010000u);
    if (stack_size > 0x01000000u || worker->tls_data_size > 0x0000D000u)
        return 0;
    stack_region = stack_top - stack_size;
    if (VirtualAlloc((void*)stack_region, stack_size + 4096u,
                     MEM_COMMIT_RESERVE, PAGE_READWRITE) !=
        (void*)stack_region) return 0;
    if (VirtualAlloc((void*)tls_region, 0x00010000u,
                     MEM_COMMIT_RESERVE, PAGE_READWRITE) !=
        (void*)tls_region) return 0;
    *(U32*)stack_top = 0xB2100000u;
    *(U32*)(stack_top + 4u) = worker->start_context1;
    *(U32*)(stack_top + 8u) = worker->start_context2;
    *(U32*)0x{NORMAL_LIVE_TLS_INDEX_ADDRESS:08X}u = 0u;
    *(U32*)(tls_region + 0x04u) = 0x{NORMAL_LIVE_PRIMARY_TLS_VECTOR:08X}u;
    *(U32*)(tls_region + 0x20u) = tls_region;
    *(U32*)(tls_region + 0x28u) = 0x{NORMAL_LIVE_PRIMARY_THREAD_OBJECT:08X}u;
    *(U32*)(tls_region + 0x250u) = 0u;
    *(U32*)0x{NORMAL_LIVE_PRIMARY_THREAD_OBJECT + 0x28:08X}u =
        0x{NORMAL_LIVE_PRIMARY_TLS_DATA:08X}u;
    *(U32*)0x{NORMAL_LIVE_PRIMARY_TLS_VECTOR:08X}u =
        0x{NORMAL_LIVE_PRIMARY_TLS_DATA + 4:08X}u;
    copy_bytes((void*)0x72000000u, (void*)tls_region, 4096u);
    for (index = 0u; index < 8u; ++index) exchange->registers[index] = 0u;
    exchange->registers[4] = stack_top;
    exchange->eflags = 0x00000202u;
    exchange->eip = worker->start_address;
    if (!live_start_vblank_lane()) {{
        live_write_summary();
        return 0;
    }}
    return 1;
}}
static U8 live_ascii_lower(U8 value) {{
    return value >= 'A' && value <= 'Z' ? value + ('a' - 'A') : value;
}}
static BOOL live_path_prefix(
    const char* path, const char* prefix, U32* relative
) {{
    U32 index = 0u;
    while (prefix[index]) {{
        if (live_ascii_lower((U8)path[index]) !=
            live_ascii_lower((U8)prefix[index])) return 0;
        ++index;
    }}
    if (relative) *relative = index;
    return 1;
}}
static BOOL live_path_equal(const char* path, const char* expected) {{
    U32 relative = 0u;
    return live_path_prefix(path, expected, &relative) && path[relative] == 0;
}}
static BOOL live_append_path(
    char* output, U32 capacity, U32* size, const char* value
) {{
    U32 index = 0u;
    while (value[index]) {{
        if (*size + 1u >= capacity) return 0;
        output[(*size)++] = value[index++];
    }}
    output[*size] = 0;
    return 1;
}}
static BOOL live_decode_object_path(
    U32 object_attributes, char* output, U32 capacity
) {{
    U32 candidates[6];
    U32 candidate, index;
    if (!object_attributes || capacity < 2u) return 0;
    candidates[0] = object_attributes;
    for (index = 0u; index < 5u; ++index)
        candidates[index + 1u] = *(U32*)(object_attributes + index * 4u);
    for (candidate = 0u; candidate < 6u; ++candidate) {{
        U32 descriptor = candidates[candidate];
        U32 length, maximum, buffer;
        BOOL path_like = 0;
        if (!descriptor) continue;
        length = *(U16*)descriptor;
        maximum = *(U16*)(descriptor + 2u);
        buffer = *(U32*)(descriptor + 4u);
        if (!length || length > maximum || length >= capacity || !buffer) continue;
        for (index = 0u; index < length; ++index) {{
            U8 value = *(U8*)(buffer + index);
            if (value < 0x20u || value > 0x7Eu) break;
            output[index] = (char)value;
            if (value == '\\\\' || value == '/' || value == ':') path_like = 1;
        }}
        if (index == length && path_like) {{
            output[length] = 0;
            return 1;
        }}
    }}
    return 0;
}}
static BOOL live_map_guest_path(
    const char* guest, char* output, U32 capacity, BOOL* writable, BOOL* disc
) {{
    const char* root = g_live_extracted_root;
    const char* alias = 0;
    U32 relative = 0u, size = 0u, index;
    *writable = 0;
    *disc = 0;
    if (live_path_equal(guest, "\\\\Device\\\\Harddisk0\\\\Partition0")) {{
        root = g_live_cache_root;
        if (!live_append_path(output, capacity, &size, root)) return 0;
        if (size && output[size - 1u] != '\\\\' && output[size - 1u] != '/')
            output[size++] = '\\\\';
        output[size] = 0;
        *writable = 1;
        return live_append_path(output, capacity, &size, "partition0.bin");
    }}
    if (live_path_prefix(guest, "\\\\Device\\\\Cdrom0\\\\", &relative) ||
        live_path_prefix(guest, "\\\\??\\\\D:\\\\", &relative) ||
        live_path_prefix(guest, "D:\\\\", &relative) ||
        live_path_prefix(guest, "Cdrom0:\\\\", &relative)) {{
        root = g_live_extracted_root;
        *disc = 1;
    }} else if (live_path_equal(guest, "\\\\Device\\\\Cdrom0")) {{
        root = g_live_extracted_root;
        relative = live_string_size(guest);
        *disc = 1;
    }} else if (
        live_path_prefix(guest, "\\\\Device\\\\Harddisk0\\\\Partition0\\\\", &relative) ||
        live_path_prefix(guest, "\\\\Device\\\\Harddisk0\\\\Partition1\\\\", &relative) ||
        live_path_prefix(guest, "\\\\??\\\\E:\\\\", &relative) ||
        live_path_prefix(guest, "E:\\\\", &relative)
    ) {{
        root = g_live_save_root;
        *writable = 1;
    }} else if (live_path_prefix(guest, "\\\\??\\\\U:\\\\", &relative) ||
               live_path_prefix(guest, "U:\\\\", &relative)) {{
        root = g_live_save_root;
        alias = "UDATA\\\\41430019";
        *writable = 1;
    }} else if (live_path_prefix(guest, "\\\\??\\\\T:\\\\", &relative) ||
               live_path_prefix(guest, "T:\\\\", &relative)) {{
        root = g_live_save_root;
        alias = "TDATA\\\\41430019";
        *writable = 1;
    }} else if (
        live_path_prefix(guest, "\\\\Device\\\\Harddisk0\\\\Partition2\\\\", &relative) ||
        live_path_prefix(guest, "\\\\??\\\\C:\\\\", &relative) ||
        live_path_prefix(guest, "C:\\\\", &relative)
    ) {{
        root = g_live_dashboard_root;
    }} else if (
        live_path_prefix(guest, "\\\\Device\\\\Harddisk0\\\\Partition3\\\\", &relative) ||
        live_path_prefix(guest, "\\\\Device\\\\Harddisk0\\\\Partition4\\\\", &relative) ||
        live_path_prefix(guest, "\\\\Device\\\\Harddisk0\\\\Partition5\\\\", &relative) ||
        live_path_prefix(guest, "\\\\??\\\\X:\\\\", &relative) ||
        live_path_prefix(guest, "\\\\??\\\\Y:\\\\", &relative) ||
        live_path_prefix(guest, "\\\\??\\\\Z:\\\\", &relative) ||
        live_path_prefix(guest, "X:\\\\", &relative) ||
        live_path_prefix(guest, "Y:\\\\", &relative) ||
        live_path_prefix(guest, "Z:\\\\", &relative)
    ) {{
        root = g_live_cache_root;
        *writable = 1;
    }} else if (guest[0] && guest[1] == ':') {{
        return 0;
    }}
    while (guest[relative] == '\\\\' || guest[relative] == '/') ++relative;
    for (index = relative; guest[index]; ++index) {{
        BOOL segment = index == relative || guest[index - 1u] == '\\\\' ||
                       guest[index - 1u] == '/';
        if (guest[index] == ':' ||
            (segment && guest[index] == '.' && guest[index + 1u] == '.' &&
             (!guest[index + 2u] || guest[index + 2u] == '\\\\' ||
              guest[index + 2u] == '/'))) return 0;
    }}
    if (!root[0] || !live_append_path(output, capacity, &size, root)) return 0;
    if (size && output[size - 1u] != '\\\\' && output[size - 1u] != '/') {{
        if (size + 1u >= capacity) return 0;
        output[size++] = '\\\\';
        output[size] = 0;
    }}
    if (alias) {{
        if (!live_append_path(output, capacity, &size, alias)) return 0;
        if (size + 1u >= capacity) return 0;
        output[size++] = '\\\\';
        output[size] = 0;
    }}
    while (guest[relative]) {{
        if (size + 1u >= capacity) return 0;
        output[size++] = guest[relative] == '/' ? '\\\\' : guest[relative];
        ++relative;
    }}
    output[size] = 0;
    return 1;
}}
static U32 live_sha1_rotate(U32 value, U32 bits) {{
    return (value << bits) | (value >> (32u - bits));
}}
static void live_sha1_transform(LiveSha1Context* context, const U8* block) {{
    U32 words[80];
    U32 a, b, c, d, e, index;
    for (index = 0u; index < 16u; ++index) {{
        U32 offset = index * 4u;
        words[index] = ((U32)block[offset] << 24u) |
            ((U32)block[offset + 1u] << 16u) |
            ((U32)block[offset + 2u] << 8u) | block[offset + 3u];
    }}
    for (index = 16u; index < 80u; ++index)
        words[index] = live_sha1_rotate(
            words[index - 3u] ^ words[index - 8u] ^
            words[index - 14u] ^ words[index - 16u], 1u);
    a = context->state[0]; b = context->state[1]; c = context->state[2];
    d = context->state[3]; e = context->state[4];
    for (index = 0u; index < 80u; ++index) {{
        U32 function, constant, next;
        if (index < 20u) {{
            function = (b & c) | ((~b) & d); constant = 0x5A827999u;
        }} else if (index < 40u) {{
            function = b ^ c ^ d; constant = 0x6ED9EBA1u;
        }} else if (index < 60u) {{
            function = (b & c) | (b & d) | (c & d); constant = 0x8F1BBCDCu;
        }} else {{
            function = b ^ c ^ d; constant = 0xCA62C1D6u;
        }}
        next = live_sha1_rotate(a, 5u) + function + e + constant + words[index];
        e = d; d = c; c = live_sha1_rotate(b, 30u); b = a; a = next;
    }}
    context->state[0] += a; context->state[1] += b; context->state[2] += c;
    context->state[3] += d; context->state[4] += e;
}}
static void live_sha1_init(LiveSha1Context* context) {{
    U32 index;
    for (index = 0u; index < sizeof(*context); ++index) ((U8*)context)[index] = 0u;
    context->state[0] = 0x67452301u; context->state[1] = 0xEFCDAB89u;
    context->state[2] = 0x98BADCFEu; context->state[3] = 0x10325476u;
    context->state[4] = 0xC3D2E1F0u;
}}
static void live_sha1_update(
    LiveSha1Context* context, const U8* payload, U32 payload_size
) {{
    U32 buffer_offset = (context->count[0] >> 3u) & 63u;
    U32 added_bits = payload_size << 3u;
    U32 first_block_size = 64u - buffer_offset;
    U32 consumed = 0u;
    context->count[0] += added_bits;
    if (context->count[0] < added_bits) ++context->count[1];
    context->count[1] += payload_size >> 29u;
    if (payload_size >= first_block_size) {{
        copy_bytes(context->buffer + buffer_offset, payload, first_block_size);
        live_sha1_transform(context, context->buffer);
        consumed = first_block_size;
        while (consumed + 63u < payload_size) {{
            live_sha1_transform(context, payload + consumed);
            consumed += 64u;
        }}
    }}
    if (consumed < payload_size)
        copy_bytes(context->buffer + (payload_size >= first_block_size ? 0u : buffer_offset),
                   payload + consumed, payload_size - consumed);
}}
static void live_sha1_final(LiveSha1Context* context, U8* digest) {{
    U8 length[8], marker = 0x80u, zero = 0u;
    U32 index;
    for (index = 0u; index < 8u; ++index) {{
        U32 shift = (7u - index) * 8u;
        length[index] = (U8)(shift >= 32u
            ? context->count[1] >> (shift - 32u) : context->count[0] >> shift);
    }}
    live_sha1_update(context, &marker, 1u);
    while ((context->count[0] & 504u) != 448u) live_sha1_update(context, &zero, 1u);
    live_sha1_update(context, length, 8u);
    for (index = 0u; index < 20u; ++index)
        digest[index] = (U8)(context->state[index / 4u] >>
                             ((3u - (index & 3u)) * 8u));
}}
static U32 live_dispatch_time_or_sha(U32 value, U32* arguments) {{
    if (value == 24u) {{
        if (arguments[0]) {{
            LiveSha1Context context;
            live_sha1_init(&context);
            copy_bytes((void*)(arguments[0] + 24u), &context, sizeof(context));
        }}
        return 0u;
    }}
    if (value == 25u) {{
        if (arguments[0]) {{
            LiveSha1Context context;
            copy_bytes(&context, (void*)(arguments[0] + 24u), sizeof(context));
            if (arguments[1] && arguments[2])
                live_sha1_update(&context, (const U8*)arguments[1], arguments[2]);
            copy_bytes((void*)(arguments[0] + 24u), &context, sizeof(context));
        }}
        return 0u;
    }}
    if (value == 26u) {{
        if (arguments[0] && arguments[1]) {{
            LiveSha1Context context;
            U8 digest[20];
            U32 index;
            copy_bytes(&context, (void*)(arguments[0] + 24u), sizeof(context));
            live_sha1_final(&context, digest);
            copy_bytes((void*)arguments[1], digest, sizeof(digest));
            for (index = 0u; index < sizeof(context); ++index)
                *(U8*)(arguments[0] + 24u + index) = 0u;
        }}
        return 0u;
    }}
    if (value == 30u) {{
        LiveSystemTime system_time;
        LiveFileTime file_time;
        U16* fields = (U16*)arguments[0];
        if (!fields || !arguments[1]) return 0u;
        system_time.year = fields[0]; system_time.month = fields[1];
        system_time.day_of_week = 0u; system_time.day = fields[2];
        system_time.hour = fields[3]; system_time.minute = fields[4];
        system_time.second = fields[5]; system_time.milliseconds = fields[6];
        if (!SystemTimeToFileTime(&system_time, &file_time)) return 0u;
        *(U32*)arguments[1] = file_time.low;
        *(U32*)(arguments[1] + 4u) = file_time.high;
        return 1u;
    }}
    if (value == 31u) {{
        LiveFileTime file_time;
        LiveSystemTime system_time;
        U16* fields = (U16*)arguments[1];
        if (!arguments[0] || !fields) return 0u;
        file_time.low = *(U32*)arguments[0];
        file_time.high = *(U32*)(arguments[0] + 4u);
        if (FileTimeToSystemTime(&file_time, &system_time)) {{
            fields[0] = system_time.year; fields[1] = system_time.month;
            fields[2] = system_time.day; fields[3] = system_time.hour;
            fields[4] = system_time.minute; fields[5] = system_time.second;
            fields[6] = system_time.milliseconds; fields[7] = system_time.day_of_week;
        }}
        return 0u;
    }}
    return 0xC0000002u;
}}
static LiveFile* live_find_file(U32 handle) {{
    U32 index, slot = (handle >> 2u) & 255u;
    LiveFile* cached = g_live_file_cache[slot];
    if (cached && cached->active && cached->handle == handle) return cached;
    for (index = 0u; index < g_live_file_count; ++index)
        if (g_live_files[index].active && g_live_files[index].handle == handle) {{
            g_live_file_cache[slot] = &g_live_files[index];
            return &g_live_files[index];
        }}
    return 0;
}}
static LiveFile* live_new_file(U32 flags) {{
    LiveFile* file = 0;
    U32 index;
    for (index = 0u; index < g_live_file_count; ++index) {{
        if (!g_live_files[index].active) {{
            file = &g_live_files[index];
            break;
        }}
    }}
    if (!file) {{
        if (g_live_file_count >= 128u) return 0;
        file = &g_live_files[g_live_file_count++];
    }}
    for (index = 0u; index < sizeof(*file); ++index) ((U8*)file)[index] = 0u;
    g_workload_next_object_handle += 4u;
    file->handle = g_workload_next_object_handle;
    file->flags = flags;
    file->active = 1u;
    file->host = (HANDLE)-1;
    g_live_file_cache[(file->handle >> 2u) & 255u] = file;
    return file;
}}
static void live_copy_string(char* output, U32 capacity, const char* input) {{
    U32 index = 0u;
    if (!capacity) return;
    while (input[index] && index + 1u < capacity) {{
        output[index] = input[index];
        ++index;
    }}
    output[index] = 0;
}}
static int live_compare_names(const char* left, const char* right) {{
    U32 index = 0u;
    while (left[index] && right[index]) {{
        U8 a = live_ascii_lower((U8)left[index]);
        U8 b = live_ascii_lower((U8)right[index]);
        if (a != b) return a < b ? -1 : 1;
        ++index;
    }}
    if (left[index] == right[index]) return 0;
    return left[index] ? 1 : -1;
}}
static BOOL live_wildcard_match(const char* pattern, const char* value) {{
    const char* star = 0;
    const char* retry = 0;
    while (*value) {{
        if (*pattern == '?' ||
            live_ascii_lower((U8)*pattern) == live_ascii_lower((U8)*value)) {{
            ++pattern;
            ++value;
        }} else if (*pattern == '*') {{
            star = pattern++;
            retry = value;
        }} else if (star) {{
            pattern = star + 1;
            value = ++retry;
        }} else return 0;
    }}
    while (*pattern == '*') ++pattern;
    return *pattern == 0;
}}
static U32 live_status_from_error(U32 error) {{
    if (error == 2u) return 0xC0000034u;
    if (error == 3u) return 0xC000003Au;
    if (error == 5u) return 0xC0000022u;
    if (error == 80u || error == 183u) return 0xC0000035u;
    return 0xC000000Du;
}}
static void live_write_io_status(U32 address, U32 status, U32 information) {{
    if (address) {{
        *(U32*)address = status;
        *(U32*)(address + 4u) = status == 0u ? information : 0u;
    }}
}}
static U32 live_file_information(
    LiveFile* file, U32 destination, U32 requested, U32 information_class
) {{
    U32 payload_size = 24u;
    U32 write_size, index;
    U64 allocation_size;
    if (!file || !destination || !requested) return 0u;
    if (information_class == 4u) payload_size = 40u;
    else if (information_class == 34u) payload_size = 56u;
    else if (information_class == 14u || information_class == 19u ||
             information_class == 20u) payload_size = 8u;
    else if (information_class == 18u) payload_size = 76u;
    write_size = requested < payload_size ? requested : payload_size;
    for (index = 0u; index < write_size; ++index) *(U8*)(destination + index) = 0u;
    allocation_size = file->size ? (file->size + 0x7FFu) & ~0x7FFull : 0u;
    if (information_class == 4u) {{
        if (write_size >= 36u) *(U32*)(destination + 32u) =
            (file->flags & 1u) ? 0x10u : 0x80u;
    }} else if (information_class == 34u) {{
        if (write_size >= 40u) *(U64*)(destination + 32u) = allocation_size;
        if (write_size >= 48u) *(U64*)(destination + 40u) = file->size;
        if (write_size >= 52u) *(U32*)(destination + 48u) =
            (file->flags & 1u) ? 0x10u : 0x80u;
    }} else if (information_class == 14u) {{
        if (write_size >= 8u) *(U64*)destination = file->position;
    }} else if (information_class == 19u) {{
        if (write_size >= 8u) *(U64*)destination = allocation_size;
    }} else if (information_class == 20u) {{
        if (write_size >= 8u) *(U64*)destination = file->size;
    }} else {{
        U32 offset = information_class == 18u ? 40u : 0u;
        if (write_size >= offset + 8u) *(U64*)(destination + offset) = allocation_size;
        if (write_size >= offset + 16u) *(U64*)(destination + offset + 8u) = file->size;
        if (write_size >= offset + 20u) *(U32*)(destination + offset + 16u) = 1u;
        if (write_size >= offset + 22u)
            *(U8*)(destination + offset + 21u) = (file->flags & 1u) ? 1u : 0u;
    }}
    return write_size;
}}
static U32 live_open_file_service(U32 value, U32* arguments) {{
    const U32 INVALID_ATTRIBUTES = 0xFFFFFFFFu;
    U32 output_handle = arguments[0];
    U32 object_attributes = arguments[2];
    U32 io_status = arguments[3];
    U32 create_options = value == 11u ? arguments[8] : arguments[5];
    U32 create_disposition = value == 11u ? arguments[7] : 1u;
    U32 attributes, flags = 0u, status = 0u, information = 0u;
    U32 win_disposition = 3u;
    BOOL directory, writable, disc;
    char guest[512], host[1024];
    LiveFile* file;
    HANDLE handle = (HANDLE)-1;
    if (!live_decode_object_path(object_attributes, guest, sizeof(guest)) ||
        !live_map_guest_path(guest, host, sizeof(host), &writable, &disc)) {{
        status = 0xC000000Du;
        live_write_io_status(io_status, status, 0u);
        return status;
    }}
    attributes = GetFileAttributesA(host);
    directory = (create_options & 1u) != 0u;
    if (!directory) {{
        U32 length = live_string_size(guest);
        directory = length && (guest[length - 1u] == '\\\\' ||
                               guest[length - 1u] == '/');
    }}
    if (directory) {{
        if (attributes == INVALID_ATTRIBUTES) {{
            if (value == 11u && writable &&
                (create_disposition == 2u || create_disposition == 3u ||
                 create_disposition == 5u)) {{
                if (!CreateDirectoryA(host, 0)) status = live_status_from_error(GetLastError());
                else information = 2u;
            }} else status = 0xC000003Au;
        }} else if (!(attributes & 0x10u)) status = 0xC000000Du;
        else if (value == 11u && create_disposition == 2u) status = 0xC0000035u;
        else information = 1u;
        flags |= 1u;
    }} else {{
        U32 access = 0x80000000u | (writable ? 0x40000000u : 0u);
        if (value == 11u) {{
            if (create_disposition == 0u) win_disposition = 2u;
            else if (create_disposition == 1u) win_disposition = 3u;
            else if (create_disposition == 2u) win_disposition = 1u;
            else if (create_disposition == 3u) win_disposition = 4u;
            else if (create_disposition == 4u) win_disposition = 5u;
            else win_disposition = 2u;
        }}
        handle = CreateFileA(host, access, 3u, 0, win_disposition, 0x80u, 0);
        if (handle == (HANDLE)-1) status = live_status_from_error(GetLastError());
        else information = attributes == INVALID_ATTRIBUTES ? 2u : 1u;
    }}
    if (status) {{
        if (output_handle) *(U32*)output_handle = 0u;
        live_write_io_status(io_status, status, 0u);
        return status;
    }}
    if (disc) flags |= 2u;
    if (writable) flags |= 8u;
    file = live_new_file(flags);
    if (!file) {{
        if (handle != (HANDLE)-1) CloseHandle(handle);
        status = 0xC000009Au;
    }} else {{
        U32 high = 0u, low = 0u;
        file->host = handle;
        live_copy_string(file->guest_path, sizeof(file->guest_path), guest);
        live_copy_string(file->host_path, sizeof(file->host_path), host);
        if (handle != (HANDLE)-1) {{
            low = GetFileSize(handle, &high);
            if (low != 0xFFFFFFFFu || GetLastError() == 0u)
                file->size = ((U64)high << 32u) | low;
        }}
        if (output_handle) *(U32*)output_handle = file->handle;
    }}
    live_write_io_status(io_status, status, information);
    return status;
}}
static U32 live_query_directory(U32* arguments) {{
    LiveFile* file = live_find_file(arguments[0]);
    U32 io_status = arguments[4], destination = arguments[5];
    U32 requested = arguments[6], information_class = arguments[7];
    U32 name_descriptor = arguments[8], count = 0u, index, status = 0u;
    char pattern[260], search_path[1280];
    HANDLE search;
    LiveFindData data;
    pattern[0] = '*'; pattern[1] = 0;
    if (!file) status = 0xC0000008u;
    else if (!(file->flags & 1u) || !destination || !requested) status = 0xC000000Du;
    else if (information_class != 1u) status = 0xC0000003u;
    if (!status && name_descriptor) {{
        U32 length = *(U16*)name_descriptor;
        U32 buffer = *(U32*)(name_descriptor + 4u);
        if (length >= sizeof(pattern) || (length && !buffer)) status = 0xC000000Du;
        else {{
            for (index = 0u; index < length; ++index) pattern[index] = *(char*)(buffer + index);
            pattern[length] = 0;
            if (live_path_equal(pattern, "*.*")) {{ pattern[0] = '*'; pattern[1] = 0; }}
            live_copy_string(file->directory_pattern, sizeof(file->directory_pattern), pattern);
            file->directory_index = 0u;
        }}
    }} else if (!status && file->directory_pattern[0]) {{
        live_copy_string(pattern, sizeof(pattern), file->directory_pattern);
    }}
    if (!status && arguments[9]) file->directory_index = 0u;
    if (!status) {{
        U32 size = 0u;
        if (!live_append_path(search_path, sizeof(search_path), &size, file->host_path))
            status = 0xC000000Du;
        else {{
            if (size && search_path[size - 1u] != '\\\\' && search_path[size - 1u] != '/')
                search_path[size++] = '\\\\';
            search_path[size++] = '*'; search_path[size] = 0;
            search = FindFirstFileA(search_path, &data);
            if (search != (HANDLE)-1) {{
                do {{
                    if (!live_path_equal(data.name, ".") && !live_path_equal(data.name, "..") &&
                        live_wildcard_match(pattern, data.name) && count < 128u) {{
                        copy_bytes(&g_live_find_entries[count], &data, sizeof(data));
                        ++count;
                    }}
                }} while (FindNextFileA(search, &data));
                FindClose(search);
            }}
            for (index = 0u; index < count; ++index) {{
                U32 right;
                for (right = index + 1u; right < count; ++right)
                    if (live_compare_names(g_live_find_entries[right].name,
                                           g_live_find_entries[index].name) < 0) {{
                        LiveFindData temporary;
                        copy_bytes(&temporary, &g_live_find_entries[index],
                                   sizeof(temporary));
                        copy_bytes(&g_live_find_entries[index],
                                   &g_live_find_entries[right], sizeof(temporary));
                        copy_bytes(&g_live_find_entries[right], &temporary,
                                   sizeof(temporary));
                    }}
            }}
            if (file->directory_index >= count) status = 0x80000006u;
            else {{
                LiveFindData* entry = &g_live_find_entries[file->directory_index];
                U32 name_length = live_string_size(entry->name);
                U32 needed = 64u + name_length;
                U64 file_size = ((U64)entry->size_high << 32u) | entry->size_low;
                U64 allocation = file_size ? (file_size + 0xFFFu) & ~0xFFFull : 0u;
                if (requested < needed) status = 0xC0000023u;
                else {{
                    for (index = 0u; index < needed; ++index)
                        *(U8*)(destination + index) = 0u;
                    *(U32*)(destination + 4u) = file->directory_index;
                    *(U64*)(destination + 8u) =
                        ((U64)entry->creation.high << 32u) | entry->creation.low;
                    *(U64*)(destination + 16u) =
                        ((U64)entry->access.high << 32u) | entry->access.low;
                    *(U64*)(destination + 24u) =
                        ((U64)entry->write.high << 32u) | entry->write.low;
                    *(U64*)(destination + 32u) = *(U64*)(destination + 24u);
                    *(U64*)(destination + 40u) = (entry->attributes & 0x10u) ? 0u : file_size;
                    *(U64*)(destination + 48u) = (entry->attributes & 0x10u) ? 0u : allocation;
                    *(U32*)(destination + 56u) = (entry->attributes & 0x10u) ? 0x10u : 0x80u;
                    *(U32*)(destination + 60u) = name_length;
                    copy_bytes((void*)(destination + 64u), entry->name, name_length);
                    ++file->directory_index;
                    live_write_io_status(io_status, 0u, needed);
                    return 0u;
                }}
            }}
        }}
    }}
    live_write_io_status(io_status, status, 0u);
    return status;
}}
static U32 live_dispatch_bootstrap_service(
    const ServiceDescriptor* service, U32* arguments, U32 fallback
) {{
    U32 value = service->runtime_value;
    if (value == 11u || value == 12u) return live_open_file_service(value, arguments);
    if (value == 13u) {{
        LiveFile* link;
        char guest[512];
        U32 status = 0u;
        if (!live_decode_object_path(arguments[1], guest, sizeof(guest)) ||
            !live_path_equal(guest, "\\\\??\\\\D:")) status = 0xC0000034u;
        else if (!(link = live_new_file(4u))) status = 0xC0000034u;
        else if (arguments[0]) *(U32*)arguments[0] = link->handle;
        return status;
    }}
    if (value == 14u) {{
        LiveFile* file = live_find_file(arguments[0]);
        U32 status = file ? 0u : 0xC0000008u;
        U32 written = file ? live_file_information(
            file, arguments[2], arguments[3], arguments[4]) : 0u;
        live_write_io_status(arguments[1], status, written);
        return status;
    }}
    if (value == 15u) {{
        static const char target[] = "\\\\Device\\\\Cdrom0";
        LiveFile* link = live_find_file(arguments[0]);
        U32 descriptor = arguments[1], written = 0u;
        if (!link || !(link->flags & 4u) || !descriptor) return 0xC0000008u;
        if (*(U32*)(descriptor + 4u)) {{
            written = *(U16*)(descriptor + 2u);
            if (written > sizeof(target) - 1u) written = sizeof(target) - 1u;
            copy_bytes((void*)*(U32*)(descriptor + 4u), target, written);
            *(U16*)descriptor = (U16)written;
        }}
        if (arguments[2]) *(U32*)arguments[2] = written;
        return 0u;
    }}
    if (value == 16u) {{
        LiveFile* file = live_find_file(arguments[0]);
        U32 destination = arguments[2], requested = arguments[3];
        U32 information_class = arguments[4], status = 0u, written = 0u;
        BOOL disc = file && (file->flags & 2u);
        if (!file) status = 0xC0000008u;
        else if (information_class == 3u) {{
            if (!destination || requested < 24u) status = 0xC0000004u;
            else {{
                *(U64*)destination = disc ? 3820880u : 312501u;
                *(U64*)(destination + 8u) = disc ? 0u : 312501u;
                *(U32*)(destination + 16u) = disc ? 1u : 32u;
                *(U32*)(destination + 20u) = disc ? 2048u : 512u;
                written = 24u;
            }}
        }} else if (information_class == 1u) {{
            const char* label = disc ? "BURNOUT2" : "XBOX DATA";
            U32 length = disc ? 8u : 9u;
            if (!destination || requested < 17u + length) status = 0xC0000004u;
            else {{
                U32 index;
                *(U64*)destination = 0u; *(U32*)(destination + 8u) = 0x41430019u;
                *(U32*)(destination + 12u) = length; *(U8*)(destination + 16u) = 0u;
                for (index = 0u; index < length; ++index)
                    *(U8*)(destination + 17u + index) = (U8)label[index];
                written = 17u + length;
            }}
        }} else status = 0xC0000003u;
        live_write_io_status(arguments[1], status, written);
        return status;
    }}
    if (value == 20u || value == 22u) {{
        LiveFile* file = live_find_file(arguments[0]);
        U32 io_status = arguments[4], buffer = arguments[5], requested = arguments[6];
        U32 transferred = 0u, consumed = 0u, status = 0u;
        U64 offset, transfer_end, position_end;
        long high;
        if (!file || file->host == (HANDLE)-1) status = 0xC0000008u;
        else if (!buffer && requested) status = 0xC000000Du;
        else if (value == 22u && !(file->flags & 8u)) status = 0xC0000022u;
        else {{
            offset = arguments[7] ? *(U64*)arguments[7] : file->position;
            high = (long)(offset >> 32u);
            if (SetFilePointer(file->host, (long)offset, &high, 0u) == 0xFFFFFFFFu &&
                GetLastError() != 0u) status = 0xC000000Du;
            else if (value == 20u) {{
                if (!ReadFile(file->host, (void*)buffer, requested, &transferred, 0))
                    status = 0xC000000Du;
                else {{
                    consumed = transferred;
                    /* Disc streams issue fixed-size reads through and beyond
                       logical EOF. Report zero-filled completion without
                       advancing the persistent cursor past consumed bytes. */
                    if ((file->flags & 2u) && transferred < requested &&
                        (offset >= file->size ||
                         (U64)transferred == file->size - offset)) {{
                        U32 index;
                        for (index = transferred; index < requested; ++index)
                            *(U8*)(buffer + index) = 0u;
                        transferred = requested;
                    }}
                }}
            }} else {{
                if (!WriteFile(file->host, (void*)buffer, requested, &transferred, 0))
                    status = 0xC000000Du;
                consumed = transferred;
            }}
            transfer_end = offset + (U64)transferred;
            position_end = offset + (U64)consumed;
            if (!arguments[7]) file->position = position_end;
            if (value == 22u && transfer_end > file->size)
                file->size = transfer_end;
        }}
        live_write_io_status(io_status, status, transferred);
        return status;
    }}
    if (value == 21u) {{
        LiveFile* file = live_find_file(arguments[0]);
        U32 information_class = arguments[4], status = 0u, consumed = 0u;
        if (!file || file->host == (HANDLE)-1) status = 0xC0000008u;
        else if (information_class == 13u) {{
            if (!arguments[2] || !arguments[3]) status = 0xC000000Du;
            else {{ file->delete_pending = *(U8*)arguments[2] ? 1u : 0u; consumed = 1u; }}
        }} else if (information_class == 14u) {{
            if (!arguments[2] || arguments[3] < 8u) status = 0xC000000Du;
            else {{ file->position = *(U64*)arguments[2]; consumed = 8u; }}
        }} else if (information_class == 20u) {{
            U64 size;
            long high;
            if (!arguments[2] || arguments[3] < 8u || !(file->flags & 8u))
                status = 0xC0000022u;
            else {{
                size = *(U64*)arguments[2]; high = (long)(size >> 32u);
                if ((SetFilePointer(file->host, (long)size, &high, 0u) == 0xFFFFFFFFu &&
                     GetLastError() != 0u) || !SetEndOfFile(file->host))
                    status = 0xC000000Du;
                else {{ file->size = size; consumed = 8u; }}
            }}
        }} else consumed = arguments[3] < 8u ? arguments[3] : 8u;
        live_write_io_status(arguments[1], status, consumed);
        return status;
    }}
    if (value == 23u) return live_query_directory(arguments);
    if (value == 24u || value == 25u || value == 26u ||
        value == 30u || value == 31u)
        return live_dispatch_time_or_sha(value, arguments);
    if (value == 9u) {{
        LiveFile* file = live_find_file(arguments[0]);
        if (file) {{
            U32 cache_slot = (file->handle >> 2u) & 255u;
            if (file->host != (HANDLE)-1) CloseHandle(file->host);
            file->host = (HANDLE)-1;
            if (g_live_file_cache[cache_slot] == file)
                g_live_file_cache[cache_slot] = 0;
            file->active = 0u;
            if (file->delete_pending) DeleteFileA(file->host_path);
        }}
        return 0u;
    }}
    return fallback;
}}
static void live_append_literal(char* output, U32* cursor, const char* value) {{
    U32 index = 0u;
    while (value[index]) output[(*cursor)++] = value[index++];
}}
static void live_append_hex(char* output, U32* cursor, U32 value) {{
    static const char digits[] = "0123456789ABCDEF";
    U32 shift;
    output[(*cursor)++] = '0'; output[(*cursor)++] = 'x';
    for (shift = 28u;; shift -= 4u) {{
        output[(*cursor)++] = digits[(value >> shift) & 15u];
        if (!shift) break;
    }}
}}
static void live_append_u32(char* output, U32* cursor, U32 value) {{
    char digits[10];
    U32 count = 0u;
    if (!value) {{
        output[(*cursor)++] = '0';
        return;
    }}
    while (value) {{
        digits[count++] = '0' + (char)(value % 10u);
        value /= 10u;
    }}
    while (count) output[(*cursor)++] = digits[--count];
}}
static BOOL live_claim_summary(void) {{
    return __atomic_exchange_n(&g_live_summary_claimed, 1u, 5) == 0u;
}}
static void live_write_fault_summary(ExceptionRecord* record, void* context) {{
    const U32 GENERIC_WRITE = 0x40000000u;
    const U32 CREATE_ALWAYS = 2u;
    const U32 FILE_ATTRIBUTE_NORMAL = 0x80u;
    HANDLE file;
    U32 cursor = 0u, written, index;
    char payload[1024];
    if (!record) return;
    if (!g_live_summary_path[0] &&
        !live_sibling_path("normal-live-fault.json", g_live_summary_path,
                           sizeof(g_live_summary_path))) return;
    if (!live_claim_summary()) return;
    live_append_literal(payload, &cursor, "{{\\\"identity\\\":{{\\\"run_id\\\":\\\"");
    for (index = 0u; g_live_run_id[index] && cursor + 1u < sizeof(payload);
         ++index) payload[cursor++] = g_live_run_id[index];
    live_append_literal(payload, &cursor,
        "\\\"}},\\\"backend\\\":\\\"same-isa-ia32\\\","
        "\\\"normal_runtime_python_callbacks\\\":0,"
        "\\\"native_fault\\\":{{\\\"exception_code\\\":\\\"");
    live_append_hex(payload, &cursor, record->code);
    live_append_literal(payload, &cursor, "\\\",\\\"exception_address\\\":\\\"");
    live_append_hex(payload, &cursor, (U32)record->address);
    live_append_literal(payload, &cursor, "\\\",\\\"startup_phase\\\":");
    payload[cursor++] = '0' + (char)(g_live_startup_phase % 10u);
    live_append_literal(payload, &cursor, ",\\\"access_kind\\\":");
    if (record->parameter_count > 0u) {{
        payload[cursor++] = '0' + (char)(record->information[0] % 10u);
    }} else payload[cursor++] = '0';
    live_append_literal(payload, &cursor, ",\\\"access_address\\\":\\\"");
    live_append_hex(payload, &cursor,
                    record->parameter_count > 1u ? record->information[1] : 0u);
    // Windows x86 CONTEXT stores EDI..EIP at DWORDs 39..46 and ESP at 49.
    live_append_literal(payload, &cursor, "\\\",\\\"registers\\\":{{\\\"eax\\\":\\\"");
    live_append_hex(payload, &cursor, context ? ((U32*)context)[44] : 0u);
    live_append_literal(payload, &cursor, "\\\",\\\"ecx\\\":\\\"");
    live_append_hex(payload, &cursor, context ? ((U32*)context)[43] : 0u);
    live_append_literal(payload, &cursor, "\\\",\\\"edx\\\":\\\"");
    live_append_hex(payload, &cursor, context ? ((U32*)context)[42] : 0u);
    live_append_literal(payload, &cursor, "\\\",\\\"ebx\\\":\\\"");
    live_append_hex(payload, &cursor, context ? ((U32*)context)[41] : 0u);
    live_append_literal(payload, &cursor, "\\\",\\\"esi\\\":\\\"");
    live_append_hex(payload, &cursor, context ? ((U32*)context)[40] : 0u);
    live_append_literal(payload, &cursor, "\\\",\\\"edi\\\":\\\"");
    live_append_hex(payload, &cursor, context ? ((U32*)context)[39] : 0u);
    live_append_literal(payload, &cursor, "\\\",\\\"ebp\\\":\\\"");
    live_append_hex(payload, &cursor, context ? ((U32*)context)[45] : 0u);
    live_append_literal(payload, &cursor, "\\\",\\\"esp\\\":\\\"");
    live_append_hex(payload, &cursor, context ? ((U32*)context)[49] : 0u);
    live_append_literal(payload, &cursor, "\\\",\\\"eip\\\":\\\"");
    live_append_hex(payload, &cursor, context ? ((U32*)context)[46] : 0u);
    live_append_literal(payload, &cursor,
        "\\\"}},\\\"audio_snapshot\\\":{{\\\"service_count\\\":");
    live_append_u32(payload, &cursor, (U32)g_live_audio_service_count);
    live_append_literal(payload, &cursor, ",\\\"stream_process_calls\\\":");
    live_append_u32(
        payload, &cursor, (U32)g_live_audio_service_value_counts[8]);
    live_append_literal(payload, &cursor, ",\\\"decoded_packets\\\":");
    live_append_u32(payload, &cursor, (U32)g_live_audio_decoded_packet_count);
    live_append_literal(payload, &cursor, ",\\\"packet_completions\\\":");
    live_append_u32(payload, &cursor, (U32)g_live_audio_packet_completion_count);
    live_append_literal(payload, &cursor, ",\\\"packet_flushes\\\":");
    live_append_u32(payload, &cursor, (U32)g_live_audio_packet_flush_count);
    live_append_literal(payload, &cursor, "}}}}\\n");
    file = CreateFileA(g_live_summary_path, GENERIC_WRITE, 0, 0,
                       CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, 0);
    if (file != (HANDLE)-1) {{
        WriteFile(file, payload, cursor, &written, 0);
        CloseHandle(file);
    }}
}}
static long __stdcall live_exception_filter(ExceptionPointers* pointers) {{
    live_write_fault_summary(
        pointers ? pointers->record : 0,
        pointers ? pointers->context : 0);
    return 1;
}}
static void live_write_summary(void) {{
    const U32 GENERIC_WRITE = 0x40000000u;
    const U32 CREATE_ALWAYS = 2u;
    const U32 FILE_ATTRIBUTE_NORMAL = 0x80u;
    HANDLE file;
    U32 written;
    U32 cursor = 0u;
    char payload[1600];
    const char* first = "{{\\\"identity\\\":{{\\\"run_id\\\":\\\"";
    const char* second = "\\\"}},\\\"backend\\\":\\\"same-isa-ia32\\\",";
    const char* third =
        "\\\"normal_runtime_python_callbacks\\\":0,\\\"native_vblank\\\":{{"
        "\\\"callback_address\\\":\\\"";
    if (!g_live_summary_path[0]) return;
    if (!live_claim_summary()) return;
    for (U32 index = 0u; first[index]; ++index) payload[cursor++] = first[index];
    for (U32 index = 0u; g_live_run_id[index] && cursor + 1u < sizeof(payload);
         ++index) payload[cursor++] = g_live_run_id[index];
    for (U32 index = 0u; second[index]; ++index) payload[cursor++] = second[index];
    for (U32 index = 0u; third[index]; ++index) payload[cursor++] = third[index];
    live_append_hex(payload, &cursor, g_live_vblank_callback_address);
    live_append_literal(payload, &cursor, "\\\",\\\"sequence\\\":");
    live_append_u32(payload, &cursor, g_live_vblank_sequence);
    live_append_literal(payload, &cursor, ",\\\"schedule_count\\\":");
    live_append_u32(payload, &cursor, g_live_vblank_schedule_count);
    live_append_literal(payload, &cursor, ",\\\"run_count\\\":");
    live_append_u32(payload, &cursor, g_live_vblank_run_count);
    live_append_literal(payload, &cursor, ",\\\"completion_count\\\":");
    live_append_u32(payload, &cursor, g_live_vblank_completion_count);
    live_append_literal(payload, &cursor, ",\\\"failure_count\\\":");
    live_append_u32(payload, &cursor, g_live_vblank_failure_count);
    live_append_literal(payload, &cursor, ",\\\"failure_code\\\":");
    live_append_u32(payload, &cursor, g_live_vblank_failure_code);
    live_append_literal(payload, &cursor,
        "}},\\\"native_render\\\":{{\\\"publication_failure_code\\\":");
    live_append_u32(payload, &cursor, g_live_render_publication_failure_code);
    live_append_literal(payload, &cursor, ",\\\"retained_vertex_ranges\\\":");
    live_append_u32(
        payload, &cursor, g_live_resource_scan.retained_vertex_range_count);
    live_append_literal(payload, &cursor, ",\\\"retained_texture_bindings\\\":");
    live_append_u32(payload, &cursor, g_live_resource_scan.retained_binding_count);
    live_append_literal(payload, &cursor, ",\\\"resource_payload_bytes\\\":");
    live_append_u32(payload, &cursor, g_live_resource_payload_size);
    live_append_literal(payload, &cursor, ",\\\"guest_flips\\\":");
    live_append_u32(payload, &cursor, (U32)g_live_flip_count);
    live_append_literal(payload, &cursor,
        "}},\\\"native_input\\\":{{\\\"refresh_count\\\":");
    live_append_u32(payload, &cursor, (U32)g_live_controller_refresh_count);
    live_append_literal(payload, &cursor, ",\\\"poll_count\\\":");
    live_append_u32(payload, &cursor, (U32)g_live_controller_poll_count);
    live_append_literal(payload, &cursor, ",\\\"observed_buttons\\\":");
    live_append_u32(payload, &cursor, g_live_controller_observed_buttons);
    live_append_literal(payload, &cursor, ",\\\"connected\\\":");
    live_append_u32(payload, &cursor, g_live_controller.connected);
    live_append_literal(payload, &cursor, ",\\\"sequence\\\":");
    live_append_u32(payload, &cursor, g_live_controller_sequence);
    live_append_literal(payload, &cursor, ",\\\"device_queries\\\":");
    live_append_u32(
        payload, &cursor, (U32)g_live_controller_service_counts[4]);
    live_append_literal(payload, &cursor, ",\\\"open_calls\\\":");
    live_append_u32(
        payload, &cursor, (U32)g_live_controller_service_counts[5]);
    live_append_literal(payload, &cursor, ",\\\"capability_calls\\\":");
    live_append_u32(
        payload, &cursor, (U32)g_live_controller_service_counts[6]);
    live_append_literal(payload, &cursor, ",\\\"state_calls\\\":");
    live_append_u32(
        payload, &cursor, (U32)g_live_controller_service_counts[7]);
    live_append_literal(payload, &cursor, ",\\\"close_calls\\\":");
    live_append_u32(
        payload, &cursor, (U32)g_live_controller_service_counts[16]);
    live_append_literal(payload, &cursor, ",\\\"set_state_calls\\\":");
    live_append_u32(
        payload, &cursor, (U32)g_live_controller_service_counts[17]);
    live_append_literal(payload, &cursor, ",\\\"open_mask\\\":");
    live_append_u32(payload, &cursor, g_live_controller_open_mask);
    live_append_literal(payload, &cursor, ",\\\"left_motor\\\":");
    live_append_u32(payload, &cursor, g_live_controller_last_left_motor);
    live_append_literal(payload, &cursor, ",\\\"right_motor\\\":");
    live_append_u32(payload, &cursor, g_live_controller_last_right_motor);
    live_append_literal(payload, &cursor,
        "}},\\\"native_audio\\\":{{\\\"service_count\\\":");
    live_append_u32(payload, &cursor, (U32)g_live_audio_service_count);
    live_append_literal(payload, &cursor, ",\\\"buffer_set_data_calls\\\":");
    live_append_u32(
        payload, &cursor, (U32)g_live_audio_service_value_counts[2]);
    live_append_literal(payload, &cursor, ",\\\"buffer_play_calls\\\":");
    live_append_u32(
        payload, &cursor, (U32)g_live_audio_service_value_counts[4]);
    live_append_literal(payload, &cursor, ",\\\"buffer_create_calls\\\":");
    live_append_u32(
        payload, &cursor, (U32)g_live_audio_service_value_counts[11]);
    live_append_literal(payload, &cursor, ",\\\"stream_create_calls\\\":");
    live_append_u32(
        payload, &cursor, (U32)g_live_audio_service_value_counts[7]);
    live_append_literal(payload, &cursor, ",\\\"stream_process_calls\\\":");
    live_append_u32(
        payload, &cursor, (U32)g_live_audio_service_value_counts[8]);
    live_append_literal(payload, &cursor, ",\\\"buffer_release_calls\\\":");
    live_append_u32(
        payload, &cursor, (U32)g_live_audio_service_value_counts[29]);
    live_append_literal(payload, &cursor, ",\\\"stream_release_calls\\\":");
    live_append_u32(
        payload, &cursor, (U32)g_live_audio_service_value_counts[30]);
    live_append_literal(payload, &cursor, ",\\\"music_mode_calls\\\":");
    live_append_u32(payload, &cursor, (U32)g_live_audio_music_mode_count);
    live_append_literal(payload, &cursor, ",\\\"music_loads\\\":");
    live_append_u32(payload, &cursor, (U32)g_live_audio_music_load_count);
    live_append_literal(payload, &cursor, ",\\\"music_failures\\\":");
    live_append_u32(payload, &cursor, (U32)g_live_audio_music_failure_count);
    live_append_literal(payload, &cursor, ",\\\"music_mode\\\":");
    live_append_u32(payload, &cursor, g_live_audio_music_mode);
    live_append_literal(payload, &cursor, ",\\\"music_active\\\":");
    live_append_u32(payload, &cursor, g_live_audio_music_active);
    live_append_literal(payload, &cursor, ",\\\"music_kind\\\":");
    live_append_u32(payload, &cursor, g_live_audio_music_kind);
    live_append_literal(payload, &cursor, ",\\\"music_track_index\\\":");
    live_append_u32(payload, &cursor, g_live_audio_music_track_index);
    live_append_literal(payload, &cursor, ",\\\"music_streams\\\":");
    live_append_u32(payload, &cursor, g_live_audio_music_stream_count);
    live_append_literal(payload, &cursor, ",\\\"lane_starts\\\":");
    live_append_u32(payload, &cursor, (U32)g_live_audio_lane_start_count);
    live_append_literal(payload, &cursor, ",\\\"lane_start_failures\\\":");
    live_append_u32(
        payload, &cursor, (U32)g_live_audio_lane_start_failure_count);
    live_append_literal(payload, &cursor, ",\\\"lane_start_error\\\":");
    live_append_u32(payload, &cursor, g_live_audio_lane_start_error);
    live_append_literal(payload, &cursor, ",\\\"lane_entries\\\":");
    live_append_u32(payload, &cursor, (U32)g_live_audio_lane_entry_count);
    live_append_literal(payload, &cursor, ",\\\"active_mixes\\\":");
    live_append_u32(payload, &cursor, (U32)g_live_audio_lane_active_mix_count);
    live_append_literal(payload, &cursor, ",\\\"effect_images\\\":");
    live_append_u32(payload, &cursor, (U32)g_live_audio_effect_image_count);
    live_append_literal(payload, &cursor, ",\\\"effect_image_failures\\\":");
    live_append_u32(
        payload, &cursor, (U32)g_live_audio_effect_image_failure_count);
    live_append_literal(payload, &cursor, ",\\\"decoded_buffers\\\":");
    live_append_u32(payload, &cursor, (U32)g_live_audio_decoded_buffer_count);
    live_append_literal(payload, &cursor, ",\\\"decoded_packets\\\":");
    live_append_u32(payload, &cursor, (U32)g_live_audio_decoded_packet_count);
    live_append_literal(payload, &cursor, ",\\\"packet_completions\\\":");
    live_append_u32(payload, &cursor, (U32)g_live_audio_packet_completion_count);
    live_append_literal(payload, &cursor, ",\\\"packet_flushes\\\":");
    live_append_u32(payload, &cursor, (U32)g_live_audio_packet_flush_count);
    live_append_literal(payload, &cursor, ",\\\"decode_failures\\\":");
    live_append_u32(payload, &cursor, (U32)g_live_audio_decode_failure_count);
    live_append_literal(payload, &cursor, ",\\\"published_buffers\\\":");
    live_append_u32(payload, &cursor, (U32)g_live_audio_published_buffer_count);
    live_append_literal(payload, &cursor, ",\\\"published_bytes\\\":");
    live_append_u32(payload, &cursor, (U32)g_live_audio_published_byte_count);
    live_append_literal(payload, &cursor, ",\\\"dropped_buffers\\\":");
    live_append_u32(payload, &cursor, (U32)g_live_audio_dropped_buffer_count);
    live_append_literal(payload, &cursor, "}}}}\\n");
    file = CreateFileA(g_live_summary_path, GENERIC_WRITE, 0, 0,
                       CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, 0);
    if (file != (HANDLE)-1) {{
        WriteFile(file, payload, cursor, &written, 0);
        CloseHandle(file);
    }}
}}
"""
    if main_seam not in source:
        raise Ia32BackendError("normal-live worker lost its main-entry seam")
    source = source.replace(main_seam, helpers + main_seam, 1)

    workload_seam = f"""    if (service->runtime_kind == {WORKLOAD_SERVICE_SYSTEM_TIME}u) {{
"""
    workload_block = f"""    if (
        service->runtime_kind == {WORKLOAD_SERVICE_XGETDEVICES}u ||
        service->runtime_kind == {WORKLOAD_SERVICE_XINPUT_OPEN}u ||
        service->runtime_kind == {WORKLOAD_SERVICE_XINPUT_CAPABILITIES}u ||
        service->runtime_kind == {WORKLOAD_SERVICE_XINPUT_STATE}u ||
        service->runtime_kind == {WORKLOAD_SERVICE_XINPUT_CLOSE}u ||
        service->runtime_kind == {WORKLOAD_SERVICE_XINPUT_SET_STATE}u)
        return live_dispatch_input_service(service, arguments);
    if (service->runtime_kind == {WORKLOAD_SERVICE_AUDIO}u)
        return live_dispatch_audio_service(service, this_pointer, arguments);
    if (service->runtime_kind == {WORKLOAD_SERVICE_COLD_CALLBACK}u) {{
        if (service->runtime_value == 1u) {{
            U32 handle;
            LiveWorker* worker;
            if (g_live_worker_count >= 64u) return 0xC000009Au;
            handle = g_live_next_worker_handle;
            g_live_next_worker_handle += 4u;
            if (arguments[0] != 0u) *(U32*)arguments[0] = handle;
            if (arguments[4] != 0u) *(U32*)arguments[4] = handle;
            worker = &g_live_workers[g_live_worker_count++];
            worker->handle = handle;
            worker->start_address = arguments[9];
            worker->start_context1 = arguments[5];
            worker->start_context2 = arguments[6];
            worker->suspended = arguments[7] != 0u;
            worker->kernel_stack_size = arguments[2];
            worker->tls_data_size = arguments[3];
            return 0u;
        }}
        if (service->runtime_value == 5u) {{
            WorkloadSemaphore* semaphore;
            U32 handle;
            if (g_workload_semaphore_count >= 1024u) return 0xC000009Au;
            handle = g_live_next_worker_handle;
            g_live_next_worker_handle += 4u;
            semaphore = &g_workload_semaphores[g_workload_semaphore_count++];
            semaphore->handle = handle;
            semaphore->count = arguments[2];
            semaphore->limit = arguments[3];
            if (semaphore->count > semaphore->limit)
                semaphore->count = semaphore->limit;
            if (arguments[0] != 0u) *(U32*)arguments[0] = handle;
            return 0u;
        }}
        if (service->runtime_value == 6u) {{
            U32 index;
            for (index = 0u; index < g_live_worker_count; ++index)
                if (g_live_workers[index].handle == arguments[0]) return 0u;
            for (index = 0u; index < g_workload_semaphore_count; ++index)
                if (g_workload_semaphores[index].handle == arguments[0]) return 0u;
            return 0xC0000008u;
        }}
        if (service->runtime_value == 7u) return 0u;
    }}
    if (service->runtime_kind == {WORKLOAD_SERVICE_SYSTEM_TIME}u) {{
"""
    if workload_seam not in source:
        raise Ia32BackendError("normal-live worker lost its workload-service seam")
    source = source.replace(workload_seam, workload_block, 1)

    bootstrap_close_seam = """            return 0u;
        }
        if (service->runtime_value == 10u) {
"""
    bootstrap_close_block = """            if (g_normal_live)
                return live_dispatch_bootstrap_service(service, arguments, 0u);
            return 0u;
        }
        if (service->runtime_value == 10u) {
"""
    if bootstrap_close_seam not in source:
        raise Ia32BackendError("normal-live worker lost its bootstrap-close seam")
    source = source.replace(bootstrap_close_seam, bootstrap_close_block, 1)

    bootstrap_fallback_seam = f"""        if (service->runtime_value == 19u) {{
            g_workload_av_saved_data_address = arguments[0];
            return 0u;
        }}
    }}
    if (service->runtime_kind == {WORKLOAD_SERVICE_TITLE}u) {{
"""
    bootstrap_fallback_block = f"""        if (service->runtime_value == 19u) {{
            g_workload_av_saved_data_address = arguments[0];
            return 0u;
        }}
        if (g_normal_live)
            return live_dispatch_bootstrap_service(
                service, arguments, service->return_value);
    }}
    if (service->runtime_kind == {WORKLOAD_SERVICE_TITLE}u) {{
"""
    if bootstrap_fallback_seam not in source:
        raise Ia32BackendError("normal-live worker lost its bootstrap fallback seam")
    source = source.replace(bootstrap_fallback_seam, bootstrap_fallback_block, 1)

    recovered_fault_seam = """        g_guest_faulted = 1;
        return -1;
"""
    recovered_fault_block = """        if (g_normal_live) live_write_fault_summary(
            pointers->record, pointers->context);
        g_guest_faulted = 1;
        return -1;
"""
    if recovered_fault_seam not in source:
        raise Ia32BackendError("normal-live worker lost its recovered-fault seam")
    source = source.replace(recovered_fault_seam, recovered_fault_block, 1)

    mapping_seam = """    mapping = OpenFileMappingA(FILE_MAP_ALL_ACCESS, 0, mapping_name());
    if (!mapping) ExitProcess(10);
    exchange = 0;
    for (index = 0;
         index < sizeof(kExchangeViewAddresses) / sizeof(kExchangeViewAddresses[0]);
         ++index) {
        exchange = (Exchange*)MapViewOfFileEx(
            mapping, FILE_MAP_ALL_ACCESS, 0, 0, 0,
            (void*)kExchangeViewAddresses[index]);
        if (exchange) break;
    }
    if (!exchange) ExitProcess(11);
"""
    mapping_block = """    g_normal_live = live_has_argument("--normal-live");
    if (g_normal_live) {
        g_live_startup_phase = 1u;
        SetUnhandledExceptionFilter(live_exception_filter);
        live_argument("--summary-output", g_live_summary_path,
                      sizeof(g_live_summary_path));
        live_argument("--run-id", g_live_run_id, sizeof(g_live_run_id));
        g_live_startup_phase = 2u;
    }
    mapping = 0;
    request_event = 0;
    if (g_normal_live) {
        g_live_startup_phase = 3u;
        exchange = live_load_boot_exchange();
        g_live_startup_phase = 4u;
    } else {
        mapping = OpenFileMappingA(FILE_MAP_ALL_ACCESS, 0, mapping_name());
        if (!mapping) ExitProcess(10);
        exchange = 0;
        for (index = 0;
             index < sizeof(kExchangeViewAddresses) / sizeof(kExchangeViewAddresses[0]);
             ++index) {
            exchange = (Exchange*)MapViewOfFileEx(
                mapping, FILE_MAP_ALL_ACCESS, 0, 0, 0,
                (void*)kExchangeViewAddresses[index]);
            if (exchange) break;
        }
    }
    if (!exchange) ExitProcess(11);
"""
    if mapping_seam not in source:
        raise Ia32BackendError("normal-live worker lost its exchange-mapping seam")
    source = source.replace(mapping_seam, mapping_block, 1)

    event_seam = """    event_name(request_name, "-request");
    event_name(response_name, "-response");
    event_name(broker_request_name, "-broker-request");
    event_name(broker_response_name, "-broker-response");
    request_event = OpenEventA(SYNCHRONIZE, 0, request_name);
    g_response_event = OpenEventA(EVENT_MODIFY_STATE, 0, response_name);
    g_broker_request_event = OpenEventA(EVENT_MODIFY_STATE, 0, broker_request_name);
    g_broker_response_event = OpenEventA(SYNCHRONIZE, 0, broker_response_name);
    if (!request_event || !g_response_event ||
        !g_broker_request_event || !g_broker_response_event) ExitProcess(20);
"""
    event_block = """    if (g_normal_live) {
        g_response_event = 0;
        g_broker_request_event = 0;
        g_broker_response_event = 0;
        if (!live_open_transports()) ExitProcess(20);
        g_live_control_thread = CreateThread(
            0, 0u, live_control_watcher, 0, 0u, 0);
        if (!g_live_control_thread) ExitProcess(20);
        if (!live_start_audio_lane()) ExitProcess(20);
    } else {
        event_name(request_name, "-request");
        event_name(response_name, "-response");
        event_name(broker_request_name, "-broker-request");
        event_name(broker_response_name, "-broker-response");
        request_event = OpenEventA(SYNCHRONIZE, 0, request_name);
        g_response_event = OpenEventA(EVENT_MODIFY_STATE, 0, response_name);
        g_broker_request_event = OpenEventA(EVENT_MODIFY_STATE, 0, broker_request_name);
        g_broker_response_event = OpenEventA(SYNCHRONIZE, 0, broker_response_name);
        if (!request_event || !g_response_event ||
            !g_broker_request_event || !g_broker_response_event) ExitProcess(20);
    }
"""
    if event_seam not in source:
        raise Ia32BackendError("normal-live worker lost its event-open seam")
    source = source.replace(event_seam, event_block, 1)

    thunk_seam = """    for (index = 0;
         index < sizeof(kHostServiceThunks) / sizeof(kHostServiceThunks[0]);
         ++index)
        copy_bytes((void*)kHostServiceThunks[index].address,
                   kHostServiceThunks[index].code,
                   kHostServiceThunks[index].size);
"""
    thunk_block = (
        """    for (index = 0; index < kNormalLiveHostDataPageCount; ++index)
        copy_bytes((void*)kNormalLiveHostDataPages[index].address,
                   kNormalLiveHostDataPages[index].payload, 4096u);
"""
        + thunk_seam
        + f"""    copy_bytes(
        (void*)0x{NORMAL_LIVE_BOOT_RETURN_THUNK_ADDRESS:08X}u,
        kNormalLiveBootReturnThunk, sizeof(kNormalLiveBootReturnThunk));
    copy_bytes(
        (void*)0x{NORMAL_LIVE_RESUME_THUNK_ADDRESS:08X}u,
        kNormalLiveResumeThunk, sizeof(kNormalLiveResumeThunk));
    copy_bytes(
        (void*)0x{NORMAL_LIVE_VBLANK_START_THUNK_ADDRESS:08X}u,
        kNormalLiveVblankStartThunk, sizeof(kNormalLiveVblankStartThunk));
    copy_bytes(
        (void*)0x{NORMAL_LIVE_VBLANK_EXIT_THUNK_ADDRESS:08X}u,
        kNormalLiveVblankExitThunk, sizeof(kNormalLiveVblankExitThunk));
    copy_bytes(
        (void*)0x{IA32_VBLANK_RETURN_SENTINEL:08X}u,
        kNormalLiveVblankReturnPatch, sizeof(kNormalLiveVblankReturnPatch));
"""
    )
    if thunk_seam not in source:
        raise Ia32BackendError("normal-live worker lost its thunk-install seam")
    source = source.replace(thunk_seam, thunk_block, 1)

    ready_seam = """    exchange->error = 0u;
    exchange->reserved = 1u;
    exchange->status = 3u;
    if (!SetEvent(g_response_event)) ExitProcess(22);
    for (;;) {
        if (WaitForSingleObject(request_event, INFINITE) != WAIT_OBJECT_0)
            protocol_failure(23u, exchange->eip);
        command = exchange->reserved;
"""
    ready_block = """    exchange->error = 0u;
    exchange->reserved = 1u;
    exchange->status = 3u;
    if (!g_normal_live && !SetEvent(g_response_event)) ExitProcess(22);
    for (;;) {
        if (g_normal_live) {
            if (live_read_u32(g_live_control, 32u) != 0u) {
                Sleep(1u);
                continue;
            }
            exchange->error = 1u;
            exchange->reserved = g_live_flip_count ? 2u : 1u;
            exchange->status = 0u;
        } else if (WaitForSingleObject(request_event, INFINITE) != WAIT_OBJECT_0) {
            protocol_failure(23u, exchange->eip);
        }
        command = exchange->reserved;
"""
    if ready_seam not in source:
        raise Ia32BackendError("normal-live worker lost its ready/dispatch seam")
    source = source.replace(ready_seam, ready_block, 1)

    overflow_seam = f"""    if (trace_header[0] >= {HOST_SERVICE_TRACE_CAPACITY}u) {{
        trace_header[1] = 1u;
        protocol_failure(42u, service_index);
    }}
"""
    overflow_block = f"""    if (trace_header[0] >= {HOST_SERVICE_TRACE_CAPACITY}u) {{
        trace_header[1] += 1u;
        if (!g_normal_live) protocol_failure(42u, service_index);
    }}
"""
    if overflow_seam not in source:
        raise Ia32BackendError("normal-live worker lost its service-audit overflow seam")
    source = source.replace(overflow_seam, overflow_block, 1)
    record_seam = "    record = &records[trace_header[0]];\n"
    if record_seam not in source:
        raise Ia32BackendError("normal-live worker lost its service-audit record seam")
    source = source.replace(
        record_seam,
        f"    record = &records[trace_header[0] % {HOST_SERVICE_TRACE_CAPACITY}u];\n",
        1,
    )

    source = source.replace(
        "            apply_host_publications();\n",
        "            if (!g_normal_live) apply_host_publications();\n",
    )
    source = source.replace(
        "        apply_host_publications();\n",
        "        if (!g_normal_live) apply_host_publications();\n",
    )
    source = source.replace(
        "            publish_pages_and_dirty();\n",
        "            if (!g_normal_live) publish_pages_and_dirty();\n",
    )
    source = source.replace(
        "        publish_pages_and_dirty();\n",
        "        if (!g_normal_live) publish_pages_and_dirty();\n",
    )

    fault_seam = """        if (g_guest_faulted) {
            exchange->elapsed_ticks = finished - started;
            if (!g_normal_live) publish_pages_and_dirty();
            if (!SetEvent(g_response_event)) ExitProcess(26);
            continue;
        }
"""
    fault_block = """        if (g_guest_faulted) {
            exchange->elapsed_ticks = finished - started;
            if (!g_normal_live) publish_pages_and_dirty();
            if (g_normal_live) ExitProcess(97);
            if (!SetEvent(g_response_event)) ExitProcess(26);
            continue;
        }
"""
    if fault_seam not in source:
        raise Ia32BackendError("normal-live worker lost its contained-fault seam")
    source = source.replace(fault_seam, fault_block, 1)

    completion_seam = f"""        exchange->eip = 0x{stop_instruction.address:08X}u;
        copy_bytes(exchange->fxsave,
                   (void*)0x{SCRATCH_ADDRESS + SCRATCH_FXSAVE_OFFSET:08X}u, 512u);
        if (!g_normal_live) publish_pages_and_dirty();
        exchange->error = 0u;
        exchange->reserved = command;
        exchange->status = 2u;
        if (!SetEvent(g_response_event)) ExitProcess(26);
"""
    completion_block = f"""        exchange->eip = 0x{stop_instruction.address:08X}u;
        copy_bytes(exchange->fxsave,
                   (void*)0x{SCRATCH_ADDRESS + SCRATCH_FXSAVE_OFFSET:08X}u, 512u);
        if (!g_normal_live) publish_pages_and_dirty();
        exchange->error = 0u;
        exchange->reserved = command;
        exchange->status = 2u;
        if (g_normal_live) {{
            if (g_live_bootstrap_phase) {{
                g_live_bootstrap_phase = 0u;
                if (!live_start_primary_worker(exchange)) ExitProcess(71);
                continue;
            }}
            if (!live_publish_frame()) {{
                if (live_read_u32(g_live_control, 32u) != 0u) {{
                    Sleep(1u);
                    continue;
                }}
                live_write_summary();
                ExitProcess(70);
            }}
            exchange->eip = 0x{NORMAL_LIVE_RESUME_THUNK_ADDRESS:08X}u;
            continue;
        }}
        if (!SetEvent(g_response_event)) ExitProcess(26);
"""
    if completion_seam not in source:
        raise Ia32BackendError("normal-live worker lost its successful-completion seam")
    source = source.replace(completion_seam, completion_block, 1)
    return _phase7_normal_live_optimization_source(source)


def _phase7_normal_live_optimization_source(source: str) -> str:
    """Install bounded hot-path caches after all Phase-7 source rewrites."""

    seams = (
        (
            """static WorkloadAllocation g_workload_allocations[1024];
""",
            """static WorkloadSemaphore* g_workload_semaphore_cache[64];
static WorkloadAllocation g_workload_allocations[1024];
""",
            "semaphore cache global",
        ),
        (
            """static HANDLE g_workload_contiguous_mapping;
""",
            """static HANDLE g_workload_contiguous_mapping;
static WorkloadAllocation* g_workload_last_allocation;
static U32 g_workload_allocation_page_tags[256];
static WorkloadAllocation* g_workload_allocation_page_cache[256];
""",
            "allocation page cache globals",
        ),
        (
            """static U32 workload_align_up(U32 value, U32 alignment) {
""",
            """static WorkloadSemaphore* workload_find_semaphore(U32 handle) {
    U32 index, slot = (handle >> 2u) & 63u;
    WorkloadSemaphore* cached = g_workload_semaphore_cache[slot];
    if (cached && cached->handle == handle) return cached;
    for (index = 0u; index < g_workload_semaphore_count; ++index) {
        WorkloadSemaphore* semaphore = &g_workload_semaphores[index];
        if (semaphore->handle == handle) {
            g_workload_semaphore_cache[slot] = semaphore;
            return semaphore;
        }
    }
    return 0;
}
static U32 workload_align_up(U32 value, U32 alignment) {
""",
            "semaphore lookup helper",
        ),
        (
            """static WorkloadAllocation* workload_find_allocation(U32 address) {
    U32 index;
    for (index = 0u; index < g_workload_allocation_count; ++index) {
        WorkloadAllocation* allocation = &g_workload_allocations[index];
        if (workload_allocation_offset(allocation, address, 0)) return allocation;
    }
    return 0;
}
""",
            """static WorkloadAllocation* workload_find_allocation(U32 address) {
    U32 index, page = address >> 12u, slot = page & 255u;
    WorkloadAllocation* cached = g_workload_allocation_page_cache[slot];
    if (g_workload_allocation_page_tags[slot] == page &&
        workload_allocation_offset(cached, address, 0)) {
        g_workload_last_allocation = cached;
        return cached;
    }
    if (workload_allocation_offset(g_workload_last_allocation, address, 0))
        return g_workload_last_allocation;
    for (index = 0u; index < g_workload_allocation_count; ++index) {
        WorkloadAllocation* allocation = &g_workload_allocations[index];
        if (workload_allocation_offset(allocation, address, 0)) {
            g_workload_last_allocation = allocation;
            g_workload_allocation_page_tags[slot] = page;
            g_workload_allocation_page_cache[slot] = allocation;
            return allocation;
        }
    }
    return 0;
}
""",
            "allocation lookup cache",
        ),
        (
            """    allocation->active = 1u;
    allocation->contiguous = contiguous;
""",
            """    allocation->active = 1u;
    allocation->contiguous = contiguous;
    g_workload_last_allocation = allocation;
    g_workload_allocation_page_tags[(address >> 12u) & 255u] = address >> 12u;
    g_workload_allocation_page_cache[(address >> 12u) & 255u] = allocation;
""",
            "allocation page cache population",
        ),
        (
            """        WorkloadSemaphore* semaphore = 0;
        if (service->runtime_value == 3u) return 0u;
        for (index = 0u; index < g_workload_semaphore_count; ++index) {
            if (g_workload_semaphores[index].handle == arguments[0]) {
                semaphore = &g_workload_semaphores[index];
                break;
            }
        }
""",
            """        WorkloadSemaphore* semaphore;
        if (service->runtime_value == 3u) return 0u;
        semaphore = workload_find_semaphore(arguments[0]);
""",
            "semaphore dispatch lookup",
        ),
        (
            """            semaphore->handle = handle;
            semaphore->count = arguments[2];
""",
            """            semaphore->handle = handle;
            g_workload_semaphore_cache[(handle >> 2u) & 63u] = semaphore;
            semaphore->count = arguments[2];
""",
            "semaphore cache population",
        ),
        (
            """            for (index = 0u; index < g_workload_semaphore_count; ++index)
                if (g_workload_semaphores[index].handle == arguments[0]) return 0u;
""",
            """            if (workload_find_semaphore(arguments[0])) return 0u;
""",
            "semaphore close validation",
        ),
        (
            """        if (service->runtime_value == 9u) {
            for (index = 0u; index < g_workload_semaphore_count; ++index)
                if (g_workload_semaphores[index].handle == arguments[0]) {
                    g_workload_semaphores[index].handle = 0u;
                    g_workload_semaphores[index].count = 0u;
                    g_workload_semaphores[index].limit = 0u;
            }
            if (g_normal_live)
                return live_dispatch_bootstrap_service(service, arguments, 0u);
            return 0u;
        }
""",
            """        if (service->runtime_value == 9u) {
            WorkloadSemaphore* semaphore = workload_find_semaphore(arguments[0]);
            if (semaphore) {
                U32 slot = (arguments[0] >> 2u) & 63u;
                if (g_workload_semaphore_cache[slot] == semaphore)
                    g_workload_semaphore_cache[slot] = 0;
                semaphore->handle = 0u;
                semaphore->count = 0u;
                semaphore->limit = 0u;
            }
            if (g_normal_live)
                return live_dispatch_bootstrap_service(service, arguments, 0u);
            return 0u;
        }
""",
            "semaphore cache invalidation",
        ),
    )
    for seam, replacement, label in seams:
        if source.count(seam) != 1:
            raise Ia32BackendError(
                f"Phase-7 worker lost its {label} optimization seam"
            )
        source = source.replace(seam, replacement, 1)
    return source


def _phase4_broker_c_source(
    *,
    page_count: int,
    host_service_thunks: Sequence[_HostServiceThunk],
) -> str:
    broker_offset, _trace_offset = _phase4_service_offsets(page_count)
    responses = ",".join(
        "{"
        + f"0x{service.value:08X}u,0x{service.memory_write_value:08X}u,"
        + f"{1 if service.execution == 'native64-broker' else 0}u"
        + "}"
        for service in host_service_thunks
    )
    response_count = "sizeof(kResponses) / sizeof(kResponses[0])"
    if not responses:
        responses = "{0u,0u,0u}"
        response_count = "0u"
    return f"""/* Deterministic generated 64-bit native service broker. */
typedef unsigned char U8;
typedef unsigned long U32;
typedef int BOOL;
typedef void* HANDLE;
typedef struct BrokerControl {{
    U32 magic, version;
    volatile U32 status;
    U32 sequence, service_index, argument_count;
    U32 arguments[4];
    U32 return_value, memory_value, error, reserved[3];
}} BrokerControl;
typedef struct BrokerResponse {{ U32 return_value, memory_value, enabled; }} BrokerResponse;
__declspec(dllimport) char* __stdcall GetCommandLineA(void);
__declspec(dllimport) HANDLE __stdcall OpenFileMappingA(U32, BOOL, const char*);
__declspec(dllimport) void* __stdcall MapViewOfFile(HANDLE, U32, U32, U32, unsigned long long);
__declspec(dllimport) HANDLE __stdcall OpenEventA(U32, BOOL, const char*);
__declspec(dllimport) BOOL __stdcall SetEvent(HANDLE);
__declspec(dllimport) U32 __stdcall WaitForSingleObject(HANDLE, U32);
__declspec(dllimport) BOOL __stdcall CloseHandle(HANDLE);
__declspec(dllimport) __declspec(noreturn) void __stdcall ExitProcess(U32);
static const BrokerResponse kResponses[] = {{{responses}}};
static const char* mapping_name(void) {{
    const char* value = GetCommandLineA();
    static char name[160];
    U32 index = 0;
    if (*value == '\"') {{ ++value; while (*value && *value != '\"') ++value; if (*value) ++value; }}
    else while (*value && *value != ' ' && *value != '\\t') ++value;
    while (*value == ' ' || *value == '\\t') ++value;
    while (*value && *value != ' ' && *value != '\\t' && index + 1 < sizeof(name))
        name[index++] = *value++;
    name[index] = 0;
    return name;
}}
static void event_name(char* target, const char* suffix) {{
    const char* source = mapping_name();
    U32 index = 0;
    while (*source && index < 190u) target[index++] = *source++;
    while (*suffix && index < 190u) target[index++] = *suffix++;
    target[index] = 0;
}}
void __cdecl mainCRTStartup(void) {{
    const U32 FILE_MAP_ALL_ACCESS = 0x000F001Fu;
    const U32 EVENT_MODIFY_STATE = 0x00000002u;
    const U32 SYNCHRONIZE = 0x00100000u;
    HANDLE mapping;
    HANDLE request_event;
    HANDLE response_event;
    U8* view;
    BrokerControl* broker;
    char request_name[192];
    char response_name[192];
    mapping = OpenFileMappingA(FILE_MAP_ALL_ACCESS, 0, mapping_name());
    if (!mapping) ExitProcess(10u);
    view = (U8*)MapViewOfFile(mapping, FILE_MAP_ALL_ACCESS, 0, 0, 0);
    if (!view) ExitProcess(11u);
    broker = (BrokerControl*)(view + {broker_offset}u);
    event_name(request_name, "-broker-request");
    event_name(response_name, "-broker-response");
    request_event = OpenEventA(SYNCHRONIZE, 0, request_name);
    response_event = OpenEventA(EVENT_MODIFY_STATE, 0, response_name);
    if (!request_event || !response_event) ExitProcess(12u);
    if (broker->magic != 0x{HOST_SERVICE_BROKER_MAGIC:08X}u ||
        broker->version != {HOST_SERVICE_BROKER_VERSION}u) ExitProcess(13u);
    broker->status = {HOST_SERVICE_BROKER_STATUS_READY}u;
    if (!SetEvent(response_event)) ExitProcess(14u);
    for (;;) {{
        if (WaitForSingleObject(request_event, 0xFFFFFFFFu) != 0u) ExitProcess(15u);
        if (broker->status == {HOST_SERVICE_BROKER_STATUS_STOP}u) {{
            broker->status = {HOST_SERVICE_BROKER_STATUS_RESPONSE}u;
            SetEvent(response_event);
            CloseHandle(request_event);
            CloseHandle(response_event);
            CloseHandle(mapping);
            ExitProcess(0u);
        }}
        if (broker->status != {HOST_SERVICE_BROKER_STATUS_REQUEST}u ||
            broker->service_index >= {response_count} ||
            !kResponses[broker->service_index].enabled) {{
            broker->error = 1u;
            broker->status = {HOST_SERVICE_BROKER_STATUS_ERROR}u;
            SetEvent(response_event);
            continue;
        }}
        broker->return_value = kResponses[broker->service_index].return_value;
        broker->memory_value = kResponses[broker->service_index].memory_value;
        broker->error = 0u;
        broker->status = {HOST_SERVICE_BROKER_STATUS_RESPONSE}u;
        if (!SetEvent(response_event)) ExitProcess(16u);
    }}
}}
"""


def _compiler_version(compiler: Path) -> str:
    completed = subprocess.run(
        [str(compiler), "--version"],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise Ia32BackendError(f"could not query compiler: {compiler}")
    first_line = (completed.stdout + completed.stderr).splitlines()
    return first_line[0] if first_line else "unknown"


def _resolve_compiler(compiler: Path | None) -> Path:
    resolved = resolve_clang_cl(compiler)
    if resolved is None:
        raise Ia32BackendError("clang-cl is required to build the IA-32 prototype")
    locked_version = str(load_toolchain_lock()["compiler"]["version"])
    observed = _compiler_version(resolved)
    if f"clang version {locked_version}" not in observed:
        raise Ia32BackendError(
            f"clang-cl version does not match locked {locked_version}: {observed}"
        )
    return resolved


def _resolve_linker(compiler: Path) -> Path:
    linker = compiler.with_name("lld-link.exe")
    if not linker.is_file():
        discovered = shutil.which("lld-link")
        linker = Path(discovered).resolve() if discovered else linker
    if not linker.is_file():
        raise Ia32BackendError("lld-link is required to build the IA-32 prototype")
    return linker


def _resolve_kernel32_library(
    explicit: Path | None,
    *,
    architecture: str = "x86",
) -> Path:
    if explicit is not None:
        library = explicit.resolve()
        if not library.is_file():
            raise Ia32BackendError(f"x86 kernel32 import library is missing: {library}")
        return library
    roots = [Path("C:/Program Files (x86)/Windows Kits/10/Lib")]
    sdk_root = os.environ.get("WindowsSdkDir")
    if sdk_root:
        roots.insert(0, Path(sdk_root) / "Lib")
    candidates: list[Path] = []
    for root in roots:
        if root.is_dir():
            candidates.extend(root.glob(f"*/um/{architecture}/kernel32.lib"))
            candidates.extend(root.glob(f"*/um/{architecture}/kernel32.Lib"))
    unique = sorted({candidate.resolve() for candidate in candidates})
    if not unique:
        raise Ia32BackendError(
            f"could not locate the Windows SDK {architecture} kernel32 import library"
        )
    return unique[-1]


def _artifact_inputs(
    *,
    capsule: ReplayCapsule,
    stop_eip: int,
    source: str,
    guest_source: str,
    compiler: Path,
    assembler: Path,
    linker: Path,
    kernel32_library: Path,
    persistent_worker: bool = False,
    decoded_store_plan: Ia32DecodedStorePlan | None = None,
    architecture_plan: _Ia32ArchitecturePlan | None = None,
    host_abi: bool = False,
    scheduler_plan: _Ia32SchedulerPlan | None = None,
    coverage_growth_plan: Ia32CoverageGrowthPlan | None = None,
    normal_launcher_cutover: bool = False,
    normal_live_launch: bool = False,
    normal_live_boot_image: bytes = b"",
    broker_source: str = "",
    broker_kernel32_library: Path | None = None,
) -> dict[str, Any]:
    inputs = {
        "format": ARTIFACT_FORMAT,
        "version": ARTIFACT_VERSION,
        "generator_version": (
            NORMAL_LIVE_GENERATOR_VERSION
            if normal_live_launch
            else CUTOVER_GENERATOR_VERSION
            if normal_launcher_cutover
            else COVERAGE_GROWTH_GENERATOR_VERSION
            if coverage_growth_plan is not None
            else RESIDENT_SCHEDULER_GENERATOR_VERSION
            if scheduler_plan is not None
            else HOST_ABI_GENERATOR_VERSION
            if host_abi
            else ARCHITECTURE_GENERATOR_VERSION
            if architecture_plan is not None
            else DECODED_STORE_GENERATOR_VERSION
            if decoded_store_plan is not None
            else PERSISTENT_GENERATOR_VERSION
            if persistent_worker
            else GENERATOR_VERSION
        ),
        "execution_contract_id": ia32_execution_contract_id(),
        "capsule_id": capsule.capsule_id,
        "entry_eip": capsule.state.eip,
        "stop_eip": stop_eip,
        "target": TARGET_TRIPLE,
        "source_sha256": _sha256(source.encode("utf-8")),
        "guest_source_sha256": _sha256(guest_source.encode("utf-8")),
        "compiler": {
            "path": str(compiler),
            "sha256": sha256_file(compiler),
            "version": _compiler_version(compiler),
        },
        "assembler": {"path": str(assembler), "sha256": sha256_file(assembler)},
        "linker": {"path": str(linker), "sha256": sha256_file(linker)},
        "kernel32_library": {
            "path": str(kernel32_library),
            "sha256": sha256_file(kernel32_library),
        },
    }
    if persistent_worker:
        inputs["persistent_worker_contract_id"] = ia32_persistent_worker_contract_id()
    if decoded_store_plan is not None:
        inputs["decoded_store_artifact"] = decoded_store_plan.inputs
    if architecture_plan is not None:
        inputs["architecture_contract_id"] = ia32_architecture_contract_id()
        inputs["architecture_map_id"] = _sha256(_canonical_json(architecture_plan.architecture_map))
    if host_abi:
        if broker_kernel32_library is None:
            raise Ia32BackendError("Phase-4 host ABI inputs omit the broker import library")
        inputs["host_abi_contract_id"] = ia32_host_abi_contract_id()
        inputs["service_broker"] = {
            "target": "x86_64-pc-windows-msvc",
            "source_sha256": _sha256(broker_source.encode("utf-8")),
            "kernel32_library": {
                "path": str(broker_kernel32_library),
                "sha256": sha256_file(broker_kernel32_library),
            },
        }
    if scheduler_plan is not None:
        inputs["resident_scheduler_contract_id"] = ia32_resident_scheduler_contract_id()
        inputs["resident_scheduler_map_id"] = _sha256(_canonical_json(scheduler_plan.scheduler_map))
    if coverage_growth_plan is not None:
        inputs["coverage_growth_contract_id"] = ia32_coverage_growth_contract_id()
        inputs["coverage_growth_map_id"] = _sha256(
            _canonical_json(coverage_growth_plan.coverage_growth_map)
        )
    if normal_launcher_cutover:
        inputs["launcher_cutover_contract_id"] = ia32_launcher_cutover_contract_id()
        if coverage_growth_plan is None:
            raise Ia32BackendError("Phase-7 cutover inputs require a Phase-6 coverage plan")
        inputs["launcher_cutover_map_id"] = _sha256(
            _canonical_json(_launcher_cutover_map(coverage_growth_plan))
        )
    if normal_live_launch:
        if not normal_live_boot_image:
            raise Ia32BackendError("normal-live inputs omit the XBE-entry boot image")
        inputs.update(
            {
                "normal_live_launch_contract_id": (ia32_normal_live_launch_contract_id()),
                "normal_live_boot_image": {
                    "format": NORMAL_LIVE_BOOT_IMAGE_FORMAT,
                    "version": NORMAL_LIVE_BOOT_IMAGE_VERSION,
                    "bytes": len(normal_live_boot_image),
                    "sha256": _sha256(normal_live_boot_image),
                },
            }
        )
    return inputs


def _toolchain_identity(inputs: Mapping[str, Any]) -> dict[str, Any]:
    identity: dict[str, Any] = {"target": inputs.get("target")}
    for name in ("compiler", "assembler", "linker", "kernel32_library"):
        record = inputs.get(name)
        if not isinstance(record, dict):
            raise Ia32BackendError(f"IA-32 artifact inputs omit {name} identity")
        identity[name] = {key: record[key] for key in ("sha256", "version") if key in record}
    identity["toolchain_id"] = _sha256(_canonical_json(identity))
    return identity


def validate_ia32_artifact_manifest(manifest: Mapping[str, Any]) -> None:
    """Validate the content identity and frozen contract of an IA-32 artifact."""

    if manifest.get("format") != ARTIFACT_FORMAT or manifest.get("version") != ARTIFACT_VERSION:
        raise Ia32BackendError("unsupported IA-32 artifact manifest")
    inputs = manifest.get("inputs")
    if not isinstance(inputs, dict):
        raise Ia32BackendError("IA-32 artifact manifest omits its inputs")
    if inputs.get("format") != ARTIFACT_FORMAT or inputs.get("version") != ARTIFACT_VERSION:
        raise Ia32BackendError("IA-32 artifact inputs use an unsupported format")
    if inputs.get("target") != TARGET_TRIPLE:
        raise Ia32BackendError("IA-32 artifact target does not match the backend contract")
    expected_artifact_id = _sha256(_canonical_json(inputs))
    if manifest.get("artifact_id") != expected_artifact_id:
        raise Ia32BackendError("IA-32 artifact content identity does not match its inputs")
    generator_version = int(inputs.get("generator_version", 0))
    if generator_version >= 13:
        expected_contract = ia32_execution_contract()
        expected_contract_id = ia32_execution_contract_id()
        if (
            inputs.get("execution_contract_id") != expected_contract_id
            or manifest.get("execution_contract_id") != expected_contract_id
            or manifest.get("execution_contract") != expected_contract
        ):
            raise Ia32BackendError("IA-32 artifact execution contract does not match Phase 0")
        if manifest.get("toolchain") != _toolchain_identity(inputs):
            raise Ia32BackendError("IA-32 artifact toolchain identity is inconsistent")
    if generator_version >= PERSISTENT_GENERATOR_VERSION:
        expected_worker_contract = ia32_persistent_worker_contract()
        expected_worker_contract_id = ia32_persistent_worker_contract_id()
        if (
            inputs.get("persistent_worker_contract_id") != expected_worker_contract_id
            or manifest.get("persistent_worker_contract_id") != expected_worker_contract_id
            or manifest.get("persistent_worker_contract") != expected_worker_contract
            or manifest.get("persistent_worker") is not True
        ):
            raise Ia32BackendError("IA-32 artifact resident-worker contract does not match Phase 1")
    if manifest.get("decoded_store_artifact") is True:
        expected_store_contract = ia32_decoded_store_artifact_contract()
        expected_store_contract_id = ia32_decoded_store_artifact_contract_id()
        decoded_store_inputs = inputs.get("decoded_store_artifact")
        if (
            not isinstance(decoded_store_inputs, dict)
            or decoded_store_inputs.get("contract_id") != expected_store_contract_id
            or manifest.get("decoded_store_artifact_contract_id") != expected_store_contract_id
            or manifest.get("decoded_store_artifact_contract") != expected_store_contract
            or manifest.get("decoded_store_artifact") is not True
        ):
            raise Ia32BackendError("IA-32 decoded-store artifact contract does not match Phase 2")
        outputs = manifest.get("phase2_outputs")
        if not isinstance(outputs, dict) or set(outputs) != set(
            expected_store_contract["required_outputs"]
        ):
            raise Ia32BackendError("IA-32 decoded-store artifact omits required Phase-2 outputs")
        output_identity_keys = {
            "section-map.json": "section_map_id",
            "coverage-map.json": "coverage_map_id",
            "direct-edge-relocations.json": "direct_edge_relocations_id",
            "indirect-target-table.json": "indirect_target_table_id",
            "rewrite-manifest.json": "rewrite_manifest_id",
        }
        for name, identity_key in output_identity_keys.items():
            output_record = outputs.get(name)
            if not isinstance(output_record, dict) or output_record.get(
                "sha256"
            ) != decoded_store_inputs.get(identity_key):
                raise Ia32BackendError(
                    f"IA-32 decoded-store artifact {name} identity is inconsistent"
                )
    if generator_version >= ARCHITECTURE_GENERATOR_VERSION:
        expected_architecture_contract = ia32_architecture_contract()
        expected_architecture_contract_id = ia32_architecture_contract_id()
        architecture_output = manifest.get("phase3_outputs")
        if (
            inputs.get("architecture_contract_id") != expected_architecture_contract_id
            or manifest.get("architecture_contract_id") != expected_architecture_contract_id
            or manifest.get("architecture_contract") != expected_architecture_contract
            or manifest.get("architecture_memory") is not True
            or not isinstance(architecture_output, dict)
            or set(architecture_output) != {"architecture-map.json"}
        ):
            raise Ia32BackendError("IA-32 architecture artifact contract does not match Phase 3")
        record = architecture_output["architecture-map.json"]
        if not isinstance(record, dict) or record.get("sha256") != inputs.get(
            "architecture_map_id"
        ):
            raise Ia32BackendError("IA-32 Phase-3 architecture-map identity is inconsistent")
    if generator_version >= HOST_ABI_GENERATOR_VERSION:
        expected_host_abi_contract = ia32_host_abi_contract()
        expected_host_abi_contract_id = ia32_host_abi_contract_id()
        phase4_outputs = manifest.get("phase4_outputs")
        broker = manifest.get("service_broker")
        if (
            inputs.get("host_abi_contract_id") != expected_host_abi_contract_id
            or manifest.get("host_abi_contract_id") != expected_host_abi_contract_id
            or manifest.get("host_abi_contract") != expected_host_abi_contract
            or manifest.get("host_abi") is not True
            or not isinstance(phase4_outputs, dict)
            or set(phase4_outputs) != {"host-abi-map.json"}
            or not isinstance(broker, dict)
            or broker.get("target") != "x86_64-pc-windows-msvc"
        ):
            raise Ia32BackendError("IA-32 host ABI artifact contract does not match Phase 4")
    if generator_version >= RESIDENT_SCHEDULER_GENERATOR_VERSION:
        expected_scheduler_contract = ia32_resident_scheduler_contract()
        expected_scheduler_contract_id = ia32_resident_scheduler_contract_id()
        phase5_outputs = manifest.get("phase5_outputs")
        if (
            inputs.get("resident_scheduler_contract_id") != expected_scheduler_contract_id
            or manifest.get("resident_scheduler_contract_id") != expected_scheduler_contract_id
            or manifest.get("resident_scheduler_contract") != expected_scheduler_contract
            or manifest.get("resident_scheduler") is not True
            or not isinstance(phase5_outputs, dict)
            or set(phase5_outputs) != {"resident-scheduler-map.json"}
        ):
            raise Ia32BackendError(
                "IA-32 resident scheduler artifact contract does not match Phase 5"
            )
        scheduler_record = phase5_outputs["resident-scheduler-map.json"]
        if not isinstance(scheduler_record, dict) or scheduler_record.get("sha256") != inputs.get(
            "resident_scheduler_map_id"
        ):
            raise Ia32BackendError("IA-32 Phase-5 scheduler-map identity is inconsistent")
    if generator_version >= COVERAGE_GROWTH_GENERATOR_VERSION:
        expected_coverage_contract = ia32_coverage_growth_contract()
        expected_coverage_contract_id = ia32_coverage_growth_contract_id()
        phase6_outputs = manifest.get("phase6_outputs")
        if (
            inputs.get("coverage_growth_contract_id") != expected_coverage_contract_id
            or manifest.get("coverage_growth_contract_id") != expected_coverage_contract_id
            or manifest.get("coverage_growth_contract") != expected_coverage_contract
            or manifest.get("coverage_growth") is not True
            or not isinstance(phase6_outputs, dict)
            or set(phase6_outputs) != {"coverage-growth-map.json"}
        ):
            raise Ia32BackendError("IA-32 coverage-growth artifact contract does not match Phase 6")
        coverage_record = phase6_outputs["coverage-growth-map.json"]
        if (
            not isinstance(coverage_record, dict)
            or coverage_record.get("sha256") != inputs.get("coverage_growth_map_id")
            or manifest.get("normal_execution_eligible") is not True
        ):
            raise Ia32BackendError("IA-32 Phase-6 coverage-growth map identity is inconsistent")
    if generator_version >= CUTOVER_GENERATOR_VERSION:
        expected_cutover_contract = ia32_launcher_cutover_contract()
        expected_cutover_contract_id = ia32_launcher_cutover_contract_id()
        phase7_outputs = manifest.get("phase7_outputs")
        if (
            inputs.get("launcher_cutover_contract_id") != expected_cutover_contract_id
            or manifest.get("launcher_cutover_contract_id") != expected_cutover_contract_id
            or manifest.get("launcher_cutover_contract") != expected_cutover_contract
            or manifest.get("normal_launcher_cutover") is not True
            or manifest.get("normal_launcher_backend") != "same-isa-ia32"
            or manifest.get("automatic_cross_backend_fallback") is not False
            or not isinstance(phase7_outputs, dict)
            or set(phase7_outputs) != {"launcher-cutover-map.json"}
        ):
            raise Ia32BackendError(
                "IA-32 launcher-cutover artifact contract does not match Phase 7"
            )
        cutover_record = phase7_outputs["launcher-cutover-map.json"]
        if not isinstance(cutover_record, dict) or cutover_record.get("sha256") != inputs.get(
            "launcher_cutover_map_id"
        ):
            raise Ia32BackendError("IA-32 Phase-7 launcher-cutover map identity is inconsistent")


def _load_reusable_artifact(root: Path, inputs: Mapping[str, Any]) -> Ia32SliceArtifact | None:
    manifest_path = root / "manifest.json"
    executable = root / "b2r-ia32-slice.exe"
    if not manifest_path.is_file() or not executable.is_file():
        return None
    try:
        manifest_value = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(manifest_value, dict) or manifest_value.get("inputs") != inputs:
        return None
    try:
        validate_ia32_artifact_manifest(manifest_value)
    except Ia32BackendError:
        return None
    if manifest_value.get("executable_sha256") != sha256_file(executable):
        return None
    try:
        _validate_phase2_output_files(root, manifest_value)
        _validate_phase3_output_files(root, manifest_value)
        _validate_phase4_output_files(root, manifest_value)
        _validate_phase5_output_files(root, manifest_value)
        _validate_phase6_output_files(root, manifest_value)
        _validate_phase7_output_files(root, manifest_value)
    except Ia32BackendError:
        return None
    return Ia32SliceArtifact(root, executable, manifest_path, manifest_value)


def _validate_phase2_output_files(root: Path, manifest: Mapping[str, Any]) -> None:
    if manifest.get("decoded_store_artifact") is not True:
        return
    outputs = manifest.get("phase2_outputs")
    if not isinstance(outputs, dict):
        raise Ia32BackendError("IA-32 decoded-store artifact omits Phase-2 outputs")
    for name, record in outputs.items():
        if not isinstance(name, str) or not isinstance(record, dict):
            raise Ia32BackendError("IA-32 decoded-store artifact output record is malformed")
        path_value = record.get("path")
        expected_sha256 = record.get("sha256")
        if path_value != name or not isinstance(expected_sha256, str):
            raise Ia32BackendError(f"IA-32 Phase-2 output {name} has invalid identity metadata")
        output = root / name
        if not output.is_file() or sha256_file(output) != expected_sha256:
            raise Ia32BackendError(f"IA-32 Phase-2 output identity mismatch: {name}")
    coverage_path = root / "coverage-map.json"
    try:
        coverage = json.loads(coverage_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise Ia32BackendError("IA-32 Phase-2 coverage map is malformed") from exc
    if not isinstance(coverage, dict) or (
        (bool(coverage.get("complete")) or manifest.get("architecture_memory") is True)
        != bool(manifest.get("normal_execution_eligible"))
    ):
        raise Ia32BackendError("IA-32 Phase-2 coverage eligibility is inconsistent")


def _validate_phase3_output_files(root: Path, manifest: Mapping[str, Any]) -> None:
    if manifest.get("architecture_memory") is not True:
        return
    outputs = manifest.get("phase3_outputs")
    if not isinstance(outputs, dict):
        raise Ia32BackendError("IA-32 architecture artifact omits Phase-3 outputs")
    record = outputs.get("architecture-map.json")
    if not isinstance(record, dict):
        raise Ia32BackendError("IA-32 architecture-map record is malformed")
    output = root / str(record.get("path", ""))
    if (
        record.get("path") != "architecture-map.json"
        or not output.is_file()
        or sha256_file(output) != record.get("sha256")
    ):
        raise Ia32BackendError("IA-32 Phase-3 architecture-map identity mismatch")


def _validate_phase4_output_files(root: Path, manifest: Mapping[str, Any]) -> None:
    if manifest.get("host_abi") is not True:
        return
    outputs = manifest.get("phase4_outputs")
    record = outputs.get("host-abi-map.json") if isinstance(outputs, dict) else None
    if not isinstance(record, dict):
        raise Ia32BackendError("IA-32 host ABI artifact omits its Phase-4 map")
    output = root / str(record.get("path", ""))
    if (
        record.get("path") != "host-abi-map.json"
        or not output.is_file()
        or sha256_file(output) != record.get("sha256")
    ):
        raise Ia32BackendError("IA-32 Phase-4 host ABI map identity mismatch")
    broker = manifest.get("service_broker")
    broker_path = root / str(broker.get("executable", "")) if isinstance(broker, dict) else root
    if (
        not isinstance(broker, dict)
        or not broker_path.is_file()
        or sha256_file(broker_path) != broker.get("sha256")
    ):
        raise Ia32BackendError("IA-32 Phase-4 native service broker identity mismatch")


def _validate_phase5_output_files(root: Path, manifest: Mapping[str, Any]) -> None:
    if manifest.get("resident_scheduler") is not True:
        return
    outputs = manifest.get("phase5_outputs")
    record = outputs.get("resident-scheduler-map.json") if isinstance(outputs, dict) else None
    if not isinstance(record, dict):
        raise Ia32BackendError("IA-32 resident scheduler artifact omits its Phase-5 map")
    output = root / str(record.get("path", ""))
    if (
        record.get("path") != "resident-scheduler-map.json"
        or not output.is_file()
        or sha256_file(output) != record.get("sha256")
    ):
        raise Ia32BackendError("IA-32 Phase-5 scheduler-map identity mismatch")


def _validate_phase6_output_files(root: Path, manifest: Mapping[str, Any]) -> None:
    if manifest.get("coverage_growth") is not True:
        return
    outputs = manifest.get("phase6_outputs")
    record = outputs.get("coverage-growth-map.json") if isinstance(outputs, dict) else None
    if not isinstance(record, dict):
        raise Ia32BackendError("IA-32 coverage-growth artifact omits its Phase-6 map")
    output = root / str(record.get("path", ""))
    if (
        record.get("path") != "coverage-growth-map.json"
        or not output.is_file()
        or sha256_file(output) != record.get("sha256")
    ):
        raise Ia32BackendError("IA-32 Phase-6 coverage-growth map identity mismatch")


def _validate_phase7_output_files(root: Path, manifest: Mapping[str, Any]) -> None:
    if manifest.get("normal_launcher_cutover") is not True:
        return
    outputs = manifest.get("phase7_outputs")
    record = outputs.get("launcher-cutover-map.json") if isinstance(outputs, dict) else None
    if not isinstance(record, dict):
        raise Ia32BackendError("IA-32 launcher-cutover artifact omits its Phase-7 map")
    output = root / str(record.get("path", ""))
    if (
        record.get("path") != "launcher-cutover-map.json"
        or not output.is_file()
        or sha256_file(output) != record.get("sha256")
    ):
        raise Ia32BackendError("IA-32 Phase-7 launcher-cutover map identity mismatch")


def _build_ia32_slice_artifact(
    capsule: ReplayCapsule,
    *,
    stop_eip: int,
    build_dir: Path,
    compiler: Path | None = None,
    kernel32_library: Path | None = None,
    persistent_worker: bool = False,
    decoded_store_plan: Ia32DecodedStorePlan | None = None,
    architecture_profile: Ia32ArchitectureProfile | None = None,
    host_abi: bool = False,
    resident_scheduler: bool = False,
    coverage_growth_plan: Ia32CoverageGrowthPlan | None = None,
    normal_launcher_cutover: bool = False,
    normal_live_launch: bool = False,
    prepared_preflight: _FixedSlicePreflight | None = None,
    prepared_architecture_plan: _Ia32ArchitecturePlan | None = None,
    declared_host_services: Sequence[_HostServiceThunk] = (),
    coverage_boundary_spans: Sequence[tuple[int, bytes]] = (),
) -> Ia32SliceArtifact:
    stop_eip = _u32(stop_eip)
    if normal_live_launch and not normal_launcher_cutover:
        raise Ia32BackendError("normal-live generation requires the Phase-7 cutover layer")
    if normal_launcher_cutover and coverage_growth_plan is None:
        raise Ia32BackendError("Phase-7 launcher cutover requires a closed Phase-6 artifact plan")
    if coverage_growth_plan is not None:
        if decoded_store_plan is None:
            raise Ia32BackendError("Phase-6 coverage growth requires a decoded-store plan")
        if coverage_growth_plan.coverage_growth_map.get("promotion_eligible") is not True:
            raise Ia32BackendError(
                "Phase-6 artifacts require a closed normal-validation coverage profile"
            )
        resident_scheduler = True
    if resident_scheduler:
        host_abi = True
        if architecture_profile is None:
            architecture_profile = Ia32ArchitectureProfile()
    scheduler_plan = (
        _resident_scheduler_plan(capsule, stop_eip=stop_eip) if resident_scheduler else None
    )
    preflight = prepared_preflight or _fixed_slice_preflight(capsule, stop_eip)
    if declared_host_services:
        preflight = replace(
            preflight,
            host_service_thunks=tuple(
                {service.target: service for service in declared_host_services}.values()
            ),
        )
    instructions = preflight.instructions
    artifact_instructions = (
        _instruction_map(decoded_store_plan.functions)
        if decoded_store_plan is not None
        else instructions
    )
    if host_abi and architecture_profile is None:
        architecture_profile = Ia32ArchitectureProfile()
    architecture_plan = prepared_architecture_plan or (
        _phase3_architecture_plan(
            capsule,
            preflight,
            stop_eip=stop_eip,
            profile=architecture_profile,
            resident_privileged_boundary=resident_scheduler,
        )
        if architecture_profile is not None
        else None
    )
    if architecture_plan is not None:
        if coverage_growth_plan is not None:
            if decoded_store_plan is None:
                raise Ia32BackendError(
                    "Phase-6 artifact instruction selection lost its decoded-store plan"
                )
            artifact_instructions = dict(architecture_plan.instructions)
        elif scheduler_plan is not None:
            if all(
                step.entry_eip in architecture_plan.instructions for step in scheduler_plan.steps
            ):
                artifact_instructions = architecture_plan.instructions
            else:
                artifact_instructions = _instruction_map(capsule.functions)
                artifact_instructions.update(architecture_plan.instructions)
        else:
            artifact_instructions = architecture_plan.instructions
    if not capsule.state.flags.interrupt_enabled:
        raise Ia32BackendError("ring-3 POPFD cannot restore a cleared interrupt-enable flag")
    if architecture_plan is None and capsule.state.fs_base:
        raise Ia32BackendError("nonzero FS base requires a TLS rewrite thunk")
    if architecture_plan is None and capsule.state.fpu_stack:
        raise Ia32BackendError("nonempty x87 input state is not encoded by the prototype thunk")
    if architecture_plan is None and any(capsule.state.mmx_registers.values()):
        raise Ia32BackendError("nonzero MMX input state is not encoded by the Phase-0 thunk")
    artifact_indirect_targets = dict(preflight.indirect_targets)
    if coverage_growth_plan is not None and decoded_store_plan is not None:
        artifact_indirect_targets.update(
            {
                int(record["source"]): tuple(int(target) for target in record["targets"])
                for record in decoded_store_plan.indirect_target_table["sites"]
                if record.get("targets")
            }
        )
    guarded_instruction_set = (
        artifact_instructions if coverage_growth_plan is not None else instructions
    )
    guarded_boundaries = _guarded_static_boundaries(
        guarded_instruction_set,
        stop_eip,
        artifact_indirect_targets,
        (service.target for service in preflight.host_service_thunks),
    )
    if architecture_plan is not None:
        implemented = {int(record["address"]) for record in architecture_plan.rewrites}
        guarded_boundaries = {
            address: reason
            for address, reason in guarded_boundaries.items()
            if address not in implemented
        }
    compiler_path = _resolve_compiler(compiler)
    assembler_path = compiler_path.with_name("clang.exe")
    if not assembler_path.is_file():
        raise Ia32BackendError("clang is required to assemble the rebuilt IA-32 title PE")
    linker_path = _resolve_linker(compiler_path)
    kernel32_path = _resolve_kernel32_library(kernel32_library)
    broker_kernel32_path = _resolve_kernel32_library(None, architecture="x64") if host_abi else None
    code_spans = _contiguous_code_spans(
        artifact_instructions,
        guarded_addresses=guarded_boundaries,
    )
    if architecture_plan is not None:
        code_spans = tuple(
            sorted((*code_spans, *architecture_plan.extra_code_spans), key=lambda item: item[0])
        )
    if coverage_boundary_spans:
        code_spans = tuple(
            sorted((*code_spans, *coverage_boundary_spans), key=lambda item: item[0])
        )
    detached_guest = coverage_growth_plan is not None
    verified_code_spans = _contiguous_code_spans(
        _instruction_map(decoded_store_plan.functions)
        if decoded_store_plan is not None
        else instructions
    )
    embedded_data_spans = _mixed_code_page_data_spans(
        capsule,
        preflight.memory_ranges,
        code_spans,
        verified_code_spans=verified_code_spans,
    )
    dependency_pages: Iterable[int] = preflight.page_addresses
    if architecture_plan is not None:
        dependency_pages = tuple(
            int(record["mapped_address"]) for record in architecture_plan.page_bindings
        )
    if scheduler_plan is not None:
        dependency_pages = tuple(sorted({*dependency_pages, *scheduler_plan.required_pages}))
    page_addresses = _artifact_page_addresses(
        capsule,
        instructions,
        code_spans,
        dependency_pages,
        detached_guest=detached_guest,
    )
    if detached_guest:
        helper_conflicts = tuple(
            address
            for address in page_addresses
            if DETACHED_GUEST_RESERVATION_END <= address < DETACHED_HELPER_GUARD_END
        )
        if helper_conflicts:
            formatted = ", ".join(f"0x{address:08X}" for address in helper_conflicts[:8])
            raise Ia32BackendError(
                "detached IA-32 helper layout overlaps captured title pages: " + formatted
            )
    if architecture_plan is not None:
        filtered_bindings = tuple(
            record
            for record in architecture_plan.page_bindings
            if int(record["mapped_address"]) in page_addresses
        )
        filtered_ownership = tuple(
            record
            for record in architecture_plan.dirty_ownership
            if int(record["mapped_address"]) in page_addresses
        )
        architecture_map = dict(architecture_plan.architecture_map)
        architecture_map["page_bindings"] = list(filtered_bindings)
        architecture_map["dirty_ownership"] = list(filtered_ownership)
        architecture_plan = replace(
            architecture_plan,
            page_bindings=filtered_bindings,
            dirty_ownership=filtered_ownership,
            architecture_map=architecture_map,
        )
    normal_live_boot_image = b""
    if normal_live_launch:
        if architecture_plan is None or scheduler_plan is None:
            raise Ia32BackendError(
                "normal-live generation requires architecture and scheduler metadata"
            )
        boot_artifact = Ia32SliceArtifact(
            root=Path(),
            executable=Path(),
            manifest_path=Path(),
            manifest={
                "page_addresses": list(page_addresses),
                "architecture_memory": True,
                "phase3_page_bindings": list(architecture_plan.page_bindings),
                "host_abi": True,
                "resident_scheduler": True,
                "resident_scheduler_step_count": len(scheduler_plan.steps),
                "normal_launcher_cutover": True,
            },
        )
        normal_live_boot_image = bytes(
            _exchange_payload(
                capsule,
                boot_artifact,
                exchange_version=CUTOVER_EXCHANGE_VERSION,
                protocol_version=WORKER_PROTOCOL_VERSION,
                worker_command=WORKER_COMMAND_ENTER,
            )
        )
    code_end = max(
        *(address + len(payload) for address, payload in code_spans),
        stop_eip + 5,
    )
    low_data_end = max(
        (address + PAGE_SIZE for address in page_addresses if address < LOW_GUEST_IMAGE_LIMIT),
        default=0,
    )
    required_end = (
        GUEST_SECTION_ADDRESS + PAGE_SIZE
        if detached_guest
        else (
            (max(code_end, low_data_end) + WINDOWS_ALLOCATION_GRANULARITY - 1)
            & ~(WINDOWS_ALLOCATION_GRANULARITY - 1)
        )
        + 2 * WINDOWS_ALLOCATION_GRANULARITY
    )
    mapped_page_addresses = set(page_addresses)
    for address, payload in code_spans:
        mapped_page_addresses.update(
            range(
                address & ~(PAGE_SIZE - 1),
                (address + len(payload) + PAGE_SIZE - 1) & ~(PAGE_SIZE - 1),
                PAGE_SIZE,
            )
        )
    mapped_page_addresses.add(stop_eip & ~(PAGE_SIZE - 1))
    source_generator = (
        _phase7_c_source
        if normal_launcher_cutover
        else _phase5_c_source
        if scheduler_plan is not None
        else _phase4_c_source
        if host_abi
        else _phase3_c_source
        if architecture_plan is not None
        else _decoded_store_c_source
        if decoded_store_plan is not None
        else _persistent_c_source
        if persistent_worker
        else _c_source
    )
    source_arguments: dict[str, Any] = {
        "page_addresses": page_addresses,
        "mapped_page_addresses": tuple(sorted(mapped_page_addresses)),
        "guest_virtual_size": required_end - GUEST_SECTION_ADDRESS,
        "code_spans": code_spans,
        "stop_eip": stop_eip,
        "host_service_thunks": preflight.host_service_thunks,
        "audited_host_services": host_abi,
    }
    if detached_guest:
        source_arguments["detached_guest"] = True
    if scheduler_plan is not None:
        source_arguments["scheduler_plan"] = scheduler_plan
    if host_abi:
        source_arguments["service_state"] = capsule.service_state
    source = source_generator(**source_arguments)
    if normal_live_launch:
        stop_instruction = artifact_instructions.get(stop_eip)
        if stop_instruction is None:
            raise Ia32BackendError(f"normal-live flip stop 0x{stop_eip:08X} is not decoded")
        normal_live_host_data_pages = _normal_live_host_data_pages(
            capsule.memory,
            preflight.host_service_thunks,
        )
        source = _phase7_normal_live_c_source(
            source,
            page_count=len(page_addresses),
            boot_image_size=len(normal_live_boot_image),
            stop_instruction=stop_instruction,
            host_data_pages=normal_live_host_data_pages,
        )
        source = _phase7_native_workload_source(
            source,
            safe_object_paths_only=True,
        )
    if architecture_plan is not None:
        source = _phase3_fault_preflight_source(source, architecture_plan)
    guest_source = (
        _detached_guest_reservation_assembly()
        if detached_guest
        else _guest_assembly(
            code_spans,
            stop_eip=stop_eip,
            data_spans=embedded_data_spans,
        )
    )
    broker_source = (
        _phase4_broker_c_source(
            page_count=len(page_addresses),
            host_service_thunks=preflight.host_service_thunks,
        )
        if host_abi
        else ""
    )
    inputs = _artifact_inputs(
        capsule=capsule,
        stop_eip=stop_eip,
        source=source,
        guest_source=guest_source,
        compiler=compiler_path,
        assembler=assembler_path,
        linker=linker_path,
        kernel32_library=kernel32_path,
        persistent_worker=persistent_worker,
        decoded_store_plan=decoded_store_plan,
        architecture_plan=architecture_plan,
        host_abi=host_abi,
        scheduler_plan=scheduler_plan,
        coverage_growth_plan=coverage_growth_plan,
        normal_launcher_cutover=normal_launcher_cutover,
        normal_live_launch=normal_live_launch,
        normal_live_boot_image=normal_live_boot_image,
        broker_source=broker_source,
        broker_kernel32_library=broker_kernel32_path,
    )
    artifact_id = _sha256(_canonical_json(inputs))
    root = build_dir.resolve() / artifact_id
    reusable = _load_reusable_artifact(root, inputs)
    if reusable is not None:
        return reusable
    root.mkdir(parents=True, exist_ok=True)
    source_path = root / "runner.c"
    object_path = root / "runner.obj"
    executable = root / "b2r-ia32-slice.exe"
    guest_source_path = root / "guest.s"
    guest_object_path = root / "guest.obj"
    broker_source_path = root / "service-broker.c"
    broker_object_path = root / "service-broker.obj"
    broker_executable = root / "b2r-ia32-service-broker.exe"
    boot_image_path = root / "boot-image.bin"
    source_path.write_text(source, encoding="utf-8", newline="\n")
    guest_source_path.write_text(guest_source, encoding="utf-8", newline="\n")
    if host_abi:
        broker_source_path.write_text(broker_source, encoding="utf-8", newline="\n")
    if normal_live_launch:
        boot_image_path.write_bytes(normal_live_boot_image)
    assembled = subprocess.run(
        [
            str(assembler_path),
            f"--target={TARGET_TRIPLE}",
            "-c",
            str(guest_source_path),
            "-o",
            str(guest_object_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if assembled.returncode != 0:
        raise Ia32BackendError(
            "rebuilt IA-32 title assembly failed:\n" + assembled.stdout + assembled.stderr
        )
    compile_command = [
        str(compiler_path),
        f"--target={TARGET_TRIPLE}",
        "/nologo",
        "/c",
        "/O2",
        "/GS-",
        "/Zl",
        str(source_path),
        f"/Fo{object_path}",
    ]
    if normal_live_launch:
        compile_command.insert(5, "/clang:-fno-builtin")
    compiled = subprocess.run(compile_command, check=False, capture_output=True, text=True)
    if compiled.returncode != 0:
        raise Ia32BackendError(
            "IA-32 helper compilation failed:\n" + compiled.stdout + compiled.stderr
        )
    link_command = [
        str(linker_path),
        "/nologo",
        "/subsystem:console",
        "/entry:mainCRTStartup",
        "/nodefaultlib",
        "/safeseh:no",
        "/largeaddressaware",
        "/fixed",
        "/dynamicbase:no",
        "/timestamp:0",
        f"/align:{4096 if detached_guest else 65536}",
        f"/base:0x{(DETACHED_HELPER_IMAGE_BASE if detached_guest else HELPER_IMAGE_BASE):08X}",
        f"/out:{executable}",
        str(object_path),
        str(guest_object_path),
        str(kernel32_path),
    ]
    linked = subprocess.run(link_command, check=False, capture_output=True, text=True)
    if linked.returncode != 0:
        raise Ia32BackendError("IA-32 helper link failed:\n" + linked.stdout + linked.stderr)
    if not detached_guest:
        _extend_guest_pe_virtual_size(executable, required_end=required_end)
    if host_abi:
        if broker_kernel32_path is None:
            raise Ia32BackendError("Phase-4 broker import library was not resolved")
        broker_compiled = subprocess.run(
            [
                str(compiler_path),
                "--target=x86_64-pc-windows-msvc",
                "/nologo",
                "/c",
                "/O2",
                "/GS-",
                "/Zl",
                str(broker_source_path),
                f"/Fo{broker_object_path}",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if broker_compiled.returncode != 0:
            raise Ia32BackendError(
                "IA-32 native service broker compilation failed:\n"
                + broker_compiled.stdout
                + broker_compiled.stderr
            )
        broker_linked = subprocess.run(
            [
                str(linker_path),
                "/nologo",
                "/machine:x64",
                "/subsystem:console",
                "/entry:mainCRTStartup",
                "/nodefaultlib",
                "/fixed",
                "/dynamicbase:no",
                "/timestamp:0",
                f"/out:{broker_executable}",
                str(broker_object_path),
                str(broker_kernel32_path),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if broker_linked.returncode != 0:
            raise Ia32BackendError(
                "IA-32 native service broker link failed:\n"
                + broker_linked.stdout
                + broker_linked.stderr
            )
    manifest = {
        "format": ARTIFACT_FORMAT,
        "version": ARTIFACT_VERSION,
        "artifact_id": artifact_id,
        "inputs": inputs,
        "execution_contract": ia32_execution_contract(),
        "execution_contract_id": ia32_execution_contract_id(),
        "toolchain": _toolchain_identity(inputs),
        "executable": executable.name,
        "executable_sha256": sha256_file(executable),
        "guest_image": executable.name,
        "guest_image_sha256": sha256_file(executable),
        "guest_image_base": (DETACHED_HELPER_IMAGE_BASE if detached_guest else HELPER_IMAGE_BASE),
        "guest_section_address": None if detached_guest else GUEST_SECTION_ADDRESS,
        "guest_virtual_end": required_end,
        "detached_guest_mapping": detached_guest,
        "decoded_preflight": {
            "mode": "build-time-fixed-capsule-dependency-trace",
            "steps": preflight.steps,
            "function_symbols": list(preflight.function_symbols),
            "page_addresses": list(preflight.page_addresses),
            "memory_ranges": [
                {"address": address, "size": size} for address, size in preflight.memory_ranges
            ],
            "verified_indirect_targets": {
                f"0x{address:08X}": [f"0x{target:08X}" for target in targets]
                for address, targets in preflight.indirect_targets.items()
            },
            "host_service_thunks": [
                {
                    "target": service.target,
                    "target_hex": f"0x{service.target:08X}",
                    "shim_name": service.shim_name,
                    "kind": service.kind,
                    "value": service.value,
                    "value_hex": f"0x{service.value:08X}",
                    "stack_cleanup_bytes": service.stack_cleanup_bytes,
                    "calling_convention": service.calling_convention,
                    "argument_count": service.argument_count,
                    "boundary": service.boundary,
                    "execution": service.execution,
                    "memory_write_argument": service.memory_write_argument,
                    "memory_write_value": service.memory_write_value,
                    "callback_argument": service.callback_argument,
                    "callback_context_argument": service.callback_context_argument,
                    "callback_value": service.callback_value,
                    "callback_stack_cleanup_bytes": service.callback_stack_cleanup_bytes,
                    "runtime_kind": service.runtime_kind,
                    "runtime_value": service.runtime_value,
                    "code_sha256": _sha256(
                        _host_service_thunk_bytes(index, service, audited=host_abi)
                    ),
                }
                for index, service in enumerate(preflight.host_service_thunks)
            ],
            "runtime_callbacks": 0,
        },
        "page_addresses": list(page_addresses),
        "mapped_page_addresses": sorted(mapped_page_addresses),
        "code_spans": [
            {"address": address, "size": len(payload), "sha256": _sha256(payload)}
            for address, payload in code_spans
        ],
        "embedded_data_spans": [
            {"address": address, "size": len(payload), "sha256": _sha256(payload)}
            for address, payload in embedded_data_spans
        ],
        "patched_boundaries": [
            {
                "kind": "slice-stop",
                "address": stop_eip,
                "target": EXIT_THUNK_ADDRESS,
            }
        ]
        + [
            {
                "kind": "host-service-abi-thunk",
                "address": service.target,
                "address_hex": f"0x{service.target:08X}",
                "shim_name": service.shim_name,
                "stack_cleanup_bytes": service.stack_cleanup_bytes,
            }
            for service in preflight.host_service_thunks
        ],
        "guarded_boundaries": [
            {
                "kind": "static-gap-trap",
                "address": address,
                "address_hex": f"0x{address:08X}",
                "reason": reason,
            }
            for address, reason in sorted(guarded_boundaries.items())
        ],
        "static_rewrite_gaps": [],
        "runtime_compilation": False,
        "runtime_decoding": False,
        "undecoded_xbe_bytes": False,
    }
    if persistent_worker:
        manifest.update(
            {
                "persistent_worker": True,
                "persistent_worker_contract": ia32_persistent_worker_contract(),
                "persistent_worker_contract_id": ia32_persistent_worker_contract_id(),
                "normal_runtime_python_callbacks": 0,
            }
        )
    if scheduler_plan is not None:
        scheduler_payload = _canonical_json(scheduler_plan.scheduler_map)
        scheduler_path = root / "resident-scheduler-map.json"
        scheduler_path.write_bytes(scheduler_payload)
        manifest.update(
            {
                "resident_scheduler": True,
                "resident_scheduler_contract": ia32_resident_scheduler_contract(),
                "resident_scheduler_contract_id": ia32_resident_scheduler_contract_id(),
                "phase5_outputs": {
                    "resident-scheduler-map.json": {
                        "path": "resident-scheduler-map.json",
                        "sha256": _sha256(scheduler_payload),
                    }
                },
                "resident_scheduler_lane_count": RESIDENT_SCHEDULER_LANE_COUNT,
                "resident_scheduler_step_count": len(scheduler_plan.steps),
                "normal_runtime_python_callbacks": 0,
                "runtime_code_patching": False,
                "native_promotion": False,
                "raw_xbe_execution": False,
            }
        )
    if normal_launcher_cutover:
        if coverage_growth_plan is None:
            raise Ia32BackendError("Phase-7 manifest lost its coverage-growth plan")
        cutover_map = _launcher_cutover_map(coverage_growth_plan)
        cutover_payload = _canonical_json(cutover_map)
        cutover_path = root / "launcher-cutover-map.json"
        cutover_path.write_bytes(cutover_payload)
        if _sha256(cutover_payload) != inputs["launcher_cutover_map_id"]:
            raise Ia32BackendError("Phase-7 launcher-cutover map changed after input identity")
        manifest.update(
            {
                "normal_launcher_cutover": True,
                "normal_launcher_backend": "same-isa-ia32",
                "diagnostic_oracle_backend": "fusion-only-64-bit",
                "automatic_cross_backend_fallback": False,
                "launcher_cutover_contract": ia32_launcher_cutover_contract(),
                "launcher_cutover_contract_id": ia32_launcher_cutover_contract_id(),
                "phase7_outputs": {
                    "launcher-cutover-map.json": {
                        "path": "launcher-cutover-map.json",
                        "sha256": _sha256(cutover_payload),
                    }
                },
                "persistent_native_memory_owner": True,
                "dirty_only_memory_publication": True,
                "cross_backend_exits": 0,
                "normal_runtime_python_callbacks": 0,
                "runtime_code_patching": False,
                "native_promotion": False,
                "raw_xbe_execution": False,
            }
        )
    if normal_live_launch:
        contract = ia32_normal_live_launch_contract()
        contract_id = ia32_normal_live_launch_contract_id()
        manifest.update(
            {
                "normal_live_launch": True,
                "normal_live_launch_contract": contract,
                "normal_live_launch_contract_id": contract_id,
                "normal_live_boot_image": {
                    "path": boot_image_path.name,
                    "bytes": len(normal_live_boot_image),
                    "sha256": sha256_file(boot_image_path),
                },
                "normal_live_entry_eip": capsule.state.eip,
                "normal_live_flip_stop_eip": stop_eip,
                "normal_live_resume_thunk": NORMAL_LIVE_RESUME_THUNK_ADDRESS,
                "normal_live_dynamic_scheduler": True,
                "normal_live_native_vblank": contract["workload"]["vblank_lane"],
                "normal_live_fixed_replay_plan": False,
                "normal_live_native_control_manifest": True,
                "normal_live_native_command_publication": True,
                "normal_live_native_resource_publication": True,
                "normal_live_native_controller_consumption": True,
                "normal_live_native_audio": True,
                "normal_live_native_filesystem": True,
                "offline_boundary_optimization_interval": (
                    PHASE7_OFFLINE_BOUNDARY_OPTIMIZATION_INTERVAL
                ),
                "ia32_runtime_optimizations": [
                    dict(record) for record in PHASE7_OFFLINE_BOUNDARY_OPTIMIZATIONS
                ],
                "normal_runtime_python_callbacks": 0,
            }
        )
    if coverage_growth_plan is not None:
        coverage_payload = _canonical_json(coverage_growth_plan.coverage_growth_map)
        coverage_path = root / "coverage-growth-map.json"
        coverage_path.write_bytes(coverage_payload)
        manifest.update(
            {
                "coverage_growth": True,
                "coverage_growth_contract": ia32_coverage_growth_contract(),
                "coverage_growth_contract_id": ia32_coverage_growth_contract_id(),
                "phase6_outputs": {
                    "coverage-growth-map.json": {
                        "path": "coverage-growth-map.json",
                        "sha256": _sha256(coverage_payload),
                    }
                },
                "coverage_profile_id": coverage_growth_plan.coverage_growth_map["profile_id"],
                "coverage_closed": True,
                "unknown_target_count": 0,
                "frontier_interpreter_invocations": 0,
                "frontier_interpreter_steps": 0,
                "normal_execution_eligible": True,
                "normal_runtime_python_callbacks": 0,
                "runtime_code_patching": False,
                "native_promotion": False,
                "raw_xbe_execution": False,
            }
        )
    if decoded_store_plan is not None:
        phase2_payloads = {
            "section-map.json": decoded_store_plan.section_map,
            "coverage-map.json": decoded_store_plan.coverage_map,
            "direct-edge-relocations.json": decoded_store_plan.direct_edge_relocations,
            "indirect-target-table.json": decoded_store_plan.indirect_target_table,
            "rewrite-manifest.json": decoded_store_plan.rewrite_manifest,
        }
        phase2_outputs = {}
        for name, value in phase2_payloads.items():
            output_path = root / name
            output_payload = _canonical_json(value)
            output_path.write_bytes(output_payload)
            phase2_outputs[name] = {
                "path": name,
                "sha256": _sha256(output_payload),
            }
        manifest.update(
            {
                "decoded_store_artifact": True,
                "decoded_store_artifact_contract": ia32_decoded_store_artifact_contract(),
                "decoded_store_artifact_contract_id": (ia32_decoded_store_artifact_contract_id()),
                "phase2_outputs": phase2_outputs,
                "normal_execution_eligible": bool(
                    decoded_store_plan.coverage_map["complete"] or architecture_plan is not None
                ),
                "runtime_code_patching": False,
                "native_promotion": False,
                "raw_xbe_execution": False,
            }
        )
    if architecture_plan is not None:
        manifest_bindings = list(architecture_plan.page_bindings)
        architecture_map = architecture_plan.architecture_map
        architecture_payload = _canonical_json(architecture_map)
        architecture_path = root / "architecture-map.json"
        architecture_path.write_bytes(architecture_payload)
        if _sha256(architecture_payload) != inputs["architecture_map_id"]:
            raise Ia32BackendError(
                "Phase-3 architecture-map filtering changed its frozen input identity"
            )
        manifest.update(
            {
                "architecture_memory": True,
                "architecture_contract": ia32_architecture_contract(),
                "architecture_contract_id": ia32_architecture_contract_id(),
                "phase3_outputs": {
                    "architecture-map.json": {
                        "path": "architecture-map.json",
                        "sha256": _sha256(architecture_payload),
                    }
                },
                "phase3_page_bindings": manifest_bindings,
                "phase3_dirty_ownership": architecture_map["dirty_ownership"],
                "phase3_timestamp_step_count": architecture_plan.timestamp_step_count,
                "recoverable_guest_faults": True,
                "self_modifying_executable_memory": False,
                "new_executable_memory": False,
                "normal_runtime_python_callbacks": 0,
                "runtime_code_patching": False,
                "native_promotion": False,
                "raw_xbe_execution": False,
            }
        )
    if host_abi:
        host_abi_map = {
            "format": "b2-recomp-ia32-host-abi-map",
            "version": 1,
            "contract_id": ia32_host_abi_contract_id(),
            "services": [
                {
                    "index": index,
                    "target": service.target,
                    "target_hex": f"0x{service.target:08X}",
                    "shim_name": service.shim_name,
                    "kind": service.kind,
                    "calling_convention": service.calling_convention,
                    "argument_count": service.argument_count,
                    "stack_cleanup_bytes": service.stack_cleanup_bytes,
                    "boundary": service.boundary,
                    "execution": service.execution,
                    "memory_write_argument": service.memory_write_argument,
                    "callback_argument": service.callback_argument,
                    "callback_context_argument": service.callback_context_argument,
                    "callback_stack_cleanup_bytes": service.callback_stack_cleanup_bytes,
                    "runtime_kind": service.runtime_kind,
                    "runtime_value": service.runtime_value,
                    "normal_live_body": service.normal_live_body,
                    "thunk_sha256": _sha256(_audited_service_thunk(index, service)),
                }
                for index, service in enumerate(preflight.host_service_thunks)
            ],
            "broker": {
                "executable": broker_executable.name,
                "sha256": sha256_file(broker_executable),
                "target": "x86_64-pc-windows-msvc",
                "protocol_magic": HOST_SERVICE_BROKER_MAGIC,
                "protocol_version": HOST_SERVICE_BROKER_VERSION,
            },
            "trace": {
                "capacity": HOST_SERVICE_TRACE_CAPACITY,
                "record_words": HOST_SERVICE_TRACE_RECORD_WORDS,
            },
        }
        host_abi_payload = _canonical_json(host_abi_map)
        host_abi_path = root / "host-abi-map.json"
        host_abi_path.write_bytes(host_abi_payload)
        manifest.update(
            {
                "host_abi": True,
                "host_abi_contract": ia32_host_abi_contract(),
                "host_abi_contract_id": ia32_host_abi_contract_id(),
                "phase4_outputs": {
                    "host-abi-map.json": {
                        "path": "host-abi-map.json",
                        "sha256": _sha256(host_abi_payload),
                    }
                },
                "service_broker": {
                    "executable": broker_executable.name,
                    "sha256": sha256_file(broker_executable),
                    "target": "x86_64-pc-windows-msvc",
                },
                "registered_service_count": len(preflight.host_service_thunks),
                "normal_live_pending_host_services": [
                    {
                        "target": service.target,
                        "target_hex": f"0x{service.target:08X}",
                        "shim_name": service.shim_name,
                        "runtime_kind": service.runtime_kind,
                        "runtime_value": service.runtime_value,
                    }
                    for service in preflight.host_service_thunks
                    if service.normal_live_body != "implemented-native32"
                ],
                "normal_live_pending_host_service_count": sum(
                    service.normal_live_body != "implemented-native32"
                    for service in preflight.host_service_thunks
                ),
                "normal_runtime_python_callbacks": 0,
                "runtime_code_patching": False,
                "native_promotion": False,
                "raw_xbe_execution": False,
            }
        )
    manifest_path = root / "manifest.json"
    manifest_path.write_bytes(_canonical_json(manifest))
    return Ia32SliceArtifact(root, executable, manifest_path, manifest)


def build_ia32_slice_artifact(
    capsule: ReplayCapsule,
    *,
    stop_eip: int,
    build_dir: Path,
    compiler: Path | None = None,
    kernel32_library: Path | None = None,
) -> Ia32SliceArtifact:
    """Build the frozen Phase-0 one-shot artifact."""

    return _build_ia32_slice_artifact(
        capsule,
        stop_eip=stop_eip,
        build_dir=build_dir,
        compiler=compiler,
        kernel32_library=kernel32_library,
    )


def build_ia32_persistent_slice_artifact(
    capsule: ReplayCapsule,
    *,
    stop_eip: int,
    build_dir: Path,
    compiler: Path | None = None,
    kernel32_library: Path | None = None,
) -> Ia32SliceArtifact:
    """Build a Phase-1 artifact hosted by a long-lived native worker."""

    return _build_ia32_slice_artifact(
        capsule,
        stop_eip=stop_eip,
        build_dir=build_dir,
        compiler=compiler,
        kernel32_library=kernel32_library,
        persistent_worker=True,
    )


def build_ia32_decoded_store_artifact(
    capsule: ReplayCapsule,
    *,
    xbe_path: Path,
    decoded_block_store_path: Path,
    stop_eip: int,
    build_dir: Path,
    compiler: Path | None = None,
    kernel32_library: Path | None = None,
    allow_incomplete_coverage: bool = False,
) -> Ia32SliceArtifact:
    """Build a Phase-2 PE from the XBE-bound decoded block store snapshot."""

    services = _capsule_host_service_thunks(capsule)
    plan = inspect_ia32_decoded_store(
        xbe_path,
        decoded_block_store_path,
        host_services=services,
    )
    expanded_capsule = ReplayCapsule(
        path=capsule.path,
        manifest=capsule.manifest,
        state=capsule.state,
        memory=capsule.memory,
        functions=plan.functions,
        scheduler_state=capsule.scheduler_state,
        service_state=capsule.service_state,
        events=capsule.events,
        resources=capsule.resources,
    )
    if plan.indirect_target_table["sites"]:
        preflight = _fixed_slice_preflight(expanded_capsule, _u32(stop_eip))
        plan = inspect_ia32_decoded_store(
            xbe_path,
            decoded_block_store_path,
            host_services=services,
            verified_indirect_targets=preflight.indirect_targets,
        )
        expanded_capsule = ReplayCapsule(
            path=capsule.path,
            manifest=capsule.manifest,
            state=capsule.state,
            memory=capsule.memory,
            functions=plan.functions,
            scheduler_state=capsule.scheduler_state,
            service_state=capsule.service_state,
            events=capsule.events,
            resources=capsule.resources,
        )
    if not bool(plan.coverage_map["complete"]) and not allow_incomplete_coverage:
        gaps = plan.coverage_map["unsupported_sites"]
        sample = ", ".join(
            f"0x{int(record['address']):08X} {record['kind']}" for record in gaps[:8]
        )
        raise Ia32BackendError(
            "decoded-store artifact has unsupported static coverage sites: " + sample
        )
    return _build_ia32_slice_artifact(
        expanded_capsule,
        stop_eip=stop_eip,
        build_dir=build_dir,
        compiler=compiler,
        kernel32_library=kernel32_library,
        persistent_worker=True,
        decoded_store_plan=plan,
    )


def build_ia32_architecture_artifact(
    capsule: ReplayCapsule,
    *,
    stop_eip: int,
    build_dir: Path,
    compiler: Path | None = None,
    kernel32_library: Path | None = None,
    profile: Ia32ArchitectureProfile | None = None,
) -> Ia32SliceArtifact:
    """Build a Phase-3 resident artifact with frozen architectural rewrites."""

    return _build_ia32_slice_artifact(
        capsule,
        stop_eip=stop_eip,
        build_dir=build_dir,
        compiler=compiler,
        kernel32_library=kernel32_library,
        persistent_worker=True,
        architecture_profile=profile or Ia32ArchitectureProfile(),
    )


def build_ia32_host_abi_artifact(
    capsule: ReplayCapsule,
    *,
    stop_eip: int,
    build_dir: Path,
    compiler: Path | None = None,
    kernel32_library: Path | None = None,
    profile: Ia32ArchitectureProfile | None = None,
) -> Ia32SliceArtifact:
    """Build a Phase-4 worker with audited thunks and a native x64 broker."""

    return _build_ia32_slice_artifact(
        capsule,
        stop_eip=stop_eip,
        build_dir=build_dir,
        compiler=compiler,
        kernel32_library=kernel32_library,
        persistent_worker=True,
        architecture_profile=profile or Ia32ArchitectureProfile(),
        host_abi=True,
    )


def build_ia32_resident_scheduler_artifact(
    capsule: ReplayCapsule,
    *,
    stop_eip: int,
    build_dir: Path,
    compiler: Path | None = None,
    kernel32_library: Path | None = None,
    profile: Ia32ArchitectureProfile | None = None,
) -> Ia32SliceArtifact:
    """Build a Phase-5 worker with three persistent native IA-32 lanes."""

    return _build_ia32_slice_artifact(
        capsule,
        stop_eip=stop_eip,
        build_dir=build_dir,
        compiler=compiler,
        kernel32_library=kernel32_library,
        persistent_worker=True,
        architecture_profile=profile or Ia32ArchitectureProfile(),
        host_abi=True,
        resident_scheduler=True,
    )


def build_ia32_decoded_store_architecture_artifact(
    capsule: ReplayCapsule,
    *,
    xbe_path: Path,
    decoded_block_store_path: Path,
    stop_eip: int,
    build_dir: Path,
    compiler: Path | None = None,
    kernel32_library: Path | None = None,
    profile: Ia32ArchitectureProfile | None = None,
) -> Ia32SliceArtifact:
    """Build Phase 3 on top of an XBE-bound Phase-2 decoded-store plan."""

    active_profile = profile or Ia32ArchitectureProfile()
    services = _capsule_host_service_thunks(capsule)
    plan = inspect_ia32_decoded_store(
        xbe_path,
        decoded_block_store_path,
        host_services=services,
    )
    expanded_capsule = ReplayCapsule(
        path=capsule.path,
        manifest=capsule.manifest,
        state=capsule.state,
        memory=capsule.memory,
        functions=plan.functions,
        scheduler_state=capsule.scheduler_state,
        service_state=capsule.service_state,
        events=capsule.events,
        resources=capsule.resources,
    )
    if plan.indirect_target_table["sites"]:
        preflight = _fixed_slice_preflight(expanded_capsule, _u32(stop_eip))
        plan = inspect_ia32_decoded_store(
            xbe_path,
            decoded_block_store_path,
            host_services=services,
            verified_indirect_targets=preflight.indirect_targets,
        )
        expanded_capsule = replace(expanded_capsule, functions=plan.functions)
    preflight = _fixed_slice_preflight(expanded_capsule, _u32(stop_eip))
    architecture_plan = _phase3_architecture_plan(
        expanded_capsule,
        preflight,
        stop_eip=_u32(stop_eip),
        profile=active_profile,
    )
    implemented_addresses = {int(record["address"]) for record in architecture_plan.rewrites}
    unresolved = [
        record
        for record in plan.coverage_map["unsupported_sites"]
        if int(record["address"]) not in implemented_addresses
    ]
    if unresolved:
        sample = ", ".join(
            f"0x{int(record['address']):08X} {record['kind']}" for record in unresolved[:8]
        )
        raise Ia32BackendError(
            "Phase-3 decoded-store artifact has unresolved static coverage sites: " + sample
        )
    return _build_ia32_slice_artifact(
        expanded_capsule,
        stop_eip=stop_eip,
        build_dir=build_dir,
        compiler=compiler,
        kernel32_library=kernel32_library,
        persistent_worker=True,
        decoded_store_plan=plan,
        architecture_profile=active_profile,
    )


def _phase6_indirect_signature(instruction: X86Instruction) -> tuple[object, ...]:
    memory_operands = [operand for operand in instruction.operands if operand.kind == "mem"]
    if len(memory_operands) != 1:
        return (instruction.mnemonic, instruction.bytes_hex)
    operand = memory_operands[0]
    return (
        instruction.mnemonic,
        operand.displacement,
        operand.scale,
        operand.absolute,
        operand.segment,
    )


def _phase6_static_jump_table_targets(
    memory: SparseMemory,
    instruction: X86Instruction,
    allowed_targets: AbstractSet[int],
) -> tuple[int, ...]:
    """Recover a bounded fixed-base IA-32 jump table from captured verified pages."""

    if instruction.mnemonic.casefold() not in {"jmp", "jmp_indirect"}:
        return ()
    memory_operands = [operand for operand in instruction.operands if operand.kind == "mem"]
    if len(memory_operands) != 1:
        return ()
    operand = memory_operands[0]
    if (
        operand.base is not None
        or operand.index is None
        or operand.scale != 4
        or operand.absolute is not None
        or operand.segment is not None
        or operand.displacement <= 0
    ):
        return ()
    table_base = _u32(operand.displacement)
    targets: list[int] = []
    for index in range(PHASE6_MAX_STATIC_JUMP_TABLE_ENTRIES):
        target = memory.read_u32(_u32(table_base + index * 4))
        if target not in allowed_targets:
            break
        targets.append(target)
    if len(targets) < 2:
        return ()
    return tuple(sorted(set(targets)))


def _phase7_aligned_copy_tail_targets(
    memory: SparseMemory,
    instructions: Mapping[int, X86Instruction],
    indirect_sites: AbstractSet[int],
    allowed_targets: AbstractSet[int],
) -> dict[int, tuple[int, ...]]:
    """Validate the masked nonzero aligned-copy tail selector as one family."""

    site = PHASE7_ALIGNED_COPY_TAIL_SITE
    if site not in indirect_sites or not set(PHASE7_ALIGNED_COPY_TAIL_TARGETS) <= allowed_targets:
        return {}
    instruction = instructions.get(site)
    if (
        instruction is None
        or instruction.bytes_hex.upper() != "FF249DD4B62800"
        or memory.read(
            PHASE7_ALIGNED_COPY_TAIL_PROOF_START,
            len(bytes.fromhex(PHASE7_ALIGNED_COPY_TAIL_PROOF_BYTES)),
        ).hex().upper()
        != PHASE7_ALIGNED_COPY_TAIL_PROOF_BYTES
        or memory.read_u32(PHASE7_ALIGNED_COPY_TAIL_TABLE) != 0
        or tuple(
            memory.read_u32(PHASE7_ALIGNED_COPY_TAIL_TABLE + index * 4)
            for index in range(1, 16)
        )
        != PHASE7_ALIGNED_COPY_TAIL_TARGETS
    ):
        return {}
    return {site: tuple(sorted(PHASE7_ALIGNED_COPY_TAIL_TARGETS))}


def _phase7_memmove_jump_table_targets(
    memory: SparseMemory,
    instructions: Mapping[int, X86Instruction],
    indirect_sites: AbstractSet[int],
    allowed_targets: AbstractSet[int],
) -> dict[int, tuple[int, ...]]:
    """Validate every indexed branch in the optimized memmove routine."""

    sites = set(PHASE7_MEMMOVE_JUMP_BINDINGS)
    table_targets = {
        target
        for entries in PHASE7_MEMMOVE_JUMP_TABLES.values()
        for _index, target in entries
    }
    if not sites <= indirect_sites or not table_targets <= allowed_targets:
        return {}
    if (
        set(PHASE7_MEMMOVE_JUMP_SITE_BYTES) != sites
        or set(PHASE7_MEMMOVE_JUMP_REFERENCES) != set(PHASE7_MEMMOVE_JUMP_TABLES)
    ):
        return {}

    physical_entries: dict[int, int] = {}
    for table_base, entries in PHASE7_MEMMOVE_JUMP_TABLES.items():
        if not entries or len({index for index, _target in entries}) != len(entries):
            return {}
        for index, target in entries:
            address = _u32(table_base + index * 4)
            prior = physical_entries.setdefault(address, target)
            if prior != target or memory.read_u32(address) != target:
                return {}
    for address, encoded in PHASE7_MEMMOVE_JUMP_CONTROL_BYTES.items():
        expected = bytes.fromhex(encoded)
        if memory.read(address, len(expected)) != expected:
            return {}
    for address, encoded in PHASE7_MEMMOVE_JUMP_SITE_BYTES.items():
        instruction = instructions.get(address)
        if instruction is None or instruction.bytes_hex.upper() != encoded:
            return {}

    observed_references = {
        table_base: {
            address
            for address, instruction in instructions.items()
            if any(
                operand.kind == "mem"
                and operand.base is None
                and operand.index is not None
                and operand.scale == 4
                and operand.absolute is None
                and operand.segment is None
                and _u32(operand.displacement) == table_base
                for operand in instruction.operands
            )
        }
        for table_base in PHASE7_MEMMOVE_JUMP_TABLES
    }
    if observed_references != PHASE7_MEMMOVE_JUMP_REFERENCES:
        return {}

    recovered: dict[int, tuple[int, ...]] = {}
    for site, (table_base, index_register) in PHASE7_MEMMOVE_JUMP_BINDINGS.items():
        instruction = instructions.get(site)
        memory_operands = (
            []
            if instruction is None
            else [operand for operand in instruction.operands if operand.kind == "mem"]
        )
        if (
            instruction is None
            or instruction.mnemonic.casefold() not in {"jmp", "jmp_indirect"}
            or len(memory_operands) != 1
            or memory_operands[0].base is not None
            or memory_operands[0].index != index_register
            or memory_operands[0].scale != 4
            or memory_operands[0].absolute is not None
            or memory_operands[0].segment is not None
            or _u32(memory_operands[0].displacement) != table_base
        ):
            return {}
        recovered[site] = tuple(
            sorted({target for _index, target in PHASE7_MEMMOVE_JUMP_TABLES[table_base]})
        )
    return recovered


def _phase7_function_pair_helper_targets(
    memory: SparseMemory,
    instructions: Mapping[int, X86Instruction],
    indirect_sites: AbstractSet[int],
    allowed_targets: AbstractSet[int],
) -> dict[int, tuple[int, ...]]:
    """Validate and bind the captured function-pair helper family as one unit."""

    helper_sites = set(PHASE7_FUNCTION_PAIR_HELPER_TARGETS)
    if not helper_sites <= indirect_sites:
        return {}
    required_decoded_targets = {
        target for pair in PHASE7_FUNCTION_PAIR_TABLE for target in pair
    } | {target for targets in PHASE7_FUNCTION_PAIR_HELPER_TARGETS.values() for target in targets}
    if not required_decoded_targets <= allowed_targets:
        return {}
    for index, pair in enumerate(PHASE7_FUNCTION_PAIR_TABLE):
        for column, expected in enumerate(pair):
            address = PHASE7_FUNCTION_PAIR_TABLE_BASE + index * 8 + column * 4
            if memory.read_u32(address) != expected:
                return {}

    def register_call(address: int, register: str) -> bool:
        instruction = instructions.get(address)
        return bool(
            instruction is not None
            and instruction.mnemonic.casefold() == "call"
            and len(instruction.operands) == 1
            and instruction.operands[0].kind == "reg"
            and instruction.operands[0].reg == register
        )

    def table_call(address: int, base: str) -> bool:
        instruction = instructions.get(address)
        if (
            instruction is None
            or instruction.mnemonic.casefold() not in {"call", "jmp"}
            or len(instruction.operands) != 1
        ):
            return False
        operand = instruction.operands[0]
        return (
            operand.kind == "mem"
            and operand.base == base
            and operand.index is None
            and operand.scale == 1
            and operand.displacement == 4
            and operand.absolute is None
            and operand.segment is None
            and operand.size == 32
        )

    def register_immediate(address: int, register: str, value: int) -> bool:
        instruction = instructions.get(address)
        return bool(
            instruction is not None
            and instruction.mnemonic.casefold() == "mov"
            and len(instruction.operands) == 2
            and instruction.operands[0].kind == "reg"
            and instruction.operands[0].reg == register
            and instruction.operands[1].kind == "imm"
            and instruction.operands[1].immediate == value
        )

    def register_move(address: int, destination: str, source: str) -> bool:
        instruction = instructions.get(address)
        return bool(
            instruction is not None
            and instruction.mnemonic.casefold() == "mov"
            and len(instruction.operands) == 2
            and instruction.operands[0].kind == "reg"
            and instruction.operands[0].reg == destination
            and instruction.operands[1].kind == "reg"
            and instruction.operands[1].reg == source
        )

    def stack_load(address: int, destination: str, displacement: int) -> bool:
        instruction = instructions.get(address)
        if (
            instruction is None
            or instruction.mnemonic.casefold() != "mov"
            or len(instruction.operands) != 2
            or instruction.operands[0].kind != "reg"
            or instruction.operands[0].reg != destination
        ):
            return False
        operand = instruction.operands[1]
        return (
            operand.kind == "mem"
            and operand.base == "esp"
            and operand.index is None
            and operand.scale == 1
            and operand.displacement == displacement
            and operand.absolute is None
            and operand.segment is None
        )

    def push_immediate(address: int, value: int) -> bool:
        instruction = instructions.get(address)
        return bool(
            instruction is not None
            and instruction.mnemonic.casefold() == "push"
            and len(instruction.operands) == 1
            and instruction.operands[0].kind == "imm"
            and instruction.operands[0].immediate == value
        )

    def direct_call(address: int, target: int) -> bool:
        instruction = instructions.get(address)
        return bool(
            instruction is not None
            and instruction.mnemonic.casefold() == "call"
            and instruction.target == target
        )

    def table_row_lea(address: int, destination: str, index: str) -> bool:
        instruction = instructions.get(address)
        if (
            instruction is None
            or instruction.mnemonic.casefold() != "lea"
            or len(instruction.operands) != 2
            or instruction.operands[0].kind != "reg"
            or instruction.operands[0].reg != destination
        ):
            return False
        operand = instruction.operands[1]
        return (
            operand.kind == "mem"
            and operand.base == "eax"
            and operand.index == index
            and operand.scale == 8
            and operand.displacement == -8
            and operand.absolute is None
            and operand.segment is None
        )

    if not all(
        (
            register_call(0x0010A22D, "ebx"),
            table_call(0x0010A253, "edi"),
            register_call(0x0010A263, "eax"),
            table_call(0x0010A290, "esi"),
            register_call(0x0010A29C, "ebx"),
            stack_load(0x0010A211, "ebp", 0xC),
            stack_load(0x0010A21D, "edi", 0x10),
            stack_load(0x0010A24B, "eax", 0x10),
            stack_load(0x0010A256, "eax", 0x18),
            table_row_lea(0x0010A24F, "edi", "esi"),
            register_move(0x0010A281, "edi", "ecx"),
            table_row_lea(0x0010A288, "esi", "edi"),
            push_immediate(0x0010A4A0, 0x0010A2F0),
            push_immediate(0x0010A4A5, len(PHASE7_FUNCTION_PAIR_TABLE)),
            push_immediate(0x0010A4A7, PHASE7_FUNCTION_PAIR_TABLE_BASE),
            register_immediate(0x0010A4AC, "ebx", 0x0010A2B0),
            direct_call(0x0010A4BC, 0x0010A210),
            register_immediate(0x0010A50A, "ebx", 0x0010A2F0),
            register_immediate(0x0010A50F, "ecx", len(PHASE7_FUNCTION_PAIR_TABLE)),
            register_immediate(0x0010A514, "eax", PHASE7_FUNCTION_PAIR_TABLE_BASE),
            direct_call(0x0010A519, 0x0010A280),
        )
    ):
        return {}
    return dict(PHASE7_FUNCTION_PAIR_HELPER_TARGETS)


def _phase7_collision_dispatch_targets(
    memory: SparseMemory,
    instructions: Mapping[int, X86Instruction],
    indirect_sites: AbstractSet[int],
    allowed_targets: AbstractSet[int],
) -> dict[int, tuple[int, ...]]:
    """Validate the finite collision primitive dispatch table as one family."""

    site = PHASE7_COLLISION_DISPATCH_SITE
    if site not in indirect_sites:
        return {}
    targets = tuple(
        sorted(
            {
                target
                for row in PHASE7_COLLISION_DISPATCH_TABLE
                for target in row
                if target
            }
        )
    )
    if (
        not set(targets) <= allowed_targets
        or PHASE7_COLLISION_DISPATCH_OBSERVED_BOUNDARY != (site, 0x0006A550)
        or PHASE7_COLLISION_DISPATCH_OBSERVED_BOUNDARY[1] not in targets
    ):
        return {}
    for row, entries in enumerate(PHASE7_COLLISION_DISPATCH_TABLE):
        for column, expected in enumerate(entries):
            address = PHASE7_COLLISION_DISPATCH_TABLE_BASE + (row * 4 + column) * 4
            if memory.read_u32(address) != expected:
                return {}
    helper_payload = memory.read(
        PHASE7_COLLISION_DISPATCH_HELPER_START,
        PHASE7_COLLISION_DISPATCH_HELPER_END
        - PHASE7_COLLISION_DISPATCH_HELPER_START,
    )
    if hashlib.sha256(helper_payload).hexdigest() != PHASE7_COLLISION_DISPATCH_HELPER_SHA256:
        return {}
    instruction = instructions.get(site)
    if (
        instruction is None
        or instruction.mnemonic.casefold() != "call"
        or len(instruction.operands) != 1
        or instruction.operands[0].kind != "reg"
        or instruction.operands[0].reg != "eax"
    ):
        return {}
    direct_callers = {
        address
        for address, candidate in instructions.items()
        if candidate.mnemonic.casefold() == "call"
        and candidate.target == PHASE7_COLLISION_DISPATCH_HELPER_START
    }
    if direct_callers != set(PHASE7_COLLISION_DISPATCH_CALLERS):
        return {}
    return {site: targets}


def _phase7_base_manager_object_targets(
    memory: SparseMemory,
    instructions: Mapping[int, X86Instruction],
    indirect_sites: AbstractSet[int],
    allowed_targets: AbstractSet[int],
) -> dict[int, tuple[int, ...]]:
    """Validate the complete finite base-manager entry vtable family."""

    sites = set(PHASE7_BASE_MANAGER_OBJECT_BINDINGS)
    targets = {target for _slot, target in PHASE7_BASE_MANAGER_OBJECT_BINDINGS.values()}
    vtable_targets = set(PHASE7_BASE_MANAGER_OBJECT_VTABLE_ENTRIES)
    if (
        not sites <= indirect_sites
        or not targets <= allowed_targets
        or not vtable_targets <= allowed_targets
    ):
        return {}
    for address, expected in PHASE7_BASE_MANAGER_OBJECT_INSTRUCTION_BYTES.items():
        instruction = instructions.get(address)
        if instruction is None or instruction.bytes_hex.upper() != expected:
            return {}
    for index, entry in enumerate(PHASE7_BASE_MANAGER_OBJECT_VTABLE_ENTRIES):
        if memory.read_u32(PHASE7_BASE_MANAGER_OBJECT_VTABLE + index * 4) != entry:
            return {}
    for slot, target in PHASE7_BASE_MANAGER_OBJECT_BINDINGS.values():
        if memory.read_u32(PHASE7_BASE_MANAGER_OBJECT_VTABLE + slot) != target:
            return {}

    installers = {
        address
        for address, instruction in instructions.items()
        if instruction.mnemonic.casefold() == "mov"
        and any(
            operand.kind == "imm" and operand.immediate == PHASE7_BASE_MANAGER_OBJECT_VTABLE
            for operand in instruction.operands
        )
    }
    if installers != {0x00139390}:
        return {}
    direct_callers = {
        address
        for address, instruction in instructions.items()
        if instruction.mnemonic.casefold() == "call" and instruction.target in vtable_targets
    }
    if direct_callers:
        return {}
    return {
        site: (target,) for site, (_slot, target) in PHASE7_BASE_MANAGER_OBJECT_BINDINGS.items()
    }


def _phase7_game_state_object_targets(
    memory: SparseMemory,
    instructions: Mapping[int, X86Instruction],
    indirect_sites: AbstractSet[int],
    allowed_targets: AbstractSet[int],
) -> dict[int, tuple[int, ...]]:
    """Validate the complete static game-state registry and its dispatches."""

    objects = {
        index: (cell, object_address, vtable, entries)
        for index, cell, object_address, vtable, entries in PHASE7_GAME_STATE_OBJECTS
    }
    if set(objects) != set(range(0x10)):
        return {}
    if {
        cell for cell, _object, _vtable, _entries in objects.values()
    } != set(range(0x002FE318, 0x002FE358, 4)):
        return {}

    concrete_entries: list[tuple[int, ...]] = []
    for index, (cell, object_address, vtable, entries) in objects.items():
        if memory.read_u32(cell) != object_address:
            return {}
        if object_address == 0:
            if index != 0x09 or vtable != 0 or entries:
                return {}
            continue
        if vtable == 0 or len(entries) != 16 or memory.read_u32(object_address) != vtable:
            return {}
        for slot, target in enumerate(entries):
            if memory.read_u32(vtable + slot * 4) != target:
                return {}
        concrete_entries.append(entries)

    all_targets = {target for entries in concrete_entries for target in entries}
    if not all_targets <= allowed_targets:
        return {}

    transition_code = memory.read(
        PHASE7_GAME_STATE_TRANSITION_CODE_START,
        PHASE7_GAME_STATE_TRANSITION_CODE_END
        - PHASE7_GAME_STATE_TRANSITION_CODE_START,
    )
    if (
        hashlib.sha256(transition_code).hexdigest()
        != PHASE7_GAME_STATE_TRANSITION_CODE_SHA256
    ):
        return {}

    active_references = tuple(
        sorted(
            address
            for address, instruction in instructions.items()
            if any(
                operand.absolute == PHASE7_GAME_STATE_ACTIVE_OBJECT_CELL
                for operand in instruction.operands
            )
        )
    )
    if (
        len(active_references) != PHASE7_GAME_STATE_REFERENCE_COUNT
        or hashlib.sha256(
            b"".join(struct.pack("<I", address) for address in active_references)
        ).hexdigest()
        != PHASE7_GAME_STATE_REFERENCE_SHA256
    ):
        return {}

    ordered_addresses = sorted(instructions)
    positions = {address: index for index, address in enumerate(ordered_addresses)}

    def writes_register(instruction: X86Instruction, register: str) -> bool:
        return bool(
            instruction.operands
            and instruction.operands[0].kind == "reg"
            and instruction.operands[0].reg == register
        )

    derived_slots: dict[int, int] = {}
    for site in sorted(indirect_sites):
        instruction = instructions.get(site)
        if instruction is None or instruction.mnemonic.casefold() not in {"call", "jmp"}:
            continue
        memory_operands = [operand for operand in instruction.operands if operand.kind == "mem"]
        if len(memory_operands) != 1:
            continue
        call_operand = memory_operands[0]
        if (
            call_operand.base is None
            or call_operand.index is not None
            or call_operand.displacement not in range(0, 0x40, 4)
        ):
            continue

        position = positions.get(site)
        if position is None:
            continue
        vtable_load: X86Instruction | None = None
        vtable_position = -1
        for previous_position in range(position - 1, max(-1, position - 20), -1):
            previous = instructions[ordered_addresses[previous_position]]
            if site - previous.address > 0x70 or previous.mnemonic.casefold() in {"ret", "jmp"}:
                break
            if writes_register(previous, call_operand.base):
                vtable_load = previous
                vtable_position = previous_position
                break
        if vtable_load is None or vtable_load.mnemonic.casefold() != "mov":
            continue
        if len(vtable_load.operands) != 2:
            continue
        vtable_source = vtable_load.operands[1]
        if (
            vtable_source.kind != "mem"
            or vtable_source.base is None
            or vtable_source.index is not None
            or vtable_source.displacement != 0
        ):
            continue

        object_load: X86Instruction | None = None
        for previous_position in range(
            vtable_position - 1,
            max(-1, vtable_position - 20),
            -1,
        ):
            previous = instructions[ordered_addresses[previous_position]]
            if (
                vtable_load.address - previous.address > 0x70
                or previous.mnemonic.casefold() in {"ret", "jmp"}
            ):
                break
            if writes_register(previous, vtable_source.base):
                object_load = previous
                break
        if object_load is None or object_load.mnemonic.casefold() != "mov":
            continue
        if len(object_load.operands) != 2:
            continue
        object_source = object_load.operands[1]
        if (
            object_source.kind != "mem"
            or object_source.absolute != PHASE7_GAME_STATE_ACTIVE_OBJECT_CELL
        ):
            continue
        derived_slots[site] = call_operand.displacement

    derived_sites = tuple(sorted(derived_slots))
    if (
        len(derived_sites) != PHASE7_GAME_STATE_DERIVED_SITE_COUNT
        or hashlib.sha256(
            b"".join(struct.pack("<I", site) for site in derived_sites)
        ).hexdigest()
        != PHASE7_GAME_STATE_DERIVED_SITE_SHA256
    ):
        return {}

    recovered = {
        site: tuple(sorted({entries[slot // 4] for entries in concrete_entries}))
        for site, slot in derived_slots.items()
    }
    for site, (slot, exact_index) in PHASE7_GAME_STATE_SPECIAL_BINDINGS.items():
        instruction = instructions.get(site)
        memory_operands = (
            []
            if instruction is None
            else [operand for operand in instruction.operands if operand.kind == "mem"]
        )
        if (
            instruction is None
            or instruction.bytes_hex.upper() != PHASE7_GAME_STATE_SPECIAL_SITE_BYTES[site]
            or instruction.mnemonic.casefold() not in {"call", "jmp"}
            or len(memory_operands) != 1
            or memory_operands[0].displacement != slot
        ):
            return {}
        if exact_index is None:
            recovered[site] = tuple(
                sorted({entries[slot // 4] for entries in concrete_entries})
            )
        else:
            exact_entries = objects[exact_index][3]
            recovered[site] = (exact_entries[slot // 4],)

    helper_code = memory.read(
        PHASE7_GAME_STATE_HELPER_START,
        PHASE7_GAME_STATE_HELPER_END - PHASE7_GAME_STATE_HELPER_START,
    )
    if hashlib.sha256(helper_code).hexdigest() != PHASE7_GAME_STATE_HELPER_SHA256:
        return {}
    helper_callers = {
        address
        for address, instruction in instructions.items()
        if instruction.mnemonic.casefold() == "call"
        and instruction.target == PHASE7_GAME_STATE_HELPER_START
    }
    if helper_callers != set(PHASE7_GAME_STATE_HELPER_CALLERS):
        return {}
    helper_methods = {
        method for method, _sha256 in PHASE7_GAME_STATE_HELPER_CALLERS.values()
    }
    if not helper_methods <= all_targets:
        return {}
    for call_site, (method, caller_sha256) in PHASE7_GAME_STATE_HELPER_CALLERS.items():
        instruction = instructions.get(call_site)
        if (
            instruction is None
            or instruction.mnemonic.casefold() != "call"
            or instruction.target != PHASE7_GAME_STATE_HELPER_START
            or method >= call_site
            or hashlib.sha256(memory.read(method, call_site + 5 - method)).hexdigest()
            != caller_sha256
        ):
            return {}
    helper_state_indexes = tuple(
        index
        for index, (_cell, object_address, _vtable, entries) in objects.items()
        if object_address and helper_methods.intersection(entries)
    )
    if helper_state_indexes != PHASE7_GAME_STATE_HELPER_STATE_INDEXES:
        return {}
    helper_entries = [objects[index][3] for index in helper_state_indexes]
    for site, (slot, site_bytes) in PHASE7_GAME_STATE_HELPER_BINDINGS.items():
        instruction = instructions.get(site)
        memory_operands = (
            []
            if instruction is None
            else [operand for operand in instruction.operands if operand.kind == "mem"]
        )
        if (
            instruction is None
            or instruction.mnemonic.casefold() != "call"
            or instruction.bytes_hex.upper() != site_bytes
            or len(memory_operands) != 1
            or memory_operands[0].displacement != slot
        ):
            return {}
        recovered[site] = tuple(
            sorted({entries[slot // 4] for entries in helper_entries})
        )

    sites = tuple(sorted(recovered))
    boundary_site, boundary_index, boundary_slot, boundary_target = (
        PHASE7_GAME_STATE_OBSERVED_BOUNDARY
    )
    if (
        len(sites) != PHASE7_GAME_STATE_SITE_COUNT
        or hashlib.sha256(
            b"".join(struct.pack("<I", site) for site in sites)
        ).hexdigest()
        != PHASE7_GAME_STATE_SITE_SHA256
        or not set(sites) <= indirect_sites
        or not {target for targets in recovered.values() for target in targets}
        <= allowed_targets
        or objects[boundary_index][3][boundary_slot // 4] != boundary_target
        or boundary_target not in recovered.get(boundary_site, ())
    ):
        return {}
    return recovered


def _phase7_level_load_strategy_targets(
    memory: SparseMemory,
    instructions: Mapping[int, X86Instruction],
    indirect_sites: AbstractSet[int],
    allowed_targets: AbstractSet[int],
) -> dict[int, tuple[int, ...]]:
    """Validate the complete finite level-load strategy-object family."""

    sites = set(PHASE7_LEVEL_LOAD_STRATEGY_BINDINGS)
    vtables = {
        vtable: entries for _object_address, vtable, entries in PHASE7_LEVEL_LOAD_STRATEGY_OBJECTS
    }
    vtable_targets = {target for entries in vtables.values() for target in entries}
    if not sites <= indirect_sites or not vtable_targets <= allowed_targets:
        return {}
    if set(PHASE7_LEVEL_LOAD_STRATEGY_SITE_BYTES) != sites:
        return {}

    for object_address, vtable, entries in PHASE7_LEVEL_LOAD_STRATEGY_OBJECTS:
        if memory.read_u32(object_address) != vtable or vtables.get(vtable) != entries:
            return {}
        for index, target in enumerate(entries):
            if memory.read_u32(vtable + index * 4) != target:
                return {}

    caller_starts = {
        caller_start
        for caller_start, _vtable, _slot in PHASE7_LEVEL_LOAD_STRATEGY_BINDINGS.values()
    }
    if caller_starts != set(PHASE7_LEVEL_LOAD_STRATEGY_CALLER_BYTES):
        return {}
    for caller_start, encoded in PHASE7_LEVEL_LOAD_STRATEGY_CALLER_BYTES.items():
        expected = bytes.fromhex(encoded)
        if memory.read(caller_start, len(expected)) != expected:
            return {}
    for installer_start, encoded in PHASE7_LEVEL_LOAD_STRATEGY_INSTALLER_BYTES.items():
        expected = bytes.fromhex(encoded)
        if memory.read(installer_start, len(expected)) != expected:
            return {}
    for address, encoded in PHASE7_LEVEL_LOAD_STRATEGY_REFERENCE_BYTES.items():
        instruction = instructions.get(address)
        if instruction is None or instruction.bytes_hex.upper() != encoded:
            return {}

    object_addresses = set(PHASE7_LEVEL_LOAD_STRATEGY_REFERENCES)
    observed_references = {
        object_address: {
            address
            for address, instruction in instructions.items()
            if any(
                (
                    operand.kind == "imm"
                    and operand.immediate == object_address
                )
                or (
                    operand.kind == "mem"
                    and operand.absolute == object_address
                )
                for operand in instruction.operands
            )
        }
        for object_address in object_addresses
    }
    if observed_references != PHASE7_LEVEL_LOAD_STRATEGY_REFERENCES:
        return {}

    recovered: dict[int, tuple[int, ...]] = {}
    for site, (caller_start, vtable, slot) in PHASE7_LEVEL_LOAD_STRATEGY_BINDINGS.items():
        instruction = instructions.get(site)
        encoded = PHASE7_LEVEL_LOAD_STRATEGY_SITE_BYTES[site]
        caller_end = caller_start + len(
            bytes.fromhex(PHASE7_LEVEL_LOAD_STRATEGY_CALLER_BYTES[caller_start])
        )
        memory_operands = (
            []
            if instruction is None
            else [operand for operand in instruction.operands if operand.kind == "mem"]
        )
        if (
            instruction is None
            or instruction.bytes_hex.upper() != encoded
            or instruction.mnemonic.casefold() not in {"call", "jmp"}
            or len(memory_operands) != 1
            or memory_operands[0].displacement != slot
            or not caller_start <= site < caller_end
        ):
            return {}
        if vtable:
            selected_entries = vtables.get(vtable)
            if selected_entries is None:
                return {}
            site_targets: tuple[int, ...] = (selected_entries[slot // 4],)
        else:
            site_targets = tuple(
                sorted({table_entries[slot // 4] for table_entries in vtables.values()})
            )
        if not set(site_targets) <= allowed_targets:
            return {}
        recovered[site] = site_targets
    return recovered


def _phase7_level_parameter_bank_targets(
    memory: SparseMemory,
    instructions: Mapping[int, X86Instruction],
    indirect_sites: AbstractSet[int],
    allowed_targets: AbstractSet[int],
) -> dict[int, tuple[int, ...]]:
    """Validate the complete fixed 32-object level-parameter dispatch bank."""

    code = memory.read(
        PHASE7_LEVEL_PARAMETER_BANK_CODE_START,
        PHASE7_LEVEL_PARAMETER_BANK_CODE_END
        - PHASE7_LEVEL_PARAMETER_BANK_CODE_START,
    )
    if hashlib.sha256(code).hexdigest() != PHASE7_LEVEL_PARAMETER_BANK_CODE_SHA256:
        return {}

    objects = {
        field: (object_address, vtable, entries)
        for field, object_address, vtable, entries in (
            PHASE7_LEVEL_PARAMETER_BANK_OBJECTS
        )
    }
    if set(objects) != set(range(0x10, 0x90, 4)):
        return {}
    for _field, (object_address, vtable, entries) in objects.items():
        if memory.read_u32(object_address) != vtable or len(entries) != 3:
            return {}
        for index, target in enumerate(entries):
            if memory.read_u32(vtable + index * 4) != target:
                return {}

    generic_sites = set(PHASE7_LEVEL_PARAMETER_BANK_GENERIC_BINDINGS)
    all_targets = {
        target
        for _object_address, _vtable, entries in objects.values()
        for target in entries
    }
    if not generic_sites <= indirect_sites or not all_targets <= allowed_targets:
        return {}
    generic_bytes = {
        0x0001AB3F: "FF12",
        0x0001ABEB: "FF5208",
        0x0001AC27: "FF5004",
    }
    recovered: dict[int, tuple[int, ...]] = {}
    for site, slot in PHASE7_LEVEL_PARAMETER_BANK_GENERIC_BINDINGS.items():
        instruction = instructions.get(site)
        memory_operands = (
            []
            if instruction is None
            else [operand for operand in instruction.operands if operand.kind == "mem"]
        )
        if (
            instruction is None
            or instruction.bytes_hex.upper() != generic_bytes[site]
            or instruction.mnemonic.casefold() != "call"
            or len(memory_operands) != 1
            or memory_operands[0].displacement != slot
        ):
            return {}
        recovered[site] = tuple(
            sorted({entries[slot // 4] for _object, _vtable, entries in objects.values()})
        )

    region = sorted(
        (
            instruction
            for address, instruction in instructions.items()
            if PHASE7_LEVEL_PARAMETER_BANK_SETTER_START
            <= address
            < PHASE7_LEVEL_PARAMETER_BANK_CODE_END
        ),
        key=lambda instruction: instruction.address,
    )
    direct_targets: dict[int, tuple[int, ...]] = {}
    for position, instruction in enumerate(region):
        memory_operands = [
            operand for operand in instruction.operands if operand.kind == "mem"
        ]
        if (
            instruction.mnemonic.casefold() != "call"
            or len(memory_operands) != 1
            or memory_operands[0].base is None
            or memory_operands[0].displacement != 4
        ):
            continue

        vtable_register = memory_operands[0].base
        vtable_load: X86Instruction | None = None
        vtable_position = -1
        for previous_position in range(position - 1, max(-1, position - 30), -1):
            previous = region[previous_position]
            if (
                instruction.address - previous.address > 0x90
                or previous.mnemonic.casefold() in {"ret", "jmp"}
            ):
                break
            if (
                previous.operands
                and previous.operands[0].kind == "reg"
                and previous.operands[0].reg == vtable_register
            ):
                vtable_load = previous
                vtable_position = previous_position
                break
        if vtable_load is None or vtable_load.mnemonic.casefold() != "mov":
            continue
        if len(vtable_load.operands) != 2:
            continue
        vtable_source = vtable_load.operands[1]
        if (
            vtable_source.kind != "mem"
            or vtable_source.base is None
            or vtable_source.index is not None
            or vtable_source.displacement != 0
        ):
            continue

        object_register = vtable_source.base
        object_load: X86Instruction | None = None
        for previous_position in range(
            vtable_position - 1,
            max(-1, vtable_position - 20),
            -1,
        ):
            previous = region[previous_position]
            if (
                vtable_load.address - previous.address > 0x60
                or previous.mnemonic.casefold() in {"ret", "jmp"}
            ):
                break
            if (
                previous.operands
                and previous.operands[0].kind == "reg"
                and previous.operands[0].reg == object_register
            ):
                object_load = previous
                break
        if object_load is None or object_load.mnemonic.casefold() != "mov":
            continue
        if len(object_load.operands) != 2:
            continue
        object_source = object_load.operands[1]
        if (
            object_source.kind != "mem"
            or object_source.base != "esi"
            or object_source.index is not None
            or object_source.displacement not in objects
        ):
            continue
        entries = objects[object_source.displacement][2]
        direct_targets[instruction.address] = (entries[1],)

    direct_sites = tuple(sorted(direct_targets))
    direct_site_sha256 = hashlib.sha256(
        b"".join(struct.pack("<I", site) for site in direct_sites)
    ).hexdigest()
    boundary_site, boundary_field, boundary_target = (
        PHASE7_LEVEL_PARAMETER_BANK_OBSERVED_BOUNDARY
    )
    if (
        len(direct_sites) != PHASE7_LEVEL_PARAMETER_BANK_SETTER_SITE_COUNT
        or direct_site_sha256
        != PHASE7_LEVEL_PARAMETER_BANK_SETTER_SITE_SHA256
        or objects[boundary_field][2][1] != boundary_target
        or direct_targets.get(boundary_site) != (boundary_target,)
        or not set(direct_sites) <= indirect_sites
        or not {
            target for targets in direct_targets.values() for target in targets
        }
        <= allowed_targets
    ):
        return {}
    recovered.update(direct_targets)
    return recovered


def _phase7_level_load_manager_targets(
    memory: SparseMemory,
    instructions: Mapping[int, X86Instruction],
    indirect_sites: AbstractSet[int],
    allowed_targets: AbstractSet[int],
) -> dict[int, tuple[int, ...]]:
    """Validate both fixed level-load manager arrays and self-dispatches."""

    sites = set(PHASE7_LEVEL_LOAD_MANAGER_BINDINGS)
    vtables = {vtable: entries for vtable, entries in PHASE7_LEVEL_LOAD_MANAGER_VTABLES}
    vtable_targets = {target for entries in vtables.values() for target in entries}
    if not sites <= indirect_sites or not vtable_targets <= allowed_targets:
        return {}
    if set(PHASE7_LEVEL_LOAD_MANAGER_SITE_BYTES) != sites:
        return {}
    if not set(PHASE7_LEVEL_LOAD_MANAGER_COMMON_VTABLES) <= set(vtables):
        return {}

    for vtable, entries in PHASE7_LEVEL_LOAD_MANAGER_VTABLES:
        for index, target in enumerate(entries):
            if memory.read_u32(vtable + index * 4) != target:
                return {}
    for object_address, vtable in PHASE7_LEVEL_LOAD_MANAGER_OBJECT_BINDINGS:
        if memory.read_u32(object_address) != vtable:
            return {}

    caller_starts = {
        caller_start
        for caller_start, _vtable, _slot in PHASE7_LEVEL_LOAD_MANAGER_BINDINGS.values()
    }
    if caller_starts != set(PHASE7_LEVEL_LOAD_MANAGER_CALLER_BYTES):
        return {}
    for caller_start, encoded in PHASE7_LEVEL_LOAD_MANAGER_CALLER_BYTES.items():
        expected = bytes.fromhex(encoded)
        if memory.read(caller_start, len(expected)) != expected:
            return {}
    for address, encoded in PHASE7_LEVEL_LOAD_MANAGER_SWEEP_PROOF_BYTES.items():
        expected = bytes.fromhex(encoded)
        if memory.read(address, len(expected)) != expected:
            return {}
    sweep_helpers = {
        helper
        for helper, _prefix_start, _encoded in (
            PHASE7_LEVEL_LOAD_MANAGER_SWEEP_DIRECT_CALLERS.values()
        )
    }
    observed_sweep_callers = {
        helper: {
            address
            for address, instruction in instructions.items()
            if instruction.mnemonic.casefold() == "call"
            and instruction.target == helper
        }
        for helper in sweep_helpers
    }
    expected_sweep_callers = {
        helper: {
            call_site
            for call_site, (candidate, _prefix_start, _encoded) in (
                PHASE7_LEVEL_LOAD_MANAGER_SWEEP_DIRECT_CALLERS.items()
            )
            if candidate == helper
        }
        for helper in sweep_helpers
    }
    if observed_sweep_callers != expected_sweep_callers:
        return {}
    for call_site, (helper, prefix_start, encoded) in (
        PHASE7_LEVEL_LOAD_MANAGER_SWEEP_DIRECT_CALLERS.items()
    ):
        expected = bytes.fromhex(encoded)
        instruction = instructions.get(call_site)
        if (
            instruction is None
            or instruction.mnemonic.casefold() != "call"
            or instruction.target != helper
            or prefix_start + len(expected) != call_site + 5
            or memory.read(prefix_start, len(expected)) != expected
        ):
            return {}
    for installer_start, encoded in PHASE7_LEVEL_LOAD_MANAGER_INSTALLER_BYTES.items():
        expected = bytes.fromhex(encoded)
        if memory.read(installer_start, len(expected)) != expected:
            return {}
    for address, encoded in PHASE7_LEVEL_LOAD_MANAGER_REFERENCE_BYTES.items():
        instruction = instructions.get(address)
        if instruction is None or instruction.bytes_hex.upper() != encoded:
            return {}

    vtable_addresses = set(PHASE7_LEVEL_LOAD_MANAGER_REFERENCES)
    observed_references = {
        vtable: {
            address
            for address, instruction in instructions.items()
            if any(
                (operand.kind == "imm" and operand.immediate == vtable)
                or (operand.kind == "mem" and operand.absolute == vtable)
                for operand in instruction.operands
            )
        }
        for vtable in vtable_addresses
    }
    if observed_references != PHASE7_LEVEL_LOAD_MANAGER_REFERENCES:
        return {}

    recovered: dict[int, tuple[int, ...]] = {}
    for site, (caller_start, vtable, slot) in PHASE7_LEVEL_LOAD_MANAGER_BINDINGS.items():
        instruction = instructions.get(site)
        encoded = PHASE7_LEVEL_LOAD_MANAGER_SITE_BYTES[site]
        caller_end = caller_start + len(
            bytes.fromhex(PHASE7_LEVEL_LOAD_MANAGER_CALLER_BYTES[caller_start])
        )
        memory_operands = (
            []
            if instruction is None
            else [operand for operand in instruction.operands if operand.kind == "mem"]
        )
        if (
            instruction is None
            or instruction.bytes_hex.upper() != encoded
            or instruction.mnemonic.casefold() not in {"call", "jmp"}
            or len(memory_operands) != 1
            or memory_operands[0].displacement != slot
            or not caller_start <= site < caller_end
        ):
            return {}
        if vtable:
            selected_entries = vtables.get(vtable)
            if selected_entries is None or slot // 4 >= len(selected_entries):
                return {}
            site_targets: tuple[int, ...] = (selected_entries[slot // 4],)
        else:
            common_entries = [vtables[item] for item in PHASE7_LEVEL_LOAD_MANAGER_COMMON_VTABLES]
            if any(slot // 4 >= len(table_entries) for table_entries in common_entries):
                return {}
            site_targets = tuple(
                sorted({table_entries[slot // 4] for table_entries in common_entries})
            )
        if not set(site_targets) <= allowed_targets:
            return {}
        recovered[site] = site_targets
    boundary_site, boundary_vtable, boundary_slot, boundary_target = (
        PHASE7_LEVEL_LOAD_MANAGER_SWEEP_OBSERVED_BOUNDARY
    )
    if (
        vtables[boundary_vtable][boundary_slot // 4] != boundary_target
        or recovered.get(boundary_site) != (boundary_target,)
    ):
        return {}
    return recovered


def _phase7_level_update_primary_targets(
    memory: SparseMemory,
    instructions: Mapping[int, X86Instruction],
    indirect_sites: AbstractSet[int],
    allowed_targets: AbstractSet[int],
) -> dict[int, tuple[int, ...]]:
    """Validate all primary slot-0x18 calls in the level-update switch."""

    target = dict(PHASE7_LEVEL_LOAD_MANAGER_VTABLES)[
        PHASE7_LEVEL_UPDATE_PRIMARY_VTABLE
    ][0x18 // 4]
    sites = set(PHASE7_LEVEL_UPDATE_PRIMARY_BINDINGS)
    if (
        not sites <= indirect_sites
        or target not in allowed_targets
        or not sites.isdisjoint(PHASE7_LEVEL_UPDATE_SECONDARY_GUARDED_SITES)
        or memory.read_u32(PHASE7_LEVEL_UPDATE_PRIMARY_CELL)
        not in PHASE7_LEVEL_UPDATE_PRIMARY_OBJECTS
    ):
        return {}

    vtable_entries = dict(PHASE7_LEVEL_LOAD_MANAGER_VTABLES)[
        PHASE7_LEVEL_UPDATE_PRIMARY_VTABLE
    ]
    for object_address in PHASE7_LEVEL_UPDATE_PRIMARY_OBJECTS:
        if memory.read_u32(object_address) != PHASE7_LEVEL_UPDATE_PRIMARY_VTABLE:
            return {}
    for index, entry in enumerate(vtable_entries):
        if memory.read_u32(PHASE7_LEVEL_UPDATE_PRIMARY_VTABLE + index * 4) != entry:
            return {}

    if memory.read(
        PHASE7_LEVEL_UPDATE_SELECTOR_ADDRESS,
        len(bytes.fromhex(PHASE7_LEVEL_UPDATE_SELECTOR_BYTES)),
    ) != bytes.fromhex(PHASE7_LEVEL_UPDATE_SELECTOR_BYTES):
        return {}
    if tuple(
        memory.read_u32(PHASE7_LEVEL_UPDATE_JUMP_TABLE_ADDRESS + index * 4)
        for index in range(len(PHASE7_LEVEL_UPDATE_JUMP_TABLE))
    ) != PHASE7_LEVEL_UPDATE_JUMP_TABLE:
        return {}

    for address, encoded in PHASE7_LEVEL_UPDATE_SWITCH_INSTRUCTION_BYTES.items():
        instruction = instructions.get(address)
        if instruction is None or instruction.bytes_hex.upper() != encoded:
            return {}
    for address, (_case_entry, helper) in PHASE7_LEVEL_UPDATE_HELPER_CALLS.items():
        instruction = instructions[address]
        if instruction.mnemonic.casefold() != "call" or instruction.target != helper:
            return {}

    expected_helper_references = {
        helper: {address}
        for address, (_case_entry, helper) in PHASE7_LEVEL_UPDATE_HELPER_CALLS.items()
    }
    observed_helper_references = {
        helper: {
            address
            for address, instruction in instructions.items()
            if instruction.target == helper
        }
        for helper in expected_helper_references
    }
    if observed_helper_references != expected_helper_references:
        return {}

    helper_sites: dict[int, set[int]] = {}
    for site, (helper, caller_start) in PHASE7_LEVEL_UPDATE_PRIMARY_BINDINGS.items():
        instruction = instructions.get(site)
        caller_payload = bytes.fromhex(
            PHASE7_LEVEL_UPDATE_PRIMARY_CALLER_BYTES[caller_start]
        )
        memory_operands = (
            []
            if instruction is None
            else [operand for operand in instruction.operands if operand.kind == "mem"]
        )
        if (
            instruction is None
            or instruction.mnemonic.casefold() != "call"
            or instruction.bytes_hex.upper() not in {"FF5018", "FF5218"}
            or len(memory_operands) != 1
            or memory_operands[0].displacement != 0x18
            or site + instruction.size != caller_start + len(caller_payload)
            or memory.read(caller_start, len(caller_payload)) != caller_payload
        ):
            return {}
        helper_sites.setdefault(helper, set()).add(site)
    if helper_sites != {
        helper: {site}
        for site, (helper, _caller_start) in PHASE7_LEVEL_UPDATE_PRIMARY_BINDINGS.items()
    }:
        return {}
    if {
        helper for _case_entry, helper in PHASE7_LEVEL_UPDATE_HELPER_CALLS.values()
    } != set(helper_sites):
        return {}
    if not all(
        PHASE7_LEVEL_UPDATE_PRIMARY_BINDINGS[site][0] == helper
        for _switch_call, (_case_entry, helper) in PHASE7_LEVEL_UPDATE_HELPER_CALLS.items()
        for site in helper_sites[helper]
    ):
        return {}
    if not {
        boundary for boundary in PHASE7_LEVEL_UPDATE_OBSERVED_BOUNDARIES
    } <= {(site, target) for site in sites}:
        return {}
    return {site: (target,) for site in sites}


def _phase7_level_sample_primary_targets(
    memory: SparseMemory,
    instructions: Mapping[int, X86Instruction],
    indirect_sites: AbstractSet[int],
    allowed_targets: AbstractSet[int],
) -> dict[int, tuple[int, ...]]:
    """Validate the three primary position queries in the fixed sample loop."""

    sites = set(PHASE7_LEVEL_SAMPLE_PRIMARY_BINDINGS)
    target = dict(PHASE7_LEVEL_LOAD_MANAGER_VTABLES)[
        PHASE7_LEVEL_UPDATE_PRIMARY_VTABLE
    ][0x18 // 4]
    if not sites <= indirect_sites or target not in allowed_targets:
        return {}

    vtable_entries = dict(PHASE7_LEVEL_LOAD_MANAGER_VTABLES)[
        PHASE7_LEVEL_UPDATE_PRIMARY_VTABLE
    ]
    for index, object_address in enumerate(PHASE7_LEVEL_UPDATE_PRIMARY_OBJECTS):
        bound_object = memory.read_u32(
            PHASE7_LEVEL_UPDATE_PRIMARY_CELL + index * 4
        )
        if (
            (index == 0 and bound_object != object_address)
            or (index != 0 and bound_object not in {0, object_address})
            or memory.read_u32(object_address)
            != PHASE7_LEVEL_UPDATE_PRIMARY_VTABLE
        ):
            return {}
    for index, entry in enumerate(vtable_entries):
        if memory.read_u32(PHASE7_LEVEL_UPDATE_PRIMARY_VTABLE + index * 4) != entry:
            return {}

    for address, encoded in PHASE7_LEVEL_SAMPLE_PROOF_BYTES.items():
        payload = bytes.fromhex(encoded)
        if memory.read(address, len(payload)) != payload:
            return {}

    for site, base in PHASE7_LEVEL_SAMPLE_PRIMARY_BINDINGS.items():
        instruction = instructions.get(site)
        memory_operands = (
            []
            if instruction is None
            else [operand for operand in instruction.operands if operand.kind == "mem"]
        )
        if (
            instruction is None
            or instruction.mnemonic.casefold() != "call"
            or instruction.bytes_hex.upper()
            != ("FF5018" if base == "eax" else "FF5218")
            or len(memory_operands) != 1
            or memory_operands[0].base != base
            or memory_operands[0].displacement != 0x18
        ):
            return {}

    observed_direct_calls = {
        address: instruction.target
        for address, instruction in instructions.items()
        if instruction.target == 0x0009D160
    }
    if observed_direct_calls != PHASE7_LEVEL_SAMPLE_DIRECT_CALLS:
        return {}
    for address, target_address in PHASE7_LEVEL_SAMPLE_DIRECT_CALLS.items():
        instruction = instructions.get(address)
        if (
            instruction is None
            or instruction.mnemonic.casefold() != "call"
            or instruction.target != target_address
        ):
            return {}

    boundary_site, boundary_object, boundary_vtable, boundary_target = (
        PHASE7_LEVEL_SAMPLE_OBSERVED_BOUNDARY
    )
    if (
        boundary_site not in sites
        or boundary_object not in PHASE7_LEVEL_UPDATE_PRIMARY_OBJECTS
        or boundary_vtable != PHASE7_LEVEL_UPDATE_PRIMARY_VTABLE
        or boundary_target != target
    ):
        return {}
    return {site: (target,) for site in sites}


def _phase7_level_query_targets(
    memory: SparseMemory,
    instructions: Mapping[int, X86Instruction],
    indirect_sites: AbstractSet[int],
    allowed_targets: AbstractSet[int],
) -> dict[int, tuple[int, ...]]:
    """Validate the fixed primary query and its predictable record sibling."""

    sites = set(PHASE7_LEVEL_QUERY_BINDINGS)
    targets = {
        target for _vtable, _slot, target in PHASE7_LEVEL_QUERY_BINDINGS.values()
    }
    if not sites <= indirect_sites or not targets <= allowed_targets:
        return {}

    for address, encoded in PHASE7_LEVEL_QUERY_PROOF_BYTES.items():
        expected = bytes.fromhex(encoded)
        if memory.read(address, len(expected)) != expected:
            return {}

    expected_callers = {
        target: {
            call_site
            for call_site, candidate in PHASE7_LEVEL_QUERY_DIRECT_CALLS.items()
            if candidate == target
        }
        for target in set(PHASE7_LEVEL_QUERY_DIRECT_CALLS.values())
    }
    observed_callers = {
        target: {
            address
            for address, instruction in instructions.items()
            if instruction.mnemonic.casefold() == "call"
            and instruction.target == target
        }
        for target in expected_callers
    }
    if observed_callers != expected_callers:
        return {}
    for call_site, target in PHASE7_LEVEL_QUERY_DIRECT_CALLS.items():
        instruction = instructions.get(call_site)
        if (
            instruction is None
            or instruction.mnemonic.casefold() != "call"
            or instruction.target != target
        ):
            return {}

    if memory.read_u32(PHASE7_LEVEL_QUERY_OWNER) != PHASE7_LEVEL_UPDATE_PRIMARY_CELL:
        return {}
    for index, object_address in enumerate(PHASE7_LEVEL_UPDATE_PRIMARY_OBJECTS):
        cell_value = memory.read_u32(PHASE7_LEVEL_UPDATE_PRIMARY_CELL + index * 4)
        if cell_value not in {0, object_address}:
            return {}
        if memory.read_u32(object_address) != PHASE7_LEVEL_UPDATE_PRIMARY_VTABLE:
            return {}
    primary_vtable = dict(PHASE7_LEVEL_LOAD_MANAGER_VTABLES)[
        PHASE7_LEVEL_UPDATE_PRIMARY_VTABLE
    ]
    for index, target in enumerate(primary_vtable):
        if memory.read_u32(PHASE7_LEVEL_UPDATE_PRIMARY_VTABLE + index * 4) != target:
            return {}

    record_count = memory.read_u32(PHASE7_LEVEL_QUERY_RECORD_COUNT_CELL)
    if (
        memory.read_u32(PHASE7_LEVEL_QUERY_RECORD_BASE_CELL)
        != PHASE7_LEVEL_QUERY_RECORD_BASE
        or not 0 < record_count <= PHASE7_LEVEL_QUERY_RECORD_CAPACITY
    ):
        return {}
    for index, target in enumerate(PHASE7_LEVEL_QUERY_RECORD_VTABLE_ENTRIES):
        if memory.read_u32(PHASE7_LEVEL_QUERY_RECORD_VTABLE + index * 4) != target:
            return {}
    for index in range(PHASE7_LEVEL_QUERY_RECORD_CAPACITY):
        record = (
            PHASE7_LEVEL_QUERY_RECORD_BASE
            + index * PHASE7_LEVEL_QUERY_RECORD_STRIDE
        )
        if memory.read_u32(record) != PHASE7_LEVEL_QUERY_RECORD_VTABLE:
            return {}
    if memory.read_u32(PHASE7_LEVEL_QUERY_POOL_BASE_CELL) != PHASE7_LEVEL_QUERY_POOL_BASE:
        return {}
    for index, target in enumerate(PHASE7_LEVEL_QUERY_POOL_VTABLE_ENTRIES):
        if memory.read_u32(PHASE7_LEVEL_QUERY_POOL_VTABLE + index * 4) != target:
            return {}
    for index in range(PHASE7_LEVEL_QUERY_POOL_CAPACITY):
        record = PHASE7_LEVEL_QUERY_POOL_BASE + index * PHASE7_LEVEL_QUERY_POOL_STRIDE
        if memory.read_u32(record) != PHASE7_LEVEL_QUERY_POOL_VTABLE:
            return {}

    observed_base_references = {
        address
        for address, instruction in instructions.items()
        if any(
            (operand.kind == "imm" and operand.immediate == PHASE7_LEVEL_QUERY_RECORD_BASE)
            or (
                operand.kind == "mem"
                and operand.absolute == PHASE7_LEVEL_QUERY_RECORD_BASE
            )
            for operand in instruction.operands
        )
    }
    observed_vtable_references = {
        address
        for address, instruction in instructions.items()
        if any(
            (operand.kind == "imm" and operand.immediate == PHASE7_LEVEL_QUERY_RECORD_VTABLE)
            or (
                operand.kind == "mem"
                and operand.absolute == PHASE7_LEVEL_QUERY_RECORD_VTABLE
            )
            for operand in instruction.operands
        )
    }
    observed_pool_base_references = {
        address
        for address, instruction in instructions.items()
        if any(
            (operand.kind == "imm" and operand.immediate == PHASE7_LEVEL_QUERY_POOL_BASE)
            or (
                operand.kind == "mem"
                and operand.absolute == PHASE7_LEVEL_QUERY_POOL_BASE
            )
            for operand in instruction.operands
        )
    }
    observed_pool_vtable_references = {
        address
        for address, instruction in instructions.items()
        if any(
            (operand.kind == "imm" and operand.immediate == PHASE7_LEVEL_QUERY_POOL_VTABLE)
            or (
                operand.kind == "mem"
                and operand.absolute == PHASE7_LEVEL_QUERY_POOL_VTABLE
            )
            for operand in instruction.operands
        )
    }
    if (
        observed_base_references != PHASE7_LEVEL_QUERY_RECORD_BASE_REFERENCES
        or observed_vtable_references != PHASE7_LEVEL_QUERY_RECORD_VTABLE_REFERENCES
        or observed_pool_base_references != PHASE7_LEVEL_QUERY_POOL_BASE_REFERENCES
        or observed_pool_vtable_references
        != PHASE7_LEVEL_QUERY_POOL_VTABLE_REFERENCES
    ):
        return {}

    recovered: dict[int, tuple[int, ...]] = {}
    expected_site_bytes = {
        0x000798B5: "FF5218",
        0x0007994D: "FF5204",
        0x0009F562: "FF5004",
        0x0009F650: "FF5004",
        0x0009F6AC: "FF5004",
    }
    for site, (vtable, slot, target) in PHASE7_LEVEL_QUERY_BINDINGS.items():
        instruction = instructions.get(site)
        memory_operands = (
            []
            if instruction is None
            else [operand for operand in instruction.operands if operand.kind == "mem"]
        )
        if (
            instruction is None
            or instruction.mnemonic.casefold() != "call"
            or instruction.bytes_hex.upper() != expected_site_bytes[site]
            or len(memory_operands) != 1
            or memory_operands[0].base != PHASE7_LEVEL_QUERY_SITE_BASES[site]
            or memory_operands[0].displacement != slot
        ):
            return {}
        table = (
            primary_vtable
            if vtable == PHASE7_LEVEL_UPDATE_PRIMARY_VTABLE
            else PHASE7_LEVEL_QUERY_RECORD_VTABLE_ENTRIES
            if vtable == PHASE7_LEVEL_QUERY_RECORD_VTABLE
            else PHASE7_LEVEL_QUERY_POOL_VTABLE_ENTRIES
        )
        if table[slot // 4] != target:
            return {}
        recovered[site] = (target,)

    observed_site, _object, observed_vtable, observed_target = (
        PHASE7_LEVEL_QUERY_OBSERVED_BOUNDARY
    )
    predicted_site, _record, predicted_vtable, predicted_target = (
        PHASE7_LEVEL_QUERY_PREDICTED_BOUNDARY
    )
    if (
        PHASE7_LEVEL_QUERY_BINDINGS[observed_site]
        != (observed_vtable, 0x18, observed_target)
        or PHASE7_LEVEL_QUERY_BINDINGS[predicted_site]
        != (predicted_vtable, 0x04, predicted_target)
    ):
        return {}
    return recovered


def _phase7_runtime_callback_vector_targets(
    memory: SparseMemory,
    instructions: Mapping[int, X86Instruction],
    indirect_sites: AbstractSet[int],
    allowed_targets: AbstractSet[int],
) -> dict[int, tuple[int, ...]]:
    """Validate the complete late level-load runtime callback vector."""

    sites = set(PHASE7_RUNTIME_CALLBACK_BINDINGS)
    vector = dict(PHASE7_RUNTIME_CALLBACK_VECTOR)
    callback_targets = set(vector.values())
    if not sites <= indirect_sites or not callback_targets <= allowed_targets:
        return {}
    if (
        set(PHASE7_RUNTIME_CALLBACK_SITE_BYTES) != sites
        or set(PHASE7_RUNTIME_CALLBACK_REFERENCES) != set(vector)
    ):
        return {}

    for cell, target in PHASE7_RUNTIME_CALLBACK_VECTOR:
        if memory.read_u32(cell) != target:
            return {}

    caller_starts = {
        caller_start
        for caller_start, _cell, _target in PHASE7_RUNTIME_CALLBACK_BINDINGS.values()
    }
    if caller_starts != set(PHASE7_RUNTIME_CALLBACK_CALLER_BYTES):
        return {}
    for caller_start, encoded in PHASE7_RUNTIME_CALLBACK_CALLER_BYTES.items():
        expected = bytes.fromhex(encoded)
        if memory.read(caller_start, len(expected)) != expected:
            return {}
    for installer_start, encoded in PHASE7_RUNTIME_CALLBACK_INSTALLER_BYTES.items():
        expected = bytes.fromhex(encoded)
        if memory.read(installer_start, len(expected)) != expected:
            return {}
    for address, encoded in PHASE7_RUNTIME_CALLBACK_REFERENCE_BYTES.items():
        instruction = instructions.get(address)
        if instruction is None or instruction.bytes_hex.upper() != encoded:
            return {}

    observed_references = {
        cell: {
            address
            for address, instruction in instructions.items()
            if any(
                operand.kind == "mem" and operand.absolute == cell
                for operand in instruction.operands
            )
        }
        for cell in vector
    }
    if observed_references != PHASE7_RUNTIME_CALLBACK_REFERENCES:
        return {}
    if any(
        instruction.mnemonic.casefold() in {"call", "jmp"}
        and instruction.target in callback_targets
        for instruction in instructions.values()
    ):
        return {}

    recovered: dict[int, tuple[int, ...]] = {}
    for site, (caller_start, cell, target) in PHASE7_RUNTIME_CALLBACK_BINDINGS.items():
        instruction = instructions.get(site)
        caller_end = caller_start + len(
            bytes.fromhex(PHASE7_RUNTIME_CALLBACK_CALLER_BYTES[caller_start])
        )
        memory_operands = (
            []
            if instruction is None
            else [operand for operand in instruction.operands if operand.kind == "mem"]
        )
        if (
            instruction is None
            or instruction.bytes_hex.upper() != PHASE7_RUNTIME_CALLBACK_SITE_BYTES[site]
            or instruction.mnemonic.casefold() != "call"
            or len(memory_operands) != 1
            or memory_operands[0].absolute != cell
            or vector.get(cell) != target
            or not caller_start <= site < caller_end
        ):
            return {}
        recovered[site] = (target,)
    return recovered


def _phase7_title_input_object_targets(
    memory: SparseMemory,
    instructions: Mapping[int, X86Instruction],
    indirect_sites: AbstractSet[int],
    allowed_targets: AbstractSet[int],
) -> dict[int, tuple[int, ...]]:
    """Validate the complete finite title-input object vtable family."""

    sites = set(PHASE7_TITLE_INPUT_OBJECT_BINDINGS)
    vtable_targets = set(PHASE7_TITLE_INPUT_OBJECT_VTABLE_ENTRIES)
    if not sites <= indirect_sites or not vtable_targets <= allowed_targets:
        return {}
    if (
        memory.read_u32(PHASE7_TITLE_INPUT_OBJECT_POINTER_CELL) != PHASE7_TITLE_INPUT_OBJECT
        or memory.read_u32(PHASE7_TITLE_INPUT_OBJECT) != PHASE7_TITLE_INPUT_OBJECT_VTABLE
    ):
        return {}
    for index, entry in enumerate(PHASE7_TITLE_INPUT_OBJECT_VTABLE_ENTRIES):
        if memory.read_u32(PHASE7_TITLE_INPUT_OBJECT_VTABLE + index * 4) != entry:
            return {}
    for address, expected in PHASE7_TITLE_INPUT_OBJECT_INSTRUCTION_BYTES.items():
        instruction = instructions.get(address)
        if instruction is None or instruction.bytes_hex.upper() != expected:
            return {}
    pointer_cell_references = {
        address
        for address, instruction in instructions.items()
        if any(
            operand.kind == "mem" and operand.absolute == PHASE7_TITLE_INPUT_OBJECT_POINTER_CELL
            for operand in instruction.operands
        )
    }
    if pointer_cell_references != {0x0002A636}:
        return {}
    direct_callers = {
        address
        for address, instruction in instructions.items()
        if instruction.mnemonic.casefold() == "call" and instruction.target in vtable_targets
    }
    if direct_callers:
        return {}
    return {site: (target,) for site, (_slot, target) in PHASE7_TITLE_INPUT_OBJECT_BINDINGS.items()}


def _phase7_post_start_object_targets(
    memory: SparseMemory,
    instructions: Mapping[int, X86Instruction],
    indirect_sites: AbstractSet[int],
    allowed_targets: AbstractSet[int],
) -> dict[int, tuple[int, ...]]:
    """Validate the bounded post-Start global-object slot-C family."""

    sites = set(PHASE7_POST_START_OBJECT_BINDINGS)
    vtable_targets = set(PHASE7_POST_START_OBJECT_VTABLE_ENTRIES)
    if not sites <= indirect_sites or not vtable_targets <= allowed_targets:
        return {}
    if (
        memory.read_u32(PHASE7_POST_START_OBJECT_POINTER_CELL)
        != PHASE7_POST_START_OBJECT_INITIAL_POINTER
        or memory.read_u32(PHASE7_POST_START_OBJECT) != PHASE7_POST_START_OBJECT_VTABLE
    ):
        return {}
    for index, entry in enumerate(PHASE7_POST_START_OBJECT_VTABLE_ENTRIES):
        if memory.read_u32(PHASE7_POST_START_OBJECT_VTABLE + index * 4) != entry:
            return {}
    for address, expected in PHASE7_POST_START_OBJECT_INSTRUCTION_BYTES.items():
        instruction = instructions.get(address)
        if instruction is None or instruction.bytes_hex.upper() != expected:
            return {}
    for slot, target in PHASE7_POST_START_OBJECT_BINDINGS.values():
        if memory.read_u32(PHASE7_POST_START_OBJECT_VTABLE + slot) != target:
            return {}
    direct_callers = {
        address
        for address, instruction in instructions.items()
        if instruction.mnemonic.casefold() == "call" and instruction.target in vtable_targets
    }
    if direct_callers:
        return {}
    return {site: (target,) for site, (_slot, target) in PHASE7_POST_START_OBJECT_BINDINGS.items()}


def _phase7_save_slot_object_targets(
    memory: SparseMemory,
    instructions: Mapping[int, X86Instruction],
    indirect_sites: AbstractSet[int],
    allowed_targets: AbstractSet[int],
) -> dict[int, tuple[int, ...]]:
    """Validate the bounded save-slot global-object slot-C family."""

    sites = set(PHASE7_SAVE_SLOT_OBJECT_BINDINGS)
    vtable_targets = set(PHASE7_SAVE_SLOT_OBJECT_VTABLE_ENTRIES)
    owner_vtable_targets = set(PHASE7_SAVE_SLOT_OWNER_VTABLE_ENTRIES)
    if not sites <= indirect_sites or not vtable_targets <= allowed_targets:
        return {}
    if not owner_vtable_targets <= allowed_targets:
        return {}
    if (
        memory.read_u32(PHASE7_SAVE_SLOT_OBJECT_POINTER_CELL)
        != PHASE7_SAVE_SLOT_OBJECT_INITIAL_POINTER
        or memory.read_u32(PHASE7_SAVE_SLOT_OBJECT) != PHASE7_SAVE_SLOT_OBJECT_VTABLE
    ):
        return {}
    for index, entry in enumerate(PHASE7_SAVE_SLOT_OBJECT_VTABLE_ENTRIES):
        if memory.read_u32(PHASE7_SAVE_SLOT_OBJECT_VTABLE + index * 4) != entry:
            return {}
    for index, entry in enumerate(PHASE7_SAVE_SLOT_OWNER_VTABLE_ENTRIES):
        if memory.read_u32(PHASE7_SAVE_SLOT_OWNER_VTABLE + index * 4) != entry:
            return {}
    for address, expected in PHASE7_SAVE_SLOT_OBJECT_INSTRUCTION_BYTES.items():
        instruction = instructions.get(address)
        if instruction is None or instruction.bytes_hex.upper() != expected:
            return {}
    for slot, target in PHASE7_SAVE_SLOT_OBJECT_BINDINGS.values():
        if memory.read_u32(PHASE7_SAVE_SLOT_OBJECT_VTABLE + slot) != target:
            return {}
    return {site: (target,) for site, (_slot, target) in PHASE7_SAVE_SLOT_OBJECT_BINDINGS.items()}


def _phase7_resource_selection_object_targets(
    memory: SparseMemory,
    instructions: Mapping[int, X86Instruction],
    indirect_sites: AbstractSet[int],
    allowed_targets: AbstractSet[int],
) -> dict[int, tuple[int, ...]]:
    """Validate all exact slot-C callers of the resource-selection object."""

    sites = set(PHASE7_RESOURCE_SELECTION_OBJECT_BINDINGS)
    vtable_targets = set(PHASE7_RESOURCE_SELECTION_OBJECT_VTABLE_ENTRIES)
    if not sites <= indirect_sites or not vtable_targets <= allowed_targets:
        return {}
    if (
        memory.read_u32(PHASE7_RESOURCE_SELECTION_OBJECT_POINTER_CELL)
        != PHASE7_RESOURCE_SELECTION_OBJECT_INITIAL_POINTER
        or memory.read_u32(PHASE7_RESOURCE_SELECTION_OBJECT)
        != PHASE7_RESOURCE_SELECTION_OBJECT_VTABLE
    ):
        return {}
    for index, entry in enumerate(PHASE7_RESOURCE_SELECTION_OBJECT_VTABLE_ENTRIES):
        if memory.read_u32(PHASE7_RESOURCE_SELECTION_OBJECT_VTABLE + index * 4) != entry:
            return {}
    caller_starts = {
        caller_start
        for caller_start, _slot, _target in PHASE7_RESOURCE_SELECTION_OBJECT_BINDINGS.values()
    }
    if caller_starts != set(PHASE7_RESOURCE_SELECTION_OBJECT_CALLER_BYTES):
        return {}
    for site, (caller_start, slot, target) in PHASE7_RESOURCE_SELECTION_OBJECT_BINDINGS.items():
        expected = bytes.fromhex(PHASE7_RESOURCE_SELECTION_OBJECT_CALLER_BYTES[caller_start])
        instruction = instructions.get(site)
        if (
            site != caller_start + len(expected) - 3
            or memory.read(caller_start, len(expected)) != expected
            or instruction is None
            or instruction.bytes_hex.upper() != expected[-3:].hex().upper()
            or memory.read_u32(PHASE7_RESOURCE_SELECTION_OBJECT_VTABLE + slot) != target
        ):
            return {}
    return {
        site: (target,)
        for site, (_caller_start, _slot, target) in PHASE7_RESOURCE_SELECTION_OBJECT_BINDINGS.items()
    }


def _phase7_level_select_object_targets(
    memory: SparseMemory,
    instructions: Mapping[int, X86Instruction],
    indirect_sites: AbstractSet[int],
    allowed_targets: AbstractSet[int],
) -> dict[int, tuple[int, ...]]:
    """Validate the complete finite level-select global-object family."""

    sites = set(PHASE7_LEVEL_SELECT_OBJECT_BINDINGS)
    vtable_targets = set(PHASE7_LEVEL_SELECT_OBJECT_VTABLE_ENTRIES)
    if not sites <= indirect_sites or not vtable_targets <= allowed_targets:
        return {}
    if (
        memory.read_u32(PHASE7_LEVEL_SELECT_OBJECT_POINTER_CELL)
        != PHASE7_LEVEL_SELECT_OBJECT
        or memory.read_u32(PHASE7_LEVEL_SELECT_OBJECT)
        != PHASE7_LEVEL_SELECT_OBJECT_VTABLE
    ):
        return {}
    for index, entry in enumerate(PHASE7_LEVEL_SELECT_OBJECT_VTABLE_ENTRIES):
        if memory.read_u32(PHASE7_LEVEL_SELECT_OBJECT_VTABLE + index * 4) != entry:
            return {}
    for caller_start, encoded in PHASE7_LEVEL_SELECT_OBJECT_CALLER_BYTES.items():
        expected = bytes.fromhex(encoded)
        if memory.read(caller_start, len(expected)) != expected:
            return {}
    for site, (caller_start, slot, target) in PHASE7_LEVEL_SELECT_OBJECT_BINDINGS.items():
        expected = bytes.fromhex(PHASE7_LEVEL_SELECT_OBJECT_CALLER_BYTES[caller_start])
        instruction = instructions.get(site)
        if (
            site != caller_start + len(expected) - 3
            or instruction is None
            or instruction.bytes_hex.upper() != expected[-3:].hex().upper()
            or memory.read_u32(PHASE7_LEVEL_SELECT_OBJECT_VTABLE + slot) != target
        ):
            return {}
    return {
        site: (target,)
        for site, (_caller_start, _slot, target) in PHASE7_LEVEL_SELECT_OBJECT_BINDINGS.items()
    }


def _phase7_new_save_object_targets(
    memory: SparseMemory,
    instructions: Mapping[int, X86Instruction],
    indirect_sites: AbstractSet[int],
    allowed_targets: AbstractSet[int],
) -> dict[int, tuple[int, ...]]:
    """Validate the complete finite new-save global-object call batch."""

    sites = set(PHASE7_NEW_SAVE_OBJECT_BINDINGS)
    vtables = (
        (
            PHASE7_POST_START_OBJECT_POINTER_CELL,
            PHASE7_POST_START_OBJECT_INITIAL_POINTER,
            PHASE7_POST_START_OBJECT,
            PHASE7_POST_START_OBJECT_VTABLE,
            PHASE7_POST_START_OBJECT_VTABLE_ENTRIES,
        ),
        (
            PHASE7_NEW_SAVE_COMPANION_OBJECT_POINTER_CELL,
            PHASE7_NEW_SAVE_COMPANION_OBJECT_INITIAL_POINTER,
            PHASE7_NEW_SAVE_COMPANION_OBJECT,
            PHASE7_NEW_SAVE_COMPANION_OBJECT_VTABLE,
            PHASE7_NEW_SAVE_COMPANION_OBJECT_VTABLE_ENTRIES,
        ),
    )
    vtable_targets = {
        target for _cell, _initial, _object, _vtable, entries in vtables for target in entries
    }
    if not sites <= indirect_sites or not vtable_targets <= allowed_targets:
        return {}
    for pointer_cell, initial_pointer, object_address, vtable, entries in vtables:
        if (
            memory.read_u32(pointer_cell) != initial_pointer
            or memory.read_u32(object_address) != vtable
        ):
            return {}
        for index, entry in enumerate(entries):
            if memory.read_u32(vtable + index * 4) != entry:
                return {}
    for address, expected in PHASE7_NEW_SAVE_OBJECT_INSTRUCTION_BYTES.items():
        instruction = instructions.get(address)
        if instruction is None or instruction.bytes_hex.upper() != expected:
            return {}
    for vtable, slot, target in PHASE7_NEW_SAVE_OBJECT_BINDINGS.values():
        if memory.read_u32(vtable + slot) != target:
            return {}
    return {
        site: (target,)
        for site, (_vtable, _slot, target) in PHASE7_NEW_SAVE_OBJECT_BINDINGS.items()
    }


def _phase7_directsound_refcount_targets(
    memory: SparseMemory,
    instructions: Mapping[int, X86Instruction],
    indirect_sites: AbstractSet[int],
    allowed_targets: AbstractSet[int],
) -> dict[int, tuple[int, ...]]:
    """Validate both finite DirectSound reference-count vtable calls as one unit."""

    sites = set(PHASE7_DIRECTSOUND_REFCOUNT_BINDINGS)
    targets = {target for _slot, target in PHASE7_DIRECTSOUND_REFCOUNT_BINDINGS.values()}
    if not sites <= indirect_sites or not targets <= allowed_targets:
        return {}
    for address, expected in PHASE7_DIRECTSOUND_REFCOUNT_INSTRUCTION_BYTES.items():
        instruction = instructions.get(address)
        if instruction is None or instruction.bytes_hex.upper() != expected:
            return {}
    for slot, target in PHASE7_DIRECTSOUND_REFCOUNT_BINDINGS.values():
        if memory.read_u32(PHASE7_DIRECTSOUND_REFCOUNT_VTABLE + slot) != target:
            return {}
    return {
        site: (target,) for site, (_slot, target) in PHASE7_DIRECTSOUND_REFCOUNT_BINDINGS.items()
    }


def _phase7_directsound_voice_targets(
    memory: SparseMemory,
    instructions: Mapping[int, X86Instruction],
    indirect_sites: AbstractSet[int],
    allowed_targets: AbstractSet[int],
) -> dict[int, tuple[int, ...]]:
    """Validate the complete finite DirectSound hardware-voice object family."""

    sites = set(PHASE7_DIRECTSOUND_VOICE_BINDINGS)
    targets = {target for _slot, target in PHASE7_DIRECTSOUND_VOICE_BINDINGS.values()}
    vtable_targets = set(PHASE7_DIRECTSOUND_VOICE_VTABLE_ENTRIES)
    if (
        not sites <= indirect_sites
        or not targets <= allowed_targets
        or not vtable_targets <= allowed_targets
    ):
        return {}
    for address, expected_bytes in PHASE7_DIRECTSOUND_VOICE_INSTRUCTION_BYTES.items():
        instruction = instructions.get(address)
        if instruction is None or instruction.bytes_hex.upper() != expected_bytes:
            return {}
    for index, entry in enumerate(PHASE7_DIRECTSOUND_VOICE_VTABLE_ENTRIES):
        if memory.read_u32(PHASE7_DIRECTSOUND_VOICE_VTABLE + index * 4) != entry:
            return {}
    for slot, target in PHASE7_DIRECTSOUND_VOICE_BINDINGS.values():
        if memory.read_u32(PHASE7_DIRECTSOUND_VOICE_VTABLE + slot) != target:
            return {}

    installers = {
        address
        for address, instruction in instructions.items()
        if instruction.mnemonic.casefold() == "mov"
        and any(
            operand.kind == "imm" and operand.immediate == PHASE7_DIRECTSOUND_VOICE_VTABLE
            for operand in instruction.operands
        )
    }
    if installers != {0x00234F4A, 0x00235510}:
        return {}
    expected_callers = {
        0x00234F36: {0x0022F2A4},
        0x002353D5: {0x0022F2BE},
        0x00232D56: {0x002318C2, 0x002353DA},
        0x002349BD: {0x00234BE6, 0x002351BE},
        0x0023550C: {0x002355CC},
    }
    for callee, expected_callee_callers in expected_callers.items():
        callers = {
            address
            for address, instruction in instructions.items()
            if instruction.mnemonic.casefold() == "call" and instruction.target == callee
        }
        if callers != expected_callee_callers:
            return {}
    return {site: (target,) for site, (_slot, target) in PHASE7_DIRECTSOUND_VOICE_BINDINGS.items()}


def _phase7_d150_callback_targets(
    instructions: Mapping[int, X86Instruction],
    indirect_sites: AbstractSet[int],
    allowed_targets: AbstractSet[int],
) -> dict[int, tuple[int, ...]]:
    """Bind both 0x10D150 callback sites from all decoded direct callers."""

    callback_sites = set(PHASE7_D150_CALLBACK_TARGETS)
    callback_targets = {
        target for targets in PHASE7_D150_CALLBACK_TARGETS.values() for target in targets
    }
    if not callback_sites <= indirect_sites or not callback_targets <= allowed_targets:
        return {}
    direct_callers = {
        address
        for address, instruction in instructions.items()
        if instruction.mnemonic.casefold() == "call" and instruction.target == 0x0010D150
    }
    if direct_callers != {0x0010D1BE, *PHASE7_D150_CALLBACK_CALLERS}:
        return {}

    entry_load = instructions.get(0x0010D16A)
    if (
        entry_load is None
        or entry_load.mnemonic.casefold() != "mov"
        or len(entry_load.operands) != 2
        or entry_load.operands[0].kind != "reg"
        or entry_load.operands[0].reg != "ebp"
    ):
        return {}
    entry_operand = entry_load.operands[1]
    if (
        entry_operand.kind != "mem"
        or entry_operand.base != "esp"
        or entry_operand.index is not None
        or entry_operand.scale != 1
        or entry_operand.displacement != 0x18
        or entry_operand.absolute is not None
        or entry_operand.segment is not None
    ):
        return {}
    for site in callback_sites:
        instruction = instructions.get(site)
        if (
            instruction is None
            or instruction.mnemonic.casefold() != "call"
            or len(instruction.operands) != 1
            or instruction.operands[0].kind != "reg"
            or instruction.operands[0].reg != "ebp"
        ):
            return {}
    for call_site, (install_site, target) in PHASE7_D150_CALLBACK_CALLERS.items():
        install = instructions.get(install_site)
        call = instructions.get(call_site)
        if (
            install is None
            or install.mnemonic.casefold() != "push"
            or len(install.operands) != 1
            or install.operands[0].kind != "imm"
            or install.operands[0].immediate != target
            or call is None
            or call.mnemonic.casefold() != "call"
            or call.target != 0x0010D150
        ):
            return {}
    recursive_call = instructions.get(0x0010D1BE)
    if (
        recursive_call is None
        or recursive_call.mnemonic.casefold() != "call"
        or recursive_call.target != 0x0010D150
    ):
        return {}
    return dict(PHASE7_D150_CALLBACK_TARGETS)


def _phase7_bb0_callback_targets(
    instructions: Mapping[int, X86Instruction],
    indirect_sites: AbstractSet[int],
    allowed_targets: AbstractSet[int],
) -> dict[int, tuple[int, ...]]:
    """Bind the sole 0x112BB0 callback from its only decoded direct caller."""

    if (
        PHASE7_BB0_CALLBACK_SITE not in indirect_sites
        or PHASE7_BB0_CALLBACK_TARGET not in allowed_targets
    ):
        return {}
    direct_callers = {
        address
        for address, instruction in instructions.items()
        if instruction.mnemonic.casefold() == "call" and instruction.target == 0x00112BB0
    }
    if direct_callers != {PHASE7_BB0_CALLBACK_CALLER}:
        return {}
    entry_load = instructions.get(0x00112BC0)
    if (
        entry_load is None
        or entry_load.mnemonic.casefold() != "mov"
        or len(entry_load.operands) != 2
        or entry_load.operands[0].kind != "reg"
        or entry_load.operands[0].reg != "edi"
    ):
        return {}
    entry_operand = entry_load.operands[1]
    if (
        entry_operand.kind != "mem"
        or entry_operand.base != "esp"
        or entry_operand.index is not None
        or entry_operand.scale != 1
        or entry_operand.displacement != 0x18
        or entry_operand.absolute is not None
        or entry_operand.segment is not None
    ):
        return {}
    indirect = instructions.get(PHASE7_BB0_CALLBACK_SITE)
    install = instructions.get(PHASE7_BB0_CALLBACK_INSTALL)
    caller = instructions.get(PHASE7_BB0_CALLBACK_CALLER)
    if (
        indirect is None
        or indirect.mnemonic.casefold() != "call"
        or len(indirect.operands) != 1
        or indirect.operands[0].kind != "reg"
        or indirect.operands[0].reg != "edi"
        or install is None
        or install.mnemonic.casefold() != "push"
        or len(install.operands) != 1
        or install.operands[0].kind != "imm"
        or install.operands[0].immediate != PHASE7_BB0_CALLBACK_TARGET
        or caller is None
        or caller.mnemonic.casefold() != "call"
        or caller.target != 0x00112BB0
    ):
        return {}
    return {PHASE7_BB0_CALLBACK_SITE: (PHASE7_BB0_CALLBACK_TARGET,)}


def _phase7_asset_stream_callback_targets(
    instructions: Mapping[int, X86Instruction],
    indirect_sites: AbstractSet[int],
    allowed_targets: AbstractSet[int],
) -> dict[int, tuple[int, ...]]:
    """Bind the asset-stream call only with its complete installed record."""

    record_targets = {
        target for _slot, target, _installer in PHASE7_ASSET_STREAM_CALLBACK_RECORD_SLOTS
    }
    callback_sites = set(PHASE7_ASSET_STREAM_CALLBACK_BINDINGS)
    if not callback_sites <= indirect_sites or not record_targets <= allowed_targets:
        return {}

    def register_memory_load(address: int, destination: str, base: str, displacement: int) -> bool:
        instruction = instructions.get(address)
        if (
            instruction is None
            or instruction.mnemonic.casefold() != "mov"
            or len(instruction.operands) != 2
            or instruction.operands[0].kind != "reg"
            or instruction.operands[0].reg != destination
        ):
            return False
        operand = instruction.operands[1]
        return (
            operand.kind == "mem"
            and operand.base == base
            and operand.index is None
            and operand.scale == 1
            and operand.displacement == displacement
            and operand.absolute is None
            and operand.segment is None
        )

    def register_memory_store(address: int, base: str, displacement: int, source: str) -> bool:
        instruction = instructions.get(address)
        if (
            instruction is None
            or instruction.mnemonic.casefold() != "mov"
            or len(instruction.operands) != 2
            or instruction.operands[1].kind != "reg"
            or instruction.operands[1].reg != source
        ):
            return False
        operand = instruction.operands[0]
        return (
            operand.kind == "mem"
            and operand.base == base
            and operand.index is None
            and operand.scale == 1
            and operand.displacement == displacement
            and operand.absolute is None
            and operand.segment is None
        )

    cleanup_call = instructions.get(0x00112E50)
    construction_call = instructions.get(0x001130C6)
    copy_call = instructions.get(0x001131BD)
    if (
        cleanup_call is None
        or cleanup_call.mnemonic.casefold() != "call"
        or len(cleanup_call.operands) != 1
        or cleanup_call.operands[0].kind != "reg"
        or cleanup_call.operands[0].reg != "eax"
        or not register_memory_load(0x00112E45, "eax", "esi", 0x0C)
        or not register_memory_load(0x00112E48, "eax", "eax", 0x10)
        or construction_call is None
        or construction_call.mnemonic.casefold() != "call"
        or len(construction_call.operands) != 1
        or construction_call.operands[0].kind != "reg"
        or construction_call.operands[0].reg != "eax"
        or not register_memory_load(0x001130AB, "eax", "ebp", 0x0C)
        or not register_memory_load(0x001130B9, "eax", "eax", 0x0C)
        or copy_call is None
        or copy_call.mnemonic.casefold() != "call"
        or len(copy_call.operands) != 1
        or copy_call.operands[0].kind != "mem"
        or copy_call.operands[0].base != "esp"
        or copy_call.operands[0].index is not None
        or copy_call.operands[0].scale != 1
        or copy_call.operands[0].displacement != 0x24
        or copy_call.operands[0].absolute is not None
        or copy_call.operands[0].segment is not None
        or not register_memory_load(0x00113109, "eax", "ebp", 0x0C)
        or not register_memory_load(0x0011310C, "eax", "eax", 0x14)
        or not register_memory_store(0x0011311B, "esp", 0x14, "eax")
    ):
        return {}

    for slot, target, installer_address in PHASE7_ASSET_STREAM_CALLBACK_RECORD_SLOTS:
        installer = instructions.get(installer_address)
        if (
            installer is None
            or installer.mnemonic.casefold() != "mov"
            or len(installer.operands) != 2
        ):
            return {}
        destination, source = installer.operands
        if (
            destination.kind != "mem"
            or destination.base != "esi"
            or destination.index is not None
            or destination.scale != 1
            or destination.displacement != slot
            or destination.absolute is not None
            or destination.segment is not None
            or source.kind != "imm"
            or source.immediate != target
        ):
            return {}

    installed_target_references = {
        address
        for address, instruction in instructions.items()
        if any(
            operand.kind == "imm" and operand.immediate in record_targets
            for operand in instruction.operands
        )
    }
    expected_installers = {
        installer for _slot, _target, installer in PHASE7_ASSET_STREAM_CALLBACK_RECORD_SLOTS
    }
    if installed_target_references != expected_installers:
        return {}

    for entry, expected_references in PHASE7_ASSET_STREAM_ENTRY_REFERENCES.items():
        references = {
            address for address, instruction in instructions.items() if instruction.target == entry
        }
        if references != expected_references:
            return {}

    direct_target_callers = {
        address
        for address, instruction in instructions.items()
        if instruction.mnemonic.casefold() == "call" and instruction.target in record_targets
    }
    if direct_target_callers:
        return {}
    return {
        site: (target,) for site, (_slot, target) in PHASE7_ASSET_STREAM_CALLBACK_BINDINGS.items()
    }


def _phase7_resource_converter_callback_targets(
    memory: SparseMemory,
    instructions: Mapping[int, X86Instruction],
    indirect_sites: AbstractSet[int],
    allowed_targets: AbstractSet[int],
) -> dict[int, tuple[int, ...]]:
    """Bind the complete fixed resource-converter callback-vector family."""

    callback_sites = set(PHASE7_RESOURCE_CONVERTER_CALLBACK_BINDINGS)
    callback_targets = {
        target for _slot, target in PHASE7_RESOURCE_CONVERTER_CALLBACK_VECTOR
    }
    if not callback_sites <= indirect_sites or not callback_targets <= allowed_targets:
        return {}
    for index, value in enumerate(PHASE7_RESOURCE_CONVERTER_DESCRIPTOR_VALUES):
        if memory.read_u32(PHASE7_RESOURCE_CONVERTER_DESCRIPTOR + index * 4) != value:
            return {}
    installer = bytes.fromhex(PHASE7_RESOURCE_CONVERTER_INSTALLER_BYTES)
    if memory.read(0x00105BE0, len(installer)) != installer:
        return {}
    for address, encoded in PHASE7_RESOURCE_CONVERTER_INSTRUCTION_BYTES.items():
        instruction = instructions.get(address)
        if instruction is None or instruction.bytes_hex.upper() != encoded:
            return {}

    for site in callback_sites:
        instruction = instructions[site]
        if (
            instruction.mnemonic.casefold() != "call"
            or len(instruction.operands) != 1
            or instruction.operands[0].kind != "reg"
            or instruction.operands[0].reg != "eax"
        ):
            return {}
    for address, (slot, target) in PHASE7_RESOURCE_CONVERTER_INSTALLER_BINDINGS.items():
        instruction = instructions.get(address)
        if (
            instruction is None
            or instruction.mnemonic.casefold() != "mov"
            or len(instruction.operands) != 2
        ):
            return {}
        destination, source = instruction.operands
        if (
            destination.kind != "mem"
            or destination.base != "eax"
            or destination.index is not None
            or destination.scale != 1
            or destination.displacement != slot
            or destination.absolute is not None
            or destination.segment is not None
            or source.kind != "imm"
            or source.immediate != target
        ):
            return {}

    descriptor_references = {
        address
        for address, instruction in instructions.items()
        if any(
            operand.kind == "imm"
            and operand.immediate == PHASE7_RESOURCE_CONVERTER_DESCRIPTOR
            for operand in instruction.operands
        )
    }
    callback_target_references = {
        address
        for address, instruction in instructions.items()
        if any(
            operand.kind == "imm" and operand.immediate in callback_targets
            for operand in instruction.operands
        )
    }
    converter_callers = {
        address
        for address, instruction in instructions.items()
        if instruction.mnemonic.casefold() == "call"
        and instruction.target == 0x00105C90
    }
    plugin_direct_callers = {
        address
        for address, instruction in instructions.items()
        if instruction.mnemonic.casefold() == "call"
        and instruction.target in {0x00105920, 0x00105BE0}
    }
    boundary_site, boundary_target = PHASE7_RESOURCE_CONVERTER_OBSERVED_BOUNDARY
    if (
        descriptor_references != {0x00105C10}
        or callback_target_references
        != {0x0010501B, *PHASE7_RESOURCE_CONVERTER_INSTALLER_BINDINGS}
        or converter_callers != {0x00105A3C}
        or plugin_direct_callers
        or PHASE7_RESOURCE_CONVERTER_CALLBACK_BINDINGS[boundary_site][1]
        != boundary_target
    ):
        return {}
    return {
        site: (target,)
        for site, (_slot, target) in PHASE7_RESOURCE_CONVERTER_CALLBACK_BINDINGS.items()
    }


def _phase7_offline_address_taken_targets(
    memory: SparseMemory,
    instructions: Mapping[int, X86Instruction],
    indirect_sites: set[int],
    allowed_targets: set[int],
) -> dict[int, tuple[int, ...]]:
    """Close callbacks from the title's finite address-taken code corpus.

    The retail image is statically linked: callback pointers originate either
    as an immediate code address in an instruction, an aligned pointer in the
    immutable data directory, or an address-named absolute callback cell.  The
    result deliberately over-approximates which declaration can reach a given
    callback site, but every admitted destination is already decoded AOT code
    or a registered native service.  Runtime decoding and promotion therefore
    remain impossible even for callbacks not exercised by the retained run.
    """

    def is_aot_entry(target: int) -> bool:
        return target in allowed_targets and (
            target not in instructions
            or target < PHASE7_OFFLINE_VTABLE_RDATA_START
        )

    targets = {
        int(operand.immediate)
        for instruction in instructions.values()
        for operand in instruction.operands
        if operand.immediate is not None and is_aot_entry(int(operand.immediate))
    }
    for address in range(
        PHASE7_OFFLINE_VTABLE_RDATA_START,
        PHASE7_OFFLINE_VTABLE_RDATA_END,
        4,
    ):
        target = memory.read_u32(address)
        if is_aot_entry(target):
            targets.add(target)
    for site in indirect_sites:
        instruction = instructions.get(site)
        if instruction is None:
            continue
        for operand in instruction.operands:
            if operand.absolute is None:
                continue
            target = memory.read_u32(int(operand.absolute))
            if is_aot_entry(target):
                targets.add(target)

    closed_targets = tuple(sorted(targets))
    if not closed_targets:
        return {}
    recovered: dict[int, tuple[int, ...]] = {}
    for site in sorted(indirect_sites):
        instruction = instructions.get(site)
        if (
            instruction is None
            or instruction.mnemonic.casefold() not in {"call", "jmp"}
            or instruction.target is not None
            or len(instruction.operands) != 1
            or instruction.operands[0].kind not in {"reg", "mem"}
        ):
            continue
        recovered[site] = closed_targets
    return recovered


def _phase7_offline_closed_vtable_targets(
    memory: SparseMemory,
    instructions: Mapping[int, X86Instruction],
    indirect_sites: AbstractSet[int],
    allowed_targets: AbstractSet[int],
) -> dict[int, tuple[int, ...]]:
    """Recover the closed low-title virtual interface corpus offline."""

    candidate_bases: set[int] = set()
    for instruction in instructions.values():
        for operand in instruction.operands:
            for value in (operand.immediate, operand.absolute):
                if (
                    value is not None
                    and PHASE7_OFFLINE_VTABLE_RDATA_START
                    <= value
                    < PHASE7_OFFLINE_VTABLE_RDATA_END
                    and value % 4 == 0
                ):
                    candidate_bases.add(int(value))
    for _page, payload in memory.export_pages():
        for (value,) in struct.iter_unpack("<I", payload):
            if (
                PHASE7_OFFLINE_VTABLE_RDATA_START
                <= value
                < PHASE7_OFFLINE_VTABLE_RDATA_END
                and value % 4 == 0
            ):
                candidate_bases.add(value)

    requested_slots = {
        operand.displacement
        for site in indirect_sites
        if (instruction := instructions.get(site)) is not None
        for operand in instruction.operands
        if operand.kind == "mem"
        and 0 <= operand.displacement <= PHASE7_OFFLINE_VTABLE_SLOT_LIMIT
        and operand.displacement % 4 == 0
    }
    slot_targets: dict[int, tuple[int, ...]] = {}
    for slot in sorted(requested_slots):
        slot_target_values = {
            memory.read_u32(base + slot)
            for base in candidate_bases
            if base + slot + 4 <= PHASE7_OFFLINE_VTABLE_RDATA_END
            and memory.read_u32(base) in allowed_targets
            and memory.read_u32(base + slot) in allowed_targets
        }
        if slot_target_values:
            slot_targets[slot] = tuple(sorted(slot_target_values))

    recovered: dict[int, tuple[int, ...]] = {}
    for site in sorted(indirect_sites):
        instruction = instructions.get(site)
        if (
            instruction is None
            or instruction.mnemonic.casefold() != "call"
            or len(instruction.operands) != 1
        ):
            continue
        operand = instruction.operands[0]
        if (
            operand.kind != "mem"
            or operand.base is None
            or operand.index is not None
            or operand.absolute is not None
            or operand.segment is not None
        ):
            continue
        site_targets = slot_targets.get(operand.displacement)
        if site_targets:
            recovered[site] = site_targets
    return recovered


def _phase6_verified_indirect_targets(
    capsule: ReplayCapsule,
    plan: Ia32DecodedStorePlan,
    profile: Ia32CoverageProfile,
    services: Mapping[int, _HostServiceThunk],
    preflight_targets: Mapping[int, Sequence[int]],
    *,
    observed_memory: SparseMemory | None = None,
) -> dict[int, tuple[int, ...]]:
    """Bind measured call sites, then close equivalent nearby vtable slots."""

    verified = {
        int(address): tuple(sorted({_u32(int(target)) for target in targets}))
        for address, targets in preflight_targets.items()
    }
    instructions = _instruction_map(plan.functions)
    service_targets = set(services)
    profile_record = _normalized_coverage_profile(profile)
    profile_targets = {int(record["address"]) for record in profile_record["targets"]}
    observed_by_source: dict[int, set[int]] = {}
    for record in profile_record["transitions"]:
        observed_by_source.setdefault(int(record["source"]), set()).add(int(record["target"]))
    for record in profile_record["boundary_exits"]:
        if "target" in record and record["boundary"] != "scheduler":
            observed_by_source.setdefault(int(record["source"]), set()).add(int(record["target"]))
    indirect_sites = {int(record["source"]) for record in plan.indirect_target_table["sites"]}
    allowed_targets = set(instructions) | service_targets

    # A profiled module entry exits through its first indirect site before the
    # next measured re-entry.  This distinguishes repeated indirect calls in
    # one decoded block instead of assigning their union to every site.
    for function in plan.functions:
        function_addresses = sorted({instruction.address for instruction in function.instructions})
        entries = sorted(set(function_addresses) & profile_targets)
        sites = sorted(set(function_addresses) & indirect_sites)
        if not entries or not sites:
            continue
        for index, entry in enumerate(entries):
            limit = entries[index + 1] if index + 1 < len(entries) else 0x1_0000_0000
            site = next((address for address in sites if entry <= address < limit), None)
            if site is None:
                continue
            measured_targets = observed_by_source.get(entry, set()) & allowed_targets
            if measured_targets:
                verified[site] = tuple(sorted(measured_targets))

    # Preserve the original single-indirect-function inference for captures
    # whose module granularity does not expose the exact resumed instruction.
    for function in plan.functions:
        function_address_set = {instruction.address for instruction in function.instructions}
        profile_entries = function_address_set & profile_targets
        indirect_function_sites = function_address_set & indirect_sites
        if not profile_entries or len(indirect_function_sites) != 1:
            continue
        site = next(iter(indirect_function_sites))
        if site in verified:
            continue
        function_targets = {
            target
            for entry in profile_entries
            for target in observed_by_source.get(entry, set())
            if target in allowed_targets
        }
        if function_targets:
            verified[site] = tuple(sorted(function_targets))

    # Absolute import/vtable cells captured in guest memory are exact service
    # declarations, not speculative control-flow recovery. The title's finite
    # CRT callback table is likewise shared by every address-named wrapper.
    for site in sorted(indirect_sites):
        if site in verified:
            continue
        absolute_operands = [
            int(operand.absolute)
            for operand in instructions[site].operands
            if operand.absolute is not None
        ]
        if len(absolute_operands) == 1:
            target = capsule.memory.read_u32(absolute_operands[0])
            if target in service_targets:
                verified[site] = (target,)
                continue
            captured_targets = PHASE7_CAPTURED_CALLBACK_CELLS.get(absolute_operands[0])
            if captured_targets is not None:
                callback_targets = tuple(sorted(set(captured_targets) & allowed_targets))
                if len(callback_targets) == len(set(captured_targets)):
                    verified[site] = callback_targets

    # Validate the reviewed callee-saved-register import family against both
    # the decoded load instruction and the resolved boot import cell.
    for site, binding in PHASE7_CAPTURED_REGISTER_IMPORT_BINDINGS.items():
        if site in verified or site not in indirect_sites:
            continue
        load_address, import_cell, target = binding
        load = instructions.get(load_address)
        indirect = instructions.get(site)
        if load is None or indirect is None or target not in allowed_targets:
            continue
        load_operands = load.operands
        indirect_operands = indirect.operands
        if (
            load.mnemonic.casefold() != "mov"
            or len(load_operands) != 2
            or load_operands[0].kind != "reg"
            or load_operands[0].reg not in {"ebp", "ebx", "esi", "edi"}
            or load_operands[1].kind != "mem"
            or load_operands[1].absolute != import_cell
            or len(indirect_operands) != 1
            or indirect_operands[0].kind != "reg"
            or indirect_operands[0].reg != load_operands[0].reg
            or capsule.memory.read_u32(import_cell) != target
        ):
            continue
        verified[site] = (target,)

    # Fixed-base indexed jump tables are immutable captured data whose entries
    # must all resolve to decoded executable targets.  Recover the whole bounded
    # table in one batch so the native artifact preserves the original indirect
    # branch instead of trapping on each observed selector value.
    for site in sorted(indirect_sites):
        if site in verified:
            continue
        jump_targets = _phase6_static_jump_table_targets(
            capsule.memory,
            instructions[site],
            allowed_targets,
        )
        if jump_targets:
            verified[site] = jump_targets

    # A finite captured vtable family is evidence only while the accepted
    # capsule still contains every address-named slot value and the indirect
    # instruction still names that same slot.  This keeps ordinary startup
    # coverage fail-closed if either the title data or decoded instruction
    # changes.
    for site, bindings in PHASE7_CAPTURED_VTABLE_SLOT_BINDINGS.items():
        if site in verified or site not in indirect_sites:
            continue
        memory_operands = [
            operand for operand in instructions[site].operands if operand.kind == "mem"
        ]
        if len(memory_operands) != 1:
            continue
        slot_offset = memory_operands[0].displacement
        vtable_targets = tuple(
            sorted(
                {
                    target
                    for vtable, target in bindings
                    if target in allowed_targets
                    and capsule.memory.read_u32(_u32(vtable + slot_offset)) == target
                }
            )
        )
        if len(vtable_targets) == len(bindings):
            verified[site] = vtable_targets

    # The low-title UI/state virtual families use ordinary MSVC vptr loads and
    # two common interface slots.  Close them from the complete static pointer
    # corpus rather than waiting for each object lifetime to appear in manual
    # gameplay.  A candidate vtable base must be present either as an encoded
    # instruction operand or as a dword in the immutable boot image, and both
    # its first entry and selected slot must name ahead-of-time decoded code.
    # Copies of those pointers preserve the same finite target set; no runtime
    # discovery, decoding, or code generation is admitted.
    for site, targets in _phase7_offline_closed_vtable_targets(
        capsule.memory,
        instructions,
        indirect_sites,
        allowed_targets,
    ).items():
        verified.setdefault(site, targets)

    verified.update(
        _phase7_base_manager_object_targets(
            capsule.memory,
            instructions,
            indirect_sites,
            allowed_targets,
        )
    )
    verified.update(
        _phase7_game_state_object_targets(
            capsule.memory,
            instructions,
            indirect_sites,
            allowed_targets,
        )
    )
    verified.update(
        _phase7_level_load_strategy_targets(
            capsule.memory,
            instructions,
            indirect_sites,
            allowed_targets,
        )
    )
    verified.update(
        _phase7_level_parameter_bank_targets(
            capsule.memory,
            instructions,
            indirect_sites,
            allowed_targets,
        )
    )
    verified.update(
        _phase7_level_load_manager_targets(
            observed_memory or capsule.memory,
            instructions,
            indirect_sites,
            allowed_targets,
        )
    )
    verified.update(
        _phase7_level_update_primary_targets(
            observed_memory or capsule.memory,
            instructions,
            indirect_sites,
            allowed_targets,
        )
    )
    verified.update(
        _phase7_level_sample_primary_targets(
            observed_memory or capsule.memory,
            instructions,
            indirect_sites,
            allowed_targets,
        )
    )
    verified.update(
        _phase7_level_query_targets(
            observed_memory or capsule.memory,
            instructions,
            indirect_sites,
            allowed_targets,
        )
    )
    verified.update(
        _phase7_runtime_callback_vector_targets(
            observed_memory or capsule.memory,
            instructions,
            indirect_sites,
            allowed_targets,
        )
    )
    verified.update(
        _phase7_aligned_copy_tail_targets(
            capsule.memory,
            instructions,
            indirect_sites,
            allowed_targets,
        )
    )
    verified.update(
        _phase7_memmove_jump_table_targets(
            capsule.memory,
            instructions,
            indirect_sites,
            allowed_targets,
        )
    )
    verified.update(
        _phase7_title_input_object_targets(
            capsule.memory,
            instructions,
            indirect_sites,
            allowed_targets,
        )
    )
    verified.update(
        _phase7_post_start_object_targets(
            capsule.memory,
            instructions,
            indirect_sites,
            allowed_targets,
        )
    )
    verified.update(
        _phase7_save_slot_object_targets(
            capsule.memory,
            instructions,
            indirect_sites,
            allowed_targets,
        )
    )
    verified.update(
        _phase7_resource_selection_object_targets(
            capsule.memory,
            instructions,
            indirect_sites,
            allowed_targets,
        )
    )
    verified.update(
        _phase7_level_select_object_targets(
            observed_memory or capsule.memory,
            instructions,
            indirect_sites,
            allowed_targets,
        )
    )
    verified.update(
        _phase7_new_save_object_targets(
            capsule.memory,
            instructions,
            indirect_sites,
            allowed_targets,
        )
    )
    verified.update(
        _phase7_directsound_refcount_targets(
            capsule.memory,
            instructions,
            indirect_sites,
            allowed_targets,
        )
    )
    verified.update(
        _phase7_directsound_voice_targets(
            capsule.memory,
            instructions,
            indirect_sites,
            allowed_targets,
        )
    )

    # The two adjacent function-pair helpers share one exact captured table and
    # two statically installed cleanup/comparison routines. Admit the whole
    # five-site family only while the table, row addressing, caller arguments,
    # and direct helper calls all still match the decoded title.
    verified.update(
        _phase7_collision_dispatch_targets(
            capsule.memory,
            instructions,
            indirect_sites,
            allowed_targets,
        )
    )
    verified.update(
        _phase7_function_pair_helper_targets(
            capsule.memory,
            instructions,
            indirect_sites,
            allowed_targets,
        )
    )
    verified.update(
        _phase7_d150_callback_targets(
            instructions,
            indirect_sites,
            allowed_targets,
        )
    )
    verified.update(
        _phase7_bb0_callback_targets(
            instructions,
            indirect_sites,
            allowed_targets,
        )
    )
    verified.update(
        _phase7_asset_stream_callback_targets(
            instructions,
            indirect_sites,
            allowed_targets,
        )
    )
    verified.update(
        _phase7_resource_converter_callback_targets(
            capsule.memory,
            instructions,
            indirect_sites,
            allowed_targets,
        )
    )

    # Overlapping module fusion hides a small set of virtual calls from the
    # entry-to-exit edge stream. Bind only the address-named targets recovered
    # from the strict repeated profile and the accepted capsule's immutable
    # vtables. A stale address or target remains unresolved and fail-closed.
    for site, profiled_targets in PHASE7_CAPTURED_INDIRECT_TARGETS.items():
        if site in verified or site not in indirect_sites:
            continue
        profiled_verified_targets = tuple(sorted(set(profiled_targets) & allowed_targets))
        if len(profiled_verified_targets) == len(set(profiled_targets)):
            verified[site] = profiled_verified_targets

    # Finish the offline proof with the complete static address-taken corpus.
    # Bespoke families above retain their narrower target sets; this pass only
    # supplies sites whose exact object lifetime was absent from manual play.
    for site, targets in _phase7_offline_address_taken_targets(
        capsule.memory,
        instructions,
        indirect_sites,
        allowed_targets,
    ).items():
        verified.setdefault(site, targets)

    # Overlapping decoded blocks can place consecutive calls from one native
    # routine in separate store records.  Propagate only a unique measured
    # target for the same vtable slot within a 0x100-byte routine window.
    for site in sorted(indirect_sites):
        if site in verified:
            continue
        signature = _phase6_indirect_signature(instructions[site])
        nearby_target_sets = {
            verified_targets
            for other, verified_targets in verified.items()
            if abs(other - site) <= 0x100
            and _phase6_indirect_signature(instructions[other]) == signature
        }
        if len(nearby_target_sets) == 1:
            verified[site] = next(iter(nearby_target_sets))
    return verified


def _phase6_active_preflight(
    capsule: ReplayCapsule,
    plan: Ia32DecodedStorePlan,
    profile: Ia32CoverageProfile,
    base: _FixedSlicePreflight,
    services: Mapping[int, _HostServiceThunk],
    verified_targets: Mapping[int, Sequence[int]],
) -> _FixedSlicePreflight:
    """Expand Phase-3 planning without dropping the captured scheduler slice."""

    profile_targets = {
        int(record["address"]) for record in _normalized_coverage_profile(profile)["targets"]
    }
    instructions: dict[int, X86Instruction] = dict(base.instructions)
    symbols: set[str] = set(base.function_symbols)
    for function in plan.functions:
        addresses = {instruction.address for instruction in function.instructions}
        if not addresses & profile_targets:
            continue
        symbols.add(function.symbol)
        for instruction in function.instructions:
            instructions.setdefault(instruction.address, instruction)
    pages = set(base.page_addresses)
    scheduler = (capsule.scheduler_state or {}).get("resident_scheduler")
    active_tls_base = capsule.state.fs_base
    if isinstance(scheduler, Mapping):
        active_tls_base = _u32(int(scheduler.get("active_tls_base", active_tls_base)))
    for instruction in instructions.values():
        for operand in instruction.operands:
            if operand.segment == "fs" and operand.absolute is not None:
                pages.add(_u32(active_tls_base + int(operand.absolute)) & ~(PAGE_SIZE - 1))
            elif operand.absolute is not None:
                page = _u32(int(operand.absolute)) & ~(PAGE_SIZE - 1)
                if page == PHASE7_PHYSICAL_ZERO_PAGE or page >= PHYSICAL_ALIAS_START:
                    pages.add(page)
    return _FixedSlicePreflight(
        instructions=instructions,
        page_addresses=tuple(sorted(pages)),
        memory_ranges=base.memory_ranges,
        write_ranges=base.write_ranges,
        function_symbols=tuple(sorted(symbols)),
        indirect_targets={
            int(address): tuple(int(target) for target in targets)
            for address, targets in verified_targets.items()
        },
        host_service_thunks=tuple(services[target] for target in sorted(services)),
        host_service_calls=base.host_service_calls,
        steps=base.steps,
    )


def _phase7_full_flip_oracle(
    capsule: ReplayCapsule,
    *,
    stop_eip: int,
) -> tuple[Mapping[str, Any], CpuState, SparseMemory]:
    scheduler = (capsule.scheduler_state or {}).get("resident_scheduler")
    if not isinstance(scheduler, Mapping):
        raise Ia32BackendError("Phase-7 full-flip capsule omits its resident scheduler")
    oracle = scheduler.get("full_flip_oracle")
    if (
        scheduler.get("capture_mode") != "observed-full-flip"
        or not isinstance(oracle, Mapping)
        or oracle.get("format") != FULL_FLIP_ORACLE_FORMAT
        or int(oracle.get("version", 0)) != 1
    ):
        raise Ia32BackendError(
            "Phase-7 full-flip validation requires a new observed F8-to-flip capsule; "
            "the legacy direct-edge capture is not executable"
        )
    actual_stop = _u32(int(oracle.get("actual_stop_eip", -1)))
    if actual_stop != _u32(stop_eip):
        raise Ia32BackendError(
            "Phase-7 full-flip stop does not match the observed scheduler continuation: "
            f"requested 0x{_u32(stop_eip):08X}, observed 0x{actual_stop:08X}"
        )
    if int(oracle.get("completed_flips", 0)) <= 0:
        raise Ia32BackendError("Phase-7 full-flip oracle contains no completed flip")
    if int(oracle.get("service_trace_overflow_count", 0)) != 0:
        raise Ia32BackendError("Phase-7 full-flip oracle overflowed its service trace")
    resource_name = str(oracle.get("terminal_memory_resource", ""))
    payload = capsule.resources.get(resource_name)
    if payload is None or len(payload) < 12:
        raise Ia32BackendError("Phase-7 full-flip oracle memory resource is missing")
    magic, version, page_count = struct.unpack_from("<4sII", payload)
    expected_size = 12 + page_count * (4 + PAGE_SIZE)
    if (
        magic != FULL_FLIP_ORACLE_RESOURCE_MAGIC
        or version != FULL_FLIP_ORACLE_RESOURCE_VERSION
        or len(payload) != expected_size
        or page_count != int(oracle.get("changed_page_count", -1))
    ):
        raise Ia32BackendError("Phase-7 full-flip oracle memory resource is malformed")
    terminal_memory = SparseMemory.from_pages(capsule.memory.export_pages())
    offset = 12
    for _index in range(page_count):
        page = struct.unpack_from("<I", payload, offset)[0]
        offset += 4
        terminal_memory.write(page * PAGE_SIZE, payload[offset : offset + PAGE_SIZE])
        offset += PAGE_SIZE
    digest = hashlib.sha256()
    for page, page_payload in terminal_memory.export_pages():
        digest.update(struct.pack("<I", page))
        digest.update(page_payload)
    if digest.hexdigest() != oracle.get("terminal_memory_sha256"):
        scheduler_tls_pages = {
            int(scheduler.get("active_tls_base", 0)) // PAGE_SIZE,
            *(
                int(lane.get("tls_base", 0)) // PAGE_SIZE
                for lane in scheduler.get("lanes", [])
                if isinstance(lane, Mapping)
            ),
        }
        scheduler_tls_pages.discard(0)
        captured_digest = hashlib.sha256()
        for page, page_payload in terminal_memory.export_pages():
            if page in scheduler_tls_pages:
                continue
            captured_digest.update(struct.pack("<I", page))
            captured_digest.update(page_payload)
        if captured_digest.hexdigest() != oracle.get("terminal_memory_sha256"):
            raise Ia32BackendError("Phase-7 full-flip terminal-memory identity mismatch")
    terminal_state_record = oracle.get("terminal_cpu_state")
    if not isinstance(terminal_state_record, Mapping):
        raise Ia32BackendError("Phase-7 full-flip oracle omits terminal CPU state")
    return oracle, cpu_state_from_record(terminal_state_record), terminal_memory


def _phase7_full_flip_oracle_page_addresses(
    capsule: ReplayCapsule,
    oracle: Mapping[str, Any],
) -> tuple[int, ...]:
    resource = capsule.resources[str(oracle["terminal_memory_resource"])]
    page_count = struct.unpack_from("<I", resource, 8)[0]
    return tuple(
        sorted(
            struct.unpack_from("<I", resource, 12 + index * (4 + PAGE_SIZE))[0] * PAGE_SIZE
            for index in range(page_count)
        )
    )


def _phase7_observed_state_record(state: CpuState, *, fs_base: int) -> dict[str, Any]:
    """Canonicalize the hardware/decoded FPU alias boundary for a full flip."""

    normalized = normalize_ia32_phase3_state(state)
    normalized.mxcsr &= ~MXCSR_STICKY_STATUS_MASK
    normalized.fpu_status_word &= ~0x7F
    for name in normalized.mmx_registers:
        normalized.mmx_registers[name] = 0
    record: dict[str, Any] = dict(cpu_state_record(normalized))
    record["fs_base"] = _u32(fs_base)
    return record


def _phase7_observed_memory_ranges(
    plan: _Ia32SchedulerPlan,
    terminal_state: CpuState,
) -> tuple[tuple[str, int, int], ...]:
    """Return the exact memory owned by the captured primary-lane boundary."""

    stack_pointer = terminal_state.get_register("esp")
    stack_size = PAGE_SIZE - (stack_pointer & (PAGE_SIZE - 1))
    ranges = [
        ("render-product", plan.render_address, plan.render_size),
        ("audio-product", plan.audio_address, plan.audio_size),
        ("primary-live-stack", stack_pointer, stack_size),
        ("active-tls", plan.active_tls_base, PAGE_SIZE),
        *((f"{lane.name}-tls", lane.tls_base, PAGE_SIZE) for lane in plan.lanes),
    ]
    return tuple(ranges)


def _phase7_replayable_service_trace(
    capsule: ReplayCapsule,
    oracle: Mapping[str, Any],
) -> tuple[tuple[int, int], ...]:
    """Project a global live trace onto the capsule's exact native32 contracts."""

    replayable_targets = {
        _u32(int(record["target"]))
        for record in capsule.service_state.get("service_registry", [])
        if isinstance(record, Mapping)
        and record.get("execution") == "native32"
        and int(record.get("kind", -1)) == NATIVE_HOST_SERVICE_RETURN_CONSTANT
    }
    return tuple(
        (_u32(int(record["target"])), _u32(int(record["result"])))
        for record in oracle.get("service_trace", [])
        if isinstance(record, Mapping) and _u32(int(record.get("target", -1))) in replayable_targets
    )


def _phase7_static_instruction_closure(
    functions: Iterable[LiftedFunction],
    roots: Iterable[int],
    verified_targets: Mapping[int, Sequence[int]],
) -> tuple[dict[int, X86Instruction], set[str]]:
    all_instructions: dict[int, X86Instruction] = {}
    instruction_symbols: dict[int, str] = {}
    for function in functions:
        for instruction in function.instructions:
            all_instructions.setdefault(instruction.address, instruction)
            instruction_symbols.setdefault(instruction.address, function.symbol)
    instructions: dict[int, X86Instruction] = {}
    symbols: set[str] = set()
    pending = [
        address for address in sorted({_u32(root) for root in roots}) if address in all_instructions
    ]
    while pending:
        address = pending.pop()
        if address in instructions:
            continue
        instruction = all_instructions[address]
        instructions[address] = instruction
        symbols.add(instruction_symbols[address])
        mnemonic = instruction.mnemonic.casefold()
        successors: set[int] = set()
        if mnemonic in {"call", "jmp", "jcc"} and instruction.target is not None:
            successors.add(int(instruction.target))
        if mnemonic in {"call_indirect", "jmp_indirect"} or (
            mnemonic in {"call", "jmp"} and instruction.target is None
        ):
            successors.update(verified_targets.get(instruction.address, ()))
        if mnemonic not in {"jmp", "jmp_indirect", "ret"}:
            successors.add(instruction.next_address)
        pending.extend(
            successor
            for successor in sorted(successors, reverse=True)
            if successor in all_instructions and successor not in instructions
        )
    return instructions, symbols


def _phase7_observed_preflight(
    capsule: ReplayCapsule,
    plan: Ia32DecodedStorePlan,
    profile: Ia32CoverageProfile,
    services: Mapping[int, _HostServiceThunk],
    verified_targets: Mapping[int, Sequence[int]],
) -> _FixedSlicePreflight:
    """Build full-flip dependencies from closed profile and captured resident pages."""

    profile_targets = {
        int(record["address"]) for record in _normalized_coverage_profile(profile)["targets"]
    }
    instructions, symbols = _phase7_static_instruction_closure(
        plan.functions,
        (*profile_targets, int(capsule.state.eip)),
        verified_targets,
    )
    if capsule.state.eip not in instructions:
        raise Ia32BackendError(
            f"Phase-7 observed entry 0x{capsule.state.eip:08X} is outside the closed profile"
        )
    pages = {
        page * PAGE_SIZE
        for page, _payload in capsule.memory.export_pages()
        if _mappable_data_page(page * PAGE_SIZE, detached_guest=True)
    }
    scheduler = (capsule.scheduler_state or {}).get("resident_scheduler")
    active_tls_base = capsule.state.fs_base
    if isinstance(scheduler, Mapping):
        active_tls_base = _u32(int(scheduler.get("active_tls_base", active_tls_base)))
    for instruction in instructions.values():
        for operand in instruction.operands:
            if operand.segment == "fs" and operand.absolute is not None:
                pages.add(_u32(active_tls_base + int(operand.absolute)) & ~(PAGE_SIZE - 1))
            elif operand.absolute is not None:
                page = _u32(int(operand.absolute)) & ~(PAGE_SIZE - 1)
                if page == PHASE7_PHYSICAL_ZERO_PAGE or page >= PHYSICAL_ALIAS_START:
                    pages.add(page)
    ranges = _merged_memory_ranges((address, PAGE_SIZE) for address in sorted(pages))
    return _FixedSlicePreflight(
        instructions=instructions,
        page_addresses=tuple(sorted(pages)),
        memory_ranges=ranges,
        write_ranges=(),
        function_symbols=tuple(sorted(symbols)),
        indirect_targets={
            int(address): tuple(int(target) for target in targets)
            for address, targets in verified_targets.items()
        },
        host_service_thunks=tuple(services[target] for target in sorted(services)),
        host_service_calls=(),
        steps=int(
            cast(Mapping[str, Any], scheduler).get("full_flip_oracle", {}).get("step_delta", 0)
        )
        if isinstance(scheduler, Mapping)
        else 0,
    )


def _initialize_normal_live_boot_stack(
    memory: SparseMemory,
    *,
    stack_size: int,
) -> int:
    if stack_size <= 0 or stack_size % PAGE_SIZE:
        raise Ia32BackendError("normal-live XBE stack size is not page aligned")
    for address in range(
        NORMAL_LIVE_BOOT_STACK_TOP - stack_size,
        NORMAL_LIVE_BOOT_STACK_TOP,
        PAGE_SIZE,
    ):
        memory.write(address, bytes(PAGE_SIZE))
    memory.write_u32(
        NORMAL_LIVE_BOOT_STACK_TOP,
        NORMAL_LIVE_BOOT_RETURN_THUNK_ADDRESS,
    )
    return NORMAL_LIVE_BOOT_STACK_TOP


def _initialize_normal_live_boot_kernel_probe(memory: SparseMemory) -> None:
    memory.write(
        NORMAL_LIVE_BOOT_KERNEL_PROBE_START,
        bytes(NORMAL_LIVE_BOOT_KERNEL_PROBE_SIZE),
    )


def _initialize_normal_live_device_pages(
    memory: SparseMemory,
    evidence_memory: SparseMemory,
) -> None:
    """Materialize only the finite MMIO identity shadows used by Phase 7."""

    for page in sorted(PHASE7_DIRECT_MMIO_PAGES):
        if page in PHASE7_CAPTURED_DIRECT_MMIO_PAGES and evidence_memory.has_allocated_page(page):
            memory.write(page, evidence_memory.native_page_snapshot(page))
        else:
            memory.write(page, bytes(PAGE_SIZE))
    # The XDK idle wait at 0x21DD7A requires both PFIFO engines to report
    # idle. The discovery memory model publishes the same finite read
    # semantics; the third status byte at 0xFD003220 intentionally stays zero.
    memory.write_u32(PHASE7_GPU_PFIFO_RUNOUT_STATUS_ADDRESS, PHASE7_GPU_PFIFO_IDLE_BIT)
    memory.write_u32(PHASE7_GPU_PFIFO_CACHE1_STATUS_ADDRESS, PHASE7_GPU_PFIFO_IDLE_BIT)


def _initialize_normal_live_boot_tls(
    memory: SparseMemory,
    *,
    tls_index_address: int,
    raw_payload: bytes,
    zero_fill_size: int,
) -> None:
    payload_size = len(raw_payload) + zero_fill_size
    if not tls_index_address or zero_fill_size < 0 or payload_size > 0xC00:
        raise Ia32BackendError("normal-live XBE TLS directory is invalid")
    memory.write_u32(tls_index_address, 0)
    memory.write_u32(NORMAL_LIVE_BOOT_TLS_BASE + 0x04, NORMAL_LIVE_BOOT_TLS_VECTOR)
    memory.write_u32(NORMAL_LIVE_BOOT_TLS_BASE + 0x20, NORMAL_LIVE_BOOT_TLS_BASE)
    memory.write_u32(NORMAL_LIVE_BOOT_TLS_BASE + 0x28, 0)
    memory.write_u32(NORMAL_LIVE_BOOT_TLS_BASE + 0x250, 0)
    memory.write_u32(NORMAL_LIVE_BOOT_TLS_VECTOR, NORMAL_LIVE_BOOT_TLS_DATA)
    memory.write(
        NORMAL_LIVE_BOOT_TLS_DATA,
        raw_payload + bytes(zero_fill_size),
    )


def _normal_live_boot_capsule(
    evidence_capsule: ReplayCapsule,
    *,
    xbe_path: Path,
    stop_eip: int,
) -> ReplayCapsule:
    """Create the deterministic XBE-entry state consumed by the native worker."""

    from runtime.xbox.shims import (
        XBOX_KEY_DATA_SIZE,
        XboxRuntimeConfig,
        XboxRuntimeShims,
    )
    from tools.loader.xbe_loader import ImportResolver
    from tools.playability.playability_probe import (
        RuntimeAbiBridge,
        XbeBackedSparseMemory,
    )

    active_tls_base = NORMAL_LIVE_BOOT_TLS_BASE
    source = load_xbe_file(xbe_path, resolve_imports=False)
    info = source.info
    certificate_address = int(info["certificate"]["virtual_address"])
    lan_key = source.arena.read(certificate_address + 0xB0, XBOX_KEY_DATA_SIZE)
    signature_key = source.arena.read(
        certificate_address + 0xC0,
        XBOX_KEY_DATA_SIZE,
    )
    alternate_signature_keys = tuple(
        source.arena.read(
            certificate_address + 0xD0 + index * XBOX_KEY_DATA_SIZE,
            XBOX_KEY_DATA_SIZE,
        )
        for index in range(16)
    )
    runtime = XboxRuntimeShims(
        XboxRuntimeConfig(
            extracted_disc_root=xbe_path.parent,
            title_id=int(info["certificate"]["title_id"]["hex"], 16),
            xbox_lan_key=lan_key,
            xbox_signature_key=signature_key,
            xbox_alternate_signature_keys=alternate_signature_keys,
        )
    )
    resolver = ImportResolver()
    runtime.register_kernel_imports(
        resolver,
        [int(record["ordinal"]) for record in info["kernel_imports"]["imports"]],
    )
    loaded = load_xbe_file(xbe_path, resolver=resolver)
    bridge = RuntimeAbiBridge(runtime)
    memory = XbeBackedSparseMemory(loaded, {})
    _initialize_normal_live_boot_kernel_probe(memory)
    _initialize_normal_live_device_pages(memory, evidence_capsule.memory)
    stack_base = _initialize_normal_live_boot_stack(
        memory,
        stack_size=int(info["header"]["stack_size"]),
    )
    bridge.synchronize_data_exports(memory)
    tls = info["tls"]
    if int(tls["tls_index_address"]) != NORMAL_LIVE_TLS_INDEX_ADDRESS:
        raise Ia32BackendError("normal-live XBE TLS index address changed")
    raw_start = int(tls["raw_data_start_address"])
    raw_end = int(tls["raw_data_end_address"])
    if raw_end < raw_start:
        raise Ia32BackendError("normal-live XBE TLS raw-data range is invalid")
    _initialize_normal_live_boot_tls(
        memory,
        tls_index_address=int(tls["tls_index_address"]),
        raw_payload=(
            source.arena.read(raw_start, raw_end - raw_start) if raw_end > raw_start else b""
        ),
        zero_fill_size=int(tls["zero_fill_size"]),
    )
    boot_memory = memory.replay_capsule_memory_snapshot()

    state = CpuState.with_registers(
        esp=stack_base,
        ebp=0,
        esi=0,
        edi=0,
        fs_base=active_tls_base,
    )
    state.eip = int(loaded.entry_point)
    boot_eflags = _eflags_from_state(state)
    scheduler_state = {
        "backend": "native-ia32-normal-live",
        "resident_scheduler": {
            "version": 1,
            "capture_mode": "normal-live-dynamic",
            "active_tls_base": active_tls_base,
            "products": {
                "render": {"address": 0x002256B8, "size": 4},
                "audio": {"address": 0x0010AB93, "size": 4},
            },
            "lanes": [
                {
                    "name": "primary",
                    "initial_eip": state.eip,
                    "tls_base": 0x73000000,
                    "initial_state": "ready",
                    "registers": {name: state.get_register(name) for name in REGISTER_NAMES},
                    "eflags": boot_eflags,
                },
                {
                    "name": "worker",
                    "initial_eip": state.eip,
                    "tls_base": 0x73010000,
                    "initial_state": "completed",
                    "registers": {name: state.get_register(name) for name in REGISTER_NAMES},
                    "eflags": boot_eflags,
                },
                {
                    "name": "vblank",
                    "initial_eip": state.eip,
                    "tls_base": 0x73020000,
                    "initial_state": "completed",
                    "registers": {name: state.get_register(name) for name in REGISTER_NAMES},
                    "eflags": boot_eflags,
                },
            ],
            "steps": [
                {
                    "sequence": 0,
                    "lane": "primary",
                    "entry_eip": state.eip,
                    "next_eip": _u32(stop_eip),
                    "exit": "complete",
                }
            ],
        },
    }
    identity = _sha256(
        _canonical_json(
            {
                "format": NORMAL_LIVE_BOOT_IMAGE_FORMAT,
                "version": NORMAL_LIVE_BOOT_IMAGE_VERSION,
                "evidence_capsule_id": evidence_capsule.capsule_id,
                "xbe_sha256": sha256_file(xbe_path),
                "entry_eip": state.eip,
                "stop_eip": _u32(stop_eip),
            }
        )
    )
    return ReplayCapsule(
        path=xbe_path.resolve(),
        manifest={
            "format": NORMAL_LIVE_BOOT_IMAGE_FORMAT,
            "version": NORMAL_LIVE_BOOT_IMAGE_VERSION,
            "content_sha256": identity,
            "source_evidence_capsule_id": evidence_capsule.capsule_id,
        },
        state=state,
        memory=boot_memory,
        functions=evidence_capsule.functions,
        scheduler_state=scheduler_state,
        service_state=evidence_capsule.service_state,
        events={},
        resources={},
    )


def build_ia32_coverage_growth_artifact(
    capsule: ReplayCapsule,
    *,
    xbe_path: Path,
    decoded_block_store_path: Path,
    coverage_profile: Ia32CoverageProfile,
    stop_eip: int,
    build_dir: Path,
    compiler: Path | None = None,
    kernel32_library: Path | None = None,
    profile: Ia32ArchitectureProfile | None = None,
    normal_launcher_cutover: bool = False,
    normal_live_launch: bool = False,
    require_full_flip_oracle: bool = False,
    observed_indirect_memory: SparseMemory | None = None,
) -> Ia32SliceArtifact:
    """Build a closed Phase-6 vertical-slice artifact over every prior native layer."""

    active_profile = profile or Ia32ArchitectureProfile()
    coverage_profile = _phase6_profile_scheduler_boundaries(coverage_profile)
    services = _phase6_host_service_thunks(
        capsule,
        coverage_profile,
        complete_offline_registry=normal_live_launch,
    )
    capsule = _capsule_with_host_services(capsule, services)
    if require_full_flip_oracle:
        _phase7_full_flip_oracle(capsule, stop_eip=_u32(stop_eip))
        captured_preflight = None
    elif normal_live_launch:
        captured_preflight = None
    else:
        captured_preflight = _fixed_slice_preflight(capsule, _u32(stop_eip))
    plan = inspect_ia32_decoded_store(
        xbe_path,
        decoded_block_store_path,
        host_services=services,
    )
    expanded_capsule = replace(capsule, functions=plan.functions)
    verified_targets: dict[int, tuple[int, ...]] = {}
    if plan.indirect_target_table["sites"]:
        preflight_targets = (
            {}
            if require_full_flip_oracle or normal_live_launch
            else _fixed_slice_preflight(
                expanded_capsule,
                _u32(stop_eip),
            ).indirect_targets
        )
        verified_targets = _phase6_verified_indirect_targets(
            expanded_capsule,
            plan,
            coverage_profile,
            services,
            preflight_targets,
            observed_memory=observed_indirect_memory,
        )
        plan = inspect_ia32_decoded_store(
            xbe_path,
            decoded_block_store_path,
            host_services=services,
            verified_indirect_targets=verified_targets,
        )
        expanded_capsule = replace(expanded_capsule, functions=plan.functions)
    if normal_live_launch:
        decoded_instructions = {
            instruction.address: instruction
            for function in plan.functions
            for instruction in function.instructions
        }
        _validate_phase7_audio_buffer_mmio_family(decoded_instructions)
        _validate_phase7_vblank_callback_family(decoded_instructions)
    active_preflight = (
        _phase7_observed_preflight(
            expanded_capsule,
            plan,
            coverage_profile,
            services,
            verified_targets,
        )
        if require_full_flip_oracle or normal_live_launch
        else _phase6_active_preflight(
            expanded_capsule,
            plan,
            coverage_profile,
            cast(_FixedSlicePreflight, captured_preflight),
            services,
            verified_targets,
        )
    )
    direct_identity_pages = {
        page
        for page in active_preflight.page_addresses
        if page in PHASE7_DIRECT_MMIO_PAGES or page == PHASE7_PHYSICAL_ZERO_PAGE
    }
    if normal_live_launch:
        missing_vblank_instructions = sorted(
            set(PHASE7_VBLANK_CALLBACK_INSTRUCTION_BYTES) - set(active_preflight.instructions)
        )
        if missing_vblank_instructions:
            raise Ia32BackendError(
                "normal-live coverage omitted the finite vblank callback family: "
                + ", ".join(f"0x{address:08X}" for address in missing_vblank_instructions)
            )
        missing_audio_buffer_pages = sorted(
            PHASE7_AUDIO_BUFFER_MMIO_PAGES - set(active_preflight.page_addresses)
        )
        if missing_audio_buffer_pages:
            raise Ia32BackendError(
                "normal-live audio buffer MMIO family omitted direct shadow pages: "
                + ", ".join(f"0x{page:08X}" for page in missing_audio_buffer_pages)
            )
        _validate_phase7_audio_sg_mmio_family(active_preflight.instructions)
        if PHASE7_AUDIO_SG_MMIO_PAGE not in active_preflight.page_addresses:
            raise Ia32BackendError(
                "normal-live audio SG MMIO family omitted its direct shadow page"
            )
        _validate_phase7_mcpx_indexed_mmio_family(active_preflight.instructions)
        if PHASE7_MCPX_MMIO_PAGE not in active_preflight.page_addresses:
            raise Ia32BackendError(
                "normal-live MCPX indexed MMIO family omitted its direct shadow page"
            )
        _validate_phase7_audio_dsp_mmio_family(active_preflight.instructions)
        if PHASE7_AUDIO_DSP_MMIO_PAGE not in active_preflight.page_addresses:
            raise Ia32BackendError(
                "normal-live audio DSP MMIO family omitted its direct shadow page"
            )
        direct_identity_pages.add(PHASE7_AUDIO_DSP_MMIO_PAGE)
    if direct_identity_pages:
        active_profile = replace(
            active_profile,
            mmio_shadow_pages={
                **(active_profile.mmio_shadow_pages or {}),
                **{page: page for page in sorted(direct_identity_pages)},
            },
            dirty_page_owners={
                **(active_profile.dirty_page_owners or {}),
                **{page: "audio" for page in PHASE7_AUDIO_BUFFER_MMIO_PAGES},
                PHASE7_AUDIO_SG_MMIO_PAGE: "audio",
                PHASE7_MCPX_MMIO_PAGE: "audio",
                PHASE7_AUDIO_DSP_MMIO_PAGE: "audio",
            },
        )
    architecture_capsule = expanded_capsule
    scheduler = (expanded_capsule.scheduler_state or {}).get("resident_scheduler")
    if isinstance(scheduler, Mapping):
        architecture_state = cpu_state_from_record(cpu_state_record(expanded_capsule.state))
        architecture_state.fs_base = _u32(
            int(scheduler.get("active_tls_base", architecture_state.fs_base))
        )
        architecture_capsule = replace(expanded_capsule, state=architecture_state)
    architecture_plan = _phase3_architecture_plan(
        architecture_capsule,
        active_preflight,
        stop_eip=_u32(stop_eip),
        profile=active_profile,
        resident_privileged_boundary=True,
        occupied_instruction_bytes=(
            address
            for function in plan.functions
            for instruction in function.instructions
            for address in range(instruction.address, instruction.next_address)
        ),
    )
    implemented_addresses = {int(record["address"]) for record in architecture_plan.rewrites} | {
        instruction.address
        for instruction in active_preflight.instructions.values()
        if _resident_fail_closed_trap(instruction)
    }
    coverage_growth_plan = plan_ia32_coverage_growth(
        plan,
        coverage_profile,
        registered_service_targets=services,
        implemented_rewrite_addresses=implemented_addresses,
    )
    return _build_ia32_slice_artifact(
        expanded_capsule,
        stop_eip=stop_eip,
        build_dir=build_dir,
        compiler=compiler,
        kernel32_library=kernel32_library,
        persistent_worker=True,
        decoded_store_plan=plan,
        architecture_profile=active_profile,
        host_abi=True,
        resident_scheduler=True,
        coverage_growth_plan=coverage_growth_plan,
        normal_launcher_cutover=normal_launcher_cutover,
        normal_live_launch=normal_live_launch,
        prepared_preflight=active_preflight,
        prepared_architecture_plan=architecture_plan,
        declared_host_services=tuple(services[target] for target in sorted(services)),
        coverage_boundary_spans=(
            (
                IA32_VBLANK_RETURN_SENTINEL,
                _jump_patch(
                    IA32_VBLANK_RETURN_SENTINEL,
                    (
                        NORMAL_LIVE_VBLANK_EXIT_THUNK_ADDRESS
                        if normal_live_launch
                        else EXIT_THUNK_ADDRESS
                    ),
                ),
            ),
        ),
    )


def build_ia32_launcher_cutover_artifact(
    capsule: ReplayCapsule,
    *,
    xbe_path: Path,
    decoded_block_store_path: Path,
    coverage_profile: Ia32CoverageProfile,
    stop_eip: int,
    build_dir: Path,
    compiler: Path | None = None,
    kernel32_library: Path | None = None,
    profile: Ia32ArchitectureProfile | None = None,
    require_full_flip_oracle: bool = False,
) -> Ia32SliceArtifact:
    """Build the Phase-7 default-backend artifact with resident memory ownership."""

    return build_ia32_coverage_growth_artifact(
        capsule,
        xbe_path=xbe_path,
        decoded_block_store_path=decoded_block_store_path,
        coverage_profile=coverage_profile,
        stop_eip=stop_eip,
        build_dir=build_dir,
        compiler=compiler,
        kernel32_library=kernel32_library,
        profile=profile,
        normal_launcher_cutover=True,
        require_full_flip_oracle=require_full_flip_oracle,
    )


def _write_normal_live_artifact_index(
    artifact: Ia32SliceArtifact,
    *,
    build_dir: Path,
) -> Path:
    index_path = build_dir.resolve() / "manifest.json"
    relative_manifest = artifact.manifest_path.resolve().relative_to(index_path.parent)
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_bytes(
        _canonical_json(
            {
                "format": "b2-recomp-ia32-artifact-index",
                "version": 1,
                "artifact_id": artifact.artifact_id,
                "artifact_manifest": relative_manifest.as_posix(),
                "artifact_manifest_sha256": sha256_file(artifact.manifest_path),
            }
        )
    )
    return index_path


def build_ia32_normal_live_artifact(
    evidence_capsule: ReplayCapsule,
    *,
    xbe_path: Path,
    decoded_block_store_path: Path,
    coverage_profile: Ia32CoverageProfile,
    stop_eip: int,
    build_dir: Path,
    compiler: Path | None = None,
    kernel32_library: Path | None = None,
    profile: Ia32ArchitectureProfile | None = None,
) -> Ia32SliceArtifact:
    """Build and index the XBE-entry Phase-7 ordinary-live artifact."""

    capsule = _normal_live_boot_capsule(
        evidence_capsule,
        xbe_path=xbe_path,
        stop_eip=stop_eip,
    )
    artifact = build_ia32_coverage_growth_artifact(
        capsule,
        xbe_path=xbe_path,
        decoded_block_store_path=decoded_block_store_path,
        coverage_profile=coverage_profile,
        stop_eip=stop_eip,
        build_dir=build_dir,
        compiler=compiler,
        kernel32_library=kernel32_library,
        profile=profile,
        normal_launcher_cutover=True,
        normal_live_launch=True,
        observed_indirect_memory=evidence_capsule.memory,
    )
    _write_normal_live_artifact_index(artifact, build_dir=build_dir)
    return artifact


def _eflags_from_state(state: CpuState) -> int:
    flags = 0x2
    flags |= int(state.flags.cf) << 0
    flags |= int(state.flags.pf) << 2
    flags |= int(state.flags.af) << 4
    flags |= int(state.flags.zf) << 6
    flags |= int(state.flags.sf) << 7
    flags |= int(state.flags.interrupt_enabled) << 9
    flags |= int(state.flags.df) << 10
    flags |= int(state.flags.of) << 11
    return flags


def _flags_from_eflags(value: int) -> CpuFlags:
    return CpuFlags(
        cf=bool(value & (1 << 0)),
        pf=bool(value & (1 << 2)),
        af=bool(value & (1 << 4)),
        zf=bool(value & (1 << 6)),
        sf=bool(value & (1 << 7)),
        interrupt_enabled=bool(value & (1 << 9)),
        df=bool(value & (1 << 10)),
        of=bool(value & (1 << 11)),
    )


def normalize_ia32_phase0_state(state: CpuState) -> CpuState:
    """Apply the Phase-0 comparison mask; sticky MXCSR status is reported separately."""

    normalized = cpu_state_from_record(cpu_state_record(state))
    normalized.fpu_control_word &= X87_CONTROL_WORD_OBSERVABLE_MASK
    normalized.mxcsr &= MXCSR_PHASE0_COMPARISON_MASK
    return normalized


def normalize_ia32_phase3_state(state: CpuState) -> CpuState:
    """Normalize physical alias bookkeeping while preserving Phase-3 observables."""

    normalized = cpu_state_from_record(cpu_state_record(state))
    normalized.fpu_control_word &= X87_CONTROL_WORD_OBSERVABLE_MASK
    normalized.mxcsr &= MXCSR_DEFINED_MASK
    # TOP is a physical-stack index; logical stack order is compared directly.
    normalized.fpu_status_word &= ~(0x7 << 11)
    if normalized.fpu_stack:
        for name in normalized.mmx_registers:
            normalized.mmx_registers[name] = 0
    elif any(normalized.mmx_registers.values()):
        normalized.fpu_stack = []
    return normalized


def _encode_fxsave(state: CpuState) -> bytes:
    payload = bytearray(EXCHANGE_FXSAVE_SIZE)
    struct.pack_into("<H", payload, 0, state.fpu_control_word & 0xFFFF)
    struct.pack_into("<H", payload, 2, state.fpu_status_word & 0xFFFF)
    struct.pack_into("<I", payload, 24, state.mxcsr & 0xFFFFFFFF)
    struct.pack_into("<I", payload, 28, 0x0000FFFF)
    for index in range(8):
        lanes = state.get_xmm_register(f"xmm{index}")
        struct.pack_into("<4f", payload, 160 + index * 16, *lanes)
    return bytes(payload)


def _decode_fxsave(state: CpuState, payload: bytes) -> None:
    if len(payload) != EXCHANGE_FXSAVE_SIZE:
        raise Ia32BackendError("IA-32 helper returned a malformed FXSAVE record")
    state.fpu_control_word = struct.unpack_from("<H", payload, 0)[0]
    state.fpu_status_word = struct.unpack_from("<H", payload, 2)[0]
    state.mxcsr = struct.unpack_from("<I", payload, 24)[0]
    state.fpu_stack = []
    for index in range(8):
        state.set_xmm_register(f"xmm{index}", struct.unpack_from("<4f", payload, 160 + index * 16))


def _float_to_extended80(value: float) -> bytes:
    sign = 0x8000 if math.copysign(1.0, value) < 0.0 else 0
    magnitude = abs(value)
    if math.isnan(magnitude):
        return struct.pack("<QH", 0xC000000000000000, sign | 0x7FFF)
    if math.isinf(magnitude):
        return struct.pack("<QH", 0x8000000000000000, sign | 0x7FFF)
    if magnitude == 0.0:
        return struct.pack("<QH", 0, sign)
    fraction, exponent = math.frexp(magnitude)
    significand = int(math.ldexp(fraction, 64))
    extended_exponent = exponent - 1 + 16383
    if extended_exponent <= 0:
        significand >>= 1 - extended_exponent
        extended_exponent = 0
    return struct.pack(
        "<QH",
        significand & 0xFFFFFFFFFFFFFFFF,
        sign | (extended_exponent & 0x7FFF),
    )


def _extended80_to_float(payload: bytes) -> float:
    if len(payload) != 10:
        raise Ia32BackendError("malformed 80-bit x87 register")
    significand, sign_exponent = struct.unpack("<QH", payload)
    sign = -1.0 if sign_exponent & 0x8000 else 1.0
    exponent = sign_exponent & 0x7FFF
    if exponent == 0x7FFF:
        if significand == 0x8000000000000000:
            return math.copysign(math.inf, sign)
        return math.nan
    if significand == 0:
        return math.copysign(0.0, sign)
    unbiased = (1 - 16383 if exponent == 0 else exponent - 16383) - 63
    return sign * math.ldexp(float(significand), unbiased)


def _encode_phase3_fxsave(state: CpuState) -> bytes:
    payload = bytearray(_encode_fxsave(state))
    top = (state.fpu_status_word >> 11) & 0x7
    tag = 0
    if state.fpu_stack:
        if len(state.fpu_stack) > 8:
            raise Ia32BackendError("Phase-3 x87 input exceeds the eight-register stack")
        for logical_index, value in enumerate(state.fpu_stack):
            physical_index = (top + logical_index) & 0x7
            tag |= 1 << physical_index
            # FXSAVE stores the register payloads in logical ST(i) order while
            # its abridged tag byte remains indexed by physical register.
            start = 32 + logical_index * 16
            payload[start : start + 10] = _float_to_extended80(value)
    elif any(state.mmx_registers.values()):
        top = 0
        struct.pack_into(
            "<H",
            payload,
            2,
            (state.fpu_status_word & ~(0x7 << 11)),
        )
        tag = 0xFF
        for index in range(8):
            struct.pack_into(
                "<QH",
                payload,
                32 + index * 16,
                state.get_mmx_register(f"mm{index}"),
                0xFFFF,
            )
    payload[4] = tag
    return bytes(payload)


def _decode_phase3_fxsave(state: CpuState, payload: bytes) -> None:
    _decode_fxsave(state, payload)
    tag = payload[4]
    top = (state.fpu_status_word >> 11) & 0x7
    populated = [index for index in range(8) if tag & (1 << index)]
    mmx_mode = bool(populated) and all(
        struct.unpack_from("<H", payload, 32 + index * 16 + 8)[0] == 0xFFFF for index in populated
    )
    state.fpu_stack = []
    for index in range(8):
        state.set_mmx_register(f"mm{index}", 0)
    if mmx_mode:
        for index in populated:
            state.set_mmx_register(
                f"mm{index}",
                struct.unpack_from("<Q", payload, 32 + index * 16)[0],
            )
        return
    for logical_index in range(8):
        physical_index = (top + logical_index) & 0x7
        if not tag & (1 << physical_index):
            break
        start = 32 + logical_index * 16
        state.fpu_stack.append(_extended80_to_float(payload[start : start + 10]))


def _exchange_payload(
    capsule: ReplayCapsule,
    artifact: Ia32SliceArtifact,
    *,
    exchange_version: int = EXCHANGE_VERSION,
    protocol_version: int = 0,
    worker_command: int = 0,
) -> bytearray:
    state = capsule.state
    pages = tuple(int(address) for address in artifact.manifest["page_addresses"])
    phase3 = artifact.manifest.get("architecture_memory") is True
    phase4 = artifact.manifest.get("host_abi") is True
    phase5 = artifact.manifest.get("resident_scheduler") is True
    phase7 = artifact.manifest.get("normal_launcher_cutover") is True
    service_bytes = (
        HOST_SERVICE_BROKER_CONTROL_SIZE
        + HOST_SERVICE_TRACE_HEADER_SIZE
        + HOST_SERVICE_TRACE_CAPACITY * HOST_SERVICE_TRACE_RECORD_SIZE
        if phase4
        else 0
    )
    scheduler_bytes = (
        RESIDENT_SCHEDULER_HEADER_SIZE
        + RESIDENT_SCHEDULER_LANE_COUNT * RESIDENT_SCHEDULER_LANE_RECORD_SIZE
        + RESIDENT_SCHEDULER_TRACE_CAPACITY * RESIDENT_SCHEDULER_TRACE_RECORD_SIZE
        if phase5
        else 0
    )
    payload = bytearray(
        EXCHANGE_HEADER_SIZE
        + len(pages) * PAGE_SIZE
        + (len(pages) if phase3 else 0)
        + service_bytes
        + scheduler_bytes
        + (CUTOVER_MEMORY_HEADER_SIZE if phase7 else 0)
    )
    struct.pack_into(
        "<6I2Q2I",
        payload,
        0,
        EXCHANGE_MAGIC,
        exchange_version,
        EXCHANGE_STATUS_PENDING,
        protocol_version,
        len(pages),
        worker_command,
        0,
        0,
        state.eip,
        _eflags_from_state(state),
    )
    for index, name in enumerate(REGISTER_NAMES):
        struct.pack_into("<I", payload, 48 + index * 4, state.get_register(name))
    payload[EXCHANGE_FXSAVE_OFFSET : EXCHANGE_FXSAVE_OFFSET + EXCHANGE_FXSAVE_SIZE] = (
        _encode_phase3_fxsave(state) if phase3 else _encode_fxsave(state)
    )
    bindings = {
        int(record["mapped_address"]): tuple(int(value) for value in record["logical_addresses"])
        for record in artifact.manifest.get("phase3_page_bindings", [])
        if isinstance(record, dict)
    }
    for index, page_address in enumerate(pages):
        start = EXCHANGE_HEADER_SIZE + index * PAGE_SIZE
        logical_addresses = bindings.get(page_address, (page_address,))
        page_payload = capsule.memory.read(logical_addresses[0], PAGE_SIZE)
        for logical_address in logical_addresses[1:]:
            candidate = capsule.memory.read(logical_address, PAGE_SIZE)
            if candidate != page_payload:
                raise Ia32BackendError(
                    f"Phase-3 aliases for mapped page 0x{page_address:08X} have "
                    "conflicting input bytes"
                )
        payload[start : start + PAGE_SIZE] = page_payload
    if phase4:
        broker_offset, trace_offset = _phase4_service_offsets(len(pages))
        struct.pack_into(
            "<16I",
            payload,
            broker_offset,
            HOST_SERVICE_BROKER_MAGIC,
            HOST_SERVICE_BROKER_VERSION,
            HOST_SERVICE_BROKER_STATUS_IDLE,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
        )
        struct.pack_into(
            "<4I",
            payload,
            trace_offset,
            0,
            0,
            HOST_SERVICE_TRACE_CAPACITY,
            1,
        )
    if phase5:
        scheduler_offset, _lanes_offset, _traces_offset = _phase5_scheduler_offsets(len(pages))
        struct.pack_into(
            "<16I",
            payload,
            scheduler_offset,
            RESIDENT_SCHEDULER_MAGIC,
            RESIDENT_SCHEDULER_VERSION,
            RESIDENT_SCHEDULER_LANE_COUNT,
            int(artifact.manifest["resident_scheduler_step_count"]),
            0,
            RESIDENT_SCHEDULER_TRACE_CAPACITY,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
        )
    if phase7:
        memory_offset = _phase7_memory_offset(len(pages))
        struct.pack_into(
            f"<{CUTOVER_MEMORY_HEADER_WORDS}I",
            payload,
            memory_offset,
            CUTOVER_MEMORY_MAGIC,
            CUTOVER_MEMORY_VERSION,
            *([0] * (CUTOVER_MEMORY_HEADER_WORDS - 2)),
        )
    return payload


def _cutover_command_segments(
    capsule: ReplayCapsule,
    artifact: Ia32SliceArtifact,
    *,
    worker_command: int,
) -> tuple[bytes, int, bytes]:
    """Build only Phase-7 control records; resident page bytes stay untouched."""

    if artifact.manifest.get("normal_launcher_cutover") is not True:
        raise Ia32BackendError("selective command publication requires a Phase-7 artifact")
    state = capsule.state
    pages = tuple(int(address) for address in artifact.manifest["page_addresses"])
    header = bytearray(EXCHANGE_HEADER_SIZE)
    struct.pack_into(
        "<6I2Q2I",
        header,
        0,
        EXCHANGE_MAGIC,
        CUTOVER_EXCHANGE_VERSION,
        EXCHANGE_STATUS_PENDING,
        WORKER_PROTOCOL_VERSION,
        len(pages),
        worker_command,
        0,
        0,
        state.eip,
        _eflags_from_state(state),
    )
    for index, name in enumerate(REGISTER_NAMES):
        struct.pack_into("<I", header, 48 + index * 4, state.get_register(name))
    header[EXCHANGE_FXSAVE_OFFSET : EXCHANGE_FXSAVE_OFFSET + EXCHANGE_FXSAVE_SIZE] = (
        _encode_phase3_fxsave(state)
    )
    tail_offset = _phase4_service_offsets(len(pages))[0]
    memory_offset = _phase7_memory_offset(len(pages))
    tail = bytearray(memory_offset - tail_offset)
    broker_offset, trace_offset = _phase4_service_offsets(len(pages))
    struct.pack_into(
        "<16I",
        tail,
        broker_offset - tail_offset,
        HOST_SERVICE_BROKER_MAGIC,
        HOST_SERVICE_BROKER_VERSION,
        HOST_SERVICE_BROKER_STATUS_IDLE,
        *([0] * 13),
    )
    struct.pack_into(
        "<4I",
        tail,
        trace_offset - tail_offset,
        0,
        0,
        HOST_SERVICE_TRACE_CAPACITY,
        1,
    )
    scheduler_offset, _lanes_offset, _traces_offset = _phase5_scheduler_offsets(len(pages))
    struct.pack_into(
        "<16I",
        tail,
        scheduler_offset - tail_offset,
        RESIDENT_SCHEDULER_MAGIC,
        RESIDENT_SCHEDULER_VERSION,
        RESIDENT_SCHEDULER_LANE_COUNT,
        int(artifact.manifest["resident_scheduler_step_count"]),
        0,
        RESIDENT_SCHEDULER_TRACE_CAPACITY,
        *([0] * 10),
    )
    return bytes(header), tail_offset, bytes(tail)


def _artifact_exchange_version(manifest: Mapping[str, Any]) -> int:
    if manifest.get("normal_launcher_cutover") is True:
        return CUTOVER_EXCHANGE_VERSION
    if manifest.get("resident_scheduler") is True:
        return RESIDENT_SCHEDULER_EXCHANGE_VERSION
    if manifest.get("host_abi") is True:
        return HOST_ABI_EXCHANGE_VERSION
    if manifest.get("architecture_memory") is True:
        return ARCHITECTURE_EXCHANGE_VERSION
    return WORKER_EXCHANGE_VERSION


def _apply_phase3_timestamp_accounting(
    state: CpuState,
    manifest: Mapping[str, Any],
) -> None:
    """Advance standalone deterministic RDTSC state, never dormant resident sites."""

    if manifest.get("resident_scheduler") is True:
        return
    state.timestamp_counter = (
        state.timestamp_counter
        + int(manifest.get("phase3_timestamp_step_count", 0)) * DETERMINISTIC_TSC_STEP
    ) & 0xFFFFFFFFFFFFFFFF


def _result_from_exchange(
    capsule: ReplayCapsule,
    artifact: Ia32SliceArtifact,
    payload: bytes | bytearray | mmap.mmap,
    *,
    process_elapsed_ns: int,
    expected_exchange_version: int = EXCHANGE_VERSION,
    resident_worker: bool = False,
    worker_process_id: int | None = None,
    worker_dispatch_index: int = 0,
    worker_startup_ns: int = 0,
    resident_dispatch_ns: int = 0,
    resident_memory: SparseMemory | None = None,
    snapshot_memory: bool = False,
) -> Ia32SliceResult:
    magic, version, status, error, page_count, _reserved = struct.unpack_from("<6I", payload)
    if magic != EXCHANGE_MAGIC or version != expected_exchange_version:
        raise Ia32BackendError("IA-32 helper corrupted the exchange header")
    if status != EXCHANGE_STATUS_COMPLETE:
        raise Ia32BackendError(f"IA-32 helper stopped with status {status}, error {error}")
    expected_pages = tuple(int(address) for address in artifact.manifest["page_addresses"])
    if page_count != len(expected_pages):
        raise Ia32BackendError("IA-32 helper returned the wrong page count")
    ticks, frequency, eip, eflags = struct.unpack_from("<2Q2I", payload, 24)
    if frequency <= 0:
        raise Ia32BackendError("IA-32 helper returned an invalid performance frequency")
    state = cpu_state_from_record(cpu_state_record(capsule.state))
    result_memory = resident_memory
    if result_memory is None:
        _unused_state, result_memory = clone_capsule_state(capsule)
    state.eip = eip
    state.flags = _flags_from_eflags(eflags)
    for index, name in enumerate(REGISTER_NAMES):
        state.set_register(name, struct.unpack_from("<I", payload, 48 + index * 4)[0])
    phase3 = artifact.manifest.get("architecture_memory") is True
    fxsave = bytes(payload[EXCHANGE_FXSAVE_OFFSET : EXCHANGE_FXSAVE_OFFSET + EXCHANGE_FXSAVE_SIZE])
    if phase3:
        _decode_phase3_fxsave(state, fxsave)
    else:
        _decode_fxsave(state, fxsave)
    bindings = {
        int(record["mapped_address"]): (
            tuple(int(value) for value in record["logical_addresses"]),
            str(record["owner"]),
        )
        for record in artifact.manifest.get("phase3_page_bindings", [])
        if isinstance(record, dict)
    }
    dirty_flags_offset = EXCHANGE_HEADER_SIZE + len(expected_pages) * PAGE_SIZE
    dirty_publications: list[dict[str, Any]] = []
    dirty_only = artifact.manifest.get("normal_launcher_cutover") is True
    for index, page_address in enumerate(expected_pages):
        start = EXCHANGE_HEADER_SIZE + index * PAGE_SIZE
        logical_addresses, owner = bindings.get(page_address, ((page_address,), "guest"))
        page_state = (
            payload[dirty_flags_offset + index]
            if phase3 and dirty_flags_offset + index < len(payload)
            else CUTOVER_PAGE_CLEAN
        )
        if dirty_only and page_state not in {
            CUTOVER_PAGE_CLEAN,
            CUTOVER_PAGE_NATIVE_DIRTY,
        }:
            raise Ia32BackendError(
                f"Phase-7 worker returned invalid page state {page_state} for 0x{page_address:08X}"
            )
        native_dirty = bool(
            phase3 and page_state == CUTOVER_PAGE_NATIVE_DIRTY
            if dirty_only
            else phase3 and page_state
        )
        if dirty_only and not native_dirty:
            continue
        output_page = bytes(payload[start : start + PAGE_SIZE])
        input_page = result_memory.read(logical_addresses[0], PAGE_SIZE)
        for logical_address in logical_addresses:
            result_memory.write(logical_address, output_page)
        if native_dirty:
            changed = [
                offset
                for offset, (before, after) in enumerate(zip(input_page, output_page))
                if before != after
            ]
            if not changed:
                raise Ia32BackendError(
                    f"Phase-3 worker published a false dirty bit for 0x{page_address:08X}"
                )
            dirty_publications.append(
                {
                    "mapped_address": page_address,
                    "logical_addresses": list(logical_addresses),
                    "owner": owner,
                    "start_offset": changed[0],
                    "end_offset": changed[-1] + 1,
                    "native_dirty": True,
                }
            )
        elif phase3 and output_page != input_page:
            raise Ia32BackendError(f"Phase-3 worker omitted a dirty bit for 0x{page_address:08X}")
    if phase3:
        _apply_phase3_timestamp_accounting(state, artifact.manifest)
    host_service_calls: list[dict[str, Any]] = []
    if artifact.manifest.get("host_abi") is True:
        _broker_offset, trace_offset = _phase4_service_offsets(len(expected_pages))
        trace_count, trace_overflow, trace_capacity, trace_version = struct.unpack_from(
            "<4I", payload, trace_offset
        )
        if (
            trace_overflow
            or trace_capacity != HOST_SERVICE_TRACE_CAPACITY
            or trace_version != 1
            or trace_count > HOST_SERVICE_TRACE_CAPACITY
        ):
            raise Ia32BackendError("IA-32 Phase-4 service trace is invalid or overflowed")
        service_records = artifact.manifest.get("decoded_preflight", {}).get(
            "host_service_thunks", []
        )
        names = {
            int(record["target"]): str(record["shim_name"])
            for record in service_records
            if isinstance(record, dict)
        }
        boundary_names = {value: name for name, value in HOST_SERVICE_BOUNDARIES.items()}
        for index in range(trace_count):
            offset = (
                trace_offset
                + HOST_SERVICE_TRACE_HEADER_SIZE
                + index * HOST_SERVICE_TRACE_RECORD_SIZE
            )
            words = struct.unpack_from(
                f"<{HOST_SERVICE_TRACE_RECORD_WORDS}I",
                payload,
                offset,
            )
            argument_count = words[6]
            if argument_count > 4:
                raise Ia32BackendError("IA-32 Phase-4 service trace has too many arguments")
            host_service_calls.append(
                {
                    "sequence": words[0],
                    "target": words[1],
                    "shim_name": names.get(words[1], f"0x{words[1]:08X}"),
                    "kind": words[2],
                    "boundary": boundary_names.get(words[3], f"unknown-{words[3]}"),
                    "execution": (
                        "native32"
                        if words[4] == HOST_SERVICE_EXECUTION_NATIVE32
                        else "native64-broker"
                        if words[4] == HOST_SERVICE_EXECUTION_NATIVE64_BROKER
                        else f"unknown-{words[4]}"
                    ),
                    "calling_convention": (
                        "stdcall"
                        if words[5] == HOST_SERVICE_CALL_STDCALL
                        else "cdecl"
                        if words[5] == HOST_SERVICE_CALL_CDECL
                        else f"unknown-{words[5]}"
                    ),
                    "argument_count": argument_count,
                    "stack_cleanup_bytes": words[7],
                    "entry_esp": words[8],
                    "return_esp": words[9],
                    "arguments": list(words[10 : 10 + argument_count]),
                    "return_value": words[15],
                    "memory_address": words[16],
                    "memory_before": words[17],
                    "memory_after": words[18],
                    "callback_target": words[19],
                    "callback_return": words[20],
                    "callback_count": words[21],
                    "runtime_kind": words[22] >> 16,
                    "runtime_value": words[22] & 0xFFFF,
                    "error": words[23],
                }
            )
    resident_scheduler: dict[str, Any] | None = None
    if artifact.manifest.get("resident_scheduler") is True:
        scheduler_offset, lanes_offset, traces_offset = _phase5_scheduler_offsets(
            len(expected_pages)
        )
        header = struct.unpack_from("<16I", payload, scheduler_offset)
        if (
            header[0] != RESIDENT_SCHEDULER_MAGIC
            or header[1] != RESIDENT_SCHEDULER_VERSION
            or header[2] != RESIDENT_SCHEDULER_LANE_COUNT
            or header[3] != int(artifact.manifest["resident_scheduler_step_count"])
            or header[4] != header[3]
            or header[5] != RESIDENT_SCHEDULER_TRACE_CAPACITY
            or header[8] != 0
            or header[9] != 0
        ):
            raise Ia32BackendError(
                "IA-32 Phase-5 scheduler returned an invalid, stranded, or unclassified schedule"
            )
        lane_state_names = {
            RESIDENT_LANE_READY: "ready",
            RESIDENT_LANE_RUNNING: "running",
            RESIDENT_LANE_WAITING: "waiting",
            RESIDENT_LANE_COMPLETED: "completed",
            RESIDENT_LANE_FAULTED: "faulted",
        }
        exit_names = {
            **{value: name for name, value in RESIDENT_EXIT_KINDS.items()},
            RESIDENT_EXIT_FAULT: "fault",
        }
        lanes: list[dict[str, Any]] = []
        for lane_id, name in enumerate(RESIDENT_SCHEDULER_LANES):
            offset = lanes_offset + lane_id * RESIDENT_SCHEDULER_LANE_RECORD_SIZE
            words = struct.unpack_from("<20I", payload, offset)
            if words[0] != lane_id or words[1] not in lane_state_names:
                raise Ia32BackendError("IA-32 Phase-5 lane record is malformed")
            lanes.append(
                {
                    "id": lane_id,
                    "name": name,
                    "state": lane_state_names[words[1]],
                    "tls_base": words[2],
                    "activation_count": words[3],
                    "eip": words[4],
                    "eflags": words[5],
                    "registers": {
                        register_name: words[6 + index]
                        for index, register_name in enumerate(REGISTER_NAMES)
                    },
                    "last_exit": exit_names.get(words[14], f"unknown-{words[14]}"),
                    "fault_code": words[15],
                    "wait_object": words[16],
                    "fault_eip": words[17],
                    "fault_address": words[18],
                }
            )
        traces: list[dict[str, Any]] = []
        for index in range(header[4]):
            words = struct.unpack_from(
                f"<{RESIDENT_SCHEDULER_TRACE_RECORD_WORDS}I",
                payload,
                traces_offset + index * RESIDENT_SCHEDULER_TRACE_RECORD_SIZE,
            )
            if words[0] != index or words[1] >= RESIDENT_SCHEDULER_LANE_COUNT:
                raise Ia32BackendError("IA-32 Phase-5 scheduler trace is malformed")
            traces.append(
                {
                    "sequence": words[0],
                    "lane": RESIDENT_SCHEDULER_LANES[words[1]],
                    "entry_eip": words[2],
                    "next_eip": words[3],
                    "exit": exit_names.get(words[4], f"unknown-{words[4]}"),
                    "wake_lane": (
                        None
                        if words[5] == 0xFFFFFFFF
                        else RESIDENT_SCHEDULER_LANES[words[5]]
                        if words[5] < RESIDENT_SCHEDULER_LANE_COUNT
                        else f"unknown-{words[5]}"
                    ),
                    "state_before": lane_state_names.get(words[6], f"unknown-{words[6]}"),
                    "state_after": lane_state_names.get(words[7], f"unknown-{words[7]}"),
                    "service_begin": words[8],
                    "service_end": words[9],
                    "tls_base": words[10],
                    "tls_before": words[11],
                    "tls_after": words[12],
                    "render_hash": words[13],
                    "audio_hash": words[14],
                    "fault_code": words[15],
                }
            )
        resident_scheduler = {
            "backend": "same-isa-ia32-resident",
            "ordering": "primary-safe-point-then-vblank-then-runnable-worker",
            "lane_count": header[2],
            "planned_steps": header[3],
            "trace_count": header[4],
            "trace_capacity": header[5],
            "worker_wakeups": header[6],
            "completed_flips": header[7],
            "stranded_contexts": header[8],
            "unclassified_exits": header[9],
            "contained_faults": header[10],
            "service_calls": header[11],
            "render_hash": header[12],
            "audio_hash": header[13],
            "lane_switches": header[14],
            "lanes": lanes,
            "trace": traces,
        }
    elapsed_ns = max(1, (ticks * 1_000_000_000) // frequency)
    memory_transport: dict[str, Any] | None = None
    if dirty_only:
        memory_words = struct.unpack_from(
            f"<{CUTOVER_MEMORY_HEADER_WORDS}I",
            payload,
            _phase7_memory_offset(len(expected_pages)),
        )
        if (
            memory_words[0] != CUTOVER_MEMORY_MAGIC
            or memory_words[1] != CUTOVER_MEMORY_VERSION
            or memory_words[2] != len(expected_pages)
            or memory_words[11] != 0
        ):
            raise Ia32BackendError("IA-32 Phase-7 memory-ownership telemetry is invalid")
        full_page_bytes = len(expected_pages) * PAGE_SIZE
        memory_transport = {
            "owner": "resident-native32-worker",
            "initial_page_seed_count": memory_words[2],
            "initial_page_seed_bytes": memory_words[3],
            "command_count": memory_words[4],
            "host_publication_page_count": memory_words[5],
            "host_publication_bytes": memory_words[6],
            "native_publication_page_count": memory_words[7],
            "native_publication_bytes": memory_words[8],
            "input_pages_bypassed": memory_words[9],
            "output_pages_bypassed": memory_words[10],
            "cross_backend_exits": memory_words[11],
            "memory_epoch": memory_words[12],
            "publication_epoch": memory_words[13],
            "legacy_full_roundtrip_bytes": full_page_bytes * 2,
            "page_payload_bytes_transferred": memory_words[6] + memory_words[8],
        }
    if snapshot_memory and resident_memory is not None:
        result_memory = SparseMemory.from_pages(resident_memory.export_pages())
    toolchain = artifact.manifest.get("toolchain")
    if not isinstance(toolchain, dict):
        inputs = artifact.manifest.get("inputs")
        if not isinstance(inputs, dict):
            raise Ia32BackendError("IA-32 artifact omits its toolchain inputs")
        toolchain = _toolchain_identity(inputs)
    return Ia32SliceResult(
        state=state,
        memory=result_memory,
        elapsed_ns=elapsed_ns,
        process_elapsed_ns=process_elapsed_ns,
        artifact_id=artifact.artifact_id,
        capsule_id=capsule.capsule_id,
        execution_contract_id=str(
            artifact.manifest.get("execution_contract_id", "legacy-unversioned")
        ),
        toolchain_id=str(toolchain["toolchain_id"]),
        stop_eip=int(artifact.manifest["inputs"]["stop_eip"]),
        resident_worker=resident_worker,
        worker_process_id=worker_process_id,
        worker_dispatch_index=worker_dispatch_index,
        worker_startup_ns=worker_startup_ns,
        resident_dispatch_ns=resident_dispatch_ns,
        dirty_publications=tuple(dirty_publications),
        host_service_calls=tuple(host_service_calls),
        resident_scheduler=resident_scheduler,
        memory_transport=memory_transport,
    )


@dataclass(frozen=True)
class _WindowsProcess:
    handle: int
    process_id: int


def _launch_windows_helper(executable: Path, mapping_name: str) -> _WindowsProcess:
    """Create the helper with low-address ASLR disabled for exact guest mappings."""

    import ctypes
    from ctypes import wintypes

    class StartupInfoW(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("lpReserved", wintypes.LPWSTR),
            ("lpDesktop", wintypes.LPWSTR),
            ("lpTitle", wintypes.LPWSTR),
            ("dwX", wintypes.DWORD),
            ("dwY", wintypes.DWORD),
            ("dwXSize", wintypes.DWORD),
            ("dwYSize", wintypes.DWORD),
            ("dwXCountChars", wintypes.DWORD),
            ("dwYCountChars", wintypes.DWORD),
            ("dwFillAttribute", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("wShowWindow", wintypes.WORD),
            ("cbReserved2", wintypes.WORD),
            ("lpReserved2", ctypes.POINTER(ctypes.c_ubyte)),
            ("hStdInput", wintypes.HANDLE),
            ("hStdOutput", wintypes.HANDLE),
            ("hStdError", wintypes.HANDLE),
        ]

    class StartupInfoExW(ctypes.Structure):
        _fields_ = [("StartupInfo", StartupInfoW), ("lpAttributeList", ctypes.c_void_p)]

    class ProcessInformation(ctypes.Structure):
        _fields_ = [
            ("hProcess", wintypes.HANDLE),
            ("hThread", wintypes.HANDLE),
            ("dwProcessId", wintypes.DWORD),
            ("dwThreadId", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    initialize = kernel32.InitializeProcThreadAttributeList
    initialize.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    initialize.restype = wintypes.BOOL
    update = kernel32.UpdateProcThreadAttribute
    update.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_size_t,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    update.restype = wintypes.BOOL
    delete = kernel32.DeleteProcThreadAttributeList
    delete.argtypes = [ctypes.c_void_p]
    create = kernel32.CreateProcessW
    create.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.BOOL,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.LPCWSTR,
        ctypes.POINTER(StartupInfoExW),
        ctypes.POINTER(ProcessInformation),
    ]
    create.restype = wintypes.BOOL

    size = ctypes.c_size_t()
    initialize(None, 1, 0, ctypes.byref(size))
    attribute_storage = ctypes.create_string_buffer(size.value)
    attribute_list = ctypes.cast(attribute_storage, ctypes.c_void_p)
    if not initialize(attribute_list, 1, 0, ctypes.byref(size)):
        raise Ia32BackendError(
            f"could not initialize IA-32 launch attributes: {ctypes.get_last_error()}"
        )
    try:
        attribute_mitigation_policy = 0x00020007
        bottom_up_aslr_off_and_force_relocate_off = ctypes.c_ulonglong(0x00020200)
        if not update(
            attribute_list,
            0,
            attribute_mitigation_policy,
            ctypes.byref(bottom_up_aslr_off_and_force_relocate_off),
            ctypes.sizeof(bottom_up_aslr_off_and_force_relocate_off),
            None,
            None,
        ):
            raise Ia32BackendError(
                f"could not set IA-32 low-address policy: {ctypes.get_last_error()}"
            )
        startup = StartupInfoExW()
        startup.StartupInfo.cb = ctypes.sizeof(startup)
        startup.lpAttributeList = attribute_list
        process = ProcessInformation()
        command_line = ctypes.create_unicode_buffer(f'"{executable}" {mapping_name}')
        extended_startup_info_present = 0x00080000
        create_no_window = 0x08000000
        if not create(
            str(executable),
            command_line,
            None,
            None,
            False,
            extended_startup_info_present | create_no_window,
            None,
            str(executable.parent),
            ctypes.byref(startup),
            ctypes.byref(process),
        ):
            raise Ia32BackendError(f"could not launch IA-32 helper: {ctypes.get_last_error()}")
    finally:
        delete(attribute_list)
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle(process.hThread)
    return _WindowsProcess(int(process.hProcess), int(process.dwProcessId))


def _close_windows_handle(handle: int) -> None:
    import ctypes
    from ctypes import wintypes

    close_handle = ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    close_handle(handle)


def _terminate_windows_process(process: _WindowsProcess, exit_code: int) -> None:
    import ctypes
    from ctypes import wintypes

    terminate = ctypes.WinDLL("kernel32", use_last_error=True).TerminateProcess
    terminate.argtypes = [wintypes.HANDLE, wintypes.UINT]
    terminate.restype = wintypes.BOOL
    terminate(process.handle, exit_code)


def _wait_windows_process(process: _WindowsProcess, timeout_seconds: float) -> int | None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    wait_for_single_object = kernel32.WaitForSingleObject
    wait_for_single_object.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    wait_for_single_object.restype = wintypes.DWORD
    get_exit_code = kernel32.GetExitCodeProcess
    get_exit_code.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    wait_result = wait_for_single_object(process.handle, max(0, int(timeout_seconds * 1000)))
    if wait_result == 0x00000102:
        return None
    if wait_result != 0:
        raise Ia32BackendError(f"could not wait for IA-32 helper: 0x{wait_result:08X}")
    exit_code = wintypes.DWORD()
    if not get_exit_code(process.handle, ctypes.byref(exit_code)):
        raise Ia32BackendError(f"could not read IA-32 helper exit code: {ctypes.get_last_error()}")
    return int(exit_code.value)


def _run_windows_helper(executable: Path, mapping_name: str, timeout_seconds: float) -> int:
    process = _launch_windows_helper(executable, mapping_name)
    try:
        return_code = _wait_windows_process(process, timeout_seconds)
        if return_code is None:
            _terminate_windows_process(process, 0xEE)
            _wait_windows_process(process, 1.0)
            raise Ia32BackendError("IA-32 helper timed out")
        return return_code
    finally:
        _close_windows_handle(process.handle)


def _create_windows_event(name: str) -> int:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_event = kernel32.CreateEventW
    create_event.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
    create_event.restype = wintypes.HANDLE
    handle = create_event(None, False, False, name)
    if not handle:
        raise Ia32BackendError(f"could not create IA-32 worker event: {ctypes.get_last_error()}")
    return int(handle)


def _set_windows_event(handle: int) -> None:
    import ctypes
    from ctypes import wintypes

    set_event = ctypes.WinDLL("kernel32", use_last_error=True).SetEvent
    set_event.argtypes = [wintypes.HANDLE]
    set_event.restype = wintypes.BOOL
    if not set_event(handle):
        raise Ia32BackendError(f"could not signal IA-32 worker: {ctypes.get_last_error()}")


def _wait_worker_signal(
    response_event: int,
    process: _WindowsProcess,
    timeout_seconds: float,
) -> str:
    import ctypes
    from ctypes import wintypes

    wait = ctypes.WinDLL("kernel32", use_last_error=True).WaitForMultipleObjects
    wait.argtypes = [
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.BOOL,
        wintypes.DWORD,
    ]
    wait.restype = wintypes.DWORD
    handles = (wintypes.HANDLE * 2)(response_event, process.handle)
    result = int(wait(2, handles, False, max(0, int(timeout_seconds * 1000))))
    if result == 0:
        return "response"
    if result == 1:
        return "process"
    if result == 0x00000102:
        return "timeout"
    raise Ia32BackendError(f"could not wait for IA-32 worker: 0x{result:08X}")


def _validate_artifact_for_capsule(
    artifact: Ia32SliceArtifact,
    capsule: ReplayCapsule,
) -> None:
    validate_ia32_artifact_manifest(artifact.manifest)
    _validate_phase2_output_files(artifact.root, artifact.manifest)
    _validate_phase3_output_files(artifact.root, artifact.manifest)
    _validate_phase4_output_files(artifact.root, artifact.manifest)
    _validate_phase5_output_files(artifact.root, artifact.manifest)
    _validate_phase6_output_files(artifact.root, artifact.manifest)
    _validate_phase7_output_files(artifact.root, artifact.manifest)
    if (
        artifact.manifest.get("decoded_store_artifact") is True
        and artifact.manifest.get("normal_execution_eligible") is not True
    ):
        raise Ia32BackendError(
            "IA-32 decoded-store artifact has unsupported coverage and is not executable"
        )
    inputs = artifact.manifest.get("inputs", {})
    if not isinstance(inputs, dict) or inputs.get("capsule_id") != capsule.capsule_id:
        raise Ia32BackendError("IA-32 artifact was built for a different replay capsule")
    if artifact.manifest.get("executable_sha256") != sha256_file(artifact.executable):
        raise Ia32BackendError("IA-32 helper executable identity does not match its manifest")
    guest_image = artifact.root / str(artifact.manifest.get("guest_image", ""))
    if not guest_image.is_file() or artifact.manifest.get("guest_image_sha256") != sha256_file(
        guest_image
    ):
        raise Ia32BackendError("rebuilt IA-32 title PE identity does not match its manifest")


def _raise_exchange_failure(
    payload: bytes | bytearray | mmap.mmap,
    artifact: Ia32SliceArtifact,
    *,
    return_code: int | None,
    operation: str,
) -> None:
    if len(payload) < EXCHANGE_HEADER_SIZE:
        raise Ia32BackendError(
            f"{operation} returned a truncated exchange for artifact {artifact.artifact_id}"
        )
    _magic, version, status, error, _page_count, detail = struct.unpack_from("<6I", payload)
    information_0, information_1, saved_eip, saved_eflags = struct.unpack_from("<2Q2I", payload, 24)
    context = f"artifact {artifact.artifact_id}, protocol {version}, guest EIP 0x{detail:08X}"
    if status == WORKER_STATUS_FAULT:
        if error == 0xC0000005:
            access = {0: "read", 1: "write", 8: "execute"}.get(
                information_0,
                f"access type {information_0}",
            )
            message = (
                f"guest access violation at 0x{detail:08X}: {access} address "
                f"0x{information_1 & 0xFFFFFFFF:08X} ({context})"
            )
            if artifact.manifest.get("recoverable_guest_faults") is True:
                raise Ia32GuestFault(
                    message,
                    code=error,
                    guest_eip=detail,
                    access_address=information_1 & 0xFFFFFFFF,
                    access_kind=access,
                )
            raise Ia32BackendError(message)
        message = f"guest exception 0x{error:08X} at 0x{detail:08X} ({context})"
        if artifact.manifest.get("recoverable_guest_faults") is True:
            raise Ia32GuestFault(message, code=error, guest_eip=detail)
        raise Ia32BackendError(message)
    if status == WORKER_STATUS_PROTOCOL_ERROR:
        raise Ia32BackendError(
            f"IA-32 worker protocol mismatch/error {error}, detail 0x{detail:08X}, "
            f"diagnostic 0x{information_0:X}/0x{information_1:X} ({context})"
        )
    if return_code == 0xC0000005 and status == EXCHANGE_STATUS_RUNNING:
        raise Ia32BackendError(
            "IA-32 worker crashed with an access violation while executing guest code; "
            f"last published guest EIP 0x{saved_eip:08X}, artifact {artifact.artifact_id}"
        )
    exit_description = "still active" if return_code is None else str(return_code)
    raise Ia32BackendError(
        f"{operation} exited with {exit_description}; status {status}, error {error}, "
        f"detail 0x{detail:08X}, context 0x{information_0:X}/0x{information_1:X}, "
        f"saved EIP 0x{saved_eip:08X}, saved EFLAGS 0x{saved_eflags:08X}, "
        f"artifact {artifact.artifact_id}"
    )


class Ia32PersistentWorker:
    """Own one resident 32-bit process and dispatch fixed-slice capsules to it."""

    def __init__(
        self,
        *,
        artifact: Ia32SliceArtifact,
        capsule: ReplayCapsule,
        view: mmap.mmap,
        payload_size: int,
        process: _WindowsProcess,
        request_event: int,
        response_event: int,
        startup_ns: int,
        timeout_seconds: float,
        broker_process: _WindowsProcess | None = None,
        broker_request_event: int | None = None,
        broker_response_event: int | None = None,
    ) -> None:
        self.artifact = artifact
        self._capsule = capsule
        self._view = view
        self._payload_size = payload_size
        self._process = process
        self._request_event = request_event
        self._response_event = response_event
        self.startup_ns = startup_ns
        self.timeout_seconds = timeout_seconds
        self._broker_process = broker_process
        self._broker_request_event = broker_request_event
        self._broker_response_event = broker_response_event
        self._resident_memory = SparseMemory.from_pages(capsule.memory.export_pages())
        self.dispatch_count = 0
        self.closed = False

    @property
    def process_id(self) -> int:
        return self._process.process_id

    @property
    def broker_process_id(self) -> int | None:
        return self._broker_process.process_id if self._broker_process is not None else None

    @classmethod
    def start(
        cls,
        artifact: Ia32SliceArtifact,
        capsule: ReplayCapsule,
        *,
        timeout_seconds: float = 10.0,
    ) -> Ia32PersistentWorker:
        if sys.platform != "win32":
            raise Ia32BackendError("the same-ISA IA-32 worker currently requires Windows")
        _validate_artifact_for_capsule(artifact, capsule)
        if artifact.manifest.get("persistent_worker") is not True:
            raise Ia32BackendError("IA-32 artifact does not implement the Phase-1 worker contract")
        exchange_version = _artifact_exchange_version(artifact.manifest)
        exchange_payload = _exchange_payload(
            capsule,
            artifact,
            exchange_version=exchange_version,
            protocol_version=WORKER_PROTOCOL_VERSION,
            worker_command=WORKER_COMMAND_ENTER,
        )
        mapping_name = f"Local\\b2r-ia32-worker-{uuid.uuid4().hex}"
        request_event = _create_windows_event(mapping_name + "-request")
        response_event = _create_windows_event(mapping_name + "-response")
        broker_request_event = (
            _create_windows_event(mapping_name + "-broker-request")
            if artifact.manifest.get("host_abi") is True
            else None
        )
        broker_response_event = (
            _create_windows_event(mapping_name + "-broker-response")
            if artifact.manifest.get("host_abi") is True
            else None
        )
        view: mmap.mmap | None = None
        process: _WindowsProcess | None = None
        broker_process: _WindowsProcess | None = None
        try:
            view = mmap.mmap(
                -1,
                len(exchange_payload),
                tagname=mapping_name,
                access=mmap.ACCESS_WRITE,
            )
            view.write(exchange_payload)
            view.seek(0)
            started_ns = time.perf_counter_ns()
            if artifact.manifest.get("host_abi") is True:
                broker_record = artifact.manifest.get("service_broker")
                if not isinstance(broker_record, dict):
                    raise Ia32BackendError("Phase-4 artifact omits its native service broker")
                broker_executable = artifact.root / str(broker_record.get("executable", ""))
                if broker_response_event is None:
                    raise Ia32BackendError("Phase-4 broker response event was not created")
                broker_process = _launch_windows_helper(broker_executable, mapping_name)
                broker_outcome = _wait_worker_signal(
                    broker_response_event,
                    broker_process,
                    timeout_seconds,
                )
                if broker_outcome != "response":
                    if broker_outcome == "timeout":
                        _terminate_windows_process(broker_process, 0xEE)
                        _wait_windows_process(broker_process, 1.0)
                    raise Ia32BackendError(
                        f"IA-32 native service broker failed to become ready for artifact "
                        f"{artifact.artifact_id}"
                    )
                broker_offset, _trace_offset = _phase4_service_offsets(
                    len(artifact.manifest["page_addresses"])
                )
                view.seek(broker_offset + 8)
                if struct.unpack("<I", view.read(4))[0] != HOST_SERVICE_BROKER_STATUS_READY:
                    raise Ia32BackendError(
                        "IA-32 native service broker published invalid ready state"
                    )
                view.seek(0)
            process = _launch_windows_helper(artifact.executable, mapping_name)
            outcome = _wait_worker_signal(response_event, process, timeout_seconds)
            startup_ns = time.perf_counter_ns() - started_ns
            view.seek(0)
            output: bytes | mmap.mmap = (
                view
                if artifact.manifest.get("normal_launcher_cutover") is True
                else view.read(len(exchange_payload))
            )
            if outcome != "response":
                if outcome == "timeout":
                    _terminate_windows_process(process, 0xEE)
                    _wait_windows_process(process, 1.0)
                    raise Ia32BackendError(
                        f"IA-32 worker startup timed out at guest EIP "
                        f"0x{capsule.state.eip:08X}, artifact {artifact.artifact_id}"
                    )
                return_code = _wait_windows_process(process, 0.0)
                _raise_exchange_failure(
                    output,
                    artifact,
                    return_code=return_code,
                    operation="IA-32 worker startup",
                )
            magic, version, status, error, page_count, protocol = struct.unpack_from("<6I", output)
            expected_pages = len(artifact.manifest["page_addresses"])
            if (
                magic != EXCHANGE_MAGIC
                or version != exchange_version
                or status != WORKER_STATUS_READY
                or error != 0
                or page_count != expected_pages
                or protocol != WORKER_PROTOCOL_VERSION
            ):
                _raise_exchange_failure(
                    output,
                    artifact,
                    return_code=None,
                    operation="IA-32 worker startup",
                )
            return cls(
                artifact=artifact,
                capsule=capsule,
                view=view,
                payload_size=len(exchange_payload),
                process=process,
                request_event=request_event,
                response_event=response_event,
                startup_ns=startup_ns,
                timeout_seconds=timeout_seconds,
                broker_process=broker_process,
                broker_request_event=broker_request_event,
                broker_response_event=broker_response_event,
            )
        except BaseException:
            if process is not None:
                if _wait_windows_process(process, 0.0) is None:
                    _terminate_windows_process(process, 0xEF)
                    _wait_windows_process(process, 1.0)
                _close_windows_handle(process.handle)
            if broker_process is not None:
                if _wait_windows_process(broker_process, 0.0) is None:
                    _terminate_windows_process(broker_process, 0xEF)
                    _wait_windows_process(broker_process, 1.0)
                _close_windows_handle(broker_process.handle)
            if view is not None:
                view.close()
            _close_windows_handle(response_event)
            _close_windows_handle(request_event)
            if broker_response_event is not None:
                _close_windows_handle(broker_response_event)
            if broker_request_event is not None:
                _close_windows_handle(broker_request_event)
            raise

    def _read_exchange(self) -> bytes:
        self._view.seek(0)
        return self._view.read(self._payload_size)

    def dispatch(
        self,
        capsule: ReplayCapsule,
        *,
        command: int = WORKER_COMMAND_ENTER,
        timeout_seconds: float | None = None,
        republish_pages: Sequence[int] = (),
        snapshot_memory: bool = False,
    ) -> Ia32SliceResult:
        if self.closed:
            raise Ia32BackendError("IA-32 worker is closed")
        _validate_artifact_for_capsule(self.artifact, capsule)
        if command not in {
            WORKER_COMMAND_ENTER,
            WORKER_COMMAND_RESUME,
            WORKER_COMMAND_SERVICE_RETURN,
            WORKER_COMMAND_SCHEDULE,
        }:
            raise Ia32BackendError(f"unsupported IA-32 worker command {command}")
        exchange_version = _artifact_exchange_version(self.artifact.manifest)
        cutover = self.artifact.manifest.get("normal_launcher_cutover") is True
        command_control_bytes = 0
        if cutover:
            header, tail_offset, tail = _cutover_command_segments(
                capsule,
                self.artifact,
                worker_command=command,
            )
            pages = tuple(int(address) for address in self.artifact.manifest["page_addresses"])
            page_indices = {address: index for index, address in enumerate(pages)}
            logical_pages: dict[int, int] = {}
            slot_logical_pages: dict[int, tuple[int, ...]] = {}
            for record in self.artifact.manifest.get("phase3_page_bindings", []):
                if not isinstance(record, Mapping):
                    continue
                mapped = int(record["mapped_address"])
                index = page_indices.get(mapped)
                if index is None:
                    continue
                logical_addresses = tuple(int(value) for value in record["logical_addresses"])
                slot_logical_pages[index] = logical_addresses
                for logical in logical_addresses:
                    logical_pages[int(logical)] = index
            states = bytearray(len(pages))
            publications: dict[int, bytes] = {}
            for raw_address in republish_pages:
                address = _u32(int(raw_address))
                if address & (PAGE_SIZE - 1):
                    raise Ia32BackendError(
                        f"Phase-7 host publication page 0x{address:08X} is not aligned"
                    )
                index = page_indices.get(address, logical_pages.get(address))
                if index is None:
                    raise Ia32BackendError(
                        f"Phase-7 host publication page 0x{address:08X} is not in the artifact"
                    )
                source_address = (
                    slot_logical_pages.get(index, (address,))[0]
                    if address in page_indices
                    else address
                )
                page = capsule.memory.read(source_address, PAGE_SIZE)
                previous = publications.get(index)
                if previous is not None and previous != page:
                    raise Ia32BackendError(
                        f"Phase-7 aliases for page slot {index} have conflicting publications"
                    )
                publications[index] = page
            self._view.seek(0)
            self._view.write(header)
            for index, page in publications.items():
                self._view.seek(EXCHANGE_HEADER_SIZE + index * PAGE_SIZE)
                self._view.write(page)
                states[index] = CUTOVER_PAGE_HOST_PUBLICATION
                for logical_address in slot_logical_pages.get(index, (pages[index],)):
                    self._resident_memory.write(logical_address, page)
            state_offset = EXCHANGE_HEADER_SIZE + len(pages) * PAGE_SIZE
            self._view.seek(state_offset)
            self._view.write(states)
            self._view.seek(tail_offset)
            self._view.write(tail)
            self._view.seek(0)
            command_control_bytes = len(header) + len(states) + len(tail)
            command_control_bytes += len(publications) * PAGE_SIZE
        else:
            if republish_pages:
                raise Ia32BackendError(
                    "explicit host page publication requires a Phase-7 cutover artifact"
                )
            exchange_payload = _exchange_payload(
                capsule,
                self.artifact,
                exchange_version=exchange_version,
                protocol_version=WORKER_PROTOCOL_VERSION,
                worker_command=command,
            )
            self._view.seek(0)
            self._view.write(exchange_payload)
            self._view.seek(0)
        started_ns = time.perf_counter_ns()
        _set_windows_event(self._request_event)
        wait_timeout = self.timeout_seconds if timeout_seconds is None else timeout_seconds
        outcome = _wait_worker_signal(self._response_event, self._process, wait_timeout)
        resident_dispatch_ns = time.perf_counter_ns() - started_ns
        output: bytes | mmap.mmap = self._view if cutover else self._read_exchange()
        if outcome == "timeout":
            _terminate_windows_process(self._process, 0xEE)
            _wait_windows_process(self._process, 1.0)
            raise Ia32BackendError(
                f"IA-32 worker dispatch timed out at guest EIP 0x{capsule.state.eip:08X}, "
                f"artifact {self.artifact.artifact_id}"
            )
        if outcome == "process":
            _raise_exchange_failure(
                output,
                self.artifact,
                return_code=_wait_windows_process(self._process, 0.0),
                operation="IA-32 worker dispatch",
            )
        status = struct.unpack_from("<I", output, 8)[0]
        if status != EXCHANGE_STATUS_COMPLETE:
            _raise_exchange_failure(
                output,
                self.artifact,
                return_code=None,
                operation="IA-32 worker dispatch",
            )
        self.dispatch_count += 1
        result = _result_from_exchange(
            capsule,
            self.artifact,
            output,
            process_elapsed_ns=resident_dispatch_ns,
            expected_exchange_version=exchange_version,
            resident_worker=True,
            worker_process_id=self.process_id,
            worker_dispatch_index=self.dispatch_count,
            worker_startup_ns=self.startup_ns,
            resident_dispatch_ns=resident_dispatch_ns,
            resident_memory=self._resident_memory if cutover else None,
            snapshot_memory=snapshot_memory,
        )
        if cutover and result.memory_transport is not None:
            result = replace(
                result,
                memory_transport={
                    **result.memory_transport,
                    "command_control_bytes_transferred": command_control_bytes,
                    "total_command_bytes_transferred": (
                        command_control_bytes
                        + int(result.memory_transport["native_publication_bytes"])
                    ),
                },
            )
        return result

    def close(self) -> None:
        if self.closed:
            return
        try:
            current_status = struct.unpack_from("<I", self._read_exchange(), 8)[0]
            worker_failed = current_status == WORKER_STATUS_PROTOCOL_ERROR or (
                current_status == WORKER_STATUS_FAULT
                and self.artifact.manifest.get("recoverable_guest_faults") is not True
            )
            if worker_failed:
                _wait_windows_process(self._process, self.timeout_seconds)
            elif _wait_windows_process(self._process, 0.0) is None:
                cutover = self.artifact.manifest.get("normal_launcher_cutover") is True
                if cutover:
                    header, tail_offset, tail = _cutover_command_segments(
                        self._capsule,
                        self.artifact,
                        worker_command=WORKER_COMMAND_STOP,
                    )
                    page_count = len(self.artifact.manifest["page_addresses"])
                    self._view.seek(0)
                    self._view.write(header)
                    self._view.seek(EXCHANGE_HEADER_SIZE + page_count * PAGE_SIZE)
                    self._view.write(bytes(page_count))
                    self._view.seek(tail_offset)
                    self._view.write(tail)
                    self._view.seek(0)
                else:
                    stop_payload = _exchange_payload(
                        self._capsule,
                        self.artifact,
                        exchange_version=_artifact_exchange_version(self.artifact.manifest),
                        protocol_version=WORKER_PROTOCOL_VERSION,
                        worker_command=WORKER_COMMAND_STOP,
                    )
                    self._view.seek(0)
                    self._view.write(stop_payload)
                    self._view.seek(0)
                _set_windows_event(self._request_event)
                outcome = _wait_worker_signal(
                    self._response_event,
                    self._process,
                    self.timeout_seconds,
                )
                output = self._view if cutover else self._read_exchange()
                if outcome == "timeout":
                    _terminate_windows_process(self._process, 0xEE)
                    _wait_windows_process(self._process, 1.0)
                    raise Ia32BackendError(
                        f"IA-32 worker stop timed out at guest EIP "
                        f"0x{self._capsule.state.eip:08X}, artifact {self.artifact.artifact_id}"
                    )
                if outcome == "response":
                    _magic, _version, status, error, _pages, command = struct.unpack_from(
                        "<6I", output
                    )
                    if (
                        status != WORKER_STATUS_STOPPED
                        or error != 0
                        or command != WORKER_COMMAND_STOP
                    ):
                        _raise_exchange_failure(
                            output,
                            self.artifact,
                            return_code=None,
                            operation="IA-32 worker stop",
                        )
                return_code = _wait_windows_process(self._process, self.timeout_seconds)
                if return_code is None:
                    _terminate_windows_process(self._process, 0xEE)
                    _wait_windows_process(self._process, 1.0)
                    raise Ia32BackendError(
                        f"IA-32 worker did not exit after stop at guest EIP "
                        f"0x{self._capsule.state.eip:08X}, artifact {self.artifact.artifact_id}"
                    )
                if return_code != 0:
                    _raise_exchange_failure(
                        output,
                        self.artifact,
                        return_code=return_code,
                        operation="IA-32 worker stop",
                    )
        finally:
            self.closed = True
            if self._broker_process is not None:
                broker_return = _wait_windows_process(
                    self._broker_process,
                    self.timeout_seconds,
                )
                if broker_return is None:
                    _terminate_windows_process(self._broker_process, 0xEE)
                    _wait_windows_process(self._broker_process, 1.0)
                _close_windows_handle(self._broker_process.handle)
            self._view.close()
            _close_windows_handle(self._response_event)
            _close_windows_handle(self._request_event)
            if self._broker_response_event is not None:
                _close_windows_handle(self._broker_response_event)
            if self._broker_request_event is not None:
                _close_windows_handle(self._broker_request_event)
            _close_windows_handle(self._process.handle)

    def __enter__(self) -> Ia32PersistentWorker:
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.close()


def run_ia32_slice_artifact(
    artifact: Ia32SliceArtifact,
    capsule: ReplayCapsule,
    *,
    timeout_seconds: float = 10.0,
) -> Ia32SliceResult:
    _validate_artifact_for_capsule(artifact, capsule)
    if artifact.manifest.get("persistent_worker") is True:
        with Ia32PersistentWorker.start(
            artifact,
            capsule,
            timeout_seconds=timeout_seconds,
        ) as worker:
            command = (
                WORKER_COMMAND_SCHEDULE
                if artifact.manifest.get("resident_scheduler") is True
                else WORKER_COMMAND_ENTER
            )
            return worker.dispatch(
                capsule,
                command=command,
                timeout_seconds=timeout_seconds,
            )
    exchange_payload = _exchange_payload(capsule, artifact)
    mapping_name = f"Local\\b2r-ia32-{uuid.uuid4().hex}"
    if sys.platform != "win32":
        raise Ia32BackendError("the same-ISA IA-32 prototype currently requires Windows")
    with mmap.mmap(
        -1, len(exchange_payload), tagname=mapping_name, access=mmap.ACCESS_WRITE
    ) as view:
        view.write(exchange_payload)
        view.seek(0)
        started_ns = time.perf_counter_ns()
        return_code = _run_windows_helper(artifact.executable, mapping_name, timeout_seconds)
        process_elapsed_ns = time.perf_counter_ns() - started_ns
        view.seek(0)
        output = view.read(len(exchange_payload))
    if return_code != 0:
        _raise_exchange_failure(
            output,
            artifact,
            return_code=return_code,
            operation="IA-32 helper",
        )
    return _result_from_exchange(
        capsule,
        artifact,
        output,
        process_elapsed_ns=process_elapsed_ns,
    )


def select_ia32_launcher_backend(
    artifact: Ia32SliceArtifact | None,
    *,
    requested: str = "auto",
) -> str:
    """Select Phase 7's fail-closed normal backend or the explicit diagnostic oracle."""

    if requested == "diagnostic-oracle":
        return "fusion-only-64-bit-diagnostic"
    if requested not in {"auto", "same-isa-ia32"}:
        raise Ia32BackendError(f"unsupported IA-32 launcher backend selection: {requested}")
    if artifact is None:
        raise Ia32BackendError(
            "Phase-7 normal launch requires a verified same-ISA IA-32 cutover artifact"
        )
    validate_ia32_artifact_manifest(artifact.manifest)
    if (
        artifact.manifest.get("normal_launcher_cutover") is not True
        or artifact.manifest.get("normal_execution_eligible") is not True
        or artifact.manifest.get("coverage_closed") is not True
        or artifact.manifest.get("cross_backend_exits") != 0
    ):
        raise Ia32BackendError(
            "Phase-7 normal launch rejected an incomplete or fallback-enabled IA-32 artifact"
        )
    return "same-isa-ia32"


def validate_ia32_normal_live_launch_artifact(
    artifact: Ia32SliceArtifact,
) -> dict[str, Any]:
    """Reject fixed-capsule artifacts at the ordinary boot-to-gameplay seam."""

    backend = select_ia32_launcher_backend(artifact)
    contract = ia32_normal_live_launch_contract()
    contract_id = ia32_normal_live_launch_contract_id()
    inputs = artifact.manifest.get("inputs")
    if not isinstance(inputs, Mapping):
        raise Ia32BackendError("IA-32 normal-live artifact omits its inputs")
    if (
        inputs.get("normal_live_launch_contract_id") != contract_id
        or artifact.manifest.get("normal_live_launch") is not True
        or artifact.manifest.get("normal_live_launch_contract_id") != contract_id
        or artifact.manifest.get("normal_live_launch_contract") != contract
    ):
        raise Ia32BackendError(
            "ordinary live launch requires a boot-entry, dynamic-scheduler IA-32 "
            "artifact; fixed replay-plan cutover artifacts are diagnostic-only"
        )
    pending_services = int(artifact.manifest.get("normal_live_pending_host_service_count", 0))
    if pending_services:
        raise Ia32BackendError(
            "ordinary live launch requires native bodies for every profiled host "
            f"service; {pending_services} measured service bodies remain pending"
        )
    if artifact.manifest.get("executable_sha256") != sha256_file(artifact.executable):
        raise Ia32BackendError("IA-32 normal-live executable identity does not match its manifest")
    return {
        "status": "valid",
        "backend": backend,
        "artifact_id": artifact.artifact_id,
        "artifact_manifest": str(artifact.manifest_path.resolve()),
        "executable": str(artifact.executable.resolve()),
        "executable_sha256": artifact.manifest["executable_sha256"],
        "contract_id": contract_id,
        "command_line_protocol_version": NORMAL_LIVE_LAUNCH_PROTOCOL_VERSION,
        "workload": contract["workload"]["supported_path"],
        "normal_runtime_python_callbacks": 0,
    }


def run_ia32_launcher_cutover(
    artifact: Ia32SliceArtifact,
    capsule: ReplayCapsule,
    *,
    dispatch_count: int = 2,
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    """Run repeated warm Phase-7 commands in one verified resident worker."""

    if dispatch_count < 2:
        raise Ia32BackendError("Phase-7 validation requires at least two warm dispatches")
    backend = select_ia32_launcher_backend(artifact)
    command = (
        WORKER_COMMAND_SCHEDULE
        if artifact.manifest.get("resident_scheduler") is True
        else WORKER_COMMAND_ENTER
    )
    summaries = []
    with Ia32PersistentWorker.start(
        artifact,
        capsule,
        timeout_seconds=timeout_seconds,
    ) as worker:
        worker_process_id = worker.process_id
        worker_startup_ns = worker.startup_ns
        for _index in range(dispatch_count):
            summaries.append(
                worker.dispatch(
                    capsule,
                    command=command,
                    timeout_seconds=timeout_seconds,
                ).summary()
            )
    return {
        "format": "b2-recomp-ia32-launcher-cutover-run",
        "version": 1,
        "backend": backend,
        "diagnostic_oracle_backend": "fusion-only-64-bit",
        "artifact_id": artifact.artifact_id,
        "capsule_id": capsule.capsule_id,
        "worker_process_id": worker_process_id,
        "worker_startup_ns": worker_startup_ns,
        "dispatch_count": dispatch_count,
        "dispatches": summaries,
        "cross_backend_exits": 0,
        "frontier_interpreter_invocations": 0,
        "frontier_interpreter_steps": 0,
        "normal_runtime_python_callbacks": 0,
        "runtime_decoding": False,
        "runtime_compilation": False,
        "runtime_code_patching": False,
        "native_promotion": False,
        "raw_xbe_execution": False,
    }


def run_ia32_full_flip_cutover(
    artifact: Ia32SliceArtifact,
    capsule: ReplayCapsule,
    *,
    dispatch_count: int = 2,
    timeout_seconds: float = 120.0,
) -> dict[str, Any]:
    """Validate an observed full flip and repeated dirty-only resident dispatch."""

    if dispatch_count < 2:
        raise Ia32BackendError("Phase-7 full-flip validation requires at least two dispatches")
    stop_eip = int(artifact.manifest["inputs"]["stop_eip"])
    plan = _resident_scheduler_plan(capsule, stop_eip=stop_eip)
    if not any(step.exit_kind == RESIDENT_EXIT_FLIP for step in plan.steps):
        raise Ia32BackendError(
            "Phase-7 live validation requires a manually captured scheduler plan "
            "with at least one completed flip"
        )
    oracle, expected_state, expected_memory = _phase7_full_flip_oracle(
        capsule,
        stop_eip=stop_eip,
    )
    oracle_pages = _phase7_full_flip_oracle_page_addresses(capsule, oracle)
    observed_memory_ranges = _phase7_observed_memory_ranges(plan, expected_state)
    expected_service_trace = list(_phase7_replayable_service_trace(capsule, oracle))

    results: list[Ia32SliceResult] = []
    backend = select_ia32_launcher_backend(artifact)
    with Ia32PersistentWorker.start(
        artifact,
        capsule,
        timeout_seconds=timeout_seconds,
    ) as worker:
        worker_process_id = worker.process_id
        worker_startup_ns = worker.startup_ns
        for index in range(dispatch_count):
            results.append(
                worker.dispatch(
                    capsule,
                    command=WORKER_COMMAND_SCHEDULE,
                    timeout_seconds=timeout_seconds,
                    republish_pages=() if index == 0 else oracle_pages,
                    snapshot_memory=True,
                )
            )

    def state_id(state: CpuState) -> str:
        return _sha256(
            _canonical_json(_phase7_observed_state_record(state, fs_base=capsule.state.fs_base))
        )

    def memory_id(memory: SparseMemory) -> str:
        return _sha256(
            b"".join(memory.read(address, size) for _name, address, size in observed_memory_ranges)
        )

    expected_render_hash = _fnv1a32(expected_memory.read(plan.render_address, plan.render_size))
    expected_audio_hash = _fnv1a32(expected_memory.read(plan.audio_address, plan.audio_size))

    dispatch_checks: list[dict[str, Any]] = []
    failed: list[str] = []
    for index, result in enumerate(results, start=1):
        scheduler = result.resident_scheduler
        transport = result.memory_transport
        expected_host_publications = 0 if index == 1 else len(oracle_pages)
        native_service_trace = [
            (int(record["target"]), int(record["return_value"]))
            for record in result.host_service_calls
        ]
        checks = {
            "state_matches_observed_oracle": index != 1
            or state_id(expected_state) == state_id(result.state),
            "memory_matches_observed_oracle": index != 1
            or memory_id(expected_memory) == memory_id(result.memory),
            "service_trace_matches_observed_oracle": native_service_trace == expected_service_trace,
            "render_matches_observed_oracle": index != 1
            or scheduler is not None
            and scheduler["render_hash"] == expected_render_hash,
            "audio_matches_observed_oracle": index != 1
            or scheduler is not None
            and scheduler["audio_hash"] == expected_audio_hash,
            "completed_flip": scheduler is not None
            and scheduler["completed_flips"] == int(oracle["completed_flips"])
            and int(oracle["completed_flips"]) > 0,
            "no_scheduler_faults": scheduler is not None
            and scheduler["contained_faults"] == 0
            and scheduler["unclassified_exits"] == 0
            and scheduler["stranded_contexts"] == 0,
            "persistent_dirty_only_transport": transport is not None
            and transport["owner"] == "resident-native32-worker"
            and transport["host_publication_page_count"] == expected_host_publications
            and transport["input_pages_bypassed"]
            == len(artifact.manifest["page_addresses"]) - expected_host_publications
            and transport["cross_backend_exits"] == 0
            and transport["total_command_bytes_transferred"]
            < transport["legacy_full_roundtrip_bytes"],
        }
        failed.extend(f"dispatch {index}: {name}" for name, passed in checks.items() if not passed)
        dispatch_checks.append(
            {
                "dispatch_index": index,
                "checks": checks,
                "expected_completed_flips": int(oracle["completed_flips"]),
                "expected_render_hash": expected_render_hash,
                "expected_audio_hash": expected_audio_hash,
                "expected_host_publication_page_count": expected_host_publications,
                "observed_oracle_applied": index == 1,
            }
        )
    if failed:
        raise Ia32BackendError("Phase-7 full-flip validation failed: " + "; ".join(failed))

    return {
        "format": "b2-recomp-ia32-full-flip-cutover-run",
        "version": 1,
        "passed": True,
        "backend": backend,
        "diagnostic_oracle_backend": "fusion-only-64-bit",
        "artifact_id": artifact.artifact_id,
        "capsule_id": capsule.capsule_id,
        "worker_process_id": worker_process_id,
        "worker_startup_ns": worker_startup_ns,
        "dispatch_count": dispatch_count,
        "dispatches": [result.summary() for result in results],
        "observed_oracle": dict(oracle),
        "observed_oracle_validation_scope": {
            "memory": [
                {"name": name, "address": address, "size": size}
                for name, address, size in observed_memory_ranges
            ],
            "terminal_changed_page_count": len(oracle_pages),
            "service_trace_total_count": len(oracle.get("service_trace", [])),
            "service_trace_replayable_count": len(expected_service_trace),
            "state_normalization": {
                "reserved_x87_control_bits_removed": True,
                "x87_mmx_physical_alias_excluded": True,
                "x87_sticky_status_excluded": True,
                "mxcsr_sticky_status_excluded": True,
                "fs_base": "captured-primary-tls-overlay",
            },
        },
        "validation": dispatch_checks,
        "cross_backend_exits": 0,
        "frontier_interpreter_invocations": 0,
        "frontier_interpreter_steps": 0,
        "normal_runtime_python_callbacks": 0,
        "runtime_decoding": False,
        "runtime_compilation": False,
        "runtime_code_patching": False,
        "native_promotion": False,
        "raw_xbe_execution": False,
    }


def load_ia32_slice_artifact(path: Path) -> Ia32SliceArtifact:
    manifest_path = path / "manifest.json" if path.is_dir() else path
    try:
        manifest_value = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise Ia32BackendError(f"could not load IA-32 artifact manifest: {manifest_path}") from exc
    if not isinstance(manifest_value, dict):
        raise Ia32BackendError(f"invalid IA-32 artifact manifest: {manifest_path}")
    if manifest_value.get("format") == "b2-recomp-ia32-artifact-index":
        indexed_artifact_id = manifest_value.get("artifact_id")
        indexed_path = manifest_value.get("artifact_manifest")
        indexed_sha256 = manifest_value.get("artifact_manifest_sha256")
        if (
            not isinstance(indexed_artifact_id, str)
            or not isinstance(indexed_path, str)
            or not isinstance(indexed_sha256, str)
        ):
            raise Ia32BackendError(f"invalid IA-32 artifact index: {manifest_path}")
        manifest_path = (manifest_path.parent / indexed_path).resolve()
        if not manifest_path.is_file() or sha256_file(manifest_path) != indexed_sha256:
            raise Ia32BackendError(
                f"IA-32 indexed artifact manifest identity mismatch: {manifest_path}"
            )
        try:
            manifest_value = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise Ia32BackendError(
                f"could not load indexed IA-32 artifact manifest: {manifest_path}"
            ) from exc
        if (
            not isinstance(manifest_value, dict)
            or manifest_value.get("artifact_id") != indexed_artifact_id
        ):
            raise Ia32BackendError(f"invalid indexed IA-32 artifact: {manifest_path}")
    validate_ia32_artifact_manifest(manifest_value)
    executable = manifest_path.parent / str(manifest_value.get("executable", ""))
    if not executable.is_file():
        raise Ia32BackendError(f"IA-32 helper executable is missing: {executable}")
    artifact = Ia32SliceArtifact(
        manifest_path.parent,
        executable,
        manifest_path,
        manifest_value,
    )
    _validate_phase2_output_files(artifact.root, artifact.manifest)
    _validate_phase3_output_files(artifact.root, artifact.manifest)
    _validate_phase4_output_files(artifact.root, artifact.manifest)
    _validate_phase5_output_files(artifact.root, artifact.manifest)
    _validate_phase6_output_files(artifact.root, artifact.manifest)
    _validate_phase7_output_files(artifact.root, artifact.manifest)
    return artifact


def validate_ia32_decoded_store_artifact_sources(
    artifact: Ia32SliceArtifact,
    *,
    xbe_path: Path,
    decoded_block_store_path: Path,
) -> None:
    """Reject Phase-2 source, sidecar, or active-toolchain provenance drift."""

    validate_ia32_artifact_manifest(artifact.manifest)
    _validate_phase2_output_files(artifact.root, artifact.manifest)
    if artifact.manifest.get("decoded_store_artifact") is not True:
        raise Ia32BackendError("IA-32 artifact is not a Phase-2 decoded-store artifact")
    inputs = artifact.manifest.get("inputs")
    decoded_inputs = inputs.get("decoded_store_artifact") if isinstance(inputs, dict) else None
    if not isinstance(decoded_inputs, dict):
        raise Ia32BackendError("IA-32 artifact omits decoded-store source identities")
    plan = inspect_ia32_decoded_store(xbe_path, decoded_block_store_path)
    if decoded_inputs.get("xbe_sha256") != plan.xbe_sha256:
        raise Ia32BackendError("IA-32 artifact XBE content identity mismatch")
    if decoded_inputs.get("store_snapshot_id") != plan.store_snapshot_id:
        raise Ia32BackendError("IA-32 artifact decoded-store snapshot identity mismatch")
    compiler = _resolve_compiler(None)
    assembler = compiler.with_name("clang.exe")
    linker = _resolve_linker(compiler)
    kernel32 = _resolve_kernel32_library(None)
    active_paths = {
        "compiler": compiler,
        "assembler": assembler,
        "linker": linker,
        "kernel32_library": kernel32,
    }
    assert isinstance(inputs, dict)
    for name, active_path in active_paths.items():
        recorded = inputs.get(name)
        if not isinstance(recorded, dict) or recorded.get("sha256") != sha256_file(active_path):
            raise Ia32BackendError(f"IA-32 artifact active {name} identity mismatch")


def load_ia32_decoded_store_artifact(
    path: Path,
    *,
    xbe_path: Path,
    decoded_block_store_path: Path,
) -> Ia32SliceArtifact:
    artifact = load_ia32_slice_artifact(path)
    validate_ia32_decoded_store_artifact_sources(
        artifact,
        xbe_path=xbe_path,
        decoded_block_store_path=decoded_block_store_path,
    )
    return artifact


def _write_report(path: Path | None, report: Mapping[str, Any]) -> None:
    payload = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if path is None:
        print(payload, end="")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload, encoding="utf-8")
        print(path)


def _parse_page_owner(value: str) -> tuple[int, str]:
    address_value, separator, owner = value.partition("=")
    if not separator or not owner:
        raise argparse.ArgumentTypeError("expected PAGE=OWNER")
    try:
        address = int(address_value, 0)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid page address: {address_value}") from exc
    if address & (PAGE_SIZE - 1):
        raise argparse.ArgumentTypeError("dirty-owner page must be 4 KiB aligned")
    return _u32(address), owner


def _parse_page_mapping(value: str) -> tuple[int, int]:
    logical_value, separator, mapped_value = value.partition("=")
    if not separator:
        raise argparse.ArgumentTypeError("expected LOGICAL_PAGE=MAPPED_PAGE")
    try:
        logical = int(logical_value, 0)
        mapped = int(mapped_value, 0)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid page mapping: {value}") from exc
    if (logical | mapped) & (PAGE_SIZE - 1):
        raise argparse.ArgumentTypeError("MMIO page mappings must be 4 KiB aligned")
    return _u32(logical), _u32(mapped)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build", help="Build one fixed-endpoint IA-32 PE artifact.")
    build.add_argument("capsule", type=Path)
    build.add_argument("--stop-eip", type=lambda value: int(value, 0), required=True)
    build.add_argument("--build-dir", type=Path, default=REPO_ROOT / "build/local/ia32")
    build.add_argument("--report", type=Path)
    execute = commands.add_parser(
        "execute", help="Build if needed and execute one fixed-endpoint slice."
    )
    execute.add_argument("capsule", type=Path)
    execute.add_argument("--stop-eip", type=lambda value: int(value, 0), required=True)
    execute.add_argument("--build-dir", type=Path, default=REPO_ROOT / "build/local/ia32")
    execute.add_argument("--report", type=Path)
    store_build = commands.add_parser(
        "build-store",
        help="Build a Phase-2 artifact from an XBE and decoded block store.",
    )
    store_execute = commands.add_parser(
        "execute-store",
        help="Build and execute a complete Phase-2 decoded-store artifact.",
    )
    for command in (store_build, store_execute):
        command.add_argument("capsule", type=Path)
        command.add_argument("--xbe", type=Path, required=True)
        command.add_argument("--decoded-block-store", type=Path, required=True)
        command.add_argument("--stop-eip", type=lambda value: int(value, 0), required=True)
        command.add_argument(
            "--build-dir",
            type=Path,
            default=REPO_ROOT / "build/local/ia32-phase2",
        )
        command.add_argument("--report", type=Path)
    architecture_build = commands.add_parser(
        "build-architecture",
        help="Build a Phase-3 architecture and memory artifact.",
    )
    architecture_execute = commands.add_parser(
        "execute-architecture",
        help="Build and execute a Phase-3 architecture and memory artifact.",
    )
    for command in (architecture_build, architecture_execute):
        command.add_argument("capsule", type=Path)
        command.add_argument("--xbe", type=Path)
        command.add_argument("--decoded-block-store", type=Path)
        command.add_argument("--stop-eip", type=lambda value: int(value, 0), required=True)
        command.add_argument(
            "--build-dir",
            type=Path,
            default=REPO_ROOT / "build/local/ia32-phase3",
        )
        command.add_argument("--dirty-owner", action="append", type=_parse_page_owner)
        command.add_argument(
            "--recoverable-fault-page",
            action="append",
            type=lambda value: int(value, 0),
        )
        command.add_argument("--mmio-shadow", action="append", type=_parse_page_mapping)
        command.add_argument("--report", type=Path)
    host_abi_build = commands.add_parser(
        "build-host-abi",
        help="Build a Phase-4 audited native host ABI artifact.",
    )
    host_abi_execute = commands.add_parser(
        "execute-host-abi",
        help="Build and execute a Phase-4 audited native host ABI artifact.",
    )
    for command in (host_abi_build, host_abi_execute):
        command.add_argument("capsule", type=Path)
        command.add_argument("--stop-eip", type=lambda value: int(value, 0), required=True)
        command.add_argument(
            "--build-dir",
            type=Path,
            default=REPO_ROOT / "build/local/ia32-phase4",
        )
        command.add_argument("--report", type=Path)
    scheduler_build = commands.add_parser(
        "build-scheduler",
        help="Build a Phase-5 resident primary/worker/vblank scheduler artifact.",
    )
    scheduler_execute = commands.add_parser(
        "execute-scheduler",
        help="Build and execute a Phase-5 resident scheduler artifact.",
    )
    for command in (scheduler_build, scheduler_execute):
        command.add_argument("capsule", type=Path)
        command.add_argument("--stop-eip", type=lambda value: int(value, 0), required=True)
        command.add_argument(
            "--build-dir",
            type=Path,
            default=REPO_ROOT / "build/local/ia32-phase5",
        )
        command.add_argument("--report", type=Path)
    coverage_build = commands.add_parser(
        "build-coverage",
        help="Build a Phase-6 closed measured vertical-slice artifact.",
    )
    coverage_execute = commands.add_parser(
        "execute-coverage",
        help="Build and execute a Phase-6 closed measured vertical-slice artifact.",
    )
    for command in (coverage_build, coverage_execute):
        command.add_argument("capsule", type=Path)
        command.add_argument("--xbe", type=Path, required=True)
        command.add_argument("--decoded-block-store", type=Path, required=True)
        command.add_argument("--coverage-profile", type=Path, required=True)
        command.add_argument("--stop-eip", type=lambda value: int(value, 0), required=True)
        command.add_argument(
            "--build-dir",
            type=Path,
            default=REPO_ROOT / "build/local/ia32-phase6",
        )
        command.add_argument("--report", type=Path)
    cutover_build = commands.add_parser(
        "build-cutover",
        help="Build a Phase-7 normal-launch artifact with resident memory ownership.",
    )
    cutover_execute = commands.add_parser(
        "execute-cutover",
        help="Build and run two warm Phase-7 resident cutover dispatches.",
    )
    full_flip_execute = commands.add_parser(
        "execute-full-flip",
        help=(
            "Build and validate repeated Phase-7 scheduler/flip dispatches "
            "against the decoded oracle."
        ),
    )
    for command in (cutover_build, cutover_execute, full_flip_execute):
        command.add_argument("capsule", type=Path)
        command.add_argument("--xbe", type=Path, required=True)
        command.add_argument("--decoded-block-store", type=Path, required=True)
        command.add_argument("--coverage-profile", type=Path, required=True)
        command.add_argument(
            "--stop-eip",
            type=lambda value: int(value, 0),
            required=command is not full_flip_execute,
            help=(
                "Guest stop EIP; execute-full-flip infers the observed continuation when omitted."
            ),
        )
        command.add_argument(
            "--build-dir",
            type=Path,
            default=REPO_ROOT / "build/local/ia32-phase7",
        )
        command.add_argument("--dispatch-count", type=int, default=2)
        command.add_argument("--report", type=Path)
    normal_live_build = commands.add_parser(
        "build-normal-live",
        help="Build the XBE-entry Phase-7 artifact used by the ordinary live path.",
    )
    normal_live_build.add_argument("capsule", type=Path)
    normal_live_build.add_argument("--xbe", type=Path, required=True)
    normal_live_build.add_argument("--decoded-block-store", type=Path, required=True)
    normal_live_build.add_argument("--coverage-profile", type=Path, required=True)
    normal_live_build.add_argument(
        "--stop-eip",
        type=lambda value: int(value, 0),
        default=NORMAL_LIVE_FLIP_STOP_ADDRESS,
    )
    normal_live_build.add_argument(
        "--build-dir",
        type=Path,
        default=REPO_ROOT / "build/local/ia32-live",
    )
    normal_live_build.add_argument("--report", type=Path)
    coverage_profile = commands.add_parser(
        "profile-coverage",
        help="Convert a performance-debug report into a Phase-6 coverage profile.",
    )
    coverage_profile.add_argument("performance_debug_report", type=Path)
    coverage_profile.add_argument(
        "--mode",
        choices=("diagnostic-discovery", "normal-validation"),
        default="normal-validation",
    )
    coverage_profile.add_argument("--report", type=Path, required=True)
    run = commands.add_parser("run", help="Execute a built fixed-endpoint artifact once.")
    run.add_argument("capsule", type=Path)
    run.add_argument("artifact", type=Path)
    run.add_argument("--xbe", type=Path)
    run.add_argument("--decoded-block-store", type=Path)
    run.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "profile-coverage":
            converted_profile = coverage_profile_from_performance_debug_report(
                args.performance_debug_report,
                mode=args.mode,
            )
            _write_report(args.report, _normalized_coverage_profile(converted_profile))
            return 0
        capsule = load_replay_capsule(args.capsule)
        if args.command == "execute-full-flip" and args.stop_eip is None:
            scheduler = (capsule.scheduler_state or {}).get("resident_scheduler")
            oracle = scheduler.get("full_flip_oracle") if isinstance(scheduler, Mapping) else None
            if not isinstance(oracle, Mapping) or "actual_stop_eip" not in oracle:
                raise Ia32BackendError(
                    "execute-full-flip could not infer an observed stop EIP from the capsule"
                )
            args.stop_eip = _u32(int(oracle["actual_stop_eip"]))
        if args.command in {
            "build",
            "execute",
            "build-store",
            "execute-store",
            "build-architecture",
            "execute-architecture",
            "build-host-abi",
            "execute-host-abi",
            "build-scheduler",
            "execute-scheduler",
            "build-coverage",
            "execute-coverage",
            "build-cutover",
            "execute-cutover",
            "execute-full-flip",
            "build-normal-live",
        }:
            if args.command in {"build-store", "execute-store"}:
                artifact = build_ia32_decoded_store_artifact(
                    capsule,
                    xbe_path=args.xbe,
                    decoded_block_store_path=args.decoded_block_store,
                    stop_eip=args.stop_eip,
                    build_dir=args.build_dir,
                )
            elif args.command in {"build-architecture", "execute-architecture"}:
                if (args.xbe is None) != (args.decoded_block_store is None):
                    raise Ia32BackendError(
                        "Phase-3 decoded-store builds require both --xbe and --decoded-block-store"
                    )
                architecture_profile = Ia32ArchitectureProfile(
                    dirty_page_owners=dict(args.dirty_owner or []),
                    recoverable_fault_pages=tuple(
                        int(value) for value in (args.recoverable_fault_page or [])
                    ),
                    mmio_shadow_pages=dict(args.mmio_shadow or []),
                )
                if args.xbe is not None and args.decoded_block_store is not None:
                    artifact = build_ia32_decoded_store_architecture_artifact(
                        capsule,
                        xbe_path=args.xbe,
                        decoded_block_store_path=args.decoded_block_store,
                        stop_eip=args.stop_eip,
                        build_dir=args.build_dir,
                        profile=architecture_profile,
                    )
                else:
                    artifact = build_ia32_architecture_artifact(
                        capsule,
                        stop_eip=args.stop_eip,
                        build_dir=args.build_dir,
                        profile=architecture_profile,
                    )
            elif args.command in {"build-host-abi", "execute-host-abi"}:
                artifact = build_ia32_host_abi_artifact(
                    capsule,
                    stop_eip=args.stop_eip,
                    build_dir=args.build_dir,
                )
            elif args.command in {"build-scheduler", "execute-scheduler"}:
                artifact = build_ia32_resident_scheduler_artifact(
                    capsule,
                    stop_eip=args.stop_eip,
                    build_dir=args.build_dir,
                )
            elif args.command in {"build-coverage", "execute-coverage"}:
                artifact = build_ia32_coverage_growth_artifact(
                    capsule,
                    xbe_path=args.xbe,
                    decoded_block_store_path=args.decoded_block_store,
                    coverage_profile=load_ia32_coverage_profile(args.coverage_profile),
                    stop_eip=args.stop_eip,
                    build_dir=args.build_dir,
                )
            elif args.command in {
                "build-cutover",
                "execute-cutover",
                "execute-full-flip",
            }:
                artifact = build_ia32_launcher_cutover_artifact(
                    capsule,
                    xbe_path=args.xbe,
                    decoded_block_store_path=args.decoded_block_store,
                    coverage_profile=load_ia32_coverage_profile(args.coverage_profile),
                    stop_eip=args.stop_eip,
                    build_dir=args.build_dir,
                    require_full_flip_oracle=args.command == "execute-full-flip",
                )
            elif args.command == "build-normal-live":
                artifact = build_ia32_normal_live_artifact(
                    capsule,
                    xbe_path=args.xbe,
                    decoded_block_store_path=args.decoded_block_store,
                    coverage_profile=load_ia32_coverage_profile(args.coverage_profile),
                    stop_eip=args.stop_eip,
                    build_dir=args.build_dir,
                )
            else:
                artifact = build_ia32_slice_artifact(
                    capsule,
                    stop_eip=args.stop_eip,
                    build_dir=args.build_dir,
                )
            if args.command in {
                "execute",
                "execute-store",
                "execute-architecture",
                "execute-host-abi",
                "execute-scheduler",
                "execute-coverage",
                "execute-cutover",
                "execute-full-flip",
            }:
                report = (
                    run_ia32_full_flip_cutover(
                        artifact,
                        capsule,
                        dispatch_count=args.dispatch_count,
                    )
                    if args.command == "execute-full-flip"
                    else run_ia32_launcher_cutover(
                        artifact,
                        capsule,
                        dispatch_count=args.dispatch_count,
                    )
                    if args.command == "execute-cutover"
                    else run_ia32_slice_artifact(artifact, capsule).summary()
                )
            else:
                report = {
                    "artifact_id": artifact.artifact_id,
                    "artifact": str(artifact.root),
                    "manifest": str(artifact.manifest_path),
                    "executable": str(artifact.executable),
                }
        else:
            artifact = load_ia32_slice_artifact(args.artifact)
            if artifact.manifest.get("decoded_store_artifact") is True:
                if args.xbe is None or args.decoded_block_store is None:
                    raise Ia32BackendError(
                        "running a Phase-2 artifact requires --xbe and --decoded-block-store"
                    )
                validate_ia32_decoded_store_artifact_sources(
                    artifact,
                    xbe_path=args.xbe,
                    decoded_block_store_path=args.decoded_block_store,
                )
            report = run_ia32_slice_artifact(artifact, capsule).summary()
        _write_report(args.report, report)
        return 0
    except (Ia32BackendError, OSError, ReplayCapsuleError, ValueError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    sys.exit(main())
