#include "xbox_save.h"
#include "crypto.h"
#include "host.h"
#include <algorithm>
#include <format>
#include <optional>
#ifndef _WIN32
#include <cerrno>
#include <cstdio>
#ifdef __linux__
#include <fcntl.h>
#endif
#endif

namespace b2 {
namespace {
namespace fs=std::filesystem;
void check_storage_path(const fs::path& path) {
    const auto attributes=GetFileAttributesW(path.c_str());
    if(attributes!=INVALID_FILE_ATTRIBUTES && (attributes&FILE_ATTRIBUTE_REPARSE_POINT))
        throw std::runtime_error("Native storage path crosses a link or junction: "+path_text(path));
}
bool same_name(std::string_view left,std::string_view right) {
    const auto upper=[](char c){return c>='a' && c<='z'?char(c-'a'+'A'):c;};
    return std::ranges::equal(left,right,{},upper,upper);
}
std::string_view binary_text(Bytes bytes) {
    return {reinterpret_cast<const char*>(bytes.data()),bytes.size()};
}
std::u16string profile_metadata(const std::string& name) {
    return u"\uFEFFName="+utf16(name)+u"\r\nNoCopy=1\r\n";
}
void publish_slot(const fs::path& staged,const fs::path& slot) {
    // An original game save can claim this slot while the import is being prepared.
#ifdef _WIN32
    if(MoveFileExW(staged.c_str(),slot.c_str(),0)) return;
    const std::error_code error(GetLastError(),std::system_category());
#elif defined(__APPLE__)
    if(!renamex_np(staged.c_str(),slot.c_str(),RENAME_EXCL)) return;
    const std::error_code error(errno,std::generic_category());
#else
    if(!renameat2(AT_FDCWD,staged.c_str(),AT_FDCWD,slot.c_str(),RENAME_NOREPLACE)) return;
    const std::error_code error(errno,std::generic_category());
#endif
    throw fs::filesystem_error("Could not install the Xbox profile",slot,error);
}
}

fs::path xbox_storage_root(const fs::path& override_root) {
    const auto root=fs::absolute(override_root.empty()?executable_path().parent_path()/"data/user":override_root).lexically_normal();
    if(fs::weakly_canonical(root)!=root) throw std::runtime_error("Native storage path crosses a link or junction");
    return root;
}

fs::path xbox_storage_child(const fs::path& parent,std::string_view name) {
    auto path=parent/std::string(name);
#ifndef _WIN32
    // FATX names are case-insensitive. Only an exact-name miss needs a directory walk.
    std::error_code error;
    if(!fs::exists(path,error) && !error && fs::is_directory(parent,error) && !error) {
        std::optional<fs::path> match;
        unsigned entries=0;
        for(const auto& entry:fs::directory_iterator(parent)) {
            if(++entries>4096) throw std::runtime_error("Native storage directory exceeds 4096 entries");
            if(!same_name(path_text(entry.path().filename()),name)) continue;
            if(match) throw std::runtime_error("Native storage contains ambiguous FATX filename casing");
            match=entry.path();
        }
        if(match) path=*match;
    }
#endif
    check_storage_path(path);
    return path;
}

XboxSaveKeys xbox_save_keys(Xbe& image,const fs::path& root) {
    const auto path=xbox_storage_child(root,"storage.key");
    std::array<std::byte,32> identity{};
    if(!fs::exists(path)) {
        random_bytes(identity.data(),identity.size());
        write_text(path,binary_text(identity),false);
    }
    File file(path);
    if(file.size()!=identity.size()) throw std::runtime_error("Native save identity has an invalid size; preserve storage.key with your saves");
    file.read(0,identity);
    XboxSaveKeys keys;
    std::ranges::copy(Bytes(identity).first(16),keys.disk.begin());
    if(std::ranges::all_of(keys.disk,[](std::byte value){return value==std::byte{};}))
        throw std::runtime_error("Native save identity has an invalid empty disk key");
    const auto certificate=image.data(u32(image.data(image.base+0x118,4),0),0x1D0);
    if(u32(certificate,0)<certificate.size()) throw std::runtime_error("Executable certificate has no save signing keys");
    const auto derive=[&](unsigned offset,XboxKey& key) {
        const auto digest=xbox_hmac(Bytes(identity).subspan(16),Bytes(certificate).subspan(offset,16));
        std::copy_n(digest.begin(),key.size(),key.begin());
    };
    derive(0xC0,keys.signature);
    derive(0xB0,keys.lan);
    for(unsigned i=0;i<keys.alternate.size();++i) derive(0xD0+i*16,keys.alternate[i]);
    return keys;
}

std::string import_xbox_save(const fs::path& executable,const fs::path& source,const fs::path& storage_override) {
    constexpr unsigned profile_size=0x7C00,checksum_offset=0x7AB4,signature_offset=0x7AB8;
    if(!fs::is_regular_file(source)) throw std::runtime_error("Select a profile file from the extracted Xbox save folder.");
    File input(source);
    if(input.size()!=profile_size) throw std::runtime_error("Select the 31,744-byte Profile 1, Profile 2 or Profile 3 file from the Xbox save folder.");
    auto profile=input.read(0,profile_size);
    // The original loader at D6420 checks the version and XOR before using the profile.
    if(u32(profile,0x7AB0)!=1) throw std::runtime_error("This file is not a supported Burnout 2 profile.");
    std::uint32_t checksum=0;
    for(unsigned offset=0;offset<checksum_offset;offset+=4) checksum^=u32(profile,offset);
    if(checksum!=u32(profile,checksum_offset)) throw std::runtime_error("The Xbox profile checksum failed. Copy the save from the Xbox again.");
    for(unsigned offset=0;offset<60;offset+=12) {
        if(!std::ranges::contains(Bytes(profile).subspan(offset,12),std::byte{}))
            throw std::runtime_error("The Xbox profile contains an invalid player name.");
    }

    if(!fs::is_regular_file(executable)) throw std::runtime_error("Select your Burnout 2 default.xbe in Game files first.");
    Xbe image(executable);
    if(!image.supported()) throw std::runtime_error("Select the supported US Burnout 2 executable in Game files.");
    const auto root=xbox_storage_root(storage_override);
    const auto title=xbox_storage_child(xbox_storage_child(root,"UDATA"),std::format("{:08X}",image.title_id));
    const auto keys=xbox_save_keys(image,root);
    const auto signature=xbox_hmac(keys.disk,xbox_hmac(keys.signature,Bytes(profile).first(signature_offset)));
    std::ranges::copy(signature,profile.begin()+signature_offset);

    // The three names at 2C84EC..2C850C map to these directories through XAPI E1002.
    constexpr std::array directories{"57BD267AFF58","57BD267AFF59","57BD267AFF5A"};
    std::optional<unsigned> free_slot;
    const auto source_name=path_text(source.filename());
    for(unsigned i=0;i<directories.size();++i) {
        const auto name=std::format("Profile {}",i+1);
        const auto slot=xbox_storage_child(title,directories[i]);
        if(!fs::exists(slot)) {
            if(!free_slot || same_name(source_name,name)) free_slot=i;
        } else if(fs::is_directory(slot)) {
            const auto saved=xbox_storage_child(slot,name);
            if(fs::is_regular_file(saved) && fs::file_size(saved)==profile_size && File(saved).read(0,profile_size)==profile) {
                const auto meta=xbox_storage_child(slot,"SaveMeta.xbx");
                const auto expected=profile_metadata(name);
                const auto bytes=std::as_bytes(std::span(expected));
                if(fs::is_regular_file(meta) && fs::file_size(meta)==bytes.size() && std::ranges::equal(File(meta).read(0,bytes.size()),bytes))
                    return name+" already contains this save.";
            }
        }
    }
    if(!free_slot) throw std::runtime_error("All three profiles are in use. Free a slot in the game's Load/Save menu and import again.");
    const auto name=std::format("Profile {}",*free_slot+1);
    const auto metadata=profile_metadata(name);
    fs::create_directories(title);
    std::array<std::byte,8> nonce;
    random_bytes(nonce.data(),nonce.size());
    const auto stage=title/(".import-"+hex_bytes(nonce));
    if(!fs::create_directory(stage)) throw std::runtime_error("Could not stage the Xbox save. Try importing again.");
    try {
        write_text(stage/name,binary_text(profile),false);
        write_text(stage/"SaveMeta.xbx",binary_text(std::as_bytes(std::span(metadata))),false);
        publish_slot(stage,title/directories[*free_slot]);
    } catch(...) {
        std::error_code error;
        fs::remove_all(stage,error);
        throw;
    }
    return "Imported into "+name+".";
}
}
