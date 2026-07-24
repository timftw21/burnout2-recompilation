#pragma once

#include <cstdint>
#include <filesystem>
#include <fstream>
#include <initializer_list>
#include <string>
#include <utility>

namespace b2r::host {

class PresenterDebugLog {
public:
    void configure_live_mode(bool live_mode);
    void open(const std::filesystem::path& path);
    void emit(
        const std::string& event,
        std::initializer_list<std::pair<std::string, std::string>> fields = {});

    const std::filesystem::path& path() const;
    bool enabled() const;

private:
    uint64_t sequence_ = 0u;
    uint32_t pending_lines_ = 0u;
    uint32_t flush_interval_ = 1u;
    bool echo_stdout_ = true;
    bool enabled_ = true;
    std::ofstream file_;
    std::filesystem::path path_;
};

}  // namespace b2r::host
