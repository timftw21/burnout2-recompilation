#pragma once
#include <atomic>
#include <filesystem>
#include <functional>
#include <string>
#include <string_view>

namespace b2 {
using InstallProgress=std::function<void(unsigned,std::string_view)>;
// The absolute folder with the letter case stored on disk. Rejects symbolic
// links and junctions, but not differences in case or Windows short names.
std::filesystem::path real_folder(const std::filesystem::path& path);
std::string verify_install_disc(const std::filesystem::path& iso,const InstallProgress& progress={});
// Prepares data/local below directory: the verified executable, an optional
// image copy and settings.cfg file locations. Other preferences are kept.
enum class InstallDisc { reference, copy, already_in_place };
std::string install_game_files(const std::filesystem::path& iso,const std::filesystem::path& directory,InstallDisc location,
                               const InstallProgress& progress={},const std::atomic_bool* cancel=nullptr);
// Extracts and verifies directory/data/local/default.xbe from the selected disc,
// for the game's own first-run setup. Settings are left to the caller.
void extract_game_executable(const std::filesystem::path& iso,const std::filesystem::path& directory);
}
