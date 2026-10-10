#include "audio.h"
#include "performance.h"
#include "dsp.h"
#include "spatial.h"
#include "file.h"
#include <SDL3/SDL.h>
#include <algorithm>
#include <bit>
#include <bitset>
#include <cmath>
#include <cstring>
#include <cstdio>
#include <format>
#include <stdexcept>
#include <tuple>

namespace b2 {
namespace {
constexpr int steps[]={7,8,9,10,11,12,13,14,16,17,19,21,23,25,28,31,34,37,41,45,50,55,60,66,73,80,88,97,
    107,118,130,143,157,173,190,209,230,253,279,307,337,371,408,449,494,544,598,658,724,796,876,963,1060,1166,
    1282,1411,1552,1707,1878,2066,2272,2499,2749,3024,3327,3660,4026,4428,4871,5358,5894,6484,7132,7845,
    8630,9493,10442,11487,12635,13899,15289,16818,18500,20350,22385,24623,27086,29794,32767};
constexpr int indices[]={-1,-1,-1,-1,2,4,6,8,-1,-1,-1,-1,2,4,6,8};
struct StreamLock {
    SDL_AudioStream* stream;
    explicit StreamLock(SDL_AudioStream* value):stream(value) {
        if(stream && !SDL_LockAudioStream(stream)) throw std::runtime_error(SDL_GetError());
    }
    ~StreamLock(){if(stream) SDL_UnlockAudioStream(stream);}
};
void validate(AudioFormat format,bool bus=false) {
    if((format.channels!=1 && format.channels!=2) || format.rate<100 || format.rate>200000)
        throw std::runtime_error("Unsupported audio channel count or sample rate");
    if(format.tag==1) {
        // The SDK's effect-input bus uses 24-bit mixer precision in a
        // four-byte container. Its samples come from mix bins, not PCM bytes.
        const bool mixer=bus && format.bits==24 && format.block_align==format.channels*4;
        if(!mixer && ((format.bits!=8 && format.bits!=16) || format.block_align!=format.channels*format.bits/8))
            throw std::runtime_error("Invalid PCM format");
    } else if(format.tag==0x69) {
        if(format.bits!=4 || format.block_align!=36*format.channels || format.samples_per_block!=64)
            throw std::runtime_error("Invalid Xbox ADPCM format");
    } else throw std::runtime_error("Unsupported audio codec");
}
void decode_adpcm(AudioFormat format,const std::byte* source,std::span<float> result) {
    for(unsigned channel=0;channel<format.channels;++channel) {
        std::int16_t initial;std::memcpy(&initial,source+channel*4,2);
        int predictor=initial,index=std::to_integer<unsigned>(source[channel*4+2]);
        if(index>88) throw std::runtime_error("Invalid Xbox ADPCM step index");
        result[channel]=predictor/32768.0f;
        for(unsigned sample=1;sample<64;++sample) {
            const auto nibble_index=sample-1,byte_index=nibble_index/2;
            const auto offset=format.channels*4+(byte_index/4)*format.channels*4+channel*4+byte_index%4;
            const unsigned packed=std::to_integer<unsigned>(source[offset]);
            const auto nibble=(packed>>((nibble_index&1)*4))&15;
            const int step=steps[index];
            const int difference=(step>>3)+((nibble&1)?step>>2:0)+((nibble&2)?step>>1:0)+((nibble&4)?step:0);
            predictor=std::clamp(predictor+((nibble&8)?-difference:difference),-32768,32767);
            index=std::clamp(index+indices[nibble],0,88);
            result[sample*format.channels+channel]=predictor/32768.0f;
        }
    }
}
}
struct Audio::Effects {
    Dsp state;
    std::array<std::byte,0x6000> pending{};
    std::bitset<0x6000> dirty;
    std::uint32_t dirty_first=0x6000,dirty_end=0;
    unsigned count=0,available=0,cursor=0;
    bool reverb=false;
    std::array<float,64> output{};
    std::vector<std::byte> program;
    explicit Effects(const EffectsImage& image,std::uint32_t reverb_index):state(image),count(static_cast<unsigned>(image.effects.size())) {
        if(reverb_index!=UINT32_MAX) {
            if(reverb_index>=image.effects.size()) throw std::runtime_error("Invalid environmental audio effect index");
            const auto& effect=image.effects[reverb_index];
            const auto state_base=0x80+(effect.descriptor[2]-image.data_file_offset)/4;
            if(state.load(state_base+5)!=0x1540) throw std::runtime_error("Environmental audio effect does not consume mix bin 10");
            reverb=true;
        }
        program.reserve(image.program_words*4);
        for(const auto& effect:image.effects) program.insert(program.end(),effect.code.begin(),effect.code.end());
        program.resize(image.program_words*4);
    }
    auto fields() {
        return std::tie(state.x,state.registers,state.accumulators,state.dma_next,state.dma_control,state.interrupts,
            state.transferred_words,state.work_remaining,state.active_effect,state.negative,state.zero,state.overflow,
            pending,dirty,dirty_first,dirty_end,count,available,cursor,reverb,output);
    }
};
namespace {
struct Level {
    double energy=0;float peak=0;std::uint64_t samples=0,nonzero=0;
    void add(float value) {
        if(!std::isfinite(value)) throw std::runtime_error("Non-finite audio sample");
        energy+=double(value)*value;peak=std::max(peak,std::abs(value));++samples;nonzero+=value!=0;
    }
    std::string report() const {
        return std::format("{{\"peak\":{},\"rms\":{},\"samples\":{},\"nonzero\":{}}}",peak,samples?std::sqrt(energy/samples):0,samples,nonzero);
    }
};
template<class Tuple> constexpr std::size_t field_size(const Tuple& fields) {
    return std::apply([](const auto&... value){return (sizeof(value)+...);},fields);
}
void write_wav(const std::filesystem::path& path,std::span<const float> samples) {
    std::ofstream file(path,std::ios::binary);if(!file) throw std::runtime_error("Cannot create audio diagnostic WAV");
    const auto word=[&](auto value){file.write(reinterpret_cast<const char*>(&value),sizeof(value));};
    file.write("RIFF",4);word(std::uint32_t(36+samples.size()*2));file.write("WAVEfmt ",8);word(std::uint32_t(16));
    word(std::uint16_t(1));word(std::uint16_t(2));word(std::uint32_t(48000));word(std::uint32_t(192000));
    word(std::uint16_t(4));word(std::uint16_t(16));file.write("data",4);word(std::uint32_t(samples.size()*2));
    for(float value:samples) {
        if(!std::isfinite(value)) throw std::runtime_error("Cannot export non-finite audio sample");
        word(static_cast<std::int16_t>(std::clamp(value*32768.f,-32768.f,32767.f)));
    }
    if(!file) throw std::runtime_error("Audio diagnostic WAV write failed");
}
}
struct Audio::Probe {
    std::array<Level,256> decoded,sent;
    std::array<Level,32> bins,dsp;
};
struct Audio::Capture {
    std::array<VoiceState,256> voices{};
    std::array<std::uint32_t,256> offsets{};
    std::array<std::uint32_t,32> headroom{};
    std::vector<std::byte> data;
    std::unique_ptr<Effects> effects;
    std::uint32_t clock=0;
};
std::unique_ptr<Audio::Capture> Audio::snapshot() {
    auto capture=std::make_unique<Capture>();
    std::array<std::span<const std::byte>,256> sources;
    std::vector<std::pair<std::uintptr_t,std::uintptr_t>> ranges;ranges.reserve(256);
    {
        StreamLock lock(stream_);
        for(unsigned i=0;i<voices_.size();++i) {
            capture->voices[i]=static_cast<const VoiceState&>(voices_[i]);sources[i]=voices_[i].source;
            if(!sources[i].empty()) {
                const auto first=reinterpret_cast<std::uintptr_t>(sources[i].data());
                ranges.emplace_back(first,first+sources[i].size());
            }
        }
        capture->headroom=bin_headroom_;capture->clock=sample_clock();
        if(effects_) capture->effects=std::make_unique<Effects>(*effects_);
    }
    // Guest code is suspended by the caller. SDL reads these sources but never
    // changes their storage. Merge shared/overlapping banks before copying them.
    std::ranges::sort(ranges);std::vector<std::pair<std::uintptr_t,std::uintptr_t>> merged;
    for(const auto& range:ranges) {
        if(!merged.empty() && range.first<merged.back().second) merged.back().second=std::max(merged.back().second,range.second);
        else merged.push_back(range);
    }
    std::size_t total=0;for(const auto& range:merged) total+=range.second-range.first;
    if(total>64*1024*1024) throw std::runtime_error("Audio capture source banks exceed 64 MiB");
    capture->data.resize(total);capture->offsets.fill(UINT32_MAX);std::size_t offset=0;
    for(const auto& range:merged) {
        const auto size=range.second-range.first;
        std::memcpy(capture->data.data()+offset,reinterpret_cast<const void*>(range.first),size);
        for(unsigned i=0;i<sources.size();++i) {
            const auto first=reinterpret_cast<std::uintptr_t>(sources[i].data());
            if(!sources[i].empty() && first>=range.first && first+sources[i].size()<=range.second)
                capture->offsets[i]=static_cast<std::uint32_t>(offset+first-range.first);
        }
        offset+=size;
    }
    return capture;
}
void Audio::restore(const Capture& capture) {
    for(unsigned i=0;i<voices_.size();++i) {
        auto& value=voices_[i];value=Voice{};static_cast<VoiceState&>(value)=capture.voices[i];
        if(!value.used) continue;
        validate(value.format,value.bus!=AudioBus::none);
        const auto invalid=[](float number){return !std::isfinite(number);};
        if(static_cast<unsigned>(value.bus)>static_cast<unsigned>(AudioBus::effects_manual) ||
            (value.bus==AudioBus::none?value.input_bin!=UINT32_MAX:value.input_bin>=32) ||
            value.bin_count>8 || value.route>256 ||
            !std::isfinite(value.cursor) || value.cursor<0 || !std::isfinite(value.step) || value.step<=0 ||
            !std::isfinite(value.spatial_pitch) || value.spatial_pitch<=0 ||
            invalid(value.gain) || invalid(value.headroom) || invalid(value.spatial_gain) ||
            std::ranges::any_of(value.block,invalid) || std::ranges::any_of(value.bin_gains,invalid) ||
            std::ranges::any_of(value.spatial_pan,invalid) || std::ranges::any_of(value.front_back,invalid) ||
            std::ranges::any_of(value.environment_gain,invalid) ||
            std::ranges::any_of(value.environment_pole,[](float pole){return !std::isfinite(pole) || pole<0 || pole>=1;}) ||
            std::ranges::any_of(value.environment_history,[&](const auto& path){return std::ranges::any_of(path,invalid);}))
            throw std::runtime_error("Invalid captured audio voice controls");
        for(unsigned bin=0;bin<value.bin_count;++bin) if(value.bins[bin]>=32) throw std::runtime_error("Invalid captured audio mix bin");
        if(value.bus==AudioBus::none) {
            const auto offset=capture.offsets[i];
            if(value.byte_count && (offset>capture.data.size() || value.byte_count>capture.data.size()-offset))
                throw std::runtime_error("Captured audio source exceeds its bank");
            if(value.byte_count) value.source=std::span(capture.data).subspan(offset,value.byte_count);
            const auto capacity=value.byte_count/value.format.block_align*(value.format.tag==0x69?64:1);
            if(value.play_start>capacity || value.play_frames>capacity-value.play_start || value.loop_start>value.play_frames ||
                value.loop_frames>value.play_frames-value.loop_start || (value.playing && !value.play_frames))
                throw std::runtime_error("Captured audio play/loop region exceeds its bank");
        }
    }
    for(unsigned i=0;i<voices_.size();++i) if(voices_[i].used) {
        unsigned count=0;
        for(auto cursor=voices_[i].route;cursor;) {
            if(cursor>256 || ++count>256 || !voices_[cursor-1].used) throw std::runtime_error("Invalid captured audio routing graph");
            cursor=voices_[cursor-1].route;
        }
    }
    bin_headroom_=capture.headroom;for(auto value:bin_headroom_) if(value>7) throw std::runtime_error("Invalid captured audio bin headroom");
    frames_processed_.store(capture.clock,std::memory_order_relaxed);
    effects_=capture.effects?std::make_unique<Effects>(*capture.effects):nullptr;
}
void Audio::capture(const std::filesystem::path& directory) {
    const auto saved=snapshot();std::filesystem::create_directories(directory);
    const auto path=directory/"audio.bin";
    if(std::filesystem::exists(path)) throw std::runtime_error("Audio capture already exists");
    std::ofstream file(path,std::ios::binary);if(!file) throw std::runtime_error("Cannot create audio capture");
    const auto write=[&](const auto& value){file.write(reinterpret_cast<const char*>(&value),sizeof(value));};
    file.write("B2AUD001",8);
    const std::array<std::uint32_t,6> header={sizeof(VoiceState),static_cast<std::uint32_t>(saved->data.size()),saved->clock,
        saved->effects?static_cast<std::uint32_t>(field_size(saved->effects->fields())):0,
        saved->effects?static_cast<std::uint32_t>(saved->effects->state.scratch.size()):0,256};
    write(header);write(saved->voices);write(saved->offsets);write(saved->headroom);
    if(saved->effects) {
        std::apply([&](const auto&... value){(write(value),...);},saved->effects->fields());
        const auto& scratch=saved->effects->state.scratch;
        file.write(reinterpret_cast<const char*>(scratch.data()),scratch.size());
    }
    file.write(reinterpret_cast<const char*>(saved->data.data()),saved->data.size());
    if(!file) throw std::runtime_error("Audio capture write failed");
}
std::string Audio::inspect_capture(const std::filesystem::path& source,const std::filesystem::path& image_path,
                                 const std::filesystem::path& output) {
    if(std::filesystem::exists(output)) throw std::runtime_error("Audio inspection requires a fresh output directory");
    File file(source);const auto header=file.read(0,32);
    if(std::memcmp(header.data(),"B2AUD001",8) || u32(header,8)!=sizeof(VoiceState) || u32(header,28)!=256 ||
        u32(header,12)>64*1024*1024 || u32(header,24)>16*1024*1024)
        throw std::runtime_error("Unsupported audio capture format or bounds");
    auto saved=std::make_unique<Capture>();saved->clock=u32(header,16);std::uint64_t position=32;
    const auto read=[&](auto& value){file.read(position,std::as_writable_bytes(std::span(&value,1)));position+=sizeof(value);};
    read(saved->voices);read(saved->offsets);read(saved->headroom);
    if(u32(header,20)) {
        File effects_file(image_path);if(effects_file.size()>65536) throw std::runtime_error("Captured audio effect image exceeds 64 KiB");
        const auto image=decode_effects(effects_file.read(0,static_cast<std::size_t>(effects_file.size())));
        if(!compiled_effects_match(image)) throw std::runtime_error("Captured audio needs its statically compiled effect image");
        saved->effects=std::make_unique<Effects>(image,UINT32_MAX);
        if(u32(header,20)!=field_size(saved->effects->fields()) || u32(header,24)!=saved->effects->state.scratch.size())
            throw std::runtime_error("Captured audio effect layout differs from this build");
        std::apply([&](auto&... value){(read(value),...);},saved->effects->fields());
        auto& effect=*saved->effects;
        if(effect.count!=image.effects.size() || effect.available>32 || effect.cursor>32 || effect.available+effect.cursor>32 ||
            effect.dirty_first>0x6000 || effect.dirty_end>0x6000 ||
            std::ranges::any_of(effect.output,[](float sample){return !std::isfinite(sample);}))
            throw std::runtime_error("Invalid captured audio effect block");
        file.read(position,effect.state.scratch);position+=effect.state.scratch.size();
    } else if(u32(header,24)) throw std::runtime_error("Audio capture has scratch without effects");
    saved->data.resize(u32(header,12));file.read(position,saved->data);position+=saved->data.size();
    if(position!=file.size()) throw std::runtime_error("Audio capture length differs from its declared layout");
    Audio replay(false);replay.restore(*saved);Probe probe;replay.probe_=&probe;
    std::array<Level,256> payload;
    std::vector<std::string> failures;
    for(unsigned id=0;id<replay.voices_.size();++id) {
        auto& voice=replay.voices_[id];if(!voice.used || voice.bus!=AudioBus::none || !voice.play_frames) continue;
        const auto original=static_cast<const VoiceState&>(voice);voice.cached_block=UINT32_MAX;
        try {
            for(unsigned frame=0;frame<std::min(voice.play_frames,voice.format.rate);++frame) {
                const auto decoded=replay.sample(voice,voice.play_start+frame);
                for(unsigned channel=0;channel<voice.format.channels;++channel) payload[id].add(decoded[channel]);
            }
        } catch(const std::exception& error) {failures.push_back(std::format("voice {} payload: {}",id+1,error.what()));}
        static_cast<VoiceState&>(voice)=original;
    }
    std::filesystem::create_directories(output);std::vector<float> mixed(48000*2*2);
    try {replay.mix(mixed);} catch(const std::exception& error) {failures.push_back(error.what());}
    write_wav(output/"scene.wav",mixed);Level final;for(float value:mixed) final.add(value);
    std::string voices,bins,errors;
    for(unsigned i=0;i<saved->voices.size();++i) {
        const auto& voice=saved->voices[i];if(!voice.used) continue;
        if(!voices.empty()) voices+=',';
        std::string assignments;
        for(unsigned n=0;n<voice.bin_count;++n) {
            if(n) assignments+=',';const auto bin=voice.bins[n];
            assignments+=std::format("{{\"bin\":{},\"gain\":{},\"headroom_shift\":{}}}",bin,voice.bin_gains[bin],saved->headroom[bin]);
        }
        voices+=std::format("{{\"id\":{},\"playing\":{},\"loop\":{},\"bus\":{},\"input_bin\":{},\"route\":{},\"source_address\":{},\"bytes\":{},\"codec\":{},\"channels\":{},\"rate\":{},\"cursor\":{},\"play_start\":{},\"play_frames\":{},\"loop_start\":{},\"loop_frames\":{},\"gain\":{},\"headroom\":{},\"spatial_gain\":{},\"pan\":[{},{}],\"environment_gain\":[{},{}],\"front_back\":[{},{}],\"bins\":[{}],\"payload\":{},\"decoded\":{},\"sent\":{}}}",
            i+1,voice.playing,voice.loop,static_cast<unsigned>(voice.bus),voice.input_bin,voice.route,json(hex32(voice.source_address)),voice.byte_count,
            voice.format.tag,voice.format.channels,voice.format.rate,voice.cursor,voice.play_start,voice.play_frames,voice.loop_start,voice.loop_frames,
            voice.gain,voice.headroom,voice.spatial_gain,voice.spatial_pan[0],voice.spatial_pan[1],voice.environment_gain[0],voice.environment_gain[1],
            voice.front_back[0],voice.front_back[1],assignments,payload[i].report(),probe.decoded[i].report(),probe.sent[i].report());
    }
    for(unsigned i=0;i<32;++i) {
        if(i) bins+=',';bins+=std::format("{{\"bin\":{},\"input\":{},\"processed\":{}}}",i,probe.bins[i].report(),probe.dsp[i].report());
    }
    for(const auto& error:failures){if(!errors.empty()) errors+=',';errors+=json(error);}
    const auto report=std::format("{{\"format\":\"b2-audio-inspection-v1\",\"passed\":{},\"game_booted\":false,\"audio_device_opened\":false,\"frozen_controls\":true,\"master_gain_applied\":false,\"sent_meter_channels\":32,\"frames\":96000,\"source_bytes\":{},\"clock\":{},\"output\":{},\"voices\":[{}],\"bins\":[{}],\"failures\":[{}]}}",
        failures.empty(),saved->data.size(),saved->clock,final.report(),voices,bins,errors);
    write_text(output/"report.json",report,false);
    return std::format("{{\"format\":\"b2-audio-inspection-v1\",\"passed\":{},\"game_booted\":false,\"audio_device_opened\":false,\"voices\":{},\"frames\":96000,\"report\":{},\"failures\":[{}]}}",
        failures.empty(),std::ranges::count_if(saved->voices,[](const auto& voice){return voice.used;}),json(utf8((output/"report.json").wstring())),errors);
}
std::vector<float> decode_audio(AudioFormat format,std::span<const std::byte> data) {
    validate(format);
    if(data.size()%format.block_align) throw std::runtime_error("Audio payload ends inside a sample block");
    if(format.tag==1) {
        std::vector<float> result(data.size()/(format.bits/8));
        for(std::size_t i=0;i<result.size();++i) {
            if(format.bits==8) result[i]=(std::to_integer<int>(data[i])-128)/128.0f;
            else {std::int16_t value;std::memcpy(&value,data.data()+i*2,2);result[i]=value/32768.0f;}
        }
        return result;
    }
    const auto blocks=data.size()/format.block_align;
    std::vector<float> result(blocks*64*format.channels);
    for(std::size_t block=0;block<blocks;++block)
        decode_adpcm(format,data.data()+block*format.block_align,std::span(result).subspan(block*64*format.channels,64*format.channels));
    return result;
}
Audio::Audio(bool output,float gain) {
    if(!(gain>=0 && gain<=1)) throw std::runtime_error("Output volume must be between zero and one");
    if(!output) return;
    if(!SDL_InitSubSystem(SDL_INIT_AUDIO)) throw std::runtime_error(SDL_GetError());
    const SDL_AudioSpec specification{SDL_AUDIO_F32,2,48000};
    stream_=SDL_OpenAudioDeviceStream(SDL_AUDIO_DEVICE_DEFAULT_PLAYBACK,&specification,
        [](void* context,SDL_AudioStream* stream,int additional,int) {
            auto& audio=*static_cast<Audio*>(context);
            std::array<float,1024> buffer{};
            while(additional>0) {
                const auto frames=std::min<std::size_t>((static_cast<unsigned>(additional)+7)/8,buffer.size()/2);
                try {audio.mix_unlocked(std::span(buffer).first(frames*2));}
                catch(const std::exception& error) {
                    audio.failed_=true;std::snprintf(audio.failure_.data(),audio.failure_.size(),"%s",error.what());break;
                }
                const int bytes=static_cast<int>(frames*2*sizeof(float));
                if(!SDL_PutAudioStreamData(stream,buffer.data(),bytes)) {audio.failed_=true;
                    std::snprintf(audio.failure_.data(),audio.failure_.size(),"%s",SDL_GetError());break;}
                additional-=bytes;
            }
        },this);
    if(!stream_) {const std::string error=SDL_GetError();SDL_QuitSubSystem(SDL_INIT_AUDIO);throw std::runtime_error(error);}
    if(!SDL_SetAudioStreamGain(stream_,gain) || !SDL_ResumeAudioStreamDevice(stream_)) {
        const std::string error=SDL_GetError();SDL_DestroyAudioStream(stream_);stream_=nullptr;
        SDL_QuitSubSystem(SDL_INIT_AUDIO);throw std::runtime_error(error);
    }
}
Audio::~Audio(){if(stream_){SDL_DestroyAudioStream(stream_);SDL_QuitSubSystem(SDL_INIT_AUDIO);}}
void Audio::performance(bool enabled) {
    StreamLock lock(stream_);
    if(enabled) {mix_batches_=0;mix_ns_=0;mix_overruns_=0;}
    performance_enabled_.store(enabled,std::memory_order_relaxed);
}
AudioPerformance Audio::performance() const {return {mix_batches_.load(std::memory_order_relaxed),mix_ns_.load(std::memory_order_relaxed),mix_overruns_.load(std::memory_order_relaxed)};}
void Audio::output_gain(float gain) {
    if(!(gain>=0 && gain<=1)) throw std::runtime_error("Output volume must be between zero and one");
    if(stream_ && !SDL_SetAudioStreamGain(stream_,gain)) throw std::runtime_error(SDL_GetError());
}
Audio::Voice& Audio::voice(std::uint32_t id) {
    if(failed_) throw std::runtime_error(failure_.data());
    if(!id || id>voices_.size() || !voices_[id-1].used) throw std::runtime_error("Invalid native audio voice");
    return voices_[id-1];
}
std::uint32_t Audio::create(AudioFormat format,AudioBus bus,std::uint32_t input_bin) {
    if(bus==AudioBus::submix && input_bin==UINT32_MAX) input_bin=31;
    if((bus==AudioBus::none && input_bin!=UINT32_MAX) || (bus!=AudioBus::none && input_bin>=32))
        throw std::runtime_error("Invalid audio input mix bin");
    validate(format,bus!=AudioBus::none);StreamLock lock(stream_);
    for(unsigned i=0;i<voices_.size();++i) if(!voices_[i].used) {
        auto& value=voices_[i];value=Voice{};value.format=format;value.step=format.rate/48000.0;
        value.used=true;value.bus=bus;value.input_bin=input_bin;value.bin_gains.fill(1);return i+1;
    }
    throw std::runtime_error("Native audio voice capacity exhausted");
}
void Audio::release(std::uint32_t id) {
    Voice retired;
    {StreamLock lock(stream_);retired=std::move(voice(id));voices_[id-1]=Voice{};
        for(auto& value:voices_) if(value.route==id) value.route=0;}
}
std::uint32_t Audio::frames(AudioFormat format,std::uint32_t bytes) {
    if(bytes%format.block_align) throw std::runtime_error("Audio region is not block-aligned");
    return bytes/format.block_align*(format.tag==0x69?64:1);
}
void Audio::data(std::uint32_t id,std::span<const std::byte> bytes) {
    AudioFormat format;{StreamLock lock(stream_);const auto& value=voice(id);format=value.format;
        if(value.bus!=AudioBus::none && !bytes.empty()) throw std::runtime_error("Mix buses cannot carry encoded sample data");}
    frames(format,static_cast<std::uint32_t>(bytes.size()));
    std::vector<std::byte> encoded(bytes.begin(),bytes.end());
    StreamLock lock(stream_);auto& value=voice(id);
    value.encoded.swap(encoded);value.source=value.encoded;value.cached_block=UINT32_MAX;value.source_address=0;
    value.byte_count=static_cast<std::uint32_t>(bytes.size());
    value.cursor=0;value.play_start=0;value.play_frames=frames(value.format,value.byte_count);
    value.environment_history={};
    value.loop_start=0;value.loop_frames=value.play_frames;value.playing=false;
}
void Audio::bind(std::uint32_t id,std::span<const std::byte> bytes,std::uint32_t source_address) {
    std::vector<std::byte> retired;
    StreamLock lock(stream_);auto& value=voice(id);
    if(value.bus!=AudioBus::none && !bytes.empty()) throw std::runtime_error("Mix buses cannot bind encoded sample data");
    value.encoded.swap(retired);value.source=bytes;value.cached_block=UINT32_MAX;value.source_address=source_address;
    value.byte_count=static_cast<std::uint32_t>(bytes.size());value.cursor=0;value.play_start=0;
    value.environment_history={};
    value.play_frames=value.byte_count/value.format.block_align*(value.format.tag==0x69?64:1);
    value.loop_start=0;value.loop_frames=value.play_frames;value.playing=false;
}
void Audio::format(std::uint32_t id,AudioFormat format) {
    StreamLock lock(stream_);auto& value=voice(id);validate(format,value.bus!=AudioBus::none);
    if(value.format==format) {value.step=format.rate/48000.0;return;}
    const auto bytes=[&](std::uint32_t count){return count/(value.format.tag==0x69?64:1)*value.format.block_align;};
    // The bound size is memory capacity; only complete codec blocks are playable.
    // Default regions can include allocation padding after the last block.
    const auto count=[&](std::uint32_t length){return length/format.block_align*(format.tag==0x69?64:1);};
    const auto play_start=frames(format,bytes(value.play_start));
    const auto play_frames=count(bytes(value.play_start+value.play_frames))-play_start;
    const auto loop_start=frames(format,bytes(value.loop_start));
    const auto loop_frames=count(bytes(value.loop_start+value.loop_frames))-loop_start;
    const auto cursor_bytes=bytes(static_cast<std::uint32_t>(value.cursor));
    const auto cursor=cursor_bytes/format.block_align*(format.tag==0x69?64:1);
    value.format=format;value.step=format.rate/48000.0;value.cached_block=UINT32_MAX;
    value.environment_history={};
    value.play_start=play_start;value.play_frames=play_frames;value.loop_start=loop_start;value.loop_frames=loop_frames;
    value.cursor=std::min(cursor,play_frames);
}
std::array<float,2> Audio::sample(Voice& value,std::uint32_t frame) {
    const auto& format=value.format;std::array<float,2> result{};
    if(format.tag==0x69) {
        const auto block=frame/64;
        if(value.cached_block!=block) {
            const auto offset=std::size_t(block)*format.block_align;
            if(offset+format.block_align>value.source.size()) throw std::runtime_error("Xbox ADPCM block exceeds the bound buffer");
            try {decode_adpcm(format,value.source.data()+offset,value.block);}
            catch(const std::exception& error) {
                throw std::runtime_error(std::format("{}: voice {}, block {}, channels {}, address {}, header {}, source {} size {}, play {}+{}, loop {}+{}, cursor {}",
                    error.what(),&value-voices_.data()+1,block,format.channels,
                    hex32(value.source_address+static_cast<std::uint32_t>(offset)),hex_bytes(value.source.subspan(offset,format.channels*4)),
                    hex32(value.source_address),value.byte_count,value.play_start,value.play_frames,value.loop_start,value.loop_frames,
                    static_cast<std::uint32_t>(value.cursor)));
            }
            value.cached_block=block;
        }
        for(unsigned channel=0;channel<format.channels;++channel) result[channel]=value.block[(frame%64)*format.channels+channel];
    } else for(unsigned channel=0;channel<format.channels;++channel) {
        const auto offset=(std::size_t(frame)*format.channels+channel)*(format.bits/8);
        if(format.bits==8) result[channel]=(std::to_integer<int>(value.source[offset])-128)/128.0f;
        else {std::int16_t sample;std::memcpy(&sample,value.source.data()+offset,2);result[channel]=sample/32768.0f;}
    }
    return result;
}
void Audio::play(std::uint32_t id,bool loop) {
    StreamLock lock(stream_);auto& value=voice(id);
    if(value.bus==AudioBus::none && !value.play_frames) throw std::runtime_error("Cannot play an empty audio buffer");
    if(value.cursor>=value.play_frames) value.cursor=0;
    value.loop=loop;value.playing=true;value.cached_block=UINT32_MAX;
}
void Audio::stop(std::uint32_t id){StreamLock lock(stream_);voice(id).playing=false;}
std::uint32_t Audio::status(std::uint32_t id){StreamLock lock(stream_);const auto& value=voice(id);return value.playing?(value.loop?5:1):0;}
AudioPosition Audio::positions(std::uint32_t id) {
    StreamLock lock(stream_);const auto& value=voice(id);
    const auto frame=static_cast<std::uint32_t>(value.cursor);
    const auto samples=value.format.tag==0x69?64U:1U;
    const auto bytes=[&](std::uint32_t count){return count/samples*value.format.block_align;};
    const auto play=bytes(frame);
    if(!value.playing || !value.byte_count) return {play,play};
    // Original CMcpxBuffer::GetCurrentPosition (234D32): at least one codec
    // block ahead, with separate loop-region and allocation-capacity wrapping.
    const auto ahead=std::max(bytes(32),std::uint32_t(value.format.block_align));
    const auto loop_start=bytes(value.loop_start),loop_size=bytes(value.loop_frames);
    const auto write=value.loop && loop_size && play>=loop_start && play-loop_start<loop_size
        ?loop_start+(play+ahead)%loop_size:(play+ahead)%value.byte_count;
    return {play,write};
}
std::uint32_t Audio::position(std::uint32_t id) {return positions(id).play;}
void Audio::position(std::uint32_t id,std::uint32_t bytes) {
    StreamLock lock(stream_);auto& value=voice(id);
    const auto frame=frames(value.format,bytes);
    if(frame>value.play_frames) throw std::runtime_error("Audio position exceeds the play region");
    value.cursor=frame;value.cached_block=UINT32_MAX;
    value.environment_history={};
}
void Audio::region(std::uint32_t id,bool loop,std::uint32_t start,std::uint32_t bytes) {
    StreamLock lock(stream_);auto& value=voice(id);
    const auto first=frames(value.format,start),count=frames(value.format,bytes);
    const auto total=loop?value.play_frames:value.byte_count/value.format.block_align*(value.format.tag==0x69?64:1);
    if(first>total || count>total-first) throw std::runtime_error("Audio region exceeds its buffer");
    if(loop){value.loop_start=first;value.loop_frames=count?count:total-first;}
    else {value.play_start=first;value.play_frames=count?count:total-first;value.cursor=0;value.loop_start=0;value.loop_frames=value.play_frames;}
}
void Audio::volume(std::uint32_t id,std::int32_t db) {
    if(db>0 || db<-10000) throw std::runtime_error("Audio volume is outside -10000..0");
    StreamLock lock(stream_);voice(id).gain=db==-10000?0:std::pow(10.0f,db/2000.0f);
}
void Audio::headroom(std::uint32_t id,std::uint32_t db) {
    if(db>10000) throw std::runtime_error("Audio headroom is outside 0..10000");
    StreamLock lock(stream_);voice(id).headroom=std::pow(10.0f,-static_cast<float>(db)/2000.0f);
}
void Audio::mixbin_headroom(std::uint32_t bin,std::uint32_t shift) {
    if(bin>=32 || shift>7) throw std::runtime_error("Invalid audio mix-bin headroom");
    StreamLock lock(stream_);bin_headroom_[bin]=shift;
}
void Audio::mixbins(std::uint32_t id,std::span<const MixBin> bins,bool volumes_only) {
    if(bins.size()>(volumes_only?32U:8U)) throw std::runtime_error("Audio mix-bin count exceeds its native capacity");
    for(const auto& bin:bins) if(bin.bin>=32 || bin.volume>0 || bin.volume<-10000)
        throw std::runtime_error("Invalid audio mix-bin assignment or volume");
    StreamLock lock(stream_);auto& value=voice(id);
    if(!volumes_only) value.bin_count=static_cast<std::uint32_t>(bins.size());
    for(unsigned i=0;i<bins.size();++i) {
        const auto& bin=bins[i];
        if(!volumes_only) value.bins[i]=bin.bin;
        value.bin_gains[bin.bin]=bin.volume==-10000?0:std::pow(10.0f,bin.volume/2000.0f);
    }
}
void Audio::frequency(std::uint32_t id,std::uint32_t hz) {
    StreamLock lock(stream_);auto& value=voice(id);
    if(!hz) hz=value.format.rate;
    if(hz<100 || hz>200000) throw std::runtime_error("Audio frequency is outside 100..200000");
    value.step=hz/48000.0;
}
void Audio::route(std::uint32_t id,std::uint32_t destination) {
    StreamLock lock(stream_);auto& value=voice(id);
    for(auto cursor=destination;cursor;cursor=voice(cursor).route) if(cursor==id)
        throw std::runtime_error("Audio routing contains a cycle");
    value.route=destination;
}
void Audio::spatial(std::uint32_t id,const SpatialResult& adjustment) {
    const auto unit=[](float value){return !std::isfinite(value) || value<0 || value>1;};
    if(unit(adjustment.gain) || !std::isfinite(adjustment.pitch) || adjustment.pitch<=0 ||
        std::ranges::any_of(adjustment.pan,unit) || std::ranges::any_of(adjustment.front_back,unit) ||
        std::ranges::any_of(adjustment.environment_pole,[](float value){return !std::isfinite(value) || value<0 || value>=1;}) ||
        std::ranges::any_of(adjustment.environment_gain,[](float value){return !std::isfinite(value) || value<0 || value>std::sqrt(10.0f);}))
        throw std::runtime_error("Invalid native 3D audio adjustment");
    StreamLock lock(stream_);auto& value=voice(id);
    value.spatial_gain=adjustment.gain;value.spatial_pan=adjustment.pan;value.spatial_pitch=adjustment.pitch;
    value.front_back=adjustment.front_back;value.environment_gain=adjustment.environment_gain;value.environment_pole=adjustment.environment_pole;
}
void Audio::mix_voice(Voice& value,std::span<const std::array<float,2>> samples,std::span<float> output,unsigned frame_count) {
    if(!probe_) {send_voice(value,samples,output,frame_count);return;}
    const auto id=static_cast<unsigned>(&value-voices_.data());
    std::array<float,32*32> before;std::copy(output.begin(),output.end(),before.begin());
    for(const auto& sample:samples) for(unsigned channel=0;channel<value.format.channels;++channel) probe_->decoded[id].add(sample[channel]);
    send_voice(value,samples,output,frame_count);
    for(unsigned bin=0;bin<32;++bin) for(unsigned frame=0;frame<samples.size();++frame)
        probe_->sent[id].add((output[bin*frame_count+frame]-before[bin*frame_count+frame])/(1U<<bin_headroom_[bin]));
}
void Audio::send_voice(Voice& value,std::span<const std::array<float,2>> samples,std::span<float> output,unsigned frame_count) {
    float gain=value.gain*value.headroom*value.spatial_gain;auto pan=value.spatial_pan;const Voice* sink=&value;
    bool effect_bus=false;
    for(auto cursor=value.route;cursor;cursor=voices_[cursor-1].route) {
        const auto& destination=voices_[cursor-1];if(!destination.used){gain=0;break;}
        if(destination.input_bin!=UINT32_MAX) gain*=sink->bin_gains[destination.input_bin];
        if(destination.bus==AudioBus::effects || destination.bus==AudioBus::effects_manual) {
            // Send first; the FXIN voice applies its own controls to the DSP return.
            sink=&destination;effect_bus=true;break;
        }
        gain*=destination.gain*destination.headroom*destination.spatial_gain;
        for(unsigned i=0;i<2;++i) pan[i]*=destination.spatial_pan[i];sink=&destination;
    }
    const auto channels=value.format.channels;
    if(effect_bus) {
        for(unsigned frame=0;frame<samples.size();++frame)
            output[sink->input_bin*frame_count+frame]+=(samples[frame][0]+(channels==2?samples[frame][1]:0))*gain/channels;
        return;
    }
    std::array<float,8> gains{};
    for(unsigned i=0;i<sink->bin_count;++i) {
        const auto bin=sink->bins[i];
        gains[i]=gain*sink->bin_gains[bin]*((bin==0 || bin==4 || bin==6 || bin==8)?pan[0]:(bin==1 || bin==5 || bin==7 || bin==9)?pan[1]:1);
        if(bin>=6 && bin<=9) gains[i]*=value.front_back[bin>=8]*value.environment_gain[0];
        else if(bin==2) gains[i]*=value.environment_gain[0];
        else if(bin==10) {
            gains[i]*=value.environment_gain[1];
            if(gains[i] && (!effects_ || !effects_->reverb)) throw std::runtime_error("Environmental audio requires an assigned reverb effect");
        }
    }
    for(unsigned frame=0;frame<samples.size();++frame) {
        std::array<std::array<float,2>,2> filtered{};
        for(unsigned path=0;path<2;++path) for(unsigned channel=0;channel<channels;++channel) {
            const auto pole=value.environment_pole[path];
            auto& previous=value.environment_history[path][channel];
            previous=pole?(1-pole)*samples[frame][channel]+pole*previous:samples[frame][channel];filtered[path][channel]=previous;
        }
        for(unsigned bin=0;bin<sink->bin_count;++bin) {
            const auto target=sink->bins[bin];
            const auto& signal=target==10?filtered[1]:(target==2 || (target>=6 && target<=9))?filtered[0]:samples[frame];
            output[target*frame_count+frame]+=signal[bin%channels]*gains[bin];
        }
    }
}
void Audio::mix_bins(std::span<float> output,unsigned frame_count) {
    std::fill(output.begin(),output.end(),0.0f);
    std::array<bool,256> active{};
    for(const auto& value:voices_) if(value.used && value.playing &&
        (value.bus==AudioBus::none || value.bus==AudioBus::effects_manual))
        for(auto cursor=value.route;cursor;cursor=voices_[cursor-1].route) active[cursor-1]=true;
    for(auto& value:voices_) {
        if(!value.used || !value.playing || value.bus!=AudioBus::none) continue;
        const auto channels=value.format.channels;
        std::array<std::array<float,2>,32> samples;unsigned count=0;
        for(;count<frame_count;++count) {
            if(value.cursor>=value.play_frames) {
                if(!value.loop || !value.loop_frames){value.playing=false;break;}
                value.cursor=value.loop_start+std::fmod(value.cursor-value.loop_start,static_cast<double>(value.loop_frames));
                value.cached_block=UINT32_MAX;
            }
            if(value.loop && value.cursor>=value.loop_start+value.loop_frames) {
                value.cursor=value.loop_start+std::fmod(value.cursor-value.loop_start,static_cast<double>(value.loop_frames));
                value.cached_block=UINT32_MAX;
            }
            const auto first=static_cast<std::uint32_t>(value.cursor);
            auto second=first+1;
            if(value.loop && second>=value.loop_start+value.loop_frames) second=value.loop_start;
            else second=std::min(second,value.play_frames-1);
            const auto blend=static_cast<float>(value.cursor-first);
            const auto a=sample(value,value.play_start+first),b=sample(value,value.play_start+second);
            for(unsigned channel=0;channel<channels;++channel) samples[count][channel]=a[channel]+(b[channel]-a[channel])*blend;
            value.cursor+=value.step*value.spatial_pitch;
        }
        mix_voice(value,std::span(samples).first(count),output,frame_count);
        if(!value.loop && value.cursor>=value.play_frames){value.cursor=value.play_frames;value.playing=false;}
    }
    for(unsigned id=0;id<voices_.size();++id) {
        auto& value=voices_[id];
        if(!value.used || (value.bus!=AudioBus::effects && value.bus!=AudioBus::effects_manual)) continue;
        const bool returning=value.playing || (value.bus==AudioBus::effects && active[id]);
        if(value.bus==AudioBus::effects) value.playing=active[id];
        if(!returning) continue;
        if(!effects_) throw std::runtime_error("Post-effects audio requires a compiled audio image");
        // The SDK's FXIN source is a 32-sample loop over its DSP output bin.
        // Read the preceding block before publishing this block's DSP inputs.
        std::array<std::array<float,2>,32> samples;
        for(unsigned i=0;i<frame_count;++i)
            samples[i][0]=Dsp::signed24(effects_->state.load(0xC00+value.input_bin*32+i))/8388608.0f;
        mix_voice(value,std::span(samples).first(frame_count),output,frame_count);
    }
    for(unsigned bin=0;bin<32;++bin) {
        const auto gain=1.0f/(1U<<bin_headroom_[bin]);
        for(auto& sample:output.subspan(bin*frame_count,frame_count)) sample=std::clamp(sample*gain,-1.0f,1.0f);
        if(probe_) for(float sample:output.subspan(bin*frame_count,frame_count)) probe_->bins[bin].add(sample);
    }
}
void Audio::mix_sources(std::span<float> output) {
    std::array<float,32*32> bins{};
    for(std::size_t start=0;start<output.size()/2;start+=32) {
        const auto count=static_cast<unsigned>(std::min<std::size_t>(32,output.size()/2-start));
        mix_bins(std::span(bins).first(count*32),count);
        for(unsigned i=0;i<count;++i) {
            const auto center=bins[2*count+i]*0.70710678f+bins[3*count+i]*0.5f;
            output[(start+i)*2]=std::clamp(bins[i]+center+bins[4*count+i]*0.70710678f,-1.0f,1.0f);
            output[(start+i)*2+1]=std::clamp(bins[count+i]+center+bins[5*count+i]*0.70710678f,-1.0f,1.0f);
        }
        if(std::ranges::any_of(std::span(bins).subspan(count*6,count*26),[](float value){return value!=0;}))
            throw std::runtime_error("Effect send requires a compiled audio image");
    }
}
void Audio::effects(const EffectsImage& image,std::uint32_t reverb_index) {
    if(!compiled_effects_match(image)) throw std::runtime_error("Audio image differs from the statically compiled effects");
    auto replacement=std::make_unique<Effects>(image,reverb_index);
    // Validate initialization and its real delay transfers before publication.
    for(unsigned i=0;i<replacement->count;++i) run_effect(replacement->state,i);
    StreamLock lock(stream_);effects_.swap(replacement);
}
void Audio::effect_data(std::uint32_t offset,std::span<std::byte> destination) {
    StreamLock lock(stream_);
    if(!effects_ || offset>effects_->state.x.size()*4 || destination.size()>effects_->state.x.size()*4-offset)
        throw std::runtime_error("Invalid native DSP effect read");
    while(!destination.empty()) {
        const auto size=std::min<std::size_t>(4-offset%4,destination.size());
        const auto value=effects_->state.load(offset/4);
        std::memcpy(destination.data(),reinterpret_cast<const std::byte*>(&value)+offset%4,size);
        destination=destination.subspan(size);offset+=static_cast<std::uint32_t>(size);
    }
}
void Audio::effect_data(std::uint32_t offset,std::span<const std::byte> source,bool deferred) {
    StreamLock lock(stream_);
    if(!effects_ || offset>effects_->state.x.size()*4 || source.size()>effects_->state.x.size()*4-offset)
        throw std::runtime_error("Invalid native DSP effect write");
    auto& effect=*effects_;
    if(deferred) {
        if(source.empty()) return;
        std::copy(source.begin(),source.end(),effect.pending.begin()+offset);
        for(std::size_t i=0;i<source.size();++i) effect.dirty.set(offset+i);
        effect.dirty_first=std::min(effect.dirty_first,offset&~3U);
        effect.dirty_end=std::max(effect.dirty_end,(offset+static_cast<std::uint32_t>(source.size())+3)&~3U);
        return;
    }
    while(!source.empty()) {
        const auto size=std::min<std::size_t>(4-offset%4,source.size());
        auto value=effect.state.load(offset/4);
        std::memcpy(reinterpret_cast<std::byte*>(&value)+offset%4,source.data(),size);
        effect.state.store(offset/4,value);
        for(std::size_t i=0;i<size;++i) effect.dirty.reset(offset+i);
        source=source.subspan(size);offset+=static_cast<std::uint32_t>(size);
    }
}
void Audio::commit_effects() {
    StreamLock lock(stream_);if(!effects_) return;
    auto& effect=*effects_;
    for(auto offset=effect.dirty_first;offset<effect.dirty_end;offset+=4) {
        auto value=effect.state.load(offset/4);bool changed=false;
        for(unsigned i=0;i<4;++i) if(effect.dirty[offset+i]) {
            reinterpret_cast<std::byte*>(&value)[i]=effect.pending[offset+i];changed=true;
        }
        if(changed) effect.state.store(offset/4,value);
    }
    effect.dirty.reset();effect.dirty_first=0x6000;effect.dirty_end=0;
}
std::uint32_t Audio::effect_x(std::uint32_t offset,unsigned size) {
    StreamLock lock(stream_);
    if(!effects_ || size>4 || !size || offset>=effects_->state.x.size()*4 || offset/4!=(offset+size-1)/4)
        throw std::runtime_error("Invalid native DSP X read");
    return (effects_->state.load(offset/4)>>(offset%4*8))&(size==4?0xFFFFFFFFU:(1U<<(size*8))-1);
}
void Audio::effect_x(std::uint32_t offset,unsigned size,std::uint32_t value) {
    StreamLock lock(stream_);
    if(!effects_ || size>4 || !size || offset>=effects_->state.x.size()*4 || offset/4!=(offset+size-1)/4)
        throw std::runtime_error("Invalid native DSP X write");
    const auto mask=size==4?0xFFFFFFFFU:(1U<<(size*8))-1,shift=offset%4*8;
    auto& state=effects_->state;state.store(offset/4,(state.load(offset/4)&~(mask<<shift))|((value&mask)<<shift));
}
std::uint32_t Audio::effect_scratch(std::uint32_t offset,unsigned size) {
    StreamLock lock(stream_);
    if(!effects_ || !size || size>4 || offset>effects_->state.scratch.size() || size>effects_->state.scratch.size()-offset)
        throw std::runtime_error("Invalid native DSP scratch read");
    std::uint32_t value=0;std::memcpy(&value,effects_->state.scratch.data()+offset,size);return value;
}
void Audio::effect_scratch(std::uint32_t offset,unsigned size,std::uint32_t value) {
    StreamLock lock(stream_);
    if(!effects_ || !size || size>4 || offset>effects_->state.scratch.size() || size>effects_->state.scratch.size()-offset)
        throw std::runtime_error("Invalid native DSP scratch write");
    std::memcpy(effects_->state.scratch.data()+offset,&value,size);
}
std::uint32_t Audio::effect_program(std::uint32_t offset,unsigned size) {
    StreamLock lock(stream_);
    if(!effects_ || !size || size>4 || offset>effects_->program.size() || size>effects_->program.size()-offset)
        throw std::runtime_error("Invalid compiled DSP program read");
    std::uint32_t value=0;std::memcpy(&value,effects_->program.data()+offset,size);return value;
}
std::string Audio::failure(){StreamLock lock(stream_);return failed_?failure_.data():std::string{};}
void Audio::mix_unlocked(std::span<float> output) {
    const auto started=performance_enabled_.load(std::memory_order_relaxed) && !output.empty()?performance_clock():0;
    const auto measured=[&] {
        if(!started) return;const auto elapsed=performance_clock()-started;
        mix_batches_.fetch_add(1,std::memory_order_relaxed);mix_ns_.fetch_add(elapsed,std::memory_order_relaxed);
        if(elapsed>(output.size()/2)*1000000000ULL/48000) mix_overruns_.fetch_add(1,std::memory_order_relaxed);
    };
    if(!effects_){mix_sources(output);frames_processed_.fetch_add(static_cast<std::uint32_t>(output.size()/2),std::memory_order_relaxed);measured();return;}
    auto& effects=*effects_;
    for(std::size_t frame=0;frame<output.size()/2;++frame) {
        if(!effects.available) {
            std::array<float,32*32> bins{};mix_bins(bins,32);
            auto& state=effects.state;
            for(unsigned bin=0;bin<32;++bin) for(unsigned i=0;i<32;++i) {
                const auto sample=static_cast<std::int32_t>(std::clamp(bins[bin*32+i]*8388608.0f,-8388608.0f,8388607.0f));
                state.store(0x1400+bin*32+i,static_cast<std::uint32_t>(sample));
            }
            for(unsigned i=0;i<effects.count;++i) run_effect(state,i);
            if(probe_) for(unsigned bin=0;bin<32;++bin) for(unsigned i=0;i<32;++i)
                probe_->dsp[bin].add(Dsp::signed24(state.load(0xC00+bin*32+i))/8388608.0f);
            frames_processed_.fetch_add(32,std::memory_order_relaxed);
            for(unsigned i=0;i<32;++i) {
                const auto sample=[&](unsigned bin){return Dsp::signed24(state.load(0xC00+bin*32+i))/8388608.0f;};
                const auto center=sample(2)*0.70710678f+sample(3)*0.5f;
                effects.output[i*2]=std::clamp(sample(0)+center+sample(4)*0.70710678f,-1.0f,1.0f);
                effects.output[i*2+1]=std::clamp(sample(1)+center+sample(5)*0.70710678f,-1.0f,1.0f);
            }
            effects.cursor=0;effects.available=32;
        }
        output[frame*2]=effects.output[effects.cursor*2];output[frame*2+1]=effects.output[effects.cursor*2+1];
        ++effects.cursor;--effects.available;
    }
    measured();
}
void Audio::mix(std::span<float> output) {
    if(output.size()%2) throw std::runtime_error("Audio mixer requires stereo frames");
    StreamLock lock(stream_);mix_unlocked(output);
}
std::string check_audio(const std::filesystem::path& effects_path,const std::filesystem::path& output) {
    unsigned checks=0;
    const auto require=[&](bool condition){++checks;if(!condition) throw std::runtime_error("Offline audio check failed");};
    const AudioFormat pcm{1,1,2,16,0,48000},stereo{1,2,4,16,0,48000},adpcm{0x69,1,36,4,64,48000};
    const std::array<std::int16_t,4> samples{8192,-8192,16384,-16384};
    const auto bytes=std::as_bytes(std::span(samples));
    const auto decoded=decode_audio(pcm,bytes);require(decoded.size()==4 && decoded[0]==0.25f && decoded[1]==-0.25f);
    require(decode_audio(stereo,bytes)==decoded);
    const std::array<std::byte,3> unsigned_pcm{std::byte{0},std::byte{128},std::byte{255}};
    const auto unsigned_samples=decode_audio({1,1,1,8,0,48000},unsigned_pcm);
    require(unsigned_samples[0]==-1 && unsigned_samples[1]==0 && unsigned_samples[2]==127/128.0f);
    std::array<std::byte,72> block{};const std::int16_t left=1024,right=-1024;
    std::memcpy(block.data(),&left,2);std::memcpy(block.data()+4,&right,2);
    const auto mono=decode_audio(adpcm,std::span(block).first(36));require(mono.size()==64 && mono.front()==1/32.0f);
    const auto two=decode_audio({0x69,2,72,4,64,48000},block);
    require(two.size()==128 && two[0]==1/32.0f && two[1]==-1/32.0f && two[126]==1/32.0f && two[127]==-1/32.0f);
    block[2]=std::byte{89};bool rejected=false;try{decode_audio(adpcm,std::span(block).first(36));}catch(const std::exception&){rejected=true;}require(rejected);
    rejected=false;try{decode_audio(pcm,bytes.first(3));}catch(const std::exception&){rejected=true;}require(rejected);
    Audio audio(false);const AudioFormat bus_format{1,1,4,24,0,48000};
    const auto source=audio.create(pcm),bus=audio.create(bus_format,AudioBus::submix);
    rejected=false;try{audio.bind(bus,bytes);}catch(const std::exception&){rejected=true;}require(rejected);
    const auto changing=audio.create(pcm);audio.data(changing,bytes);audio.format(changing,stereo);
    audio.play(changing,false);std::array<float,4> reformatted{};audio.mix(reformatted);
    require(reformatted==std::array<float,4>{0.25f,-0.25f,0.5f,-0.5f} && audio.position(changing)==8 && !audio.status(changing));
    audio.format(changing,pcm);block[2]=std::byte{0};audio.data(changing,block);
    const AudioFormat stereo_adpcm{0x69,2,72,4,64,48000};audio.format(changing,stereo_adpcm);audio.format(changing,stereo_adpcm);
    audio.play(changing,false);std::array<float,128> recoded{};audio.mix(recoded);
    require(recoded[0]==1/32.0f && recoded[1]==-1/32.0f && recoded[126]==1/32.0f && recoded[127]==-1/32.0f);
    require(audio.position(changing)==72 && !audio.status(changing));audio.release(changing);
    std::array<std::int16_t,2> bound{8192,-8192};const auto dynamic=audio.create(pcm);
    audio.bind(dynamic,std::as_bytes(std::span(bound)));bound[0]=16384;audio.play(dynamic,true);
    std::array<float,4> bound_mix{};audio.mix(bound_mix);
    require(bound_mix==std::array<float,4>{0.5f,0.5f,-0.25f,-0.25f});audio.stop(dynamic);
    std::array<std::byte,74> padded{};std::copy(block.begin(),block.end(),padded.begin());
    audio.bind(dynamic,padded);audio.format(dynamic,stereo_adpcm);audio.play(dynamic,false);audio.mix(recoded);
    require(recoded[0]==1/32.0f && recoded[1]==-1/32.0f && audio.position(dynamic)==72 && !audio.status(dynamic));audio.release(dynamic);
    const auto cursor=audio.create(pcm);const std::array<std::byte,140> silence{};audio.data(cursor,silence);
    audio.region(cursor,false,10,100);audio.play(cursor,false);
    auto positions=audio.positions(cursor);require(positions.play==0 && positions.write==64);
    audio.position(cursor,80);positions=audio.positions(cursor);require(positions.play==80 && positions.write==4);
    audio.region(cursor,true,6,20);audio.position(cursor,10);audio.play(cursor,true);
    positions=audio.positions(cursor);require(positions.play==10 && positions.write==20);
    audio.stop(cursor);positions=audio.positions(cursor);require(positions.play==10 && positions.write==10);audio.release(cursor);
    const auto compressed_cursor=audio.create(stereo_adpcm);const std::array<std::byte,216> compressed_silence{};
    audio.data(compressed_cursor,compressed_silence);audio.play(compressed_cursor,false);
    positions=audio.positions(compressed_cursor);require(positions.play==0 && positions.write==72);audio.release(compressed_cursor);
    audio.data(source,bytes);audio.play(source,false);std::array<float,8> mixed{};audio.mix(mixed);
    require(mixed[0]==0.25f && mixed[1]==0.25f && mixed[6]==-0.5f && audio.status(source)==0 && audio.position(source)==8);
    audio.position(source,0);audio.volume(source,-2000);audio.volume(bus,-2000);audio.route(source,bus);audio.play(source,true);audio.mix(mixed);
    require(std::abs(mixed[0]-0.0025f)<0.000001f && audio.status(source)==5);
    audio.stop(source);audio.mix(mixed);require(std::ranges::all_of(mixed,[](float value){return value==0;}));
    audio.route(source,0);audio.volume(source,0);audio.region(source,true,2,4);audio.position(source,0);audio.play(source,true);
    std::array<float,12> looped{};audio.mix(looped);require(looped[0]==0.25f && looped[2]==-0.25f && looped[4]==0.5f && looped[6]==-0.25f && looped[10]==-0.25f);
    audio.stop(source);audio.region(source,false,0,8);audio.position(source,0);audio.frequency(source,24000);audio.play(source,false);audio.mix(mixed);
    require(mixed[0]==0.25f && mixed[2]==0 && audio.position(source)==4);
    rejected=false;try{audio.route(bus,source);audio.route(source,bus);}catch(const std::exception&){rejected=true;}require(rejected);
    audio.route(bus,0);audio.route(source,0);audio.stop(source);audio.frequency(source,48000);audio.position(source,0);
    const std::array<MixBin,1> right_only{{{1,-2000}}};audio.mixbins(source,right_only);audio.headroom(source,2000);
    audio.play(source,true);audio.mix(mixed);require(mixed[0]==0 && std::abs(mixed[1]-0.0025f)<0.000001f);
    audio.stop(source);audio.position(source,0);audio.headroom(source,0);audio.mixbin_headroom(1,1);audio.play(source,true);audio.mix(mixed);
    require(mixed[0]==0 && std::abs(mixed[1]-0.0125f)<0.000001f);
    const std::array<MixBin,1> mute_right{{{1,-10000}}};audio.mixbins(source,mute_right,true);audio.mix(mixed);
    require(std::ranges::all_of(mixed,[](float value){return value==0;}));
    rejected=false;try{audio.mixbin_headroom(32,0);}catch(const std::exception&){rejected=true;}require(rejected);
    rejected=false;try{const std::array<MixBin,1> invalid{{{1,1}}};audio.mixbins(source,invalid);}catch(const std::exception&){rejected=true;}require(rejected);
    audio.mixbins(source,{});audio.mix(mixed);require(std::ranges::all_of(mixed,[](float value){return value==0;}));
    audio.release(source);rejected=false;try{audio.status(source);}catch(const std::exception&){rejected=true;}require(rejected);
    Audio distance(false);const auto emitter=distance.create(pcm);distance.data(emitter,bytes);distance.play(emitter,true);
    distance.spatial(emitter,{.gain=0.5f});distance.mix(mixed);require(mixed[0]==0.125f && mixed[1]==0.125f);
    distance.position(emitter,0);distance.spatial(emitter,{.pan={0,1}});distance.mix(mixed);require(mixed[0]==0 && mixed[1]==0.25f);
    distance.position(emitter,0);distance.spatial(emitter,{.pitch=0.5});distance.mix(mixed);require(mixed[0]==0.25f && mixed[2]==0);
    rejected=false;try{distance.spatial(emitter,{.gain=-1});}catch(const std::exception&){rejected=true;}require(rejected);
    distance.position(emitter,0);distance.spatial(emitter,{.environment_gain={0.5f,0},.environment_pole={0.5f,0}});
    const std::array<MixBin,1> center_only{{{2,0}}};distance.mixbins(emitter,center_only);distance.mix(mixed);
    require(std::abs(mixed[0]-0.25f*0.5f*0.5f*0.70710678f)<0.000001f && mixed[0]==mixed[1]);
    distance.position(emitter,0);distance.mix(mixed);require(std::abs(mixed[0]-0.04419417f)<0.000001f);
    const auto snapshot=distance.snapshot();auto replay=std::make_unique<Audio>(false);replay->restore(*snapshot);
    std::array<float,64> reference{},captured{};distance.mix(reference);replay->mix(captured);
    require(reference==captured && distance.sample_clock()==replay->sample_clock());
    auto shared=std::make_unique<Audio>(false);std::array<std::int16_t,4> bank={8192,16384,-8192,-16384};
    const auto first=shared->create(pcm),second=shared->create(pcm);
    shared->bind(first,std::as_bytes(std::span(bank)));shared->bind(second,std::as_bytes(std::span(bank)).subspan(2,4));
    const auto merged=shared->snapshot();require(merged->data.size()==8 && merged->offsets[second-1]==merged->offsets[first-1]+2);
    auto restored=std::make_unique<Audio>(false);restored->restore(*merged);restored->play(first,true);restored->play(second,true);
    Audio::Probe probe;restored->probe_=&probe;restored->mix(captured);
    require(probe.decoded[first-1].peak==0.5f && probe.decoded[second-1].peak==0.5f && probe.sent[first-1].peak==0.5f);
    if(!output.empty()) {
        if(std::filesystem::exists(output)) throw std::runtime_error("Audio checks require a fresh output directory");
        File file(effects_path);if(file.size()>65536) throw std::runtime_error("Effects image exceeds 64 KiB");
        const auto image=decode_effects(file.read(0,static_cast<std::size_t>(file.size())));
        auto fixture=std::make_unique<Audio>(false);auto& scene=*fixture;scene.effects(image,2);
        // Known original distortion parameters also used by the effects checks.
        const std::array<std::uint32_t,11> parameters{0xDC0,0xDC0,0x1BC5B,0x3FFFFF,0x19A027,0x7FFFFF,0x1A7250,0,0x1FFFFF,0,0x7126E8};
        scene.effect_data((0xAE+5)*4,std::as_bytes(std::span(parameters)),false);
        const auto source=scene.create(pcm),bus=scene.create(bus_format,AudioBus::effects,14);
        scene.data(source,bytes);scene.mixbins(source,std::array<MixBin,1>{{{14,0}}});
        scene.mixbins(bus,std::array<MixBin,5>{{{6,0},{8,0},{7,0},{9,0},{10,0}}});
        scene.route(source,bus);scene.play(source,true);
        std::array<float,82> warmup{};scene.mix(warmup);scene.capture(output/"capture");
        // Check serialized DSP/filter/partial-block state against uninterrupted playback.
        scene.mix(reference);const auto report=Audio::inspect_capture(output/"capture/audio.bin",effects_path,output/"replay");
        require(report.find("\"passed\":true")!=std::string::npos);
        File wav(output/"replay/scene.wav");const auto replayed=wav.read(44,reference.size()*2);
        bool equal=true;for(unsigned i=0;i<reference.size();++i)
            equal&=std::bit_cast<std::int16_t>(u16(replayed,i*2))==static_cast<std::int16_t>(std::clamp(reference[i]*32768.f,-32768.f,32767.f));
        require(equal);
    }
    audio.performance(true);audio.mix(mixed);
    const auto recorded=audio.performance();require(recorded.batches==1 && recorded.mix_ns>0);
    audio.performance(false);audio.mix(mixed);require(audio.performance().batches==1);
    return "{\"format\":\"b2-audio-check-v1\",\"passed\":true,\"game_booted\":false,\"audio_device_opened\":false,\"cases\":"+std::to_string(checks)+"}";
}
}
