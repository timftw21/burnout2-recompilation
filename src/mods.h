#pragma once
#include "disc.h"
#include <map>

namespace b2 {
struct ModFile {
    std::string path;
    std::filesystem::path source;
    std::uint32_t size=0;
    std::string mod;
};
struct Mod {
    std::string id,name,description,version,author,error;
    std::vector<ModFile> files;
};
struct ModConflict {std::string path,previous,winner;};
bool valid_mod_id(std::string_view id);
std::vector<Mod> discover_mods(const std::filesystem::path& root);
std::string create_mod(const std::filesystem::path& directory,std::string_view name);
std::string install_modding_test(const std::filesystem::path& disc,const std::filesystem::path& root);

// Built once at session start. Reads keep using the existing native file services.
class Mods {
public:
    Mods(const std::filesystem::path& root,std::span<const std::string> enabled,Disc& disc);
    const ModFile* lookup(std::string_view relative) const;
    const std::vector<ModConflict>& conflicts() const {return conflicts_;}
    std::string report() const;
private:
    std::map<std::string,ModFile,std::less<>> files_;
    std::vector<std::string> enabled_;
    std::vector<ModConflict> conflicts_;
};
std::string inspect_mods(const std::filesystem::path& disc,const std::filesystem::path& root,
                         std::span<const std::string> enabled);
}
