#!/usr/bin/env bash
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
cd -- "$root"
case "$(uname -s):$(uname -m)" in
    Linux:x86_64) platform=linux ;;
    Darwin:arm64|Darwin:x86_64)
        platform=macos
        for directory in /opt/homebrew/bin /usr/local/bin /opt/homebrew/opt/bison/bin /usr/local/opt/bison/bin /opt/homebrew/opt/flex/bin /usr/local/opt/flex/bin /opt/homebrew/opt/perl/bin /usr/local/opt/perl/bin; do
            [[ ! -d $directory ]] || export PATH="$directory:$PATH"
        done
        if command -v brew >/dev/null; then
            for package in openssl@3 libpng jpeg-turbo mpfr; do
                if directory=$(brew --prefix "$package" 2>/dev/null); then
                    export CMAKE_PREFIX_PATH="$directory${CMAKE_PREFIX_PATH:+:$CMAKE_PREFIX_PATH}"
                    export PKG_CONFIG_PATH="$directory/lib/pkgconfig${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}"
                fi
            done
        fi
        export SDKROOT="$(xcrun --sdk macosx --show-sdk-path)"
        xcrun --sdk macosx --find metal >/dev/null
        ;;
    *) printf '%s\n' 'This builder requires Linux x86-64 or macOS Intel/Apple Silicon.' >&2; exit 1 ;;
esac
tools_only=false
if (( $# == 1 )) && [[ $1 == --tools-only ]]; then tools_only=true
elif (( $# != 1 )) || [[ ! -f $1 ]]; then
    printf '%s\n' 'Usage: bash Build.sh "/path/to/Burnout 2.xiso.iso"' >&2; exit 1
fi
disc=""
if ! $tools_only; then disc="$(cd -- "$(dirname -- "$1")" && pwd -P)/$(basename -- "$1")"; fi
if [[ $disc == *$'\n'* || $root == *$'\n'* ]]; then
    printf '%s\n' 'Game and source paths cannot contain line breaks.' >&2; exit 1
fi
compiler_version=$(clang -dumpversion 2>/dev/null || true)
if [[ $platform == linux ]] && { [[ ! ${compiler_version%%.*} =~ ^[0-9]+$ ]] || (( ${compiler_version%%.*} < 19 )) || ! command -v ld.lld >/dev/null; }; then
    for directory in /usr/lib/llvm-{30..19}/bin; do
        if [[ -x $directory/clang && -x $directory/clang++ && -x $directory/ld.lld ]]; then
            export PATH="$directory:$PATH"; break
        fi
    done
fi
for program in cmake ninja clang clang++ git curl pkg-config; do
    command -v "$program" >/dev/null || { printf 'Missing %s; see BUILDING.txt.\n' "$program" >&2; exit 1; }
done
mkdir -p "build/$platform" data/local
lock_directory=""
stage=""
cleanup() {
    if [[ -n $stage ]]; then
        rm -f -- "$stage/default.xbe" "$stage/b2distfx.bin" "$stage/effects.cpp" "$stage/effects.cpp.tmp"
        rmdir -- "$stage"
    fi
    [[ -z $lock_directory ]] || rmdir -- "$lock_directory"
}
trap cleanup EXIT
if [[ $platform == linux ]]; then
    for program in ld.lld flock; do command -v "$program" >/dev/null || { printf 'Missing %s.\n' "$program" >&2; exit 1; }; done
    exec 9>build/linux-build.lock
    flock -n 9 || { printf '%s\n' 'Another Linux build is already running in this source folder.' >&2; exit 1; }
    b2_sha256() { local value;value=$(sha256sum "$1");printf '%s' "${value%% *}"; }
    source cmake/posix-dependencies.sh
else
    mkdir build/macos-build.lockdir || { printf '%s\n' 'Another Mac build is running, or its lock directory remains from an interrupted build.' >&2; exit 1; }
    lock_directory="$root/build/macos-build.lockdir"
    b2_sha256() { local value;value=$(shasum -a 256 "$1");printf '%s' "${value%% *}"; }
    source cmake/macos-dependencies.sh
fi
configure_options=()
if [[ $platform == macos ]]; then
    configure_options+=("-DB2_DXMT_LLVM=$B2_DXMT_LLVM" "-DCMAKE_C_COMPILER=$(xcrun --find clang)" "-DCMAKE_CXX_COMPILER=$(xcrun --find clang++)")
fi
fresh=()
cache="$root/build/$platform/CMakeCache.txt"
if [[ -f $cache ]] && { [[ $(sed -n 's/^CMAKE_HOME_DIRECTORY:INTERNAL=//p' "$cache") != "$root" ]] || [[ $(sed -n 's/^CMAKE_CACHEFILE_DIR:INTERNAL=//p' "$cache") != "$root/build/$platform" ]]; }; then
    fresh=(--fresh)
fi
configured_game=false
if ! $tools_only && [[ -f data/local/default.xbe && -f data/local/b2distfx.bin ]]; then
    cmake --preset "$platform" ${fresh[@]+"${fresh[@]}"} ${configure_options[@]+"${configure_options[@]}"} -DB2_XBE="$root/data/local/default.xbe" -DB2_EFFECTS="$root/data/local/b2distfx.bin"
    configured_game=true
else
    cmake --preset "$platform" ${fresh[@]+"${fresh[@]}"} ${configure_options[@]+"${configure_options[@]}"} -DB2_XBE=
fi
cmake --build --preset "$platform-release" --target b2-tool
if $tools_only; then printf 'Built the native tool: %s\n' "$root/build/$platform/Release/b2-tool";exit 0;fi
tool="$root/build/$platform/Release/b2-tool"
stage=$(mktemp -d "$root/data/local/b2-setup.XXXXXX")
"$tool" extract "$disc" default.xbe "$stage/default.xbe"
"$tool" verify "$stage/default.xbe" > "build/$platform/target.json"
printf '%s\n' 'Verified the supported US Xbox executable.'
"$tool" extract "$disc" data/b2distfx.bin "$stage/b2distfx.bin"
"$tool" compile-effects "$stage/b2distfx.bin" "$stage/effects.cpp" > "build/$platform/effects.json"
for file in default.xbe b2distfx.bin; do
    if [[ -f data/local/$file ]] && cmp -s -- "$stage/$file" "data/local/$file"; then
        rm -- "$stage/$file"
    else
        mv -f -- "$stage/$file" "data/local/$file"
    fi
done
if ! $configured_game; then
    cmake --preset "$platform" ${configure_options[@]+"${configure_options[@]}"} -DB2_XBE="$root/data/local/default.xbe" -DB2_EFFECTS="$root/data/local/b2distfx.bin"
fi
cmake --build --preset "$platform-release"
settings="$root/build/$platform/Release/data/local/settings.cfg"
mkdir -p -- "$(dirname -- "$settings")"
{
    if [[ -f $settings ]]; then
        while IFS= read -r line || [[ -n $line ]]; do
            case "$line" in xbe=*|disc=*) ;; *) printf '%s\n' "$line" ;; esac
        done < "$settings"
    fi
    printf 'xbe=%s\ndisc=%s\n' "$root/data/local/default.xbe" "$disc"
} > "$settings.tmp"
mv -f -- "$settings.tmp" "$settings"
printf '\nBuilt successfully. Start the game with:\n  %q\n' "$root/build/$platform/Release/b2"
