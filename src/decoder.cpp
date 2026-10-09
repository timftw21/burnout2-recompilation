#include "decoder.h"
#include <algorithm>
#include <deque>
#include <map>
#include <set>
#include <sstream>
#include <stdexcept>
#include <unordered_set>
#include <bit>

namespace b2 {
namespace {
const Instruction* previous(const Function& function, std::uint32_t address) {
    const auto found = function.instructions.lower_bound(address);
    if (found == function.instructions.begin()) return nullptr;
    const auto& instruction = std::prev(found)->second;
    return instruction.address + instruction.decoded.length == address ? &instruction : nullptr;
}
struct TableGuard { std::uint32_t compare, branch, count; };
// A small modular interval proves masked, short-copy and reverse-copy table
// indices without retaining a decoded image or guessing table lengths.
struct Range {
    std::uint32_t first=0;
    unsigned count=0; // Zero means unknown; finite ranges contain at most 256 values.
    auto operator<=>(const Range&) const = default;
};
Range enclosing(std::vector<std::uint32_t> values) {
    if(values.empty()) return {};
    std::ranges::sort(values); const auto last=std::unique(values.begin(),values.end()); values.erase(last,values.end());
    std::uint32_t largest_gap=0,first=values.front();
    for(std::size_t i=0;i<values.size();++i) {
        const auto next=values[(i+1)%values.size()];
        const auto gap=next-values[i];
        if(gap>=largest_gap) {largest_gap=gap;first=next;}
    }
    if(values.size()==1) return {first,1};
    const auto count=static_cast<std::uint64_t>(UINT32_MAX)-largest_gap+2;
    return count<=256?Range{first,static_cast<unsigned>(count)}:Range{};
}
Range merge(Range first,Range second) {
    if(!first.count || !second.count) return {};
    if(first==second) return first;
    std::vector<std::uint32_t> values; values.reserve(first.count+second.count);
    for(unsigned i=0;i<first.count;++i) values.push_back(first.first+i);
    for(unsigned i=0;i<second.count;++i) values.push_back(second.first+i);
    return enclosing(std::move(values));
}
int general(ZydisRegister reg) {
    switch(ZydisRegisterGetLargestEnclosing(ZYDIS_MACHINE_MODE_LEGACY_32,reg)) {
    case ZYDIS_REGISTER_EAX:return 0;case ZYDIS_REGISTER_ECX:return 1;case ZYDIS_REGISTER_EDX:return 2;case ZYDIS_REGISTER_EBX:return 3;
    case ZYDIS_REGISTER_ESP:return 4;case ZYDIS_REGISTER_EBP:return 5;case ZYDIS_REGISTER_ESI:return 6;case ZYDIS_REGISTER_EDI:return 7;
    default:return -1;
    }
}
struct FlowState {
    std::array<Range,8> registers{};
    std::array<std::uint32_t,8> nonzero_masks{};
    Range stack_top;
    int compared=-1;
    std::uint32_t immediate=0;
    bool subtract=false;
    bool test=false;
    auto operator<=>(const FlowState&) const = default;
};
Range filter_range(Range range,ZydisMnemonic branch,std::uint32_t bound,bool taken) {
    if(branch!=ZYDIS_MNEMONIC_JB && branch!=ZYDIS_MNEMONIC_JBE && branch!=ZYDIS_MNEMONIC_JNB &&
       branch!=ZYDIS_MNEMONIC_JNBE && branch!=ZYDIS_MNEMONIC_JZ && branch!=ZYDIS_MNEMONIC_JNZ) return range;
    const auto accepts=[&](std::uint32_t value) {
        bool result;
        switch(branch) {
        case ZYDIS_MNEMONIC_JB:result=value<bound;break;
        case ZYDIS_MNEMONIC_JBE:result=value<=bound;break;
        case ZYDIS_MNEMONIC_JNB:result=value>=bound;break;
        case ZYDIS_MNEMONIC_JNBE:result=value>bound;break;
        case ZYDIS_MNEMONIC_JZ:result=value==bound;break;
        case ZYDIS_MNEMONIC_JNZ:result=value!=bound;break;
        default:return true;
        }
        return result==taken;
    };
    if(!range.count) {
        if((branch==ZYDIS_MNEMONIC_JZ && taken) || (branch==ZYDIS_MNEMONIC_JNZ && !taken)) return {bound,1};
        if(branch==ZYDIS_MNEMONIC_JZ || branch==ZYDIS_MNEMONIC_JNZ) return {};
        const bool upper=(taken && (branch==ZYDIS_MNEMONIC_JB || branch==ZYDIS_MNEMONIC_JBE)) ||
            (!taken && (branch==ZYDIS_MNEMONIC_JNB || branch==ZYDIS_MNEMONIC_JNBE));
        const auto inclusive=branch==ZYDIS_MNEMONIC_JBE || branch==ZYDIS_MNEMONIC_JNBE;
        const auto limit=static_cast<std::uint64_t>(bound)+(inclusive?1:0);
        if(upper && limit && limit<=256) range={0,static_cast<unsigned>(limit)};
        else if(!upper && limit<0x100000000ULL && 0x100000000ULL-limit<=256)
            range={static_cast<std::uint32_t>(limit),static_cast<unsigned>(0x100000000ULL-limit)};
        else return {};
    }
    std::vector<std::uint32_t> values;
    for(unsigned i=0;i<range.count;++i) if(accepts(range.first+i)) values.push_back(range.first+i);
    return enclosing(std::move(values));
}
std::map<std::uint32_t,std::vector<std::uint32_t>> bounded_tables(Xbe& image,const Function& function) {
    std::map<std::uint32_t,FlowState> states;
    std::deque<std::uint32_t> pending{function.entry}; states.emplace(function.entry,FlowState{});
    const auto update=[&](std::uint32_t pc,FlowState value) {
        if(!function.instructions.contains(pc)) return;
        const auto found=states.find(pc);
        if(found==states.end()) {states.emplace(pc,value);pending.push_back(pc);return;}
        auto combined=found->second;
        for(unsigned i=0;i<8;++i) combined.registers[i]=merge(combined.registers[i],value.registers[i]);
        combined.stack_top=merge(combined.stack_top,value.stack_top);
        for(unsigned i=0;i<8;++i) if(combined.nonzero_masks[i]!=value.nonzero_masks[i]) combined.nonzero_masks[i]=0;
        if(combined.compared!=value.compared || combined.immediate!=value.immediate || combined.subtract!=value.subtract || combined.test!=value.test) combined.compared=-1;
        if(combined!=found->second) {found->second=combined;pending.push_back(pc);}
    };
    unsigned iterations=0;
    while(!pending.empty() && ++iterations<=function.instructions.size()*32) {
        const auto pc=pending.front();pending.pop_front();
        const auto& instruction=function.instructions.at(pc); const auto& ins=instruction.decoded; const auto& operands=instruction.operands;
        auto state=states.at(pc); const auto original=state;
        const auto mnemonic=ins.mnemonic;
        for(unsigned i=0;i<ins.operand_count;++i) {
            const auto& operand=operands[i];
            if((operand.actions&ZYDIS_OPERAND_ACTION_MASK_WRITE) &&
                (operand.type==ZYDIS_OPERAND_TYPE_MEMORY ||
                 (operand.type==ZYDIS_OPERAND_TYPE_REGISTER && general(operand.reg.value)==4))) state.stack_top={};
        }
        if(mnemonic==ZYDIS_MNEMONIC_PUSH && ins.operand_width==32) {
            if(operands[0].type==ZYDIS_OPERAND_TYPE_IMMEDIATE) state.stack_top={static_cast<std::uint32_t>(operands[0].imm.value.u),1};
            else if(operands[0].type==ZYDIS_OPERAND_TYPE_REGISTER) {
                const auto index=general(operands[0].reg.value);
                if(index>=0) state.stack_top=original.registers[index];
            }
        }
        if(ins.cpu_flags && (ins.cpu_flags->modified || ins.cpu_flags->set_0 || ins.cpu_flags->set_1 || ins.cpu_flags->undefined)) state.compared=-1;
        for(unsigned i=0;i<ins.operand_count;++i) if(operands[i].type==ZYDIS_OPERAND_TYPE_REGISTER && (operands[i].actions&ZYDIS_OPERAND_ACTION_MASK_WRITE)) {
            const auto index=general(operands[i].reg.value);if(index>=0) {
                state.registers[index]={};state.nonzero_masks[index]=0; if(index==state.compared) state.compared=-1;
            }
        }
        const auto target=operands[0].type==ZYDIS_OPERAND_TYPE_REGISTER?general(operands[0].reg.value):-1;
        if(target>=0 && operands[0].size==32) {
            const bool immediate=operands[1].type==ZYDIS_OPERAND_TYPE_IMMEDIATE;
            const auto value=immediate?static_cast<std::uint32_t>(operands[1].imm.value.u):0U;
            const auto source=operands[1].type==ZYDIS_OPERAND_TYPE_REGISTER?general(operands[1].reg.value):-1;
            const auto previous_range=original.registers[target];
            if(mnemonic==ZYDIS_MNEMONIC_POP) state.registers[target]=original.stack_top;
            else if(mnemonic==ZYDIS_MNEMONIC_MOV) {
                if(immediate) state.registers[target]={value,1}; else if(source>=0 && operands[1].size==32) {
                    state.registers[target]=original.registers[source];state.nonzero_masks[target]=original.nonzero_masks[source];
                }
            } else if((mnemonic==ZYDIS_MNEMONIC_XOR || mnemonic==ZYDIS_MNEMONIC_SUB) && source==target) state.registers[target]={0,1};
            else if(mnemonic==ZYDIS_MNEMONIC_AND && immediate && value<=255) {
                const auto mask=original.nonzero_masks[target];const bool nonzero=mask && (mask&value)==mask;
                state.registers[target]={nonzero?1U:0U,nonzero?value:value+1}; state.nonzero_masks[target]=nonzero?mask:0;
            } else if(mnemonic==ZYDIS_MNEMONIC_AND && source>=0) {
                const auto first=previous_range,second=original.registers[source];
                const auto mask_bound=[](Range range)->std::optional<unsigned> {
                    if(range.count && static_cast<std::uint64_t>(range.first)+range.count-1<=255) return range.first+range.count-1;
                    return {};
                };
                const auto a=mask_bound(first),b=mask_bound(second);
                if(a || b) state.registers[target]={0,(a && b?std::min(*a,*b):a?*a:*b)+1};
            } else if(mnemonic==ZYDIS_MNEMONIC_MOVZX && operands[1].size==8) {
                state.registers[target]={0,256};
            }
            else if((mnemonic==ZYDIS_MNEMONIC_ADD || mnemonic==ZYDIS_MNEMONIC_SUB) && immediate && previous_range.count)
                state.registers[target]={previous_range.first+(mnemonic==ZYDIS_MNEMONIC_ADD?value:0U-value),previous_range.count};
            else if(mnemonic==ZYDIS_MNEMONIC_NEG && previous_range.count)
                state.registers[target]={0U-(previous_range.first+previous_range.count-1),previous_range.count};
            else if(mnemonic==ZYDIS_MNEMONIC_SHR && immediate && previous_range.count) {
                std::vector<std::uint32_t> values;
                for(unsigned i=0;i<previous_range.count;++i) values.push_back((previous_range.first+i)>>(value&31));
                state.registers[target]=enclosing(std::move(values));
            }
            const bool constant_register=source>=0 && operands[1].size==32 && original.registers[source].count==1;
            if((mnemonic==ZYDIS_MNEMONIC_CMP || mnemonic==ZYDIS_MNEMONIC_SUB) && (immediate || constant_register)) {
                state.compared=target;state.immediate=immediate?value:original.registers[source].first;
                state.subtract=mnemonic==ZYDIS_MNEMONIC_SUB;state.test=false;
            } else if(mnemonic==ZYDIS_MNEMONIC_TEST && (immediate || source==target)) {
                state.compared=target;state.immediate=immediate?value:UINT32_MAX;state.subtract=false;state.test=true;
            }
        }
        const auto category=ins.meta.category; const auto destination=instruction.target();const auto next=pc+ins.length;
        if(category==ZYDIS_CATEGORY_CALL) {update(next,FlowState{});continue;}
        if(category==ZYDIS_CATEGORY_RET || category==ZYDIS_CATEGORY_INTERRUPT || mnemonic==ZYDIS_MNEMONIC_INT3 || mnemonic==ZYDIS_MNEMONIC_HLT) continue;
        if(category==ZYDIS_CATEGORY_COND_BR) {
            for(bool taken:{false,true}) {
                auto edge_state=state;
                if(state.compared>=0) {
                    const bool carry=mnemonic==ZYDIS_MNEMONIC_JB || mnemonic==ZYDIS_MNEMONIC_JNB;
                    if(state.test) {
                        if((mnemonic==ZYDIS_MNEMONIC_JNZ && taken) || (mnemonic==ZYDIS_MNEMONIC_JZ && !taken))
                            edge_state.nonzero_masks[state.compared]=state.immediate;
                    } else if(state.subtract && carry && ((mnemonic==ZYDIS_MNEMONIC_JB)==taken) && state.immediate && state.immediate<=256)
                        edge_state.registers[state.compared]={0U-state.immediate,state.immediate};
                    else if(!state.subtract) edge_state.registers[state.compared]=filter_range(state.registers[state.compared],mnemonic,state.immediate,taken);
                }
                if(!taken) update(next,edge_state);else if(destination) update(*destination,edge_state);
            }
        } else if(category==ZYDIS_CATEGORY_UNCOND_BR) {
            if(destination) update(*destination,state);
            else for(auto target_address:instruction.jump_targets) update(target_address,state);
        } else update(next,state);
    }
    std::map<std::uint32_t,std::vector<std::uint32_t>> result;
    if(!pending.empty()) return result;
    for(const auto& [pc,instruction]:function.instructions) {
        const auto& operand=instruction.operands[0];
        if(instruction.decoded.meta.category!=ZYDIS_CATEGORY_UNCOND_BR || instruction.target() || instruction.kernel_ordinal ||
            !instruction.jump_targets.empty() || !states.contains(pc) || operand.type!=ZYDIS_OPERAND_TYPE_MEMORY || operand.size!=32 ||
            operand.mem.base!=ZYDIS_REGISTER_NONE || operand.mem.index==ZYDIS_REGISTER_NONE || operand.mem.scale!=4 || operand.mem.segment!=ZYDIS_REGISTER_DS) continue;
        const auto index=general(operand.mem.index); if(index<0) continue;
        const auto range=states.at(pc).registers[index]; if(!range.count) continue;
        std::vector<std::uint32_t> targets;
        try {
            for(unsigned i=0;i<range.count;++i) {
                const auto address=static_cast<std::uint32_t>(operand.mem.disp.value)+(range.first+i)*4;
                const auto data=image.data(address,4);const auto target_address=u32(data,0);image.code_section(target_address);targets.push_back(target_address);
            }
        } catch(const std::exception&) {continue;}
        result.emplace(pc,std::move(targets));
    }
    return result;
}
std::optional<TableGuard> table_guard(Xbe& image, const Function& function, const Instruction& jump) {
    const auto& operand = jump.operands[0];
    if (operand.type != ZYDIS_OPERAND_TYPE_MEMORY || operand.size != 32 ||
        operand.mem.base != ZYDIS_REGISTER_NONE || operand.mem.index == ZYDIS_REGISTER_NONE ||
        operand.mem.scale != 4 || operand.mem.segment != ZYDIS_REGISTER_DS || !operand.mem.disp.has_displacement)
        return {};
    const auto* branch = previous(function, jump.address);
    const ZydisDecodedOperand* lookup = nullptr;
    if (branch && branch->decoded.mnemonic == ZYDIS_MNEMONIC_MOVZX &&
        branch->operands[0].type == ZYDIS_OPERAND_TYPE_REGISTER && branch->operands[0].reg.value == operand.mem.index &&
        branch->operands[1].type == ZYDIS_OPERAND_TYPE_MEMORY && branch->operands[1].size == 8 &&
        branch->operands[1].mem.base == operand.mem.index && branch->operands[1].mem.index == ZYDIS_REGISTER_NONE &&
        branch->operands[1].mem.segment == ZYDIS_REGISTER_DS && branch->operands[1].mem.disp.has_displacement) {
        lookup = &branch->operands[1];
        branch = previous(function, branch->address);
    }
    if (!branch || (branch->decoded.mnemonic != ZYDIS_MNEMONIC_JNBE && branch->decoded.mnemonic != ZYDIS_MNEMONIC_JNB)) return {};
    const auto* compare = previous(function, branch->address);
    // Compilers may save registers between CMP and its branch. Only permit
    // instructions proven to preserve flags and the complete index register.
    for (unsigned skipped = 0; compare && compare->decoded.mnemonic != ZYDIS_MNEMONIC_CMP && skipped < 8; ++skipped) {
        const auto& decoded = compare->decoded;
        if (decoded.mnemonic != ZYDIS_MNEMONIC_PUSH && decoded.mnemonic != ZYDIS_MNEMONIC_MOV && decoded.mnemonic != ZYDIS_MNEMONIC_LEA)
            return {};
        if (decoded.cpu_flags && (decoded.cpu_flags->modified || decoded.cpu_flags->set_0 || decoded.cpu_flags->set_1 || decoded.cpu_flags->undefined)) return {};
        for (unsigned i = 0; i < decoded.operand_count; ++i) {
            const auto& changed = compare->operands[i];
            if (changed.type == ZYDIS_OPERAND_TYPE_REGISTER && (changed.actions & ZYDIS_OPERAND_ACTION_MASK_WRITE) &&
                ZydisRegisterGetLargestEnclosing(ZYDIS_MACHINE_MODE_LEGACY_32, changed.reg.value) == operand.mem.index) return {};
        }
        compare = previous(function, compare->address);
    }
    if (!compare || compare->decoded.mnemonic != ZYDIS_MNEMONIC_CMP ||
        compare->operands[0].type != ZYDIS_OPERAND_TYPE_REGISTER || compare->operands[0].size != 32 ||
        compare->operands[0].reg.value != operand.mem.index || compare->operands[1].type != ZYDIS_OPERAND_TYPE_IMMEDIATE)
        return {};
    const auto bound = compare->operands[1].imm.value.u;
    auto count = bound + (branch->decoded.mnemonic == ZYDIS_MNEMONIC_JNBE ? 1 : 0);
    if (!count || count > 256 || (function.entry > compare->address && function.entry <= jump.address)) return {};
    if (lookup) {
        const auto remap = image.data(static_cast<std::uint32_t>(lookup->mem.disp.value), static_cast<std::size_t>(count));
        count = std::to_integer<unsigned char>(*std::ranges::max_element(remap)) + 1U;
    }
    return TableGuard{compare->address, branch->address, static_cast<std::uint32_t>(count)};
}
}
Decoder::Decoder() {
    if (!ZYAN_SUCCESS(ZydisDecoderInit(&decoder_, ZYDIS_MACHINE_MODE_LEGACY_32, ZYDIS_STACK_WIDTH_32)) ||
        !ZYAN_SUCCESS(ZydisFormatterInit(&formatter_, ZYDIS_FORMATTER_STYLE_INTEL)))
        throw std::runtime_error("Cannot initialise the x86 decoder");
}

Instruction Decoder::decode(Xbe& image, std::uint32_t address) {
    Instruction result{};
    result.address = address;
    if (cache_image_ != &image || address < cache_address_ ||
        static_cast<std::uint64_t>(address) - cache_address_ + result.bytes.size() > cache_size_) {
        cache_size_ = image.code_bytes(address, cache_);
        cache_address_ = address;
        cache_image_ = &image;
    }
    const auto offset = address - cache_address_;
    const auto count = std::min(result.bytes.size(), cache_size_ - offset);
    std::copy_n(cache_.begin() + offset, count, result.bytes.begin());
    if (!ZYAN_SUCCESS(ZydisDecoderDecodeFull(&decoder_, result.bytes.data(), count,
                                           &result.decoded, result.operands.data())))
        throw std::runtime_error("Invalid or truncated x86 instruction at " + hex32(address) +
                                 " [" + hex_bytes(Bytes(result.bytes).first(count)) + ']');
    ++decoded_count_;
    std::array<char, 256> text{};
    if (!ZYAN_SUCCESS(ZydisFormatterFormatInstruction(&formatter_, &result.decoded, result.operands.data(),
                        result.decoded.operand_count_visible, text.data(), text.size(), address, nullptr)))
        throw std::runtime_error("Cannot format instruction at " + hex32(address));
    result.text = text.data();
    return result;
}

std::optional<std::uint32_t> Instruction::target() const {
    for (unsigned i = 0; i < decoded.operand_count_visible; ++i) {
        const auto& operand = operands[i];
        if(operand.type==ZYDIS_OPERAND_TYPE_POINTER) return operand.ptr.offset;
        if (operand.type == ZYDIS_OPERAND_TYPE_IMMEDIATE && operand.imm.is_relative) {
            ZyanU64 target;
            if (ZYAN_SUCCESS(ZydisCalcAbsoluteAddress(&decoded, &operand, address, &target)))
                return static_cast<std::uint32_t>(target);
        }
    }
    return std::nullopt;
}

std::string Instruction::report() const {
    std::ostringstream out;
    out << "{\"address\":" << json(hex32(address)) << ",\"bytes\":"
        << json(hex_bytes(Bytes(bytes).first(decoded.length))) << ",\"text\":" << json(text);
    if (const auto dest = target()) out << ",\"target\":" << json(hex32(*dest));
    if (kernel_ordinal) out << ",\"kernel_ordinal\":" << *kernel_ordinal;
    if (!jump_targets.empty()) {
        out << ",\"jump_targets\":[";
        for (std::size_t i = 0; i < jump_targets.size(); ++i) {
            if (i) out << ',';
            out << json(hex32(jump_targets[i]));
        }
        out << ']';
    }
    return out.str() + '}';
}

std::string disassemble(Xbe& image, std::uint32_t address, std::uint32_t byte_count) {
    if (!byte_count || byte_count > 64 * 1024) throw std::runtime_error("Decode range must be 1..65536 bytes");
    const auto& section = image.code_section(address);
    if (static_cast<std::uint64_t>(address) - section.address + byte_count > std::min(section.size, section.raw_size))
        throw std::runtime_error("Decode range leaves the code section");
    Decoder decoder;
    std::ostringstream out;
    out << "{\"format\":\"b2-decode-v1\",\"sha256\":" << json(image.sha256) << ",\"instructions\":[";
    bool first = true;
    std::uint32_t consumed = 0;
    while (consumed < byte_count) {
        const auto instruction = decoder.decode(image, address + consumed);
        if (instruction.decoded.length > byte_count - consumed)
            throw std::runtime_error("Range ends inside an instruction at " + hex32(address + consumed));
        if (!first) out << ',';
        first = false;
        out << instruction.report();
        consumed += instruction.decoded.length;
    }
    return out.str() + "]}";
}

Function recover(Xbe& image, std::uint32_t address, std::uint32_t instruction_budget) {
    Decoder decoder;
    return recover(image, address, instruction_budget, decoder);
}

Function recover(Xbe& image, std::uint32_t address, std::uint32_t instruction_budget, Decoder& decoder) {
    if (!instruction_budget || instruction_budget > 4096) throw std::runtime_error("Analysis budget must be 1..4096 instructions");
    std::deque<std::uint32_t> pending{address};
    std::unordered_set<std::uint32_t> scheduled{address};
    Function function{address};
    std::map<std::uint32_t, TableGuard> guards;
    const auto schedule = [&](std::uint32_t target) {
        image.code_section(target);
        if (scheduled.insert(target).second) pending.push_back(target);
    };
    do {
    while (!pending.empty() && function.instructions.size() < instruction_budget) {
        const auto pc = pending.front();
        pending.pop_front();
        auto instruction = decoder.decode(image, pc);
        const auto next = pc + instruction.decoded.length;
        const auto after = function.instructions.lower_bound(pc);
        if ((after != function.instructions.end() && after->first < next) ||
            (after != function.instructions.begin() &&
             std::prev(after)->first + std::prev(after)->second.decoded.length > pc))
            throw std::runtime_error("Overlapping instruction boundaries at " + hex32(pc));
        const auto category = instruction.decoded.meta.category;
        const auto destination = instruction.target();
        if ((category == ZYDIS_CATEGORY_CALL || category == ZYDIS_CATEGORY_UNCOND_BR) && !destination) {
            const auto& operand = instruction.operands[0];
            if (operand.type == ZYDIS_OPERAND_TYPE_MEMORY && operand.size == 32 &&
                operand.mem.base == ZYDIS_REGISTER_NONE && operand.mem.index == ZYDIS_REGISTER_NONE &&
                operand.mem.segment == ZYDIS_REGISTER_DS)
                if (const auto* import = image.kernel_import(static_cast<std::uint32_t>(operand.mem.disp.value)))
                    instruction.kernel_ordinal = import->ordinal;
        }
        const auto edge = [&](std::string_view kind, std::optional<std::uint32_t> target) {
            function.edges.push_back({pc, std::string(kind), target});
        };
        if (category == ZYDIS_CATEGORY_CALL) {
            edge(instruction.kernel_ordinal ? "kernel-call" : "call", instruction.kernel_ordinal ? instruction.kernel_ordinal : destination);
            function.unresolved |= !destination && !instruction.kernel_ordinal;
            schedule(next);
        } else if (category == ZYDIS_CATEGORY_UNCOND_BR || category == ZYDIS_CATEGORY_COND_BR) {
            if (instruction.kernel_ordinal) edge("kernel-jump", instruction.kernel_ordinal);
            else if (destination) {
                edge(category == ZYDIS_CATEGORY_COND_BR ? "conditional" : "jump", destination);
                schedule(*destination);
            } else if (const auto guard = table_guard(image, function, instruction)) {
                const auto table = image.data(static_cast<std::uint32_t>(instruction.operands[0].mem.disp.value), guard->count * 4);
                for (std::uint32_t i = 0; i < guard->count; ++i) {
                    const auto target = u32(table, i * 4);
                    schedule(target);
                    instruction.jump_targets.push_back(target);
                    edge("table-jump", target);
                }
                guards.emplace(pc, *guard);
            } else { edge("jump", {}); function.unresolved = true; }
            if (category == ZYDIS_CATEGORY_COND_BR) schedule(next);
        } else if (category == ZYDIS_CATEGORY_RET) {
            edge("return", std::nullopt);
        } else if (instruction.decoded.mnemonic == ZYDIS_MNEMONIC_INT3 ||
                   instruction.decoded.mnemonic == ZYDIS_MNEMONIC_HLT ||
                   instruction.decoded.meta.category == ZYDIS_CATEGORY_INTERRUPT) {
            edge("trap", std::nullopt);
            function.unresolved |= instruction.decoded.mnemonic != ZYDIS_MNEMONIC_INT3 && instruction.decoded.mnemonic != ZYDIS_MNEMONIC_INT;
        } else {
            schedule(next);
        }
        function.instructions.emplace(pc, std::move(instruction));
    }
    if(!pending.empty()) break;
    const auto tables=bounded_tables(image,function);
    for(const auto& [pc,targets]:tables) {
        function.instructions.at(pc).jump_targets=targets;
        for(auto target:targets) {schedule(target);function.edges.push_back({pc,"table-jump",target});}
    }
    } while(!pending.empty());
    function.budget_exhausted = !pending.empty();
    // Every path to the table must execute its bounds comparison and branch.
    // Case entries or other branches into the middle invalidate that proof.
    for (const auto& [pc, guard] : guards) for (const auto& edge : function.edges)
        if (edge.to && (edge.kind == "jump" || edge.kind == "conditional" || edge.kind == "table-jump") &&
            *edge.to > guard.compare && *edge.to <= pc) {
            function.instructions.at(pc).jump_targets.clear();
            function.unresolved = true;
            break;
        }
    function.unresolved=std::ranges::any_of(function.instructions,[](const auto& pair) {
        const auto& instruction=pair.second;const auto category=instruction.decoded.meta.category;
        return (category==ZYDIS_CATEGORY_CALL || category==ZYDIS_CATEGORY_UNCOND_BR) && !instruction.target() &&
            !instruction.kernel_ordinal && instruction.jump_targets.empty();
    });
    // Compile installed and file-initialized callback references. This discovers
    // targets without claiming mutable pointers have a closed set of values.
    std::set<std::uint32_t> called_slots;
    const auto is_code=[&](std::uint32_t callback) {
        return std::ranges::any_of(image.sections,[&](const Section& section) {
            return section.executable() && section.name!=".rdata" && section.name!=".data" && section.name!="XON_RD" && callback>=section.address &&
                static_cast<std::uint64_t>(callback)-section.address<std::min(section.size,section.raw_size);
        });
    };
    const auto add_callback=[&](std::uint32_t callback) {if(is_code(callback)) function.static_callbacks.push_back(callback);};
    // The registered RenderWare transform plugin stores two methods relative
    // to its engine offset. EA890 dispatches the first through that same offset;
    // these are callbacks, not arbitrary integer writes into engine storage.
    if(function.entry==0x000EA8B0U) {
        constexpr std::string_view constructor="8B442408C780A8DC5A00F0A50E00A3E4645A00C780ACDC5A00B0A60E00FF05E8645A008B442404C3";
        if(hex_bytes(image.data(function.entry,constructor.size()/2))!=constructor ||
           hex_bytes(image.data(0x000EA890,12))!="8B0DE4645A00FFA1A8DC5A00")
            throw std::runtime_error("RenderWare transform plugin differs from its verified ABI");
        for(const auto& [pc,instruction]:function.instructions) {
            const auto& output=instruction.operands[0];const auto& input=instruction.operands[1];
            if(instruction.decoded.mnemonic==ZYDIS_MNEMONIC_MOV && output.type==ZYDIS_OPERAND_TYPE_MEMORY &&
               output.mem.base==ZYDIS_REGISTER_EAX && input.type==ZYDIS_OPERAND_TYPE_IMMEDIATE)
                add_callback(static_cast<std::uint32_t>(input.imm.value.u));
        }
    }
    // The material-effects atomic constructor saves the original object callback
    // in its plugin record, then installs a wrapper into the atomic's +10h slot.
    if(function.entry==0x001018B0U) {
        constexpr std::string_view constructor="8B4424048B0DBC6C5A00C70401000000008B15A8DC5A004A668950608B501089540104C7401070181000C3";
        if(hex_bytes(image.data(function.entry,constructor.size()/2))!=constructor)
            throw std::runtime_error("RenderWare atomic constructor differs from its verified ABI");
        for(const auto& [pc,instruction]:function.instructions)
            if(instruction.decoded.mnemonic==ZYDIS_MNEMONIC_MOV && instruction.operands[0].type==ZYDIS_OPERAND_TYPE_MEMORY &&
               instruction.operands[0].mem.base==ZYDIS_REGISTER_EAX && instruction.operands[0].mem.disp.value==0x10 &&
               instruction.operands[1].type==ZYDIS_OPERAND_TYPE_IMMEDIATE)
                add_callback(static_cast<std::uint32_t>(instruction.operands[1].imm.value.u));
    }
    std::set<std::pair<std::uint32_t,bool>> seen_tables;
    const auto add_vtable=[&](std::uint32_t table,unsigned minimum_indirections=0,bool nullable=false) {
        // A literal or loaded global can name a table or a preinitialized object whose first
        // field points to it. 1AA2A installs object 30F61C -> table 2B08B8;
        // 1AB2E/1AB3F then dispatch its first method. Follow one data pointer,
        // never an arbitrary memory graph, and keep the original table checks.
        // Frontend global 31FC88 -> object 31FC98 -> table 2B4CC0 requires
        // three bounded reads; 2A741 invokes that object's first method.
        for(unsigned depth=0;depth<3;++depth) {
            const auto section=std::ranges::find_if(image.sections,[&](const Section& candidate) {
                return (candidate.name==".rdata" || candidate.name==".data" || candidate.name=="XON_RD") && table>=candidate.address &&
                    static_cast<std::uint64_t>(table)-candidate.address+8<=std::min(candidate.size,candidate.raw_size);
            });
            if(section==image.sections.end()) return;
            const auto header=image.data(table,8);
            if(!is_code(u32(header,0)) || !is_code(u32(header,4))) {table=u32(header,0);continue;}
            // A loaded global must lead through an object to its method table.
            // CRT character-classification tables also contain packed words
            // that happen to fall within executable address ranges.
            if(depth<minimum_indirections) return;
            if(!seen_tables.emplace(table,nullable).second) return;
            const auto available=std::min(section->size,section->raw_size)-(table-section->address);
            const auto bytes=image.data(table,std::min<std::uint32_t>(available/4,256)*4);
            for(std::size_t offset=0;offset<bytes.size();offset+=4) {
                const auto target=u32(bytes,offset);if(!target && nullable) continue;
                if(!is_code(target)) break;add_callback(target);
            }
            return;
        }
    };
    // Original constructors take file-backed virtual-table addresses as literals.
    // Compile their file-backed method groups, retaining unresolved dispatch.
    for(const auto& [pc,instruction]:function.instructions) {
        const auto& source=instruction.operands[instruction.decoded.mnemonic==ZYDIS_MNEMONIC_MOV?1:0];
        if((instruction.decoded.mnemonic==ZYDIS_MNEMONIC_MOV || instruction.decoded.mnemonic==ZYDIS_MNEMONIC_PUSH) &&
            source.type==ZYDIS_OPERAND_TYPE_IMMEDIATE) add_vtable(static_cast<std::uint32_t>(source.imm.value.u));
        else if(instruction.decoded.mnemonic==ZYDIS_MNEMONIC_MOV && source.type==ZYDIS_OPERAND_TYPE_MEMORY &&
                source.size==32 && source.mem.base==ZYDIS_REGISTER_NONE && source.mem.index==ZYDIS_REGISTER_NONE &&
                source.mem.segment==ZYDIS_REGISTER_DS)
            add_vtable(static_cast<std::uint32_t>(source.mem.disp.value),2);
    }
    // UI draw callbacks are installed in value objects at +48h and consumed
    // from queued records at +50h by C1210. C14A0 also allocates a value object
    // from the same queue, returning the record's +8h address. Include its
    // callers and inlined constructors without changing mutable dispatch.
    const auto matches_bytes=[&](std::uint32_t address,std::string_view expected) {
        return hex_bytes(image.data(address,expected.size()/2))==expected;
    };
    constexpr std::array ui_initializers{0x0001EF10U,0x0001E720U,0x000229D0U,0x0002EF40U,0x000303B0U,0x000C0E70U,0x000C14A0U};
    constexpr std::array ui_methods{0x000BFBF0U,0x000BFE70U,0x000C0080U,0x000C0390U,0x000C04A0U,0x000C0890U,0x000C08F0U,0x000C0A60U};
    const bool ui_producer=std::ranges::find(ui_initializers,function.entry)!=ui_initializers.end() ||
        std::ranges::any_of(function.instructions,[&](const auto& item) {
            const auto& instruction=item.second;
            if(instruction.decoded.meta.category==ZYDIS_CATEGORY_CALL && instruction.target() &&
               std::ranges::find(ui_initializers,*instruction.target())!=ui_initializers.end()) return true;
            const auto& output=instruction.operands[0];const auto& input=instruction.operands[1];
            return instruction.decoded.mnemonic==ZYDIS_MNEMONIC_MOV && output.type==ZYDIS_OPERAND_TYPE_MEMORY && output.size==32 &&
                output.mem.disp.value==0x48 && input.type==ZYDIS_OPERAND_TYPE_IMMEDIATE &&
                std::ranges::find(ui_methods,static_cast<std::uint32_t>(input.imm.value.u))!=ui_methods.end();
        });
    std::vector<std::pair<std::uint32_t,int>> ui_callback_registers;
    if(ui_producer) for(const auto& [pc,instruction]:function.instructions) {
        const auto& output=instruction.operands[0];const auto& input=instruction.operands[1];
        if(instruction.decoded.mnemonic!=ZYDIS_MNEMONIC_MOV || output.type!=ZYDIS_OPERAND_TYPE_MEMORY || output.size!=32 ||
           output.mem.segment!=ZYDIS_REGISTER_DS || output.mem.base==ZYDIS_REGISTER_NONE || output.mem.base==ZYDIS_REGISTER_ESP ||
           output.mem.base==ZYDIS_REGISTER_EBP || output.mem.index!=ZYDIS_REGISTER_NONE || output.mem.disp.value!=0x48 ||
           !(input.type==ZYDIS_OPERAND_TYPE_IMMEDIATE || (input.type==ZYDIS_OPERAND_TYPE_REGISTER && input.size==32))) continue;
        if(!matches_bytes(0x0001EF10,"D944240C8B542418D84C24148B442414") ||
           !matches_bytes(0x0001EF2E,"C74148F0FB0B00") ||
           !matches_bytes(0x0001E749,"C7464870FE0B00") ||
           !matches_bytes(0x000229D0,"538B5C242056578B7C242C8BF1578BCB") ||
           !matches_bytes(0x000229F9,"C7464880000C00") ||
           !matches_bytes(0x0002EF40,"568B742424817E0C80000000") ||
           !matches_bytes(0x0002EF64,"C7414890030C00") ||
           !matches_bytes(0x000303B0,"D9442418535556578B7C2430") ||
           !matches_bytes(0x000304B0,"C74148A0040C00") ||
           !matches_bytes(0x000C0E70,"D9442418535556578B7C2430") ||
           !matches_bytes(0x000C0F7D,"C7414890080C00") ||
           !matches_bytes(0x000C1057,"C74148F0080C00") ||
           !matches_bytes(0x000C10A7,"C74148600A0C00") ||
           !matches_bytes(0x000C14A0,"8B513C568B742408C1E60403F28D460450E8EA8801008B4C241083C00883C4048970448948405EC20800") ||
           !matches_bytes(0x000C1306,"FF5650"))
            throw std::runtime_error("UI draw callback layout differs from its verified ABI");
        if(input.type==ZYDIS_OPERAND_TYPE_IMMEDIATE) add_callback(static_cast<std::uint32_t>(input.imm.value.u));
        else ui_callback_registers.emplace_back(pc,general(input.reg.value));
    }
    // Original resource descriptor constructors initialize ESI through 10CF90
    // before installing methods at +28h..+3Ch. 10B08E..10B0A3 installs one
    // complete group; 109F8B and 10A07A dispatch its +3Ch and +28h methods.
    // Match this descriptor contract, rather than treating arbitrary packet
    // words stored through a register as executable references.
    std::optional<std::uint32_t> resource_constructor;
    for(const auto& [pc,instruction]:function.instructions) {
        if(instruction.decoded.meta.category!=ZYDIS_CATEGORY_CALL || instruction.target()!=0x0010CF90U) continue;
        const auto* argument=previous(function,pc);
        if(!argument || argument->decoded.mnemonic!=ZYDIS_MNEMONIC_PUSH ||
            argument->operands[0].type!=ZYDIS_OPERAND_TYPE_REGISTER || argument->operands[0].reg.value!=ZYDIS_REGISTER_ESI) continue;
        constexpr std::string_view prefix="8B44240C568B7424085056E8909EFFFF";
        if(hex_bytes(image.data(0x0010CF90,prefix.size()/2))!=prefix)
            throw std::runtime_error("Resource descriptor constructor differs from its verified ABI");
        resource_constructor=pc;break;
    }
    if(resource_constructor) for(const auto& [pc,instruction]:function.instructions) {
        const auto& output=instruction.operands[0];const auto& input=instruction.operands[1];
        if(pc<=*resource_constructor || instruction.decoded.mnemonic!=ZYDIS_MNEMONIC_MOV ||
            output.type!=ZYDIS_OPERAND_TYPE_MEMORY || output.size!=32 || output.mem.segment!=ZYDIS_REGISTER_DS ||
            output.mem.base!=ZYDIS_REGISTER_ESI || output.mem.index!=ZYDIS_REGISTER_NONE ||
            output.mem.disp.value<0x28 || output.mem.disp.value>0x3C || (output.mem.disp.value&3) ||
            input.type!=ZYDIS_OPERAND_TYPE_IMMEDIATE) continue;
        add_callback(static_cast<std::uint32_t>(input.imm.value.u));
    }
    // EF9C0 attaches a frame-linked object, whose +10h field is invoked by
    // the original frame update. Discover installed methods for that typed
    // object; the atomic constructor additionally installs its +48h method.
    std::set<ZydisRegister> frame_objects;
    for(const auto& [pc,instruction]:function.instructions) if(instruction.target()==0x000EF9C0U) {
        constexpr std::string_view setter="8B4C24048B410485C07411";
        if(hex_bytes(image.data(0x000EF9C0,setter.size()/2))!=setter)
            throw std::runtime_error("RenderWare frame-object setter differs from its verified ABI");
        const auto* value=previous(function,pc);
        for(unsigned distance=0;value && distance<24;++distance) {
            if(value->decoded.mnemonic==ZYDIS_MNEMONIC_PUSH && value->decoded.operand_width==32) {
                if(value->operands[0].type==ZYDIS_OPERAND_TYPE_REGISTER) frame_objects.insert(value->operands[0].reg.value);
                break;
            }
            if(value->decoded.meta.category==ZYDIS_CATEGORY_CALL || value->decoded.meta.category==ZYDIS_CATEGORY_UNCOND_BR) break;
            value=previous(function,value->address);
        }
    }
    for(const auto& [pc,instruction]:function.instructions) {
        const auto& output=instruction.operands[0];const auto& input=instruction.operands[1];
        if(instruction.decoded.mnemonic!=ZYDIS_MNEMONIC_MOV || output.type!=ZYDIS_OPERAND_TYPE_MEMORY || output.size!=32 ||
           output.mem.index!=ZYDIS_REGISTER_NONE || !frame_objects.contains(output.mem.base) ||
           (output.mem.disp.value!=0x10 && !(function.entry==0x000FDC60U && output.mem.disp.value==0x48))) continue;
        if(input.type==ZYDIS_OPERAND_TYPE_IMMEDIATE) {add_callback(static_cast<std::uint32_t>(input.imm.value.u));continue;}
        auto reg=input.type==ZYDIS_OPERAND_TYPE_REGISTER?general(input.reg.value):-1;
        const auto* value=previous(function,pc);
        for(unsigned distance=0;reg>=0 && value && distance<24;++distance) {
            const auto category=value->decoded.meta.category;
            if(category==ZYDIS_CATEGORY_CALL || category==ZYDIS_CATEGORY_UNCOND_BR || category==ZYDIS_CATEGORY_RET) break;
            if(std::ranges::any_of(std::span(value->operands).first(value->decoded.operand_count),[&](const auto& operand) {
                return operand.type==ZYDIS_OPERAND_TYPE_REGISTER && general(operand.reg.value)==reg &&
                    (operand.actions&ZYDIS_OPERAND_ACTION_MASK_WRITE);
            })) {
                const auto& source=value->operands[1];
                if(value->decoded.mnemonic!=ZYDIS_MNEMONIC_MOV || value->operands[0].size!=32) break;
                if(source.type==ZYDIS_OPERAND_TYPE_IMMEDIATE) {add_callback(static_cast<std::uint32_t>(source.imm.value.u));break;}
                reg=source.type==ZYDIS_OPERAND_TYPE_REGISTER && source.size==32?general(source.reg.value):-1;
            }
            value=previous(function,value->address);
        }
    }
    const auto absolute_slot=[](const ZydisDecodedOperand& operand) {
        return operand.type==ZYDIS_OPERAND_TYPE_MEMORY && operand.size==32 &&
            operand.mem.base==ZYDIS_REGISTER_NONE && operand.mem.index==ZYDIS_REGISTER_NONE && operand.mem.segment==ZYDIS_REGISTER_DS;
    };
    for(const auto& [pc,instruction]:function.instructions) {
        const auto& operand=instruction.operands[0];
        const auto category=instruction.decoded.meta.category;
        if((category!=ZYDIS_CATEGORY_CALL && category!=ZYDIS_CATEGORY_UNCOND_BR) || instruction.target() || instruction.kernel_ordinal) continue;
        if(absolute_slot(operand)) called_slots.insert(static_cast<std::uint32_t>(operand.mem.disp.value));
        else if(operand.type==ZYDIS_OPERAND_TYPE_MEMORY && operand.size==32 && operand.mem.base==ZYDIS_REGISTER_NONE &&
                operand.mem.index!=ZYDIS_REGISTER_NONE && operand.mem.scale==4 && operand.mem.segment==ZYDIS_REGISTER_DS) {
            const auto table=static_cast<std::uint32_t>(operand.mem.disp.value);
            const auto section=std::ranges::find_if(image.sections,[&](const Section& candidate) {
                return table>=candidate.address && static_cast<std::uint64_t>(table)-candidate.address+4<=std::min(candidate.size,candidate.raw_size);
            });
            if(section!=image.sections.end()) {
                const auto available=std::min(section->size,section->raw_size)-(table-section->address);
                const auto bytes=image.data(table,std::min<std::uint32_t>(available/4,256)*4);
                // Unguarded SDK tables can have a domain enforced by their API
                // callers. Discover their contiguous file-backed code pointers,
                // but retain unresolved dispatch: this is not a proof of bounds.
                for(std::size_t offset=0;offset<bytes.size();offset+=4) {
                    const auto target=u32(bytes,offset);if(!is_code(target)) break;add_callback(target);
                }
            }
        }
        else if(operand.type==ZYDIS_OPERAND_TYPE_REGISTER && operand.size==32) {
            auto reg=general(operand.reg.value);const auto* origin=previous(function,pc);
            for(unsigned distance=0;reg>=0 && origin && distance<16;++distance) {
                const auto kind=origin->decoded.meta.category;
                if(kind==ZYDIS_CATEGORY_CALL || kind==ZYDIS_CATEGORY_UNCOND_BR || kind==ZYDIS_CATEGORY_RET) break;
                const auto& output=origin->operands[0];const auto& input=origin->operands[1];
                const bool writes=std::ranges::any_of(std::span(origin->operands).first(origin->decoded.operand_count),[&](const auto& changed) {
                    return changed.type==ZYDIS_OPERAND_TYPE_REGISTER && general(changed.reg.value)==reg &&
                        (changed.actions&ZYDIS_OPERAND_ACTION_MASK_WRITE);
                });
                if(writes) {
                    if(origin->decoded.mnemonic!=ZYDIS_MNEMONIC_MOV || output.type!=ZYDIS_OPERAND_TYPE_REGISTER || output.size!=32 || general(output.reg.value)!=reg) break;
                    if(absolute_slot(input)) {called_slots.insert(static_cast<std::uint32_t>(input.mem.disp.value));break;}
                    if(input.type==ZYDIS_OPERAND_TYPE_IMMEDIATE) {add_callback(static_cast<std::uint32_t>(input.imm.value.u));break;}
                    if(input.type!=ZYDIS_OPERAND_TYPE_REGISTER || input.size!=32) break;
                    reg=general(input.reg.value);
                }
                origin=previous(function,origin->address);
            }
        }
    }
    for(auto slot:called_slots) if(std::ranges::any_of(image.sections,[&](const Section& section) {
        return slot>=section.address && static_cast<std::uint64_t>(slot)-section.address+4<=std::min(section.size,section.raw_size);
    })) add_callback(u32(image.data(slot,4),0));
    function.indirect_slots.assign(called_slots.begin(),called_slots.end());
    for(const auto& [pc,instruction]:function.instructions) {
        const auto& output=instruction.operands[0];const auto& input=instruction.operands[1];
        if(instruction.decoded.mnemonic!=ZYDIS_MNEMONIC_MOV || output.type!=ZYDIS_OPERAND_TYPE_MEMORY || output.size!=32 ||
            (output.mem.segment!=ZYDIS_REGISTER_DS && output.mem.segment!=ZYDIS_REGISTER_SS) ||
            output.mem.base!=ZYDIS_REGISTER_NONE || output.mem.index!=ZYDIS_REGISTER_NONE) continue;
        std::optional<std::uint32_t> callback;
        if(input.type==ZYDIS_OPERAND_TYPE_IMMEDIATE) callback=static_cast<std::uint32_t>(input.imm.value.u);
        else if(input.type==ZYDIS_OPERAND_TYPE_REGISTER && input.size==32) {
            auto reg=general(input.reg.value);const auto* value=previous(function,pc);
            for(unsigned distance=0;reg>=0 && value && distance<24;++distance) {
                const auto category=value->decoded.meta.category;
                if(category==ZYDIS_CATEGORY_CALL || category==ZYDIS_CATEGORY_UNCOND_BR ||
                   category==ZYDIS_CATEGORY_COND_BR || category==ZYDIS_CATEGORY_RET) break;
                if(std::ranges::any_of(std::span(value->operands).first(value->decoded.operand_count),[&](const auto& operand) {
                    return operand.type==ZYDIS_OPERAND_TYPE_REGISTER && general(operand.reg.value)==reg &&
                        (operand.actions&ZYDIS_OPERAND_ACTION_MASK_WRITE);
                })) {
                    const auto& source=value->operands[1];
                    if(value->decoded.mnemonic!=ZYDIS_MNEMONIC_MOV || value->operands[0].size!=32) break;
                    if(source.type==ZYDIS_OPERAND_TYPE_IMMEDIATE) {callback=static_cast<std::uint32_t>(source.imm.value.u);break;}
                    reg=source.type==ZYDIS_OPERAND_TYPE_REGISTER && source.size==32?general(source.reg.value):-1;
                }
                value=previous(function,value->address);
            }
        }
        if(!callback || !is_code(*callback)) continue;
        // Integer packet words can also fall inside .text. A write alone is
        // not evidence of a function pointer; require an actual called slot.
        function.callback_stores.emplace_back(static_cast<std::uint32_t>(output.mem.disp.value),*callback);
    }
    // The original floating formatter installs one six-cell global interface.
    // Preserve its complete verified group, including methods not yet called.
    if(function.entry==0x0011E251U) {
        if(!matches_bytes(function.entry,"B86B181200A3B47E3400C705B87E340016151200C705BC7E34007B151200C705C07E3400BE141200C705C47E340061151200A3C87E3400C3"))
            throw std::runtime_error("CRT conversion interface differs from its verified initializer");
        for(const auto& [slot,method]:function.callback_stores)
            if(slot>=0x00347EB4U && slot<=0x00347EC8U) add_callback(method);
    }
    // RxNodeDefinition getters return a file-backed descriptor containing a name
    // and seven methods. Original FB748/FB75D/FB796 dispatch these method slots.
    // Validate the complete method group and printable name before discovering it.
    if(function.instructions.size()==2) {
        const auto& first=function.instructions.begin()->second;
        const auto& last=function.instructions.rbegin()->second;
        if(first.decoded.mnemonic==ZYDIS_MNEMONIC_MOV && first.operands[0].type==ZYDIS_OPERAND_TYPE_REGISTER &&
            first.operands[0].reg.value==ZYDIS_REGISTER_EAX && first.operands[1].type==ZYDIS_OPERAND_TYPE_IMMEDIATE &&
            last.decoded.mnemonic==ZYDIS_MNEMONIC_RET && last.decoded.operand_count_visible==0) try {
            const auto record=image.data(static_cast<std::uint32_t>(first.operands[1].imm.value.u),32);
            const auto name=image.data(u32(record,0),64);
            const auto end=std::ranges::find(name,std::byte{0});
            const bool printable=end!=name.begin() && end!=name.end() && std::all_of(name.begin(),end,[](std::byte c) {
                return c>=std::byte{32} && c<=std::byte{126};
            });
            std::vector<std::uint32_t> methods;
            bool valid=printable && is_code(u32(record,4));
            for(unsigned i=1;i<=7;++i) {
                const auto method=u32(record,i*4);
                valid &= !method || is_code(method);if(method) methods.push_back(method);
            }
            if(valid && methods.size()>=2) for(auto method:methods) add_callback(method);
        } catch(const std::exception&) {}
    }
    // F2930 stores arguments 3..5 as plugin callbacks (F2A66..F2AA5).
    // Recognize the original engine/object registrar wrappers by their complete
    // forwarding sequence, not just their addresses. Their callbacks are 2..3
    // and 2..4 respectively. The registry pointer varies between object types.
    struct CallbackApi {unsigned first=0,count=0;int incoming_register=-1,record_argument=-1;std::uint32_t record_offset=4;};
    const auto callback_api=[&](std::uint32_t target)->CallbackApi {
        if(target==0x000F2930) return {3,6};
        // 10C650 takes its request as argument 4 and forwards request+4 as
        // argument 4 of 109EC0 (10C6BE/10C6C8/10C6D1 -> 109FD7).
        if(target==0x0010C650U) {
            constexpr std::string_view prefix="83EC18535556578B7C243C33DB33ED";
            if(hex_bytes(image.data(target,prefix.size()/2))!=prefix)
                throw std::runtime_error("Resource request consumer differs from its verified ABI");
            return {0,0,-1,4};
        }
        // Verified original consumers: reverse array iteration (134F2), linked
        // objects (FD837), list iteration (10D177/10D1AA), and constructor pairs
        // (10A22D/10A263), and descriptor allocation (109FD7). The constructor
        // 8FAE5 and 9059E consume argument 2 in the resource-array helpers.
        // The constructor iterator uses incoming EBX as its begin callback; the allocator reads
        // argument 4 into EBP at 109FA7 and passes argument 5 as its context.
        struct Consumer {std::uint32_t address;std::string_view prefix;CallbackApi callbacks;};
        constexpr Consumer consumers[]={
            // The stream registrar stores arguments 3..5 in plugin fields
            // +0Ch/+10h/+14h. F6DB0 invokes the read callback at F6EAD.
            {0x000F6D70,"8B4424048B401085C07411",{2,5}},
            {0x00215880,"8B4424048B0DB8562200898188190000C20400",{0,1}},
            {0x000E68C4,"558BEC8B450C85C07505A130010100",{2,3}},
            // 112930 stores argument 2 as +4 of a thread record. 112960
            // invokes that field; 112980 passes 112960 to CreateThread.
            {0x00112930,"8B4424048B4C24088B54240C894804",{1,2}},
            // File-worker jobs install completion callbacks at +20h. The
            // worker calls them with handle, byte count, error and context.
            {0x001107C0,"565768987B5A00E894E0FFFF",{2,3}},
            {0x00110870,"56578B7C240C686C7B5A00",{3,4}},
            {0x001108C0,"56578B7C240C686C7B5A00",{2,3}},
            // This resource wrapper stores argument 6 in its context record
            // and submits 117240, which tail-calls that stored completion.
            {0x00117260,"8B44240C8B4C2410568B7424088906",{5,6}},
            // Stream-manager iterators invoke argument 2 for each source.
            {0x00110C90,"518B4C240853558B6920",{1,2}},
            {0x00110D10,"8B442404538B5C240C",{1,2}},
            // Media descriptors carry their completion method at +3Ch;
            // 11D730 copies it to +D8h, called by 11CA40 at 11D2E2.
            {0x0011D730,"5333DB55568B742410899E50010000",{0,0,-1,1,0x3C}},
            // The UI value constructor stores argument 8 at +48h, consumed
            // by C1210. Callers may select its method through a switch.
            {0x0001D180,"D944240C8B442420D84C24148B542414894148",{7,8}},
            {0x000134D0,"8B44240C487826538B5C2414",{3,4}},
            {0x000FD810,"535556578B7C24148B4708",{1,2}},
            {0x0008F3D0,"558BEC83E4F081EC44010000",{2,3}},
            {0x00090420,"8B411C8B512081EC7C010000",{2,3}},
            {0x0010D150,"5355568B74241085F67506",{2,3}},
            // Resource release forwards argument 2 through the descriptor's
            // release method and to 10D0C0, which invokes it at 10D10D.
            {0x00109DE0,"538B5C24088B038B4034558B6C241056",{1,2}},
            {0x0010D0C0,"8B5424048B0A8B4148",{1,2}},
            {0x00112BB0,"538B5C240855568B3333ED",{1,2}},
            {0x00109EC0,"81EC10020000A1B07E340033842410020000",{4,5}},
            {0x00109A90,"568B35BC6E5A0081FEBC6E5A00",{0,1}},
            {0x0010A210,"558B6C240C5633F685ED577621",{2,3,3}}
        };
        for(const auto& consumer:consumers) if(target==consumer.address) {
            if(hex_bytes(image.data(target,consumer.prefix.size()/2))!=consumer.prefix)
                throw std::runtime_error("Callback consumer prefix differs from its verified ABI");
            return consumer.callbacks;
        }
        constexpr std::string_view object="8B4424148B4C24108B54240C508B44240C518B4C240C52505168";
        constexpr std::string_view engine="8B4424108B4C240C8B5424086A00508B44240C51525068";
        constexpr std::string_view request="8B4424108B4C240C8B542408508B44240851525068";
        constexpr std::string_view allocator="8B4424148B4C24108B542408508B442410518B4C240C52505168";
        try {
            const auto bytes=image.data(target,39);
            if(hex_bytes(Bytes(bytes).first(26))==allocator && bytes[30]==std::byte{0xE8} &&
               target+35+u32(bytes,31)==0x00109EC0U && hex_bytes(Bytes(bytes).subspan(35,4))=="83C418C3")
                return {3,4};
            if(hex_bytes(Bytes(bytes).first(21))==request && bytes[25]==std::byte{0xE8} &&
                hex_bytes(Bytes(bytes).subspan(30,4))=="83C414C3") {
                const auto registrar=target+30+u32(bytes,26);
                if(registrar==0x0010C650U) return {0,0,-1,3};
                if(registrar==0x000F6D70U) return {1,4};
            }
            const auto prefix=hex_bytes(Bytes(bytes).first(26));
            const auto length=prefix==object?26U:prefix.starts_with(engine)?23U:0U;
            if(length && bytes[length+4]==std::byte{0xE8} &&
                target+length+9+u32(bytes,length+5)==0x000F2930 &&
                hex_bytes(Bytes(bytes).subspan(length+9,4))=="83C418C3") return {2,length==26?5U:4U};
        } catch(const std::exception&) {}
        return {};
    };
    std::map<std::uint32_t,std::vector<std::uint32_t>> predecessors;
    const auto register_callbacks=[&](std::uint32_t at,int reg) {
        if(reg<0) return;
        if(predecessors.empty()) for(const auto& [address,value]:function.instructions) {
            const auto edge=[&](std::uint32_t target) {
                if(function.instructions.contains(target)) predecessors[target].push_back(address);
            };
            const auto kind=value.decoded.meta.category;
            if(kind==ZYDIS_CATEGORY_UNCOND_BR || kind==ZYDIS_CATEGORY_COND_BR) {
                if(value.target()) edge(*value.target());
                else for(auto target:value.jump_targets) edge(target);
            }
            if(kind!=ZYDIS_CATEGORY_UNCOND_BR && kind!=ZYDIS_CATEGORY_RET && kind!=ZYDIS_CATEGORY_INTERRUPT &&
               value.decoded.mnemonic!=ZYDIS_MNEMONIC_INT3 && value.decoded.mnemonic!=ZYDIS_MNEMONIC_HLT)
                edge(address+value.decoded.length);
        }
        std::vector<std::pair<std::uint32_t,int>> pending{{at,reg}};
        std::set<std::pair<std::uint32_t,int>> seen;
        while(!pending.empty() && seen.size()<4096) {
            const auto [address,incoming]=pending.back();pending.pop_back();
            if(!seen.emplace(address,incoming).second) continue;
            for(auto origin:predecessors[address]) {
                const auto& value=function.instructions.at(origin);auto source=incoming;
                if(value.decoded.meta.category==ZYDIS_CATEGORY_CALL) {
                    // This verified queue factory preserves EBX/EBP/ESI/EDI.
                    // Other calls remain barriers until their ABI is proved.
                    if(value.target()!=0x000C14A0U || incoming==0 || incoming==1 || incoming==2 || incoming==4) continue;
                    if(!matches_bytes(0x000C14A0,"8B513C568B742408C1E60403F28D460450E8EA8801008B4C241083C00883C4048970448948405EC20800"))
                        throw std::runtime_error("UI queue factory differs from its verified ABI");
                } else if(std::ranges::any_of(std::span(value.operands).first(value.decoded.operand_count),[&](const auto& operand) {
                    return operand.type==ZYDIS_OPERAND_TYPE_REGISTER && general(operand.reg.value)==incoming &&
                        (operand.actions&ZYDIS_OPERAND_ACTION_MASK_WRITE);
                })) {
                    const auto& output=value.operands[0];const auto& input=value.operands[1];
                    if(value.decoded.mnemonic!=ZYDIS_MNEMONIC_MOV || output.type!=ZYDIS_OPERAND_TYPE_REGISTER ||
                       output.size!=32 || general(output.reg.value)!=incoming) continue;
                    if(input.type==ZYDIS_OPERAND_TYPE_IMMEDIATE) {add_callback(static_cast<std::uint32_t>(input.imm.value.u));continue;}
                    if(input.type==ZYDIS_OPERAND_TYPE_MEMORY && input.size==32 && input.mem.segment==ZYDIS_REGISTER_DS &&
                       input.mem.base==ZYDIS_REGISTER_NONE && input.mem.index!=ZYDIS_REGISTER_NONE && input.mem.scale==4) {
                        add_vtable(static_cast<std::uint32_t>(input.mem.disp.value),0,true);continue;
                    }
                    if(input.type!=ZYDIS_OPERAND_TYPE_REGISTER || input.size!=32) continue;
                    source=general(input.reg.value);if(source<0) continue;
                }
                pending.emplace_back(origin,source);
            }
        }
    };
    // UI producers also install methods through registers (36670 -> 366AD).
    // Follow their real origins with the same bounded control-flow walk.
    for(const auto& [pc,reg]:ui_callback_registers) register_callbacks(pc,reg);
    // A called indexed load proves that a file-backed pointer array is a
    // dispatch table. Trace its register through the actual branch predecessors.
    for(const auto& [pc,instruction]:function.instructions) {
        if(instruction.decoded.meta.category!=ZYDIS_CATEGORY_CALL || instruction.target() || instruction.kernel_ordinal) continue;
        const auto& method=instruction.operands[0];
        if(method.type==ZYDIS_OPERAND_TYPE_REGISTER && method.size==32) register_callbacks(pc,general(method.reg.value));
        else if(method.type==ZYDIS_OPERAND_TYPE_MEMORY && method.size==32 && method.mem.segment==ZYDIS_REGISTER_DS &&
                method.mem.base==ZYDIS_REGISTER_NONE && method.mem.index!=ZYDIS_REGISTER_NONE && method.mem.scale==4)
            add_vtable(static_cast<std::uint32_t>(method.mem.disp.value),0,true);
    }
    for(const auto& [pc,instruction]:function.instructions) {
        if(instruction.decoded.meta.category!=ZYDIS_CATEGORY_CALL || !instruction.target()) continue;
        if(instruction.target()==0x000E67A7U) {
            // XapiRegisterThreadNotifyRoutine links an eight-byte LIST_ENTRY
            // followed by its callback. E6690 walks that list and calls +8.
            constexpr std::string_view registrar="56BE382C340056FF15703D2900837C240C00";
            if(hex_bytes(image.data(0x000E67A7,registrar.size()/2))!=registrar ||
               hex_bytes(image.data(0x000E66AC,11))!="FF7424108BC68B36FF5008")
                throw std::runtime_error("Thread notification registrar differs from its verified ABI");
            std::array<std::optional<std::uint32_t>,2> arguments;
            const auto* value=previous(function,pc);unsigned argument=0;
            for(unsigned distance=0;value && distance<32 && argument<arguments.size();++distance) {
                if(value->decoded.mnemonic==ZYDIS_MNEMONIC_PUSH && value->decoded.operand_width==32) {
                    if(value->operands[0].type==ZYDIS_OPERAND_TYPE_IMMEDIATE)
                        arguments[argument]=static_cast<std::uint32_t>(value->operands[0].imm.value.u);
                    ++argument;
                } else {
                    const auto kind=value->decoded.meta.category;
                    if(kind==ZYDIS_CATEGORY_CALL || kind==ZYDIS_CATEGORY_UNCOND_BR || kind==ZYDIS_CATEGORY_RET ||
                       std::ranges::any_of(std::span(value->operands).first(value->decoded.operand_count),[](const auto& operand) {
                           return operand.type==ZYDIS_OPERAND_TYPE_REGISTER && general(operand.reg.value)==4 &&
                               (operand.actions&ZYDIS_OPERAND_ACTION_MASK_WRITE);
                       })) break;
                }
                value=previous(function,value->address);
            }
            if(arguments[0] && arguments[1] && *arguments[1]) {
                const auto field=*arguments[0]+8;
                bool written=false;value=previous(function,pc);
                for(unsigned distance=0;value && distance<64;++distance) {
                    const auto& output=value->operands[0];const auto& input=value->operands[1];
                    if(absolute_slot(output) && output.mem.disp.value==field &&
                       (output.actions&ZYDIS_OPERAND_ACTION_MASK_WRITE)) {
                        if(value->decoded.mnemonic==ZYDIS_MNEMONIC_MOV && input.type==ZYDIS_OPERAND_TYPE_IMMEDIATE)
                            add_callback(static_cast<std::uint32_t>(input.imm.value.u));
                        written=true;break;
                    }
                    const auto kind=value->decoded.meta.category;
                    if(kind==ZYDIS_CATEGORY_CALL || kind==ZYDIS_CATEGORY_UNCOND_BR || kind==ZYDIS_CATEGORY_RET) break;
                    value=previous(function,value->address);
                }
                if(!written) try {add_callback(u32(image.data(field,4),0));} catch(const std::exception&) {}
            }
        }
        if(instruction.target()==0x0010D4F0U) {
            // The resource registrar takes (descriptor, table, count, table,
            // count). Its loops at 10D560/10D5B0 use 24-byte records and
            // install the method at +14h, defaulting null methods to ED200.
            // Follow literal table/count pairs, not arbitrary data pointers.
            constexpr std::string_view prefix="538B5C240C85DB5556577554";
            if(hex_bytes(image.data(0x0010D4F0,prefix.size()/2))!=prefix ||
                hex_bytes(image.data(0x0010D560,9))!="0FB7C78D14408D34D3" ||
                hex_bytes(image.data(0x0010D57D,14))!="8B461485C07507C7461400D20E00" ||
                hex_bytes(image.data(0x0010D5B0,13))!="8B4C24200FB7C38D04408D3CC1")
                throw std::runtime_error("Resource method registrar differs from its verified ABI");
            std::array<std::optional<std::uint32_t>,5> arguments;
            const auto* value=previous(function,pc);unsigned argument=0;
            for(unsigned distance=0;value && distance<32 && argument<arguments.size();++distance) {
                if(value->decoded.mnemonic==ZYDIS_MNEMONIC_PUSH && value->decoded.operand_width==32) {
                    if(value->operands[0].type==ZYDIS_OPERAND_TYPE_IMMEDIATE)
                        arguments[argument]=static_cast<std::uint32_t>(value->operands[0].imm.value.u);
                    ++argument;
                } else {
                    const auto kind=value->decoded.meta.category;
                    if(kind==ZYDIS_CATEGORY_CALL || kind==ZYDIS_CATEGORY_UNCOND_BR || kind==ZYDIS_CATEGORY_RET ||
                        std::ranges::any_of(std::span(value->operands).first(value->decoded.operand_count),[](const auto& operand) {
                            return operand.type==ZYDIS_OPERAND_TYPE_REGISTER && general(operand.reg.value)==4 &&
                                (operand.actions&ZYDIS_OPERAND_ACTION_MASK_WRITE);
                        })) break;
                }
                value=previous(function,value->address);
            }
            for(unsigned pair:{1U,3U}) {
                if(!arguments[pair] || !arguments[pair+1] || !*arguments[pair]) continue;
                const auto count=*arguments[pair+1]&0xFFFFU;
                if(!count || count>256) continue;
                try {
                    const auto records=image.data(*arguments[pair],count*24);
                    bool valid=true;
                    for(unsigned record=0;record<count;++record) {
                        const auto method=u32(records,record*24+20);
                        valid &= !method || is_code(method);
                    }
                    if(valid) for(unsigned record=0;record<count;++record) {
                        const auto method=u32(records,record*24+20);
                        add_callback(method?method:0x000ED200U);
                    }
                } catch(const std::exception&) {}
            }
        }
        const auto [first,count,callback_register,record_argument,record_offset]=callback_api(*instruction.target());
        if(record_argument>=0) {
            // Follow this particular stack argument back to a local request,
            // then find the last write to its callback field. The verified
            // name lookup 10C030 has no callee stack cleanup; CB47F..CB490
            // keeps the already-pushed request across that lookup.
            auto slot=std::int64_t(record_argument)*4;int source_register=-1;bool field=false;
            const auto* origin=previous(function,pc);
            for(unsigned distance=0;origin && distance<64;++distance) {
                const auto kind=origin->decoded.meta.category;
                if(kind==ZYDIS_CATEGORY_UNCOND_BR || kind==ZYDIS_CATEGORY_RET) break;
                if(kind==ZYDIS_CATEGORY_CALL) {
                    if(origin->target()==0x0010C030U) {
                        if(source_register>=0 && source_register!=3 && source_register<5) break;
                        constexpr std::string_view lookup="538B5C240885DB750433C05BC3";
                        if(hex_bytes(image.data(0x0010C030,lookup.size()/2))!=lookup)
                            throw std::runtime_error("Resource name lookup differs from its verified ABI");
                    } else if(field && record_offset==0x3C && (origin->target()==0x0011B430U || origin->target()==0x0011B730U)) {
                        // Both cdecl helpers initialize other media objects;
                        // the caller retains its local descriptor and stack.
                        const auto prefix=origin->target()==0x0011B430U?"8B44240C8B48045657":"8B4424048B54240832C9";
                        if(hex_bytes(image.data(*origin->target(),std::string_view(prefix).size()/2))!=prefix)
                            throw std::runtime_error("Media descriptor preparation differs from its verified ABI");
                    } else break;
                    origin=previous(function,origin->address);continue;
                }
                const auto& output=origin->operands[0];const auto& input=origin->operands[1];
                const bool stack_memory=output.type==ZYDIS_OPERAND_TYPE_MEMORY && output.size==32 &&
                    output.mem.base==ZYDIS_REGISTER_ESP && output.mem.index==ZYDIS_REGISTER_NONE &&
                    output.mem.segment==ZYDIS_REGISTER_SS &&
                    output.mem.disp.value==slot;
                if(field && stack_memory && (output.actions&ZYDIS_OPERAND_ACTION_MASK_WRITE)) {
                    if(origin->decoded.mnemonic==ZYDIS_MNEMONIC_MOV && input.type==ZYDIS_OPERAND_TYPE_IMMEDIATE)
                        add_callback(static_cast<std::uint32_t>(input.imm.value.u));
                    break;
                }
                if(!field && source_register<0 &&
                    ((origin->decoded.mnemonic==ZYDIS_MNEMONIC_PUSH && origin->decoded.operand_width==32 && slot==0) ||
                     (origin->decoded.mnemonic==ZYDIS_MNEMONIC_MOV && stack_memory))) {
                    const auto& source=origin->decoded.mnemonic==ZYDIS_MNEMONIC_PUSH?output:input;
                    if(source.type==ZYDIS_OPERAND_TYPE_REGISTER && source.size==32) source_register=general(source.reg.value);
                    else if(source.type==ZYDIS_OPERAND_TYPE_IMMEDIATE) {
                        try {add_callback(u32(image.data(static_cast<std::uint32_t>(source.imm.value.u)+record_offset,4),0));}
                        catch(const std::exception&) {}
                        break;
                    } else break;
                } else if(!field && source_register>=0 && std::ranges::any_of(
                    std::span(origin->operands).first(origin->decoded.operand_count),[&](const auto& operand) {
                        return operand.type==ZYDIS_OPERAND_TYPE_REGISTER && general(operand.reg.value)==source_register &&
                            (operand.actions&ZYDIS_OPERAND_ACTION_MASK_WRITE);
                    })) {
                    if(output.type!=ZYDIS_OPERAND_TYPE_REGISTER || output.size!=32 || general(output.reg.value)!=source_register) break;
                    if(origin->decoded.mnemonic==ZYDIS_MNEMONIC_LEA && input.type==ZYDIS_OPERAND_TYPE_MEMORY &&
                        input.mem.base==ZYDIS_REGISTER_ESP && input.mem.index==ZYDIS_REGISTER_NONE) {
                        slot=input.mem.disp.value+record_offset;field=true;source_register=-1;
                    } else if(origin->decoded.mnemonic==ZYDIS_MNEMONIC_MOV && input.type==ZYDIS_OPERAND_TYPE_REGISTER && input.size==32)
                        source_register=general(input.reg.value);
                    else break;
                }
                if(field || source_register<0) {
                    if(origin->decoded.mnemonic==ZYDIS_MNEMONIC_PUSH && origin->decoded.operand_width==32) slot-=4;
                    else if((origin->decoded.mnemonic==ZYDIS_MNEMONIC_ADD || origin->decoded.mnemonic==ZYDIS_MNEMONIC_SUB) &&
                        output.type==ZYDIS_OPERAND_TYPE_REGISTER && output.reg.value==ZYDIS_REGISTER_ESP && input.type==ZYDIS_OPERAND_TYPE_IMMEDIATE)
                        slot+=(origin->decoded.mnemonic==ZYDIS_MNEMONIC_ADD?1:-1)*input.imm.value.s;
                    else if(std::ranges::any_of(std::span(origin->operands).first(origin->decoded.operand_count),[](const auto& operand) {
                        return operand.type==ZYDIS_OPERAND_TYPE_REGISTER && general(operand.reg.value)==4 &&
                            (operand.actions&ZYDIS_OPERAND_ACTION_MASK_WRITE);
                    })) break;
                    if(slot<0 || slot>65536) break;
                }
                origin=previous(function,origin->address);
            }
        }
        if(!count && callback_register<0) continue;
        const auto* value=previous(function,pc);unsigned argument=0;std::uint64_t assigned=0;
        const auto stack_callback=[&](unsigned slot,const Instruction& origin,const ZydisDecodedOperand* source) {
            if(slot<first || slot>=count || (assigned&(std::uint64_t{1}<<slot))) return;
            assigned|=std::uint64_t{1}<<slot;
            if(!source) return;
            if(source->type==ZYDIS_OPERAND_TYPE_IMMEDIATE) add_callback(static_cast<std::uint32_t>(source->imm.value.u));
            else if(source->type==ZYDIS_OPERAND_TYPE_REGISTER && source->size==32)
                register_callbacks(origin.address,general(source->reg.value));
        };
        // Original geometry callers interleave long vector calculations between
        // argument pushes (B2530 -> 90420). Bound the local scan while retaining
        // the same verified consumers and stack/control-flow barriers.
        for(unsigned distance=0;value && distance<128 && argument<count;++distance) {
            if(value->decoded.mnemonic==ZYDIS_MNEMONIC_PUSH && value->decoded.operand_width==32) {
                stack_callback(argument,*value,&value->operands[0]);
                ++argument;
            } else {
                const auto kind=value->decoded.meta.category;
                if(kind==ZYDIS_CATEGORY_CALL || kind==ZYDIS_CATEGORY_UNCOND_BR || kind==ZYDIS_CATEGORY_RET) break;
                const auto& output=value->operands[0];const auto& input=value->operands[1];
                if((value->decoded.mnemonic==ZYDIS_MNEMONIC_ADD || value->decoded.mnemonic==ZYDIS_MNEMONIC_SUB) &&
                   output.type==ZYDIS_OPERAND_TYPE_REGISTER && output.reg.value==ZYDIS_REGISTER_ESP &&
                   input.type==ZYDIS_OPERAND_TYPE_IMMEDIATE && input.imm.value.u<=4096 && !(input.imm.value.u&3)) {
                    const auto words=static_cast<unsigned>(input.imm.value.u/4);
                    if(value->decoded.mnemonic==ZYDIS_MNEMONIC_ADD) {if(words>argument) break;argument-=words;}
                    else argument+=words;
                } else if(output.type==ZYDIS_OPERAND_TYPE_MEMORY && output.size==32 && output.mem.base==ZYDIS_REGISTER_ESP &&
                          output.mem.index==ZYDIS_REGISTER_NONE && output.mem.disp.value>=0 && output.mem.disp.value<=512 &&
                          !(output.mem.disp.value&3) && (output.actions&ZYDIS_OPERAND_ACTION_MASK_WRITE)) {
                    stack_callback(argument+static_cast<unsigned>(output.mem.disp.value/4),*value,
                        value->decoded.mnemonic==ZYDIS_MNEMONIC_MOV?&input:nullptr);
                } else if(std::ranges::any_of(std::span(value->operands).first(value->decoded.operand_count),[](const auto& operand) {
                    return operand.type==ZYDIS_OPERAND_TYPE_REGISTER && general(operand.reg.value)==4 &&
                        (operand.actions&ZYDIS_OPERAND_ACTION_MASK_WRITE);
                })) break;
            }
            value=previous(function,value->address);
        }
        register_callbacks(pc,callback_register);
    }
    // Source-action initializers bind a three-word {object, method, context}
    // record through 11B620. Specialized sources replace its +4h method.
    const bool media_action=function.entry==0x0011B620U || std::ranges::any_of(function.instructions,[](const auto& item) {
        return item.second.decoded.meta.category==ZYDIS_CATEGORY_CALL && item.second.target()==0x0011B620U;
    });
    if(media_action) {
        if(hex_bytes(image.data(0x0011B620,7))!="8B4C240C8B5104" ||
           hex_bytes(image.data(0x0011B630,7))!="C74204E0870D00" ||
           hex_bytes(image.data(0x0011B650,14))!="8B4424048B48088B105152FF5004")
            throw std::runtime_error("Media source action differs from its verified ABI");
        for(const auto& [pc,instruction]:function.instructions) {
            const auto& output=instruction.operands[0];const auto& input=instruction.operands[1];
            if(instruction.decoded.mnemonic==ZYDIS_MNEMONIC_MOV && output.type==ZYDIS_OPERAND_TYPE_MEMORY && output.size==32 &&
               output.mem.segment==ZYDIS_REGISTER_DS && output.mem.base!=ZYDIS_REGISTER_NONE &&
               output.mem.base!=ZYDIS_REGISTER_ESP && output.mem.base!=ZYDIS_REGISTER_EBP &&
               output.mem.index==ZYDIS_REGISTER_NONE && output.mem.disp.value==4 && input.type==ZYDIS_OPERAND_TYPE_IMMEDIATE)
                add_callback(static_cast<std::uint32_t>(input.imm.value.u));
        }
    }
    // Media queue producers install {method, context} notification records.
    // Follow the paired writes only when the verified queue initializer is
    // present, rather than classifying arbitrary object words as callbacks.
    const bool media_queue=std::ranges::any_of(function.instructions,[](const auto& item) {
        return item.second.decoded.meta.category==ZYDIS_CATEGORY_CALL && item.second.target()==0x0011B730U;
    });
    const bool source_close_record=function.entry==0x00111E00U || std::ranges::any_of(function.instructions,[](const auto& item) {
        return item.second.decoded.meta.category==ZYDIS_CATEGORY_CALL && item.second.target()==0x00111E00U;
    });
    if(media_queue || function.entry==0x00111710U || source_close_record) {
        constexpr std::string_view initializer="8B4424048B54240832C98848018848028808";
        if(hex_bytes(image.data(0x0011B730,initializer.size()/2))!=initializer)
            throw std::runtime_error("Media queue initializer differs from its verified ABI");
        // The stream manager's constructor installs its observer tuple at
        // +28h/+2Ch; 11B430 binds it and 11B4B0 invokes it on completion.
        if(function.entry==0x00111710U &&
           (hex_bytes(image.data(function.entry,10))!="568B7424085733FF5756" ||
            hex_bytes(image.data(0x001117AA,10))!="C74628600C110089762C"))
            throw std::runtime_error("Media manager observer differs from its verified ABI");
        // 111E00 writes context before method in a source-close descriptor.
        // The source constructor copies +24h/+28h into +27Ch/+280h, which
        // 1155C0 invokes when the stream finishes closing.
        if(source_close_record &&
           (!matches_bytes(0x00111E00,"89511C8B501056895118894128C74124F0151100") ||
            !matches_bytes(0x00115A1E,"8B96800200005255FFD0")))
            throw std::runtime_error("Media source close record differs from its verified ABI");
        for(auto it=function.instructions.begin();it!=function.instructions.end();++it) {
            const auto& value=it->second;const auto& output=value.operands[0];const auto& input=value.operands[1];
            if(value.decoded.mnemonic!=ZYDIS_MNEMONIC_MOV || output.type!=ZYDIS_OPERAND_TYPE_MEMORY || output.size!=32 ||
               output.mem.base==ZYDIS_REGISTER_NONE || output.mem.base==ZYDIS_REGISTER_ESP || output.mem.base==ZYDIS_REGISTER_EBP ||
               output.mem.index!=ZYDIS_REGISTER_NONE || input.type!=ZYDIS_OPERAND_TYPE_IMMEDIATE) continue;
            const auto context_matches=[&](const Instruction& context) {
                const auto& field=context.operands[0];
                return context.decoded.mnemonic==ZYDIS_MNEMONIC_MOV && field.type==ZYDIS_OPERAND_TYPE_MEMORY && field.size==32 &&
                    field.mem.base==output.mem.base && field.mem.segment==output.mem.segment && field.mem.index==ZYDIS_REGISTER_NONE &&
                    field.mem.disp.value==output.mem.disp.value+4 && context.operands[1].type==ZYDIS_OPERAND_TYPE_REGISTER;
            };
            const auto next=function.instructions.find(value.address+value.decoded.length);
            const auto* prior=previous(function,value.address);
            if((next!=function.instructions.end() && context_matches(next->second)) || (prior && context_matches(*prior)))
                add_callback(static_cast<std::uint32_t>(input.imm.value.u));
        }
    }
    // Stream constructors also build indexed {method, context} arrays. Follow
    // the complete pair: both stores must use the same array and byte offset.
    // The context comes from a separate array and is adjusted by that offset.
    const bool stream_constructor=std::ranges::any_of(function.instructions,[](const auto& item) {
        return item.second.decoded.meta.category==ZYDIS_CATEGORY_CALL && item.second.target()==0x0011B430U;
    });
    if(stream_constructor) for(const auto& [pc,instruction]:function.instructions) {
        const auto& output=instruction.operands[0];const auto& input=instruction.operands[1];
        if(instruction.decoded.mnemonic!=ZYDIS_MNEMONIC_MOV || output.type!=ZYDIS_OPERAND_TYPE_MEMORY || output.size!=32 ||
           output.mem.segment!=ZYDIS_REGISTER_DS || output.mem.base==ZYDIS_REGISTER_NONE || output.mem.index==ZYDIS_REGISTER_NONE ||
           output.mem.scale!=1 || output.mem.disp.value!=0 || input.type!=ZYDIS_OPERAND_TYPE_IMMEDIATE ||
           !is_code(static_cast<std::uint32_t>(input.imm.value.u))) continue;
        const auto* array=previous(function,pc);
        const auto next=[&](const Instruction* value)->const Instruction* {
            if(!value) return nullptr;
            const auto found=function.instructions.find(value->address+value->decoded.length);
            return found==function.instructions.end()?nullptr:&found->second;
        };
        const auto load=[](const Instruction* value) {
            return value && value->decoded.mnemonic==ZYDIS_MNEMONIC_MOV &&
                value->operands[0].type==ZYDIS_OPERAND_TYPE_REGISTER && value->operands[0].size==32 &&
                value->operands[1].type==ZYDIS_OPERAND_TYPE_MEMORY && value->operands[1].size==32 &&
                value->operands[1].mem.segment==ZYDIS_REGISTER_DS && value->operands[1].mem.base!=ZYDIS_REGISTER_NONE &&
                value->operands[1].mem.index==ZYDIS_REGISTER_NONE;
        };
        const auto* context=next(&instruction);const auto* array_again=next(context);
        const auto* adjust=next(array_again);const auto* store=next(adjust);
        if(!load(array) || !load(context) || !load(array_again) || !adjust || !store) continue;
        const auto offset=output.mem.index==array->operands[0].reg.value?output.mem.base:
            output.mem.base==array->operands[0].reg.value?output.mem.index:ZYDIS_REGISTER_NONE;
        if(offset==ZYDIS_REGISTER_NONE || array->operands[0].reg.value==array->operands[1].mem.base ||
           context->operands[0].reg.value==offset || context->operands[0].reg.value==array->operands[1].mem.base ||
           array_again->operands[0].reg.value==offset || array_again->operands[0].reg.value==context->operands[0].reg.value ||
           context->operands[1].mem.base!=array->operands[1].mem.base ||
           context->operands[1].mem.disp.value==array->operands[1].mem.disp.value ||
           array_again->operands[1].mem.base!=array->operands[1].mem.base ||
           array_again->operands[1].mem.disp.value!=array->operands[1].mem.disp.value ||
           adjust->decoded.mnemonic!=ZYDIS_MNEMONIC_ADD || adjust->operands[0].type!=ZYDIS_OPERAND_TYPE_REGISTER ||
           adjust->operands[0].reg.value!=context->operands[0].reg.value || adjust->operands[1].type!=ZYDIS_OPERAND_TYPE_REGISTER ||
           adjust->operands[1].reg.value!=offset) continue;
        const auto& field=store->operands[0];const auto& value=store->operands[1];
        const auto pointer=array_again->operands[0].reg.value;
        if(store->decoded.mnemonic!=ZYDIS_MNEMONIC_MOV || field.type!=ZYDIS_OPERAND_TYPE_MEMORY || field.size!=32 ||
           field.mem.segment!=ZYDIS_REGISTER_DS || field.mem.scale!=1 || field.mem.disp.value!=4 ||
           !((field.mem.base==offset && field.mem.index==pointer) || (field.mem.base==pointer && field.mem.index==offset)) ||
           value.type!=ZYDIS_OPERAND_TYPE_REGISTER || value.reg.value!=context->operands[0].reg.value) continue;
        if(!matches_bytes(0x0011B430,"8B44240C8B480456578B7C240C8B7720") ||
           !matches_bytes(0x0011583F,"8B481085C9740A8B51045250FF1183C408"))
            throw std::runtime_error("Indexed stream completion differs from its verified ABI");
        add_callback(static_cast<std::uint32_t>(input.imm.value.u));
    }
    std::ranges::sort(function.static_callbacks);
    function.static_callbacks.erase(std::unique(function.static_callbacks.begin(),function.static_callbacks.end()),function.static_callbacks.end());
    return function;
}

std::string analyse(Xbe& image, std::uint32_t address, std::uint32_t instruction_budget) {
    const auto function = recover(image, address, instruction_budget);
    std::ostringstream out;
    out << "{\"format\":\"b2-analysis-v1\",\"sha256\":" << json(image.sha256)
        << ",\"entry\":" << json(hex32(address)) << ",\"instruction_count\":" << function.instructions.size()
        << ",\"budget_exhausted\":" << (function.budget_exhausted ? "true" : "false")
        << ",\"unresolved_control_flow\":" << (function.unresolved ? "true" : "false") << ",\"instructions\":[";
    bool first = true;
    for (const auto& [pc, instruction] : function.instructions) {
        if (!first) out << ',';
        first = false;
        out << instruction.report();
    }
    out << "],\"edges\":[";
    first = true;
    for (const auto& edge : function.edges) {
        if (!first) out << ',';
        first = false;
        out << "{\"from\":" << json(hex32(edge.from)) << ",\"kind\":" << json(edge.kind)
            << ",\"to\":" << (edge.to ? json(hex32(*edge.to)) : "null") << '}';
    }
    out << "],\"static_callbacks\":[";
    for(std::size_t i=0;i<function.static_callbacks.size();++i) {if(i) out<<',';out<<json(hex32(function.static_callbacks[i]));}
    return out.str() + "]}";
}
}
