# Sourced by Build.sh. The pinned native HLSL compiler needs a copy-propagation
# fix: stock 2.1 incorrectly moves float negation through integer bitcasts.
# Only its standalone library is built; no Python or Wine runtime is needed.
prefix="$root/build/deps/vkd3d${b2_dependency_suffix:-}"
export PKG_CONFIG_PATH="$prefix/lib/pkgconfig${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}"
if ! pkg-config --atleast-version=2.1 b2-vkd3d-shader ||
    ! cmp -s "$root/cmake/vkd3d-copy-propagation.patch" "$prefix/share/b2/vkd3d-copy-propagation.patch" ||
    ! cmp -s "$root/cmake/vkd3d-json.patch" "$prefix/share/b2/vkd3d-json.patch"; then
    for program in make flex bison perl cc patch; do
        command -v "$program" >/dev/null || { printf 'Missing %s; see BUILDING.txt.\n' "$program" >&2; exit 1; }
    done
    perl -MJSON::PP -e 1 || { printf '%s\n' 'Perl with its core JSON::PP module is required; see BUILDING.txt.' >&2; exit 1; }
    archive="$root/build/deps/vkd3d-2.1.tar.xz"
    mkdir -p -- "$root/build/deps"
    checksum=7510146aff2adfb4ae07ab890701a607e5ff7c66e57100cfc9f630ec92eeda6a
    if [[ ! -f $archive ]] || [[ $(b2_sha256 "$archive") != "$checksum" ]]; then
        curl -fL --retry 2 https://dl.winehq.org/vkd3d/source/vkd3d-2.1.tar.xz -o "$archive.download"
        [[ $(b2_sha256 "$archive.download") == "$checksum" ]] || { printf '%s\n' 'vkd3d checksum mismatch.' >&2; exit 1; }
        mv -f -- "$archive.download" "$archive"
    fi
    source_directory="$root/build/deps/vkd3d-source${b2_dependency_suffix:-}"
    mkdir -p "$source_directory"
    tar -xJf "$archive" --strip-components=1 -C "$source_directory"
    (
        cd -- "$source_directory"
        patch -p1 < "$root/cmake/vkd3d-copy-propagation.patch"
        patch -p1 < "$root/cmake/vkd3d-json.patch"
        # The release archive includes configure/Makefile.in; our patch updates
        # configure and configure.ac together, so no Autotools regeneration is needed.
        ./configure --prefix="$prefix" --disable-maintainer-mode --disable-tests --disable-demos --disable-doxygen-doc
        make include/private/vkd3d_version.h libvkd3d-shader.pc
        make -j2 libvkd3d-shader.la
        make install-libLTLIBRARIES lib_LTLIBRARIES=libvkd3d-shader.la
        mkdir -p "$prefix/lib/pkgconfig" "$prefix/include/vkd3d" "$prefix/share/licenses/vkd3d" "$prefix/share/b2"
        install -m644 libvkd3d-shader.pc "$prefix/lib/pkgconfig/b2-vkd3d-shader.pc"
        install -m644 include/vkd3d_shader.h include/vkd3d_types.h "$prefix/include/vkd3d/"
        install -m644 COPYING "$prefix/share/licenses/vkd3d/"
        install -m644 "$root/cmake/vkd3d-copy-propagation.patch" "$prefix/share/b2/"
        install -m644 "$root/cmake/vkd3d-json.patch" "$prefix/share/b2/"
    )
fi
