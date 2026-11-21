#!/usr/bin/env bash
# Packages the built Linux game as one AppImage: the game, its own SDL3 and DXVK
# libraries, libpng and libjpeg (whose names differ between distributions),
# resources and license notices. It contains no game data; on first launch the
# game asks for the player's disc and keeps its files in ~/.local/share.
# Run on Ubuntu 24.04 after Build.sh. Usage: bash cmake/package-linux.sh [BUILD_DIRECTORY] [OUTPUT_DIRECTORY]
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
build=$(cd -- "${1:-$root/build/linux}" && pwd -P)
output=${2:-$root/build/release}
game="$build/Release/b2"
[[ -x $game ]] || { printf 'Build the game first; %s is missing.\n' "$game" >&2; exit 1; }
for program in mksquashfs curl sha256sum ldd dpkg-query; do
    command -v "$program" >/dev/null || { printf 'Missing %s; install squashfs-tools on Ubuntu 24.04.\n' "$program" >&2; exit 1; }
done
mkdir -p -- "$output"
appimage="$output/Burnout2-x86_64.AppImage"
[[ ! -e $appimage ]] || { printf 'Package already exists: %s\n' "$appimage" >&2; exit 1; }

# The pinned static AppImage runtime needs no libfuse2 on the player's system.
runtime="$root/build/deps/appimage-runtime-20251108-x86_64"
runtime_sha256=2fca8b443c92510f1483a883f60061ad09b46b978b2631c807cd873a47ec260d
if [[ ! -f $runtime ]] || [[ $(sha256sum "$runtime" | cut -d' ' -f1) != "$runtime_sha256" ]]; then
    mkdir -p -- "$root/build/deps"
    curl -fL --retry 2 https://github.com/AppImage/type2-runtime/releases/download/20251108/runtime-x86_64 -o "$runtime.download"
    [[ $(sha256sum "$runtime.download" | cut -d' ' -f1) == "$runtime_sha256" ]] || { rm -f -- "$runtime.download"; printf '%s\n' 'AppImage runtime checksum mismatch.' >&2; exit 1; }
    mv -f -- "$runtime.download" "$runtime"
fi

work=$(mktemp -d "${TMPDIR:-/tmp}/b2-appimage.XXXXXX")
trap 'rm -rf -- "$work"' EXIT
app="$work/AppDir"
bin="$app/usr/bin"
licenses="$app/usr/share/licenses/burnout2-recompilation"
mkdir -p -- "$bin/resources" "$licenses"
install -m755 "$game" "$bin/b2"
install -m644 "$root/resources/b2.png" "$root/resources/achievement-unlocked.wav" "$bin/resources/"
install -m644 "$root/LICENSE" "$root/NOTICE.md" "$root/THIRD_PARTY_NOTICES.txt" "$licenses/"

# Libraries sit beside b2, which already searches $ORIGIN first.
bundled=()
while read -r name _ path _; do
    case $name in
        libSDL3.so.*|libdxvk_d3d11.so.*|libdxvk_dxgi.so.*|libpng16.so.*|libjpeg.so.*)
            install -m644 "$(readlink -f -- "$path")" "$bin/$name"; bundled+=("$name")
            if [[ $path == /usr/* || $path == /lib/* ]]; then
                package=$(dpkg-query -S "$(readlink -f -- "$path")" | head -n1 | cut -d: -f1)
                install -m644 "/usr/share/doc/$package/copyright" "$licenses/$package.copyright"
                printf '%s\t%s\n' "$name" "$(dpkg-query -W -f='${Package} ${Version}' "$package")" >> "$licenses/bundled-libraries.txt"
            else
                printf '%s\tbuilt from the pinned sources in THIRD_PARTY_NOTICES.txt\n' "$name" >> "$licenses/bundled-libraries.txt"
            fi ;;
    esac
done < <(ldd "$game")
(( ${#bundled[@]} == 5 )) || { printf 'Expected five bundled libraries, found: %s\n' "${bundled[*]}" >&2; exit 1; }
install -m644 "$build/_deps/sdl3-src/LICENSE.txt" "$licenses/SDL3.LICENSE.txt"
dxvk_license=$(find "$build/_deps" -maxdepth 2 -path '*dxvk*' -name LICENSE -print -quit)
[[ -n $dxvk_license ]] || { printf '%s\n' 'The DXVK license file is missing from the build.' >&2; exit 1; }
install -m644 "$dxvk_license" "$licenses/DXVK.LICENSE"

cat > "$app/burnout2-recompilation.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=Burnout 2 Recompilation
Comment=Play Burnout 2: Point of Impact from your own disc image
Exec=b2
Icon=burnout2-recompilation
Terminal=false
Categories=Game;ArcadeGame;
EOF
install -m644 "$root/resources/b2.png" "$app/burnout2-recompilation.png"
ln -s burnout2-recompilation.png "$app/.DirIcon"
ln -s usr/bin/b2 "$app/AppRun"

mksquashfs "$app" "$work/image.squashfs" -root-owned -noappend -comp zstd -Xcompression-level 19 -quiet
cat "$runtime" "$work/image.squashfs" > "$appimage.tmp"
chmod 755 "$appimage.tmp"
mv -- "$appimage.tmp" "$appimage"
(cd -- "$output" && sha256sum "$(basename -- "$appimage")" > "$(basename -- "$appimage").sha256")
printf 'AppImage ready: %s (%s bytes)\n' "$appimage" "$(stat -c %s "$appimage")"
