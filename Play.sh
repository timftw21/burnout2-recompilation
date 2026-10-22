#!/usr/bin/env bash
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
cd -- "$root"
case $(uname -s) in
    Linux) platform=linux ;;
    Darwin)
        platform=macos
        if [[ $(uname -m) == arm64 ]]; then brew_bin=/opt/homebrew/bin; else brew_bin=/usr/local/bin; fi
        if [[ -d $brew_bin ]]; then export PATH="$brew_bin:$PATH"; fi
        ;;
    *) printf '%s\n' 'Use the Windows game executable on this platform.' >&2; exit 1 ;;
esac
game="$root/build/$platform/Release/b2"
[[ -x $game ]] || { printf '%s\n' 'Run the installer to build the game first.' >&2; exit 1; }
exec "$game" "$@"
