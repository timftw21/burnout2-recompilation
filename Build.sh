#!/usr/bin/env bash
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
cd -- "$root"
case "$(uname -s):$(uname -m)" in
    Linux:x86_64) platform=linux ;;
    Darwin:arm64|Darwin:x86_64)
        platform=macos
        if [[ $(uname -m) == arm64 ]]; then brew_prefix=/opt/homebrew; else brew_prefix=/usr/local; fi
        for directory in "$brew_prefix/bin" "$brew_prefix/opt/bison/bin" "$brew_prefix/opt/flex/bin" "$brew_prefix/opt/perl/bin"; do
            [[ ! -d $directory ]] || export PATH="$directory:$PATH"
        done
        if command -v brew >/dev/null; then
            for package in openssl@3 libpng jpeg-turbo mpfr gmp; do
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
if [[ $platform == linux && -x $root/build/deps/cmake-linux/bin/cmake ]]; then export PATH="$root/build/deps/cmake-linux/bin:$PATH"; fi
build_directory=${B2_BUILD_DIRECTORY:-"$root/build/$platform"}
[[ $build_directory == /* ]] || build_directory="$root/$build_directory"
build_jobs=${B2_BUILD_JOBS:-6}
if [[ $platform == macos && -z ${B2_BUILD_JOBS:-} ]]; then build_jobs=4; fi
tools_only=false
game_inputs=false
if (( $# == 1 )) && [[ $1 == --tools-only ]]; then tools_only=true
elif (( $# == 1 )) && [[ $1 == --game-inputs ]]; then
    game_inputs=true
    for file in default.xbe b2distfx.bin; do
        [[ -f data/local/$file ]] || { printf 'Missing data/local/%s.\n' "$file" >&2; exit 1; }
    done
elif (( $# != 1 )) || [[ ! -f $1 ]]; then
    printf '%s\n' 'Usage: bash Build.sh "/path/to/Burnout 2.xiso.iso" | --game-inputs | --tools-only' >&2; exit 1
fi
disc=""
if ! $tools_only && ! $game_inputs; then disc="$(cd -- "$(dirname -- "$1")" && pwd -P)/$(basename -- "$1")"; fi
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
mkdir -p "$build_directory" data/local
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
if [[ $platform == linux ]]; then configure_options+=("-DB2_LINK_JOBS=$build_jobs"); fi
build_options=()
# Report independent compiler failures together in CI, retaining the job limit.
if [[ ${CI:-} == true ]]; then build_options=(-- -k0); fi
if [[ $platform == macos ]]; then
    configure_options+=("-DB2_DXMT_LLVM=$B2_DXMT_LLVM" "-DCMAKE_C_COMPILER=$(xcrun --find clang)" "-DCMAKE_CXX_COMPILER=$(xcrun --find clang++)")
fi
fresh=()
cache="$build_directory/CMakeCache.txt"
if [[ -f $cache ]] && { [[ $(sed -n 's/^CMAKE_HOME_DIRECTORY:INTERNAL=//p' "$cache") != "$root" ]] || [[ $(sed -n 's/^CMAKE_CACHEFILE_DIR:INTERNAL=//p' "$cache") != "$build_directory" ]]; }; then
    fresh=(--fresh)
fi
configured_game=false
if ! $tools_only && [[ -f data/local/default.xbe && -f data/local/b2distfx.bin ]]; then
    cmake --preset "$platform" -B "$build_directory" ${fresh[@]+"${fresh[@]}"} ${configure_options[@]+"${configure_options[@]}"} -DB2_XBE="$root/data/local/default.xbe" -DB2_EFFECTS="$root/data/local/b2distfx.bin"
    configured_game=true
else
    cmake --preset "$platform" -B "$build_directory" ${fresh[@]+"${fresh[@]}"} ${configure_options[@]+"${configure_options[@]}"} -DB2_XBE=
fi
cmake --build "$build_directory" --config Release --parallel "$build_jobs" --target b2-tool ${build_options[@]+"${build_options[@]}"}
if $tools_only; then printf 'Built the native tool: %s\n' "$build_directory/Release/b2-tool";exit 0;fi
tool="$build_directory/Release/b2-tool"
if $game_inputs; then
    "$tool" verify "$root/data/local/default.xbe" > "$build_directory/target.json"
    cmake --build "$build_directory" --config Release --parallel "$build_jobs" ${build_options[@]+"${build_options[@]}"}
    printf 'Built the native game from verified extracted inputs: %s\n' "$build_directory/Release/b2"
    exit 0
fi
stage=$(mktemp -d "$root/data/local/b2-setup.XXXXXX")
"$tool" extract "$disc" default.xbe "$stage/default.xbe"
"$tool" verify "$stage/default.xbe" > "$build_directory/target.json"
printf '%s\n' 'Verified the supported US Xbox executable.'
"$tool" extract "$disc" data/b2distfx.bin "$stage/b2distfx.bin"
"$tool" compile-effects "$stage/b2distfx.bin" "$stage/effects.cpp" > "$build_directory/effects.json"
for file in default.xbe b2distfx.bin; do
    if [[ -f data/local/$file ]] && cmp -s -- "$stage/$file" "data/local/$file"; then
        rm -- "$stage/$file"
    else
        mv -f -- "$stage/$file" "data/local/$file"
    fi
done
if ! $configured_game; then
    cmake --preset "$platform" -B "$build_directory" ${configure_options[@]+"${configure_options[@]}"} -DB2_XBE="$root/data/local/default.xbe" -DB2_EFFECTS="$root/data/local/b2distfx.bin"
fi
cmake --build "$build_directory" --config Release --parallel "$build_jobs" ${build_options[@]+"${build_options[@]}"}
"$tool" install-modding-test "$disc" "$build_directory/Release/mods"
settings="$build_directory/Release/data/local/settings.cfg"
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
if [[ -n ${B2_LAUNCH_SCRIPT:-} ]]; then
    printf '\nBuilt successfully. Start the game with:\n  bash %q\n' "$B2_LAUNCH_SCRIPT"
else
    printf '\nBuilt successfully. Start the game with:\n  %q\n' "$build_directory/Release/b2"
fi
