#pragma once
#include <cstddef>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <span>
#include <string>
#include <string_view>
#include <vector>

namespace b2 {
using Bytes = std::span<const std::byte>;

class File {
public:
    explicit File(const std::filesystem::path& path);
    std::uint64_t size() const { return size_; }
    void check(std::uint64_t offset, std::uint64_t count) const;
    void read(std::uint64_t offset, std::span<std::byte> output);
    std::vector<std::byte> read(std::uint64_t offset, std::size_t count);
private:
    std::ifstream stream_;
    std::uint64_t size_;
};

std::uint16_t u16(Bytes bytes, std::size_t offset);
std::uint32_t u32(Bytes bytes, std::size_t offset);
std::string utf8(std::wstring_view text);
std::string json(std::string_view text);
std::string hex32(std::uint32_t value);
std::string hex_bytes(Bytes bytes);
std::string hash(File& file, std::uint64_t offset, std::uint64_t count,
                 bool sha1 = false, Bytes prefix = {});
void copy(File& file, std::uint64_t offset, std::uint64_t count,
          const std::filesystem::path& output);
void write_text(const std::filesystem::path& output, std::string_view text, bool replace);
}
