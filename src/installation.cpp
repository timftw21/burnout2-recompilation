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
struct Temporary {
    std::filesystem::path path;
    explicit Temporary(std::filesystem::path value):path(std::move(value)) {
        if(std::filesystem::exists(path)) throw std::runtime_error("An unfinished installation file already exists: "+path_text(path));
    }
    ~Temporary() {std::error_code error;std::filesystem::remove(path,error);}
};
}
void extract_game_executable(const std::filesystem::path& iso,const std::filesystem::path& directory) {
    Disc disc(iso);verified(disc);
    const auto local=std::filesystem::absolute(directory).lexically_normal()/"data/local";
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
    const auto root=std::filesystem::absolute(directory).lexically_normal();
    if(root==root.root_path() || std::filesystem::weakly_canonical(root)!=root)
        throw std::runtime_error("Choose an installation folder without links or junctions.");
    const auto local=root/"data/local";
    if(std::filesystem::weakly_canonical(local)!=local) throw std::runtime_error("The game-data folder crosses a link or junction.");
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
