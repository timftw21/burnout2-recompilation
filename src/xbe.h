#pragma once
#include "file.h"
#include <optional>

namespace b2 {
struct Section {
    std::string name;
    std::uint32_t flags, address, size, raw_offset, raw_size;
    std::string expected_sha1;
    std::uint32_t header_address=0,head_reference=0,tail_reference=0;
    bool executable() const { return (flags & 4) != 0; }
    bool preloaded() const { return (flags & 2) != 0; }
};
struct KernelImport {
    std::uint32_t address, ordinal;
    std::string_view name;
};
struct Tls {
    std::uint32_t raw_start, raw_end, index_address, callback_table, zero_fill, characteristics;
    std::vector<std::uint32_t> callbacks;
};

class Xbe {
public:
    explicit Xbe(const std::filesystem::path& path);
    std::uint32_t base = 0, image_size = 0, entry = 0, kernel_thunks = 0;
    std::uint32_t title_id = 0, title_version = 0;
    std::uint32_t stack_commit = 0, heap_reserve = 0, heap_commit = 0;
    std::uint32_t non_kernel_imports = 0;
    std::string title, encoding, sha256;
    std::vector<Section> sections;
    std::vector<KernelImport> imports;
    std::optional<Tls> tls;
    bool supported() const;
    const Section& code_section(std::uint32_t address) const;
    std::size_t code_bytes(std::uint32_t address, std::span<std::byte> output);
    std::vector<std::byte> data(std::uint32_t address, std::size_t count);
    const KernelImport* kernel_import(std::uint32_t address) const;
    void load(std::span<std::byte> destination, bool preload_only = true);
    void load_section(const Section&, std::span<std::byte> destination);
    std::string report();
private:
    File file_;
    std::uint32_t headers_size_ = 0;
    std::uint64_t file_offset(std::uint32_t address, std::size_t count) const;
    std::string name(std::uint32_t address);
};
std::string check_image(Xbe& image);
}
