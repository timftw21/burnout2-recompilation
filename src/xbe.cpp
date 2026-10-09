#include "xbe.h"
#include "target.h"
#include "kernel_names.h"
#include <algorithm>
#include <array>
#include <cstring>
#include <format>
#include <sstream>
#include <stdexcept>

namespace b2 {
Xbe::Xbe(const std::filesystem::path& path) : file_(path) {
    const auto header = file_.read(0, 0x178);
    if (u32(header, 0) != 0x48454258) throw std::runtime_error("Not an Xbox executable");
    if (file_.size() > 64 * 1024 * 1024) throw std::runtime_error("Executable exceeds 64 MiB inspection limit");
    base = u32(header, 0x104);
    headers_size_ = u32(header, 0x108);
    image_size = u32(header, 0x10C);
    const auto image_header_size = u32(header, 0x110);
    if (image_header_size < header.size() || headers_size_ < image_header_size ||
        headers_size_ > file_.size() || image_size < headers_size_ || image_size > 128 * 1024 * 1024 ||
        static_cast<std::uint64_t>(base) + image_size > 0x100000000ULL)
        throw std::runtime_error("Invalid executable header sizes");
    const auto section_count = u32(header, 0x11C);
    if (!section_count || section_count > 256) throw std::runtime_error("Invalid section count");
    const auto table_address = u32(header, 0x120);
    const auto table_offset = file_offset(table_address, section_count * 56);
    const auto table = file_.read(table_offset, section_count * 56);
    std::vector<std::uint32_t> names;
    for (std::uint32_t i = 0; i < section_count; ++i) {
        const auto offset = i * 56;
        Section section{"", u32(table, offset), u32(table, offset + 4), u32(table, offset + 8),
                        u32(table, offset + 12), u32(table, offset + 16),
                        hex_bytes(Bytes(table).subspan(offset + 36, 20))};
        section.header_address=table_address+offset;
        section.head_reference=u32(table,offset+28);section.tail_reference=u32(table,offset+32);
        for(const auto reference:{section.head_reference,section.tail_reference}) if(reference &&
            (reference<base || static_cast<std::uint64_t>(reference)-base+2>headers_size_))
            throw std::runtime_error("Section shared-page reference lies outside the executable headers");
        if (section.address < static_cast<std::uint64_t>(base) + headers_size_ ||
            static_cast<std::uint64_t>(section.address) + section.size >
                                      static_cast<std::uint64_t>(base) + image_size)
            throw std::runtime_error("Section lies outside the executable image");
        file_.check(section.raw_offset, section.raw_size);
        for (const auto& previous : sections) {
            if (section.size && previous.size && section.address < static_cast<std::uint64_t>(previous.address) + previous.size &&
                previous.address < static_cast<std::uint64_t>(section.address) + section.size)
                throw std::runtime_error("Overlapping executable sections");
        }
        names.push_back(u32(table, offset + 20));
        sections.push_back(std::move(section));
    }
    for (std::size_t i = 0; i < sections.size(); ++i) sections[i].name = name(names[i]);

    const auto certificate_address = u32(header, 0x118);
    const auto certificate = file_.read(file_offset(certificate_address, 0xB0), 0xB0);
    if (u32(certificate, 0) < certificate.size()) throw std::runtime_error("Truncated certificate");
    file_offset(certificate_address, u32(certificate, 0));
    title_id = u32(certificate, 8);
    title_version = u32(certificate, 0xAC);
    std::wstring wide_title;
    for (std::size_t i = 0; i < 40; ++i) {
        const auto value = u16(certificate, 0xC + i * 2);
        if (!value) break;
        wide_title.push_back(static_cast<wchar_t>(value));
    }
    title = utf8(wide_title);
    struct Keys { const char* name; std::uint32_t entry, kernel; };
    // Recovered XBE address encodings, recorded in knowledge/facts.json.
    constexpr Keys keys[] = {{"retail", 2835109803, 1533886646},
                             {"debug", 2491784523, 4021416274},
                             {"beta", 3867341915, 1178828237}};
    for (const auto& key : keys) {
        const auto candidate = u32(header, 0x128) ^ key.entry;
        const bool valid = std::ranges::any_of(sections, [&](const auto& section) {
            return section.executable() && candidate >= section.address &&
                   static_cast<std::uint64_t>(candidate) - section.address < std::min(section.size, section.raw_size);
        });
        if (!valid) continue;
        if (!encoding.empty()) throw std::runtime_error("Ambiguous executable address encoding");
        entry = candidate;
        kernel_thunks = u32(header, 0x158) ^ key.kernel;
        encoding = key.name;
    }
    if (encoding.empty()) throw std::runtime_error("No valid executable entry point");
    stack_commit = u32(header, 0x130);
    heap_reserve = u32(header, 0x134);
    heap_commit = u32(header, 0x138);
    non_kernel_imports = u32(header, 0x15C);
    // An XBE thunk contains a high-bit ordinal until its loader binds it.
    // Bound the terminated table, and reject pointers/flags masquerading as ordinals.
    for (std::uint32_t i = 0; i <= 512; ++i) {
        if (kernel_thunks > UINT32_MAX - i * 4) throw std::runtime_error("Kernel import table wraps the address space");
        const auto address = kernel_thunks + i * 4;
        const auto raw = u32(data(address, 4), 0);
        if (!raw) break;
        if (i == 512) throw std::runtime_error("Unterminated kernel import table");
        const auto ordinal = raw & 0x1FFU;
        if ((raw & ~0x1FFU) != 0x80000000U || !ordinal)
            throw std::runtime_error("Invalid kernel import at " + hex32(address));
        imports.push_back({address, ordinal, kernel_name(ordinal)});
    }
    if (const auto address = u32(header, 0x12C)) {
        const auto bytes = data(address, 24);
        tls = Tls{u32(bytes, 0), u32(bytes, 4), u32(bytes, 8), u32(bytes, 12), u32(bytes, 16), u32(bytes, 20)};
        if (tls->raw_end < tls->raw_start ||
            static_cast<std::uint64_t>(tls->raw_end) - tls->raw_start + tls->zero_fill > 1024 * 1024)
            throw std::runtime_error("Invalid or oversized TLS template");
        if (tls->raw_end != tls->raw_start) file_offset(tls->raw_start, tls->raw_end - tls->raw_start);
        if (tls->index_address && !std::ranges::any_of(sections, [&](const Section& section) {
            return tls->index_address >= section.address &&
                static_cast<std::uint64_t>(tls->index_address) - section.address + 4 <= section.size;
        })) throw std::runtime_error("TLS index lies outside the mapped sections");
        if (tls->callback_table) for (std::uint32_t i = 0; i <= 256; ++i) {
            if (tls->callback_table > UINT32_MAX - i * 4) throw std::runtime_error("TLS callback table wraps the address space");
            const auto callback = u32(data(tls->callback_table + i * 4, 4), 0);
            if (!callback) break;
            if (i == 256) throw std::runtime_error("Unterminated TLS callback table");
            code_section(callback);
            tls->callbacks.push_back(callback);
        }
    }
    sha256 = hash(file_, 0, file_.size());
}

std::uint64_t Xbe::file_offset(std::uint32_t address, std::size_t count) const {
    if (address >= base && static_cast<std::uint64_t>(address) - base + count <= headers_size_)
        return address - base;
    for (const auto& section : sections) {
        if (address >= section.address && static_cast<std::uint64_t>(address) - section.address + count <=
                                         std::min(section.size, section.raw_size))
            return static_cast<std::uint64_t>(section.raw_offset) + address - section.address;
    }
    throw std::runtime_error("Address is not backed by executable data: " + hex32(address));
}

std::string Xbe::name(std::uint32_t address) {
    std::string result;
    for (std::uint32_t i = 0; i < 64; ++i) {
        if (address > UINT32_MAX - i) throw std::runtime_error("Section name address overflow");
        const auto value = file_.read(file_offset(address + i, 1), 1)[0];
        if (value == std::byte{}) return result;
        result += static_cast<char>(std::to_integer<unsigned char>(value));
    }
    throw std::runtime_error("Unterminated section name");
}

bool Xbe::supported() const { return file_.size() == target::file_size && sha256 == target::sha256; }
std::vector<std::byte> Xbe::data(std::uint32_t address, std::size_t count) {
    if (!count || count > 64 * 1024) throw std::runtime_error("Executable data read must be 1..65536 bytes");
    return file_.read(file_offset(address, count), count);
}
const KernelImport* Xbe::kernel_import(std::uint32_t address) const {
    const auto found = std::ranges::lower_bound(imports, address, {}, &KernelImport::address);
    return found != imports.end() && found->address == address ? &*found : nullptr;
}
void Xbe::load(std::span<std::byte> destination, bool preload_only) {
    if (!supported()) throw std::runtime_error("Loading requires the verified target executable");
    if (non_kernel_imports) throw std::runtime_error("Non-kernel imports need loader bindings");
    if (destination.size() != image_size) throw std::runtime_error("Executable image buffer has the wrong size");
    std::fill(destination.begin(), destination.end(), std::byte{});
    file_.read(0, destination.first(headers_size_));
    for (const auto& section : sections) {
        if (preload_only && !section.preloaded()) continue;
        load_section(section,destination.subspan(section.address-base,section.size));
    }
}
void Xbe::load_section(const Section& section,std::span<std::byte> destination) {
    if(!supported() || destination.size()!=section.size) throw std::runtime_error("Invalid executable section load");
    std::fill(destination.begin(),destination.end(),std::byte{});
    file_.read(section.raw_offset,destination.first(std::min(section.size,section.raw_size)));
}
const Section& Xbe::code_section(std::uint32_t address) const {
    for (const auto& section : sections)
        if (section.executable() && address >= section.address &&
            static_cast<std::uint64_t>(address) - section.address < std::min(section.size, section.raw_size)) return section;
    throw std::runtime_error("Address is not executable code: " + hex32(address));
}

std::size_t Xbe::code_bytes(std::uint32_t address, std::span<std::byte> output) {
    const auto& section = code_section(address);
    const auto available = std::min(section.size, section.raw_size) - (address - section.address);
    const auto count = std::min<std::size_t>(available, output.size());
    file_.read(file_offset(address, count), output.first(count));
    return count;
}

std::string Xbe::report() {
    std::ostringstream out;
    out << "{\"format\":\"b2-xbe-v1\",\"title\":" << json(title)
        << ",\"title_id\":" << json(hex32(title_id)) << ",\"title_version\":" << title_version
        << ",\"file_size\":" << file_.size() << ",\"sha256\":" << json(sha256)
        << ",\"supported\":" << (supported() ? "true" : "false")
        << ",\"expected_target\":" << json(target::id) << ",\"expected_sha256\":" << json(target::sha256)
        << ",\"encoding\":" << json(encoding) << ",\"base\":" << json(hex32(base))
        << ",\"image_size\":" << image_size << ",\"entry\":" << json(hex32(entry))
        << ",\"kernel_thunks\":" << json(hex32(kernel_thunks))
        << ",\"stack_commit\":" << stack_commit << ",\"heap_reserve\":" << heap_reserve
        << ",\"heap_commit\":" << heap_commit << ",\"non_kernel_imports\":" << json(hex32(non_kernel_imports))
        << ",\"tls\":";
    if (!tls) out << "null";
    else {
        out << "{\"raw_start\":" << json(hex32(tls->raw_start)) << ",\"raw_end\":" << json(hex32(tls->raw_end))
            << ",\"index_address\":" << json(hex32(tls->index_address)) << ",\"zero_fill\":" << tls->zero_fill
            << ",\"characteristics\":" << tls->characteristics << ",\"callbacks\":[";
        for (std::size_t i = 0; i < tls->callbacks.size(); ++i) {
            if (i) out << ',';
            out << json(hex32(tls->callbacks[i]));
        }
        out << "]}";
    }
    out << ",\"imports\":[";
    for (std::size_t i = 0; i < imports.size(); ++i) {
        if (i) out << ',';
        out << "{\"address\":" << json(hex32(imports[i].address)) << ",\"ordinal\":" << imports[i].ordinal
            << ",\"name\":" << json(imports[i].name) << '}';
    }
    out << "],\"sections\":[";
    for (std::size_t i = 0; i < sections.size(); ++i) {
        const auto& section = sections[i];
        const std::uint32_t size = section.raw_size;
        const auto prefix = std::as_bytes(std::span(&size, 1));
        const auto actual = hash(file_, section.raw_offset, section.raw_size, true, prefix);
        if (i) out << ',';
        out << "{\"name\":" << json(section.name) << ",\"address\":" << json(hex32(section.address))
            << ",\"flags\":" << json(hex32(section.flags)) << ",\"preloaded\":" << (section.preloaded()?"true":"false")
            << ",\"size\":" << section.size << ",\"raw_offset\":" << section.raw_offset
            << ",\"raw_size\":" << section.raw_size << ",\"executable\":" << (section.executable() ? "true" : "false")
            << ",\"sha1\":" << json(actual) << ",\"embedded_sha1_matches\":"
            << (actual == section.expected_sha1 ? "true" : "false") << '}';
    }
    return out.str() + "]}";
}

std::string check_image(Xbe& image) {
    std::vector<std::byte> buffer(image.image_size);
    std::uint64_t compared = 0, zero_checked = 0;
    unsigned loaded = 0, preloaded = 0;
    for (const bool preload_only : {true, false}) {
        std::fill(buffer.begin(), buffer.end(), std::byte{0xA5});
        image.load(buffer, preload_only);
        for (const auto& section : image.sections) {
            const auto memory = Bytes(buffer).subspan(section.address - image.base, section.size);
            const auto raw = preload_only && !section.preloaded() ? 0U : std::min(section.size, section.raw_size);
            for (std::uint32_t offset = 0; offset < raw;) {
                const auto count = std::min<std::uint32_t>(32 * 1024, raw - offset);
                const auto source = image.data(section.address + offset, count);
                if (!std::ranges::equal(source, memory.subspan(offset, count)))
                    throw std::runtime_error("Loaded section differs from executable bytes: " + section.name);
                compared += count;
                offset += count;
            }
            if (!std::ranges::all_of(memory.subspan(raw), [](std::byte value) { return value == std::byte{}; }))
                throw std::runtime_error("Uninitialised section storage was not zeroed: " + section.name);
            zero_checked += section.size - raw;
            if (raw) ++(preload_only ? preloaded : loaded);
        }
        // Header pointers and zero-filled TLS storage must survive the same load.
        if (u32(buffer, 0) != 0x48454258 || u32(buffer, 0x104) != image.base)
            throw std::runtime_error("Loaded executable header is corrupt");
        if (image.tls && image.tls->index_address &&
            u32(buffer, image.tls->index_address - image.base) != 0)
            throw std::runtime_error("Verified target's initial TLS index is not zero");
        if(image.tls) {
            const auto directory=u32(buffer,0x12C)-image.base;
            const std::uint32_t fields[]={image.tls->raw_start,image.tls->raw_end,image.tls->index_address,
                image.tls->callback_table,image.tls->zero_fill,image.tls->characteristics};
            for(unsigned i=0;i<std::size(fields);++i) if(u32(buffer,directory+i*4)!=fields[i])
                throw std::runtime_error("Loaded TLS directory differs from original executable metadata");
        }
    }
    return std::format("{{\"format\":\"b2-image-check-v1\",\"passed\":true,\"sha256\":{},\"image_size\":{},"
        "\"loaded_sections\":{},\"preloaded_sections\":{},\"raw_bytes_compared\":{},\"zero_bytes_checked\":{},"
        "\"kernel_imports\":{},\"tls_template_bytes\":{}}}", json(image.sha256), image.image_size, loaded, preloaded,
        compared, zero_checked, image.imports.size(), image.tls ? image.tls->raw_end - image.tls->raw_start + image.tls->zero_fill : 0U);
}
}
