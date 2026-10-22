# DXVK Native supplies the D3D11 interfaces and Vulkan implementation, without Wine.
find_package(OpenSSL REQUIRED)
find_package(PNG REQUIRED)
find_package(JPEG REQUIRED)
find_package(Threads REQUIRED)
find_package(PkgConfig REQUIRED)
foreach(option IN ITEMS SDL_STATIC SDL_TEST_LIBRARY SDL_TESTS SDL_EXAMPLES SDL_INSTALL SDL_GPU SDL_RENDER SDL_OPENGL SDL_OPENGLES SDL_VULKAN)
    set(${option} OFF CACHE BOOL "" FORCE)
endforeach()
set(SDL_SHARED ON CACHE BOOL "" FORCE)
set(SDL_VULKAN ON CACHE BOOL "" FORCE)
FetchContent_Declare(sdl3
    URL https://github.com/libsdl-org/SDL/archive/829a65d769d935c4852f8159e964312c0957260a.tar.gz
    URL_HASH SHA256=48E15CF1A6928453D77AFA955D1FDFBBCA9169E2BC3D76D111ACB9B8BB076D66
    DOWNLOAD_EXTRACT_TIMESTAMP TRUE)
FetchContent_MakeAvailable(sdl3)
pkg_check_modules(B2_VKD3D REQUIRED IMPORTED_TARGET b2-vkd3d-shader>=2.1)
set(b2_shader_compiler_inputs ${B2_VKD3D_LINK_LIBRARIES}
    "${CMAKE_CURRENT_SOURCE_DIR}/cmake/vkd3d-copy-propagation.patch")
FetchContent_Declare(dxvk_native
    URL https://github.com/doitsujin/dxvk/releases/download/v2.7.1/dxvk-native-2.7.1-steamrt-sniper.tar.gz
    URL_HASH SHA256=BF0EBBC4BCDEE52811B084ABBEDBA51CCAAA304810EC633B76554E91957D2E20
    DOWNLOAD_EXTRACT_TIMESTAMP TRUE)
FetchContent_MakeAvailable(dxvk_native)
set(b2_dxvk_library_dir "${dxvk_native_SOURCE_DIR}/lib")
add_library(b2-platform INTERFACE)
target_include_directories(b2-platform SYSTEM INTERFACE "${dxvk_native_SOURCE_DIR}/include/dxvk")
target_compile_definitions(b2-platform INTERFACE DXVK_WSI_SDL3)
target_compile_options(b2-platform INTERFACE "$<$<COMPILE_LANGUAGE:CXX>:-fms-extensions;-Wno-ignored-attributes>")
target_link_libraries(b2-platform INTERFACE OpenSSL::Crypto Threads::Threads ${CMAKE_DL_LIBS})
add_library(b2-d3d11 INTERFACE)
target_link_libraries(b2-d3d11 INTERFACE b2-platform SDL3::SDL3-shared
    "${b2_dxvk_library_dir}/libdxvk_d3d11.so.0.20701"
    "${b2_dxvk_library_dir}/libdxvk_dxgi.so.0.20701")
add_library(b2-hlsl INTERFACE)
target_link_libraries(b2-hlsl INTERFACE PkgConfig::B2_VKD3D)
