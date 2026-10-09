#pragma once
#include "decoder.h"
#include <map>

namespace b2 {
struct LoweringIssue {
    std::uint32_t address;
    std::string bytes, instruction, reason;
};
struct LoweredFunction {
    std::string source;
    std::vector<LoweringIssue> issues;
    std::map<std::string,std::string> native_helpers;
    bool floating = false;
    std::string native_api;
};
LoweredFunction lower(const Function& function);
std::string recompile(Xbe& image, std::uint32_t address, std::uint32_t instruction_budget);
}
