cmake_minimum_required(VERSION 3.28)
project(burnout2 VERSION 1.0.0 LANGUAGES C CXX)

if(NOT WIN32 OR NOT CMAKE_SIZEOF_VOID_P EQUAL 8)
    message(FATAL_ERROR "The initial target is Windows x64.")
endif()
set(CMAKE_CXX_STANDARD 23)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_CXX_SCAN_FOR_MODULES OFF)
include(CheckIPOSupported)
check_ipo_supported(RESULT b2_ipo_supported LANGUAGES CXX)
set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS
    "${CMAKE_CURRENT_SOURCE_DIR}/knowledge/supported_target.json"
    "${CMAKE_CURRENT_SOURCE_DIR}/knowledge/facts.json")

file(READ "${CMAKE_CURRENT_SOURCE_DIR}/knowledge/supported_target.json" target_json)
foreach(field IN ITEMS target_id file_size file_sha256)
    string(JSON B2_${field} GET "${target_json}" targets 0 ${field})
endforeach()

configure_file(cmake/target.h.in generated/target.h @ONLY)
file(READ "${CMAKE_CURRENT_SOURCE_DIR}/knowledge/facts.json" facts_json)
string(JSON kernel_name_count LENGTH "${facts_json}" executable KERNEL_EXPORT_NAMES value items)
math(EXPR kernel_name_last "${kernel_name_count} - 1")
set(kernel_names "")
foreach(index RANGE ${kernel_name_last})
    string(JSON ordinal GET "${facts_json}" executable KERNEL_EXPORT_NAMES value items ${index} 0)
    string(JSON name GET "${facts_json}" executable KERNEL_EXPORT_NAMES value items ${index} 1)
    string(APPEND kernel_names "    case ${ordinal}: return \"${name}\";\n")
endforeach()
configure_file(cmake/kernel_names.h.in generated/kernel_names.h @ONLY)

include(FetchContent)
set(ZYDIS_BUILD_TOOLS OFF CACHE BOOL "" FORCE)
set(ZYDIS_BUILD_EXAMPLES OFF CACHE BOOL "" FORCE)
set(ZYDIS_BUILD_DOXYGEN OFF CACHE BOOL "" FORCE)
set(ZYDIS_BUILD_TESTS OFF CACHE BOOL "" FORCE)
set(ZYDIS_BUILD_SHARED_LIB OFF CACHE BOOL "" FORCE)
set(ZYDIS_FEATURE_ENCODER OFF CACHE BOOL "" FORCE)
FetchContent_Declare(zydis
    GIT_REPOSITORY https://github.com/zyantific/zydis.git
    GIT_TAG a2278f1d254e492f6a6b39f6cb5d1f5d515659dc
    GIT_SUBMODULES dependencies/zycore
    GIT_PROGRESS FALSE)
FetchContent_MakeAvailable(zydis)

add_library(b2-core STATIC src/file.cpp src/disc.cpp src/xbe.cpp src/assets.cpp src/effects.cpp src/diagnostics.cpp src/crypto.cpp)
target_include_directories(b2-core PUBLIC src PRIVATE "${CMAKE_CURRENT_BINARY_DIR}/generated")
target_link_libraries(b2-core PRIVATE bcrypt)
target_compile_definitions(b2-core PUBLIC WIN32_LEAN_AND_MEAN NOMINMAX UNICODE _UNICODE)

add_library(b2-graphics STATIC src/render.cpp src/render_checks.cpp src/vertex_program.cpp src/shaders.cpp src/gpu.cpp)
target_link_libraries(b2-graphics PUBLIC b2-core PRIVATE d3d11 dxgi windowscodecs ole32)

add_executable(b2-tool src/tool.cpp src/decoder.cpp src/recompiler.cpp src/batch.cpp src/dsp_compiler.cpp src/shader_compiler.cpp)
target_include_directories(b2-tool PRIVATE "${CMAKE_CURRENT_BINARY_DIR}/generated")
target_link_libraries(b2-tool PRIVATE b2-core b2-graphics Zydis d3dcompiler)
add_custom_command(TARGET b2-tool POST_BUILD
    COMMAND "${CMAKE_COMMAND}" -E copy_if_different
        "${CMAKE_CURRENT_SOURCE_DIR}/THIRD_PARTY_NOTICES.txt" "$<TARGET_FILE_DIR:b2-tool>/THIRD_PARTY_NOTICES.txt"
    VERBATIM)

set(B2_XBE "" CACHE FILEPATH "Verified local executable for native CPU diagnostics")
if(B2_XBE)
    if(NOT EXISTS "${B2_XBE}")
        message(FATAL_ERROR "B2_XBE does not exist; extract default.xbe with b2-tool first.")
    endif()
    enable_language(ASM_MASM)
    foreach(option IN ITEMS SDL_SHARED SDL_TEST_LIBRARY SDL_TESTS SDL_EXAMPLES SDL_INSTALL SDL_GPU SDL_RENDER SDL_OPENGL SDL_OPENGLES SDL_VULKAN)
        set(${option} OFF CACHE BOOL "" FORCE)
    endforeach()
    set(SDL_STATIC ON CACHE BOOL "" FORCE)
    FetchContent_Declare(sdl3
        URL https://github.com/libsdl-org/SDL/archive/829a65d769d935c4852f8159e964312c0957260a.tar.gz
        URL_HASH SHA256=48E15CF1A6928453D77AFA955D1FDFBBCA9169E2BC3D76D111ACB9B8BB076D66
        DOWNLOAD_EXTRACT_TIMESTAMP TRUE)
    FetchContent_MakeAvailable(sdl3)
    FetchContent_Declare(imgui
        URL https://github.com/ocornut/imgui/archive/f1cc2ae15e53a861a874c3034aae6798fde194ab.tar.gz
        URL_HASH SHA256=F30BE7F3E2136615B1848A9AFB72F6DFD8A60820DDF1C1A85997BF3B722E7CFC
        DOWNLOAD_EXTRACT_TIMESTAMP TRUE)
    FetchContent_MakeAvailable(imgui)
    include(cmake/imgui-shaders.cmake)
    add_library(b2-ui STATIC
        "${imgui_SOURCE_DIR}/imgui.cpp" "${imgui_SOURCE_DIR}/imgui_draw.cpp"
        "${imgui_SOURCE_DIR}/imgui_tables.cpp" "${imgui_SOURCE_DIR}/imgui_widgets.cpp"
        "${imgui_SOURCE_DIR}/backends/imgui_impl_sdl3.cpp" "${CMAKE_CURRENT_BINARY_DIR}/generated/imgui_impl_dx11.cpp")
    target_include_directories(b2-ui PUBLIC "${imgui_SOURCE_DIR}" "${imgui_SOURCE_DIR}/backends")
    target_include_directories(b2-ui PRIVATE src)
    target_link_libraries(b2-ui PRIVATE SDL3::SDL3-static d3d11)
    add_library(b2-input STATIC src/input.cpp src/audio.cpp)
    target_include_directories(b2-input PUBLIC src)
    target_link_libraries(b2-input PRIVATE SDL3::SDL3-static)
    add_library(b2-cpu STATIC src/cpu.cpp src/xbox.cpp src/floating.cpp src/cpu_floating.asm)
    target_include_directories(b2-cpu PUBLIC src)
    target_link_libraries(b2-cpu PUBLIC b2-core PRIVATE b2-input b2-graphics iphlpapi)
    set(B2_EFFECTS "${CMAKE_CURRENT_SOURCE_DIR}/data/local/b2distfx.bin" CACHE FILEPATH "Local Xbox audio effects image")
    if(NOT EXISTS "${B2_EFFECTS}")
        message(FATAL_ERROR "B2_EFFECTS is missing; use b2-tool extract ISO data/b2distfx.bin OUTPUT first.")
    endif()
    set(native_effects "${CMAKE_CURRENT_BINARY_DIR}/generated/$<CONFIG>/native_effects.cpp")
    add_custom_command(OUTPUT "${native_effects}"
        COMMAND b2-tool compile-effects "${B2_EFFECTS}" "${native_effects}" --replace
        DEPENDS b2-tool "${B2_EFFECTS}"
        VERBATIM)
    target_sources(b2-cpu PRIVATE src/dsp.cpp src/spatial.cpp "${native_effects}")
    add_library(b2-app STATIC src/app.cpp)
    target_link_libraries(b2-app PRIVATE b2-core b2-graphics b2-cpu b2-ui SDL3::SDL3-static comdlg32)
    string(JSON finite_address GET "${facts_json}" title TITLE_FLOAT_FINITE_CHECK_ADDRESS value)
    string(JSON compare_address GET "${facts_json}" title TITLE_FIXED_WIDTH_COMPARE_ADDRESS value)
    string(JSON main_loop_address GET "${facts_json}" title TITLE_MAIN_LOOP_ENTRY_ADDRESS value)
    target_compile_definitions(b2-cpu PRIVATE B2_MAIN_ADDRESS=${main_loop_address})
    string(JSON constructor_start GET "${facts_json}" title TITLE_CRT_CONSTRUCTOR_TABLE_START_ADDRESS value)
    string(JSON constructor_end GET "${facts_json}" title TITLE_CRT_CONSTRUCTOR_TABLE_END_ADDRESS value)
    string(JSON pair_count LENGTH "${facts_json}" title PHASE7_FUNCTION_PAIR_TABLE value)
    math(EXPR last_pair "${pair_count} - 1")
    set(startup_pair_seeds "")
    foreach(pair RANGE ${last_pair})
        foreach(slot RANGE 1)
            string(JSON pair_address GET "${facts_json}" title PHASE7_FUNCTION_PAIR_TABLE value ${pair} ${slot})
            string(APPEND startup_pair_seeds "${pair_address}\n")
        endforeach()
    endforeach()
    set(startup_table_seeds "")
    string(JSON title_fact_count LENGTH "${facts_json}" title)
    math(EXPR title_fact_last "${title_fact_count} - 1")
    foreach(index RANGE ${title_fact_last})
        string(JSON field MEMBER "${facts_json}" title ${index})
        if(field MATCHES "^PHASE7_.*_VTABLE_ENTRIES$")
            string(REGEX REPLACE "_ENTRIES$" "" table_field "${field}")
            string(JSON table_address GET "${facts_json}" title ${table_field} value)
            string(JSON table_count LENGTH "${facts_json}" title ${field} value)
            math(EXPR table_end "${table_address} + ${table_count} * 4")
            # The native generator reads and validates the original file-backed entries.
            string(APPEND startup_table_seeds "table ${table_address} ${table_end}\n")
        elseif(field STREQUAL "PHASE7_LEVEL_LOAD_MANAGER_VTABLES")
            string(JSON table_rows LENGTH "${facts_json}" title ${field} value)
            math(EXPR table_last "${table_rows} - 1")
            foreach(row RANGE ${table_last})
                string(JSON table_address GET "${facts_json}" title ${field} value ${row} 0)
                string(JSON table_count LENGTH "${facts_json}" title ${field} value ${row} 1)
                math(EXPR table_end "${table_address} + ${table_count} * 4")
                string(APPEND startup_table_seeds "table ${table_address} ${table_end}\n")
            endforeach()
        elseif(field STREQUAL "PHASE7_CAPTURED_VTABLE_SLOT_BINDINGS")
            string(JSON binding_rows LENGTH "${facts_json}" title ${field} value items)
            math(EXPR binding_last "${binding_rows} - 1")
            foreach(row RANGE ${binding_last})
                string(JSON binding_count LENGTH "${facts_json}" title ${field} value items ${row} 1)
                math(EXPR binding_end "${binding_count} - 1")
                foreach(binding RANGE ${binding_end})
                    string(JSON slot_address GET "${facts_json}" title ${field} value items ${row} 1 ${binding} 0)
                    math(EXPR slot_end "${slot_address} + 4")
                    string(APPEND startup_table_seeds "table ${slot_address} ${slot_end}\n")
                endforeach()
            endforeach()
        endif()
    endforeach()
    configure_file(cmake/cpu-seeds.txt.in generated/cpu-seeds.txt @ONLY)
    set(native_cpu "${CMAKE_CURRENT_BINARY_DIR}/generated/$<CONFIG>/native_cpu.cpp")
    set(native_shards "")
    foreach(index RANGE 0 511)
        if(index LESS 10)
            set(suffix "0${index}")
        else()
            set(suffix "${index}")
        endif()
        list(APPEND native_shards "${native_cpu}.${suffix}.cpp")
    endforeach()
    set(native_stamp "${native_cpu}.stamp")
    add_custom_command(OUTPUT "${native_stamp}"
        BYPRODUCTS "${native_cpu}" "${native_cpu}.asm" "${native_cpu}.h" "${native_cpu}.json" ${native_shards}
        COMMAND b2-tool batch "${B2_XBE}" "${CMAKE_CURRENT_BINARY_DIR}/generated/cpu-seeds.txt"
            65536 4096 "${native_cpu}" --replace --allow-partial --shards 512 --report "${native_cpu}.json"
        COMMAND "${CMAKE_COMMAND}" -E touch "${native_stamp}"
        DEPENDS b2-tool "${B2_XBE}" "${CMAKE_CURRENT_BINARY_DIR}/generated/cpu-seeds.txt"
        VERBATIM)
    add_custom_target(b2-native-generated DEPENDS "${native_stamp}")
    set(native_shaders "${CMAKE_CURRENT_BINARY_DIR}/generated/$<CONFIG>/native_shaders.cpp")
    # The generator preserves unchanged source timestamps; stamp completed work.
    set(native_shader_stamp "${native_shaders}.stamp")
    add_custom_command(OUTPUT "${native_shader_stamp}"
        BYPRODUCTS "${native_shaders}"
        COMMAND b2-tool compile-shaders "${B2_XBE}" "${native_shaders}" --replace
        COMMAND "${CMAKE_COMMAND}" -E touch "${native_shader_stamp}"
        DEPENDS b2-tool "${B2_XBE}" "${CMAKE_CURRENT_BINARY_DIR}/generated/gui_shaders.h"
        VERBATIM)
    add_custom_target(b2-shaders-generated DEPENDS "${native_shader_stamp}")
    add_executable(b2 src/main.cpp src/cpu_checks.cpp src/cpu_reference.asm "${native_cpu}" "${native_cpu}.asm" ${native_shards} "${native_shaders}")
    add_dependencies(b2 b2-native-generated b2-shaders-generated)
    target_link_libraries(b2 PRIVATE b2-core b2-graphics b2-cpu b2-app)
    target_compile_definitions(b2 PRIVATE B2_FINITE_ADDRESS=${finite_address} B2_COMPARE_ADDRESS=${compare_address})
else()
    file(WRITE "${CMAKE_CURRENT_BINARY_DIR}/generated/gui_shaders.h"
        "#pragma once\n#include <string_view>\nnamespace b2 {inline constexpr std::string_view gui_vertex_source{},gui_pixel_source{};}\n")
endif()

foreach(target IN ITEMS b2-core b2-cpu b2-input b2-graphics b2-app b2-tool b2)
    if(NOT TARGET ${target})
        continue()
    endif()
    if(MSVC)
        target_compile_options(${target} PRIVATE "$<$<COMPILE_LANGUAGE:CXX>:/W4;/permissive-;/Zc:__cplusplus;/utf-8>")
        if(target STREQUAL "b2")
            target_compile_options(${target} PRIVATE "$<$<COMPILE_LANGUAGE:CXX>:/MP2>")
            target_link_options(${target} PRIVATE "$<$<CONFIG:Release>:/DEBUG:FULL;/OPT:REF;/OPT:ICF>")
        endif()
    endif()
endforeach()

if(b2_ipo_supported)
    foreach(target IN ITEMS b2-core b2-cpu b2-input b2-graphics b2-app b2-tool b2)
        if(TARGET ${target})
            set_property(TARGET ${target} PROPERTY INTERPROCEDURAL_OPTIMIZATION_RELEASE TRUE)
        endif()
    endforeach()
endif()
