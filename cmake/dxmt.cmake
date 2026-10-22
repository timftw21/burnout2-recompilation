# Build the pinned native DXMT sources directly with CMake. Its upstream Meson
# build requires Python; the source lists below follow that same revision.
enable_language(OBJC)
find_program(b2_xcrun xcrun REQUIRED)
execute_process(COMMAND getconf PAGESIZE OUTPUT_VARIABLE dxmt_page_size OUTPUT_STRIP_TRAILING_WHITESPACE COMMAND_ERROR_IS_FATAL ANY)
if(NOT dxmt_page_size MATCHES "^(4096|16384)$")
    message(FATAL_ERROR "Unsupported native Mac page size: ${dxmt_page_size}")
endif()
set(dxmt_generated "${CMAKE_CURRENT_BINARY_DIR}/generated/dxmt")
file(MAKE_DIRECTORY "${dxmt_generated}")
file(WRITE "${dxmt_generated}/version.h" "#define DXMT_VERSION \"50102f99\"\n")
function(dxmt_sources output directory)
    file(STRINGS "${dxmt_SOURCE_DIR}/${directory}/meson.build" lines REGEX "^ *'[^']+\\.(cpp|c)'[, ]*$")
    set(sources "")
    foreach(line IN LISTS lines)
        string(REGEX REPLACE "^ *'([^']+)'.*$" "\\1" source "${line}")
        list(APPEND sources "${dxmt_SOURCE_DIR}/${directory}/${source}")
    endforeach()
    set(${output} "${sources}" PARENT_SCOPE)
endfunction()
function(dxmt_shader name path kind)
    set(ir "${dxmt_generated}/${name}.air")
    add_custom_command(OUTPUT "${ir}"
        COMMAND "${b2_xcrun}" -sdk macosx metal -std=metal3.1 --target=air64-apple-macos14.0 -c "${path}" -o "${ir}"
        DEPENDS "${path}" VERBATIM)
    set(binary "${ir}")
    if(kind STREQUAL "metallib")
        set(binary "${dxmt_generated}/${name}.metallib")
        add_custom_command(OUTPUT "${binary}" COMMAND "${b2_xcrun}" -sdk macosx metallib "${ir}" -o "${binary}" DEPENDS "${ir}" VERBATIM)
    endif()
    add_custom_command(OUTPUT "${dxmt_generated}/${name}.h"
        COMMAND "${CMAKE_COMMAND}" "-DINPUT=${binary}" "-DOUTPUT=${dxmt_generated}/${name}.h" "-DNAME=${name}" -P "${CMAKE_CURRENT_SOURCE_DIR}/cmake/binary-header.cmake"
        DEPENDS "${binary}" "${CMAKE_CURRENT_SOURCE_DIR}/cmake/binary-header.cmake" VERBATIM)
endfunction()
dxmt_shader(air_msad "${dxmt_SOURCE_DIR}/src/airconv/shaders/air_msad.metal" air)
dxmt_shader(air_samplepos "${dxmt_SOURCE_DIR}/src/airconv/shaders/air_samplepos.metal" air)
dxmt_shader(air_tessellation "${dxmt_SOURCE_DIR}/src/airconv/shaders/air_tessellation.metal" air)
dxmt_shader(dxmt_command "${dxmt_SOURCE_DIR}/src/dxmt/dxmt_command.metal" metallib)
dxmt_sources(parser src/../libs/DXBCParser)
dxmt_sources(airconv src/airconv)
dxmt_sources(util src/util)
dxmt_sources(core src/dxmt)
dxmt_sources(dxgi src/dxgi)
dxmt_sources(d3d11 src/d3d11)
# Native WCHAR follows the host's wchar_t width. The Wine path's UTF-16 copy
# produces invalid UTF-32 on macOS; use DXMT's width-aware transcoder instead.
file(READ "${dxmt_SOURCE_DIR}/src/dxgi/dxgi_adapter.cpp" adapter_source)
set(adapter_name_copy "device_.name().getCString((char *)pDesc->Description, sizeof(pDesc->Description), WMTUTF16StringEncoding);")
string(FIND "${adapter_source}" "${adapter_name_copy}" adapter_name_call)
if(adapter_name_call LESS 0)
    message(FATAL_ERROR "Pinned DXMT adapter description source changed.")
endif()
string(REPLACE "${adapter_name_copy}"
    "const auto name = device_.name().getUTF8String();\n      str::transcodeString(pDesc->Description, sizeof(pDesc->Description) / sizeof(pDesc->Description[0]) - 1, name.data(), name.size());"
    adapter_source "${adapter_source}")
file(CONFIGURE OUTPUT "${dxmt_generated}/dxgi_adapter.cpp" CONTENT "${adapter_source}" @ONLY)
list(REMOVE_ITEM dxgi "${dxmt_SOURCE_DIR}/src/dxgi/dxgi_adapter.cpp")
list(APPEND dxgi "${dxmt_generated}/dxgi_adapter.cpp")
# Intel's Metal driver requires a declared format even for vertex-only work.
# Rasterization and color writes stay disabled; no color attachment is used.
file(READ "${dxmt_SOURCE_DIR}/src/dxmt/dxmt_command.cpp" command_source)
foreach(stage IN ITEMS gs ts)
    set(marshal_rasterization "${stage}_marshal_pipeline.rasterization_enabled = false;")
    string(FIND "${command_source}" "${marshal_rasterization}" marshal_call)
    if(marshal_call LESS 0)
        message(FATAL_ERROR "Pinned DXMT vertex-only pipeline source changed.")
    endif()
    string(REPLACE "${marshal_rasterization}"
        "${marshal_rasterization}\n    ${stage}_marshal_pipeline.colors[0].pixel_format = WMTPixelFormatRGBA8Unorm;\n    ${stage}_marshal_pipeline.colors[0].write_mask = WMTColorWriteMaskNone;"
        command_source "${command_source}")
endforeach()
file(CONFIGURE OUTPUT "${dxmt_generated}/dxmt_command.cpp" CONTENT "${command_source}" @ONLY)
list(REMOVE_ITEM core "${dxmt_SOURCE_DIR}/src/dxmt/dxmt_command.cpp")
list(APPEND core "${dxmt_generated}/dxmt_command.cpp")
# The d3d11 source list already includes the six D3D10 implementation files.
list(FILTER util EXCLUDE REGEX "wsi_.*_win32\\.cpp$")
list(FILTER airconv EXCLUDE REGEX "airconv_cli\\.cpp$")
# The native allocator's upstream aligned_alloc arguments are reversed.
# POSIX allocation also accepts texture lengths that are not page multiples.
file(READ "${dxmt_SOURCE_DIR}/src/util/wsi_platform_darwin.cpp" allocator_source)
string(FIND "${allocator_source}" "return aligned_alloc(size, alignment);" allocator_call)
if(allocator_call LESS 0)
    message(FATAL_ERROR "Pinned DXMT native allocation source changed.")
endif()
string(REPLACE "return aligned_alloc(size, alignment);" "void *allocation = nullptr;\n  return posix_memalign(&allocation, alignment, size) ? nullptr : allocation;" allocator_source "${allocator_source}")
file(CONFIGURE OUTPUT "${dxmt_generated}/wsi_platform_darwin.cpp" CONTENT "${allocator_source}" @ONLY)
list(REMOVE_ITEM util "${dxmt_SOURCE_DIR}/src/util/wsi_platform_darwin.cpp")
list(APPEND util "${dxmt_generated}/wsi_platform_darwin.cpp")
set(dxmt_includes "${dxmt_SOURCE_DIR}/include" "${dxmt_SOURCE_DIR}/include/native/windows"
    "${dxmt_SOURCE_DIR}/include/native/directx" "${dxmt_SOURCE_DIR}/libs"
    "${dxmt_SOURCE_DIR}/src/util" "${dxmt_SOURCE_DIR}/src/dxmt" "${dxmt_SOURCE_DIR}/src/airconv"
    "${dxmt_SOURCE_DIR}/src/nativemetal" "${dxmt_SOURCE_DIR}/src/winemetal" "${dxmt_generated}")
add_library(b2-dxmt-parser STATIC ${parser})
add_library(b2-dxmt-airconv STATIC ${airconv} "${dxmt_generated}/air_msad.h" "${dxmt_generated}/air_samplepos.h" "${dxmt_generated}/air_tessellation.h")
target_include_directories(b2-dxmt-airconv SYSTEM PRIVATE ${LLVM_INCLUDE_DIRS})
target_compile_options(b2-dxmt-airconv PRIVATE -fno-exceptions -fno-rtti)
llvm_map_components_to_libnames(dxmt_llvm passes bitwriter)
target_link_libraries(b2-dxmt-airconv PRIVATE b2-dxmt-parser ${dxmt_llvm})
# Native DXMT's window thunk expects Wine's macdrv. Give it the SDL-owned Metal
# layer directly; SDL remains responsible for the view's lifetime and sizing.
file(READ "${dxmt_SOURCE_DIR}/src/winemetal/unix/winemetal_unix.c" metal)
string(FIND "${metal}" "  struct macdrv_win_data *(*pfn_get_win_data)" metal_start)
string(FIND "${metal}" "static NTSTATUS\n_ReleaseMetalView" metal_end)
if(metal_start LESS 0 OR metal_end LESS metal_start)
    message(FATAL_ERROR "Pinned DXMT window thunk changed.")
endif()
string(SUBSTRING "${metal}" 0 ${metal_start} before)
string(SUBSTRING "${metal}" ${metal_end} -1 after)
string(FIND "${after}" "static NTSTATUS\nthunk_SM50Initialize" release_end)
if(release_end LESS 0)
    message(FATAL_ERROR "Pinned DXMT view release thunk changed.")
endif()
string(SUBSTRING "${after}" ${release_end} -1 after)
file(WRITE "${dxmt_generated}/winemetal_unix.m" "${before}  CAMetalLayer *layer = (CAMetalLayer *)params->hwnd;\n  layer.device = (id<MTLDevice>)params->device;\n  params->ret_view = (obj_handle_t)[layer retain];\n  params->ret_layer = (obj_handle_t)layer;\n  return STATUS_SUCCESS;\n}\n\nstatic NTSTATUS\n_ReleaseMetalView(void *obj) {\n  struct unixcall_generic_obj_noret *params = obj;\n  [(CAMetalLayer *)params->handle release];\n  return STATUS_SUCCESS;\n}\n\n${after}")
add_library(b2-dxmt-metal SHARED "${dxmt_SOURCE_DIR}/src/winemetal/winemetal_thunks.c"
    "${dxmt_generated}/winemetal_unix.m" "${dxmt_SOURCE_DIR}/src/winemetal/unix/cache.c")
set_source_files_properties("${dxmt_SOURCE_DIR}/src/winemetal/winemetal_thunks.c"
    "${dxmt_SOURCE_DIR}/src/winemetal/unix/cache.c" PROPERTIES LANGUAGE OBJC)
target_compile_definitions(b2-dxmt-metal PRIVATE DXMT_NATIVE=1)
target_include_directories(b2-dxmt-metal PRIVATE "${dxmt_SOURCE_DIR}/src/winemetal/unix")
target_link_libraries(b2-dxmt-metal PRIVATE b2-dxmt-airconv sqlite3
    "-framework Cocoa" "-framework Foundation" "-framework CoreFoundation" "-framework CoreGraphics"
    "-framework Metal" "-framework MetalFX" "-framework QuartzCore" "-framework ColorSync")
add_library(b2-dxmt-core STATIC ${util} ${core} "${dxmt_generated}/dxmt_command.h")
target_link_libraries(b2-dxmt-core PRIVATE b2-dxmt-metal)
add_library(b2-dxmt-dxgi SHARED ${dxgi})
target_include_directories(b2-dxmt-dxgi PUBLIC "${dxmt_SOURCE_DIR}/src/dxgi")
target_link_libraries(b2-dxmt-dxgi PRIVATE b2-dxmt-core b2-dxmt-metal)
add_library(b2-dxmt-d3d11 SHARED ${d3d11})
target_link_libraries(b2-dxmt-d3d11 PRIVATE b2-dxmt-dxgi b2-dxmt-core b2-dxmt-parser b2-dxmt-metal)
foreach(target IN ITEMS b2-dxmt-parser b2-dxmt-airconv b2-dxmt-metal b2-dxmt-core b2-dxmt-dxgi b2-dxmt-d3d11)
    target_include_directories(${target} PRIVATE ${dxmt_includes})
    target_compile_options(${target} PRIVATE -fblocks -Wno-ignored-attributes -Wno-extern-c-compat -Wno-microsoft-exception-spec)
    target_compile_definitions(${target} PRIVATE NOMINMAX _WIN32_WINNT=0xa00 "DXMT_PAGE_SIZE=${dxmt_page_size}")
    # Match DXMT's language mode. LLVM 15's headers predate C++23's constexpr
    # unique_ptr, and DXMT uses _int8 as an identifier, not an MS integer type.
    set_target_properties(${target} PROPERTIES POSITION_INDEPENDENT_CODE ON CXX_STANDARD 20)
endforeach()
set_target_properties(b2-dxmt-metal b2-dxmt-dxgi b2-dxmt-d3d11 PROPERTIES
    LIBRARY_OUTPUT_DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}/$<CONFIG>" BUILD_RPATH "@loader_path")
