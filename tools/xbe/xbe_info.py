#!/usr/bin/env python3
"""Xbox XBE metadata parser for local analysis and loader planning."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import struct
from pathlib import Path
from typing import Any


class XbeFormatError(ValueError):
    """Raised when an input file is not a parseable XBE."""


MAGIC = b"XBEH"
HEADER_MIN_SIZE = 0x178
HEADER_EXTENDED_TYPE1_SIZE = 0x180
HEADER_EXTENDED_TYPE2_SIZE = 0x184
SECTION_HEADER_SIZE = 0x38
LIBRARY_VERSION_SIZE = 0x10
TLS_HEADER_SIZE = 0x18
IMPORT_DESCRIPTOR_SIZE = 0x08
CERTIFICATE_MIN_SIZE = 0x1D0
CERTIFICATE_EXTENDED_SIZE = 0x1EC
CERTIFICATE_TITLE_NAME_OFFSET = 0x0C
CERTIFICATE_TITLE_NAME_SIZE = 0x50
TERMINATED_TABLE_MAX_ENTRIES = 4096

ENTRY_KEYS = {
    "beta": 0xE682F45B,
    "debug": 0x94859D4B,
    "retail": 0xA8FC57AB,
}

KERNEL_THUNK_KEYS = {
    "beta": 0x46437DCD,
    "debug": 0xEFB1F152,
    "retail": 0x5B6D40B6,
}

INIT_FLAGS = {
    0x00000001: "MOUNT_UTILITY_DRIVE",
    0x00000002: "FORMAT_UTILITY_DRIVE",
    0x00000004: "LIMIT_64_MB",
    0x00000008: "DONT_SETUP_HARD_DISK",
}

MEDIA_FLAGS = {
    0x00000001: "HARD_DISK",
    0x00000002: "DVD_X2",
    0x00000004: "DVD_CD",
    0x00000008: "CD",
    0x00000010: "DVD_5_RO",
    0x00000020: "DVD_9_RO",
    0x00000040: "DVD_5_RW",
    0x00000080: "DVD_9_RW",
    0x00000100: "DONGLE",
    0x00000200: "MEDIA_BOARD",
    0x40000000: "NONSECURE_HARD_DISK",
    0x80000000: "NONSECURE_MODE",
}

REGION_FLAGS = {
    0x00000001: "NA",
    0x00000002: "JAPAN",
    0x00000004: "REST_OF_WORLD",
    0x80000000: "MANUFACTURING",
}

SECTION_FLAGS = {
    0x00000001: "WRITABLE",
    0x00000002: "PRELOAD",
    0x00000004: "EXECUTABLE",
    0x00000008: "INSERTED_FILE",
    0x00000010: "HEAD_PAGE_READ_ONLY",
    0x00000020: "TAIL_PAGE_READ_ONLY",
}

# Source: https://xboxdevwiki.net/Kernel#Kernel_exports
KERNEL_EXPORT_NAMES = {
    1: 'AvGetSavedDataAddress',
    2: 'AvSendTVEncoderOption',
    3: 'AvSetDisplayMode',
    4: 'AvSetSavedDataAddress',
    5: 'DbgBreakPoint',
    6: 'DbgBreakPointWithStatus',
    7: 'DbgLoadImageSymbols',
    8: 'DbgPrint',
    9: 'HalReadSMCTrayState',
    10: 'DbgPrompt',
    11: 'DbgUnLoadImageSymbols',
    12: 'ExAcquireReadWriteLockExclusive',
    13: 'ExAcquireReadWriteLockShared',
    14: 'ExAllocatePool',
    15: 'ExAllocatePoolWithTag',
    16: 'ExEventObjectType',
    17: 'ExFreePool',
    18: 'ExInitializeReadWriteLock',
    19: 'ExInterlockedAddLargeInteger',
    20: 'ExInterlockedAddLargeStatistic',
    21: 'ExInterlockedCompareExchange64',
    22: 'ExMutantObjectType',
    23: 'ExQueryPoolBlockSize',
    24: 'ExQueryNonVolatileSetting',
    25: 'ExReadWriteRefurbInfo',
    26: 'ExRaiseException',
    27: 'ExRaiseStatus',
    28: 'ExReleaseReadWriteLock',
    29: 'ExSaveNonVolatileSetting',
    30: 'ExSemaphoreObjectType',
    31: 'ExTimerObjectType',
    32: 'ExfInterlockedInsertHeadList',
    33: 'ExfInterlockedInsertTailList',
    34: 'ExfInterlockedRemoveHeadList',
    35: 'FscGetCacheSize',
    36: 'FscInvalidateIdleBlocks',
    37: 'FscSetCacheSize',
    38: 'HalClearSoftwareInterrupt',
    39: 'HalDisableSystemInterrupt',
    40: 'HalDiskCachePartitionCount',
    41: 'HalDiskModelNumber',
    42: 'HalDiskSerialNumber',
    43: 'HalEnableSystemInterrupt',
    44: 'HalGetInterruptVector',
    45: 'HalReadSMBusValue',
    46: 'HalReadWritePCISpace',
    47: 'HalRegisterShutdownNotification',
    48: 'HalRequestSoftwareInterrupt',
    49: 'HalReturnToFirmware',
    50: 'HalWriteSMBusValue',
    51: 'InterlockedCompareExchange',
    52: 'InterlockedDecrement',
    53: 'InterlockedIncrement',
    54: 'InterlockedExchange',
    55: 'InterlockedExchangeAdd',
    56: 'InterlockedFlushSList',
    57: 'InterlockedPopEntrySList',
    58: 'InterlockedPushEntrySList',
    59: 'IoAllocateIrp',
    60: 'IoBuildAsynchronousFsdRequest',
    61: 'IoBuildDeviceIoControlRequest',
    62: 'IoBuildSynchronousFsdRequest',
    63: 'IoCheckShareAccess',
    64: 'IoCompletionObjectType',
    65: 'IoCreateDevice',
    66: 'IoCreateFile',
    67: 'IoCreateSymbolicLink',
    68: 'IoDeleteDevice',
    69: 'IoDeleteSymbolicLink',
    70: 'IoDeviceObjectType',
    71: 'IoFileObjectType',
    72: 'IoFreeIrp',
    73: 'IoInitializeIrp',
    74: 'IoInvalidDeviceRequest',
    75: 'IoQueryFileInformation',
    76: 'IoQueryVolumeInformation',
    77: 'IoQueueThreadIrp',
    78: 'IoRemoveShareAccess',
    79: 'IoSetIoCompletion',
    80: 'IoSetShareAccess',
    81: 'IoStartNextPacket',
    82: 'IoStartNextPacketByKey',
    83: 'IoStartPacket',
    84: 'IoSynchronousDeviceIoControlRequest',
    85: 'IoSynchronousFsdRequest',
    86: 'IofCallDriver',
    87: 'IofCompleteRequest',
    88: 'KdDebuggerEnabled',
    89: 'KdDebuggerNotPresent',
    90: 'IoDismountVolume',
    91: 'IoDismountVolumeByName',
    92: 'KeAlertResumeThread',
    93: 'KeAlertThread',
    94: 'KeBoostPriorityThread',
    95: 'KeBugCheck',
    96: 'KeBugCheckEx',
    97: 'KeCancelTimer',
    98: 'KeConnectInterrupt',
    99: 'KeDelayExecutionThread',
    100: 'KeDisconnectInterrupt',
    101: 'KeEnterCriticalRegion',
    102: 'MmGlobalData',
    103: 'KeGetCurrentIrql',
    104: 'KeGetCurrentThread',
    105: 'KeInitializeApc',
    106: 'KeInitializeDeviceQueue',
    107: 'KeInitializeDpc',
    108: 'KeInitializeEvent',
    109: 'KeInitializeInterrupt',
    110: 'KeInitializeMutant',
    111: 'KeInitializeQueue',
    112: 'KeInitializeSemaphore',
    113: 'KeInitializeTimerEx',
    114: 'KeInsertByKeyDeviceQueue',
    115: 'KeInsertDeviceQueue',
    116: 'KeInsertHeadQueue',
    117: 'KeInsertQueue',
    118: 'KeInsertQueueApc',
    119: 'KeInsertQueueDpc',
    120: 'KeInterruptTime',
    121: 'KeIsExecutingDpc',
    122: 'KeLeaveCriticalRegion',
    123: 'KePulseEvent',
    124: 'KeQueryBasePriorityThread',
    125: 'KeQueryInterruptTime',
    126: 'KeQueryPerformanceCounter',
    127: 'KeQueryPerformanceFrequency',
    128: 'KeQuerySystemTime',
    129: 'KeRaiseIrqlToDpcLevel',
    130: 'KeRaiseIrqlToSynchLevel',
    131: 'KeReleaseMutant',
    132: 'KeReleaseSemaphore',
    133: 'KeRemoveByKeyDeviceQueue',
    134: 'KeRemoveDeviceQueue',
    135: 'KeRemoveEntryDeviceQueue',
    136: 'KeRemoveQueue',
    137: 'KeRemoveQueueDpc',
    138: 'KeResetEvent',
    139: 'KeRestoreFloatingPointState',
    140: 'KeResumeThread',
    141: 'KeRundownQueue',
    142: 'KeSaveFloatingPointState',
    143: 'KeSetBasePriorityThread',
    144: 'KeSetDisableBoostThread',
    145: 'KeSetEvent',
    146: 'KeSetEventBoostPriority',
    147: 'KeSetPriorityProcess',
    148: 'KeSetPriorityThread',
    149: 'KeSetTimer',
    150: 'KeSetTimerEx',
    151: 'KeStallExecutionProcessor',
    152: 'KeSuspendThread',
    153: 'KeSynchronizeExecution',
    154: 'KeSystemTime',
    155: 'KeTestAlertThread',
    156: 'KeTickCount',
    157: 'KeTimeIncrement',
    158: 'KeWaitForMultipleObjects',
    159: 'KeWaitForSingleObject',
    160: 'KfRaiseIrql',
    161: 'KfLowerIrql',
    162: 'KiBugCheckData',
    163: 'KiUnlockDispatcherDatabase',
    164: 'LaunchDataPage',
    165: 'MmAllocateContiguousMemory',
    166: 'MmAllocateContiguousMemoryEx',
    167: 'MmAllocateSystemMemory',
    168: 'MmClaimGpuInstanceMemory',
    169: 'MmCreateKernelStack',
    170: 'MmDeleteKernelStack',
    171: 'MmFreeContiguousMemory',
    172: 'MmFreeSystemMemory',
    173: 'MmGetPhysicalAddress',
    174: 'MmIsAddressValid',
    175: 'MmLockUnlockBufferPages',
    176: 'MmLockUnlockPhysicalPage',
    177: 'MmMapIoSpace',
    178: 'MmPersistContiguousMemory',
    179: 'MmQueryAddressProtect',
    180: 'MmQueryAllocationSize',
    181: 'MmQueryStatistics',
    182: 'MmSetAddressProtect',
    183: 'MmUnmapIoSpace',
    184: 'NtAllocateVirtualMemory',
    185: 'NtCancelTimer',
    186: 'NtClearEvent',
    187: 'NtClose',
    188: 'NtCreateDirectoryObject',
    189: 'NtCreateEvent',
    190: 'NtCreateFile',
    191: 'NtCreateIoCompletion',
    192: 'NtCreateMutant',
    193: 'NtCreateSemaphore',
    194: 'NtCreateTimer',
    195: 'NtDeleteFile',
    196: 'NtDeviceIoControlFile',
    197: 'NtDuplicateObject',
    198: 'NtFlushBuffersFile',
    199: 'NtFreeVirtualMemory',
    200: 'NtFsControlFile',
    201: 'NtOpenDirectoryObject',
    202: 'NtOpenFile',
    203: 'NtOpenSymbolicLinkObject',
    204: 'NtProtectVirtualMemory',
    205: 'NtPulseEvent',
    206: 'NtQueueApcThread',
    207: 'NtQueryDirectoryFile',
    208: 'NtQueryDirectoryObject',
    209: 'NtQueryEvent',
    210: 'NtQueryFullAttributesFile',
    211: 'NtQueryInformationFile',
    212: 'NtQueryIoCompletion',
    213: 'NtQueryMutant',
    214: 'NtQuerySemaphore',
    215: 'NtQuerySymbolicLinkObject',
    216: 'NtQueryTimer',
    217: 'NtQueryVirtualMemory',
    218: 'NtQueryVolumeInformationFile',
    219: 'NtReadFile',
    220: 'NtReadFileScatter',
    221: 'NtReleaseMutant',
    222: 'NtReleaseSemaphore',
    223: 'NtRemoveIoCompletion',
    224: 'NtResumeThread',
    225: 'NtSetEvent',
    226: 'NtSetInformationFile',
    227: 'NtSetIoCompletion',
    228: 'NtSetSystemTime',
    229: 'NtSetTimerEx',
    230: 'NtSignalAndWaitForSingleObjectEx',
    231: 'NtSuspendThread',
    232: 'NtUserIoApcDispatcher',
    233: 'NtWaitForSingleObject',
    234: 'NtWaitForSingleObjectEx',
    235: 'NtWaitForMultipleObjectsEx',
    236: 'NtWriteFile',
    237: 'NtWriteFileGather',
    238: 'NtYieldExecution',
    239: 'ObCreateObject',
    240: 'ObDirectoryObjectType',
    241: 'ObInsertObject',
    242: 'ObMakeTemporaryObject',
    243: 'ObOpenObjectByName',
    244: 'ObOpenObjectByPointer',
    245: 'ObpObjectHandleTable',
    246: 'ObReferenceObjectByHandle',
    247: 'ObReferenceObjectByName',
    248: 'ObReferenceObjectByPointer',
    249: 'ObSymbolicLinkObjectType',
    250: 'ObfDereferenceObject',
    251: 'ObfReferenceObject',
    252: 'PhyGetLinkState',
    253: 'PhyInitialize',
    254: 'PsCreateSystemThread',
    255: 'PsCreateSystemThreadEx',
    256: 'PsQueryStatistics',
    257: 'PsSetCreateThreadNotifyRoutine',
    258: 'PsTerminateSystemThread',
    259: 'PsThreadObjectType',
    260: 'RtlAnsiStringToUnicodeString',
    261: 'RtlAppendStringToString',
    262: 'RtlAppendUnicodeStringToString',
    263: 'RtlAppendUnicodeToString',
    264: 'RtlAssert',
    265: 'RtlCaptureContext',
    266: 'RtlCaptureStackBackTrace',
    267: 'RtlCharToInteger',
    268: 'RtlCompareMemory',
    269: 'RtlCompareMemoryUlong',
    270: 'RtlCompareString',
    271: 'RtlCompareUnicodeString',
    272: 'RtlCopyString',
    273: 'RtlCopyUnicodeString',
    274: 'RtlCreateUnicodeString',
    275: 'RtlDowncaseUnicodeChar',
    276: 'RtlDowncaseUnicodeString',
    277: 'RtlEnterCriticalSection',
    278: 'RtlEnterCriticalSectionAndRegion',
    279: 'RtlEqualString',
    280: 'RtlEqualUnicodeString',
    281: 'RtlExtendedIntegerMultiply',
    282: 'RtlExtendedLargeIntegerDivide',
    283: 'RtlExtendedMagicDivide',
    284: 'RtlFillMemory',
    285: 'RtlFillMemoryUlong',
    286: 'RtlFreeAnsiString',
    287: 'RtlFreeUnicodeString',
    288: 'RtlGetCallersAddress',
    289: 'RtlInitAnsiString',
    290: 'RtlInitUnicodeString',
    291: 'RtlInitializeCriticalSection',
    292: 'RtlIntegerToChar',
    293: 'RtlIntegerToUnicodeString',
    294: 'RtlLeaveCriticalSection',
    295: 'RtlLeaveCriticalSectionAndRegion',
    296: 'RtlLowerChar',
    297: 'RtlMapGenericMask',
    298: 'RtlMoveMemory',
    299: 'RtlMultiByteToUnicodeN',
    300: 'RtlMultiByteToUnicodeSize',
    301: 'RtlNtStatusToDosError',
    302: 'RtlRaiseException',
    303: 'RtlRaiseStatus',
    304: 'RtlTimeFieldsToTime',
    305: 'RtlTimeToTimeFields',
    306: 'RtlTryEnterCriticalSection',
    307: 'RtlUlongByteSwap',
    308: 'RtlUnicodeStringToAnsiString',
    309: 'RtlUnicodeStringToInteger',
    310: 'RtlUnicodeToMultiByteN',
    311: 'RtlUnicodeToMultiByteSize',
    312: 'RtlUnwind',
    313: 'RtlUpcaseUnicodeChar',
    314: 'RtlUpcaseUnicodeString',
    315: 'RtlUpcaseUnicodeToMultiByteN',
    316: 'RtlUpperChar',
    317: 'RtlUpperString',
    318: 'RtlUshortByteSwap',
    319: 'RtlWalkFrameChain',
    320: 'RtlZeroMemory',
    321: 'XboxEEPROMKey',
    322: 'XboxHardwareInfo',
    323: 'XboxHDKey',
    324: 'XboxKrnlVersion',
    325: 'XboxSignatureKey',
    326: 'XeImageFileName',
    327: 'XeLoadSection',
    328: 'XeUnloadSection',
    329: 'READ_PORT_BUFFER_UCHAR',
    330: 'READ_PORT_BUFFER_USHORT',
    331: 'READ_PORT_BUFFER_ULONG',
    332: 'WRITE_PORT_BUFFER_UCHAR',
    333: 'WRITE_PORT_BUFFER_USHORT',
    334: 'WRITE_PORT_BUFFER_ULONG',
    335: 'XcSHAInit',
    336: 'XcSHAUpdate',
    337: 'XcSHAFinal',
    338: 'XcRC4Key',
    339: 'XcRC4Crypt',
    340: 'XcHMAC',
    341: 'XcPKEncPublic',
    342: 'XcPKDecPrivate',
    343: 'XcPKGetKeyLen',
    344: 'XcVerifyPKCS1Signature',
    345: 'XcModExp',
    346: 'XcDESKeyParity',
    347: 'XcKeyTable',
    348: 'XcBlockCrypt',
    349: 'XcBlockCryptCBC',
    350: 'XcCryptService',
    351: 'XcUpdateCrypto',
    352: 'RtlRip',
    353: 'XboxLANKey',
    354: 'XboxAlternateSignatureKeys',
    355: 'XePublicKeyData',
    356: 'HalBootSMCVideoMode',
    357: 'IdexChannelObject',
    358: 'HalIsResetOrShutdownPending',
    359: 'IoMarkIrpMustComplete',
    360: 'HalInitiateShutdown',
    361: 'RtlSnprintf',
    362: 'RtlSprintf',
    363: 'RtlVsnprintf',
    364: 'RtlVsprintf',
    365: 'HalEnableSecureTrayEject',
    366: 'HalWriteSMCScratchRegister',
    370: 'XProfpControl',
    371: 'XProfpGetData',
    372: 'IrtClientInitFast',
    373: 'IrtSweep',
    374: 'MmDbgAllocateMemory',
    375: 'MmDbgFreeMemory',
    376: 'MmDbgQueryAvailablePages',
    377: 'MmDbgReleaseAddress',
    378: 'MmDbgWriteCheck',
}

def _require_range(data: bytes, offset: int, size: int, label: str) -> None:
    if offset < 0 or size < 0 or offset + size > len(data):
        raise XbeFormatError(
            f"{label} range 0x{offset:X}..0x{offset + size:X} is outside file"
        )


def _u16(data: bytes, offset: int) -> int:
    _require_range(data, offset, 2, "u16")
    return struct.unpack_from("<H", data, offset)[0]


def _u32(data: bytes, offset: int) -> int:
    _require_range(data, offset, 4, "u32")
    return struct.unpack_from("<I", data, offset)[0]


def _timestamp_to_iso8601(value: int) -> str | None:
    if value == 0:
        return None
    try:
        return dt.datetime.fromtimestamp(value, tz=dt.UTC).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _decode_fixed_ascii(raw: bytes) -> str:
    return raw.split(b"\x00", 1)[0].decode("ascii", errors="replace")


def _decode_fixed_utf16le(raw: bytes) -> str:
    text = raw.decode("utf-16-le", errors="replace")
    return text.split("\x00", 1)[0]


def _read_c_string(data: bytes, offset: int) -> str:
    _require_range(data, offset, 1, "string")
    end = data.find(b"\x00", offset)
    if end == -1:
        end = len(data)
    return data[offset:end].decode("ascii", errors="replace")


def _read_wide_string(data: bytes, offset: int) -> str:
    _require_range(data, offset, 2, "wide string")
    cursor = offset
    raw = bytearray()
    while cursor + 1 < len(data):
        unit = data[cursor : cursor + 2]
        if unit == b"\x00\x00":
            break
        raw.extend(unit)
        cursor += 2
    return raw.decode("utf-16-le", errors="replace")


def _flags(value: int, names: dict[int, str]) -> list[str]:
    return [name for bit, name in names.items() if value & bit]


def _unknown_flag_bits(value: int, names: dict[int, str]) -> int:
    known = 0
    for bit in names:
        known |= bit
    return value & ~known


def _hex32(value: int) -> str:
    return f"0x{value:08X}"


def _range(start: int, size: int) -> dict[str, Any]:
    return {
        "start": start,
        "end": start + size,
        "size": size,
        "start_hex": _hex32(start),
        "end_hex": _hex32(start + size),
    }


def _title_id_details(title_id: int) -> dict[str, Any]:
    hex_value = f"{title_id:08X}"
    publisher_code: str | None = None
    try:
        publisher_bytes = bytes.fromhex(hex_value[:4])
        if all(32 <= byte <= 126 for byte in publisher_bytes):
            publisher_code = publisher_bytes.decode("ascii")
    except ValueError:
        publisher_code = None

    return {
        "hex": hex_value,
        "publisher_code": publisher_code,
        "publisher_title_number": int(hex_value[4:], 16),
    }


def _parse_header(data: bytes) -> dict[str, Any]:
    timestamp = _u32(data, 0x0114)
    pe_timestamp = _u32(data, 0x0148)
    init_flags = _u32(data, 0x0124)
    header = {
        "base_address": _u32(data, 0x0104),
        "headers_size": _u32(data, 0x0108),
        "image_size": _u32(data, 0x010C),
        "image_header_size": _u32(data, 0x0110),
        "timestamp": timestamp,
        "timestamp_utc": _timestamp_to_iso8601(timestamp),
        "certificate_address": _u32(data, 0x0118),
        "section_count": _u32(data, 0x011C),
        "section_headers_address": _u32(data, 0x0120),
        "init_flags": init_flags,
        "init_flag_names": _flags(init_flags, INIT_FLAGS),
        "init_unknown_flag_bits": _unknown_flag_bits(init_flags, INIT_FLAGS),
        "encoded_entry_point": _u32(data, 0x0128),
        "tls_address": _u32(data, 0x012C),
        "stack_size": _u32(data, 0x0130),
        "pe_heap_reserve": _u32(data, 0x0134),
        "pe_heap_commit": _u32(data, 0x0138),
        "pe_base_address": _u32(data, 0x013C),
        "pe_image_size": _u32(data, 0x0140),
        "pe_checksum": _u32(data, 0x0144),
        "pe_timestamp": pe_timestamp,
        "pe_timestamp_utc": _timestamp_to_iso8601(pe_timestamp),
        "debug_pathname_address": _u32(data, 0x014C),
        "debug_filename_address": _u32(data, 0x0150),
        "debug_unicode_filename_address": _u32(data, 0x0154),
        "encoded_kernel_thunk_address": _u32(data, 0x0158),
        "non_kernel_import_directory_address": _u32(data, 0x015C),
        "library_versions_count": _u32(data, 0x0160),
        "library_versions_address": _u32(data, 0x0164),
        "kernel_library_version_address": _u32(data, 0x0168),
        "xapi_library_version_address": _u32(data, 0x016C),
        "logo_bitmap_address": _u32(data, 0x0170),
        "logo_bitmap_size": _u32(data, 0x0174),
    }

    if header["image_header_size"] >= HEADER_EXTENDED_TYPE1_SIZE:
        header["library_features_address"] = _u32(data, 0x0178)
        header["library_features_count"] = _u32(data, 0x017C)
    else:
        header["library_features_address"] = 0
        header["library_features_count"] = 0

    if header["image_header_size"] >= HEADER_EXTENDED_TYPE2_SIZE:
        header["debug_info_address"] = _u32(data, 0x0180)
    else:
        header["debug_info_address"] = 0

    header["entry_point"] = _decode_keyed_address(
        header["encoded_entry_point"], ENTRY_KEYS, header
    )
    header["kernel_thunk_address"] = _decode_keyed_address(
        header["encoded_kernel_thunk_address"], KERNEL_THUNK_KEYS, header
    )
    return header


def _validate_header(data: bytes, header: dict[str, Any]) -> None:
    if header["image_header_size"] < HEADER_MIN_SIZE:
        raise XbeFormatError(
            f"unexpected image header size: 0x{header['image_header_size']:X}"
        )
    if header["headers_size"] < header["image_header_size"]:
        raise XbeFormatError(
            "headers_size is smaller than image_header_size: "
            f"0x{header['headers_size']:X} < 0x{header['image_header_size']:X}"
        )
    if header["headers_size"] > len(data):
        raise XbeFormatError(
            f"headers_size 0x{header['headers_size']:X} is larger than file"
        )
    if header["image_size"] < header["headers_size"]:
        raise XbeFormatError(
            "image_size is smaller than headers_size: "
            f"0x{header['image_size']:X} < 0x{header['headers_size']:X}"
        )


def _decode_keyed_address(
    encoded: int, keys: dict[str, int], header: dict[str, Any]
) -> dict[str, Any]:
    base_address = header["base_address"]
    image_end = base_address + header["image_size"]
    candidates = []
    chosen = None

    for name, key in keys.items():
        decoded = encoded ^ key
        valid = base_address <= decoded < image_end
        candidate = {
            "key": name,
            "value": decoded,
            "address": _hex32(decoded),
            "valid_for_image": valid,
        }
        candidates.append(candidate)
        if valid and chosen is None:
            chosen = candidate

    return {
        "encoded": _hex32(encoded),
        "encoded_value": encoded,
        "selected": chosen,
        "candidates": candidates,
    }


def _header_address_to_file_offset(
    address: int, header: dict[str, Any], size: int = 1
) -> int:
    base_address = header["base_address"]
    headers_size = header["headers_size"]
    if base_address <= address and address + size <= base_address + headers_size:
        return address - base_address
    raise XbeFormatError(
        f"virtual range 0x{address:08X}..0x{address + size:08X} "
        "is not inside the XBE header"
    )


def _map_virtual_address(
    address: int,
    header: dict[str, Any],
    sections: list[dict[str, Any]],
    size: int = 1,
    *,
    require_file_backed: bool = True,
) -> dict[str, Any]:
    if address == 0:
        raise XbeFormatError("null virtual address cannot be mapped")
    if size < 0:
        raise XbeFormatError("negative mapping size is invalid")

    base_address = header["base_address"]
    headers_size = header["headers_size"]
    if base_address <= address and address + size <= base_address + headers_size:
        return {
            "region": "$headers",
            "region_index": None,
            "virtual_address": address,
            "virtual_offset": address - base_address,
            "file_offset": address - base_address,
            "file_backed": True,
            "zero_fill": False,
        }

    for section in sections:
        virtual_address = section["virtual_address"]
        virtual_size = section["virtual_size"]
        relative = address - virtual_address
        if relative < 0 or relative + size > virtual_size:
            continue

        raw_size = section["raw_size"]
        file_backed = relative + size <= raw_size
        if file_backed:
            file_offset = section["raw_address"] + relative
        elif require_file_backed:
            raise XbeFormatError(
                f"virtual range 0x{address:08X}..0x{address + size:08X} "
                f"falls in zero-fill memory for section {section['name']!r}"
            )
        else:
            file_offset = None

        return {
            "region": section["name"],
            "region_index": section["index"],
            "virtual_address": address,
            "virtual_offset": relative,
            "file_offset": file_offset,
            "file_backed": file_backed,
            "zero_fill": not file_backed,
        }

    raise XbeFormatError(f"virtual address 0x{address:08X} could not be mapped")


def _parse_section_headers(data: bytes, header: dict[str, Any]) -> list[dict[str, Any]]:
    section_headers_offset = _header_address_to_file_offset(
        header["section_headers_address"],
        header,
        header["section_count"] * SECTION_HEADER_SIZE,
    )

    sections = []
    for index in range(header["section_count"]):
        offset = section_headers_offset + index * SECTION_HEADER_SIZE
        _require_range(data, offset, SECTION_HEADER_SIZE, "section header")
        (
            flags,
            virtual_address,
            virtual_size,
            raw_address,
            raw_size,
            section_name_address,
            section_name_ref_count,
            head_shared_page_ref_count_address,
            tail_shared_page_ref_count_address,
        ) = struct.unpack_from("<IIIIIIIII", data, offset)
        digest = data[offset + 0x24 : offset + SECTION_HEADER_SIZE].hex().upper()

        sections.append(
            {
                "index": index,
                "name": None,
                "flags": flags,
                "flag_names": _flags(flags, SECTION_FLAGS),
                "unknown_flag_bits": _unknown_flag_bits(flags, SECTION_FLAGS),
                "virtual_address": virtual_address,
                "virtual_size": virtual_size,
                "virtual_end": virtual_address + virtual_size,
                "raw_address": raw_address,
                "raw_size": raw_size,
                "raw_end": raw_address + raw_size,
                "zero_fill_size": max(virtual_size - raw_size, 0),
                "raw_overflow_size": max(raw_size - virtual_size, 0),
                "section_name_address": section_name_address,
                "section_name_ref_count": section_name_ref_count,
                "head_shared_page_ref_count_address": head_shared_page_ref_count_address,
                "tail_shared_page_ref_count_address": tail_shared_page_ref_count_address,
                "digest_sha1": digest,
            }
        )

    for section in sections:
        try:
            name_mapping = _map_virtual_address(
                section["section_name_address"], header, sections
            )
            section["name"] = _read_c_string(data, name_mapping["file_offset"])
        except XbeFormatError:
            section["name"] = f"section_{section['index']}"

        section["digest_verification"] = _verify_section_digest(data, section)

    return sections


def _verify_section_digest(data: bytes, section: dict[str, Any]) -> dict[str, Any]:
    raw_address = section["raw_address"]
    raw_size = section["raw_size"]
    expected = section["digest_sha1"]

    if raw_size == 0:
        section_data = b""
    else:
        try:
            _require_range(data, raw_address, raw_size, f"section {section['index']} raw")
            section_data = data[raw_address : raw_address + raw_size]
        except XbeFormatError as exc:
            return {
                "algorithm": "sha1(length_le32 + raw_section)",
                "expected_sha1": expected,
                "actual_sha1": None,
                "matches": False,
                "error": str(exc),
            }

    actual = hashlib.sha1(
        struct.pack("<I", raw_size) + section_data, usedforsecurity=False
    ).hexdigest().upper()
    return {
        "algorithm": "sha1(length_le32 + raw_section)",
        "expected_sha1": expected,
        "actual_sha1": actual,
        "matches": actual == expected,
        "error": None,
    }


def _parse_certificate(
    data: bytes, header: dict[str, Any], sections: list[dict[str, Any]]
) -> dict[str, Any]:
    certificate_mapping = _map_virtual_address(
        header["certificate_address"], header, sections, CERTIFICATE_MIN_SIZE
    )
    certificate_offset = certificate_mapping["file_offset"]
    certificate_size = _u32(data, certificate_offset)
    _require_range(data, certificate_offset, certificate_size, "certificate")

    title_id = _u32(data, certificate_offset + 0x08)
    title_raw = data[
        certificate_offset
        + CERTIFICATE_TITLE_NAME_OFFSET : certificate_offset
        + CERTIFICATE_TITLE_NAME_OFFSET
        + CERTIFICATE_TITLE_NAME_SIZE
    ]
    alternate_title_ids = [
        _u32(data, certificate_offset + 0x5C + index * 4) for index in range(16)
    ]
    allowed_media = _u32(data, certificate_offset + 0x9C)
    game_region = _u32(data, certificate_offset + 0xA0)

    certificate = {
        "offset": certificate_offset,
        "virtual_address": header["certificate_address"],
        "size": certificate_size,
        "timestamp": _u32(data, certificate_offset + 0x04),
        "timestamp_utc": _timestamp_to_iso8601(_u32(data, certificate_offset + 0x04)),
        "title_id": _title_id_details(title_id),
        "title_name": _decode_fixed_utf16le(title_raw),
        "alternate_title_ids": [
            _title_id_details(value) for value in alternate_title_ids if value != 0
        ],
        "allowed_media": allowed_media,
        "allowed_media_names": _flags(allowed_media, MEDIA_FLAGS),
        "allowed_media_unknown_bits": _unknown_flag_bits(allowed_media, MEDIA_FLAGS),
        "game_region": game_region,
        "game_region_names": _flags(game_region, REGION_FLAGS),
        "game_region_unknown_bits": _unknown_flag_bits(game_region, REGION_FLAGS),
        "ratings": _u32(data, certificate_offset + 0xA4),
        "disc_number": _u32(data, certificate_offset + 0xA8),
        "version": _u32(data, certificate_offset + 0xAC),
        "has_lan_key": any(data[certificate_offset + 0xB0 : certificate_offset + 0xC0]),
        "has_signature_key": any(
            data[certificate_offset + 0xC0 : certificate_offset + 0xD0]
        ),
        "extended": None,
    }

    if certificate_size >= CERTIFICATE_EXTENDED_SIZE:
        certificate["extended"] = {
            "original_certificate_size": _u32(data, certificate_offset + 0x1D0),
            "online_service_id": _u32(data, certificate_offset + 0x1D4),
            "security_flags": _u32(data, certificate_offset + 0x1D8),
            "has_code_encryption_key": any(
                data[certificate_offset + 0x1DC : certificate_offset + 0x1EC]
            ),
        }

    return certificate


def _parse_library_version(data: bytes, offset: int, label: str) -> dict[str, Any]:
    _require_range(data, offset, LIBRARY_VERSION_SIZE, label)
    flags = _u16(data, offset + 0x0E)
    return {
        "name": _decode_fixed_ascii(data[offset : offset + 0x08]),
        "version_major": _u16(data, offset + 0x08),
        "version_minor": _u16(data, offset + 0x0A),
        "version_build": _u16(data, offset + 0x0C),
        "flags": flags,
        "qfe_version": flags & 0x1FFF,
        "approved": (flags >> 13) & 0x03,
        "debug_build": bool(flags & 0x8000),
    }


def _parse_library_versions(
    data: bytes, header: dict[str, Any], sections: list[dict[str, Any]]
) -> dict[str, Any]:
    versions = []
    if header["library_versions_count"] and header["library_versions_address"]:
        mapping = _map_virtual_address(
            header["library_versions_address"],
            header,
            sections,
            header["library_versions_count"] * LIBRARY_VERSION_SIZE,
        )
        for index in range(header["library_versions_count"]):
            versions.append(
                _parse_library_version(
                    data,
                    mapping["file_offset"] + index * LIBRARY_VERSION_SIZE,
                    f"library version {index}",
                )
                | {"index": index}
            )

    named_versions = {}
    for key, address in (
        ("kernel", header["kernel_library_version_address"]),
        ("xapi", header["xapi_library_version_address"]),
    ):
        if not address:
            named_versions[key] = None
            continue
        try:
            mapping = _map_virtual_address(address, header, sections, LIBRARY_VERSION_SIZE)
            named_versions[key] = _parse_library_version(
                data, mapping["file_offset"], f"{key} library version"
            ) | {"virtual_address": address}
        except XbeFormatError as exc:
            named_versions[key] = {"error": str(exc), "virtual_address": address}

    return {"versions": versions, **named_versions}


def _parse_library_features(
    data: bytes, header: dict[str, Any], sections: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    count = header["library_features_count"]
    address = header["library_features_address"]
    if not count or not address:
        return []

    mapping = _map_virtual_address(address, header, sections, count * LIBRARY_VERSION_SIZE)
    features = []
    for index in range(count):
        offset = mapping["file_offset"] + index * LIBRARY_VERSION_SIZE
        _require_range(data, offset, LIBRARY_VERSION_SIZE, f"library feature {index}")
        features.append(
            {
                "index": index,
                "name": _decode_fixed_ascii(data[offset : offset + 0x08]),
                "version_major": _u16(data, offset + 0x08),
                "version_minor": _u16(data, offset + 0x0A),
                "version_build": _u16(data, offset + 0x0C),
                "flags": _u16(data, offset + 0x0E),
            }
        )
    return features


def _parse_tls(
    data: bytes, header: dict[str, Any], sections: list[dict[str, Any]]
) -> dict[str, Any] | None:
    address = header["tls_address"]
    if not address:
        return None

    mapping = _map_virtual_address(address, header, sections, TLS_HEADER_SIZE)
    offset = mapping["file_offset"]
    callback_table_address = _u32(data, offset + 0x0C)
    tls = {
        "virtual_address": address,
        "file_offset": offset,
        "raw_data_start_address": _u32(data, offset + 0x00),
        "raw_data_end_address": _u32(data, offset + 0x04),
        "tls_index_address": _u32(data, offset + 0x08),
        "callback_table_address": callback_table_address,
        "zero_fill_size": _u32(data, offset + 0x10),
        "characteristics": _u32(data, offset + 0x14),
        "callback_addresses": [],
        "callback_parse_error": None,
    }

    if callback_table_address:
        try:
            tls["callback_addresses"] = [
                entry["value"]
                for entry in _read_terminated_u32_table(
                    data, header, sections, callback_table_address
                )
            ]
        except XbeFormatError as exc:
            tls["callback_parse_error"] = str(exc)
    return tls


def _read_terminated_u32_table(
    data: bytes,
    header: dict[str, Any],
    sections: list[dict[str, Any]],
    address: int,
    *,
    max_entries: int = TERMINATED_TABLE_MAX_ENTRIES,
) -> list[dict[str, Any]]:
    entries = []
    cursor = address
    for index in range(max_entries):
        mapping = _map_virtual_address(cursor, header, sections, 4)
        value = _u32(data, mapping["file_offset"])
        if value == 0:
            return entries
        entries.append(
            {
                "index": index,
                "address": cursor,
                "address_hex": _hex32(cursor),
                "file_offset": mapping["file_offset"],
                "value": value,
                "value_hex": _hex32(value),
            }
        )
        cursor += 4
    raise XbeFormatError(
        f"unterminated u32 table at 0x{address:08X}; exceeded {max_entries} entries"
    )


def _parse_kernel_imports(
    data: bytes, header: dict[str, Any], sections: list[dict[str, Any]]
) -> dict[str, Any]:
    selected = header["kernel_thunk_address"]["selected"]
    if not selected:
        return {
            "table_address": None,
            "count": 0,
            "imports": [],
            "error": "encoded kernel thunk address did not decode inside the image",
        }

    table_address = selected["value"]
    table = _read_terminated_u32_table(data, header, sections, table_address)
    imports = []
    for entry in table:
        ordinal = entry["value"] & 0x1FF
        imports.append(
            {
                "index": entry["index"],
                "thunk_address": entry["address"],
                "thunk_address_hex": entry["address_hex"],
                "raw_value": entry["value"],
                "raw_value_hex": entry["value_hex"],
                "ordinal": ordinal,
                "name": KERNEL_EXPORT_NAMES.get(ordinal),
            }
        )
    return {
        "table_address": table_address,
        "table_address_hex": _hex32(table_address),
        "count": len(imports),
        "imports": imports,
        "error": None,
    }


def _parse_non_kernel_imports(
    data: bytes, header: dict[str, Any], sections: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    address = header["non_kernel_import_directory_address"]
    if not address:
        return []

    imports = []
    cursor = address
    for index in range(TERMINATED_TABLE_MAX_ENTRIES):
        mapping = _map_virtual_address(cursor, header, sections, IMPORT_DESCRIPTOR_SIZE)
        offset = mapping["file_offset"]
        thunk_array_address = _u32(data, offset + 0x00)
        image_name_address = _u32(data, offset + 0x04)
        if thunk_array_address == 0 or image_name_address == 0:
            return imports

        name_mapping = _map_virtual_address(image_name_address, header, sections, 2)
        image_name = _read_wide_string(data, name_mapping["file_offset"])
        thunk_table = _read_terminated_u32_table(
            data, header, sections, thunk_array_address
        )
        thunks = [
            {
                "index": entry["index"],
                "thunk_address": entry["address"],
                "thunk_address_hex": entry["address_hex"],
                "raw_value": entry["value"],
                "raw_value_hex": entry["value_hex"],
                "ordinal": entry["value"] & 0x7FFFFFFF,
            }
            for entry in thunk_table
        ]
        imports.append(
            {
                "index": index,
                "descriptor_address": cursor,
                "descriptor_address_hex": _hex32(cursor),
                "image_name": image_name,
                "image_name_address": image_name_address,
                "thunk_array_address": thunk_array_address,
                "thunk_count": len(thunks),
                "thunks": thunks,
            }
        )
        cursor += IMPORT_DESCRIPTOR_SIZE

    raise XbeFormatError(
        f"unterminated import descriptor table at 0x{address:08X}; "
        f"exceeded {TERMINATED_TABLE_MAX_ENTRIES} descriptors"
    )


def _read_optional_strings(
    data: bytes, header: dict[str, Any], sections: list[dict[str, Any]]
) -> dict[str, Any]:
    strings: dict[str, Any] = {}
    fields = (
        ("debug_pathname", "debug_pathname_address", _read_c_string, 1),
        ("debug_filename", "debug_filename_address", _read_c_string, 1),
        ("debug_unicode_filename", "debug_unicode_filename_address", _read_wide_string, 2),
    )
    for output_key, address_key, reader, size in fields:
        address = header[address_key]
        if not address:
            strings[output_key] = None
            continue
        try:
            mapping = _map_virtual_address(address, header, sections, size)
            strings[output_key] = reader(data, mapping["file_offset"])
        except XbeFormatError as exc:
            strings[output_key] = {"error": str(exc), "virtual_address": address}
    return strings


def _build_memory_map(
    header: dict[str, Any], sections: list[dict[str, Any]]
) -> dict[str, Any]:
    regions = [
        {
            "kind": "headers",
            "name": "$headers",
            "index": None,
            "flags": 0,
            "flag_names": [],
            "virtual_address": header["base_address"],
            "virtual_size": header["headers_size"],
            "virtual_end": header["base_address"] + header["headers_size"],
            "raw_address": 0,
            "raw_size": header["headers_size"],
            "raw_end": header["headers_size"],
            "file_backed_size": header["headers_size"],
            "zero_fill_size": 0,
            "raw_overflow_size": 0,
            "virtual_range": _range(header["base_address"], header["headers_size"]),
            "raw_range": _range(0, header["headers_size"]),
        }
    ]

    for section in sections:
        regions.append(
            {
                "kind": "section",
                "name": section["name"],
                "index": section["index"],
                "flags": section["flags"],
                "flag_names": section["flag_names"],
                "virtual_address": section["virtual_address"],
                "virtual_size": section["virtual_size"],
                "virtual_end": section["virtual_end"],
                "raw_address": section["raw_address"],
                "raw_size": section["raw_size"],
                "raw_end": section["raw_end"],
                "file_backed_size": min(section["raw_size"], section["virtual_size"]),
                "zero_fill_size": section["zero_fill_size"],
                "raw_overflow_size": section["raw_overflow_size"],
                "virtual_range": _range(
                    section["virtual_address"], section["virtual_size"]
                ),
                "raw_range": _range(section["raw_address"], section["raw_size"]),
            }
        )

    regions.sort(key=lambda item: (item["virtual_address"], item["index"] or -1))
    gaps = []
    image_start = header["base_address"]
    image_end = image_start + header["image_size"]
    cursor = image_start
    for region in regions:
        if region["virtual_address"] > cursor:
            gaps.append(_range(cursor, region["virtual_address"] - cursor))
        cursor = max(cursor, region["virtual_end"])
    if cursor < image_end:
        gaps.append(_range(cursor, image_end - cursor))

    return {
        "base_address": header["base_address"],
        "image_size": header["image_size"],
        "image_end": image_end,
        "base_address_hex": _hex32(header["base_address"]),
        "image_end_hex": _hex32(image_end),
        "regions": regions,
        "gaps": gaps,
        "zero_fill_total": sum(region["zero_fill_size"] for region in regions),
        "raw_overflow_total": sum(region["raw_overflow_size"] for region in regions),
    }


def parse_xbe_bytes(data: bytes) -> dict[str, Any]:
    """Parse XBE metadata without exposing executable bytes."""

    if len(data) < HEADER_MIN_SIZE:
        raise XbeFormatError(
            f"file is too small for an XBE header: {len(data)} bytes"
        )
    if data[:4] != MAGIC:
        raise XbeFormatError("missing XBEH magic")

    header = _parse_header(data)
    _validate_header(data, header)
    sections = _parse_section_headers(data, header)

    certificate = _parse_certificate(data, header, sections)
    libraries = _parse_library_versions(data, header, sections)
    library_features = _parse_library_features(data, header, sections)
    tls = _parse_tls(data, header, sections)
    kernel_imports = _parse_kernel_imports(data, header, sections)
    non_kernel_imports = _parse_non_kernel_imports(data, header, sections)
    debug_strings = _read_optional_strings(data, header, sections)
    memory_map = _build_memory_map(header, sections)

    return {
        "format": "xbe",
        "header": header,
        "certificate": certificate,
        "debug_strings": debug_strings,
        "memory_map": memory_map,
        "sections": sections,
        "tls": tls,
        "libraries": libraries,
        "library_features": library_features,
        "kernel_imports": kernel_imports,
        "non_kernel_imports": non_kernel_imports,
    }


def parse_xbe_file(path: Path) -> dict[str, Any]:
    return parse_xbe_bytes(path.read_bytes())


def main() -> int:
    parser = argparse.ArgumentParser(description="Emit metadata for an Xbox XBE file.")
    parser.add_argument("xbe", type=Path, help="Path to the XBE file to inspect.")
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print JSON output.",
    )
    parser.add_argument(
        "--memory-map",
        action="store_true",
        help="Emit only the loader-oriented memory map.",
    )
    args = parser.parse_args()

    info = parse_xbe_file(args.xbe)
    output = info["memory_map"] if args.memory_map else info
    print(json.dumps(output, indent=2 if args.pretty else None, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
