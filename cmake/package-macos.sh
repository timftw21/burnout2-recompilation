#!/usr/bin/env bash
# Packages the built macOS game as a self-contained, ad-hoc signed app in a
# disk image. Non-system libraries are copied into Contents/Frameworks with
# relative install names. It contains no game data; on first launch the game
# asks for the player's disc and keeps its files in Application Support.
# Usage: bash cmake/package-macos.sh [BUILD_DIRECTORY] [OUTPUT_DIRECTORY]
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
build=$(cd -- "${1:-$root/build/macos}" && pwd -P)
output=${2:-$root/build/release}
game="$build/Release/b2"
[[ -x $game ]] || { printf 'Build the game first; %s is missing.\n' "$game" >&2; exit 1; }
case $(uname -m) in arm64) label=AppleSilicon ;; x86_64) label=Intel ;; *) printf '%s\n' 'Unsupported Mac architecture.' >&2; exit 1 ;; esac
mkdir -p -- "$output"
output=$(cd -- "$output" && pwd -P)
dmg="$output/Burnout2-macOS-$label.dmg"
[[ ! -e $dmg ]] || { printf 'Package already exists: %s\n' "$dmg" >&2; exit 1; }
version=$(sed -n 's/^project(burnout2 VERSION \([0-9.]*\) .*/\1/p' "$root/CMakeLists.txt")
[[ -n $version ]] || { printf '%s\n' 'Cannot read the project version.' >&2; exit 1; }

work=$(mktemp -d "${TMPDIR:-/tmp}/b2-macos.XXXXXX")
trap 'rm -rf -- "$work"' EXIT
app="$work/image/Burnout 2 Recompilation.app"
contents="$app/Contents"
frameworks="$contents/Frameworks"
licenses="$contents/Resources/licenses"
mkdir -p -- "$contents/MacOS" "$frameworks" "$licenses"
install -m755 "$game" "$contents/MacOS/b2"
install -m644 "$root/resources/b2.png" "$root/resources/achievement-unlocked.wav" "$contents/Resources/"
install -m644 "$root/LICENSE" "$root/NOTICE.md" "$root/THIRD_PARTY_NOTICES.txt" "$licenses/"

# Resolve a dependency as the build's dynamic loader would, from the image's original folder.
# Kept compatible with macOS's bash 3.2: no associative arrays or mapfile.
tab=$'\t'
origins=()
origin_of() {
    local entry
    for entry in ${origins[@]+"${origins[@]}"}; do [[ ${entry%%"$tab"*} == "$1" ]] && { printf '%s' "${entry#*"$tab"}"; return; }; done
    printf '%s' "$build/Release"
}
rpaths() { otool -l "$1" | awk '/cmd LC_RPATH/{found=1} found && $1=="path" {print $2; found=0}'; }
resolve() {
    local dependency=$1 file=$2 origin name candidate rpath
    origin=$(origin_of "$file")
    name=${dependency##*/}
    case $dependency in
        @loader_path/*) candidate="$origin/${dependency#@loader_path/}" ;;
        @executable_path/*) candidate="$build/Release/${dependency#@executable_path/}" ;;
        @rpath/*)
            while read -r rpath; do
                [[ -n $rpath ]] || continue
                rpath=${rpath/#@loader_path/$origin}; rpath=${rpath/#@executable_path/$build/Release}
                [[ -f $rpath/$name ]] && { candidate="$rpath/$name"; break; }
            done <<< "$(rpaths "$file")"
            [[ -n ${candidate:-} ]] || candidate="$build/Release/$name"
            [[ -f $candidate ]] || candidate=$(find "$build/Release" "$build/_deps" -name "$name" -not -path '*/CMakeFiles/*' -print -quit) ;;
        *) candidate=$dependency ;;
    esac
    [[ -n $candidate && -f $candidate ]] || { printf 'Cannot find %s needed by %s.\n' "$dependency" "$file" >&2; return 1; }
    (cd -- "$(dirname -- "$candidate")" && printf '%s/%s' "$(pwd -P)" "$(basename -- "$candidate")")
}
license_for() {
    local source=$1 name=$2 keg
    keg=$(cd -- "$(dirname -- "$source")/.." && pwd -P)
    if [[ $keg == */Cellar/*/* ]]; then
        local formula=${keg#*/Cellar/}; formula=${formula%%/*}; local release=${keg##*/}
        mkdir -p -- "$licenses/$formula-$release"
        find "$keg" -maxdepth 1 \( -iname 'COPYING*' -o -iname 'LICENSE*' -o -iname 'COPYRIGHT*' \) -exec install -m644 {} "$licenses/$formula-$release/" \;
        printf '%s\tHomebrew %s %s\n' "$name" "$formula" "$release" >> "$licenses/bundled-libraries.txt"
    else
        printf '%s\tbuilt from the pinned sources in THIRD_PARTY_NOTICES.txt\n' "$name" >> "$licenses/bundled-libraries.txt"
    fi
}
queue=("$contents/MacOS/b2")
while (( ${#queue[@]} )); do
    file=${queue[0]}; queue=("${queue[@]:1}")
    self=""; [[ $file == "$frameworks"/* ]] && self=$(otool -D "$file" | tail -n +2)
    while read -r dependency; do
        [[ -z $dependency || $dependency == "$self" ]] && continue
        case $dependency in /usr/lib/*|/System/*) continue ;; esac
        name=${dependency##*/}
        if [[ ! -f $frameworks/$name ]]; then
            source=$(resolve "$dependency" "$file")
            install -m755 "$source" "$frameworks/$name"
            install_name_tool -id "@rpath/$name" "$frameworks/$name"
            origins+=("$frameworks/$name$tab$(dirname -- "$source")")
            license_for "$source" "$name"
            queue+=("$frameworks/$name")
        fi
        [[ $dependency == "@rpath/$name" ]] || install_name_tool -change "$dependency" "@rpath/$name" "$file"
    done <<< "$(otool -L "$file" | tail -n +2 | awk '{print $1}')"
    # Remove absolute build-machine search paths; bundled code is found through Frameworks.
    while read -r rpath; do
        if [[ $rpath == /* ]]; then install_name_tool -delete_rpath "$rpath" "$file"; fi
    done <<< "$(rpaths "$file")"
done
install_name_tool -add_rpath "@executable_path/../Frameworks" "$contents/MacOS/b2"
for name in libSDL3.0.dylib libb2-dxmt-d3d11.dylib libb2-dxmt-dxgi.dylib; do
    [[ -f $frameworks/$name ]] || { printf 'The app is missing %s.\n' "$name" >&2; exit 1; }
done
for source in "$build/_deps/sdl3-src" "$build/_deps/dxmt-src"; do
    find "$source" -maxdepth 1 \( -iname 'COPYING*' -o -iname 'LICENSE*' \) -exec sh -c 'install -m644 "$1" "$2/$(basename "$3")-$(basename "$1")"' _ {} "$licenses" "$source" \;
done

minimum=$(for file in "$contents/MacOS/b2" "$frameworks"/*; do
    otool -l "$file" | awk '/cmd LC_BUILD_VERSION/{found=1} found && $1=="minos" {print $2; found=0}
        /cmd LC_VERSION_MIN_MACOSX/{legacy=1} legacy && $1=="version" {print $2; legacy=0}'
done | awk -F. '{value=$1*10000+$2*100+$3; if(value>newest) {newest=value; version=$0}} END {print version}')
[[ -n $minimum ]] || { printf '%s\n' 'Cannot determine the minimum macOS version.' >&2; exit 1; }
mkdir -p -- "$work/b2.iconset"
for size in 16 32 128 256; do
    sips -z "$size" "$size" "$root/resources/b2.png" --out "$work/b2.iconset/icon_${size}x${size}.png" >/dev/null
    sips -z "$((size * 2))" "$((size * 2))" "$root/resources/b2.png" --out "$work/b2.iconset/icon_${size}x${size}@2x.png" >/dev/null
done
iconutil -c icns "$work/b2.iconset" -o "$contents/Resources/b2.icns"
cat > "$contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleIdentifier</key><string>io.github.timftw21.burnout2.recompilation</string>
<key>CFBundleName</key><string>Burnout 2</string>
<key>CFBundleDisplayName</key><string>Burnout 2 Recompilation</string>
<key>CFBundleExecutable</key><string>b2</string>
<key>CFBundleIconFile</key><string>b2</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleShortVersionString</key><string>$version</string>
<key>CFBundleVersion</key><string>$version</string>
<key>LSMinimumSystemVersion</key><string>$minimum</string>
<key>LSApplicationCategoryType</key><string>public.app-category.racing-games</string>
<key>NSHighResolutionCapable</key><true/>
</dict></plist>
EOF
plutil -lint "$contents/Info.plist" >/dev/null

# Library edits invalidate signatures; Apple Silicon requires valid ad-hoc ones.
for file in "$frameworks"/*; do codesign --force --sign - "$file"; done
codesign --force --sign - "$app"
codesign --verify --deep --strict "$app"
ln -s /Applications "$work/image/Applications"
hdiutil create -quiet -volname "Burnout 2 Recompilation" -srcfolder "$work/image" -ov -format UDZO "$work/package.dmg"
mv -- "$work/package.dmg" "$dmg"
(cd -- "$output" && shasum -a 256 "$(basename -- "$dmg")" > "$(basename -- "$dmg").sha256")
printf 'Disk image ready: %s (requires macOS %s or newer)\n' "$dmg" "$minimum"
