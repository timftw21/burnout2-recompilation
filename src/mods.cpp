#include "mods.h"
#include "assets.h"
#include "target.h"
#include <algorithm>
#include <array>
#include <chrono>
#include <format>
#include <set>
#include <sstream>

namespace b2 {
namespace {
constexpr unsigned max_mods=128,max_files=16384;
constexpr std::string_view modding_test_description="A simple mod that recolors Burnout 2's logo.";
constexpr std::string_view previous_modding_test_description="Magenta and cyan checks on Burnout 2 logos.";
constexpr std::string_view modding_test_sha256="47C7C61DF20EB8B8E1CD0C7584D9C332E96D92797FA66337BAD956D87D42C3E0";
std::string key(std::string_view path) {
    std::string result(path);
    for(auto& ch:result) {
        if(ch=='\\') ch='/';
        else if(ch>='A' && ch<='Z') ch+='a'-'A';
    }
    return result;
}
void plain_directory(const std::filesystem::path& path) {
    if(!std::filesystem::is_directory(path) || std::filesystem::weakly_canonical(path)!=path)
        throw std::runtime_error("Mod directory must be a real folder without links or junctions: "+path_text(path));
}
void safe_file(const std::filesystem::path& path) {
    if(!std::filesystem::is_regular_file(std::filesystem::symlink_status(path)) ||
       std::filesystem::canonical(path)!=path)
        throw std::runtime_error("Mod files cannot be links, junctions or special files: "+path_text(path));
}
bool asset_path(std::string_view path) {
    if(path.empty() || path.size()>240) return false;
    std::size_t start=0;
    while(start<path.size()) {
        const auto end=path.find('/',start);
        const auto part=path.substr(start,end==std::string_view::npos?path.size()-start:end-start);
        if(part.empty() || part=="." || part==".." || part.back()=='.' || part.back()==' ' || part.size()>255) return false;
        for(unsigned char ch:part) if(ch<32 || ch>=127 || std::string_view("\\:*?\"<>|").find(ch)!=std::string_view::npos) return false;
        if(end==std::string_view::npos) return true;
        start=end+1;
    }
    return false;
}
Mod read_mod(const std::filesystem::path& directory,unsigned& entries) {
    Mod mod;mod.id=path_text(directory.filename());mod.name=mod.id;
    try {
        if(!valid_mod_id(mod.id)) throw std::runtime_error("Folder names must use 1-64 lowercase letters, digits, underscores or hyphens.");
        plain_directory(directory);safe_file(directory/"mod.cfg");
        File manifest(directory/"mod.cfg");
        if(manifest.size()>8192) throw std::runtime_error("mod.cfg exceeds 8 KiB.");
        const auto bytes=manifest.read(0,static_cast<std::size_t>(manifest.size()));
        std::istringstream lines(std::string(reinterpret_cast<const char*>(bytes.data()),bytes.size()));
        std::set<std::string> seen;std::string line,target_id,format;
        bool first=true;
        while(std::getline(lines,line)) {
            if(first && line.starts_with("\xEF\xBB\xBF")) line.erase(0,3);
            first=false;
            if(line.ends_with('\r')) line.pop_back();
            if(line.empty() || line.starts_with('#')) continue;
            const auto split=line.find('=');
            if(split==std::string::npos) throw std::runtime_error("mod.cfg requires key=value lines.");
            const auto field=line.substr(0,split),value=line.substr(split+1);
            if(value.empty() || value.size()>256 || !seen.insert(field).second ||
               std::ranges::any_of(value,[](unsigned char ch){return ch<32 || ch==127;}))
                throw std::runtime_error("mod.cfg has an empty, duplicate or invalid field.");
            static_cast<void>(wide(value));
            if(field=="format") format=value;
            else if(field=="target") target_id=value;
            else if(field=="name") mod.name=value;
            else if(field=="description") mod.description=value;
            else if(field=="version") mod.version=value;
            else if(field=="author") mod.author=value;
            else throw std::runtime_error("Unknown mod.cfg field: "+field);
        }
        if(format!="1") throw std::runtime_error("Unsupported mod format; format=1 is required.");
        if(target_id!=target::id) throw std::runtime_error("Mod target does not match "+std::string(target::id)+'.');
        const auto files=directory/"files";plain_directory(files);
        for(auto it=std::filesystem::recursive_directory_iterator(files);it!=std::filesystem::recursive_directory_iterator();++it) {
            if(++entries>max_files || it.depth()>16) throw std::runtime_error("Mods exceed 16384 entries or 16 directory levels.");
            const auto path=it->path();
            const auto relative=key(path_text(path.lexically_relative(files)));
            if(!asset_path(relative)) throw std::runtime_error("Invalid Xbox asset path: "+relative);
            if(it->is_directory()) {plain_directory(path);continue;}
            safe_file(path);
            const auto normalized=key(relative);
            if(normalized.ends_with(".xbe") || normalized=="data/b2distfx.bin")
                throw std::runtime_error("Executable and compiled DSP replacements require rebuilding the game: "+relative);
            const auto size=it->file_size();
            if(size>UINT32_MAX) throw std::runtime_error("Mod file exceeds the Xbox 4 GiB limit: "+relative);
            mod.files.push_back({normalized,path,static_cast<std::uint32_t>(size),mod.id});
        }
        std::ranges::sort(mod.files,{},&ModFile::path);
        for(std::size_t i=1;i<mod.files.size();++i) if(mod.files[i-1].path==mod.files[i].path)
            throw std::runtime_error("Duplicate asset path ignoring case: "+mod.files[i].path);
        if(mod.files.empty()) throw std::runtime_error("No replacement files in the files folder.");
    } catch(const std::exception& error) {mod.error=error.what();mod.files.clear();}
    return mod;
}
}
bool valid_mod_id(std::string_view id) {
    return !id.empty() && id.size()<=64 && std::ranges::all_of(id,[](char ch) {
        return (ch>='a' && ch<='z') || (ch>='0' && ch<='9') || ch=='_' || ch=='-';
    });
}
std::vector<Mod> discover_mods(const std::filesystem::path& root) {
    const auto directory=std::filesystem::absolute(root).lexically_normal();
    if(!std::filesystem::exists(directory)) return {};
    plain_directory(directory);
    std::vector<Mod> mods;unsigned entries=0,file_entries=0;
    for(const auto& entry:std::filesystem::directory_iterator(directory)) {
        if(++entries>max_mods) throw std::runtime_error("Mods folder exceeds 128 entries.");
        if(!entry.is_directory()) continue;
        auto mod=read_mod(entry.path(),file_entries);
        if(file_entries>max_files) throw std::runtime_error("Mods folder exceeds 16384 asset entries.");
        mods.push_back(std::move(mod));
    }
    std::ranges::sort(mods,{},&Mod::id);return mods;
}
std::string create_mod(const std::filesystem::path& directory,std::string_view name) {
    const auto path=std::filesystem::absolute(directory).lexically_normal();
    if(!valid_mod_id(path_text(path.filename())) || name.empty() || name.size()>256 ||
       std::ranges::any_of(name,[](unsigned char ch){return ch<32 || ch==127;}))
        throw std::runtime_error("Use a lowercase mod folder id and a name of 1-256 characters.");
    plain_directory(path.parent_path());
    if(std::filesystem::exists(path)) throw std::runtime_error("Mod destination already exists.");
    if(!std::filesystem::create_directory(path)) throw std::runtime_error("Mod destination already exists.");
    std::filesystem::create_directory(path/"files");
    write_text(path/"mod.cfg","format=1\ntarget="+std::string(target::id)+"\nname="+std::string(name)+"\n# description=Briefly explain what this mod changes.\nversion=1.0\n",false);
    return "{\"format\":\"b2-create-mod-v1\",\"directory\":"+json(path_text(path))+",\"target\":"+json(target::id)+'}';
}
std::string install_modding_test(const std::filesystem::path& disc_path,const std::filesystem::path& root) {
    Disc disc(disc_path);const auto executable=disc.find("default.xbe");
    if(executable.size!=target::file_size || disc.sha256(executable)!=target::sha256)
        throw std::runtime_error("Test Mod installation requires the verified supported disc.");
    const auto directory=std::filesystem::absolute(root).lexically_normal();
    if(!std::filesystem::exists(directory)) {
        plain_directory(directory.parent_path());std::filesystem::create_directory(directory);
    }
    plain_directory(directory);
    const auto destination=directory/"modding-test";
    const auto report=[&](std::string_view status) {
        return "{\"format\":\"b2-builtin-mod-v1\",\"id\":\"modding-test\",\"name\":\"Test Mod\",\"status\":"+
            json(status)+",\"default_enabled\":false,\"directory\":"+json(path_text(destination))+'}';
    };
    if(std::filesystem::exists(std::filesystem::symlink_status(destination))) {
        plain_directory(destination);
        unsigned entries=0;const auto mod=read_mod(destination,entries);
        const bool rename=mod.name=="Modding test";
        const bool describe=mod.description.empty() || mod.description==previous_modding_test_description;
        if(mod.error.empty() && (rename || describe) &&
           mod.files.size()==1 && mod.files[0].path=="frontend/global.dic") {
            File asset(mod.files[0].source);
            if(asset.size()==1253484 && hash(asset,0,asset.size())==modding_test_sha256) {
                std::string text;
                {
                    File manifest(destination/"mod.cfg");const auto bytes=manifest.read(0,static_cast<std::size_t>(manifest.size()));
                    text.assign(reinterpret_cast<const char*>(bytes.data()),bytes.size());
                }
                const auto replace_field=[&](std::string_view field,std::string_view value) {
                    const auto prefix=std::string(field)+'=',replacement=prefix+std::string(value);
                    for(std::size_t start=text.starts_with("\xEF\xBB\xBF")?3:0;start<text.size();) {
                        if(std::string_view(text).substr(start).starts_with(prefix)) {
                            const auto end=text.find_first_of("\r\n",start);
                            text.replace(start,(end==std::string::npos?text.size():end)-start,replacement);return;
                        }
                        const auto end=text.find('\n',start);if(end==std::string::npos) break;start=end+1;
                    }
                    if(!text.ends_with('\n')) text+='\n';
                    text+=replacement+'\n';
                };
                if(rename) replace_field("name","Test Mod");
                if(describe) replace_field("description",modding_test_description);
                if(text.size()<=8192) {
                    write_text(destination/"mod.cfg",text,true);return report("updated_metadata");
                }
            }
        }
        return report("preserved");
    }
    const auto entry=disc.find("Frontend/global.dic");
    // Original dictionary bytes audited against the supported US disc. Recreate
    // the user-verified mod without putting any original assets in the release.
    if(entry.size!=1253484 || disc.sha256(entry)!="812F85BACCF0E1D68F12923D7BE20BF3B18601EB5DDF327569575FFD6DE3ACF0")
        throw std::runtime_error("Test Mod requires the original supported Frontend/global.dic.");
    const auto staging=directory/std::format(".modding-test-{}.tmp",std::chrono::steady_clock::now().time_since_epoch().count());
    if(!std::filesystem::create_directory(staging)) throw std::runtime_error("Cannot create Test Mod staging folder.");
    const auto output=staging/"files/Frontend/global.dic";
    try {
        disc.extract("Frontend/global.dic",output);
        {
            TextureDictionary dictionary(output);
            std::fstream stream(output,std::ios::in|std::ios::out|std::ios::binary);
            if(!stream) throw std::runtime_error("Cannot edit Test Mod textures.");
            for(const auto name:std::array<std::string_view,2>{"b2logoattract","b2logo"}) {
                const auto texture=std::ranges::find(dictionary.textures(),name,&NativeTexture::name);
                if(texture==dictionary.textures().end() || texture->width!=256 || texture->height!=128 ||
                   texture->format!=15 || texture->mip_levels!=1 || texture->data_size!=32768)
                    throw std::runtime_error("Unsupported Test Mod logo layout.");
                auto bytes=dictionary.pixels(*texture);
                for(unsigned block=0;block<64*32;++block) {
                    const bool magenta=((block%64/4+block/64/4)&1)==0;
                    for(unsigned endpoint=0;endpoint<4;endpoint+=2) {
                        const auto offset=block*16+8+endpoint;
                        const unsigned color=u16(bytes,offset),rb=std::max(color>>11,color&31U),green=(color>>5)&63;
                        const auto five=std::max(rb,(green*31+31)/63),six=std::max(green,(rb*63+15)/31);
                        const auto tinted=magenta?(five<<11)|five:(six<<5)|five;
                        bytes[offset]=std::byte(tinted&255);bytes[offset+1]=std::byte(tinted>>8);
                    }
                }
                // Keep both alpha and palette selectors, including black edges.
                stream.seekp(static_cast<std::streamoff>(texture->data_offset));
                stream.write(reinterpret_cast<const char*>(bytes.data()),static_cast<std::streamsize>(bytes.size()));
                if(!stream) throw std::runtime_error("Cannot write Test Mod textures.");
            }
            stream.close();if(!stream) throw std::runtime_error("Cannot finish Test Mod textures.");
        }
        {
            File generated(output);
            if(hash(generated,0,generated.size())!=modding_test_sha256)
                throw std::runtime_error("Generated Test Mod does not match the verified texture mod.");
        }
        write_text(staging/"mod.cfg","format=1\ntarget="+std::string(target::id)+"\nname=Test Mod\ndescription="+std::string(modding_test_description)+"\nversion=1.0\n",false);
        if(std::filesystem::exists(destination)) throw std::runtime_error("Test Mod destination appeared during installation.");
        std::filesystem::rename(staging,destination);
    } catch(...) {
        // Remove only the known files created by this invocation.
        std::error_code ignored;
        for(const auto& path:std::array{output,staging/"files/Frontend",staging/"files",staging/"mod.cfg",staging})
            std::filesystem::remove(path,ignored);
        throw;
    }
    return report("installed");
}
Mods::Mods(const std::filesystem::path& root,std::span<const std::string> enabled,Disc& disc) {
    if(enabled.size()>max_mods) throw std::runtime_error("More than 128 mods enabled.");
    if(enabled.empty()) return;
    const auto mods=discover_mods(root);
    for(const auto& id:enabled) {
        if(!valid_mod_id(id) || std::ranges::find(enabled_,id)!=enabled_.end()) throw std::runtime_error("Invalid or duplicate enabled mod: "+id);
        const auto mod=std::ranges::find(mods,id,&Mod::id);
        if(mod==mods.end()) throw std::runtime_error("Enabled mod is missing: "+id);
        if(!mod->error.empty()) throw std::runtime_error("Mod "+id+": "+mod->error);
        enabled_.push_back(id);
        for(const auto& file:mod->files) {
            const auto original=disc.lookup(file.path);
            if(!original || original->directory()) throw std::runtime_error("Mod "+id+" must replace an existing disc file: "+file.path);
            if(const auto previous=files_.find(file.path);previous!=files_.end())
                conflicts_.push_back({file.path,previous->second.mod,id});
            files_.insert_or_assign(file.path,file);
        }
    }
}
const ModFile* Mods::lookup(std::string_view relative) const {
    if(files_.empty()) return nullptr;
    const auto found=files_.find(key(relative));return found==files_.end()?nullptr:&found->second;
}
std::string Mods::report() const {
    std::ostringstream report;report<<"{\"format\":\"b2-mods-v1\",\"target\":"<<json(target::id)<<",\"enabled\":[";
    for(unsigned i=0;i<enabled_.size();++i) {if(i) report<<',';report<<json(enabled_[i]);}
    report<<"],\"files\":[";unsigned count=0;
    for(const auto& [path,file]:files_) {
        if(count++) report<<',';
        report<<"{\"path\":"<<json(path)<<",\"mod\":"<<json(file.mod)<<",\"bytes\":"<<file.size<<'}';
    }
    report<<"],\"conflicts\":[";
    for(unsigned i=0;i<conflicts_.size();++i) {
        if(i) report<<',';const auto& conflict=conflicts_[i];
        report<<"{\"path\":"<<json(conflict.path)<<",\"previous\":"<<json(conflict.previous)<<",\"winner\":"<<json(conflict.winner)<<'}';
    }
    report<<"]}";return report.str();
}
std::string inspect_mods(const std::filesystem::path& disc_path,const std::filesystem::path& root,std::span<const std::string> enabled) {
    Disc disc(disc_path);const auto executable=disc.find("default.xbe");
    if(executable.size!=target::file_size || disc.sha256(executable)!=target::sha256)
        throw std::runtime_error("Mod inspection requires the verified supported disc.");
    return Mods(root,enabled,disc).report();
}
}
