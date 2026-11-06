#pragma once
#include "disc.h"
#include <map>
#include <optional>

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
    bool texture_pack=false;
    std::optional<ModFile> script;
};
struct ModConflict {std::string path,previous,winner;};
bool valid_mod_id(std::string_view id);
std::vector<Mod> discover_mods(const std::filesystem::path& root);
std::string create_mod(const std::filesystem::path& directory,std::string_view name,bool texture_pack=false);
Mod read_texture_pack(const std::filesystem::path& directory,bool metadata_only=false);
std::string install_modding_test(const std::filesystem::path& disc,const std::filesystem::path& root);

// Built once at session start. Reads keep using the existing native file services.
class Mods {
public:
    Mods(const std::filesystem::path& root,std::span<const std::string> enabled,Disc& disc);
    const ModFile* lookup(std::string_view relative) const;
    const std::vector<ModConflict>& conflicts() const {return conflicts_;}
    std::span<const ModFile> textures() const {return textures_;}
    std::span<const ModFile> scripts() const {return scripts_;}
    std::string report() const;
private:
    std::map<std::string,ModFile,std::less<>> files_;
    std::vector<std::string> enabled_;
    std::vector<ModConflict> conflicts_;
    std::vector<ModFile> textures_;
    std::vector<ModFile> scripts_;
};
std::string inspect_mods(const std::filesystem::path& disc,const std::filesystem::path& root,
                         std::span<const std::string> enabled);
}
