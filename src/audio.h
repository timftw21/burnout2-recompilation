#pragma once
#include <array>
#include <atomic>
#include <cstddef>
#include <cstdint>
#include <memory>
#include <span>
#include <string>
#include <vector>

struct SDL_AudioStream;
namespace b2 {
struct EffectsImage;
struct SpatialResult;
struct AudioFormat {
    std::uint16_t tag=1,channels=1,block_align=2,bits=16,samples_per_block=0;
    std::uint32_t rate=48000;
    bool operator==(const AudioFormat&) const = default;
};
struct MixBin {std::uint32_t bin;std::int32_t volume;};
struct AudioPosition {std::uint32_t play,write;};
enum class AudioBus {none,submix,effects,effects_manual};
std::vector<float> decode_audio(AudioFormat,std::span<const std::byte>);
class Audio {
public:
    explicit Audio(bool output=true);
    ~Audio();
    Audio(const Audio&)=delete;
    Audio& operator=(const Audio&)=delete;
    std::uint32_t create(AudioFormat,AudioBus bus=AudioBus::none,std::uint32_t input_bin=UINT32_MAX);
    void release(std::uint32_t);
    void data(std::uint32_t,std::span<const std::byte>);
    void bind(std::uint32_t,std::span<const std::byte>,std::uint32_t source_address=0); // Caller keeps storage alive until unbound.
    void format(std::uint32_t,AudioFormat);
    void play(std::uint32_t,bool loop);
    void stop(std::uint32_t);
    std::uint32_t status(std::uint32_t);
    AudioPosition positions(std::uint32_t);
    std::uint32_t position(std::uint32_t);
    void position(std::uint32_t,std::uint32_t bytes);
    void region(std::uint32_t,bool loop,std::uint32_t start,std::uint32_t bytes);
    void volume(std::uint32_t,std::int32_t hundredths_db);
    void headroom(std::uint32_t,std::uint32_t hundredths_db);
    void mixbin_headroom(std::uint32_t bin,std::uint32_t shift);
    void mixbins(std::uint32_t,std::span<const MixBin>,bool volumes_only=false);
    void frequency(std::uint32_t,std::uint32_t hz);
    void route(std::uint32_t,std::uint32_t destination);
    void spatial(std::uint32_t,const SpatialResult&);
    void mix(std::span<float> stereo);
    std::uint32_t sample_clock() const {return frames_processed_.load(std::memory_order_relaxed);}
    void effects(const EffectsImage&,std::uint32_t reverb_index=UINT32_MAX);
    void effect_data(std::uint32_t byte_offset,std::span<std::byte> destination);
    void effect_data(std::uint32_t byte_offset,std::span<const std::byte> source,bool deferred);
    void commit_effects();
    std::uint32_t effect_x(std::uint32_t byte_offset,unsigned size);
    void effect_x(std::uint32_t byte_offset,unsigned size,std::uint32_t value);
    std::uint32_t effect_scratch(std::uint32_t byte_offset,unsigned size);
    void effect_scratch(std::uint32_t byte_offset,unsigned size,std::uint32_t value);
    std::uint32_t effect_program(std::uint32_t byte_offset,unsigned size);
    std::string failure();
    bool output_ready() const {return stream_!=nullptr;}
private:
    struct Voice {
        AudioFormat format;
        std::vector<std::byte> encoded;
        std::span<const std::byte> source;
        std::array<float,128> block{};
        std::uint32_t cached_block=UINT32_MAX,source_address=0;
        double cursor=0,step=1;
        float gain=1,headroom=1,spatial_gain=1;
        std::array<float,2> spatial_pan{1,1};
        std::array<float,2> front_back{1,0},environment_gain{1,0},environment_pole{};
        std::array<std::array<float,2>,2> environment_history{};
        double spatial_pitch=1;
        std::array<std::uint32_t,8> bins{0,1};
        std::array<float,32> bin_gains{};
        std::uint32_t bin_count=2,input_bin=UINT32_MAX;
        std::uint32_t byte_count=0,play_start=0,play_frames=0,loop_start=0,loop_frames=0,route=0;
        AudioBus bus=AudioBus::none;
        bool used=false,playing=false,loop=false;
    };
    Voice& voice(std::uint32_t);
    static std::uint32_t frames(AudioFormat,std::uint32_t bytes);
    std::array<float,2> sample(Voice&,std::uint32_t frame);
    void mix_unlocked(std::span<float>);
    void mix_sources(std::span<float>);
    void mix_bins(std::span<float>,unsigned frames);
    void mix_voice(Voice&,std::span<const std::array<float,2>>,std::span<float>,unsigned frames);
    struct Effects;
    std::unique_ptr<Effects> effects_;
    SDL_AudioStream* stream_=nullptr;
    std::array<Voice,256> voices_{};
    std::array<std::uint32_t,32> bin_headroom_{};
    std::atomic<std::uint32_t> frames_processed_{0};
    bool failed_=false;
    std::array<char,256> failure_{};
};
std::string check_audio();
}
