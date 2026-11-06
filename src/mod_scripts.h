#pragma once
#include "mods.h"
#include <array>
#include <memory>

namespace b2 {
inline constexpr std::size_t max_script_bytes=64*1024;
inline constexpr std::string_view previous_test_mod_script="b2.show_message(\"Test Mod scripting is working.\")\n";
inline constexpr std::string_view test_mod_script=R"(b2.show_message("Test Mod scripting is working.")

b2.on("race_finish", function(event)
    b2.show_message("Test Mod: finished in position " .. event.position .. ".")
end)
)";
enum class ScriptEventType : unsigned {race_finish,crash_result,starting_boost,burnout,time_attack_lap,count};
inline constexpr std::array<std::string_view,unsigned(ScriptEventType::count)> script_event_names{
    "race_finish","crash_result","starting_boost","burnout","time_attack_lap"};
inline constexpr unsigned all_script_events=(1U<<unsigned(ScriptEventType::count))-1;
struct ScriptEvent {
    ScriptEventType type{};
    unsigned player=0,position=0,zone=0,medal=0,chain=0,track=0,mode=0;
    float seconds=0;
};
// One cooperative game thread produces these records. Observers never run Lua,
// allocate, log, or read files. Only subscribed events enter the bounded queue.
class ScriptEvents {
public:
    explicit ScriptEvents(unsigned subscriptions=0):subscriptions_(subscriptions) {}
    void push(const ScriptEvent& event) {
        if(unsigned(event.type)>=script_event_names.size() || !(subscriptions_&(1U<<unsigned(event.type)))) return;
        if(count_==records_.size()) {++dropped_;return;}
        records_[(begin_+count_++)%records_.size()]=event;
    }
    std::optional<ScriptEvent> pop() {
        if(!count_) return {};
        auto event=records_[begin_];begin_=(begin_+1)%records_.size();--count_;return event;
    }
private:
    friend class ModScripts;
    std::array<ScriptEvent,64> records_{};
    unsigned begin_=0,count_=0,subscriptions_=0,dropped_=0;
};
struct ScriptMessage {std::string mod,text;bool on_screen=false;};
struct ScriptRun {std::string mod;std::size_t peak_bytes=0;double elapsed_ms=0;std::optional<unsigned> camera_fov;unsigned handlers=0;std::string event;};
struct ScriptError {std::string mod,text;};
struct ScriptResult {
    std::optional<unsigned> camera_fov;
    std::vector<ScriptRun> runs;
    std::vector<ScriptMessage> messages;
    std::vector<ModConflict> conflicts;
    std::vector<ScriptError> errors;
    bool gameplay=false;
    std::string report() const;
};
class ModScripts {
public:
    ModScripts(std::span<const ModFile>,unsigned camera_fov=100);
    ~ModScripts();
    ModScripts(const ModScripts&)=delete;
    ModScripts& operator=(const ModScripts&)=delete;
    const ScriptResult& startup() const;
    ScriptEvents& events();
    ScriptResult dispatch(unsigned camera_fov); // Called at completed frame boundaries.
private:
    struct State;
    std::unique_ptr<State> state_;
};
ScriptResult run_startup_scripts(std::span<const ModFile>,unsigned camera_fov=100);
std::string create_script_mod(const std::filesystem::path& directory,std::string_view name);
std::string inspect_scripts(const std::filesystem::path& disc,const std::filesystem::path& root,std::span<const std::string> enabled);
std::string check_scripting(const std::filesystem::path& directory);
}
