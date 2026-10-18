#pragma once
#include "file.h"

namespace b2 {
inline constexpr unsigned imported_alpha_offset=0x63E00;
void validate_interstate(const std::filesystem::path& directory);
std::string import_interstate(const std::filesystem::path& burnout,
                             const std::filesystem::path& burnout2,
                             const std::filesystem::path& destination);
}
