#pragma once
#include "xbe.h"
#include <array>

namespace b2 {
using XboxKey=std::array<std::byte,16>;
struct XboxSaveKeys {
    XboxKey disk,signature,lan;
    std::array<XboxKey,16> alternate;
};
std::filesystem::path xbox_storage_root(const std::filesystem::path& override_root);
std::filesystem::path xbox_storage_child(const std::filesystem::path& parent,std::string_view name);
XboxSaveKeys xbox_save_keys(Xbe&,const std::filesystem::path& root);
std::string import_xbox_save(const std::filesystem::path& executable,const std::filesystem::path& source,
                             const std::filesystem::path& storage_override={});
}
