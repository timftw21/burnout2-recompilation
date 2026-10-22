#include "file.h"
#include "host.h"
#ifdef _WIN32
#include <bcrypt.h>
#else
#include <openssl/evp.h>
#include <codecvt>
#include <locale>
#include <unistd.h>
#endif
#include <algorithm>
#include <array>
#include <bit>
#include <cstring>
#include <climits>
#include <format>
#include <limits>
#include <stdexcept>

namespace b2 {
File::File(const std::filesystem::path& path) : stream_(path, std::ios::binary) {
    if (!stream_) throw std::runtime_error("Cannot open " + path_text(path));
    size_ = std::filesystem::file_size(path);
}

void File::check(std::uint64_t offset, std::uint64_t count) const {
    if (offset > size_ || count > size_ - offset)
        throw std::runtime_error(std::format("Read outside file: offset {}, size {}", offset, count));
}

void File::read(std::uint64_t offset, std::span<std::byte> output) {
    check(offset, output.size());
    if (output.empty()) return;
    if (output.size() > static_cast<std::size_t>(std::numeric_limits<std::streamsize>::max()))
        throw std::runtime_error("Read is too large");
    stream_.clear();
    stream_.seekg(static_cast<std::streamoff>(offset));
    stream_.read(reinterpret_cast<char*>(output.data()), static_cast<std::streamsize>(output.size()));
    if (!stream_) throw std::runtime_error("File read failed");
}

std::vector<std::byte> File::read(std::uint64_t offset, std::size_t count) {
    check(offset, count);
    std::vector<std::byte> bytes(count);
    read(offset, bytes);
    return bytes;
}

template<class T> T little(Bytes bytes, std::size_t offset) {
    if (offset > bytes.size() || sizeof(T) > bytes.size() - offset)
        throw std::runtime_error("Truncated binary field");
    T value;
    std::memcpy(&value, bytes.data() + offset, sizeof(T));
    if constexpr (std::endian::native == std::endian::big) value = std::byteswap(value);
    return value;
}
std::uint16_t u16(Bytes bytes, std::size_t offset) { return little<std::uint16_t>(bytes, offset); }
std::uint32_t u32(Bytes bytes, std::size_t offset) { return little<std::uint32_t>(bytes, offset); }

std::string utf8(std::wstring_view text) {
#ifdef _WIN32
    if (text.empty()) return {};
    if (text.size() > INT_MAX) throw std::runtime_error("Text is too long");
    const auto count = static_cast<int>(text.size());
    const int size = WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, text.data(), count,
                                       nullptr, 0, nullptr, nullptr);
    if (!size) throw std::runtime_error("Invalid Unicode text");
    std::string result(size, '\0');
    if (!WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, text.data(), count,
                            result.data(), size, nullptr, nullptr))
        throw std::runtime_error("Unicode conversion failed");
    return result;
#else
    return std::wstring_convert<std::codecvt_utf8<wchar_t>>{}.to_bytes(text.data(),text.data()+text.size());
#endif
}

std::wstring wide(std::string_view text) {
#ifdef _WIN32
    if(text.size()>INT_MAX) throw std::runtime_error("Text is too long");
    const auto count=MultiByteToWideChar(CP_UTF8,MB_ERR_INVALID_CHARS,text.data(),static_cast<int>(text.size()),nullptr,0);
    if(!count && !text.empty()) throw std::runtime_error("Invalid UTF-8 text");
    std::wstring result(count,0);
    MultiByteToWideChar(CP_UTF8,MB_ERR_INVALID_CHARS,text.data(),static_cast<int>(text.size()),result.data(),count);return result;
#else
    return std::wstring_convert<std::codecvt_utf8<wchar_t>>{}.from_bytes(text.data(),text.data()+text.size());
#endif
}
std::string path_text(const std::filesystem::path& path) {
    const auto text=path.u8string();return {reinterpret_cast<const char*>(text.data()),text.size()};
}
std::string utf8(std::u16string_view text) {
#ifdef _WIN32
    return utf8(std::wstring_view(reinterpret_cast<const wchar_t*>(text.data()),text.size()));
#else
    return std::wstring_convert<std::codecvt_utf8_utf16<char16_t>,char16_t>{}.to_bytes(text.data(),text.data()+text.size());
#endif
}
std::u16string utf16(std::string_view text) {
#ifdef _WIN32
    const auto value=wide(text);return {reinterpret_cast<const char16_t*>(value.data()),value.size()};
#else
    return std::wstring_convert<std::codecvt_utf8_utf16<char16_t>,char16_t>{}.from_bytes(text.data(),text.data()+text.size());
#endif
}

std::filesystem::path utf8_path(std::string_view text) {
    return std::filesystem::path(std::u8string_view(reinterpret_cast<const char8_t*>(text.data()),text.size()));
}

std::string json(std::string_view text) {
    std::string result = "\"";
    for (unsigned char c : text) {
        switch (c) {
        case '"': result += "\\\""; break;
        case '\\': result += "\\\\"; break;
        case '\n': result += "\\n"; break;
        case '\r': result += "\\r"; break;
        case '\t': result += "\\t"; break;
        default:
            if (c < 0x20) result += std::format("\\u{:04X}", c);
            else result += static_cast<char>(c);
        }
    }
    return result + '"';
}

std::string hex32(std::uint32_t value) { return std::format("0x{:08X}", value); }
std::string hex_bytes(Bytes bytes) {
    constexpr char digits[] = "0123456789ABCDEF";
    std::string result(bytes.size() * 2, '0');
    for (std::size_t i = 0; i < bytes.size(); ++i) {
        const auto value = std::to_integer<unsigned>(bytes[i]);
        result[i * 2] = digits[value >> 4];
        result[i * 2 + 1] = digits[value & 15];
    }
    return result;
}

namespace {
#ifdef _WIN32
void crypto_check(NTSTATUS status) {
    if (status < 0) throw std::runtime_error(std::format("Windows hash error: 0x{:08X}",
                                                        static_cast<std::uint32_t>(status)));
}
struct Algorithm {
    BCRYPT_ALG_HANDLE handle = nullptr;
    ~Algorithm() { if (handle) BCryptCloseAlgorithmProvider(handle, 0); }
};
struct Hash {
    BCRYPT_HASH_HANDLE handle = nullptr;
    ~Hash() { if (handle) BCryptDestroyHash(handle); }
};
#endif
constexpr std::size_t chunk_size = 256 * 1024;
}

std::string hash(File& file, std::uint64_t offset, std::uint64_t count, bool sha1, Bytes prefix) {
    file.check(offset, count);
#ifdef _WIN32
    Algorithm algorithm;
    crypto_check(BCryptOpenAlgorithmProvider(&algorithm.handle,
                                            sha1 ? BCRYPT_SHA1_ALGORITHM : BCRYPT_SHA256_ALGORITHM,
                                            nullptr, 0));
    Hash hash;
    crypto_check(BCryptCreateHash(algorithm.handle, &hash.handle, nullptr, 0, nullptr, 0, 0));
    if (!prefix.empty())
        crypto_check(BCryptHashData(hash.handle, reinterpret_cast<PUCHAR>(const_cast<std::byte*>(prefix.data())),
                                    static_cast<ULONG>(prefix.size()), 0));
#else
    struct Hash {EVP_MD_CTX* handle=EVP_MD_CTX_new();~Hash(){EVP_MD_CTX_free(handle);}} hash;
    const auto check=[](int result){if(result!=1) throw std::runtime_error("Native hash failed");};
    if(!hash.handle) throw std::runtime_error("Cannot allocate native hash");
    check(EVP_DigestInit_ex(hash.handle,sha1?EVP_sha1():EVP_sha256(),nullptr));
    check(EVP_DigestUpdate(hash.handle,prefix.data(),prefix.size()));
#endif
    std::vector<std::byte> buffer(chunk_size);
    while (count) {
        const auto size = static_cast<std::size_t>(std::min<std::uint64_t>(count, buffer.size()));
        file.read(offset, std::span(buffer).first(size));
#ifdef _WIN32
        crypto_check(BCryptHashData(hash.handle, reinterpret_cast<PUCHAR>(buffer.data()),
                                    static_cast<ULONG>(size), 0));
#else
        check(EVP_DigestUpdate(hash.handle,buffer.data(),size));
#endif
        offset += size;
        count -= size;
    }
    std::array<std::byte, 32> digest{};
    const unsigned size = sha1 ? 20 : 32;
#ifdef _WIN32
    crypto_check(BCryptFinishHash(hash.handle, reinterpret_cast<PUCHAR>(digest.data()), size, 0));
#else
    unsigned written=0;check(EVP_DigestFinal_ex(hash.handle,reinterpret_cast<unsigned char*>(digest.data()),&written));
    if(written!=size) throw std::runtime_error("Invalid native hash size");
#endif
    return hex_bytes(std::span(digest).first(size));
}

void copy(File& file, std::uint64_t offset, std::uint64_t count, const std::filesystem::path& output) {
    file.check(offset, count);
    if (std::filesystem::exists(output)) throw std::runtime_error("Output already exists");
    if (!output.parent_path().empty()) std::filesystem::create_directories(output.parent_path());
    std::ofstream stream(output, std::ios::binary | std::ios::out | std::ios::noreplace);
    if (!stream) throw std::runtime_error("Cannot create output file");
    try {
        std::vector<std::byte> buffer(chunk_size);
        while (count) {
            const auto size = static_cast<std::size_t>(std::min<std::uint64_t>(count, buffer.size()));
            file.read(offset, std::span(buffer).first(size));
            stream.write(reinterpret_cast<const char*>(buffer.data()), static_cast<std::streamsize>(size));
            if (!stream) throw std::runtime_error("Output write failed");
            offset += size;
            count -= size;
        }
        stream.close();
        if (!stream) throw std::runtime_error("Output close failed");
    } catch (...) {
        stream.close();
        std::error_code error;
        std::filesystem::remove(output, error);
        throw;
    }
}

void write_text(const std::filesystem::path& output, std::string_view text, bool replace) {
    if(replace && std::filesystem::exists(output)) {
        File previous(output);
        if(previous.size()==text.size()) {
            std::array<std::byte,8192> buffer;
            bool same=true;
            for(std::size_t offset=0;offset<text.size();) {
                const auto count=std::min(buffer.size(),text.size()-offset);
                previous.read(offset,std::span(buffer).first(count));
                if(std::memcmp(buffer.data(),text.data()+offset,count)) { same=false; break; }
                offset+=count;
            }
            if(same) return;
        }
    }
    if (!output.parent_path().empty()) std::filesystem::create_directories(output.parent_path());
    auto temporary = output;
    temporary += ".tmp";
    std::ofstream stream(temporary, std::ios::binary | std::ios::out | std::ios::noreplace);
    if (!stream) throw std::runtime_error("Cannot create temporary output file");
    try {
        stream.write(text.data(), static_cast<std::streamsize>(text.size()));
        stream.close();
        if (!stream) throw std::runtime_error("Output write failed");
#ifdef _WIN32
        if (!MoveFileExW(temporary.c_str(), output.c_str(), replace ? MOVEFILE_REPLACE_EXISTING : 0))
            throw std::runtime_error("Cannot publish output; destination may already exist");
#else
        if(replace) std::filesystem::rename(temporary,output);
        else {
            if(::link(temporary.c_str(),output.c_str())) throw std::runtime_error("Cannot publish output; destination may already exist");
            std::filesystem::remove(temporary);
        }
#endif
    } catch (...) {
        stream.close();
        std::error_code error;
        std::filesystem::remove(temporary, error);
        throw;
    }
}
}
