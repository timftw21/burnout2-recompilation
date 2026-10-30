#include "achievements.h"
#include "cpu.h"
#include "file.h"
#include <cmath>
#include <sstream>

namespace b2 {
void Achievements::unlock(AchievementId id) {
    const auto index=unsigned(id);
    if(!enabled_ || unlocked_.test(index)) return;
    unlocked_.set(index);pending_.set(index);dirty_=true;
}
void Achievements::enabled(bool value) {
    if(enabled_==value) return;
    enabled_=value;if(!enabled_) pending_.reset();dirty_=true;
}
void Achievements::reset() {
    unlocked_.reset();pending_.reset();dirty_=true;
}
void Achievements::crash_result(std::uint32_t zone,std::uint32_t medal) {
    // Original 12650 compares this run's score with the zone's three targets:
    // 0=no medal, 1=bronze, 2=silver, 3=gold. There are 30 crash zones.
    if(zone<30 && medal>=1 && medal<=3) unlock(AchievementId::crash_course);
}
std::optional<AchievementId> Achievements::take_notification() {
    for(unsigned i=0;i<achievement_definitions.size();++i) if(pending_.test(i)) {
        pending_.reset(i);return AchievementId(i);
    }
    return {};
}
void Achievements::load(const std::filesystem::path& path) {
    if(!std::filesystem::exists(path)) return;
    File file(path);
    if(file.size()>16384) throw std::runtime_error("Achievement file exceeds 16 KiB.");
    const auto bytes=file.read(0,std::size_t(file.size()));
    std::istringstream input(std::string(reinterpret_cast<const char*>(bytes.data()),bytes.size()));
    std::string line;
    const auto next=[&] {if(!std::getline(input,line)) return false;if(line.ends_with('\r')) line.pop_back();return true;};
    if(!next() || line!="b2-achievements-v1") throw std::runtime_error("Unsupported achievement file.");
    decltype(unlocked_) loaded;bool enabled=true;
    while(next()) {
        const auto split=line.find('=');if(split==std::string::npos) continue;
        if(line.substr(0,split)=="enabled") {
            const auto value=line.substr(split+1);
            if(value!="0" && value!="1") throw std::runtime_error("Invalid achievement setting.");
            enabled=value=="1";continue;
        }
        for(unsigned i=0;i<achievement_definitions.size();++i) if(line.substr(0,split)==achievement_definitions[i].id) {
            const auto value=line.substr(split+1);
            if(value!="0" && value!="1") throw std::runtime_error("Invalid achievement status.");
            loaded.set(i,value=="1");
        }
    }
    unlocked_=loaded;enabled_=enabled;pending_.reset();dirty_=false;
}
void Achievements::save(const std::filesystem::path& path) const {
    std::string text=std::string("b2-achievements-v1\nenabled=")+(enabled_?"1\n":"0\n");
    for(unsigned i=0;i<achievement_definitions.size();++i)
        text+=std::string(achievement_definitions[i].id)+(unlocked_.test(i)?"=1\n":"=0\n");
    write_text(path,text,true);
}
namespace {
bool observing(const Cpu& cpu,const Memory& memory) {
    return cpu.achievements && cpu.achievements->enabled() && !memory.load<std::uint32_t>(0x2FFD74);
}
bool player(const Memory& memory,std::uint32_t racer) {
    return racer && (racer==memory.load<std::uint32_t>(0x2FE370) || racer==memory.load<std::uint32_t>(0x2FE374)) &&
        memory.load<std::uint32_t>(racer+0xCC4)==1;
}
bool medals(const Memory& memory,std::uint32_t progress,unsigned count,unsigned gold) {
    for(unsigned i=0;i<count;++i) if(memory.load<std::uint32_t>(progress+i*4)!=gold) return false;
    return true;
}
}
void title_crash_result(Cpu& cpu,Memory& memory,std::uint32_t progress,std::uint32_t zone,std::uint32_t medal) {
    if(!observing(cpu,memory) || zone>=30 || medal<1 || medal>3) return;
    cpu.achievements->crash_result(zone,medal);
    if(medals(memory,progress,30,3)) cpu.achievements->unlock(AchievementId::see_you_in_hell);
}
void title_training_result(Cpu& cpu,Memory& memory,std::uint32_t progress) {
    // Driving 101 uses -1 for no medal and 0/1/2 for bronze/silver/gold.
    if(observing(cpu,memory) && memory.load<std::uint32_t>(progress+0x24)<=2 && medals(memory,progress,6,2))
        cpu.achievements->unlock(AchievementId::dangerous_driving);
}
void title_championship_result(Cpu& cpu,Memory& memory,std::uint32_t progress) {
    if(!observing(cpu,memory) || memory.load<std::uint32_t>(progress)>=22) return;
    // 11470 has accepted this event's win and stored its completion flag.
    const auto complete=[&](unsigned event) {return memory.load<std::uint32_t>(progress+(event+1)*0x9C)==1;};
    if(complete(3) && complete(7) && complete(11)) cpu.achievements->unlock(AchievementId::copped_cars);
    if(complete(14)) cpu.achievements->unlock(AchievementId::burnout_big_leagues);
    if(complete(21)) cpu.achievements->unlock(AchievementId::burnt_out);
}
void title_race_result(Cpu& cpu,Memory& memory,std::uint32_t stats) {
    if(!observing(cpu,memory) || !player(memory,memory.load<std::uint32_t>(stats+0x634)) ||
       !memory.load<std::uint32_t>(stats+0x2C) || memory.load<std::uint32_t>(stats+0x30) || memory.load<std::uint32_t>(stats+0x34)!=1) return;
    switch(memory.load<std::uint32_t>(0x34AB58)) {
    case 1:case 2:case 5:case 6:case 10:case 11: cpu.achievements->unlock(AchievementId::youre_winner);break;
    }
}
void title_starting_boost(Cpu& cpu,Memory& memory,std::uint32_t vehicle) {
    // This site is the timed accelerator/brake success, before the separate
    // automatic Crash Zone launch. FC0 links the physical car to its racer.
    if(observing(cpu,memory) && player(memory,memory.load<std::uint32_t>(vehicle+0xFC0)))
        cpu.achievements->unlock(AchievementId::started_in_second_gear);
}
void title_burnout_chain(Cpu& cpu,Memory& memory,std::uint32_t boost,std::uint32_t racer) {
    if(observing(cpu,memory) && player(memory,racer) && boost==racer+0x608 && memory.load<std::uint32_t>(boost+0x1C)>=15)
        cpu.achievements->unlock(AchievementId::burnout_x_achievement);
}
void title_time_attack_lap(Cpu& cpu,Memory& memory,std::uint32_t session,std::uint32_t time_bits) {
    if(!observing(cpu,memory) || session!=0x303C34 || memory.load<std::uint32_t>(0x34AB58)!=8 ||
       memory.load<std::uint32_t>(0x48A150)!=10) return;
    const float seconds=std::bit_cast<float>(time_bits);
    if(std::isfinite(seconds) && seconds>0 && seconds<=40.f) cpu.achievements->unlock(AchievementId::stay_frosty);
}
}
