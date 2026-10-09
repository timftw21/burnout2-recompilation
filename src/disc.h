#pragma once
#include "file.h"
#include <optional>

namespace b2 {
struct DiscEntry {
    std::string name;
    std::uint32_t sector;
    std::uint32_t size;
    std::uint8_t attributes;
    bool directory() const { return (attributes & 0x10) != 0; }
};

class Disc {
public:
    explicit Disc(const std::filesystem::path& path);
    std::vector<DiscEntry> list(std::string_view directory = {});
    DiscEntry find(std::string_view path);
    std::optional<DiscEntry> lookup(std::string_view path);
    void read(const DiscEntry& entry, std::uint64_t offset, std::span<std::byte> output);
    void read(std::uint64_t offset, std::span<std::byte> output) { file_.read(offset, output); }
    std::string sha256(const DiscEntry& entry);
    std::uint64_t size() const { return file_.size(); }
    void extract(std::string_view path, const std::filesystem::path& output);
private:
    File file_;
    DiscEntry root_;
    std::vector<DiscEntry> directory(const DiscEntry& entry);
};
}
