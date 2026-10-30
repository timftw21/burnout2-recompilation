#pragma once
#include <array>
#include <bitset>
#include <cstdint>
#include <filesystem>
#include <optional>

namespace b2 {
enum class AchievementId : unsigned {
    crash_course, see_you_in_hell, dangerous_driving, youre_winner, copped_cars,
    burnout_big_leagues, burnt_out, started_in_second_gear, burnout_x_achievement, stay_frosty, count
};
struct AchievementDefinition { const char* id; const char* title; const char* description; };
inline constexpr std::array achievement_definitions{
    // Keep the original save key so the test achievement carries over.
    AchievementDefinition{"crash_course","Crash + Burn","Earn bronze or better on any Crash Zone."},
    AchievementDefinition{"see_you_in_hell","See You in Hell","Earn gold on all Crash Zones."},
    AchievementDefinition{"dangerous_driving","Dangerous Driving","Earn gold on all Offensive Driving 101 lessons."},
    AchievementDefinition{"youre_winner","You're Winner !","Earn first place in any race."},
    AchievementDefinition{"copped_cars","Copped Cars","Complete Pursuit 1, Pursuit 2, and Pursuit 3 in Championship mode."},
    AchievementDefinition{"burnout_big_leagues","Burnout Big Leagues","Complete Championship mode."},
    AchievementDefinition{"burnt_out","Burnt Out","Complete Custom Series Championship mode."},
    AchievementDefinition{"started_in_second_gear","Started in Second Gear","Activate a starting boost."},
    AchievementDefinition{"burnout_x_achievement","Burnout x Achievement","Achieve a Burnout x 15 or higher."},
    AchievementDefinition{"stay_frosty","Stay Frosty","Achieve a time of 0:40.00 or faster on Crystal Summit Peak in Time Attack mode."}
};
static_assert(achievement_definitions.size()==unsigned(AchievementId::count));

class Achievements {
public:
    void unlock(AchievementId);
    void crash_result(std::uint32_t zone,std::uint32_t medal);
    bool unlocked(AchievementId id) const {return unlocked_.test(unsigned(id));}
    unsigned count() const {return unsigned(unlocked_.count());}
    bool enabled() const {return enabled_;}
    void enabled(bool);
    void reset();
    std::optional<AchievementId> take_notification();
    void load(const std::filesystem::path&);
    void save(const std::filesystem::path&) const;
    bool dirty() const {return dirty_;}
    void dirty(bool value) {dirty_=value;}
private:
    std::bitset<achievement_definitions.size()> unlocked_,pending_;
    bool dirty_=false,enabled_=true;
};
}
