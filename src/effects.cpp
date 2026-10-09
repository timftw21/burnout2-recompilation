#include "effects.h"
#include <stdexcept>

namespace b2 {
namespace {
// Verified against the supported SDK's compiled 236304/23637D/2363AA routines.
std::uint64_t cipher_step(std::uint64_t state) {
    const auto feedback=(state^(state>>2)^(state>>3)^(state>>63))&1;
    return (state>>1)|(feedback<<63);
}
std::array<std::byte,8> cipher_key() {
    std::uint64_t state=0x7FA49BCA49DE12BAULL;
    for(unsigned i=0;i<81;++i) state=cipher_step(state);
    std::array<std::byte,8> key{};
    for(unsigned i=0;i<8;++i) key[i]=static_cast<std::byte>(state>>(i*8));
    return key;
}
void decrypt(std::span<std::byte> bytes,Bytes seed) {
    if(seed.size()!=8) throw std::runtime_error("Invalid DSP cipher seed");
    const auto key=cipher_key();
    std::uint64_t state=static_cast<std::uint64_t>(u32(key,4))<<32|u32(key,0);
    state=cipher_step(state);
    for(std::size_t i=0;i<bytes.size();++i) {
        const auto k=std::to_integer<unsigned>(key[i&7]),s=std::to_integer<unsigned>(seed[i&7]);
        const auto difference=static_cast<std::uint8_t>(std::to_integer<unsigned>(bytes[i])-k*s);
        state=cipher_step(state);
        bytes[i]=static_cast<std::byte>(static_cast<std::uint8_t>(k^static_cast<std::uint8_t>(state)^difference));
    }
}
}
EffectsImage decode_effects(Bytes image) {
    if(image.size()<0x818 || image.size()>65536) throw std::runtime_error("DSP image exceeds its bounded format");
    EffectsImage result;result.program_words=u32(image,0x804);
    const auto data_words=u32(image,0x80C);
    if(result.program_words>4096 || data_words>4096) throw std::runtime_error("Invalid DSP section sizes");
    const auto data_offset=0x818+result.program_words*4,metadata=data_offset+data_words*4;
    result.data_file_offset=data_offset;
    if(metadata+8>image.size()) throw std::runtime_error("Truncated DSP descriptor table");
    const auto count=u32(image,metadata),seed_offset=metadata+8+count*32;
    if(!count || count>64 || seed_offset+count*8>image.size()) throw std::runtime_error("Invalid DSP effect count");
    result.descriptor_flags=u32(image,metadata+4);
    std::vector<std::byte> seeds(image.begin()+seed_offset,image.begin()+seed_offset+count*8);
    const auto key=cipher_key();decrypt(seeds,key);
    result.data.reserve(data_words);
    for(unsigned i=0;i<data_words;++i) result.data.push_back(u32(image,data_offset+i*4));
    result.effects.reserve(count);std::uint32_t offset=0x818;
    for(unsigned i=0;i<count;++i) {
        EffectProgram effect;effect.file_offset=offset;
        for(unsigned field=0;field<8;++field) effect.descriptor[field]=u32(image,metadata+8+i*32+field*4);
        const auto size=effect.descriptor[1];
        const auto state=effect.descriptor[2],state_bytes=effect.descriptor[3];
        if(state<data_offset || (state&3U) || (state_bytes&3U) || state-data_offset>data_words*4 || state_bytes>data_words*4-(state-data_offset))
            throw std::runtime_error("DSP effect state exceeds its data section");
        if(!size || (size&3U) || static_cast<std::uint64_t>(offset)+size>data_offset)
            throw std::runtime_error("DSP effect program exceeds its declared section");
        effect.code.assign(image.begin()+offset,image.begin()+offset+size);
        decrypt(effect.code,Bytes(seeds).subspan(i*8,8));result.effects.push_back(std::move(effect));offset+=size;
    }
    return result;
}
}
