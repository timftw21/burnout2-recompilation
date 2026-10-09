#include "crypto.h"
#include <algorithm>
#include <bit>
#include <cstring>

namespace b2 {
void Sha1::reset() {
    state={0x67452301,0xEFCDAB89,0x98BADCFE,0x10325476,0xC3D2E1F0};
    count={};buffer={};
}
void Sha1::block(Bytes input) {
    std::array<std::uint32_t,80> words{};
    for(unsigned i=0;i<16;++i) {
        std::uint32_t value;std::memcpy(&value,input.data()+i*4,4);
        words[i]=std::byteswap(value);
    }
    for(unsigned i=16;i<80;++i) words[i]=std::rotl(words[i-3]^words[i-8]^words[i-14]^words[i-16],1);
    auto a=state[0],b=state[1],c=state[2],d=state[3],e=state[4];
    for(unsigned i=0;i<80;++i) {
        const auto f=i<20?((b&c)|(~b&d)):i<40?(b^c^d):i<60?((b&c)|(b&d)|(c&d)):(b^c^d);
        const auto k=i<20?0x5A827999U:i<40?0x6ED9EBA1U:i<60?0x8F1BBCDCU:0xCA62C1D6U;
        const auto next=std::rotl(a,5)+f+e+k+words[i];
        e=d;d=c;c=std::rotl(b,30);b=a;a=next;
    }
    state[0]+=a;state[1]+=b;state[2]+=c;state[3]+=d;state[4]+=e;
}
void Sha1::update(Bytes input) {
    if(input.empty()) return;
    const auto bits=(static_cast<std::uint64_t>(count[1])<<32)|count[0];
    const auto next=bits+static_cast<std::uint64_t>(input.size())*8;
    count={static_cast<std::uint32_t>(next),static_cast<std::uint32_t>(next>>32)};
    auto used=static_cast<std::size_t>((bits>>3)&63);
    if(used) {
        const auto size=std::min(64-used,input.size());
        std::copy_n(input.begin(),size,buffer.begin()+used);input=input.subspan(size);used+=size;
        if(used<64) return;
        block(buffer);
    }
    while(input.size()>=64) {block(input.first(64));input=input.subspan(64);}
    std::copy(input.begin(),input.end(),buffer.begin());
}
ShaDigest Sha1::finish() {
    const auto bits=(static_cast<std::uint64_t>(count[1])<<32)|count[0];
    std::array<std::byte,72> padding{};padding[0]=std::byte{0x80};
    const auto used=static_cast<unsigned>((bits>>3)&63),size=used<56?56-used:120-used;
    const auto big=std::byteswap(bits);std::memcpy(padding.data()+size,&big,8);
    update(Bytes(padding).first(size+8));
    ShaDigest digest{};
    for(unsigned i=0;i<5;++i) {const auto value=std::byteswap(state[i]);std::memcpy(digest.data()+i*4,&value,4);}
    state={};count={};buffer={};return digest;
}
ShaDigest xbox_hmac(Bytes key,Bytes first,Bytes second) {
    // The Xbox contract truncates keys above 64 bytes instead of hashing them.
    std::array<std::byte,64> pad{};
    std::copy_n(key.begin(),std::min(key.size(),pad.size()),pad.begin());
    for(auto& value:pad) value^=std::byte{0x36};
    Sha1 sha;sha.reset();sha.update(pad);sha.update(first);sha.update(second);const auto inner=sha.finish();
    for(auto& value:pad) value^=std::byte{0x36^0x5C};
    sha.reset();sha.update(pad);sha.update(inner);return sha.finish();
}
}
