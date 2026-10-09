#include "dsp.h"
#include "audio.h"
#include "spatial.h"
#include <algorithm>
#include <cstring>
#include <chrono>
#include <format>
#include <stdexcept>

namespace b2 {
Dsp::Dsp(const EffectsImage& image) {
    if(image.data.size()>4096-0x80) throw std::runtime_error("DSP data exceeds X memory");
    std::copy(image.data.begin(),image.data.end(),x.begin()+0x80);
    for(unsigned i=32;i<40;++i) registers[i]=0xFFFFFF;
    std::uint64_t size=0xC000;
    for(const auto& effect:image.effects) {
        size+=effect.descriptor[7];
        if(size>16*1024*1024) throw std::runtime_error("DSP scratch exceeds its bounded capacity");
    }
    scratch.resize(static_cast<std::size_t>(size));
}
std::uint32_t Dsp::read(unsigned reg) {
    if(reg>=registers.size()) throw std::runtime_error("Invalid DSP register");
    if(reg>=8 && reg<=15) {
        const unsigned accumulator=reg&1U;
        const auto value=accumulators[accumulator];
        if(reg<10) return static_cast<std::uint32_t>(value)&0xFFFFFF;
        if(reg<12) return static_cast<std::uint32_t>(static_cast<std::int8_t>(value>>48))&0xFFFFFF;
        if(reg<14) return static_cast<std::uint32_t>(value>>24)&0xFFFFFF;
        const auto sample=value>>24;
        return static_cast<std::uint32_t>(std::clamp<std::int64_t>(sample,-0x800000,0x7FFFFF))&0xFFFFFF;
    }
    return registers[reg]&0xFFFFFF;
}
void Dsp::write(unsigned reg,std::uint32_t value) {
    value&=0xFFFFFF;
    if(reg>=registers.size()) throw std::runtime_error("Invalid DSP register");
    if(reg>=8 && reg<=15) {
        auto& accumulator=accumulators[reg&1U];
        if(reg>=14) accumulator=static_cast<std::int64_t>(signed24(value))*0x1000000;
        else {
            const unsigned shift=reg<10?0:reg<12?48:24;
            const std::uint64_t mask=(reg<12 && reg>=10?0xFFULL:0xFFFFFFULL)<<shift;
            accumulator=signed56((static_cast<std::uint64_t>(accumulator)&~mask)|((static_cast<std::uint64_t>(value)<<shift)&mask));
        }
        return;
    }
    registers[reg]=value;
}
std::int64_t Dsp::operand(unsigned reg) {
    if(reg==14 || reg==15) return accumulators[reg-14];
    return static_cast<std::int64_t>(signed24(read(reg)))*0x1000000;
}
std::uint32_t Dsp::load(std::uint32_t address) {
    address&=0xFFFFFF;
    // Both Xbox mix-buffer windows name the same 32 bins of 32 samples.
    if(address>=0x1400 && address<0x1800) address-=0x800;
    if(address<x.size()) return x[address];
    if(address==0xFFFFD4) return dma_next;
    if(address==0xFFFFD6) return dma_control;
    if(address==0xFFFFC5) return interrupts;
    throw std::runtime_error(std::format("Unbound DSP X read {:06X}",address));
}
void Dsp::store(std::uint32_t address,std::uint32_t value) {
    address&=0xFFFFFF;value&=0xFFFFFF;
    if(address>=0x1400 && address<0x1800) address-=0x800;
    if(address<x.size()){x[address]=value;return;}
    if(address==0xFFFFD4){dma_next=value;return;}
    if(address==0xFFFFC5){interrupts&=~value;return;}
    if(address==0xFFFFD6) {
        switch(value&7U) {
        case 0:return;
        case 1:dma_control=(dma_control|16)&~32U;if(!(dma_control&8)) transfer();return;
        case 2:dma_control=(dma_control|32)&~16U;return;
        case 3:dma_control|=8;return;
        case 4:dma_control&=~8U;if(dma_control&16) transfer();return;
        default:throw std::runtime_error("Unbound DSP DMA action");
        }
    }
    throw std::runtime_error(std::format("Unbound DSP X write {:06X}",address));
}
std::uint32_t Dsp::ea(unsigned index,unsigned mode) {
    auto& pointer=registers[16+index];
    if(registers[32+index]!=0xFFFFFF) throw std::runtime_error("Unbound DSP modulo address mode");
    const auto old=pointer;
    const auto offset=registers[24+index];
    switch(mode) {
    case 0:pointer=(pointer-offset)&0xFFFFFF;return old;
    case 1:pointer=(pointer+offset)&0xFFFFFF;return old;
    case 2:pointer=(pointer-1)&0xFFFFFF;return old;
    case 3:pointer=(pointer+1)&0xFFFFFF;return old;
    case 4:return old;
    case 5:return (pointer+offset)&0xFFFFFF;
    case 7:pointer=(pointer-1)&0xFFFFFF;return pointer;
    default:throw std::runtime_error("Unbound DSP address mode");
    }
}
void Dsp::flags(std::int64_t result,bool over) {negative=result<0;zero=result==0;overflow=over;}
void Dsp::arithmetic(unsigned destination,std::int64_t result,bool over,bool rounded) {
    if(registers[57]&(1U<<20)) {
        const auto high=rounded?0x7FFFFF000000LL:0x7FFFFFFFFFFFLL;
        const auto limited=std::clamp<std::int64_t>(result,-0x800000000000LL,high);
        over|=limited!=result;result=limited;
    }
    accumulators[destination]=result;flags(result,over);
}
void Dsp::add(unsigned destination,std::int64_t source,bool subtract,bool save) {
    const auto original=accumulators[destination];
    const auto raw=subtract?original-source:original+source;
    const auto result=signed56(static_cast<std::uint64_t>(raw));
    const bool over=subtract?((original<0)!=(source<0) && (original<0)!=(result<0)):
        ((original<0)==(source<0) && (original<0)!=(result<0));
    if(save) arithmetic(destination,result,over);else flags(result,over);
}
void Dsp::clear(unsigned destination){accumulators[destination]=0;flags(0);}
void Dsp::negate(unsigned destination) {const auto before=accumulators[destination];
    const auto result=signed56(static_cast<std::uint64_t>(-before));arithmetic(destination,result,before==-(1LL<<55));}
void Dsp::shift(unsigned source,unsigned destination,unsigned count,bool right) {
    if(count>=56) throw std::runtime_error("DSP shift count exceeds the verified range");
    const auto original=accumulators[source];
    const auto result=right?original>>count:signed56(static_cast<std::uint64_t>(original)<<count);
    const bool over=!right && (result>>count)!=original;
    arithmetic(destination,result,over);
}
void Dsp::multiply(unsigned destination,std::uint32_t first,std::uint32_t second,bool accumulate,bool round,bool negate_product) {
    auto product=static_cast<std::int64_t>(signed24(first))*signed24(second);
    product*=2; // Signed fractional 24-bit operands, 48-bit product.
    if(negate_product) product=-product;
    const auto raw=product+(accumulate?accumulators[destination]:0);
    auto result=signed56(static_cast<std::uint64_t>(raw));
    if(round) {
        const auto low=static_cast<std::uint64_t>(result)&0xFFFFFF;
        const auto high=result>>24;
        result=signed56(static_cast<std::uint64_t>((high+((low>0x800000 || (low==0x800000 && (high&1)))?1:0))*0x1000000));
    }
    arithmetic(destination,result,raw!=signed56(static_cast<std::uint64_t>(raw)),round);
}
void Dsp::logical_and(unsigned destination,std::uint32_t value) {
    const auto old=static_cast<std::uint64_t>(accumulators[destination]);
    const auto word=static_cast<std::uint32_t>(old>>24)&value&0xFFFFFF;
    accumulators[destination]=signed56((old&~(0xFFFFFFULL<<24))|(static_cast<std::uint64_t>(word)<<24));
    negative=(word&0x800000)!=0;zero=word==0;overflow=false;
}
void Dsp::transfer() {
    unsigned nodes=0;
    while(!(dma_next&0x4000)) {
        if(++nodes>1024) throw std::runtime_error("DSP DMA command chain contains a cycle");
        const auto node=dma_next&0x3FFF;
        if(node>=x.size()-6) throw std::runtime_error("DSP DMA command is outside X memory");
        const auto next=x[node],control=x[node+1],count=x[node+2],source=x[node+3];
        auto offset=x[node+4];const auto base=x[node+5],size=x[node+6]+1;
        const auto format=(control>>10)&7U,buffer=(control>>5)&15U;
        const bool output=(control&2)!=0,circular=buffer==14;
        if((control&0x200D) || (buffer!=14 && buffer!=15) || (format!=1 && format!=2 && format!=6))
            throw std::runtime_error(std::format("Unbound DSP DMA format/control {:06X}",control));
        const unsigned item_bytes=format==1?2:4;
        if(source>=x.size() || count>x.size()-source || !size || base>scratch.size() || size>scratch.size()-base)
            throw std::runtime_error("DSP DMA range exceeds native sample or scratch storage");
        if(circular && offset>=size) offset=0;
        for(unsigned i=0;i<count;++i) {
            std::uint32_t value=output?load(source+i):0;
            if(output && item_bytes==2) value>>=8;
            for(unsigned byte=0;byte<item_bytes;++byte) {
                if(offset>=size) {if(circular) offset=0;else throw std::runtime_error("DSP DMA exceeds scratch region");}
                auto& destination=scratch[base+offset++];
                if(output) destination=static_cast<std::byte>(value>>(byte*8));
                else value|=static_cast<std::uint32_t>(std::to_integer<unsigned>(destination))<<(byte*8);
            }
            if(!output) store(source+i,item_bytes==2?value<<8:value);
        }
        if(circular && offset==size) offset=0;
        if(control&16) x[node+4]=offset;
        dma_next=next;transferred_words+=count;
        if(control&0x200) interrupts|=0x80;
    }
}
std::string check_effects(const std::filesystem::path& source) {
    File file(source);if(file.size()>65536) throw std::runtime_error("Effects input exceeds 64 KiB");
    const auto image=decode_effects(file.read(0,static_cast<std::size_t>(file.size())));
    if(!compiled_effects_match(image)) throw std::runtime_error("Effects input differs from the compiled programs");
    unsigned checks=0;std::vector<std::string> failures;
    const auto require=[&](bool passed,std::string_view detail){++checks;if(!passed) failures.emplace_back(detail);};
    Dsp state(image);
    state.write(14,0xFFFFFF);require(state.accumulators[0]==-0x1000000,"signed accumulator move");
    state.write(10,0x7F);require(state.read(10)==0x7F,"extension write");
    state.write(10,0xFF);require(state.read(10)==0xFFFFFF,"extension sign extension");
    state.write(14,0x7FFFFF);state.add(0,0x1000000);require(state.read(14)==0x7FFFFF,"move saturation");
    state.write(57,1U<<20);state.write(14,0x7FFFFF);state.add(0,0x1000000);
    require(state.accumulators[0]==0x7FFFFFFFFFFFLL && state.overflow,"saturation mode precision");
    state.write(57,0);state.multiply(0,0x400000,0x400000,false,false);
    require(state.read(14)==0x200000,"fractional multiplication");
    state.accumulators[0]=0x1800000;state.multiply(0,0,0,true,true);
    require(state.accumulators[0]==0x2000000,"convergent rounding up");
    state.accumulators[0]=0x2800000;state.multiply(0,0,0,true,true);
    require(state.accumulators[0]==0x2000000,"convergent rounding even");
    state.write(16,400);state.write(24,2);require(state.ea(0,1)==400 && state.read(16)==402,"postincrement address");
    state.write(14,0);state.add(0,0x1000000,true,false);require(state.less() && !state.zero,"signed comparison");
    Dsp gain(image);const auto input=gain.x[0x80+5],output=gain.x[0x80+6];
    gain.x[0x80+14]=1;gain.x[0x80+15]=0x400000;
    for(unsigned i=0;i<32;++i) gain.x[input+i]=(i&1)?0xC00000:0x400000;
    run_effect(gain,0);
    for(unsigned i=0;i<32;++i) require(gain.x[output+i]==((i&1)?0xE00000U:0x200000U),"gain output");
    gain.x[0x80+4]|=2;run_effect(gain,0);
    require(gain.x[output]==0x400000 && gain.x[output+1]==0xC00000,"gain accumulation");
    Dsp alias(image);alias.store(0x80+5,0x15C0);alias.store(0x80+6,0xDC0);
    alias.store(0x80+14,1);alias.store(0x80+15,0x400000);alias.store(0x15C0,0x400000);
    run_effect(alias,0);
    require(alias.load(0x15C0)==0x200000 && alias.load(0xDC0)==0x200000,"compiled effect shares both mix-buffer windows");
    Dsp filter(image);const auto filter_input=filter.x[0x519+5],filter_output=filter.x[0x519+9];
    filter.store(filter_input,0x400000);run_effect(filter,4);
    require(filter.x[filter_output]==0x400000,std::format("filter impulse path: input {:X}, output {:X}, value {:X}, paired {:X}",
        filter_input,filter_output,filter.x[filter_output],filter.x[filter.x[0x519+10]]));
    require(filter.x[0x519+37]==10 && filter.x[0x519+62]==10 && !(filter.read(57)&(1U<<20)),"filter history and status");
    // The SDL provider and offline effects use the same native processing path.
    // An effect-send sample must reach its actual DSP input, with bin headroom.
    Audio audio(false);audio.effects(image);
    const auto gain_offset=0x200+image.effects[0].descriptor[2]-image.data_file_offset+15*4;
    const auto original_gain=audio.effect_x(gain_offset,4);
    const std::array<std::uint32_t,1> update{0x00622D0D},immediate{0x00400000};
    std::array<std::uint32_t,1> readback{};
    audio.effect_data(gain_offset,std::as_bytes(std::span(update)),true);
    audio.effect_data(gain_offset,std::as_writable_bytes(std::span(readback)));
    require(readback[0]==original_gain,"queued effect data remains invisible before commit");
    audio.commit_effects();
    require(audio.effect_x(gain_offset,4)==update[0],"captured effect gain commits to DSP memory");
    audio.effect_data(gain_offset,std::as_bytes(std::span(update)),true);
    audio.effect_data(gain_offset+8,std::as_bytes(std::span(update)),true);
    audio.effect_x(gain_offset+4,4,0x00ABCDEF);audio.commit_effects();
    require(audio.effect_x(gain_offset+4,4)==0x00ABCDEF && audio.effect_x(gain_offset+8,4)==update[0],
        "deferred effects preserve unrelated live DSP state");
    audio.effect_data(gain_offset,std::as_bytes(std::span(update)),true);
    audio.effect_data(gain_offset,std::as_bytes(std::span(immediate)),false);audio.commit_effects();
    require(audio.effect_x(gain_offset,4)==immediate[0],"immediate effect writes supersede queued bytes");
    const std::array<std::byte,4> partial{std::byte{0x12},std::byte{0x7F},std::byte{0x34},std::byte{0x56}};
    audio.effect_data(gain_offset+2,partial,true);audio.commit_effects();
    audio.effect_data(gain_offset,std::as_writable_bytes(std::span(readback)));
    require(readback[0]==0x00120000 && audio.effect_x(gain_offset+4,4)==0x00AB5634,
        "unaligned effect data preserves adjacent bytes and 24-bit DSP words");
    bool effect_range_rejected=false;
    try {audio.effect_data(0x6000,std::as_bytes(std::span(update)),true);}
    catch(const std::runtime_error&) {effect_range_rejected=true;}
    require(effect_range_rejected,"effect data cannot escape native DSP memory");
    audio.effect_data(gain_offset,std::as_bytes(std::span(update)),true);audio.effects(image);
    const auto replaced_gain=audio.effect_x(gain_offset,4);audio.commit_effects();
    require(audio.effect_x(gain_offset,4)==replaced_gain,"effect image replacement discards queued parameters");
    const auto source_id=audio.create({1,1,2,16,0,48000});
    std::array<std::int16_t,32> samples{};samples.fill(8192);audio.data(source_id,std::as_bytes(std::span(samples)));
    const auto bin=(filter_input-0x1400)/32;
    require(filter_input>=0x1400 && filter_input<0x1800 && !(filter_input&31),"effect input mix-bin alignment");
    if(bin<32) {
        const std::array<MixBin,1> send{{{bin,0}}};audio.mixbins(source_id,send);audio.mixbin_headroom(bin,1);
        audio.play(source_id,true);std::array<float,64> mixed_output{};audio.mix(mixed_output);
        require(audio.effect_x(filter_input*4,4)==0x100000,"effect send and bin headroom");
        require(std::ranges::any_of(mixed_output,[](float value){return value!=0;}),"3D effect send reaches the speakers");
        const auto bus=audio.create({1,1,4,24,0,48000},AudioBus::submix);audio.mixbins(bus,send);
        audio.volume(bus,-2000);audio.route(source_id,bus);audio.mix(mixed_output);
        require(std::abs(static_cast<int>(audio.effect_x(filter_input*4,4))-104857)<2,"effect input-bus gain");
        audio.effect_scratch(8,4,0x11223344);const auto clock=audio.sample_clock();audio.effects(image);
        require(audio.effect_scratch(8,4)==0 && audio.sample_clock()==clock,"effect replacement retires scratch and preserves clock");
        audio.mix(mixed_output);
        require(audio.sample_clock()==clock+32 && std::abs(static_cast<int>(audio.effect_x(filter_input*4,4))-104857)<2,
            "replacement resumes native processing");
    }
    // Replay the manual lesson trace's engine distortion and 3D FXIN route.
    const auto engine=[&](std::int32_t volume,AudioBus mode,bool start,std::span<float> output) {
        Audio audio(false);audio.effects(image,2);
        const std::array<std::uint32_t,11> captured{0xDC0,0xDC0,0x1BC5B,0x3FFFFF,0x19A027,0x7FFFFF,0x1A7250,0,0x1FFFFF,0,0x7126E8};
        audio.effect_data((0xAE+5)*4,std::as_bytes(std::span(captured)),false);
        const auto source=audio.create({1,1,2,16,0,48000});
        const auto bus=audio.create({1,1,4,24,0,48000},mode,14);
        const std::array<MixBin,1> send{{{14,0}}};audio.mixbins(source,send);
        const std::array<MixBin,5> spatial{{{6,0},{8,0},{7,0},{9,0},{10,0}}};audio.mixbins(bus,spatial);
        audio.spatial(bus,{.pan={0.70710678f,0.70710678f},.front_back={0.5f,0.70710678f}});
        audio.volume(bus,volume);audio.data(source,std::as_bytes(std::span(samples)));audio.route(source,bus);audio.play(source,true);
        if(start) audio.play(bus,true);
        audio.mix(output);
    };
    std::array<float,128> normal{},quiet{},muted{},manual{};
    engine(0,AudioBus::effects,false,normal);
    require(std::ranges::all_of(std::span(normal).first(64),[](float value){return value==0;}) &&
        std::ranges::any_of(std::span(normal).subspan(64),[](float value){return value!=0;}),"engine DSP return reaches 3D speakers after one block");
    engine(-2000,AudioBus::effects,false,quiet);
    bool scaled=true;for(unsigned i=0;i<normal.size();++i) scaled&=std::abs(quiet[i]-normal[i]*0.1f)<0.000002f;
    require(scaled,"FXIN return volume applies once after effects");
    engine(-10000,AudioBus::effects,false,muted);
    require(std::ranges::all_of(muted,[](float value){return value==0;}),"FXIN return can be muted");
    engine(0,AudioBus::effects_manual,false,manual);
    require(std::ranges::all_of(manual,[](float value){return value==0;}),"FXIN2 requires explicit playback");
    engine(0,AudioBus::effects_manual,true,manual);
    require(manual==normal,"FXIN2 playback uses the same processed return");
    Dsp graph(image);const auto started=std::chrono::steady_clock::now();
    try {
        for(unsigned block=0;block<64;++block) {
            std::fill(graph.x.begin()+0xC00,graph.x.begin()+0x1000,0);
            if(!block) graph.store(0x1540,0x400000);
            for(unsigned effect=0;effect<image.effects.size();++effect) run_effect(graph,effect);
        }
    } catch(const std::exception& error) {failures.push_back(std::format("effect {}: {}",graph.active_effect,error.what()));}
    require(graph.transferred_words>0,"native delay transfers");
    const auto elapsed=std::chrono::duration<double,std::milli>(std::chrono::steady_clock::now()-started).count();
    std::string errors;for(const auto& failure:failures){if(!errors.empty()) errors+=',';errors+=json(failure);}
    return std::format("{{\"format\":\"b2-effects-check-v1\",\"passed\":{},\"game_booted\":false,\"programs\":{},\"checks\":{},\"frames\":2048,\"transfer_words\":{},\"processing_ms\":{},\"runtime_decoder\":false,\"failures\":[{}]}}",
        failures.empty(),image.effects.size(),checks,graph.transferred_words,elapsed,errors);
}
}
