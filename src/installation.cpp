#include "installation.h"
#include "disc.h"
#include "target.h"
#ifdef _WIN32
#include <Windows.h>
#endif
#include <algorithm>
#include <format>
#include <fstream>

namespace b2 {
namespace {
DiscEntry verified(Disc& disc) {
    const auto entry=disc.find("default.xbe");
    if(entry.size!=target::file_size || disc.sha256(entry)!=target::sha256)
        throw std::runtime_error("Select the supported US Xbox release of Burnout 2: Point of Impact.");
    return entry;
}
void replace_file(const std::filesystem::path& temporary,const std::filesystem::path& output) {
#ifdef _WIN32
    if(!MoveFileExW(temporary.c_str(),output.c_str(),MOVEFILE_REPLACE_EXISTING|MOVEFILE_WRITE_THROUGH))
        throw std::runtime_error("Cannot save "+path_text(output)+std::format(" (Windows error {}).",GetLastError()));
#else
    std::filesystem::rename(temporary,output);
#endif
}
// A private work file; one left by an interrupted run is replaced.
struct Temporary {
    std::filesystem::path path;
    explicit Temporary(std::filesystem::path value):path(std::move(value)) {
        if(std::filesystem::exists(std::filesystem::symlink_status(path)) && !std::filesystem::is_regular_file(std::filesystem::symlink_status(path)))
            throw std::runtime_error("An unexpected item uses an installation work-file name: "+path_text(path));
        // write_text also stages through NAME.tmp.
        auto partial=path;partial+=".tmp";
        std::filesystem::remove(path);std::filesystem::remove(partial);
    }
    ~Temporary() {std::error_code error;std::filesystem::remove(path,error);}
};
#ifdef _WIN32
bool name_surrogate(const std::filesystem::path& path) {
    WIN32_FIND_DATAW data{};
    const auto handle=FindFirstFileExW(path.c_str(),FindExInfoBasic,&data,FindExSearchNameMatch,nullptr,0);
    if(handle==INVALID_HANDLE_VALUE) return false;
    FindClose(handle);
    return (data.dwFileAttributes&FILE_ATTRIBUTE_REPARSE_POINT) && IsReparseTagNameSurrogate(data.dwReserved0);
}
#endif
}
std::filesystem::path real_folder(const std::filesystem::path& path) {
    auto folder=std::filesystem::absolute(path).lexically_normal();
    if(!folder.has_filename() && folder!=folder.root_path()) folder=folder.parent_path();
#ifdef _WIN32
    // Cloud-file and other data reparse points are ordinary folders; only links redirect writes.
    for(auto part=folder;part.has_relative_path();part=part.parent_path())
        if(name_surrogate(part)) throw std::runtime_error("Choose a folder that is not reached through a link or junction.");
    return std::filesystem::weakly_canonical(folder);
#else
    if(std::filesystem::weakly_canonical(folder)!=folder) throw std::runtime_error("Choose a folder that is not reached through a symbolic link.");
    return folder;
#endif
}
void extract_game_executable(const std::filesystem::path& iso,const std::filesystem::path& directory) {
    Disc disc(iso);verified(disc);
    const auto local=real_folder(directory)/"data/local";
    std::filesystem::create_directories(local);
    if(std::filesystem::exists(local/"default.xbe") && std::filesystem::file_size(local/"default.xbe")==target::file_size) {
        File existing(local/"default.xbe");if(hash(existing,0,existing.size())==target::sha256) return;
    }
    Temporary extracted(local/"default.xbe.installing");disc.extract("default.xbe",extracted.path);
    {File check(extracted.path);if(hash(check,0,check.size())!=target::sha256) throw std::runtime_error("Extracted game executable failed verification.");}
    replace_file(extracted.path,local/"default.xbe");
}
std::string verify_install_disc(const std::filesystem::path& path,const InstallProgress& progress) {
    if(progress) progress(0,"Checking your game image");
    Disc disc(path);verified(disc);
    if(progress) progress(100,"Game image verified");
    return std::format("{{\"format\":\"b2-install-disc-v1\",\"passed\":true,\"source_sha256\":{},\"disc_bytes\":{}}}",json(target::sha256),disc.size());
}
std::string install_game_files(const std::filesystem::path& iso,const std::filesystem::path& directory,bool copy_disc,
                               const InstallProgress& progress,const std::atomic_bool* cancel) {
    const auto cancelled=[&] {if(cancel && cancel->load()) throw std::runtime_error("Installation cancelled. Your original game image and saves were kept.");};
    cancelled();
    Disc disc(iso);const auto entry=verified(disc);
    const auto root=real_folder(directory);
    if(root==root.root_path()) throw std::runtime_error("Choose a folder rather than the top of a drive.");
    const auto local=real_folder(root/"data/local");
    std::filesystem::create_directories(local);
    const auto executable=local/"default.xbe",settings=local/"settings.cfg",image=local/"game.iso";
    auto disc_path=std::filesystem::absolute(iso).lexically_normal();
    if(progress) progress(0,copy_disc?"Copying your game image":"Preparing game files");
    const bool same_image=std::filesystem::exists(image) && std::filesystem::equivalent(iso,image);
    if(copy_disc && !same_image) {
        if(std::filesystem::space(local).available<disc.size()+entry.size+16*1024*1024)
            throw std::runtime_error("There is not enough free space to copy the game image. Choose another folder or keep the image in its current location.");
        Temporary temporary(local/"game.iso.installing");
        File source(iso);std::ofstream output(temporary.path,std::ios::binary|std::ios::trunc);
        output.exceptions(std::ios::failbit|std::ios::badbit);
        std::vector<std::byte> buffer(1024*1024);unsigned previous=101;
        for(std::uint64_t offset=0;offset<source.size();) {
            cancelled();const auto count=std::min<std::uint64_t>(buffer.size(),source.size()-offset);
            source.read(offset,std::span(buffer).first(static_cast<std::size_t>(count)));
            output.write(reinterpret_cast<const char*>(buffer.data()),static_cast<std::streamsize>(count));offset+=count;
            const auto percent=static_cast<unsigned>(offset*100/source.size());
            if(progress && percent!=previous) {progress(percent,"Copying your game image");previous=percent;}
        }
        output.close();cancelled();
        {Disc copied(temporary.path);verified(copied);}
        replace_file(temporary.path,image);
    }
    cancelled();
    Temporary extracted(local/"default.xbe.installing");disc.extract("default.xbe",extracted.path);
    {File check(extracted.path);if(hash(check,0,check.size())!=target::sha256) throw std::runtime_error("Extracted game executable failed verification.");}
    std::string preferences;
    if(std::filesystem::exists(settings)) {
        if(std::filesystem::file_size(settings)>65536) throw std::runtime_error("Existing settings file exceeds 64 KiB.");
        std::ifstream input(settings);std::string line;
        while(std::getline(input,line)) if(!line.starts_with("xbe=") && !line.starts_with("disc=")) preferences+=line+'\n';
    }
    const auto path=path_text(copy_disc?std::filesystem::path("data/local/game.iso"):disc_path);
    if(path.find_first_of("\r\n")!=std::string::npos) throw std::runtime_error("The game-image path contains a line break.");
    Temporary configured(local/"settings.cfg.installing");
    write_text(configured.path,"xbe=data/local/default.xbe\ndisc="+path+'\n'+preferences,false);
    cancelled();replace_file(extracted.path,executable);replace_file(configured.path,settings);
    if(progress) progress(100,"Game files ready");
    return std::format("{{\"format\":\"b2-install-files-v1\",\"passed\":true,\"source_sha256\":{},\"copied_disc\":{},\"directory\":{}}}",json(target::sha256),copy_disc,json(path_text(root)));
}
}
