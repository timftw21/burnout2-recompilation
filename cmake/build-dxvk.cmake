cmake_minimum_required(VERSION 4.2)
function(b2_run directory)
    execute_process(COMMAND ${ARGN} WORKING_DIRECTORY "${directory}" COMMAND_ERROR_IS_FATAL ANY)
endfunction()
set(ENV{CC} "${B2_CC}")
set(ENV{CXX} "${B2_CXX}")
set(ENV{GIT_CEILING_DIRECTORIES} "${B2_DXVK_SOURCE}:${B2_MUON_SOURCE}")
set(ENV{PATH} "${B2_WORK}/muon:$ENV{PATH}")
set(muon "${B2_WORK}/muon/muon")
if(NOT EXISTS "${muon}")
    b2_run("${B2_MUON_SOURCE}" "${CMAKE_COMMAND}" -E env CFLAGS=-O2
        sh bootstrap.sh "${B2_WORK}/muon")
    b2_run("${B2_MUON_SOURCE}" "${B2_WORK}/muon/muon-bootstrap" setup
        -Dbuildtype=release -Dlibcurl=disabled -Dlibarchive=disabled -Dlibpkgconf=disabled
        -Dnative_backtrace=disabled -Ddocs=disabled -Dmeson-docs=disabled -Dmeson-tests=disabled
        "${B2_WORK}/muon")
    b2_run("${B2_MUON_SOURCE}" "${B2_NINJA}" -C "${B2_WORK}/muon" -j2 muon)
endif()
file(MAKE_DIRECTORY "${B2_WORK}/pkgconfig" "${B2_WORK}/lib")
file(WRITE "${B2_WORK}/pkgconfig/sdl3.pc"
    "Name: SDL3\nDescription: Shared SDL3 built by Burnout 2\nVersion: 3.4.18\nLibs: -L\"${B2_SDL_LIB}\" -lSDL3\nCflags: -I\"${B2_SDL_INCLUDE}\"\n")
set(ENV{PKG_CONFIG_PATH} "${B2_WORK}/pkgconfig:$ENV{PKG_CONFIG_PATH}")
b2_run("${B2_DXVK_SOURCE}" "${muon}" setup -W -Dbuildtype=release -Ddebug=false -Db_ndebug=if-release
    -Denable_d3d8=false -Denable_d3d9=false -Denable_d3d10=false
    -Dnative_sdl3=enabled -Dnative_sdl2=disabled -Dnative_glfw=disabled "${B2_WORK}/dxvk")
b2_run("${B2_DXVK_SOURCE}" "${B2_NINJA}" -C "${B2_WORK}/dxvk" -j2
    src/d3d11/libdxvk_d3d11.so.0.30101 src/dxgi/libdxvk_dxgi.so.0.30101)
foreach(component IN ITEMS d3d11 dxgi)
    set(name "libdxvk_${component}.so.0.30101")
    file(COPY_FILE "${B2_WORK}/dxvk/src/${component}/${name}" "${B2_WORK}/lib/${name}")
    if(B2_STRIP)
        b2_run("${B2_WORK}/lib" "${B2_STRIP}" --strip-unneeded "${name}")
    endif()
    if(NOT EXISTS "${B2_WORK}/lib/libdxvk_${component}.so.0")
        file(CREATE_LINK "${name}" "${B2_WORK}/lib/libdxvk_${component}.so.0" SYMBOLIC)
    endif()
endforeach()
