#pragma once
#include <atomic>
#include <filesystem>
#include <functional>
#include <mutex>
#include <string>
#include <thread>

namespace b2 {
struct MusicImportProgress {
    unsigned completed=0,total=0;
    bool finished=false,succeeded=false;
    std::string file,message;
};
// Import once, outside gameplay. The original Xbox code reads the resulting
// catalogue and WMA files through the normal native file services.
unsigned import_custom_music(const std::filesystem::path& source,const std::filesystem::path& destination,
    const std::function<bool(unsigned,unsigned,std::string_view)>& progress={});
unsigned custom_music_count(const std::filesystem::path& directory);
class MusicImport {
public:
    ~MusicImport();
    void start(const std::filesystem::path& source,const std::filesystem::path& destination);
    void cancel();
    MusicImportProgress status() const;
private:
    mutable std::mutex mutex_;
    MusicImportProgress progress_;
    std::atomic<bool> cancelled_{false};
    std::thread worker_;
};
}
