#include "presenter_debug_log.h"

#include <filesystem>
#include <iomanip>
#include <iostream>
#include <sstream>
#include <stdexcept>

namespace b2r::host {
namespace {

std::string escape_json(const std::string& value) {
    std::ostringstream out;
    for (const unsigned char ch : value) {
        switch (ch) {
            case '\\': out << "\\\\"; break;
            case '"': out << "\\\""; break;
            case '\b': out << "\\b"; break;
            case '\f': out << "\\f"; break;
            case '\n': out << "\\n"; break;
            case '\r': out << "\\r"; break;
            case '\t': out << "\\t"; break;
            default:
                if (ch < 0x20u) {
                    out << "\\u" << std::hex << std::setw(4) << std::setfill('0')
                        << static_cast<int>(ch);
                } else {
                    out << ch;
                }
                break;
        }
    }
    return out.str();
}

std::string json_string(const std::string& value) {
    return "\"" + escape_json(value) + "\"";
}

}  // namespace

void PresenterDebugLog::configure_live_mode(bool live_mode) {
    echo_stdout_ = !live_mode;
    flush_interval_ = live_mode ? 120u : 1u;
}

void PresenterDebugLog::open(const std::filesystem::path& path) {
    if (path.empty()) {
        enabled_ = false;
        return;
    }
    if (path.has_parent_path()) {
        std::filesystem::create_directories(path.parent_path());
    }
    file_.open(path, std::ios::out | std::ios::trunc);
    if (!file_) {
        throw std::runtime_error("failed to open debug json log: " + path.string());
    }
    path_ = path;
}

void PresenterDebugLog::emit(
    const std::string& event,
    std::initializer_list<std::pair<std::string, std::string>> fields) {
    if (!enabled_) {
        return;
    }
    std::ostringstream line;
    line << "{\"sequence\":" << sequence_++ << ",\"event\":" << json_string(event);
    for (const auto& [key, value] : fields) {
        line << ',' << json_string(key) << ':' << value;
    }
    line << '}';
    if (file_) {
        file_ << line.str() << '\n';
        if (++pending_lines_ >= flush_interval_) {
            file_.flush();
            pending_lines_ = 0u;
        }
    }
    if (echo_stdout_) {
        std::cout << line.str() << '\n';
    }
}

const std::filesystem::path& PresenterDebugLog::path() const {
    return path_;
}

bool PresenterDebugLog::enabled() const {
    return enabled_;
}

}  // namespace b2r::host
