"""Cached native execution for resumable lifted x86 functions."""

from __future__ import annotations

import ctypes
import hashlib
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from collections import deque
from pathlib import Path
from typing import Any, Callable, Iterable

from tools.recomp.x86_lifter import (
    CpuFlags,
    CpuState,
    ExecutionTrace,
    LiftedFunction,
    SparseMemory,
    emit_cpp,
)


class NativeExecutorError(RuntimeError):
    pass


def _partition_instructions_by_address(
    instructions: list[Any],
    *,
    maximum_count: int,
    initial_span: int = 0x10000,
) -> list[list[Any]]:
    """Build stable address-range modules that do not shift after frontier growth."""
    if maximum_count <= 0 or initial_span <= 0:
        raise ValueError("native module partition limits must be positive")

    def partition(items: list[Any], range_start: int, span: int) -> list[list[Any]]:
        if len(items) <= maximum_count:
            return [items]
        if span <= 1:
            return [
                items[index : index + maximum_count]
                for index in range(0, len(items), maximum_count)
            ]
        half = max(1, span // 2)
        split = range_start + half
        lower = [instruction for instruction in items if instruction.address < split]
        upper = [instruction for instruction in items if instruction.address >= split]
        result: list[list[Any]] = []
        if lower:
            result.extend(partition(lower, range_start, half))
        if upper:
            result.extend(partition(upper, split, span - half))
        return result

    buckets: dict[int, list[Any]] = {}
    for instruction in sorted(instructions, key=lambda item: item.address):
        bucket_start = instruction.address // initial_span * initial_span
        buckets.setdefault(bucket_start, []).append(instruction)
    modules: list[list[Any]] = []
    for bucket_start, items in sorted(buckets.items()):
        modules.extend(partition(items, bucket_start, initial_span))
    return modules


class _Flags(ctypes.Structure):
    _fields_ = [(name, ctypes.c_bool) for name in ("cf", "pf", "af", "zf", "sf", "of")]


class _Xmm(ctypes.Structure):
    _fields_ = [("lane", ctypes.c_float * 4)]


class _Context(ctypes.Structure):
    pass


_ReadU32 = ctypes.CFUNCTYPE(ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32)
_WriteU32 = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32)
_ReadU8 = ctypes.CFUNCTYPE(ctypes.c_uint8, ctypes.c_void_p, ctypes.c_uint32)
_WriteU8 = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint8)
_Call = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(_Context))
_Observe = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.POINTER(_Context))

_Context._fields_ = [
    *[(name, ctypes.c_uint32) for name in ("eax", "ecx", "edx", "ebx", "esp", "ebp", "esi", "edi", "fs_base")],
    ("timestamp_counter", ctypes.c_uint64),
    ("mxcsr", ctypes.c_uint32),
    ("fpu_status_word", ctypes.c_uint32),
    ("fpu_stack", ctypes.c_float * 8),
    ("fpu_depth", ctypes.c_uint32),
    ("xmm", _Xmm * 8),
    ("mmx", ctypes.c_uint64 * 8),
    ("flags", _Flags),
    ("eip", ctypes.c_uint32),
    ("steps", ctypes.c_uint64),
    ("step_budget", ctypes.c_uint64),
    ("yield_requested", ctypes.c_bool),
    ("user", ctypes.c_void_p),
    ("read_u32", _ReadU32),
    ("write_u32", _WriteU32),
    ("read_u8", _ReadU8),
    ("write_u8", _WriteU8),
    ("call", _Call),
    ("observe", _Observe),
    ("read_pages", ctypes.POINTER(ctypes.c_void_p)),
    ("callback_pages", ctypes.POINTER(ctypes.c_uint8)),
    ("dirty_pages", ctypes.POINTER(ctypes.c_uint8)),
]


class NativeResumableExecutor:
    """Compile a lifted frame once, then resume it across host-service boundaries."""

    SYMBOL = "b2r_native_loop"
    MAX_INSTRUCTIONS_PER_MODULE = 3000

    def __init__(
        self,
        function: LiftedFunction,
        *,
        build_dir: Path,
        observer_addresses: Iterable[int] = (),
        callback_addresses: Iterable[int] = (),
        memory_callback_addresses: Iterable[int] = (),
        compiler: str | None = None,
    ) -> None:
        compile_profile = "clang-cl-o1-address-stable-v2"
        build_dir.mkdir(parents=True, exist_ok=True)
        compiler_path = compiler or shutil.which("clang-cl")
        if compiler_path is None:
            raise NativeExecutorError("clang-cl is required to build the native guest loop")
        instructions = list(function.instructions)
        callback_addresses = {int(address) for address in callback_addresses}
        memory_callback_addresses = {
            int(address) for address in memory_callback_addresses
        }
        chunks = _partition_instructions_by_address(
            instructions,
            maximum_count=self.MAX_INSTRUCTIONS_PER_MODULE,
        ) or [[]]
        modules: list[tuple[LiftedFunction, str, Path, Path]] = []
        for chunk in chunks:
            module_identity = (
                f"{chunk[0].address:08X}_{chunk[-1].address:08X}"
                if chunk
                else f"{function.base_address:08X}_empty"
            )
            module = LiftedFunction(
                symbol=f"{function.symbol}_native_{module_identity}",
                base_address=chunk[0].address if chunk else function.base_address,
                code_size=(chunk[-1].next_address - chunk[0].address) if chunk else 0,
                instructions=tuple(chunk),
            )
            source = emit_cpp(
                module,
                exported_symbol=self.SYMBOL,
                resumable=True,
                observer_addresses=observer_addresses,
                callback_addresses=callback_addresses
                & {instruction.address for instruction in module.instructions},
            )
            digest = hashlib.sha256(
                (compile_profile + "\n" + source).encode("utf-8")
            ).hexdigest()[:16]
            source_path = build_dir / f"native-loop-{digest}.cpp"
            dll_path = build_dir / f"native-loop-{digest}.dll"
            modules.append((module, source, source_path, dll_path))

        def compile_module(item: tuple[LiftedFunction, str, Path, Path]) -> None:
            _module, source, source_path, dll_path = item
            if dll_path.exists():
                return
            source_path.write_text(source, encoding="utf-8", newline="\n")
            completed = subprocess.run(
                [
                    compiler_path,
                    "/nologo",
                    "/std:c++17",
                    "/O1",
                    "/LD",
                    str(source_path),
                    f"/Fe:{dll_path}",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if completed.returncode != 0:
                raise NativeExecutorError(
                    "native guest-loop compilation failed:\n"
                    + (completed.stdout + completed.stderr).strip()
                )
        with ThreadPoolExecutor(max_workers=min(4, len(modules))) as pool:
            list(pool.map(compile_module, modules))

        self.dll_path = modules[0][3]
        self._base_address = function.base_address
        self._valid_addresses = {instruction.address for instruction in function.instructions}
        # Execution callback targets yield through generated control flow; they
        # do not make every data access sharing the target's 4 KiB code page
        # volatile. Only addresses whose memory semantics change on reads must
        # bypass the native page cache.
        self._callback_pages = {
            int(address) >> 12 for address in memory_callback_addresses
        }
        self._libraries: list[ctypes.CDLL] = []
        self._entries_by_address: dict[int, Callable] = {}
        self.last_run_summary: dict[str, Any] | None = None
        for module, _source, _source_path, dll_path in modules:
            library = ctypes.CDLL(str(dll_path))
            entry = getattr(library, self.SYMBOL)
            entry.argtypes = [ctypes.POINTER(_Context)]
            entry.restype = ctypes.c_uint32
            self._libraries.append(library)
            for instruction in module.instructions:
                self._entries_by_address[instruction.address] = entry

    def run(
        self,
        state: CpuState,
        memory: SparseMemory,
        *,
        call_handlers: dict[int, Callable] | None = None,
        step_observer: Callable | None = None,
        max_steps: int = 0,
        max_memory_pages: int = 65536,
        slice_steps: int = 0,
        yield_handler: Callable[[CpuState, SparseMemory, int], bool | None] | None = None,
        yield_predicate: Callable[[], bool] | None = None,
    ) -> int:
        """Run until the step budget, an unhandled target, or callback exception."""
        handlers = call_handlers or {}
        trace = ExecutionTrace(enabled=False)
        context = _context_from_state(state)
        if context.eip == 0:
            context.eip = self._base_address
        callback_error: list[BaseException] = []
        page_table = (ctypes.c_void_p * (1 << 20))()
        callback_pages = (ctypes.c_uint8 * (1 << 20))()
        dirty_pages = (ctypes.c_uint8 * (1 << 20))()
        page_buffers: dict[int, ctypes.Array] = {}
        page_generations: dict[int, int] = {}
        for page in self._callback_pages:
            callback_pages[page] = 1

        def cache_page(address: int) -> None:
            if address >= 0x80000000:
                return
            page = address >> 12
            if callback_pages[page] or page in page_buffers:
                return
            page_address = page << 12
            page_snapshot = getattr(memory, "native_page_snapshot", None)
            if callable(page_snapshot):
                payload = page_snapshot(page_address)
            else:
                payload = SparseMemory.read(memory, page_address, 4096)
            buffer = (ctypes.c_uint8 * 4096).from_buffer_copy(payload)
            page_buffers[page] = buffer
            page_table[page] = ctypes.addressof(buffer)
            page_generations[page] = memory.page_generation(address)

        def invalidate_changed_pages() -> None:
            changed = [
                page
                for page, generation in page_generations.items()
                if memory.page_generation(page << 12) != generation
            ]
            for page in changed:
                page_table[page] = None
                page_buffers.pop(page, None)
                page_generations.pop(page, None)

        def sync_dirty_pages() -> None:
            for page, buffer in page_buffers.items():
                if not dirty_pages[page]:
                    continue
                memory.write(page << 12, bytes(buffer))
                dirty_pages[page] = 0
                page_generations[page] = memory.page_generation(page << 12)

        def guard(callback: Callable, fallback: int | None = None) -> Callable:
            def wrapped(*args):
                try:
                    return callback(*args)
                except BaseException as exc:  # propagate safely after native code yields
                    callback_error.append(exc)
                    context.yield_requested = True
                    return fallback
            return wrapped

        def read32(_user, address: int) -> int:
            value = memory.read_u32(address)
            cache_page(address)
            return value

        def read8(_user, address: int) -> int:
            value = memory.read(address, 1)[0]
            cache_page(address)
            return value

        read_u32 = _ReadU32(guard(read32, 0))
        def check_write(address: int) -> None:
            if (
                max_memory_pages
                and memory.allocated_page_count >= max_memory_pages
                and not memory.has_allocated_page(address)
            ):
                raise NativeExecutorError(
                    f"native guest loop exceeded {max_memory_pages} memory pages "
                    f"while writing 0x{address:08X} at eip 0x{context.eip:08X}"
                )

        def write32(_user, address: int, value: int) -> None:
            check_write(address)
            page = address >> 12
            if address < 0x80000000 and (address & 0xFFF) <= 0xFFC:
                cache_page(address)
            if page in page_buffers and (address & 0xFFF) <= 0xFFC:
                ctypes.c_uint32.from_address(
                    int(page_table[page]) + (address & 0xFFF)
                ).value = value
                dirty_pages[page] = 1
                return
            memory.write_u32(address, value)
            if yield_predicate is not None and yield_predicate():
                context.yield_requested = True

        def write8(_user, address: int, value: int) -> None:
            check_write(address)
            page = address >> 12
            if address < 0x80000000:
                cache_page(address)
            if page in page_buffers:
                ctypes.c_uint8.from_address(
                    int(page_table[page]) + (address & 0xFFF)
                ).value = value
                dirty_pages[page] = 1
                return
            memory.write(address, bytes((value,)))
            if yield_predicate is not None and yield_predicate():
                context.yield_requested = True

        write_u32 = _WriteU32(guard(write32))
        read_u8 = _ReadU8(guard(read8, 0))
        write_u8 = _WriteU8(guard(write8))
        unused_call = _Call(lambda _user, _target, _context: None)

        def observe(_user, context_pointer) -> None:
            if step_observer is None:
                return
            sync_dirty_pages()
            _state_from_context(state, context_pointer.contents)
            step_observer(state, memory, trace, int(context_pointer.contents.steps))
            _update_context(context_pointer.contents, state)
            invalidate_changed_pages()

        observe_callback = _Observe(guard(observe))
        context.read_u32 = read_u32
        context.write_u32 = write_u32
        context.read_u8 = read_u8
        context.write_u8 = write_u8
        context.call = unused_call
        context.observe = observe_callback
        context.read_pages = ctypes.cast(page_table, ctypes.POINTER(ctypes.c_void_p))
        context.callback_pages = ctypes.cast(
            callback_pages, ctypes.POINTER(ctypes.c_uint8)
        )
        context.dirty_pages = ctypes.cast(dirty_pages, ctypes.POINTER(ctypes.c_uint8))
        if slice_steps < 0:
            raise NativeExecutorError("slice_steps must not be negative")
        next_slice = slice_steps if slice_steps else 0
        context.step_budget = (
            min(max_steps, next_slice) if max_steps and next_slice else max_steps or next_slice
        )
        transitions: deque[dict[str, int]] = deque(maxlen=32)

        def finish(reason: str, target: int, dispatch_eip: int) -> int:
            _state_from_context(state, context)
            stack_pointer = int(context.esp)
            self.last_run_summary = {
                "reason": reason,
                "target": target,
                "target_hex": f"0x{target:08X}",
                "dispatch_eip": dispatch_eip,
                "dispatch_eip_hex": f"0x{dispatch_eip:08X}",
                "steps": int(context.steps),
                "step_budget": int(context.step_budget),
                "registers": {
                    name: f"0x{int(getattr(context, name)):08X}"
                    for name in ("eax", "ecx", "edx", "ebx", "esp", "ebp", "esi", "edi")
                },
                "stack_words": [
                    {
                        "address": (stack_pointer + offset) & 0xFFFFFFFF,
                        "address_hex": f"0x{(stack_pointer + offset) & 0xFFFFFFFF:08X}",
                        "value": memory.read_u32((stack_pointer + offset) & 0xFFFFFFFF),
                        "value_hex": f"0x{memory.read_u32((stack_pointer + offset) & 0xFFFFFFFF):08X}",
                    }
                    for offset in range(0, 64, 4)
                ],
                "transitions": list(transitions),
            }
            return target

        while True:
            dispatch_eip = int(context.eip)
            entry = self._entries_by_address.get(dispatch_eip)
            if entry is None:
                target = dispatch_eip
            else:
                target = int(entry(ctypes.byref(context)))
            transitions.append(
                {
                    "dispatch_eip": dispatch_eip,
                    "target": target,
                    "steps": int(context.steps),
                }
            )
            if callback_error:
                sync_dirty_pages()
                _state_from_context(state, context)
                raise callback_error[0]
            handled_requested_yield = False
            if context.yield_requested:
                sync_dirty_pages()
                _state_from_context(state, context)
                context.yield_requested = False
                if yield_handler is not None and yield_handler(
                    state, memory, int(context.steps)
                ) is False:
                    return finish("yield_handler_stop", int(context.eip), dispatch_eip)
                _update_context(context, state)
                invalidate_changed_pages()
                handled_requested_yield = True
            if next_slice and context.steps >= next_slice:
                if not handled_requested_yield:
                    sync_dirty_pages()
                    _state_from_context(state, context)
                    if yield_handler is not None and yield_handler(
                        state, memory, int(context.steps)
                    ) is False:
                        return finish("yield_handler_stop", int(context.eip), dispatch_eip)
                    _update_context(context, state)
                    invalidate_changed_pages()
                if max_steps and context.steps >= max_steps:
                    return finish("step_budget", target, dispatch_eip)
                while next_slice <= context.steps:
                    next_slice += slice_steps
                context.step_budget = min(max_steps, next_slice) if max_steps else next_slice
            if max_steps and context.steps >= max_steps:
                sync_dirty_pages()
                return finish("step_budget", target, dispatch_eip)
            handler = handlers.get(target)
            if handler is None:
                if target in self._valid_addresses:
                    continue
                sync_dirty_pages()
                return finish("unhandled_target", target, dispatch_eip)
            sync_dirty_pages()
            _state_from_context(state, context)
            handler(state, memory, target, trace)
            state.eip = memory.read_u32(state.get_register("esp"))
            state.set_register("esp", state.get_register("esp") + 4)
            _update_context(context, state)
            invalidate_changed_pages()


def _context_from_state(state: CpuState) -> _Context:
    context = _Context()
    _update_context(context, state)
    return context


def _update_context(context: _Context, state: CpuState) -> None:
    for name in ("eax", "ecx", "edx", "ebx", "esp", "ebp", "esi", "edi"):
        setattr(context, name, state.get_register(name))
    context.fs_base = state.fs_base
    context.timestamp_counter = state.timestamp_counter
    context.mxcsr = state.mxcsr
    context.fpu_status_word = state.fpu_status_word
    context.fpu_depth = min(len(state.fpu_stack), 8)
    for index in range(8):
        context.fpu_stack[index] = state.fpu_stack[index] if index < len(state.fpu_stack) else 0.0
        lanes = state.get_xmm_register(f"xmm{index}")
        for lane in range(4):
            context.xmm[index].lane[lane] = lanes[lane]
        context.mmx[index] = state.get_mmx_register(f"mm{index}")
    context.flags = _Flags(**state.flags.to_dict())
    context.eip = state.eip


def _state_from_context(state: CpuState, context: _Context) -> None:
    for name in ("eax", "ecx", "edx", "ebx", "esp", "ebp", "esi", "edi"):
        state.set_register(name, getattr(context, name))
    state.fs_base = context.fs_base
    state.timestamp_counter = context.timestamp_counter
    state.mxcsr = context.mxcsr
    state.fpu_status_word = context.fpu_status_word
    state.fpu_stack = [float(context.fpu_stack[index]) for index in range(context.fpu_depth)]
    for index in range(8):
        state.set_xmm_register(f"xmm{index}", context.xmm[index].lane)
        state.set_mmx_register(f"mm{index}", context.mmx[index])
    state.flags = CpuFlags(**{name: bool(getattr(context.flags, name)) for name in ("cf", "pf", "af", "zf", "sf", "of")})
    state.eip = context.eip
