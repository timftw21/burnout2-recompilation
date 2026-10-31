#pragma once
#include "render.h"
#include <optional>

namespace b2 {
inline constexpr unsigned ui_art_scale=8;
inline constexpr unsigned ui_art_mips=4; // 8x, 4x, 2x and original atlas size.
struct UiArt {TextureFormat format;std::vector<std::byte> bytes;};
bool ui_art_candidate(const TextureData&);
// Exact original atlases only, at 8x size with coverage mips down to 1x.
// The white font retains BC3 storage; colors use uncompressed BGRA.
std::optional<UiArt> redraw_ui_texture(const TextureData&);
}
