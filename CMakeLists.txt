cmake_minimum_required(VERSION 3.25)

project(b2_recomp VERSION 0.1.0 LANGUAGES CXX)

include(CTest)

option(B2R_BUILD_PRESENTER "Build the Windows Vulkan/SDL3 presenter targets" OFF)
option(B2R_ENABLE_PROFILING "Retain symbols and frame pointers for profiling" OFF)
option(B2R_ENABLE_SANITIZERS "Enable address and undefined-behavior sanitizers" OFF)
option(B2R_WARNINGS_AS_ERRORS "Treat native warnings as errors" ON)

function(b2r_configure_native_target target)
    target_compile_features(${target} PRIVATE cxx_std_17)

    if(MSVC)
        target_compile_options(${target} PRIVATE /W4 /permissive- /EHsc /Zc:__cplusplus)
        if(B2R_WARNINGS_AS_ERRORS)
            target_compile_options(${target} PRIVATE /WX)
        endif()
    elseif(CMAKE_CXX_COMPILER_ID MATCHES "Clang|GNU")
        target_compile_options(${target} PRIVATE -Wall -Wextra -Wpedantic)
        if(B2R_WARNINGS_AS_ERRORS)
            target_compile_options(${target} PRIVATE -Werror)
        endif()
    endif()

    if(B2R_ENABLE_PROFILING)
        if(MSVC)
            target_compile_options(${target} PRIVATE /Zi)
            target_link_options(${target} PRIVATE /DEBUG)
        else()
            target_compile_options(${target} PRIVATE -g -fno-omit-frame-pointer)
        endif()
    endif()

    if(B2R_ENABLE_SANITIZERS)
        if(NOT CMAKE_CXX_COMPILER_ID MATCHES "Clang|GNU")
            message(FATAL_ERROR "B2R_ENABLE_SANITIZERS requires Clang or GCC")
        endif()
        target_compile_options(
            ${target}
            PRIVATE -fsanitize=address,undefined -fno-omit-frame-pointer
        )
        target_link_options(${target} PRIVATE -fsanitize=address,undefined)

        if(WIN32 AND CMAKE_CXX_COMPILER_ID MATCHES "Clang")
            execute_process(
                COMMAND "${CMAKE_CXX_COMPILER}" --print-resource-dir
                OUTPUT_VARIABLE clang_resource_dir
                OUTPUT_STRIP_TRAILING_WHITESPACE
                COMMAND_ERROR_IS_FATAL ANY
            )
            if(CMAKE_SIZEOF_VOID_P EQUAL 8)
                set(clang_asan_arch x86_64)
            else()
                set(clang_asan_arch i386)
            endif()
            set(
                clang_asan_runtime
                "${clang_resource_dir}/lib/windows/clang_rt.asan_dynamic-${clang_asan_arch}.dll"
            )
            if(NOT EXISTS "${clang_asan_runtime}")
                message(FATAL_ERROR "Clang ASan runtime not found: ${clang_asan_runtime}")
            endif()
            add_custom_command(
                TARGET ${target}
                POST_BUILD
                COMMAND
                    "${CMAKE_COMMAND}" -E copy_if_different
                    "${clang_asan_runtime}"
                    "$<TARGET_FILE_DIR:${target}>"
                VERBATIM
            )
        endif()
    endif()
endfunction()

add_library(b2r_nv2a_vertex_program INTERFACE)
target_compile_features(b2r_nv2a_vertex_program INTERFACE cxx_std_17)
target_include_directories(
    b2r_nv2a_vertex_program
    INTERFACE
        "${CMAKE_CURRENT_SOURCE_DIR}/runtime"
        "${CMAKE_CURRENT_SOURCE_DIR}/runtime/host"
)

if(BUILD_TESTING)
    add_executable(
        b2r_native_tests
        tests/native/nv2a_vertex_program_tests.cpp
    )
    target_link_libraries(b2r_native_tests PRIVATE b2r_nv2a_vertex_program)
    b2r_configure_native_target(b2r_native_tests)

    add_executable(
        b2r_host_core_tests
        tests/native/host_core_tests.cpp
    )
    target_link_libraries(b2r_host_core_tests PRIVATE b2r_nv2a_vertex_program)
    b2r_configure_native_target(b2r_host_core_tests)

    set(B2R_NATIVE_TEST_CASES
        field_and_swizzle
        vector_operations
        write_mask
        program_execution
        invalid_program
    )
    foreach(test_case IN LISTS B2R_NATIVE_TEST_CASES)
        add_test(
            NAME "native.nv2a_vertex_program.${test_case}"
            COMMAND b2r_native_tests "${test_case}"
        )
    endforeach()

    set(B2R_HOST_CORE_TEST_CASES
        transport_layout_and_sequences
        texture_layout_and_formats
        texture_unswizzle
        dirty_range_lifetime
        pipeline_key_and_fps_sampler
    )
    foreach(test_case IN LISTS B2R_HOST_CORE_TEST_CASES)
        add_test(
            NAME "native.host_core.${test_case}"
            COMMAND b2r_host_core_tests "${test_case}"
        )
    endforeach()
endif()

if(B2R_BUILD_PRESENTER)
    if(NOT WIN32)
        message(FATAL_ERROR "The current Vulkan presenter supports Windows only")
    endif()

    find_package(Vulkan REQUIRED)
    find_path(
        B2R_SDL3_INCLUDE_DIR
        NAMES SDL3/SDL.h
        HINTS "$ENV{VULKAN_SDK}/Include"
        REQUIRED
    )
    find_library(
        B2R_SDL3_LIBRARY
        NAMES SDL3
        HINTS "$ENV{VULKAN_SDK}/Lib"
        REQUIRED
    )
    find_file(
        B2R_SDL3_RUNTIME
        NAMES SDL3.dll
        HINTS "$ENV{VULKAN_SDK}/Bin"
        REQUIRED
    )
    find_program(
        B2R_GLSLC_EXECUTABLE
        NAMES glslc
        HINTS "$ENV{VULKAN_SDK}/Bin"
        REQUIRED
    )

    set(B2R_SHADER_OUTPUT_DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}/shaders")
    set(B2R_VERTEX_SHADER "${B2R_SHADER_OUTPUT_DIRECTORY}/nv2a_inline.vert.spv")
    set(B2R_FRAGMENT_SHADER "${B2R_SHADER_OUTPUT_DIRECTORY}/nv2a_inline.frag.spv")
    set(
        B2R_TEXTURE_CONVERT_SHADER
        "${B2R_SHADER_OUTPUT_DIRECTORY}/nv2a_texture_convert.comp.spv"
    )
    set(B2R_SHADER_OUTPUTS
        "${B2R_VERTEX_SHADER}"
        "${B2R_FRAGMENT_SHADER}"
        "${B2R_TEXTURE_CONVERT_SHADER}"
    )
    set(B2R_SHADER_SOURCES
        runtime/host/shaders/nv2a_inline.vert
        runtime/host/shaders/nv2a_inline.frag
        runtime/host/shaders/nv2a_texture_convert.comp
    )
    foreach(shader_output shader_source IN ZIP_LISTS B2R_SHADER_OUTPUTS B2R_SHADER_SOURCES)
        add_custom_command(
            OUTPUT "${shader_output}"
            COMMAND "${CMAKE_COMMAND}" -E make_directory "${B2R_SHADER_OUTPUT_DIRECTORY}"
            COMMAND
                "${B2R_GLSLC_EXECUTABLE}"
                "${CMAKE_CURRENT_SOURCE_DIR}/${shader_source}"
                -o "${shader_output}"
            DEPENDS "${CMAKE_CURRENT_SOURCE_DIR}/${shader_source}"
            COMMENT "Compiling ${shader_source}"
            VERBATIM
        )
    endforeach()
    add_custom_target(b2_presenter_shaders ALL DEPENDS ${B2R_SHADER_OUTPUTS})

    set(B2R_PRESENTER_SOURCES
        runtime/host/vulkan_first_frame.cpp
        runtime/host/vulkan_presenter.cpp
        runtime/host/vulkan_renderer.cpp
        runtime/host/vulkan_resources.cpp
        runtime/host/presenter_diagnostics.cpp
        runtime/host/nv2a_command_processor.cpp
        runtime/host/live_presenter_transport.cpp
        runtime/host/presenter_debug_log.cpp
        runtime/host/presenter_metrics.cpp
        runtime/host/presenter_options.cpp
        runtime/platform/sdl/sdl_audio.cpp
        runtime/platform/sdl/sdl_audio_c_api.cpp
        runtime/platform/sdl/sdl_platform.cpp
    )
    add_executable(b2_first_frame ${B2R_PRESENTER_SOURCES})
    add_library(b2_presenter SHARED ${B2R_PRESENTER_SOURCES})
    add_dependencies(b2_first_frame b2_presenter_shaders)
    add_dependencies(b2_presenter b2_presenter_shaders)

    foreach(target IN ITEMS b2_first_frame b2_presenter)
        target_link_libraries(
            ${target}
            PRIVATE
                b2r_nv2a_vertex_program
                Vulkan::Vulkan
                "${B2R_SDL3_LIBRARY}"
                user32
                gdi32
                shell32
        )
        target_include_directories(${target} PRIVATE "${B2R_SDL3_INCLUDE_DIR}")
        b2r_configure_native_target(${target})
        add_custom_command(
            TARGET ${target}
            POST_BUILD
            COMMAND
                "${CMAKE_COMMAND}" -E copy_if_different
                "${B2R_SDL3_RUNTIME}"
                "$<TARGET_FILE_DIR:${target}>/SDL3.dll"
            VERBATIM
        )
    endforeach()

    set_target_properties(b2_presenter PROPERTIES OUTPUT_NAME b2_presenter)
endif()
