# Official native LLVM package: DXMT uses the LLVM 15 libraries, while the
# application and our tools use the current C++23 compiler selected by Build.sh.
if command -v brew >/dev/null; then
    for package in spirv-headers vulkan-headers; do
        headers=$(brew --prefix "$package")
        export CPPFLAGS="-I$headers/include${CPPFLAGS:+ $CPPFLAGS}"
    done
    # The upstream configure step checks the loader even for its shader library.
    vulkan_loader=$(brew --prefix vulkan-loader)
    export LDFLAGS="-L$vulkan_loader/lib${LDFLAGS:+ $LDFLAGS}"
fi
case $(uname -m) in
    arm64) llvm_name=clang+llvm-15.0.7-arm64-apple-darwin22.0
        llvm_hash=867c6afd41158c132ef05a8f1ddaecf476a26b91c85def8e124414f9a9ba188d ;;
    x86_64) llvm_name=clang+llvm-15.0.7-x86_64-apple-darwin21.0
        llvm_hash=d16b6d536364c5bec6583d12dd7e6cf841b9f508c4430d9ee886726bd9983f1c ;;
esac
export B2_DXMT_LLVM="$root/build/deps/$llvm_name"
if [[ ! -f $B2_DXMT_LLVM/lib/cmake/llvm/LLVMConfig.cmake ]]; then
    mkdir -p "$root/build/deps"
    archive="$root/build/deps/$llvm_name.tar.xz"
    if [[ ! -f $archive ]] || [[ $(b2_sha256 "$archive") != "$llvm_hash" ]]; then
        curl -fL --retry 2 "https://github.com/llvm/llvm-project/releases/download/llvmorg-15.0.7/$llvm_name.tar.xz" -o "$archive.download"
        [[ $(b2_sha256 "$archive.download") == "$llvm_hash" ]] || { printf '%s\n' 'LLVM checksum mismatch.' >&2; exit 1; }
        mv -f "$archive.download" "$archive"
    fi
    tar -xJf "$archive" -C "$root/build/deps"
fi
b2_dependency_suffix="-macos-$(uname -m)"
source cmake/posix-dependencies.sh
