#!/usr/bin/env bash
# Native Linux build and launch inside a user-installed Flatpak environment.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
runtime=25.08
sdk=org.freedesktop.Sdk
platform=org.freedesktop.Platform
extension=org.freedesktop.Sdk.Extension.llvm20
build_directory="$root/build/steamdeck"
game="$build_directory/Release/b2"
cd -- "$root"
[[ $(uname -s):$(uname -m) == Linux:x86_64 ]] || { printf '%s\n' 'Steam Deck setup requires Linux x86-64.' >&2; exit 1; }
case ${1:-} in
    prepare)
        command -v flatpak >/dev/null || { printf '%s\n' 'Install Flatpak using your distribution, then rerun setup. SteamOS includes Flatpak.' >&2; exit 1; }
        flatpak remote-add --user --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo
        flatpak install --user --noninteractive -y flathub "$platform/x86_64/$runtime" "$sdk/x86_64/$runtime" "$extension/x86_64/$runtime"
        ;;
    build)
        shift
        # The SDK and host keep the same source/disc paths; no copy of the disc.
        exec flatpak run --user --devel --share=network --filesystem=host \
            --env=FLATPAK_ENABLE_SDK_EXT=llvm20 --command=bash "$sdk/x86_64/$runtime" \
            "$root/cmake/steamdeck.sh" inside-build "$@"
        ;;
    inside-build)
        shift
        [[ -f /.flatpak-info && -f /usr/lib/sdk/llvm20/enable.sh ]] || { printf '%s\n' 'Run this build through the Steam Deck installer.' >&2; exit 1; }
        source /usr/lib/sdk/llvm20/enable.sh
        source "$root/cmake/cmake-linux.sh"
        # The SDK has Vulkan headers but omits the SPIR-V grammar headers.
        spirv="$root/build/deps/spirv-headers-steamdeck"
        if [[ ! -f $spirv/include/spirv/unified1/spirv.h ]]; then
            archive="$root/build/deps/spirv-headers-steamdeck.tar.gz"
            checksum=1b220e3eec1714f0451b0e3652979bd280edf10893f617837b88e6359a804ded
            mkdir -p -- "$spirv"
            curl -fL --retry 2 https://codeload.github.com/KhronosGroup/SPIRV-Headers/tar.gz/04f10f650d514df88b76d25e83db360142c7b174 -o "$archive.download"
            [[ $(sha256sum "$archive.download" | cut -d' ' -f1) == "$checksum" ]] || { printf '%s\n' 'SPIR-V headers checksum mismatch.' >&2; exit 1; }
            mv -f -- "$archive.download" "$archive"
            tar -xzf "$archive" --strip-components=1 -C "$spirv"
        fi
        export CPATH="$spirv/include${CPATH:+:$CPATH}"
        export B2_BUILD_DIRECTORY="$build_directory" B2_BUILD_JOBS=2 b2_dependency_suffix=-steamdeck
        export B2_LAUNCH_SCRIPT="$root/Play.sh"
        settings="$build_directory/Release/data/local/settings.cfg"
        first_install=false
        [[ -f $settings ]] || first_install=true
        bash "$root/Build.sh" "$@"
        if $first_install && [[ -f $settings ]]; then
            printf 'fullscreen=1\nrefresh_rate=0\nresolution_scale=1\naspect_ratio=1\n' >> "$settings"
        fi
        if [[ -x $game ]]; then printf '%s\n' "$runtime" > "$build_directory/runtime"; fi
        ;;
    play)
        shift
        [[ -x $game ]] || { printf '%s\n' 'Run Install.sh --steamdeck to build the game first.' >&2; exit 1; }
        # Steam's host loader paths must not replace the runtime's libraries.
        unset LD_LIBRARY_PATH LD_PRELOAD
        # Keep XWayland available even when a Wayland socket exists: Gaming Mode
        # or SDL's driver selection may still require the X11 display.
        # Abstract X11 sockets need the host network namespace. A host-launch
        # wrapper can replace DISPLAY, so also retain Steam's game display.
        display=()
        if [[ -n ${DISPLAY:-} ]]; then display+=("DISPLAY=$DISPLAY"); fi
        exec flatpak run --user --share=ipc --share=network --socket=wayland --socket=x11 \
            --socket=pulseaudio --device=all --filesystem=host \
            --unset-env=LD_LIBRARY_PATH --unset-env=LD_PRELOAD \
            --command=env "$platform/x86_64/$runtime" "${display[@]}" "$game" "$@"
        ;;
    *) printf '%s\n' 'Use Install.sh --steamdeck or Play.sh --steamdeck.' >&2; exit 1 ;;
esac
