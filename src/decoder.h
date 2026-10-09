#pragma once
#include "xbe.h"
#include <Zydis/Zydis.h>
#include <array>
#include <map>
#include <optional>

namespace b2 {
struct Instruction {
    std::uint32_t address;
    ZydisDecodedInstruction decoded;
    std::array<ZydisDecodedOperand, ZYDIS_MAX_OPERAND_COUNT> operands;
    std::array<std::byte, ZYDIS_MAX_INSTRUCTION_LENGTH> bytes;
    std::string text;
    std::optional<std::uint32_t> kernel_ordinal;
    std::vector<std::uint32_t> jump_targets;
    std::optional<std::uint32_t> target() const;
    std::string report() const;
};

class Decoder {
public:
    Decoder();
    Instruction decode(Xbe& image, std::uint32_t address);
    std::uint64_t decoded_count() const { return decoded_count_; }
private:
    ZydisDecoder decoder_{};
    ZydisFormatter formatter_{};
    std::array<std::byte, 16 * 1024> cache_{};
    std::uint32_t cache_address_ = 0;
    std::size_t cache_size_ = 0;
    const Xbe* cache_image_ = nullptr;
    std::uint64_t decoded_count_ = 0;
};
struct Edge {
    std::uint32_t from;
    std::string kind;
    std::optional<std::uint32_t> to;
};
struct Function {
    std::uint32_t entry;
    std::map<std::uint32_t, Instruction> instructions;
    std::vector<Edge> edges;
    std::vector<std::uint32_t> static_callbacks;
    std::vector<std::uint32_t> indirect_slots;
    std::vector<std::pair<std::uint32_t,std::uint32_t>> callback_stores;
    bool budget_exhausted = false;
    bool unresolved = false;
};
Function recover(Xbe& image, std::uint32_t address, std::uint32_t instruction_budget);
Function recover(Xbe& image, std::uint32_t address, std::uint32_t instruction_budget, Decoder& decoder);
std::string disassemble(Xbe& image, std::uint32_t address, std::uint32_t byte_count);
std::string analyse(Xbe& image, std::uint32_t address, std::uint32_t instruction_budget);
}
