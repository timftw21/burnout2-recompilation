#include "disc.h"
#include <algorithm>
#include <array>
#include <cctype>
#include <stdexcept>
#include <unordered_set>

namespace b2 {
namespace {
constexpr std::uint64_t sector_size = 2048;
constexpr std::string_view magic = "MICROSOFT*XBOX*MEDIA";
bool equal_name(std::string_view a, std::string_view b) {
    return a.size() == b.size() && std::equal(a.begin(), a.end(), b.begin(), [](unsigned char x, unsigned char y) {
        return std::tolower(x) == std::tolower(y);
    });
}
}

Disc::Disc(const std::filesystem::path& path) : file_(path) {
    const auto header = file_.read(32 * sector_size, sector_size);
    const auto is_magic = [&](std::size_t offset) {
        return std::string_view(reinterpret_cast<const char*>(header.data() + offset), magic.size()) == magic;
    };
    if (!is_magic(0) || !is_magic(sector_size - magic.size()))
        throw std::runtime_error("Not a partition-only Xbox XISO image");
    root_ = {"", u32(header, 0x14), u32(header, 0x18), 0x10};
    file_.check(root_.sector * sector_size, root_.size);
}

std::vector<DiscEntry> Disc::directory(const DiscEntry& entry) {
    if (!entry.directory()) throw std::runtime_error("Path is not a directory");
    if (!entry.size) return {};
    // Child offsets are 16-bit multiples of four; read nodes, not the disc image.
    if (entry.size > 1024 * 1024) throw std::runtime_error("Directory exceeds 1 MiB inspection limit");
    const auto table = file_.read(entry.sector * sector_size, entry.size);
    std::vector<std::uint32_t> pending{0};
    std::unordered_set<std::uint32_t> seen;
    std::vector<DiscEntry> entries;
    while (!pending.empty()) {
        const auto offset = pending.back();
        pending.pop_back();
        if (!seen.insert(offset).second) throw std::runtime_error("Cyclic or shared directory node");
        if (offset > table.size() || 14 > table.size() - offset)
            throw std::runtime_error("Truncated directory node");
        const auto left = u16(table, offset);
        const auto right = u16(table, offset + 2);
        const auto name_size = std::to_integer<unsigned>(table[offset + 13]);
        if (!name_size || name_size > table.size() - offset - 14)
            throw std::runtime_error("Invalid directory filename length");
        DiscEntry child{
            std::string(reinterpret_cast<const char*>(table.data() + offset + 14), name_size),
            u32(table, offset + 4), u32(table, offset + 8),
            std::to_integer<std::uint8_t>(table[offset + 12])};
        if (child.name == "." || child.name == ".." || child.name.find_first_of("/\\\0", 0, 3) != std::string::npos)
            throw std::runtime_error("Invalid directory filename");
        file_.check(child.sector * sector_size, child.size);
        entries.push_back(std::move(child));
        if (right) pending.push_back(static_cast<std::uint32_t>(right) * 4);
        if (left) pending.push_back(static_cast<std::uint32_t>(left) * 4);
    }
    std::ranges::sort(entries, [](const auto& a, const auto& b) {
        return std::lexicographical_compare(a.name.begin(), a.name.end(), b.name.begin(), b.name.end(),
            [](unsigned char x, unsigned char y) { return std::tolower(x) < std::tolower(y); });
    });
    for (std::size_t i = 1; i < entries.size(); ++i)
        if (equal_name(entries[i - 1].name, entries[i].name)) throw std::runtime_error("Duplicate directory filename");
    return entries;
}

std::optional<DiscEntry> Disc::lookup(std::string_view path) {
    auto entry = root_;
    std::size_t start = 0;
    while (start < path.size()) {
        const auto end = path.find_first_of("/\\", start);
        const auto part = path.substr(start, end == std::string_view::npos ? path.size() - start : end - start);
        if (part.empty() || part == "." || part == "..") throw std::runtime_error("Invalid disc path component");
        const auto entries = directory(entry);
        const auto match = std::ranges::find_if(entries, [&](const auto& item) { return equal_name(item.name, part); });
        if (match == entries.end()) return std::nullopt;
        entry = *match;
        if (end == std::string_view::npos) break;
        if (!entry.directory()) return std::nullopt;
        start = end + 1;
        if (start == path.size()) throw std::runtime_error("Trailing disc path separator");
    }
    return entry;
}

DiscEntry Disc::find(std::string_view path) {
    const auto entry = lookup(path);
    if (!entry) throw std::runtime_error("Disc file not found: " + std::string(path));
    return *entry;
}
void Disc::read(const DiscEntry& entry, std::uint64_t offset, std::span<std::byte> output) {
    if (entry.directory() || offset > entry.size || output.size() > entry.size-offset)
        throw std::runtime_error("Read outside disc file");
    file_.read(entry.sector*sector_size+offset,output);
}
std::string Disc::sha256(const DiscEntry& entry) {
    if (entry.directory()) throw std::runtime_error("Cannot hash a disc directory");
    return hash(file_,entry.sector*sector_size,entry.size);
}

std::vector<DiscEntry> Disc::list(std::string_view directory_path) { return directory(find(directory_path)); }
void Disc::extract(std::string_view path, const std::filesystem::path& output) {
    const auto entry = find(path);
    if (entry.directory()) throw std::runtime_error("Extract requires a file path");
    copy(file_, entry.sector * sector_size, entry.size, output);
}
}
