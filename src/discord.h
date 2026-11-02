#pragma once
#include <memory>
#include <string_view>

namespace b2 {
class DiscordPresence {
public:
    explicit DiscordPresence(std::string_view application);
    ~DiscordPresence();
    void update(bool enabled,bool playing,bool settings);
    std::string_view status() const;
private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};
}
