#pragma once
#include "file.h"
#include <array>

namespace b2 {
using ShaDigest=std::array<std::byte,20>;
// Xbox SHA storage is copied by the original SDK; it must contain no host pointers.
struct Sha1 {
    std::array<std::uint32_t,6> reserved{};
    std::array<std::uint32_t,5> state{};
    std::array<std::uint32_t,2> count{};
    std::array<std::byte,64> buffer{};
    void reset();
    void update(Bytes);
    ShaDigest finish();
private:
    void block(Bytes);
};
static_assert(sizeof(Sha1)==116);
ShaDigest xbox_hmac(Bytes key,Bytes first,Bytes second={});
}
