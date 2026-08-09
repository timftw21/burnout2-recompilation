from __future__ import annotations

import os
import tempfile
import struct
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tools.host import first_frame_smoke
from tools.host.first_frame_smoke import (
    DEFAULT_PRESENTER_MODULES,
    DEFAULT_SOURCE,
    FirstFrameSmokeError,
    Toolchain,
    build_command,
    compile_shaders,
    inspect_bmp,
    read_debug_events,
    run_first_frame,
    summarize_smoke,
    validate_presenter_build_manifest,
    write_presenter_build_manifest,
)


PRESENTER_SOURCE_PATHS = (
    Path("runtime/host/command_work_cache.h"),
    Path("runtime/nv2a/raster_coordinates.h"),
    Path("runtime/host/nv2a_command_processor.cpp"),
    Path("runtime/host/vulkan_presenter.cpp"),
    Path("runtime/host/vulkan_renderer.cpp"),
    Path("runtime/host/vulkan_resources.cpp"),
    Path("runtime/host/presenter_diagnostics.cpp"),
    Path("runtime/host/vulkan_presenter_internal.h"),
    Path("runtime/host/vulkan_presenter_runtime.h"),
    Path("runtime/host/vulkan_first_frame.cpp"),
    Path("runtime/host/live_presenter_transport.cpp"),
    Path("runtime/host/presenter_debug_log.cpp"),
    Path("runtime/host/presenter_metrics.cpp"),
    Path("runtime/host/presenter_options.cpp"),
    Path("runtime/host/presenter_options.h"),
    Path("runtime/platform/sdl/sdl_audio.cpp"),
    Path("runtime/platform/sdl/sdl_audio_c_api.cpp"),
    Path("runtime/platform/sdl/sdl_platform.cpp"),
)


def presenter_source_text() -> str:
    source = "\n".join(path.read_text(encoding="utf-8") for path in PRESENTER_SOURCE_PATHS)
    # Source-contract tests predate the out-of-class presenter method split.
    # Normalize the class qualifier so their function-boundary probes continue
    # to inspect implementations instead of matching later declarations.
    return source.replace("VulkanPresenter::", "")


class FirstFrameSmokeTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "explicit FreeLibrary is Windows-only")
    def test_embedded_presenter_unloads_library_before_return(self) -> None:
        events: list[str] = []
        presenter = Mock()
        presenter._handle = 0x1234
        presenter.b2r_presenter_main.side_effect = lambda *_args: events.append("entry") or 7
        dll_directory = Mock()
        dll_directory.close.side_effect = lambda: events.append("directory_close")

        with tempfile.TemporaryDirectory() as temp_dir:
            library = Path(temp_dir) / "b2_presenter.dll"
            library.write_bytes(b"test")
            with (
                patch.object(
                    first_frame_smoke.os,
                    "add_dll_directory",
                    return_value=dll_directory,
                ),
                patch.object(
                    first_frame_smoke.ctypes,
                    "CDLL",
                    return_value=presenter,
                ),
                patch.object(
                    first_frame_smoke._ctypes,
                    "FreeLibrary",
                    side_effect=lambda _handle: events.append("free"),
                ) as free_library,
            ):
                result = first_frame_smoke.run_embedded_presenter(
                    [str(library)],
                    library=library,
                )

        self.assertEqual(result, 7)
        free_library.assert_called_once_with(0x1234)
        self.assertEqual(events, ["entry", "free", "directory_close"])

    def test_default_presenter_build_compiles_every_native_module(self) -> None:
        toolchain = Toolchain(
            clangxx=Path("clang++.exe"),
            vulkan_sdk=Path("C:/VulkanSDK/test"),
        )

        command = build_command(
            source=DEFAULT_SOURCE,
            output=Path("build/local/first-frame/b2_first_frame.exe"),
            toolchain=toolchain,
        )

        for source in (DEFAULT_SOURCE, *DEFAULT_PRESENTER_MODULES):
            self.assertIn(str(source), command)

    def test_build_manifest_rejects_changed_presenter_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            sdk = root / "sdk"
            source = root / "presenter.cpp"
            executable = root / "presenter.exe"
            clangxx = root / "clang++.exe"
            manifest = root / "presenter.build.json"
            source.write_text("int main() { return 0; }\n", encoding="utf-8")
            executable.write_bytes(b"MZ-presenter")
            clangxx.write_bytes(b"clang")
            for path in (
                sdk / "Lib" / "SDL3.lib",
                sdk / "Lib" / "vulkan-1.lib",
                sdk / "Bin" / "SDL3.dll",
            ):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(path.name.encode("ascii"))
            (root / "SDL3.dll").write_bytes(b"SDL3.dll")
            toolchain = Toolchain(clangxx=clangxx, vulkan_sdk=sdk)
            with patch(
                "tools.host.first_frame_smoke._command_version",
                return_value="test compiler 1.0",
            ):
                write_presenter_build_manifest(
                    path=manifest,
                    source=source,
                    output=executable,
                    toolchain=toolchain,
                    include_shaders=False,
                )

            valid = validate_presenter_build_manifest(
                manifest,
                executable=executable,
                source=source,
                require_shaders=False,
            )
            source.write_text("int main() { return 1; }\n", encoding="utf-8")

            with self.assertRaisesRegex(FirstFrameSmokeError, "source SHA-256 changed"):
                validate_presenter_build_manifest(
                    manifest,
                    executable=executable,
                    source=source,
                    require_shaders=False,
                )
            overridden = validate_presenter_build_manifest(
                manifest,
                executable=executable,
                source=source,
                require_shaders=False,
                allow_stale=True,
            )

        self.assertTrue(valid["valid"])
        self.assertFalse(overridden["valid"])
        self.assertTrue(overridden["override_used"])
        self.assertEqual(overridden["status"], "stale_override")

    def test_shader_compilation_skips_current_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            sdk = root / "sdk"
            glslc = sdk / "Bin" / "glslc.exe"
            glslc.parent.mkdir(parents=True)
            glslc.touch()
            sources = [root / name for name in ("a.vert", "b.frag", "c.comp")]
            outputs = [root / f"{source.name}.spv" for source in sources]
            for source in sources:
                source.write_text("#version 450\nvoid main() {}\n", encoding="utf-8")
            for output in outputs:
                output.write_bytes(struct.pack("<I", 0x07230203))
            compiler_time = glslc.stat().st_mtime + 2.0
            for output in outputs:
                os.utime(output, (compiler_time, compiler_time))

            with patch("tools.host.first_frame_smoke.subprocess.run") as run:
                result = compile_shaders(
                    toolchain=Toolchain(clangxx=root / "clang.exe", vulkan_sdk=sdk),
                    vertex_source=sources[0],
                    fragment_source=sources[1],
                    texture_convert_source=sources[2],
                    vertex_output=outputs[0],
                    fragment_output=outputs[1],
                    texture_convert_output=outputs[2],
                )

        run.assert_not_called()
        self.assertEqual(result["compiled_count"], 0)
        self.assertEqual(result["cache_hit_count"], 3)
        self.assertEqual(result["commands"], [])

    def test_shader_compilation_rebuilds_only_stale_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            sdk = root / "sdk"
            glslc = sdk / "Bin" / "glslc.exe"
            glslc.parent.mkdir(parents=True)
            glslc.touch()
            sources = [root / name for name in ("a.vert", "b.frag", "c.comp")]
            outputs = [root / f"{source.name}.spv" for source in sources]
            for source in sources:
                source.write_text("#version 450\nvoid main() {}\n", encoding="utf-8")
            for output in outputs[1:]:
                output.write_bytes(struct.pack("<I", 0x07230203))
                current_time = glslc.stat().st_mtime + 2.0
                os.utime(output, (current_time, current_time))
            completed = Mock(returncode=0, stdout="", stderr="")
            with patch(
                "tools.host.first_frame_smoke.subprocess.run",
                return_value=completed,
            ) as run:
                result = compile_shaders(
                    toolchain=Toolchain(clangxx=root / "clang.exe", vulkan_sdk=sdk),
                    vertex_source=sources[0],
                    fragment_source=sources[1],
                    texture_convert_source=sources[2],
                    vertex_output=outputs[0],
                    fragment_output=outputs[1],
                    texture_convert_output=outputs[2],
                )

        self.assertEqual(run.call_count, 1)
        self.assertEqual(result["compiled_count"], 1)
        self.assertEqual(result["cache_hit_count"], 2)
        self.assertEqual(result["commands"][0][-1], str(outputs[0]))

    def test_presenter_run_can_omit_diagnostic_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            executable = Path(temp_dir) / "presenter.exe"
            executable.touch()
            completed = Mock(returncode=0, stdout="", stderr="")
            with patch(
                "tools.host.first_frame_smoke.subprocess.run",
                return_value=completed,
            ) as run:
                result = run_first_frame(
                    executable=executable,
                    debug_json=None,
                    max_frames=0,
                    timeout_seconds=0,
                    screenshot=None,
                    hotkey_screenshot_directory=None,
                )

        command = run.call_args.args[0]
        self.assertNotIn("--debug-json", command)
        self.assertEqual(result["debug_json"], None)
        self.assertEqual(result["events"], [])

    def test_presenter_run_forwards_live_control_transport(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            executable = Path(temp_dir) / "presenter.exe"
            executable.touch()
            completed = Mock(returncode=0, stdout="", stderr="")
            with patch(
                "tools.host.first_frame_smoke.subprocess.run",
                return_value=completed,
            ) as run:
                run_first_frame(
                    executable=executable,
                    debug_json=None,
                    max_frames=0,
                    timeout_seconds=0,
                    screenshot=None,
                    hotkey_screenshot_directory=None,
                    live_control_transport="Local\\b2_recomp_live_test",
                )

        command = run.call_args.args[0]
        self.assertEqual(
            command[command.index("--live-control-transport") + 1],
            "Local\\b2_recomp_live_test",
        )

    def test_presenter_run_forwards_command_work_cache_trace(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            executable = root / "presenter.exe"
            trace = root / "diagnostics" / "cache-trace.jsonl"
            executable.touch()
            completed = Mock(returncode=0, stdout="", stderr="")
            with patch(
                "tools.host.first_frame_smoke.subprocess.run",
                return_value=completed,
            ) as run:
                run_first_frame(
                    executable=executable,
                    debug_json=None,
                    max_frames=0,
                    timeout_seconds=0,
                    screenshot=None,
                    hotkey_screenshot_directory=None,
                    command_work_cache_trace=trace,
                )

        command = run.call_args.args[0]
        self.assertEqual(
            command[command.index("--command-work-cache-trace") + 1],
            str(trace),
        )

    def test_presenter_uses_requested_title_and_window_icon(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            executable = root / "presenter.exe"
            icon = root / "window-icon.png"
            executable.touch()
            icon.touch()
            completed = Mock(returncode=0, stdout="", stderr="")
            with patch(
                "tools.host.first_frame_smoke.subprocess.run",
                return_value=completed,
            ) as run:
                run_first_frame(
                    executable=executable,
                    debug_json=None,
                    max_frames=0,
                    timeout_seconds=0,
                    screenshot=None,
                    hotkey_screenshot_directory=None,
                    window_icon=icon,
                )

        command = run.call_args.args[0]
        self.assertEqual(command[command.index("--window-icon") + 1], str(icon))
        host_source = presenter_source_text()
        self.assertIn('L"Burnout 2: Point of Impact"', host_source)
        self.assertIn("SDL_LoadSurface", host_source)
        self.assertIn("SDL_SetWindowIcon", host_source)

    def test_live_presenter_hot_reloads_render_and_publishes_controller_state(self) -> None:
        host_source = presenter_source_text()
        runner_source = Path("tools/host/first_frame_smoke.py").read_text(encoding="utf-8")
        self.assertIn("--live-render-stream-json", host_source)
        self.assertIn("live_render_stream_reloaded", host_source)
        self.assertIn("--controller-state-json", host_source)
        self.assertIn("controller_state_published", host_source)
        self.assertIn("kLiveControlMagic", host_source)
        self.assertIn("kLiveManifestMagic", host_source)
        self.assertIn("kLiveCommandMagic", host_source)
        self.assertIn("kLiveResourceMagic", host_source)
        self.assertIn("shared_memory_v1", host_source)
        self.assertIn("live_command_capture_bytes_", host_source)
        self.assertIn("current_live_resource_snapshot_bytes_", host_source)
        self.assertIn("if live_render_stream or analyze_render_stream", runner_source)
        self.assertIn("debug_json=None if args.no_diagnostics", runner_source)
        self.assertIn("if (!enabled_)", host_source)

    def test_live_presenter_consumes_native_ia32_pcm_publications(self) -> None:
        host_source = presenter_source_text()
        layout_source = Path("runtime/host/live_transport_layout.h").read_text(
            encoding="utf-8"
        )

        self.assertIn("kLiveAudioSequenceOffset = 8192", layout_source)
        self.assertIn("kLiveAudioPayloadOffset = 8224", layout_source)
        self.assertIn("bool LivePresenterTransport::read_audio_pcm", host_source)
        self.assertIn("transport_.read_audio_pcm(live_audio_payload_)", host_source)
        self.assertIn("SdlAudioOutput::instance()", host_source)
        self.assertIn(
            "audio.queue(\n            live_audio_payload_.data(), live_audio_payload_.size())",
            host_source,
        )
        self.assertIn("consume_live_audio();", host_source)

    def test_vulkan_pipeline_cache_is_persistent_and_used_for_all_pipelines(self) -> None:
        host_source = presenter_source_text()

        self.assertIn("--pipeline-cache", host_source)
        self.assertIn("vkCreatePipelineCache", host_source)
        self.assertIn("vkGetPipelineCacheData", host_source)
        self.assertIn("MoveFileExW", host_source)
        self.assertIn(
            "vkCreateComputePipelines(\n        device_,\n        pipeline_cache_",
            host_source,
        )
        self.assertIn(
            "vkCreateGraphicsPipelines(\n        device_,\n        pipeline_cache_",
            host_source,
        )
        self.assertIn(
            "std::vector<VkGraphicsPipelineCreateInfo> pipeline_infos",
            host_source,
        )
        self.assertIn(
            "static_cast<uint32_t>(pipeline_infos.size())",
            host_source,
        )

    def test_presenter_runner_forwards_pipeline_cache_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            executable = root / "presenter.exe"
            pipeline_cache = root / "cache" / "vulkan.bin"
            executable.touch()
            completed = Mock(returncode=0, stdout="", stderr="")
            with patch(
                "tools.host.first_frame_smoke.subprocess.run",
                return_value=completed,
            ) as run:
                run_first_frame(
                    executable=executable,
                    debug_json=None,
                    max_frames=0,
                    timeout_seconds=0,
                    screenshot=None,
                    hotkey_screenshot_directory=None,
                    pipeline_cache=pipeline_cache,
                )

            command = run.call_args.args[0]
            self.assertEqual(
                command[command.index("--pipeline-cache") + 1],
                str(pipeline_cache),
            )

    def test_presenter_runner_forwards_pipelined_acknowledgement(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            executable = Path(temp_dir) / "presenter.exe"
            executable.touch()
            completed = Mock(returncode=0, stdout="", stderr="")
            with patch(
                "tools.host.first_frame_smoke.subprocess.run",
                return_value=completed,
            ) as run:
                run_first_frame(
                    executable=executable,
                    debug_json=None,
                    max_frames=0,
                    timeout_seconds=0,
                    render_stream_json=Path("render.json"),
                    live_render_stream=True,
                    screenshot=None,
                    hotkey_screenshot_directory=None,
                    presentation_pipeline_depth=2,
                )

            command = run.call_args.args[0]
            self.assertEqual(
                command[command.index("--presentation-pipeline-depth") + 1],
                "2",
            )

    def test_depth_two_acknowledges_after_source_residency(self) -> None:
        source = presenter_source_text()
        load_block = source.split("bool load_recovered_render_work", 1)[1].split(
            "void destroy_native_render_resources", 1
        )[0]
        reload_block = source.split("void reload_live_render_work", 1)[1].split(
            "std::vector<uint32_t> read_spirv", 1
        )[0]

        source_commit = load_block.index("recovered_source_ = std::move(next_source)")
        callback = load_block.index("source_resident_callback();")
        interpretation = load_block.index("interpret_recovered_d3d_packed_append(")
        pipelined_ack = reload_block.index("acknowledge_current_presentation()")
        loaded = reload_block.index("load_recovered_render_work(source_resident_callback)")
        resource_work = reload_block.index("destroy_native_render_resources(")
        legacy_ack = reload_block.index("if (options_.presentation_pipeline_depth == 1u)")

        self.assertLess(source_commit, callback)
        self.assertLess(callback, interpretation)
        self.assertLess(pipelined_ack, loaded)
        self.assertLess(loaded, resource_work)
        self.assertLess(resource_work, legacy_ack)
        self.assertIn('"after_source_load"', reload_block)
        self.assertIn('"source_residency_us"', reload_block)
        self.assertIn('"post_ack_us"', reload_block)

    def test_smoke_summary_surfaces_pipeline_cache_reuse(self) -> None:
        summary = summarize_smoke(
            compile_result=None,
            run_result={
                "returncode": 0,
                "debug_json": "events.jsonl",
                "screenshot": None,
                "events": [
                    {
                        "event": "vulkan_pipeline_cache_opened",
                        "path": "vulkan.bin",
                        "loaded_bytes": 4096,
                        "initial_data_rejected": False,
                    },
                    {
                        "event": "vulkan_pipeline_cache_saved",
                        "path": "vulkan.bin",
                        "bytes": 6144,
                        "written": True,
                    },
                ],
            },
        )

        self.assertEqual(summary["run"]["pipeline_cache"]["loaded_bytes"], 4096)
        self.assertEqual(summary["run"]["pipeline_cache"]["saved_bytes"], 6144)
        self.assertTrue(summary["run"]["pipeline_cache"]["written"])

    def test_headless_render_stream_analysis_reports_transform_coverage(self) -> None:
        host_source = presenter_source_text()
        runner_source = Path("tools/host/first_frame_smoke.py").read_text(encoding="utf-8")
        self.assertIn("--analyze-render-stream", host_source)
        self.assertIn("render_stream_analysis_complete", host_source)
        self.assertIn("continuation_bootstrapped", host_source)
        self.assertIn("nv2a_render_state_diagnostics", host_source)
        self.assertIn("indexed_program_tiny_coverage_suspected", host_source)
        self.assertIn("subpixel_indexed_program_draw_count", host_source)
        self.assertIn("viewport_constants_valid", host_source)
        self.assertIn("draw_has_supported_host_transform", host_source)
        self.assertIn("NV097_SET_VIEWPORT_OFFSET aliases vertex constant 59", host_source)
        self.assertIn("NV097_SET_VIEWPORT_SCALE aliases vertex constant 58", host_source)
        self.assertIn("method == 0x1E90u", host_source)
        self.assertIn("(draw.transform_execution_mode & 3u) != 2u", host_source)
        self.assertIn("--analyze-render-stream", runner_source)
        self.assertIn("render_stream_analysis_completed", runner_source)

    def test_program_vertices_reconstruct_homogeneous_clip_positions(self) -> None:
        host_source = presenter_source_text()
        shader_source = Path("runtime/host/shaders/nv2a_inline.vert").read_text(encoding="utf-8")

        self.assertIn("program_position_valid", host_source)
        self.assertIn("(result.output_masks[0] & 14u) == 14u", host_source)
        self.assertIn("position_w_usable", host_source)
        self.assertIn("vertex.w = 1.0f;", host_source)
        self.assertIn("vertex.x *= vertex.w", host_source)
        self.assertIn("vertex.z *= vertex.w", host_source)
        self.assertIn("VK_FORMAT_R32G32B32A32_SFLOAT", host_source)
        self.assertIn("layout(location = 0) in vec4 in_position", shader_source)
        self.assertIn("(output_masks[0] & 14u) == 14u", shader_source)
        self.assertIn("position_w_usable", shader_source)
        self.assertIn("position.w = 1.0;", shader_source)
        self.assertIn("gl_Position = in_position", shader_source)
        self.assertIn("create_depth_resources();", host_source)
        self.assertIn("nv2a_depth_compare_op", host_source)
        self.assertIn("state.depth_write_enable", host_source)
        self.assertIn("clip_width >= swapchain_extent_.width", host_source)
        self.assertIn("clip_height >= swapchain_extent_.height", host_source)
        self.assertIn("nv2a_cull_mode", host_source)
        self.assertIn("nv2a_front_face", host_source)
        self.assertIn("--presented-draw-begin", host_source)
        self.assertIn("--presented-draw-end", host_source)
        vertex_program_source = Path("runtime/host/nv2a_vertex_program.h").read_text(
            encoding="utf-8"
        )
        self.assertIn("index == 12u", vertex_program_source)
        self.assertIn("ilu_opcode != 0u && temporary_index == 1u", vertex_program_source)

    def test_indexed_vertex_programs_execute_in_the_vulkan_vertex_shader(self) -> None:
        host_source = presenter_source_text()
        shader_source = Path("runtime/host/shaders/nv2a_inline.vert").read_text(encoding="utf-8")
        runner_source = Path("tools/host/first_frame_smoke.py").read_text(encoding="utf-8")

        self.assertIn("struct NativeVertexProgramState", host_source)
        self.assertIn("refresh_vertex_program_states", host_source)
        self.assertIn("VK_BUFFER_USAGE_STORAGE_BUFFER_BIT", host_source)
        self.assertIn("gpu_vertex_program_compatible", host_source)
        self.assertIn("|| !draw.indexed_array", host_source)
        self.assertIn("Context-constant writes are legal NV2A behavior", host_source)
        self.assertIn("nv2a_vertex_program_states_refreshed", host_source)
        self.assertIn("gpu_vertex_program_vertices", host_source)
        self.assertIn("gpu_output_diagnostics_deferred", host_source)
        self.assertIn("--cpu-vertex-programs", host_source)
        self.assertIn("--cpu-vertex-programs", runner_source)

        self.assertIn("binding = 9", shader_source)
        self.assertIn("binding = 10", shader_source)
        self.assertIn("bool execute_vertex_program", shader_source)
        self.assertIn("temporary_index == 12u", shader_source)
        self.assertIn("temporary_index < 12u", shader_source)
        self.assertIn("ilu_opcode != 0u && temporary_index == 1u", shader_source)
        self.assertIn("Compatibility filtering keeps context-constant writes on CPU", shader_source)
        self.assertIn("transform_constants[58].z", shader_source)
        self.assertIn("position.xyz *= position.w", shader_source)

    def test_indexed_attributes_fetch_from_raw_gpu_resources(self) -> None:
        host_source = presenter_source_text()
        shader_source = Path("runtime/host/shaders/nv2a_inline.vert").read_text(encoding="utf-8")
        runner_source = Path("tools/host/first_frame_smoke.py").read_text(encoding="utf-8")
        dirty_range_source = Path("runtime/host/dirty_ranges.h").read_text(encoding="utf-8")

        self.assertIn("prepare_gpu_raw_attribute_fetch", host_source)
        self.assertIn("gpu_raw_vertex_bytes", host_source)
        self.assertIn("gpu_raw_vertex_indices", host_source)
        self.assertIn("refresh_raw_vertex_buffers", host_source)
        self.assertIn("GpuRawVertexResourceCache", host_source)
        self.assertIn("refresh_gpu_raw_vertex_resource_cache", host_source)
        self.assertIn("append_dirty_range", host_source)
        self.assertIn("inline void append_dirty_range", dirty_range_source)
        self.assertIn("std::minmax_element", host_source)
        self.assertIn("kCompareChunkBytes = 64u", host_source)
        self.assertIn("resource_upload_ranges", host_source)
        self.assertIn("nv2a_gpu_raw_dirty_range_validation", host_source)
        self.assertIn("state.raw_attribute_fetch ? 0u : 1u", host_source)
        self.assertIn("draw.gpu_raw_attribute_fetch", host_source)
        self.assertIn("expanded_vertex_bytes_avoided", host_source)
        self.assertIn("--cpu-vertex-attributes", host_source)
        self.assertIn("--cpu-vertex-attributes", runner_source)

        self.assertIn("binding = 11", shader_source)
        self.assertIn("decode_raw_vertex_attribute", shader_source)
        self.assertIn("raw_vertex_u32", shader_source)
        self.assertIn("raw_source_index_base", shader_source)
        self.assertIn("current_vertex_attributes[input_index]", shader_source)
        self.assertIn("packed & 0x7FFu", shader_source)
        self.assertIn("float(signed_value) / 32767.0", shader_source)

    def test_texture_conversion_runs_in_a_batched_compute_shader(self) -> None:
        host_source = presenter_source_text()
        compute_source = Path("runtime/host/shaders/nv2a_texture_convert.comp").read_text(
            encoding="utf-8"
        )
        runner_source = Path("tools/host/first_frame_smoke.py").read_text(encoding="utf-8")

        self.assertIn("execute_gpu_texture_conversion_batch", host_source)
        self.assertIn("vkCreateComputePipelines(texture conversion)", host_source)
        self.assertIn("nv2a_gpu_texture_conversion_batch", host_source)
        self.assertIn("nv2a_gpu_texture_conversion_validation", host_source)
        self.assertIn("destroy_gpu_batch_and_use_cpu", host_source)
        self.assertIn("build_cpu_dxt_conversion_mips", host_source)
        self.assertIn("const bool validate_gpu_texture_conversion", host_source)
        self.assertIn("options_.strict_render_validation", host_source)
        self.assertIn("!options_.live_render_stream", host_source)
        self.assertIn("cpu_texture_conversion", host_source)
        self.assertIn("--cpu-texture-conversion", host_source)
        self.assertIn("DEFAULT_TEXTURE_CONVERT_SHADER", runner_source)
        self.assertIn("--texture-convert-shader", runner_source)
        self.assertIn("--cpu-texture-conversion", runner_source)

        self.assertIn("MODE_DXT1", compute_source)
        self.assertIn("MODE_DXT5", compute_source)
        self.assertIn("MODE_DOWNSAMPLE", compute_source)
        self.assertIn("decode_dxt1", compute_source)
        self.assertIn("decode_dxt5", compute_source)
        self.assertIn("downsample", compute_source)
        self.assertIn("rgba_words", compute_source)

    def test_smoke_summary_surfaces_gpu_texture_conversion(self) -> None:
        summary = summarize_smoke(
            compile_result=None,
            run_result={
                "returncode": 0,
                "debug_json": "events.jsonl",
                "screenshot": None,
                "events": [
                    {
                        "event": "nv2a_texture_resources_refreshed",
                        "gpu_conversion_backend": "compute",
                        "gpu_converted": 4,
                        "cpu_converted": 1,
                        "render_target_feedback_image_cache_hits": 1,
                        "render_target_feedback_image_cache_misses": 0,
                        "render_target_feedback_image_cache_stores": 1,
                        "render_target_feedback_image_cache_evictions": 0,
                        "render_target_feedback_image_cache_resident": 1,
                        "render_target_feedback_image_cache_capacity": 8,
                    },
                    {
                        "event": "nv2a_gpu_texture_conversion_batch",
                        "dxt1_textures": 3,
                        "dxt5_textures": 1,
                        "mips": 18,
                        "generated_mips": 7,
                        "input_bytes": 1000,
                        "output_bytes": 6000,
                        "conversion_us": 250,
                        "gpu_submission_us": 75,
                    },
                    {
                        "event": "nv2a_gpu_texture_conversion_validation",
                        "passed": True,
                        "coverage_complete": True,
                        "mismatch_bytes": 0,
                    },
                ],
            },
        )

        conversion = summary["run"]["texture_conversion"]
        self.assertEqual(conversion["backend"], "compute")
        self.assertEqual(conversion["gpu_converted_textures"], 4)
        self.assertEqual(conversion["dxt1_textures"], 3)
        self.assertEqual(conversion["dxt5_textures"], 1)
        self.assertEqual(conversion["mips"], 18)
        self.assertEqual(conversion["gpu_submission_us"], 75)
        self.assertTrue(conversion["validation_passed"])
        self.assertTrue(conversion["validation_coverage_complete"])
        feedback_cache = summary["run"]["render_target_feedback_image_cache"]
        self.assertEqual(feedback_cache["hit_count"], 1)
        self.assertEqual(feedback_cache["miss_count"], 0)
        self.assertEqual(feedback_cache["store_count"], 1)
        self.assertEqual(feedback_cache["eviction_count"], 0)
        self.assertEqual(feedback_cache["resident_count"], 1)
        self.assertEqual(feedback_cache["capacity"], 8)

    def test_live_reload_waits_only_for_in_flight_frame_and_reuses_pipelines(self) -> None:
        source = presenter_source_text()

        self.assertIn('"vkWaitForFences(live reload)"', source)
        self.assertNotIn('"vkDeviceWaitIdle(live reload)"', source)
        self.assertIn("nv2a_graphics_pipeline_cache_hit", source)
        self.assertIn("graphics_pipelines_.begin(), graphics_pipelines_.end()", source)
        self.assertIn("live_render_stream_reload_skipped", source)
        self.assertIn("already_presented_without_command_or_resource_delta", source)

    def test_live_reload_cost_does_not_scale_with_complete_command_history(self) -> None:
        source = presenter_source_text()

        self.assertIn("interpret_recovered_d3d_append", source)
        self.assertIn("last_interpreted_command_delta_", source)
        self.assertIn("presentable_command_record_count", source)
        self.assertIn("read_live_command_stream_delta", source)
        self.assertIn("read_live_command_span_stream_delta", source)
        self.assertIn("interpret_recovered_d3d_packed_append", source)
        self.assertIn("interpret_recovered_d3d_span_append", source)
        self.assertIn("live_command_delta_bytes_", source)
        self.assertIn('? "bulk_span_v1" : "packed_direct"', source)
        self.assertIn("std::array<char, 8> magic{}", source)
        self.assertIn('std::string(magic.data(), magic.size()) != "B2SPAN01"', source)
        self.assertIn('"command_file_reused"', source)
        self.assertIn("last_command_file_reused_", source)
        self.assertIn("snapshot_base_record_count", source)
        self.assertIn("command_read_begin", source)
        self.assertIn("target_command_count - command_read_begin", source)
        self.assertIn('"native_command_records_read"', source)
        self.assertNotIn("tail_context_commands = 65536", source)
        self.assertNotIn("retained_commands", source)
        self.assertNotIn("append_live_command_stream", source)
        self.assertIn("std::array<uint8_t, 8> payload{}", source)
        self.assertIn("uint8_t payload_size = 0", source)
        command_struct = source.split("struct RecoveredD3DCommand", 1)[1].split("};", 1)[0]
        self.assertNotIn("std::vector<uint8_t> payload;", command_struct)
        # Only one-shot replay reserves complete history. Live presentation
        # allocates and interprets the newly published absolute interval.
        self.assertEqual(source.count("commands.reserve(count);"), 1)
        self.assertNotIn("commands.reserve(record_count);", source)

    def test_packed_interpreter_uses_ordered_word_fast_path_with_fallback(self) -> None:
        source = presenter_source_text()
        interpreter = source.split("push_buffer_words_from_recovered_commands", 1)[1].split(
            "struct SurfacePayloadCandidate", 1
        )[0]

        self.assertIn("words.reserve(command_count)", interpreter)
        self.assertIn("if (ordered_word_writes)", interpreter)
        self.assertIn("ordered_push_buffer_append_count", interpreter)
        self.assertIn("if (indexed_word_writes)", interpreter)
        self.assertIn("indexed_word_push_buffer_append_count", interpreter)
        self.assertIn("reconstructed_push_buffer_append_count", interpreter)
        self.assertIn("pending_bytes", interpreter)
        self.assertIn("surface_payload_scan_required", source)
        self.assertIn("surface_payload_scan_skipped_count", source)

    def test_method_interpreter_batches_hot_data_and_state_packets(self) -> None:
        source = presenter_source_text()
        interpreter = source.split("void interpret_push_buffer_method_packet", 1)[1].split(
            "void interpret_long_non_increasing_packet", 1
        )[0]

        self.assertIn("bulk_indexed", interpreter)
        self.assertIn("first_method == 0x1800u", interpreter)
        self.assertIn("bulk_inline", interpreter)
        self.assertIn("first_method == 0x1818u", interpreter)
        self.assertIn("bulk_state", interpreter)
        self.assertIn("nv2a_method_is_batchable_state", interpreter)
        self.assertIn("nv2a_state_method_is_unchanged", interpreter)
        self.assertIn("one_contiguous_run", interpreter)
        self.assertIn("active_vertex_indices.resize", interpreter)
        self.assertIn("inline_words.resize", interpreter)
        self.assertIn("push_buffer_words_scratch", source)
        self.assertIn('"bulk_indexed_method_delta"', source)
        self.assertIn('"bulk_inline_method_delta"', source)
        self.assertIn('"bulk_state_method_delta"', source)
        self.assertIn('"state_method_noop_delta"', source)
        self.assertIn("state_seed_updates_required", source)

    def test_live_span_interpreter_caches_structural_command_work(self) -> None:
        source = presenter_source_text()
        cache = Path("runtime/host/command_work_cache.h").read_text(encoding="utf-8")

        self.assertIn("CommandWorkCache command_work_cache_", source)
        self.assertIn("command_work_cache->materialize", source)
        self.assertIn("live_command_generation_", source)
        self.assertIn('"command_work_cache_hit"', source)
        self.assertIn(
            "later span payload replaces",
            Path("tests/native/host_core_tests.cpp").read_text(encoding="utf-8"),
        )
        self.assertIn("kPlanCapacity = 256u", cache)
        self.assertIn("std::unordered_multimap<uint64_t, PlanIterator>", cache)
        self.assertIn("plans_.splice(plans_.end(), plans_, plan_iterator)", cache)
        self.assertIn("kByteCapacity = 64u * 1024u * 1024u", cache)
        self.assertIn("descriptor.address + 0x1000u < pending_max_address", cache)
        self.assertIn("structural.address = descriptor.address - segment_anchor", cache)
        self.assertIn("descriptor.address + layout.descriptor_payload_offset", cache)
        self.assertIn("kWindowBytes = 16u * 1024u", cache)
        self.assertIn("allow_contiguous_merge", cache)
        self.assertIn("std::memcpy", cache)

    def test_feedback_specs_are_indexed_and_cached_per_render_generation(self) -> None:
        source = presenter_source_text()
        feedback = source.split("presented_render_target_feedback_specs() const", 1)[1].split(
            "render_target_feedback_texture_matches_spec", 1
        )[0]

        self.assertIn("feedback_spec_cache_generation_", feedback)
        self.assertIn("latest_offscreen_producers", feedback)
        self.assertIn("latest_offscreen_producers_by_address", feedback)
        self.assertIn("cubemap_face_producer_addresses", feedback)
        self.assertIn("nv2a_texture_uncompressed_bytes_per_pixel", feedback)
        self.assertNotIn("producer_index < consumer_index", feedback)
        self.assertIn('"feedback_spec_build_us"', source)
        self.assertIn('"feedback_spec_cache_hits"', source)

    def test_texture_refresh_reuses_stable_sampler_descriptor_sets(self) -> None:
        source = presenter_source_text()
        binding_refresh = source.split("void refresh_host_texture_bindings()", 1)[1].split(
            "void refresh_host_textures", 1
        )[0]
        texture_refresh = source.split("void refresh_host_textures", 1)[1].split(
            "std::vector<NativeVertex> prepare_presented_vertices", 1
        )[0]

        self.assertIn("const bool layout_unchanged", binding_refresh)
        self.assertIn("stage_binding.texture_2d_view", binding_refresh)
        self.assertIn("stage_binding.texture_cube_view", binding_refresh)
        self.assertIn("write.dstBinding = stage + dimension * 4u", binding_refresh)
        self.assertIn("texture_binding_set_reuse_count_", binding_refresh)
        self.assertIn("texture_binding_set_rebuild_count_", binding_refresh)
        self.assertNotIn("refresh_host_texture_bindings(true)", source)
        self.assertNotIn(
            "destroy_host_texture_bindings();\n        std::vector<HostTexture> previous",
            texture_refresh,
        )
        self.assertIn('"texture_binding_update_us"', source)
        self.assertIn('"texture_binding_descriptor_sets_allocated"', source)

    def test_resource_refresh_indexes_texture_matches_and_pipeline_states(self) -> None:
        source = presenter_source_text()
        texture_refresh = source.split("void refresh_host_textures", 1)[1].split(
            "std::vector<NativeVertex> prepare_presented_vertices", 1
        )[0]
        pipeline_refresh = source.split("void create_native_graphics_pipeline()", 1)[1].split(
            "void create_buffer", 1
        )[0]

        self.assertIn("previous_indices_by_address", texture_refresh)
        self.assertIn("active_indices_by_address", texture_refresh)
        self.assertIn("feedback_canonical_addresses", texture_refresh)
        self.assertIn("find_previous_index", texture_refresh)
        self.assertIn("render_target_feedback_image_cache_", texture_refresh)
        self.assertIn(
            "cache_render_target_feedback_texture(texture)",
            texture_refresh,
        )
        self.assertIn(
            "trim_render_target_feedback_image_cache()",
            texture_refresh,
        )
        self.assertNotIn("std::find_if(\n                previous.begin()", texture_refresh)
        self.assertIn(
            "std::unordered_set<NativePipelineState, NativePipelineStateHash>",
            pipeline_refresh,
        )
        self.assertIn("resident_states.find(state)", pipeline_refresh)
        self.assertNotIn("std::find(states.begin(), states.end()", pipeline_refresh)
        for field in (
            '"resource_preflight_us"',
            '"resource_destroy_us"',
            '"native_resource_create_us"',
            '"vertex_resource_prepare_us"',
            '"state_resource_prepare_us"',
            '"texture_resource_prepare_us"',
            '"texture_refresh_us"',
            '"offscreen_resource_prepare_us"',
            '"resource_bookkeeping_us"',
            '"pipeline_prepare_us"',
            '"pipeline_state_discovery_us"',
            '"texture_indexed_lookup_candidates"',
            '"render_target_feedback_image_cache_hits"',
            '"render_target_feedback_image_cache_resident"',
        ):
            self.assertIn(field, source)

    def test_live_reload_batches_reads_reuses_texture_content_and_caps_guest_at_60_hz(self) -> None:
        source = presenter_source_text()

        self.assertIn("live_command_delta_bytes_.resize", source)
        self.assertIn("read_live_render_manifest", source)
        self.assertIn("transport_.read_manifest_file(", source)
        self.assertIn('"B2PRS001"', source)
        self.assertIn("acknowledge_current_presentation();", source)
        self.assertIn("OpenEventW", source)
        self.assertIn("SetEvent(presentation_ack_event_)", source)
        self.assertIn("publication_event_", source)
        self.assertIn('"B2TEX001"', source)
        self.assertIn("reusable_by_address", source)
        binary_loader = source.index(
            "std::vector<RecoveredTextureResource> "
            "load_recovered_texture_resources_bytes("
        )
        stream_fallback = source.index("std::istringstream stream(", binary_loader)
        direct_loader = source[binary_loader:stream_fallback]
        self.assertIn(
            'std::memcmp(bytes.data(), "B2TEX001", 8u) == 0',
            direct_loader,
        )
        self.assertIn("resource.payload.resize(payload_size)", direct_loader)
        self.assertNotIn("std::istringstream", direct_loader)
        self.assertIn('"resource_snapshot_reused_bytes"', source)
        self.assertIn("texture_content_identity", source)
        self.assertIn("retain_matching", source)
        self.assertIn('"nv2a_texture_resources_refreshed"', source)
        self.assertIn("std::chrono::nanoseconds(16666667)", source)
        self.assertIn("CreateWaitableTimerExW", source)
        self.assertIn("kHighResolutionWaitableTimerFlag", source)
        self.assertIn("wait_for_frame_deadline(next_frame_time)", source)
        wait_begin = source.index("void wait_for_frame_deadline(")
        wait_end = source.index(
            "void queue_hotkey_screenshot()",
            wait_begin,
        )
        wait_source = source[wait_begin:wait_end]
        self.assertIn("sleep_until_frame_deadline(deadline)", wait_source)
        self.assertIn("WaitForMultipleObjects", wait_source)
        self.assertIn("reload_live_render_work(true)", wait_source)
        self.assertIn("options_.presentation_pipeline_depth != 2u", wait_source)
        self.assertIn('"guest_frame_rate_limit_hz"', source)
        self.assertIn("kTargetGuestFrameRateHz = 60u", source)
        self.assertIn('"target_frame_us"', source)

    def test_live_reload_retries_incomplete_shared_manifest(self) -> None:
        source = presenter_source_text()

        self.assertIn("load_initial_recovered_render_work", source)
        self.assertIn("live_render_startup_manifest_retried", source)
        self.assertIn("std::chrono::seconds(2)", source)
        self.assertIn("The validated manifest is the publication boundary", source)
        self.assertIn("FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE", source)
        self.assertIn("manifest_text[final_non_space] != '}'", source)
        self.assertIn("write_time == live_render_write_time_", source)
        self.assertIn("WaitForSingleObject(publication_event_, 0u)", source)
        self.assertIn("publication_retry_pending_", source)
        event_probe = source.index("WaitForSingleObject(publication_event_, 0u)")
        timestamp_probe = source.index(
            "std::filesystem::last_write_time(\n            options_.render_stream_json",
            event_probe,
        )
        self.assertLess(event_probe, timestamp_probe)
        self.assertNotIn("command-sidecar-only fast path", source)

    def test_normal_live_reload_defers_expensive_presented_geometry_diagnostics(self) -> None:
        source = presenter_source_text()
        sampling_begin = source.index("const bool diagnostic_sample_due")
        sampling_end = source.index(
            "last_presented_diagnostics_sampled_ = diagnostic_sample_due",
            sampling_begin,
        )
        sampling_source = source[sampling_begin:sampling_end]

        self.assertIn("const bool emit_presented_details", source)
        self.assertIn("live_render_reload_count_ % 120u == 0u", source)
        self.assertIn("options_.strict_render_validation", sampling_source)
        self.assertNotIn("create_textures", sampling_source)
        self.assertIn("if (emit_presented_details)", source)
        self.assertIn("diagnostic_sample_due", source)
        self.assertIn('"presented_diagnostics_sampled"', source)
        self.assertIn('"render_validation_us"', source)
        self.assertIn('"resource_prepare_us"', source)
        self.assertIn(
            "if (!log_.enabled() && !options_.strict_render_validation)",
            source,
        )
        self.assertIn("presented_diagnostics_generation_", source)
        self.assertIn("presented_diagnostics_valid_ = false", source)
        self.assertIn(
            "collect_diagnostics ? &draw_diagnostics.transform : nullptr",
            source,
        )
        self.assertIn('"nv2a_presented_geometry_anomalies"', source)
        self.assertIn("exact_center_origin_draw_count", source)
        self.assertIn("manifest_guest_flip_count", source)
        self.assertIn("presented_vertex_count", source)
        self.assertIn("fullscreen_draw_count", source)
        self.assertIn("presented_texture_addresses", source)

    def test_live_resource_generation_is_transactional_and_owns_diagnostics(self) -> None:
        source = presenter_source_text()

        load_begin = source.index("bool load_recovered_render_work(")
        load_end = source.index("void destroy_native_render_resources", load_begin)
        load_source = source[load_begin:load_end]
        source_load = load_source.index("load_or_build_recovered_d3d_command_stream")
        generation_commit = load_source.index(
            "live_resource_generation_ = next_resource_generation"
        )
        self.assertGreater(generation_commit, source_load)
        self.assertIn("resource_source == live_resource_source_", load_source)
        self.assertIn("last_resource_generation_changed_", load_source)
        self.assertIn("if (create_textures)", source)
        self.assertIn("unsupported_texture_resource_count_ = 0", source)

    def test_frame_readback_reports_low_information_coverage(self) -> None:
        source = presenter_source_text()

        self.assertIn("bright_count", source)
        self.assertIn("dark_count", source)
        self.assertIn("near_solid", source)
        self.assertIn("low_information", source)

    def test_keyboard_controller_pulses_survive_guest_polling_interval(self) -> None:
        source = presenter_source_text()

        self.assertIn("kMinimumPulseMs = 150", source)
        self.assertIn("kMaximumPulseMs = 2000", source)
        self.assertIn("kMinimumPulseGuestFlips = 2u", source)
        self.assertIn("key_down[static_cast<size_t>(key.scancode)]", source)
        self.assertIn("buttons_from_keys()", source)
        self.assertIn("latch_pressed_buttons(", source)
        self.assertIn("latched_buttons |= bit", source)
        self.assertIn("latch_guest_flip_counts[bit_index]", source)
        self.assertIn("latch_max_deadlines[bit_index]", source)
        self.assertIn("flip_pulse >= kMinimumPulseGuestFlips", source)
        self.assertIn("impl_->update_latches(guest_flip_count)", source)
        self.assertIn("case SDLK_RETURN: return kA", source)
        self.assertIn("case SDLK_SPACE:", source)
        self.assertIn("state.buttons = state.keyboard_buttons", source)

    def test_sdl3_gamepad_input_maps_complete_xbox_controller_state(self) -> None:
        source = presenter_source_text()

        self.assertIn("#include <SDL3/SDL.h>", source)
        self.assertIn("SDL_InitSubSystem(SDL_INIT_GAMEPAD)", source)
        self.assertIn("SDL_GetGamepads(&count)", source)
        self.assertIn("SDL_OpenGamepad(id)", source)
        self.assertIn("SDL_UpdateGamepads();", source)
        self.assertIn("SDL_GAMEPAD_BUTTON_SOUTH", source)
        self.assertIn("SDL_GAMEPAD_BUTTON_RIGHT_SHOULDER", source)
        self.assertIn("SDL_GAMEPAD_BUTTON_DPAD_UP", source)
        self.assertIn("SDL_GAMEPAD_AXIS_LEFT_TRIGGER", source)
        self.assertIn("SDL_GAMEPAD_AXIS_RIGHTY", source)
        self.assertIn("SDL_GetGamepadMapping(candidate)", source)
        self.assertIn("SDL_GetGamepadGUIDForID(gamepad_id)", source)
        self.assertIn("status.gamepad_initialized", source)
        self.assertIn("host_controller_connected", source)
        self.assertNotIn("joyGetPosEx", source)
        self.assertNotIn("latch_controller_button_presses", source)
        poll_source = source.split("PlatformPollResult SdlPlatform::poll(", 1)[1].split(
            "PlatformPollResult SdlPlatform::inject_confirm", 1
        )[0]
        self.assertNotIn("latch_pressed_buttons", poll_source)
        self.assertIn("controller_states_equivalent", source)
        self.assertIn("kStickPublishHysteresis = 256", source)
        self.assertIn("kTriggerPublishHysteresis = 8", source)
        self.assertIn("controller.left_trigger", source)
        self.assertIn("controller.thumb_lx", source)
        self.assertIn("state.keyboard_buttons | state.latched_buttons", source)
        self.assertIn('"replace_retry_count"', source)

    def test_frame_events_attribute_blocking_vulkan_boundaries(self) -> None:
        source = presenter_source_text()

        self.assertIn('"draw_fence_wait_us"', source)
        self.assertIn('"window_message_pump_us"', source)
        self.assertIn('"controller_poll_us"', source)
        self.assertIn('"keyboard_latch_us"', source)
        self.assertIn('"audio_submit_us"', source)
        self.assertIn('"reload_probe_us"', source)
        self.assertIn('"pre_render_unattributed_us"', source)
        self.assertIn('"acquire_us"', source)
        self.assertIn('"submit_us"', source)
        self.assertIn('"readback_wait_us"', source)
        self.assertIn('"present_us"', source)
        self.assertIn('"draw_unattributed_us"', source)
        self.assertIn("const auto acquire_begin", source)
        self.assertIn("const auto submit_begin", source)
        self.assertIn("const auto present_begin", source)

    def test_native_dxt1_decoder_uses_linear_block_order(self) -> None:
        source = presenter_source_text()

        self.assertIn("block_index % blocks_x", source)
        self.assertIn("block_index / blocks_x", source)
        self.assertNotIn("block_index >> (bit * 2u)", source)

    def test_native_dxt5_decoder_uploads_title_alpha_textures(self) -> None:
        source = presenter_source_text()

        self.assertIn("decompress_dxt5", source)
        self.assertIn('resource.format == "DXT5"', source)
        self.assertIn("alpha_indices >> (3u * pixel_index)", source)
        self.assertIn("alphas[6] = 0u", source)
        self.assertIn("alphas[7] = 255u", source)

    def test_native_texture_lookup_disambiguates_reused_guest_addresses(self) -> None:
        source = presenter_source_text()

        self.assertIn("host_texture_matches_draw", source)
        self.assertIn("texture.width == width", source)
        self.assertIn("texture.height == height", source)
        self.assertIn(
            "nv2a_texture_format_matches(texture.format, format_raw)",
            source,
        )
        self.assertIn("descriptor_for_draw(draw)", source)
        self.assertNotIn("descriptor_for_texture(uint32_t guest_address)", source)

    def test_native_replay_converts_quad_lists_to_triangle_strips(self) -> None:
        source = presenter_source_text()

        self.assertIn(
            "quad_strip_order{0u, 1u, 3u, 2u}",
            source,
        )
        self.assertIn("converted.primitive = 6u", source)
        self.assertIn("converted_quad_count", source)
        self.assertIn("discarded_quad_vertex_count", source)

    def test_native_replay_materializes_indexed_vertex_arrays(self) -> None:
        source = presenter_source_text()

        self.assertIn("method == 0x1800u", source)
        self.assertIn("method == 0x1808u", source)
        self.assertIn('"zero_count_indexed_array_noop_packets"', source)
        self.assertNotIn('"zero_count_indexed_array_packets"', source)
        self.assertIn("finish_indexed_draw", source)
        self.assertIn("materialize_indexed_draws", source)
        self.assertIn('resource.format != "VERTEX_BUFFER"', source)
        self.assertIn("vertex.program_inputs_valid = true", source)
        self.assertIn('"materialized_indexed_draws"', source)
        self.assertIn(
            '"missing_presented_indexed_resource_draw_count"',
            source,
        )

    def test_native_replay_discards_stale_ring_tail_after_jump(self) -> None:
        source = presenter_source_text()

        self.assertIn("const bool jump =", source)
        self.assertIn("words[index].run_id == run_id", source)
        self.assertIn("stale ring contents", source)

    def test_native_replay_preserves_current_attributes_for_disabled_arrays(
        self,
    ) -> None:
        source = presenter_source_text()

        self.assertIn("Nv2aVertexAttributes current_vertex_attributes", source)
        self.assertIn("method >= 0x1940u && method <= 0x197Cu", source)
        self.assertIn("static_cast<float>(data & 0xFFu) / 255.0f", source)
        self.assertIn(
            "vertex.program_inputs = draw.current_vertex_attributes",
            source,
        )
        self.assertIn(
            "0x1A00u + static_cast<uint32_t>(attribute) * 16u",
            source,
        )

    def test_native_replay_distinguishes_widget_scissors_from_offscreen_targets(
        self,
    ) -> None:
        source = presenter_source_text()

        self.assertIn("presented_surface_color_offset", source)
        self.assertIn("draw_targets_presented_surface", source)
        self.assertIn("draw_surface_clip_is_subsurface_viewport", source)
        self.assertIn("kNv2aViewportSubpixelBias = 0.53125f", source)
        self.assertIn(
            "clip_width >= swapchain_extent_.width\n"
            "        || clip_height >= swapchain_extent_.height",
            source,
        )
        self.assertIn("viewport_scale_x - half_width", source)
        self.assertIn("viewport_scale_y + half_height", source)
        self.assertIn("clip_x != 0u || clip_y != 0u", source)
        self.assertIn("VK_DYNAMIC_STATE_SCISSOR", source)
        self.assertIn("vkCmdSetScissor", source)
        self.assertIn("draw_scissor(draw, target_extent)", source)
        self.assertIn("nv2a_screen_coordinate_to_vulkan_ndc", source)
        shader = Path("runtime/host/shaders/nv2a_inline.vert").read_text(encoding="utf-8")
        self.assertIn("position.xy -= vec2(0.03125);", shader)
        self.assertIn('"offscreen_render_target_draw_count"', source)
        self.assertIn('"presented_primitive_draw_counts"', source)
        self.assertIn('"unsupported_primitive_draw_counts"', source)
        self.assertIn('"nv2a_unsupported_presented_primitive"', source)
        self.assertIn("case 5u:", source)
        self.assertIn("VK_PRIMITIVE_TOPOLOGY_TRIANGLE_LIST", source)
        self.assertIn("native_pipeline_primitive_supported", source)
        self.assertIn("VK_PRIMITIVE_TOPOLOGY_LINE_LIST", source)

    def test_native_replay_retains_linear_render_target_feedback(self) -> None:
        source = presenter_source_text()

        self.assertIn("texture_image_rects", source)
        self.assertIn("nv2a_texture_extent", source)
        self.assertIn(
            "normalize_presented_linear_texture_coordinates",
            source,
        )
        self.assertIn("render_target_feedback", source)
        self.assertIn("record_render_target_feedback", source)
        self.assertIn("render_target_feedback_texture_matches_spec", source)
        self.assertIn(
            "nv2a_render_target_feedback_formats_compatible",
            source,
        )
        self.assertIn(
            "nv2a_render_target_feedback_format_matches",
            source,
        )
        self.assertIn("active_count != specs.size()", source)
        self.assertIn("|| texture.render_target_feedback", source)
        self.assertIn("const bool already_retained", source)
        self.assertIn('"render_target_feedback_stale"', source)
        self.assertIn('"render_target_feedback_missing"', source)
        self.assertIn('"render_target_feedback_required_addresses"', source)
        self.assertIn("vkCmdCopyImage(", source)
        self.assertIn("vkCmdBlitImage(", source)
        self.assertIn(
            "swapchain_extent_.height == 480u && height == 448u",
            source,
        )
        self.assertIn("VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT", source)
        self.assertIn("VK_IMAGE_USAGE_TRANSFER_SRC_BIT", source)

    def test_native_replay_resolves_aliased_offscreen_shadow_targets(self) -> None:
        source = presenter_source_text()

        self.assertIn("nv2a_canonical_resource_address", source)
        self.assertIn("offscreen_produced", source)
        self.assertIn("record_offscreen_render_targets", source)
        self.assertIn("host_offscreen_target_replay_supported", source)
        self.assertIn("offscreen_render_target_replay_draw_count", source)
        self.assertIn("VK_DYNAMIC_STATE_VIEWPORT", source)
        self.assertIn("create_depth_attachment", source)
        self.assertIn("target.depth_view", source)
        self.assertIn("VK_PIPELINE_STAGE_LATE_FRAGMENT_TESTS_BIT", source)
        self.assertIn('"dedicated_depth_count"', source)
        self.assertIn('"shares_presented_depth", json_bool(false)', source)

    def test_live_reload_reuses_only_exact_offscreen_attachments(self) -> None:
        source = presenter_source_text()
        preserve_begin = source.index("const bool preserve_offscreen_render_targets =")
        preserve_end = source.index(
            "destroy_native_render_resources(",
            preserve_begin,
        )
        preserve_block = source[preserve_begin:preserve_end]

        self.assertIn("VkImageView color_view = VK_NULL_HANDLE", source)
        self.assertIn("texture.image == target.color_image", source)
        self.assertIn("target.color_image = texture->image", source)
        self.assertIn("!offscreen_render_targets_.empty()", preserve_block)
        self.assertIn(
            "offscreen_render_targets_match_presented_specs()",
            preserve_block,
        )
        self.assertNotIn(
            "resources_unchanged",
            preserve_block,
        )
        self.assertNotIn(
            "render_target_feedback_refresh_required()",
            preserve_block,
        )
        self.assertIn(
            '"offscreen target backing texture changed during refresh"',
            source,
        )
        self.assertIn('"offscreen_render_target_count"', source)

    def test_native_pipeline_preserves_captured_nv2a_blend_state(self) -> None:
        source = presenter_source_text()
        pipeline_header = Path("runtime/host/native_pipeline_state.h").read_text(encoding="utf-8")

        pipeline_state = pipeline_header.split("struct NativePipelineState", 1)[1].split(
            "};",
            1,
        )[0]
        self.assertIn("blend_source_factor", pipeline_state)
        self.assertIn("blend_destination_factor", pipeline_state)
        self.assertIn("blend_equation", pipeline_state)
        self.assertIn(
            "case 0x0304u: return VK_BLEND_FACTOR_DST_ALPHA;",
            source,
        )
        self.assertIn(
            "case 0x800Bu: return VK_BLEND_OP_REVERSE_SUBTRACT;",
            source,
        )
        self.assertIn(
            "state.blend_source_factor,\n            VK_BLEND_FACTOR_ONE",
            source,
        )
        self.assertIn("nv2a_blend_op(state.blend_equation)", source)
        self.assertNotIn(
            "blend_attachment.srcColorBlendFactor = VK_BLEND_FACTOR_SRC_ALPHA;",
            source,
        )

    def test_native_front_face_preserves_guest_screen_winding(self) -> None:
        source = presenter_source_text()
        front_face = source.split("VkFrontFace nv2a_front_face", 1)[1].split("}", 1)[0]

        self.assertIn("face == 0x0900u", front_face)
        self.assertIn("? VK_FRONT_FACE_CLOCKWISE", front_face)
        self.assertIn(": VK_FRONT_FACE_COUNTER_CLOCKWISE", front_face)

    def test_native_swapchain_preserves_nv2a_combiner_byte_values(self) -> None:
        source = presenter_source_text()
        choose_format = source.split("choose_surface_format", 1)[1].split(
            "VkExtent2D choose_extent", 1
        )[0]

        self.assertLess(
            choose_format.index("VK_FORMAT_B8G8R8A8_UNORM"),
            choose_format.index("VK_FORMAT_B8G8R8A8_SRGB"),
        )
        self.assertIn("xbox_combiner_unorm_output", source)

    def test_strict_render_validation_rejects_white_fallback_textures(self) -> None:
        host_source = presenter_source_text()
        runner_source = Path("tools/host/first_frame_smoke.py").read_text(encoding="utf-8")

        self.assertIn("--strict-render-validation", host_source)
        self.assertIn("unmatched_presented_texture_draw_count", host_source)
        self.assertIn("strict render validation failed", host_source)
        self.assertIn("--strict-render-validation", runner_source)

    def test_lossless_flip_ack_uses_share_friendly_binary_token(self) -> None:
        source = presenter_source_text()

        self.assertIn("cannot publish lossless flip audit acknowledgement", source)
        self.assertIn('std::memcpy(ack.data(), "B2ACK001", 8)', source)
        self.assertIn("FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE", source)
        self.assertIn('ack_transport", json_string("binary")', source)

    def test_lossless_flip_health_checks_are_sampled_and_candidate_triggered(self) -> None:
        source = presenter_source_text()

        self.assertIn("flip_audit_health_interval = 30u", source)
        self.assertIn('return "resource_changed"', source)
        self.assertIn('return "command_shape_changed"', source)
        self.assertIn('return "periodic_sample"', source)
        self.assertIn("const bool record_frame_readback", source)
        self.assertIn('health_check_reason", json_string', source)

    def test_native_replay_maps_top_left_guest_y_to_negative_ndc(self) -> None:
        source = presenter_source_text()

        self.assertIn(
            "static_cast<float>(target_height) - 1.0f",
            source,
        )
        self.assertNotIn(
            "vertex.y = 1.0f - vertex.y * 2.0f / static_cast<float>(swapchain_extent_.height)",
            source,
        )

    def test_native_replay_recovers_persistent_half_surface_splash_quad(self) -> None:
        source = presenter_source_text()

        self.assertIn("recover_presented_half_surface_quad", source)
        self.assertIn("vertex.x *= 2.0f", source)
        self.assertIn("vertex.u = (vertex.u - 0.5f) * 2.0f", source)
        self.assertIn("presented_half_quad_recovered", source)

    def test_native_replay_expands_observed_448_line_present_quad(self) -> None:
        source = presenter_source_text()

        self.assertIn("output_height == 480u", source)
        self.assertIn("nearly_equal(max_y, 448.0f)", source)
        self.assertIn("static_cast<float>(output_height) / max_y", source)
        self.assertIn("presented_overscan_height_recovered", source)

    def test_native_manual_mode_accepts_zero_as_unlimited_frames(self) -> None:
        host_source = presenter_source_text()
        runner_source = Path("tools/host/first_frame_smoke.py").read_text(encoding="utf-8")

        self.assertIn(
            "options_.max_frames == 0u || frame_count_ < options_.max_frames", host_source
        )
        self.assertNotIn("--max-frames must be greater than zero", host_source)
        self.assertIn("timeout=timeout_seconds if timeout_seconds > 0 else None", runner_source)

    def test_f12_captures_unique_current_frame_screenshots(self) -> None:
        host_source = presenter_source_text()
        runner_source = Path("tools/host/first_frame_smoke.py").read_text(encoding="utf-8")

        self.assertIn("key.key == SDLK_F12", host_source)
        self.assertIn("hotkey_screenshot_pending_ = true", host_source)
        self.assertIn("next_hotkey_screenshot_path()", host_source)
        self.assertIn("retain_hotkey_render_capture", host_source)
        self.assertIn("hotkey_render_capture_retained", host_source)
        self.assertIn('capture_screenshot(pending_hotkey_screenshot_path_, "f12")', host_source)
        self.assertIn('"render_capture_manifest"', host_source)
        self.assertIn("--hotkey-screenshot-directory", host_source)
        self.assertIn("DEFAULT_HOTKEY_SCREENSHOT_DIR", runner_source)

    def test_f11_writes_complete_metrics_snapshot(self) -> None:
        host_source = presenter_source_text()
        runner_source = Path("tools/host/first_frame_smoke.py").read_text(encoding="utf-8")

        self.assertIn("key.key == SDLK_F11", host_source)
        self.assertIn("write_metrics_report()", host_source)
        self.assertIn("--metrics-report-directory", host_source)
        self.assertIn("DEFAULT_METRICS_REPORT_DIR", runner_source)
        self.assertIn("vkCmdWriteTimestamp", host_source)
        self.assertIn("vkGetQueryPoolResults", host_source)
        for label in (
            "guest FPS:",
            "guest FPS sample flips:",
            "guest FPS sample seconds:",
            "guest completed flips:",
            "presenter FPS:",
            "guest instructions:",
            "compiled blocks / invalidations:",
            "push-buffer commands:",
            "draws:",
            "triangles:",
            "pipeline creations:",
            "pipeline cache misses:",
            "descriptor allocations:",
            "command buffers:",
            "queue submissions:",
            "barriers:",
            "uploads MB:",
            "readbacks MB:",
            "CPU render ms:",
            "window message pump ms:",
            "controller poll ms:",
            "keyboard latch ms:",
            "audio submit ms:",
            "reload probe ms:",
            "pre-render unattributed ms:",
            "GPU frame ms:",
            "fence-wait ms:",
        ):
            self.assertIn(label, host_source)

    def test_f9_toggles_completed_guest_frame_fps_counter(self) -> None:
        host_source = presenter_source_text()
        metrics_source = Path("runtime/host/frame_metrics.h").read_text(encoding="utf-8")

        self.assertIn("key.key == SDLK_F8", host_source)
        self.assertIn("result.request_replay_capture = true", host_source)
        self.assertIn("transport_.request_replay_capture()", host_source)
        self.assertIn("Replay capture ARMED (F8 to capture)", host_source)
        self.assertIn("key.key == SDLK_F9", host_source)
        self.assertIn("key.key == SDLK_F10", host_source)
        self.assertIn("result.toggle_hot_path_profile = true", host_source)
        self.assertIn("fps_counter_enabled_ = !fps_counter_enabled_", host_source)
        self.assertIn("CompletedFlipFpsSampler", host_source)
        self.assertIn("std::chrono::seconds(1)", metrics_source)
        self.assertIn("current_manifest_guest_flip_count_", host_source)
        self.assertIn("update_guest_fps_sample(now)", host_source)
        self.assertIn("guest_fps_sample_valid_", host_source)
        self.assertNotIn("if (!fps_counter_enabled_)", host_source)
        self.assertIn('" | Game FPS: "', host_source)
        self.assertIn("SDL_SetWindowTitle", host_source)
        self.assertIn('"fps_counter_toggled"', host_source)
        self.assertIn('json_string("completed_guest_flips")', host_source)

    def test_presenter_runner_forwards_metrics_report_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            executable = root / "presenter.exe"
            report_dir = root / "reports" / "local"
            executable.touch()
            completed = Mock(returncode=0, stdout="", stderr="")
            with patch(
                "tools.host.first_frame_smoke.subprocess.run",
                return_value=completed,
            ) as run:
                run_first_frame(
                    executable=executable,
                    debug_json=None,
                    max_frames=0,
                    timeout_seconds=0,
                    screenshot=None,
                    hotkey_screenshot_directory=None,
                    metrics_report_directory=report_dir,
                )

            command = run.call_args.args[0]
            self.assertEqual(
                command[command.index("--metrics-report-directory") + 1],
                str(report_dir),
            )

    def test_f12_retains_only_the_completed_flip_command_prefix(self) -> None:
        source = presenter_source_text()

        self.assertIn("command_snapshot_source_record_count", source)
        self.assertIn("command_snapshot_trimmed_record_count", source)
        self.assertIn("command_snapshot_exact_prefix", source)
        self.assertIn("std::filesystem::resize_file", source)
        self.assertIn(
            "commands.resize(static_cast<size_t>(declared_record_count))",
            source,
        )
        self.assertIn("native_draw_interpreter_bootstrap_methods", source)
        self.assertIn("interpreter-bootstrap.bin", source)
        self.assertIn("interpreter_bootstrap_path", source)
        self.assertIn("load_interpreter_bootstrap_state", source)

    def test_native_replay_uses_latest_completed_guest_flip(self) -> None:
        source = presenter_source_text()

        self.assertIn("if (method == 0x012Cu)", source)
        self.assertIn("presented_draw_begin = interpreted.frame_draw_begin", source)
        self.assertIn("draw_index < end_draw", source)

    def test_live_replay_accepts_acknowledged_command_continuation_epochs(self) -> None:
        source = presenter_source_text()

        self.assertIn("command_snapshot_base_record_count", source)
        self.assertIn("command_read_begin = interpreted_source_command_count_", source)
        self.assertIn(
            "first_record_count - snapshot_base_record_count",
            source,
        )
        self.assertIn("options_.analyze_render_stream_only", source)
        self.assertIn(
            "current_manifest_command_base_count_",
            source,
        )
        self.assertIn("render_stream_continuation_bootstrapped", source)
        self.assertIn(
            "live render continuation snapshot cannot bootstrap a new presenter",
            source,
        )
        self.assertIn(
            "live render continuation snapshot skipped uninterpreted commands",
            source,
        )

    def test_renderer_translates_texture_alpha_and_fixed_function_state(self) -> None:
        host_source = presenter_source_text()
        vertex_shader = Path("runtime/host/shaders/nv2a_inline.vert").read_text(encoding="utf-8")
        fragment_shader = Path("runtime/host/shaders/nv2a_inline.frag").read_text(encoding="utf-8")

        self.assertIn("register_offset == 0x08u", host_source)
        self.assertIn("register_offset == 0x14u", host_source)
        self.assertIn("VK_SAMPLER_ADDRESS_MODE_REPEAT", host_source)
        self.assertIn("VK_SAMPLER_ADDRESS_MODE_MIRRORED_REPEAT", host_source)
        self.assertIn("host_texture_bindings_", host_source)
        self.assertIn("HostTextureBindingSpec", host_source)
        self.assertIn("draw.texture_filters[stage]", host_source)
        self.assertIn("decompress_dxt_mip_chain", host_source)
        self.assertIn("image_info.mipLevels = texture.mip_levels", host_source)
        self.assertIn("sampler_info.mipmapMode", host_source)
        self.assertIn("sampler_info.mipLodBias", host_source)
        self.assertIn("convert_bgra8_texture", host_source)
        self.assertIn("convert_r5g6b5_texture", host_source)
        self.assertIn("unswizzle_texture_2d", host_source)
        self.assertIn("opaque_x8_alpha_forced", host_source)
        self.assertIn("method == 0x0300u", host_source)
        self.assertIn("method == 0x033Cu", host_source)
        self.assertIn("method == 0x0340u", host_source)
        self.assertIn("method >= 0x0680u && method <= 0x06BCu", host_source)
        self.assertIn("execute_presented_fixed_function_transform", host_source)
        self.assertIn("fixed_function_transformed_draw_count", host_source)
        self.assertIn(
            "default_fixed_function_texture_combiner_recovery_required",
            host_source,
        )
        self.assertIn("state.combiner_color_inputs[0] = 0x08040000u", host_source)
        self.assertIn("state.combiner_alpha_inputs[0] = 0x18140000u", host_source)
        self.assertIn("vkCmdPushConstants", host_source)
        self.assertIn("layout(location = 2) in vec4 in_uv", vertex_shader)
        self.assertIn("sampleTexture2DProjective", fragment_shader)
        self.assertIn("sampleTextureCube", fragment_shader)
        self.assertIn("state.texture_alpha_kill_mask", fragment_shader)
        self.assertIn("state.texture_opaque_alpha_mask", fragment_shader)
        self.assertLess(
            fragment_shader.index("sampled.a = 1.0"),
            fragment_shader.index("state.texture_alpha_kill_mask"),
        )
        self.assertIn("discard", fragment_shader)

    def test_native_alpha_kill_requires_an_active_texture_stage(self) -> None:
        host_source = presenter_source_text()
        fragment_shader = Path("runtime/host/shaders/nv2a_inline.frag").read_text(encoding="utf-8")

        self.assertIn(
            "state.texture_modes[stage] = enabled",
            host_source,
        )
        self.assertIn(
            "state.texture_alpha_kill_mask |=",
            host_source,
        )
        self.assertIn(
            "if ((state.texture_alpha_kill_mask & (1u << stage)) != 0u\n"
            "            && sampled.a == 0.0)",
            fragment_shader,
        )

    def test_renderer_replays_multistage_cubemap_textures(self) -> None:
        host_source = presenter_source_text()
        vertex_shader = Path("runtime/host/shaders/nv2a_inline.vert").read_text(encoding="utf-8")
        fragment_shader = Path("runtime/host/shaders/nv2a_inline.frag").read_text(encoding="utf-8")

        self.assertIn("nv2a_texture_format_is_cubemap", host_source)
        self.assertIn("VK_IMAGE_CREATE_CUBE_COMPATIBLE_BIT", host_source)
        self.assertIn("VK_IMAGE_VIEW_TYPE_CUBE", host_source)
        self.assertIn("VK_IMAGE_VIEW_TYPE_2D", host_source)
        self.assertIn("cubemap_face_producer_addresses", host_source)
        self.assertIn('"render_target_feedback_cubemaps"', host_source)
        self.assertIn('"cubemap_face_count"', host_source)
        self.assertIn("descriptor_count * 8u", host_source)
        self.assertIn("host_texture_matches_stage", host_source)
        self.assertIn(
            "nv2a_canonical_resource_address(\n                draw.texture_offsets[stage])",
            host_source,
        )
        self.assertIn(
            "nv2a_canonical_resource_address(\n"
            "                            draw.texture_offsets[stage])\n"
            "                            == nv2a_canonical_resource_address(\n"
            "                                resource.address)",
            host_source,
        )
        self.assertNotIn(
            "nv2a_canonical_resource_address(\n                draw.texture_addresses[stage])",
            host_source,
        )
        self.assertIn("texture_linears", vertex_shader)
        self.assertIn("outputs[texture_output]", vertex_shader)
        self.assertIn("uniform samplerCube textureCube1", fragment_shader)
        self.assertIn("state.texture_modes[stage]", fragment_shader)
        self.assertIn("texture_mode == 3u", fragment_shader)
        self.assertIn("registers[8u + stage] = sampled", fragment_shader)

    def test_native_replay_overlays_recovered_text_on_rendered_frames(self) -> None:
        source = presenter_source_text()

        self.assertIn(
            "frontend_text_rectangle_count_ = append_frontend_text_vertices(",
            source,
        )
        self.assertNotIn("if (interpreted_stream_.draws.empty())", source)
        self.assertIn("frontend_text_pipeline_state()", source)
        self.assertIn(
            "vkCmdDraw(\n        command_buffer,\n        frontend_text_vertex_count_", source
        )
        self.assertNotIn("vkCmdClearAttachments", source)
        self.assertIn("rasterize_frontend_text", source)
        self.assertIn('L"Impact"', source)
        self.assertIn("recovered_source_.frontend_text_x_bits", source)
        self.assertIn("recovered_source_.frontend_text_color_argb", source)
        self.assertIn("vertex.a = alpha * color_a", source)
        self.assertIn("state.blend_source_factor = 0x0302u", source)
        self.assertIn("json_string(recovered_frontend_text_)", source)

    def test_native_push_buffer_packets_carry_only_across_contiguous_appends(self) -> None:
        source = presenter_source_text()

        self.assertIn("uint32_t run_id = 0", source)
        self.assertIn("push_buffer_word_starts_run(words, index)", source)
        self.assertIn(
            "words[index - 1u].run_id == packet_begin->run_id",
            source,
        )
        self.assertIn(
            "packet_begin->run_id == (packet_end - 1)->run_id",
            source,
        )
        self.assertIn("interpret_pending_push_buffer_method_packet", source)
        self.assertIn("words[index].address != interpreted.pending_next_address", source)

    def test_native_replay_retains_full_dynamic_push_buffer_aperture(self) -> None:
        source = presenter_source_text()

        self.assertIn(
            "kRecoveredPushBufferApertureSize = 0x01000000u",
            source,
        )
        self.assertIn("std::vector<uint8_t> pending_bytes(aperture_span)", source)
        self.assertNotIn("std::array<uint8_t, 0x10000> pending_bytes", source)

    def test_live_vertex_upload_reuses_mapping_and_compacts_presented_span(self) -> None:
        source = presenter_source_text()

        self.assertIn("void* vertex_mapped_ = nullptr", source)
        self.assertIn("if (!vertex_mapped_)", source)
        self.assertIn("presented_vertex_span(", source)
        self.assertIn("uploaded_vertex_base_", source)
        self.assertIn("uploaded_vertex_count_", source)
        self.assertIn("draw.first_vertex - uploaded_vertex_base_", source)
        self.assertIn('"uploaded_vertices"', source)

    def test_inspects_and_summarizes_pixel_readback_bmp(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            screenshot = Path(temp_dir) / "frame.bmp"
            pixels = bytes((3, 2, 1, 255))
            screenshot.write_bytes(
                struct.pack("<2sIHHI", b"BM", 54 + len(pixels), 0, 0, 54)
                + struct.pack("<IiiHHIIiiII", 40, 1, -1, 1, 32, 0, len(pixels), 2835, 2835, 0, 0)
                + pixels
            )
            inspected = inspect_bmp(screenshot)
            summary = summarize_smoke(
                compile_result=None,
                run_result={
                    "returncode": 0,
                    "debug_json": "events.jsonl",
                    "screenshot": str(screenshot),
                    "events": [
                        {
                            "event": "frame_readback_captured",
                            "trigger": "automatic",
                            "unique_colors": 1,
                            "dominant_rgba": 0x010203FF,
                            "dominant_count": 1,
                        },
                        {
                            "event": "hotkey_render_capture_retained",
                            "manifest": "reports/local/screenshots/capture-render/render.json",
                        },
                        {
                            "event": "frame_readback_captured",
                            "trigger": "f12",
                            "output": "reports/local/screenshots/capture.bmp",
                            "unique_colors": 1,
                            "dominant_rgba": 0x010203FF,
                            "dominant_count": 1,
                        },
                    ],
                },
            )

        self.assertEqual(inspected["width"], 1)
        self.assertEqual(inspected["height"], 1)
        self.assertEqual(inspected["bits_per_pixel"], 32)
        self.assertTrue(summary["run"]["pixel_readback_captured"])
        self.assertEqual(summary["run"]["readback_unique_colors"], 1)
        self.assertEqual(summary["run"]["readback_dominant_rgba"], 0x010203FF)
        self.assertEqual(summary["run"]["readback_dominant_count"], 1)
        self.assertEqual(summary["run"]["hotkey_screenshot_count"], 1)
        self.assertEqual(
            summary["run"]["hotkey_screenshot_outputs"],
            ["reports/local/screenshots/capture.bmp"],
        )
        self.assertEqual(summary["run"]["hotkey_render_capture_count"], 1)
        self.assertEqual(
            summary["run"]["hotkey_render_capture_manifests"],
            ["reports/local/screenshots/capture-render/render.json"],
        )

    def test_build_command_targets_windows_vulkan_dependencies(self) -> None:
        toolchain = Toolchain(
            clangxx=Path("C:/Program Files/LLVM/bin/clang++.exe"),
            vulkan_sdk=Path("C:/VulkanSDK/1.4.341.1"),
        )

        command = build_command(
            source=Path("runtime/host/vulkan_first_frame.cpp"),
            output=Path("build/local/first-frame/b2_first_frame.exe"),
            toolchain=toolchain,
        )

        self.assertIn("-std=c++17", command)
        self.assertIn("-O2", command)
        self.assertIn("-DUNICODE", command)
        self.assertIn("-D_UNICODE", command)
        self.assertIn("-lvulkan-1", command)
        self.assertIn("-luser32", command)
        self.assertIn("-lgdi32", command)
        self.assertIn("-lshell32", command)
        self.assertIn("-lSDL3", command)
        self.assertNotIn("-lwinmm", command)
        normalized = [item.replace("\\", "/") for item in command]
        self.assertTrue(any(item.startswith("-IC:/VulkanSDK/1.4.341.1") for item in normalized))

    def test_reads_jsonl_debug_events(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "events.jsonl"
            path.write_text(
                '{"sequence":0,"event":"main_loop_enter"}\n'
                '{"sequence":1,"event":"frame_presented","frame":1}\n',
                encoding="utf-8",
            )

            events = read_debug_events(path)

        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["event"], "main_loop_enter")
        self.assertEqual(events[1]["frame"], 1)

    def test_summarizes_first_frame_and_input_evidence(self) -> None:
        run_result = {
            "returncode": 0,
            "debug_json": "reports/local/first-frame/events.jsonl",
            "events": [
                {"sequence": 0, "event": "startup"},
                {"sequence": 1, "event": "physical_device_selected", "name": "GPU"},
                {"sequence": 2, "event": "main_loop_enter"},
                {"sequence": 3, "event": "input_injected", "virtual_key": 32},
                {
                    "sequence": 4,
                    "event": "input_event",
                    "message": "keydown",
                    "virtual_key": 32,
                    "frame": 1,
                },
                {
                    "sequence": 5,
                    "event": "input_event",
                    "message": "keyup",
                    "virtual_key": 32,
                    "frame": 1,
                },
                {
                    "sequence": 6,
                    "event": "input_injection_processed",
                    "messages": 2,
                    "input_events": 2,
                    "before_frame": 1,
                },
                {
                    "sequence": 7,
                    "event": "recovered_d3d_command_stream_loaded",
                    "commands": 6,
                    "mmio_writes": 4,
                    "push_buffer_writes": 2,
                    "source": "reports/local/render/recovered-d3d-stream.json",
                },
                {
                    "sequence": 8,
                    "event": "d3d8_stream_interpreted",
                    "push_buffer_words": 2,
                    "method_packets": 1,
                    "interpreted_methods": 1,
                    "zero_count_method_words": 0,
                    "surface_payload_samples": 2,
                    "surface_payload_dominant_count": 1,
                    "surface_payload_color_valid": "false",
                    "diagnostic_clear_source": "d3d8_clear_color_method",
                    "clear_color_valid": "true",
                    "translation_semantics": "d3d8-nv2a-method-push-buffer-interpretation",
                },
                {
                    "sequence": 9,
                    "event": "translated_render_work_recorded",
                    "commands": 6,
                    "method_packets": 1,
                    "interpreted_methods": 1,
                    "translation_semantics": "d3d8-nv2a-method-push-buffer-interpretation",
                },
                {
                    "sequence": 10,
                    "event": "nv2a_native_resources_created",
                    "vertices": 8,
                    "draws": 2,
                    "presented_draws": 1,
                    "guest_flips": 2,
                    "textures": 1,
                    "presented_half_quad_recovered": True,
                    "presented_overscan_height_recovered": True,
                },
                {
                    "sequence": 11,
                    "event": "frontend_text_draw_recorded",
                    "text": "Recovered frontend",
                    "rectangles": 42,
                },
                {
                    "sequence": 12,
                    "event": "frame_presented",
                    "frame": 1,
                    "translated_commands": 6,
                    "source_d3d_commands": 6,
                    "target_frame_ms": 16,
                    "pacing_sleep_ms": 12,
                },
                {"sequence": 13, "event": "main_loop_exit"},
            ],
        }
        compile_result = {
            "returncode": 0,
            "output": "build/local/first-frame/b2_first_frame.exe",
        }

        summary = summarize_smoke(
            compile_result=compile_result,
            run_result=run_result,
        )

        self.assertEqual(summary["format"], "b2-recomp-first-frame-smoke")
        self.assertEqual(summary["target_platform"], "windows")
        self.assertEqual(summary["renderer_backend"], "vulkan")
        self.assertTrue(summary["build"]["compiled"])
        self.assertTrue(summary["run"]["main_loop_entered"])
        self.assertTrue(summary["run"]["main_loop_exited"])
        self.assertTrue(summary["run"]["visible_frame_presented"])
        self.assertTrue(summary["run"]["recovered_frontend_text_drawn"])
        self.assertEqual(summary["run"]["recovered_frontend_text"], "Recovered frontend")
        self.assertEqual(summary["run"]["frontend_text_rectangles"], 42)
        self.assertEqual(summary["run"]["nv2a_presented_draws"], 1)
        self.assertEqual(summary["run"]["nv2a_guest_flips"], 2)
        self.assertTrue(summary["run"]["nv2a_presented_half_quad_recovered"])
        self.assertTrue(summary["run"]["nv2a_presented_overscan_height_recovered"])
        self.assertEqual(summary["run"]["frames_presented"], 1)
        self.assertEqual(summary["run"]["input_events"], 2)
        self.assertEqual(summary["run"]["input_keydown_events"], 1)
        self.assertEqual(summary["run"]["input_keyup_events"], 1)
        self.assertTrue(summary["run"]["injected_input_processed_before_first_frame"])
        self.assertEqual(summary["run"]["injected_input_processed_messages"], 2)
        self.assertTrue(summary["run"]["translated_renderer_work"])
        self.assertEqual(summary["run"]["translated_command_count"], 6)
        self.assertTrue(summary["run"]["recovered_d3d_command_stream"])
        self.assertEqual(summary["run"]["recovered_d3d_command_count"], 6)
        self.assertEqual(
            summary["run"]["recovered_d3d_stream_source"],
            "reports/local/render/recovered-d3d-stream.json",
        )
        self.assertEqual(summary["run"]["recovered_d3d_mmio_writes"], 4)
        self.assertEqual(summary["run"]["recovered_d3d_push_buffer_writes"], 2)
        self.assertTrue(summary["run"]["d3d8_method_interpretation"])
        self.assertEqual(
            summary["run"]["d3d8_translation_semantics"],
            "d3d8-nv2a-method-push-buffer-interpretation",
        )
        self.assertEqual(summary["run"]["d3d8_push_buffer_words"], 2)
        self.assertEqual(summary["run"]["d3d8_method_packets"], 1)
        self.assertEqual(summary["run"]["d3d8_interpreted_methods"], 1)
        self.assertEqual(summary["run"]["d3d8_zero_count_method_words"], 0)
        self.assertEqual(summary["run"]["d3d8_surface_payload_samples"], 2)
        self.assertEqual(summary["run"]["d3d8_surface_payload_dominant_count"], 1)
        self.assertFalse(summary["run"]["d3d8_surface_payload_color_valid"])
        self.assertEqual(
            summary["run"]["d3d8_diagnostic_clear_source"],
            "d3d8_clear_color_method",
        )
        self.assertTrue(summary["run"]["d3d8_clear_color_valid"])
        self.assertEqual(summary["run"]["frame_pacing"]["target_frame_ms"], 16)
        self.assertEqual(summary["run"]["frame_pacing"]["events_with_pacing"], 1)
        self.assertEqual(summary["run"]["selected_device"]["name"], "GPU")


if __name__ == "__main__":
    unittest.main()
