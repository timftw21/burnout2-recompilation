# Sourced from Linux setup, including the Steam Deck's Flatpak SDK.
cmake_directory="$root/build/deps/cmake-linux"
if [[ -x $cmake_directory/bin/cmake ]]; then export PATH="$cmake_directory/bin:$PATH"; fi
cmake_version=$(cmake --version 2>/dev/null | awk 'NR==1 {print $3}' || true)
if ! awk -v version="$cmake_version" 'BEGIN {split(version,v,".");exit !(v[1]>4 || (v[1]==4 && v[2]>=2))}'; then
    archive="$root/build/deps/cmake-4.4.4-linux-x86_64.tar.gz"
    checksum=e5bb807f7728cb60cd8b27ebc97a2edb469b68655f21e844a600c3575b76f5bb
    mkdir -p -- "$cmake_directory"
    if [[ ! -f $archive ]] || [[ $(sha256sum "$archive" | cut -d' ' -f1) != "$checksum" ]]; then
        curl -fL --retry 2 https://github.com/Kitware/CMake/releases/download/v4.4.4/cmake-4.4.4-linux-x86_64.tar.gz -o "$archive.download"
        [[ $(sha256sum "$archive.download" | cut -d' ' -f1) == "$checksum" ]] || { printf '%s\n' 'CMake checksum mismatch.' >&2; exit 1; }
        mv -f -- "$archive.download" "$archive"
    fi
    tar -xzf "$archive" --strip-components=1 -C "$cmake_directory"
    export PATH="$cmake_directory/bin:$PATH"
fi
