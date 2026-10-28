#!/usr/bin/env bash
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
cd -- "$root"
mode=install
disc=""
steamdeck=false
if [[ ${1:-} == --steamdeck ]]; then steamdeck=true; shift; fi
case ${1:-} in
    --help|-h)
        printf '%s\n' 'Burnout 2 setup' 'Usage: bash Install.sh [--steamdeck] [XISO]' '       bash Install.sh [--steamdeck] --dependencies-only' '       bash Install.sh [--steamdeck] --shortcuts-only' 'SteamOS automatically selects user-only Flatpak setup.' 'CI=true disables dialogs for unattended setup.'
        exit 0 ;;
    --dependencies-only) mode=dependencies; shift ;;
    --shortcuts-only) mode=shortcuts; shift ;;
    -*) printf '%s\n' 'Unknown setup option; use --help.' >&2; exit 1 ;;
esac
if (( $# > 1 )) || { [[ $mode != install ]] && (( $# )); }; then
    printf '%s\n' 'Invalid setup arguments; use --help.' >&2; exit 1
fi
if (( $# )); then disc=$1; fi
if [[ -n $disc && ! -f $disc ]]; then printf 'Disc image does not exist: %s\n' "$disc" >&2; exit 1; fi
case $(uname -s):$(uname -m) in
    Linux:x86_64) platform=linux ;;
    Darwin:x86_64|Darwin:arm64) platform=macos ;;
    *) printf '%s\n' 'Setup supports Linux x86-64 and native Intel/Apple Silicon macOS.' >&2; exit 1 ;;
esac
if [[ $platform == linux && -r /etc/os-release ]] && (source /etc/os-release; [[ ${ID:-} == steamos ]]); then steamdeck=true; fi
if $steamdeck && [[ $platform != linux ]]; then printf '%s\n' 'Steam Deck setup requires Linux x86-64.' >&2; exit 1; fi
game_directory="$root/build/$platform/Release"
if $steamdeck; then game_directory="$root/build/steamdeck/Release"; fi
if [[ $root == *$'\n'* || $root == *$'\r'* ]]; then
    printf '%s\n' 'The installation folder cannot contain line breaks.' >&2; exit 1
fi
dialog=false
if [[ ${CI:-} != true ]]; then
    if [[ $platform == macos ]]; then dialog=macos
    elif [[ -n ${DISPLAY:-}${WAYLAND_DISPLAY:-} ]] && command -v zenity >/dev/null; then dialog=linux
    elif [[ -n ${DISPLAY:-}${WAYLAND_DISPLAY:-} ]] && command -v kdialog >/dev/null; then dialog=kde
    fi
fi
message() {
    printf '%s\n' "$1"
    case $dialog in
        macos) osascript "$root/cmake/install-dialog.applescript" message "$1" ;;
        linux) zenity --info --no-markup --title='Burnout 2 setup' --text="$1" --width=480 ;;
        kde) kdialog --title 'Burnout 2 setup' --msgbox "$1" ;;
    esac
}
failed() {
    local status=$?
    if (( status )); then
        printf '\nSetup stopped. Your existing saves and settings are preserved.\n' >&2
        if [[ -t 0 && ${CI:-} != true ]]; then read -r -p 'Press Return to close this window. ' _ || true; fi
    fi
}
trap failed EXIT
version_at_least() {
    [[ $1 =~ ^[0-9]+(\.[0-9]+)*$ ]] || return 1
    awk -v actual="$1" -v minimum="$2" 'BEGIN {split(actual,a,"."); split(minimum,b,"."); for(i=1;i<=3;i++) {if(a[i]+0>b[i]+0) exit 0; if(a[i]+0<b[i]+0) exit 1} exit 0}'
}
confirm() {
    [[ ${CI:-} == true ]] && return
    local text='Setup downloads missing build tools, asks for your US Xbox Burnout 2 XISO, and builds the game in this folder. Existing saves, settings and imported music are preserved. Continue?'
    case $dialog in
        macos) osascript "$root/cmake/install-dialog.applescript" confirm "$text" ;;
        linux) zenity --question --no-markup --title='Burnout 2 setup' --text="$text" --width=480 ;;
        kde) kdialog --title 'Burnout 2 setup' --yesno "$text" ;;
        *)
            printf '%s\n' "$text"
            [[ -t 0 ]] || { printf '%s\n' 'Unattended setup requires CI=true.' >&2; return 1; }
            read -r -p '[y/N] ' answer
            [[ $answer == [yY] || $answer == [yY][eE][sS] ]] ;;
    esac
}
prepare_linux() {
    if $steamdeck; then bash "$root/cmake/steamdeck.sh" prepare; return; fi
    getconf GNU_LIBC_VERSION >/dev/null 2>&1 || { printf '%s\n' 'Setup requires a glibc-based Linux distribution.' >&2; return 1; }
    local manager package
    local -a packages missing=() admin=()
    if command -v apt-get >/dev/null; then
        manager=apt
        packages=(build-essential clang-19 lld-19 ninja-build git curl ca-certificates tar gzip xz-utils util-linux pkg-config patch libssl-dev libfontconfig-dev libpng-dev libjpeg-dev libasound2-dev libpulse-dev libx11-dev libxext-dev libxrandr-dev libxcursor-dev libxi-dev libxfixes-dev libxss-dev libxtst-dev libwayland-dev libxkbcommon-dev libdrm-dev libgbm-dev libegl1-mesa-dev libgl-dev libdecor-0-dev libudev-dev libvulkan-dev glslang-tools spirv-headers flex bison libjson-perl zenity xdg-utils ffmpeg)
        for package in "${packages[@]}"; do
            [[ $(dpkg-query -W -f='${db:Status-Status}' "$package" 2>/dev/null || true) == installed ]] || missing+=("$package")
        done
    elif command -v dnf >/dev/null; then
        manager=dnf
        packages=(gcc gcc-c++ clang lld llvm ninja-build git curl ca-certificates tar gzip xz util-linux make pkgconf-pkg-config patch openssl-devel fontconfig-devel libpng-devel libjpeg-turbo-devel alsa-lib-devel pulseaudio-libs-devel libX11-devel libXext-devel libXrandr-devel libXcursor-devel libXi-devel libXfixes-devel libXScrnSaver-devel libXtst-devel wayland-devel libxkbcommon-devel libdrm-devel mesa-libgbm-devel mesa-libEGL-devel mesa-libGL-devel libdecor-devel systemd-devel vulkan-headers vulkan-loader-devel glslang spirv-headers flex bison perl-JSON zenity xdg-utils ffmpeg-free)
        for package in "${packages[@]}"; do rpm -q --quiet "$package" || missing+=("$package"); done
    elif command -v pacman >/dev/null; then
        manager=pacman
        packages=(base-devel clang lld llvm ninja git curl ca-certificates tar gzip xz util-linux make pkgconf patch openssl fontconfig libpng libjpeg-turbo alsa-lib libpulse libx11 libxext libxrandr libxcursor libxi libxfixes libxss libxtst wayland libxkbcommon libdrm mesa libglvnd libdecor systemd-libs vulkan-headers vulkan-icd-loader glslang spirv-headers flex bison perl-json zenity xdg-utils ffmpeg)
        for package in "${packages[@]}"; do pacman -Q "$package" >/dev/null 2>&1 || missing+=("$package"); done
    else
        printf '%s\n' 'Automatic dependency setup supports apt, dnf and pacman. For other distributions, use the manual dependencies in BUILDING.txt and Build.sh.' >&2
        return 1
    fi
    if (( ${#missing[@]} )); then
        if (( EUID )); then
            command -v sudo >/dev/null || { printf '%s\n' 'sudo is required to install missing system packages.' >&2; return 1; }
            admin=(sudo)
        fi
        printf '\nInstalling missing system packages. Your system may ask for your password.\n'
        case $manager in
            apt) "${admin[@]}" apt-get update; "${admin[@]}" apt-get install -y --no-install-recommends "${missing[@]}" ;;
            dnf) "${admin[@]}" dnf install -y "${missing[@]}" ;;
            pacman) "${admin[@]}" pacman -S --needed --noconfirm "${missing[@]}" ;;
        esac
    fi
    for directory in /usr/lib/llvm-{30..19}/bin; do
        if [[ -x $directory/clang && -x $directory/clang++ && -x $directory/ld.lld ]]; then export PATH="$directory:$PATH"; break; fi
    done
    version_at_least "$(clang -dumpversion)" 19 || { printf '%s\n' 'Clang 19 or newer is required; update your distribution or use BUILDING.txt.' >&2; return 1; }
    source "$root/cmake/cmake-linux.sh"
    if [[ ${CI:-} != true && -n ${DISPLAY:-}${WAYLAND_DISPLAY:-} ]]; then dialog=linux; fi
}
prepare_macos() {
    version_at_least "$(sw_vers -productVersion)" 15.2 || { printf '%s\n' 'Building requires macOS 15.2+ for Xcode 16.3+. The game itself targets macOS 14+.' >&2; return 1; }
    if [[ $(sysctl -n sysctl.proc_translated 2>/dev/null || true) == 1 ]]; then
        printf '%s\n' 'Open Terminal without Rosetta, then run setup again to build for Apple Silicon.' >&2; return 1
    fi
    if [[ $(xcode-select -p 2>/dev/null || true) == */CommandLineTools && -d /Applications/Xcode.app/Contents/Developer ]]; then
        export DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer
    fi
    local xcode_version
    xcode_version=$(xcodebuild -version 2>/dev/null | awk '/^Xcode / {print $2}' || true)
    if ! version_at_least "$xcode_version" 16.3; then
        if [[ $dialog == macos ]]; then osascript "$root/cmake/install-dialog.applescript" xcode; fi
        printf '%s\n' 'Install Xcode 16.3 or newer, open it to finish setup, then run this installer again: https://developer.apple.com/download/all/?q=Xcode' >&2
        return 1
    fi
    xcodebuild -checkFirstLaunchStatus || { printf '%s\n' 'Open Xcode and complete its first-launch setup, then run this installer again.' >&2; return 1; }
    local brew_prefix
    if [[ $(uname -m) == arm64 ]]; then brew_prefix=/opt/homebrew; else brew_prefix=/usr/local; fi
    if [[ ! -x $brew_prefix/bin/brew ]]; then
        local brew_installer="$root/build/deps/homebrew-install.sh"
        local brew_checksum=5f333bbe53bc490e51e7ccb1df8779b3dd6ee73a1a7379efda216edb08ccb148
        mkdir -p -- "$root/build/deps"
        curl -fL --retry 2 https://raw.githubusercontent.com/Homebrew/install/85d1a3fc80d8a63932f632aca8f787de11d14a64/install.sh -o "$brew_installer.download"
        [[ $(shasum -a 256 "$brew_installer.download" | cut -d' ' -f1) == "$brew_checksum" ]] || { printf '%s\n' 'Homebrew installer checksum mismatch.' >&2; return 1; }
        mv -f -- "$brew_installer.download" "$brew_installer"
        if [[ ${CI:-} == true ]]; then NONINTERACTIVE=1 /bin/bash "$brew_installer"; else /bin/bash "$brew_installer"; fi
    fi
    export PATH="$brew_prefix/bin:$PATH"
    brew install cmake ninja make pkgconf openssl@3 libpng jpeg-turbo spirv-headers vulkan-headers vulkan-loader bison flex perl ffmpeg
    if [[ $(uname -m) == arm64 ]]; then brew install mpfr gmp; fi
    local perl_prefix
    perl_prefix=$(brew --prefix perl)
    export PATH="$perl_prefix/bin:$PATH"
    if ! perl -MJSON -e 1 2>/dev/null; then PERL_MM_USE_DEFAULT=1 cpan -T JSON; fi
    if ! xcrun --sdk macosx --find metal; then xcodebuild -downloadComponent MetalToolchain; fi
    osacompile -o "$root/build/setup/install-dialog.scpt" "$root/cmake/install-dialog.applescript"
}
desktop_value() {
    local value=$1
    value=${value//\\/\\\\}
    value=${value// /\\s}
    value=${value//$'\t'/\\t}
    value=${value//$'\r'/\\r}
    printf '%s' "$value"
}
desktop_exec_argument() {
    local value=$1
    value=${value//\\/\\\\}
    value=${value//\"/\\\"}
    value=${value//\$/\\\$}
    value=${value//\`/\\\`}
    value=${value//%/%%}
    printf '"%s"' "$(desktop_value "$value")"
}
shortcuts() {
    [[ -x $game_directory/b2 ]] || { printf '%s\n' 'Build the game before creating its shortcut.' >&2; return 1; }
    if [[ $platform == linux ]]; then
        local applications="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
        local play_options=""
        if $steamdeck; then play_options=" --steamdeck"; fi
        mkdir -p -- "$applications"
        cat > "$applications/burnout2-recompilation.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Burnout 2: Point of Impact
Comment=Play Burnout 2
Exec=/bin/bash $(desktop_exec_argument "$root/Play.sh")$play_options
Path=$(desktop_value "$root")
Icon=$(desktop_value "$root/resources/b2.png")
Terminal=false
Categories=Game;ArcadeGame;
EOF
        printf 'Created application menu shortcut: %s\n' "$applications/burnout2-recompilation.desktop"
    else
        local app="$HOME/Applications/Burnout 2 Recompilation.app"
        if [[ -e $app ]]; then
            [[ ! -L $app && $(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$app/Contents/Info.plist" 2>/dev/null || true) == io.github.timftw21.burnout2 ]] || { printf '%s\n' 'The application shortcut location is occupied by another app.' >&2; return 1; }
        fi
        mkdir -p -- "$app/Contents/MacOS"
        printf '#!/bin/bash\nexec /bin/bash %q "$@"\n' "$root/Play.sh" > "$app/Contents/MacOS/b2"
        chmod +x "$app/Contents/MacOS/b2"
        cat > "$app/Contents/Info.plist" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleIdentifier</key><string>io.github.timftw21.burnout2</string>
<key>CFBundleName</key><string>Burnout 2</string>
<key>CFBundleDisplayName</key><string>Burnout 2: Point of Impact</string>
<key>CFBundleExecutable</key><string>b2</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>NSHighResolutionCapable</key><true/>
</dict></plist>
EOF
        plutil -lint "$app/Contents/Info.plist"
        printf 'Created application shortcut: %s\n' "$app"
    fi
}
if [[ $mode == shortcuts ]]; then shortcuts; exit 0; fi
if [[ $mode == install ]] && ! confirm; then printf '%s\n' 'Setup cancelled.'; exit 0; fi
mkdir -p -- "$root/build/setup"
printf '\n[1/3] Preparing build tools\n'
if [[ $platform == linux ]]; then prepare_linux; else prepare_macos; fi
if [[ $mode == dependencies ]]; then printf '%s\n' 'Build tools are ready.'; exit 0; fi
if [[ -z $disc ]]; then
    case $dialog in
        macos) disc=$(osascript "$root/cmake/install-dialog.applescript" disc "$root") || { printf '%s\n' 'Setup cancelled.'; exit 0; } ;;
        linux) disc=$(zenity --file-selection --title='Select your US Xbox Burnout 2 XISO' --file-filter='Xbox disc images | *.iso *.xiso *.ISO *.XISO' --file-filter='All files | *') || { printf '%s\n' 'Setup cancelled.'; exit 0; } ;;
        kde) disc=$(kdialog --title 'Select your US Xbox Burnout 2 XISO' --getopenfilename "$HOME" '*.iso *.xiso *.ISO *.XISO|Xbox disc images') || { printf '%s\n' 'Setup cancelled.'; exit 0; } ;;
        *) read -r -p 'Path to your US Xbox Burnout 2 XISO (without surrounding quotes): ' disc ;;
    esac
fi
[[ -f $disc ]] || { printf 'Disc image does not exist: %s\n' "$disc" >&2; exit 1; }
printf '\n[2/3] Building the game. Press Control-C to cancel.\n'
if $steamdeck; then
    printf 'Build log: %s\n' "$root/build/setup/install-steamdeck.log"
    bash "$root/cmake/steamdeck.sh" build "$disc" 2>&1 | tee "$root/build/setup/install-steamdeck.log"
else
    printf 'Build log: %s\n' "$root/build/setup/install-$platform.log"
    bash "$root/Build.sh" "$disc" 2>&1 | tee "$root/build/setup/install-$platform.log"
fi
printf '\n[3/3] Creating application shortcut\n'
shortcuts
if [[ $platform == linux ]]; then location='your application menu'; else location='your home Applications folder'; fi
message "Setup is complete. Launch Burnout 2 from $location. Keep this source folder in place; the shortcut uses its installed game. Rerun setup here to update while preserving saves and settings."
