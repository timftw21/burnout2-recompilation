# Backport DXVK commit 3dbf33cb2205dd0d573cbc7a65e3855d97a4800b.
# Released Native binaries still present at the previous buffer dimensions.
# Muon builds the pinned sources without requiring Python/Meson.
FetchContent_Declare(b2_dxvk_source
    URL https://codeload.github.com/doitsujin/dxvk/tar.gz/refs/tags/v3.1.1
    URL_HASH SHA256=EF4944134F6C39D0937F265391DB2D08BCA33CD3A2F0EE9133E1EB55F1CC657C
    SOURCE_DIR "${CMAKE_BINARY_DIR}/_deps/dxvk-3.1.1-src"
    SOURCE_SUBDIR b2-no-cmake DOWNLOAD_EXTRACT_TIMESTAMP FALSE)
FetchContent_Declare(b2_muon
    URL https://codeload.github.com/muon-build/muon/tar.gz/74f8b8858f59e158bd12b417931d308b64105bed
    URL_HASH SHA256=45B6D923B260BF8C0B79DB3128481E497EEFC840CEC230F854C30162E9228584
    SOURCE_SUBDIR b2-no-cmake DOWNLOAD_EXTRACT_TIMESTAMP FALSE)
FetchContent_MakeAvailable(b2_dxvk_source b2_muon)

function(b2_dxvk_submodule name destination repository revision checksum)
    FetchContent_Declare(${name}
        URL "https://codeload.github.com/${repository}/tar.gz/${revision}"
        URL_HASH "SHA256=${checksum}"
        SOURCE_DIR "${b2_dxvk_source_SOURCE_DIR}/${destination}"
        SOURCE_SUBDIR b2-no-cmake DOWNLOAD_EXTRACT_TIMESTAMP FALSE)
    FetchContent_MakeAvailable(${name})
endfunction()
b2_dxvk_submodule(b2_dxvk_vulkan include/vulkan KhronosGroup/Vulkan-Headers
    8864cdc896bbc2a9b6eb36b3218fc9ef57908d77 35824117F649BEFC664A12622BC801EDDBBA036AC03D906DB400EE1C1E7903E9)
b2_dxvk_submodule(b2_dxvk_spirv include/spirv KhronosGroup/SPIRV-Headers
    04f10f650d514df88b76d25e83db360142c7b174 1B220E3EEC1714F0451B0E3652979BD280EDF10893F617837B88E6359A804DED)
b2_dxvk_submodule(b2_dxvk_spv subprojects/dxbc-spirv doitsujin/dxbc-spirv
    bf14419e5fa7eacb817b7b632f03cb61d61bbad7 7A2BD35D2A2186F5F77137C00302B0D9364AC01017E8AE41E30F61850D7CC2F4)
b2_dxvk_submodule(b2_dxvk_spv_headers subprojects/dxbc-spirv/submodules/spirv_headers KhronosGroup/SPIRV-Headers
    c8ad050fcb29e42a2f57d9f59e97488f465c436d 5B6F38B430F1C68C7395C570053403DA14EB4CECC7FCB4040E7A5248C2F63874)
b2_dxvk_submodule(b2_dxvk_directx include/native/directx Joshua-Ashton/mingw-directx-headers
    9df86f2341616ef1888ae59919feaa6d4fad693d 9C1B8BBFD2D6C758FAC4D93C2808A46BC1EEE429FE656B4F21B29E02615AA8AA)
b2_dxvk_submodule(b2_dxvk_display subprojects/libdisplay-info doitsujin/libdisplay-info
    275e6459c7ab1ddd4b125f28d0440716e4888078 C83C3E600F90A3AC98932E9259DE62AD8AD3A65DD5EFB1D2ABCB1E4D1C8F494A)

find_program(b2_dxvk_patch patch REQUIRED)
find_program(b2_dxvk_ninja ninja REQUIRED)
find_program(b2_dxvk_glslang NAMES glslang glslangValidator REQUIRED)
find_program(b2_dxvk_perl perl REQUIRED)
file(READ "${b2_dxvk_source_SOURCE_DIR}/src/d3d11/d3d11_swapchain.cpp" b2_dxvk_swapchain)
string(FIND "${b2_dxvk_swapchain}" "setSurfaceExtent({ pDesc->Width, pDesc->Height })" b2_dxvk_fixed)
if(b2_dxvk_fixed EQUAL -1)
    execute_process(COMMAND "${b2_dxvk_patch}" -p1 -i "${CMAKE_CURRENT_LIST_DIR}/dxvk-native.patch"
        WORKING_DIRECTORY "${b2_dxvk_source_SOURCE_DIR}" COMMAND_ERROR_IS_FATAL ANY)
endif()
configure_file("${CMAKE_CURRENT_LIST_DIR}/dxvk-pnp.pl"
    "${b2_dxvk_source_SOURCE_DIR}/subprojects/libdisplay-info/tool/gen-search-table.pl" COPYONLY)

set(b2_dxvk_work "${CMAKE_BINARY_DIR}/_deps/dxvk-backport-3.1.1/$<CONFIG>")
set(b2_dxvk_library_dir "${b2_dxvk_work}/lib")
set(b2_dxvk_libraries "${b2_dxvk_library_dir}/libdxvk_d3d11.so.0.30101"
    "${b2_dxvk_library_dir}/libdxvk_dxgi.so.0.30101")
add_custom_command(OUTPUT ${b2_dxvk_libraries}
    COMMAND "${CMAKE_COMMAND}"
        "-DB2_DXVK_SOURCE=${b2_dxvk_source_SOURCE_DIR}"
        "-DB2_MUON_SOURCE=${b2_muon_SOURCE_DIR}"
        "-DB2_WORK=${b2_dxvk_work}"
        "-DB2_CC=${CMAKE_C_COMPILER}" "-DB2_CXX=${CMAKE_CXX_COMPILER}"
        "-DB2_NINJA=${b2_dxvk_ninja}" "-DB2_STRIP=${CMAKE_STRIP}"
        "-DB2_SDL_INCLUDE=${sdl3_SOURCE_DIR}/include"
        "-DB2_SDL_LIB=$<TARGET_FILE_DIR:SDL3-shared>"
        -P "${CMAKE_CURRENT_LIST_DIR}/build-dxvk.cmake"
    DEPENDS SDL3-shared "${CMAKE_CURRENT_LIST_FILE}" "${CMAKE_CURRENT_LIST_DIR}/build-dxvk.cmake"
        "${CMAKE_CURRENT_LIST_DIR}/dxvk-native.patch" "${CMAKE_CURRENT_LIST_DIR}/dxvk-pnp.pl"
        "${b2_dxvk_source_SOURCE_DIR}/src/d3d11/d3d11_swapchain.cpp"
        "${b2_dxvk_source_SOURCE_DIR}/subprojects/libdisplay-info/tool/gen-search-table.pl"
        "${b2_dxvk_source_SOURCE_DIR}/subprojects/libdisplay-info/meson.build"
    USES_TERMINAL VERBATIM)
add_custom_target(b2-dxvk-native DEPENDS ${b2_dxvk_libraries})
foreach(component IN ITEMS d3d11 dxgi)
    add_library(b2-dxvk-${component} SHARED IMPORTED GLOBAL)
    set_target_properties(b2-dxvk-${component} PROPERTIES
        IMPORTED_SONAME "libdxvk_${component}.so.0"
        IMPORTED_CONFIGURATIONS "${CMAKE_CONFIGURATION_TYPES}")
    foreach(config IN ITEMS "${CMAKE_BUILD_TYPE}" ${CMAKE_CONFIGURATION_TYPES})
        string(REPLACE "$<CONFIG>" "${config}" location "${b2_dxvk_library_dir}/libdxvk_${component}.so.0.30101")
        set(property IMPORTED_LOCATION)
        if(config)
            string(TOUPPER "${config}" upper)
            string(APPEND property "_${upper}")
        endif()
        set_property(TARGET b2-dxvk-${component} PROPERTY ${property} "${location}")
    endforeach()
    add_dependencies(b2-dxvk-${component} b2-dxvk-native)
endforeach()
