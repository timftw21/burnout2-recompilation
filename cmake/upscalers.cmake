# Vendor SDK files are fetched from immutable upstream commits and checked
# before use. Downloads stay in the ignored build tree.
file(READ "${CMAKE_CURRENT_LIST_DIR}/upscalers.json" b2_upscaler_manifest)
string(JSON b2_upscaler_count LENGTH "${b2_upscaler_manifest}")
math(EXPR b2_upscaler_last "${b2_upscaler_count}-1")
set(b2_upscaler_root "${CMAKE_SOURCE_DIR}/build/deps/upscalers")
foreach(index RANGE ${b2_upscaler_last})
    foreach(field IN ITEMS sdk path url sha256)
        string(JSON ${field} GET "${b2_upscaler_manifest}" ${index} ${field})
    endforeach()
    string(TOLOWER "${sha256}" sha256)
    set(destination "${b2_upscaler_root}/${sdk}/${path}")
    set(valid FALSE)
    if(EXISTS "${destination}")
        file(SHA256 "${destination}" actual)
        if(actual STREQUAL sha256)
            set(valid TRUE)
        endif()
    endif()
    if(NOT valid)
        get_filename_component(directory "${destination}" DIRECTORY)
        file(MAKE_DIRECTORY "${directory}")
        file(DOWNLOAD "${url}" "${destination}.download" EXPECTED_HASH "SHA256=${sha256}"
             TLS_VERIFY ON STATUS download_status)
        list(GET download_status 0 download_code)
        if(NOT download_code EQUAL 0)
            message(FATAL_ERROR "Cannot download ${sdk}/${path}: ${download_status}")
        endif()
        file(RENAME "${destination}.download" "${destination}")
    endif()
endforeach()
target_sources(b2-graphics PRIVATE src/upscaling.cpp src/upscaling_checks.cpp)
target_include_directories(b2-graphics SYSTEM PRIVATE
    "${b2_upscaler_root}/dlss/include" "${b2_upscaler_root}/xess/inc"
    "${b2_upscaler_root}/fsr/Kits/FidelityFX/api/include"
    "${b2_upscaler_root}/fsr/Kits/FidelityFX/upscalers/include")
target_link_libraries(b2-graphics PRIVATE d3d12
    "$<IF:$<CONFIG:Debug>,${b2_upscaler_root}/dlss/lib/Windows_x86_64/x64/nvsdk_ngx_d_dbg.lib,${b2_upscaler_root}/dlss/lib/Windows_x86_64/x64/nvsdk_ngx_d.lib>"
    "${b2_upscaler_root}/xess/lib/libxess.lib")
target_compile_definitions(b2-graphics PRIVATE PLATFORM_WINDOWS)

function(b2_copy_upscalers target)
    foreach(binary IN ITEMS
        "dlss/lib/Windows_x86_64/rel/nvngx_dlss.dll"
        "xess/bin/libxess.dll"
        "fsr/Kits/FidelityFX/signedbin/amd_fidelityfx_loader_dx12.dll"
        "fsr/Kits/FidelityFX/signedbin/amd_fidelityfx_upscaler_dx12.dll")
        add_custom_command(TARGET ${target} POST_BUILD COMMAND "${CMAKE_COMMAND}" -E copy_if_different
            "${b2_upscaler_root}/${binary}" "$<TARGET_FILE_DIR:${target}>" VERBATIM)
    endforeach()
    foreach(license IN ITEMS "dlss/LICENSE.txt" "xess/LICENSE.txt" "fsr/Kits/FidelityFX/docs/license.md")
        string(REGEX REPLACE "/.*" "" sdk "${license}")
        add_custom_command(TARGET ${target} POST_BUILD COMMAND "${CMAKE_COMMAND}" -E make_directory
            "$<TARGET_FILE_DIR:${target}>/licenses"
            COMMAND "${CMAKE_COMMAND}" -E copy_if_different "${b2_upscaler_root}/${license}"
            "$<TARGET_FILE_DIR:${target}>/licenses/${sdk}.txt" VERBATIM)
    endforeach()
endfunction()
