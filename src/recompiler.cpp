#include "recompiler.h"
#include "native_api.h"
#include "floating.h"
#include <algorithm>
#include <format>
#include <set>
#include <sstream>
#include <stdexcept>

namespace b2 {
namespace {
constexpr const char* registers[] = {"eax", "ecx", "edx", "ebx", "esp", "ebp", "esi", "edi"};
struct RegisterPart { unsigned index, bits, shift; };
RegisterPart reg(ZydisRegister value) {
    constexpr ZydisRegister full[] = {ZYDIS_REGISTER_EAX, ZYDIS_REGISTER_ECX, ZYDIS_REGISTER_EDX, ZYDIS_REGISTER_EBX,
                                      ZYDIS_REGISTER_ESP, ZYDIS_REGISTER_EBP, ZYDIS_REGISTER_ESI, ZYDIS_REGISTER_EDI};
    constexpr ZydisRegister word[] = {ZYDIS_REGISTER_AX, ZYDIS_REGISTER_CX, ZYDIS_REGISTER_DX, ZYDIS_REGISTER_BX,
                                      ZYDIS_REGISTER_SP, ZYDIS_REGISTER_BP, ZYDIS_REGISTER_SI, ZYDIS_REGISTER_DI};
    constexpr ZydisRegister low[] = {ZYDIS_REGISTER_AL, ZYDIS_REGISTER_CL, ZYDIS_REGISTER_DL, ZYDIS_REGISTER_BL};
    constexpr ZydisRegister high[] = {ZYDIS_REGISTER_AH, ZYDIS_REGISTER_CH, ZYDIS_REGISTER_DH, ZYDIS_REGISTER_BH};
    for (unsigned i = 0; i < 8; ++i) {
        if (value == full[i]) return {i, 32, 0};
        if (value == word[i]) return {i, 16, 0};
        if (i < 4 && value == low[i]) return {i, 8, 0};
        if (i < 4 && value == high[i]) return {i, 8, 8};
    }
    throw std::runtime_error("Unsupported CPU register");
}
std::string constant(std::uint32_t value) { return std::format("0x{:08X}U", value); }
std::string label(std::uint32_t address) { return std::format("i_{:08X}", address); }
std::string type(unsigned bits) {
    if (bits != 8 && bits != 16 && bits != 32) throw std::runtime_error("Unsupported operand width");
    return std::format("std::uint{}_t", bits);
}
bool batchable_x87(const Instruction& instruction) {
    const auto& ins=instruction.decoded;
    if(ins.opcode_map!=ZYDIS_OPCODE_MAP_DEFAULT || ins.opcode<0xD8 || ins.opcode>0xDF || ins.address_width!=32 ||
       (ins.attributes&(ZYDIS_ATTRIB_HAS_LOCK|ZYDIS_ATTRIB_HAS_REP|ZYDIS_ATTRIB_HAS_REPE|ZYDIS_ATTRIB_HAS_REPNE))) return false;
    // Control/environment operations, integer results and CPU flag comparisons
    // remain individual instructions. These operations only affect x87 state.
    constexpr std::string_view allowed[]={"fld","fst","fstp","fild","fist","fistp","fisttp","fadd","faddp",
        "fiadd","fsub","fsubp","fsubr","fsubrp","fisub","fisubr","fmul","fmulp","fimul","fdiv","fdivp",
        "fdivr","fdivrp","fidiv","fidivr","fsqrt","fchs","fabs","fxch","ffree","fld1","fldz",
        "fldl2t","fldl2e","fldpi","fldlg2","fldln2","fcom","fcomp","fcompp","fucom","fucomp","fucompp"};
    if(std::ranges::find(allowed,ZydisMnemonicGetString(ins.mnemonic))==std::end(allowed)) return false;
    for(unsigned i=0;i<ins.operand_count_visible;++i) if(instruction.operands[i].type==ZYDIS_OPERAND_TYPE_MEMORY) {
        const auto& operand=instruction.operands[i];
        if(operand.mem.type!=ZYDIS_MEMOP_TYPE_MEM || !operand.size || operand.size%8) return false;
    }
    return true;
}

class Emitter {
public:
    std::set<unsigned> used, dirty;
    bool uses_flags = false, writes_flags = false;
    bool calls = false;
    bool restores = false;
    bool returns = false;
    bool floating = false;
    std::set<std::uint32_t> loop_headers;
    std::map<std::uint32_t,std::vector<const Instruction*>> floating_runs;
    std::map<std::string,std::string> native_helpers;
    std::map<std::string,std::string> native_declarations;
    std::ostringstream body;

    std::string read_reg(ZydisRegister value) {
        const auto part = reg(value);
        used.insert(part.index);
        std::string result = registers[part.index];
        if (part.shift) result = "(" + result + " >> 8)";
        if (part.bits < 32) result = "(" + result + " & " + constant((1U << part.bits) - 1) + ')';
        return result;
    }
    std::string address(const ZydisDecodedOperand& operand, bool lea = false) {
        if (operand.type != ZYDIS_OPERAND_TYPE_MEMORY) throw std::runtime_error("Expected a memory operand");
        if (operand.mem.type != ZYDIS_MEMOP_TYPE_MEM && !(lea && operand.mem.type == ZYDIS_MEMOP_TYPE_AGEN))
            throw std::runtime_error("Special memory operand is unsupported");
        std::string value = constant(operand.mem.disp.has_displacement ? static_cast<std::uint32_t>(operand.mem.disp.value) : 0);
        if (operand.mem.base != ZYDIS_REGISTER_NONE) value += " + " + read_reg(operand.mem.base);
        if (operand.mem.index != ZYDIS_REGISTER_NONE)
            value += " + " + read_reg(operand.mem.index) + " * " + std::to_string(operand.mem.scale) + 'U';
        if (!lea && operand.mem.segment == ZYDIS_REGISTER_FS) value += " + cpu.fs_base";
        if (!lea && operand.mem.segment == ZYDIS_REGISTER_GS) value += " + cpu.gs_base";
        return "std::uint32_t(" + value + ')';
    }
    std::string read(const ZydisDecodedOperand& operand) {
        switch (operand.type) {
        case ZYDIS_OPERAND_TYPE_REGISTER: return read_reg(operand.reg.value);
        case ZYDIS_OPERAND_TYPE_IMMEDIATE: return constant(static_cast<std::uint32_t>(operand.imm.value.u));
        case ZYDIS_OPERAND_TYPE_MEMORY: return "memory.load<" + type(operand.size) + ">(" + address(operand) + ')';
        default: throw std::runtime_error("Unsupported operand type");
        }
    }
    void write(const ZydisDecodedOperand& operand, const std::string& value) {
        if (operand.type == ZYDIS_OPERAND_TYPE_MEMORY) {
            body << "    memory.store<" << type(operand.size) << ">(" << address(operand)
                 << ", static_cast<" << type(operand.size) << ">(" << value << "));\n";
        } else if (operand.type == ZYDIS_OPERAND_TYPE_REGISTER) {
            const auto part = reg(operand.reg.value);
            used.insert(part.index); dirty.insert(part.index);
            body << "    " << registers[part.index] << " = ";
            if (part.bits == 32) body << value;
            else {
                const auto field = ((1U << part.bits) - 1) << part.shift;
                body << '(' << registers[part.index] << " & " << constant(~field) << ") | ((("
                     << value << ") << " << part.shift << ") & " << constant(field) << ')';
            }
            body << ";\n";
        } else throw std::runtime_error("Unsupported destination operand");
    }
    std::string condition(ZydisMnemonic mnemonic) {
        std::string_view code = ZydisMnemonicGetString(mnemonic);
        if (code.starts_with("cmov")) code.remove_prefix(4);
        else if (code.starts_with("set")) code.remove_prefix(3);
        else if (code.starts_with('j')) code.remove_prefix(1);
        if (code == "ecxz") { used.insert(1); return "ecx == 0"; }
        uses_flags = true;
        if (code == "z") return "(flags & b2::zero) != 0";
        if (code == "nz") return "(flags & b2::zero) == 0";
        if (code == "b") return "(flags & b2::carry) != 0";
        if (code == "nb") return "(flags & b2::carry) == 0";
        if (code == "be") return "(flags & (b2::carry | b2::zero)) != 0";
        if (code == "nbe") return "(flags & (b2::carry | b2::zero)) == 0";
        if (code == "l") return "((flags >> 7) ^ (flags >> 11)) & 1U";
        if (code == "nl") return "(((flags >> 7) ^ (flags >> 11)) & 1U) == 0";
        if (code == "le") return "(flags & b2::zero) || (((flags >> 7) ^ (flags >> 11)) & 1U)";
        if (code == "nle") return "!(flags & b2::zero) && !(((flags >> 7) ^ (flags >> 11)) & 1U)";
        for (const auto& [name, flag] : {std::pair{"o", "overflow"}, {"s", "sign"}, {"p", "parity"}}) {
            if (code == name) return std::string("(flags & b2::") + flag + ") != 0";
            if (code.starts_with('n') && code.substr(1) == name) return std::string("(flags & b2::") + flag + ") == 0";
        }
        throw std::runtime_error("Unsupported condition code");
    }
    void flags(std::string_view operation, unsigned bits) {
        type(bits);
        uses_flags = writes_flags = true;
        body << "    flags = b2::" << operation << '<' << bits << ">(flags, a";
        if (operation != "logic_flags") body << ", b";
        body << ");\n";
    }
    unsigned xmm_register(ZydisRegister value) {
        if (value < ZYDIS_REGISTER_XMM0 || value > ZYDIS_REGISTER_XMM7)
            throw std::runtime_error("Expected an Xbox XMM register");
        return value-ZYDIS_REGISTER_XMM0;
    }
    std::string vector(const ZydisDecodedOperand& operand, bool scalar = false) {
        if (operand.type == ZYDIS_OPERAND_TYPE_REGISTER)
            return "b2::xmm(cpu.floating,"+std::to_string(xmm_register(operand.reg.value))+')';
        if (operand.type != ZYDIS_OPERAND_TYPE_MEMORY) throw std::runtime_error("Expected SIMD register or memory");
        if (scalar) return "b2::scalar_vector(memory.load<std::uint32_t>("+address(operand)+"))";
        return "b2::load_vector(memory,"+address(operand)+')';
    }
    void vector_write(const ZydisDecodedOperand& operand, const std::string& value, bool scalar = false) {
        if (operand.type == ZYDIS_OPERAND_TYPE_REGISTER)
            body << "    b2::set_xmm(cpu.floating," << xmm_register(operand.reg.value) << ',' << value << ");\n";
        else if (operand.type == ZYDIS_OPERAND_TYPE_MEMORY)
            body << "    b2::store_vector(memory," << address(operand) << ',' << value << ',' << (scalar?"true":"false") << ");\n";
        else throw std::runtime_error("Unsupported SIMD destination");
    }
    void mmx(const Instruction& instruction) {
        const auto& ins=instruction.decoded;
        const ZydisDecodedOperand* memory_operand=nullptr;
        const ZydisDecodedOperand* immediate=nullptr;
        for(unsigned i=0;i<ins.operand_count_visible;++i) {
            const auto& operand=instruction.operands[i];
            if(operand.type==ZYDIS_OPERAND_TYPE_MEMORY) memory_operand=&operand;
            else if(operand.type==ZYDIS_OPERAND_TYPE_IMMEDIATE && operand.size==8) immediate=&operand;
            else if(operand.type!=ZYDIS_OPERAND_TYPE_REGISTER || operand.reg.value<ZYDIS_REGISTER_MM0 || operand.reg.value>ZYDIS_REGISTER_MM7)
                throw std::runtime_error("Expected an MMX register or memory operand");
        }
        if(memory_operand && memory_operand->size!=64) throw std::runtime_error("Expected a 64-bit MMX memory operand");
        floating=true;
        const unsigned modrm=memory_operand?(ins.raw.modrm.reg<<3)|1:(3U<<6)|(ins.raw.modrm.reg<<3)|ins.raw.modrm.rm;
        const bool empty=ins.mnemonic==ZYDIS_MNEMONIC_EMMS;
        const auto helper=(empty?std::string("b2_mmx_77"):std::format("b2_mmx_{:02X}{:02X}",ins.opcode,modrm))+
            (immediate?std::format("_{:02X}",immediate->imm.value.u):"");
        native_helpers[helper]=helper+" PROC\n    db 00Fh,"+std::format("0{:02X}h",ins.opcode)+
            (empty?"":std::format(",0{:02X}h",modrm))+(immediate?std::format(",0{:02X}h",immediate->imm.value.u):"")+"\n    ret\n"+helper+" ENDP\n";
        native_declarations[helper]="extern \"C\" void "+helper+'('+(memory_operand?"void*":"")+");\n";
        body << "    " << helper << '(';
        if(memory_operand) body << "memory.access(" << address(*memory_operand) << ",8)";
        body << ");\n";
    }
    void x87(const Instruction& instruction) {
        const auto& ins=instruction.decoded;
        const std::string_view mnemonic=ZydisMnemonicGetString(ins.mnemonic);
        if (mnemonic.starts_with("fcmov")) throw std::runtime_error("Floating conditional move needs guest flags");
        floating=true;
        const bool waiting=mnemonic=="fwait";
        const bool compare_flags=mnemonic.starts_with("fcomi") || mnemonic.starts_with("fucomi");
        const bool status=mnemonic=="fnstsw" && ins.raw.modrm.mod==3;
        const ZydisDecodedOperand* memory=nullptr;
        for (unsigned i=0;i<ins.operand_count_visible;++i)
            if (instruction.operands[i].type==ZYDIS_OPERAND_TYPE_MEMORY) memory=&instruction.operands[i];
        const bool environment=mnemonic=="fnsave" || mnemonic=="frstor" || mnemonic=="fnstenv" || mnemonic=="fldenv";
        if (environment && ins.operand_width!=32) throw std::runtime_error("Only 32-bit x87 environments are supported");
        const unsigned modrm=memory ? (ins.raw.modrm.reg<<3)|1 : (3U<<6)|(ins.raw.modrm.reg<<3)|ins.raw.modrm.rm;
        const auto helper=waiting?std::string("b2_x87_9B"):std::format("b2_x87_{:02X}{:02X}",ins.opcode,modrm);
        std::string assembly=helper+" PROC\n    db "+std::format("0{:02X}h",ins.opcode);
        if (!waiting) assembly+=std::format(",0{:02X}h",modrm);
        assembly+='\n';
        if (compare_flags) assembly+="    pushfq\n    pop rax\n";
        if (status) assembly+="    movzx eax,ax\n";
        assembly+="    ret\n"+helper+" ENDP\n";
        native_helpers[helper]=assembly;
        native_declarations[helper]="extern \"C\" "+std::string(status||compare_flags?"std::uint32_t ":"void ")+helper+'('+(memory?"void*":"")+");\n";
        const bool control=waiting || mnemonic=="fnstsw" || mnemonic=="fnstcw" || mnemonic=="fldcw" ||
            mnemonic=="fnsave" || mnemonic=="frstor" || mnemonic=="fnstenv" || mnemonic=="fldenv" ||
            mnemonic=="fnclex" || mnemonic=="fninit" || mnemonic=="fnop";
        if (memory) {
            if (!memory->size || memory->size%8) throw std::runtime_error("Unsupported x87 memory width");
            body << "    const auto fp_address=" << address(*memory) << ";\n"
                 << "    auto* fp_memory=memory.access(fp_address," << memory->size/8 << ");\n";
        }
        if (!control) {
            body << "    cpu.floating.instruction=" << constant(instruction.address) << ";\n"
                 << "    cpu.floating.opcode=" << (((ins.opcode&7U)<<8)|((3U<<6)|(ins.raw.modrm.reg<<3)|ins.raw.modrm.rm)) << "U;\n"
                 << "    cpu.floating.code_selector=cpu.cs_selector;\n";
            if (memory) {
                body << "    cpu.floating.data=fp_address;\n    cpu.floating.data_selector=cpu."
                    << (memory->mem.segment==ZYDIS_REGISTER_SS?"ss":memory->mem.segment==ZYDIS_REGISTER_FS?"fs":memory->mem.segment==ZYDIS_REGISTER_GS?"gs":"ds") << "_selector;\n";
                // Record the original ModRM, not the RCX addressing used by the host helper.
                body << "    cpu.floating.opcode=" << (((ins.opcode&7U)<<8)|((ins.raw.modrm.mod<<6)|(ins.raw.modrm.reg<<3)|ins.raw.modrm.rm)) << "U;\n";
            }
        }
        if (status || compare_flags) body << "    const auto fp_value=";
        else body << "    ";
        body << helper << '(' << (memory?"fp_memory":"") << ");\n";
        if (status) write(instruction.operands[0],"fp_value");
        if (compare_flags) {
            uses_flags=writes_flags=true;
            body << "    flags=(flags&~b2::arithmetic_flags)|(fp_value&(b2::carry|b2::parity|b2::zero));\n";
        }
        if (mnemonic=="fnsave" || mnemonic=="fnstenv") body << "    cpu.floating.environment(fp_memory);\n";
        if (mnemonic=="frstor" || mnemonic=="fldenv") body << "    cpu.floating.restore_environment(fp_memory);\n";
        if (mnemonic=="fnsave" || mnemonic=="fninit")
            body << "    cpu.floating.instruction=cpu.floating.data=0;\n    cpu.floating.opcode=cpu.floating.code_selector=cpu.floating.data_selector=0;\n";
    }
    void x87_run(const std::vector<const Instruction*>& run) {
        const auto helper=std::format("b2_x87_run_{:08X}_{:08X}",run.front()->address,run.back()->address);
        std::string assembly=helper+" PROC\n",ready;
        unsigned operands=0;
        body << "    if(!cpu.diagnostic) {\n"
             << "        struct Operand {void* pointer;std::uint32_t address;std::uint16_t selector;};\n"
             << "        static_assert(sizeof(Operand)==16);\n";
        std::ostringstream pointers;
        for(const auto* instruction:run) {
            const auto& ins=instruction->decoded;
            const ZydisDecodedOperand* memory=nullptr;
            for(unsigned i=0;i<ins.operand_count_visible;++i)
                if(instruction->operands[i].type==ZYDIS_OPERAND_TYPE_MEMORY) memory=&instruction->operands[i];
            if(memory) {
                const auto offset=operands*16;
                body << "        const auto fp_address_" << operands << '=' << address(*memory) << ";\n";
                pointers << "            {memory.try_access(fp_address_" << operands << ',' << memory->size/8
                         << "),fp_address_" << operands << ",cpu."
                         << (memory->mem.segment==ZYDIS_REGISTER_SS?"ss":memory->mem.segment==ZYDIS_REGISTER_FS?"fs":memory->mem.segment==ZYDIS_REGISTER_GS?"gs":"ds")
                         << "_selector},\n";
                if(!ready.empty()) ready+=" && ";
                ready+=std::format("fp_operands[{}].pointer",operands++);
                assembly+=std::format("    mov rax,qword ptr [rcx+{}]\n    mov r8d,dword ptr [rcx+{}]\n"
                    "    mov dword ptr [rdx+{}],r8d\n    mov r8w,word ptr [rcx+{}]\n    mov word ptr [rdx+{}],r8w\n",
                    offset,offset+8,offsetof(FloatingState,data),offset+12,offsetof(FloatingState,data_selector));
            }
            const auto original_modrm=(ins.raw.modrm.mod<<6)|(ins.raw.modrm.reg<<3)|ins.raw.modrm.rm;
            assembly+=std::format("    mov dword ptr [rdx+{}],0{:08X}h\n    mov word ptr [rdx+{}],0{:04X}h\n"
                "    db 0{:02X}h,0{:02X}h\n",offsetof(FloatingState,instruction),instruction->address,
                offsetof(FloatingState,opcode),((ins.opcode&7U)<<8)|original_modrm,ins.opcode,
                memory?(ins.raw.modrm.reg<<3):original_modrm);
        }
        assembly+="    ret\n"+helper+" ENDP\n";
        native_helpers[helper]=std::move(assembly);
        native_declarations[helper]="extern \"C\" void "+helper+"(const void*,FloatingState*);\n";
        if(operands) body << "        const std::array<Operand," << operands << "> fp_operands{{\n" << pointers.str() << "        }};\n";
        // Failed range checks fall through to the original compiled sequence:
        // earlier stores, floating state and the failing instruction stay exact.
        body << "        if(" << (operands?ready:"true") << ") {\n"
             << "            cpu.floating.code_selector=cpu.cs_selector;\n            " << helper
             << '(' << (operands?"fp_operands.data()":"nullptr") << ",&cpu.floating);\n"
             << "            goto " << label(run.back()->address+run.back()->decoded.length) << ";\n        }\n    }\n";
    }
    void instruction(const Instruction& instruction) {
        const auto& ins = instruction.decoded;
        const auto& operands = instruction.operands;
        const auto next = instruction.address + ins.length;
        const auto repeats = ins.attributes & (ZYDIS_ATTRIB_HAS_REP | ZYDIS_ATTRIB_HAS_REPE | ZYDIS_ATTRIB_HAS_REPNE);
        const bool string_move = ins.meta.category == ZYDIS_CATEGORY_STRINGOP &&
            (ins.mnemonic == ZYDIS_MNEMONIC_MOVSB || ins.mnemonic == ZYDIS_MNEMONIC_MOVSW || ins.mnemonic == ZYDIS_MNEMONIC_MOVSD);
        const bool string_fill = ins.mnemonic == ZYDIS_MNEMONIC_STOSB || ins.mnemonic == ZYDIS_MNEMONIC_STOSW || ins.mnemonic == ZYDIS_MNEMONIC_STOSD;
        const bool string_compare=ins.meta.category==ZYDIS_CATEGORY_STRINGOP &&
            (ins.mnemonic==ZYDIS_MNEMONIC_CMPSB || ins.mnemonic==ZYDIS_MNEMONIC_CMPSW || ins.mnemonic==ZYDIS_MNEMONIC_CMPSD);
        const bool string_scan=ins.mnemonic==ZYDIS_MNEMONIC_SCASB || ins.mnemonic==ZYDIS_MNEMONIC_SCASW || ins.mnemonic==ZYDIS_MNEMONIC_SCASD;
        const bool locked=ins.attributes&ZYDIS_ATTRIB_HAS_LOCK;
        if (ins.address_width != 32 || (locked && ins.mnemonic!=ZYDIS_MNEMONIC_XCHG && ins.mnemonic!=ZYDIS_MNEMONIC_XADD && ins.mnemonic!=ZYDIS_MNEMONIC_CMPXCHG) ||
            (repeats && !string_compare && !string_scan && !string_move && !string_fill) ||
            ((string_compare || string_scan || string_move || string_fill) &&
             (ins.attributes & (ZYDIS_ATTRIB_HAS_SEGMENT_FS | ZYDIS_ATTRIB_HAS_SEGMENT_GS))))
            throw std::runtime_error("Instruction prefixes need an implementation");
        body << label(instruction.address) << ": { // " << instruction.text << '\n';
        if(loop_headers.contains(instruction.address))
            body << "    if(b2::checkpoint_due(cpu)) { commit(); b2::checkpoint(cpu," << constant(instruction.address) << "); }\n";
        if(const auto run=floating_runs.find(instruction.address);run!=floating_runs.end()) x87_run(run->second);
        bool terminator = false;
        switch (ins.mnemonic) {
        case ZYDIS_MNEMONIC_SAHF:
            uses_flags=writes_flags=true;
            body << "    flags=(flags&~0xD5U)|((" << read_reg(ZYDIS_REGISTER_EAX) << ">>8)&0xD5U);\n";
            break;
        case ZYDIS_MNEMONIC_LAHF:
            uses_flags=true;
            write(ZydisDecodedOperand{.type=ZYDIS_OPERAND_TYPE_REGISTER,.reg={ZYDIS_REGISTER_AH}},"(flags&0xD5U)|2U");
            break;
        case ZYDIS_MNEMONIC_XCHG: {
            if(operands[0].type==ZYDIS_OPERAND_TYPE_MEMORY || operands[1].type==ZYDIS_OPERAND_TYPE_MEMORY) {
                const auto& memory_operand=operands[operands[0].type==ZYDIS_OPERAND_TYPE_MEMORY?0:1];
                const auto& register_operand=operands[operands[0].type==ZYDIS_OPERAND_TYPE_MEMORY?1:0];
                body << "    const auto location=" << address(memory_operand) << ";\n"
                    << "    const auto value=memory.exchange<" << type(memory_operand.size) << ">(location,static_cast<"
                    << type(memory_operand.size) << ">(" << read(register_operand) << "));\n";
                write(register_operand,"value");
            } else {
                body << "    const auto a=" << read(operands[0]) << ";\n    const auto b=" << read(operands[1]) << ";\n";
                write(operands[0],"b"); write(operands[1],"a");
            }
            break;
        }
        case ZYDIS_MNEMONIC_XADD: {
            uses_flags=writes_flags=true;
            const auto bits=operands[0].size;
            body << "    const auto b=" << read(operands[1]) << ";\n";
            if(operands[0].type==ZYDIS_OPERAND_TYPE_MEMORY) {
                body << "    const auto location=" << address(operands[0]) << ";\n    const auto a=memory."
                    << (locked?"fetch_add":"load") << '<' << type(bits) << ">(location";
                if(locked) body << ",static_cast<" << type(bits) << ">(b)";
                body << ");\n";
            } else body << "    const auto a=" << read(operands[0]) << ";\n";
            body << "    flags=b2::add_flags<" << bits << ">(flags,a,b);\n";
            write(operands[1],"a");
            if(operands[0].type==ZYDIS_OPERAND_TYPE_MEMORY) {
                if(!locked) body << "    memory.store<" << type(bits) << ">(location,static_cast<" << type(bits) << ">(a+b));\n";
            } else write(operands[0],"a+b");
            break;
        }
        case ZYDIS_MNEMONIC_CMPXCHG: {
            const auto bits=operands[0].size;
            type(bits); uses_flags=writes_flags=true;
            const auto accumulator=bits==8?ZYDIS_REGISTER_AL:bits==16?ZYDIS_REGISTER_AX:ZYDIS_REGISTER_EAX;
            body << "    const auto expected=" << read_reg(accumulator) << ";\n"
                << "    const auto desired=" << read(operands[1]) << ";\n";
            if(operands[0].type==ZYDIS_OPERAND_TYPE_MEMORY && locked)
                body << "    const auto prior=memory.compare_exchange<" << type(bits) << ">(" << address(operands[0])
                    << ",static_cast<" << type(bits) << ">(expected),static_cast<" << type(bits) << ">(desired));\n";
            else body << "    const auto prior=" << read(operands[0]) << ";\n";
            body << "    flags=b2::sub_flags<" << bits << ">(flags,expected,prior);\n    if(expected==prior) {\n";
            if(!locked) write(operands[0],"desired");
            body << "    } else {\n";
            write(ZydisDecodedOperand{.type=ZYDIS_OPERAND_TYPE_REGISTER,.reg={accumulator}},"prior");
            body << "    }\n";
            break;
        }
        case ZYDIS_MNEMONIC_BSF: {
            uses_flags=writes_flags=true;
            body << "    const std::uint32_t value=" << read(operands[1]) << ";\n"
                << "    flags=(flags&~b2::zero)|(value?0U:b2::zero);\n    if(value) {\n";
            write(operands[0],"static_cast<std::uint32_t>(std::countr_zero(value))");
            body << "    }\n";
            break;
        }
        case ZYDIS_MNEMONIC_CLI: case ZYDIS_MNEMONIC_STI:
            uses_flags=writes_flags=true;
            body << "    flags " << (ins.mnemonic==ZYDIS_MNEMONIC_CLI?"&= ~":"|= ") << "0x200U;\n";
            break;
        case ZYDIS_MNEMONIC_SGDT:
            body << "    const auto location=" << address(operands[0]) << ";\n"
                << "    memory.access(location,6);\n    memory.store<std::uint16_t>(location,cpu.gdt_limit);\n"
                << "    memory.store<std::uint32_t>(location+2,cpu.gdt_base);\n";
            break;
        case ZYDIS_MNEMONIC_PREFETCHNTA:
            body << "    memory.prefetch(" << address(operands[0]) << ");\n";
            break;
        case ZYDIS_MNEMONIC_SFENCE:
            body << "    _mm_sfence();\n";
            break;
        case ZYDIS_MNEMONIC_STMXCSR:
            floating=true;
            write(operands[0],"_mm_getcsr()");
            break;
        case ZYDIS_MNEMONIC_LDMXCSR:
            floating=true;
            body << "    const auto control=" << read(operands[0]) << ";\n"
                << "    if(control&~0xFFFFU) throw std::runtime_error(\"Guest MXCSR has reserved bits set\");\n"
                << "    _mm_setcsr(control);\n";
            break;
        case ZYDIS_MNEMONIC_MOVQ: case ZYDIS_MNEMONIC_MOVNTQ: case ZYDIS_MNEMONIC_EMMS:
        case ZYDIS_MNEMONIC_PSHUFW: case ZYDIS_MNEMONIC_PUNPCKLWD: case ZYDIS_MNEMONIC_PUNPCKHWD:
        case ZYDIS_MNEMONIC_PUNPCKLDQ: case ZYDIS_MNEMONIC_PUNPCKHDQ:
            mmx(instruction); break;
        case ZYDIS_MNEMONIC_SHUFPS: case ZYDIS_MNEMONIC_UNPCKLPS: case ZYDIS_MNEMONIC_UNPCKHPS:
        case ZYDIS_MNEMONIC_MOVHLPS: case ZYDIS_MNEMONIC_MOVLHPS: {
            floating=true;
            const auto name=ins.mnemonic==ZYDIS_MNEMONIC_SHUFPS?"shuffle_ps":
                ins.mnemonic==ZYDIS_MNEMONIC_UNPCKLPS?"unpacklo_ps":ins.mnemonic==ZYDIS_MNEMONIC_UNPCKHPS?"unpackhi_ps":
                ins.mnemonic==ZYDIS_MNEMONIC_MOVHLPS?"movehl_ps":"movelh_ps";
            auto value="_mm_"+std::string(name)+'('+vector(operands[0])+','+vector(operands[1]);
            if(ins.mnemonic==ZYDIS_MNEMONIC_SHUFPS) value+=','+read(operands[2]);
            vector_write(operands[0],value+')');
            break;
        }
        case ZYDIS_MNEMONIC_MOVLPS: case ZYDIS_MNEMONIC_MOVHPS: {
            floating=true;
            const bool store=operands[0].type==ZYDIS_OPERAND_TYPE_MEMORY;
            const auto& memory_operand=operands[store?0:1];
            const auto& register_operand=operands[store?1:0];
            const auto storage="cpu.floating.xmm["+std::to_string(xmm_register(register_operand.reg.value))+"].data()+"+
                (ins.mnemonic==ZYDIS_MNEMONIC_MOVHPS?"2":"0");
            const auto mapped="memory.access("+address(memory_operand)+",8)";
            body << "    std::memcpy(" << (store?mapped:storage) << ',' << (store?storage:mapped) << ",8);\n";
            break;
        }
        case ZYDIS_MNEMONIC_MOVAPS: case ZYDIS_MNEMONIC_MOVUPS: case ZYDIS_MNEMONIC_MOVSS: {
            floating=true;
            const bool scalar=ins.mnemonic==ZYDIS_MNEMONIC_MOVSS;
            if (ins.mnemonic==ZYDIS_MNEMONIC_MOVAPS) for(unsigned i=0;i<2;++i)
                if(operands[i].type==ZYDIS_OPERAND_TYPE_MEMORY)
                    body << "    if(" << address(operands[i]) << "&15U) throw std::runtime_error(\"Unaligned guest MOVAPS\");\n";
            auto value=vector(operands[1],scalar);
            if (scalar && operands[0].type==ZYDIS_OPERAND_TYPE_REGISTER && operands[1].type==ZYDIS_OPERAND_TYPE_REGISTER)
                value="_mm_move_ss("+vector(operands[0])+','+value+')';
            vector_write(operands[0],value,scalar); break;
        }
        case ZYDIS_MNEMONIC_MOVNTPS:
            floating=true;
            body << "    b2::stream_vector(memory," << address(operands[0]) << ',' << vector(operands[1]) << ");\n";
            break;
        case ZYDIS_MNEMONIC_CVTTSS2SI: case ZYDIS_MNEMONIC_CVTSS2SI:
            floating=true;
            write(operands[0],std::string("static_cast<std::uint32_t>(")+
                (ins.mnemonic==ZYDIS_MNEMONIC_CVTTSS2SI?"_mm_cvtt_ss2si(":"_mm_cvt_ss2si(")+vector(operands[1],true)+"))");
            break;
        case ZYDIS_MNEMONIC_CVTSI2SS:
            floating=true;
            vector_write(operands[0],"_mm_cvtsi32_ss("+vector(operands[0])+",std::bit_cast<std::int32_t>("+read(operands[1])+"))");
            break;
        case ZYDIS_MNEMONIC_ADDSS: case ZYDIS_MNEMONIC_SUBSS: case ZYDIS_MNEMONIC_MULSS: case ZYDIS_MNEMONIC_DIVSS:
        case ZYDIS_MNEMONIC_MINSS: case ZYDIS_MNEMONIC_MAXSS: case ZYDIS_MNEMONIC_SQRTSS: case ZYDIS_MNEMONIC_RSQRTSS:
        case ZYDIS_MNEMONIC_RCPSS: case ZYDIS_MNEMONIC_ADDPS: case ZYDIS_MNEMONIC_SUBPS: case ZYDIS_MNEMONIC_MULPS:
        case ZYDIS_MNEMONIC_DIVPS: case ZYDIS_MNEMONIC_MINPS: case ZYDIS_MNEMONIC_MAXPS: case ZYDIS_MNEMONIC_SQRTPS:
        case ZYDIS_MNEMONIC_RSQRTPS: case ZYDIS_MNEMONIC_RCPPS: case ZYDIS_MNEMONIC_XORPS: case ZYDIS_MNEMONIC_ANDPS:
        case ZYDIS_MNEMONIC_ORPS: case ZYDIS_MNEMONIC_ANDNPS: {
            floating=true;
            std::string name=ZydisMnemonicGetString(ins.mnemonic);
            const bool scalar=name.ends_with("ss");
            name.insert(name.size()-2,"_");
            const bool unary=name.starts_with("sqrt_") || name.starts_with("rsqrt_") || name.starts_with("rcp_");
            std::string value=(name.starts_with("rsqrt_") ? "b2::" : "_mm_")+name+'(';
            if (!unary) value+=vector(operands[0])+',';
            value+=vector(operands[1],scalar)+')';
            if (unary && scalar) value="_mm_move_ss("+vector(operands[0])+','+value+')';
            vector_write(operands[0],value); break;
        }
        case ZYDIS_MNEMONIC_MOV: case ZYDIS_MNEMONIC_MOVZX:
            body << "    const auto value = " << read(operands[1]) << ";\n";
            write(operands[0], "value"); break;
        case ZYDIS_MNEMONIC_MOVSX:
            body << "    const auto value = b2::sign_extend<" << operands[1].size << ">(" << read(operands[1]) << ");\n";
            type(operands[1].size); write(operands[0], "value"); break;
        case ZYDIS_MNEMONIC_LEA:
            body << "    const auto value = " << address(operands[1], true) << ";\n";
            write(operands[0], "value"); break;
        case ZYDIS_MNEMONIC_ADD: case ZYDIS_MNEMONIC_SUB: case ZYDIS_MNEMONIC_CMP:
        case ZYDIS_MNEMONIC_ADC: case ZYDIS_MNEMONIC_SBB:
        case ZYDIS_MNEMONIC_AND: case ZYDIS_MNEMONIC_OR: case ZYDIS_MNEMONIC_XOR: case ZYDIS_MNEMONIC_TEST: {
            body << "    const std::uint32_t a = " << read(operands[0]) << ";\n"
                 << "    const std::uint32_t b = " << read(operands[1]) << ";\n";
            const auto bits = operands[0].size;
            const auto m = ins.mnemonic;
            if (m == ZYDIS_MNEMONIC_ADD || m == ZYDIS_MNEMONIC_SUB || m == ZYDIS_MNEMONIC_CMP ||
                m == ZYDIS_MNEMONIC_ADC || m == ZYDIS_MNEMONIC_SBB) {
                const bool addition = m == ZYDIS_MNEMONIC_ADD || m == ZYDIS_MNEMONIC_ADC;
                const bool with_carry = m == ZYDIS_MNEMONIC_ADC || m == ZYDIS_MNEMONIC_SBB;
                if (with_carry) {
                    type(bits);
                    uses_flags = writes_flags = true;
                    body << "    const auto input_carry = flags & b2::carry;\n"
                         << "    flags = b2::" << (addition ? "add_flags" : "sub_flags") << '<' << bits
                         << ">(flags, a, b, input_carry);\n";
                } else flags(addition ? "add_flags" : "sub_flags", bits);
                if (m != ZYDIS_MNEMONIC_CMP)
                    write(operands[0], std::string(addition ? "a + b" : "a - b") +
                                      (with_carry ? (addition ? " + input_carry" : " - input_carry") : ""));
            } else {
                const char operation = m == ZYDIS_MNEMONIC_OR ? '|' : m == ZYDIS_MNEMONIC_XOR ? '^' : '&';
                body << "    const auto value = a " << operation << " b;\n";
                uses_flags = writes_flags = true;
                type(bits);
                body << "    flags = b2::logic_flags<" << bits << ">(flags, value);\n";
                if (m != ZYDIS_MNEMONIC_TEST) write(operands[0], "value");
            }
            break;
        }
        case ZYDIS_MNEMONIC_SETZ: case ZYDIS_MNEMONIC_SETNZ: case ZYDIS_MNEMONIC_SETB: case ZYDIS_MNEMONIC_SETNB:
        case ZYDIS_MNEMONIC_SETBE: case ZYDIS_MNEMONIC_SETNBE: case ZYDIS_MNEMONIC_SETL: case ZYDIS_MNEMONIC_SETNL:
        case ZYDIS_MNEMONIC_SETLE: case ZYDIS_MNEMONIC_SETNLE:
            body << "    const std::uint32_t value = (" << condition(ins.mnemonic) << ") ? 1U : 0U;\n";
            write(operands[0], "value"); break;
        case ZYDIS_MNEMONIC_JZ: case ZYDIS_MNEMONIC_JNZ: case ZYDIS_MNEMONIC_JB: case ZYDIS_MNEMONIC_JNB:
        case ZYDIS_MNEMONIC_JBE: case ZYDIS_MNEMONIC_JNBE: case ZYDIS_MNEMONIC_JL: case ZYDIS_MNEMONIC_JNL:
        case ZYDIS_MNEMONIC_JLE: case ZYDIS_MNEMONIC_JNLE:
            body << "    if (" << condition(ins.mnemonic) << ") goto " << label(*instruction.target()) << ";\n";
            break;
        case ZYDIS_MNEMONIC_JMP:
            if(operands[0].type==ZYDIS_OPERAND_TYPE_POINTER) {
                calls=true;
                body << "    commit();\n    cpu.eip=" << constant(instruction.address) << ";\n"
                    << "    b2::load_code_selector(cpu,memory," << operands[0].ptr.segment << ',' << constant(operands[0].ptr.offset)
                    << ");\n    goto " << label(*instruction.target()) << ";\n";
            } else if (instruction.kernel_ordinal) {
                calls=restores=returns=true;
                body << "    commit();\n    cpu.eip=" << constant(instruction.address) << ";\n"
                    << "    b2::call_kernel<" << *instruction.kernel_ordinal << ">(cpu,memory);\n    restore();\n    goto done;\n";
            } else if (!instruction.jump_targets.empty()) {
                calls=true;
                body << "    switch(" << read(operands[0]) << ") {\n";
                const std::set<std::uint32_t> targets(instruction.jump_targets.begin(),instruction.jump_targets.end());
                for(const auto target : targets) body << "    case " << constant(target) << ": goto " << label(target) << ";\n";
                body << "    default: commit(); cpu.eip=" << constant(instruction.address)
                    << "; throw std::runtime_error(\"Guest jump table target is outside its recovered set\");\n    }\n";
            } else if (instruction.target()) body << "    goto " << label(*instruction.target()) << ";\n";
            else {
                calls=restores=returns=true;
                body << "    const auto target=" << read(operands[0]) << ";\n    commit();\n"
                    << "    b2::invoke_native(cpu,memory,target);\n    restore();\n    goto done;\n";
            }
            terminator = true; break;
        case ZYDIS_MNEMONIC_PUSH: {
            const auto width = ins.operand_width;
            if (width != 16 && width != 32) throw std::runtime_error("Unsupported push width");
            used.insert(4); dirty.insert(4);
            body << "    const auto value = " << read(operands[0]) << ";\n    esp -= " << width / 8
                 << "U;\n    memory.store<" << type(width) << ">(esp, static_cast<" << type(width) << ">(value));\n";
            break;
        }
        case ZYDIS_MNEMONIC_PUSHF: case ZYDIS_MNEMONIC_PUSHFD: {
            const unsigned width=ins.operand_width;
            if(width!=16 && width!=32) throw std::runtime_error("Unsupported flags stack width");
            used.insert(4); dirty.insert(4); uses_flags=true;
            body << "    esp-=" << width/8 << "U;\n    memory.store<" << type(width)
                << ">(esp,static_cast<" << type(width) << ">((flags&~0x30000U)|2U));\n";
            break;
        }
        case ZYDIS_MNEMONIC_POPF: case ZYDIS_MNEMONIC_POPFD: {
            const unsigned width=ins.operand_width;
            if(width!=16 && width!=32) throw std::runtime_error("Unsupported flags stack width");
            used.insert(4); dirty.insert(4); uses_flags=writes_flags=true;
            body << "    const auto value=memory.load<" << type(width) << ">(esp);\n    esp+=" << width/8
                << "U;\n    flags=b2::pop_flags(flags,value," << width << ",cpu.cs_selector&3U);\n";
            break;
        }
        case ZYDIS_MNEMONIC_POP: {
            const auto width = ins.operand_width;
            if (width != 16 && width != 32) throw std::runtime_error("Unsupported pop width");
            used.insert(4); dirty.insert(4);
            body << "    const auto value = memory.load<" << type(width) << ">(esp);\n    esp += " << width / 8 << "U;\n";
            write(operands[0], "value"); break;
        }
        case ZYDIS_MNEMONIC_CALL: {
            if (ins.operand_width != 32 || operands[0].type==ZYDIS_OPERAND_TYPE_POINTER)
                throw std::runtime_error("Only 32-bit near native calls are supported");
            calls = restores = true;
            const bool indirect=!instruction.target() && !instruction.kernel_ordinal;
            if(indirect) body << "    const auto target=" << read(operands[0]) << ";\n";
            used.insert(4); dirty.insert(4);
            body << "    esp -= 4U;\n    memory.store<std::uint32_t>(esp, " << constant(next)
                 << ");\n    commit();\n    cpu.eip = " << constant(instruction.target().value_or(instruction.address)) << ";\n    ";
            if (instruction.kernel_ordinal) body << "b2::call_kernel<" << *instruction.kernel_ordinal << ">(cpu,memory);\n";
            else if (instruction.target()) body << "guest_" << std::format("{:08X}", *instruction.target()) << "(cpu, memory);\n";
            else body << "b2::invoke_native(cpu,memory,target);\n";
            body << "    if (cpu.eip != " << constant(next)
                 << ") throw std::runtime_error(\"Unexpected return from call at " << hex32(instruction.address) << "\");\n"
                 << "    restore();\n";
            break;
        }
        case ZYDIS_MNEMONIC_RDTSC:
            calls=true;
            body << "    commit();\n    cpu.eip=" << constant(instruction.address) << ";\n"
                 << "    const auto timestamp=b2::read_timestamp(cpu);\n";
            write(ZydisDecodedOperand{.type=ZYDIS_OPERAND_TYPE_REGISTER,.reg={ZYDIS_REGISTER_EAX}},"static_cast<std::uint32_t>(timestamp)");
            write(ZydisDecodedOperand{.type=ZYDIS_OPERAND_TYPE_REGISTER,.reg={ZYDIS_REGISTER_EDX}},"static_cast<std::uint32_t>(timestamp>>32)");
            break;
        case ZYDIS_MNEMONIC_IN: case ZYDIS_MNEMONIC_OUT: {
            calls=restores=true;
            const bool input=ins.mnemonic==ZYDIS_MNEMONIC_IN;
            const auto& port=operands[input?1:0];
            const auto& value=operands[input?0:1];
            body << "    const auto port=static_cast<std::uint16_t>(" << read(port) << ");\n";
            if(!input) body << "    const auto value=" << read(value) << ";\n";
            body << "    commit();\n    cpu.eip=" << constant(instruction.address) << ";\n";
            if(input) body << "    const auto value=b2::read_port(cpu,port," << value.size << ");\n";
            else body << "    b2::write_port(cpu,port," << value.size << ",value);\n";
            body << "    restore();\n";
            if(input) write(value,"value");
            break;
        }
        case ZYDIS_MNEMONIC_INT3: case ZYDIS_MNEMONIC_INT:
            calls=true;
            body << "    commit();\n    cpu.eip=" << constant(next) << ";\n"
                 << "    throw b2::GuestTrap(" << (ins.mnemonic==ZYDIS_MNEMONIC_INT3?3:operands[0].imm.value.u)
                 << ',' << constant(instruction.address) << ',' << constant(next) << ");\n";
            terminator=true; break;
        case ZYDIS_MNEMONIC_LEAVE:
            if (ins.operand_width != 32) throw std::runtime_error("Unsupported leave width");
            used.insert(4); used.insert(5); dirty.insert(4); dirty.insert(5);
            body << "    esp = ebp;\n    ebp = memory.load<std::uint32_t>(esp);\n    esp += 4U;\n";
            break;
        case ZYDIS_MNEMONIC_INC: case ZYDIS_MNEMONIC_DEC: {
            const bool increment = ins.mnemonic == ZYDIS_MNEMONIC_INC;
            type(operands[0].size);
            uses_flags = writes_flags = true;
            body << "    const auto a = " << read(operands[0]) << ";\n    flags = (b2::"
                 << (increment ? "add_flags" : "sub_flags") << '<' << operands[0].size
                 << ">(flags, a, 1U) & ~b2::carry) | (flags & b2::carry);\n";
            write(operands[0], increment ? "a + 1U" : "a - 1U"); break;
        }
        case ZYDIS_MNEMONIC_NOT: case ZYDIS_MNEMONIC_NEG:
            body << "    const auto a = " << read(operands[0]) << ";\n";
            if (ins.mnemonic == ZYDIS_MNEMONIC_NEG) {
                type(operands[0].size);
                uses_flags = writes_flags = true;
                body << "    flags = b2::sub_flags<" << operands[0].size << ">(flags, 0U, a);\n";
            }
            write(operands[0], ins.mnemonic == ZYDIS_MNEMONIC_NOT ? "~a" : "0U - a"); break;
        case ZYDIS_MNEMONIC_CDQ:
            used.insert(0); used.insert(2); dirty.insert(2);
            body << "    edx = 0U - (eax >> 31);\n"; break;
        case ZYDIS_MNEMONIC_CWDE:
            used.insert(0); dirty.insert(0);
            body << "    eax = b2::sign_extend<16>(eax);\n"; break;
        case ZYDIS_MNEMONIC_SHL: case ZYDIS_MNEMONIC_SHR: case ZYDIS_MNEMONIC_SAR: {
            type(operands[0].size);
            uses_flags = writes_flags = true;
            body << "    const auto result = b2::shift<" << operands[0].size << ">(flags, " << read(operands[0])
                 << ", " << read(operands[1]) << ", b2::Shift::"
                 << (ins.mnemonic == ZYDIS_MNEMONIC_SHL ? "left" : ins.mnemonic == ZYDIS_MNEMONIC_SHR ? "right" : "arithmetic_right")
                 << ");\n    flags = result.flags;\n";
            write(operands[0], "result.value"); break;
        }
        case ZYDIS_MNEMONIC_ROR: case ZYDIS_MNEMONIC_RCR: case ZYDIS_MNEMONIC_ROL: case ZYDIS_MNEMONIC_RCL: {
            type(operands[0].size);
            uses_flags=writes_flags=true;
            body << "    const auto result=b2::rotate_" << (ins.mnemonic==ZYDIS_MNEMONIC_ROL || ins.mnemonic==ZYDIS_MNEMONIC_RCL?"left":"right")
                << '<' << operands[0].size << ','
                << (ins.mnemonic==ZYDIS_MNEMONIC_RCR || ins.mnemonic==ZYDIS_MNEMONIC_RCL?"true":"false") << ">(" << read(operands[0]) << ','
                << read(operands[1]) << ",flags);\n    flags=result.flags;\n";
            write(operands[0],"result.value"); break;
        }
        case ZYDIS_MNEMONIC_SHLD: case ZYDIS_MNEMONIC_SHRD: {
            if(operands[0].size!=16 && operands[0].size!=32) throw std::runtime_error("Invalid double shift width");
            uses_flags=writes_flags=true;
            body << "    const auto result=b2::double_shift<" << operands[0].size << ','
                << (ins.mnemonic==ZYDIS_MNEMONIC_SHRD?"true":"false") << ">(" << read(operands[0]) << ','
                << read(operands[1]) << ',' << read(operands[2]) << ",flags);\n    flags=result.flags;\n";
            write(operands[0],"result.value"); break;
        }
        case ZYDIS_MNEMONIC_MUL: case ZYDIS_MNEMONIC_IMUL: {
            uses_flags = writes_flags = true;
            const auto bits = operands[0].size;
            type(bits);
            const bool implicit = ins.operand_count_visible == 1;
            const auto accumulator = bits == 8 ? ZYDIS_REGISTER_AL : bits == 16 ? ZYDIS_REGISTER_AX : ZYDIS_REGISTER_EAX;
            const auto a = implicit ? read_reg(accumulator) : read(operands[ins.operand_count_visible == 3 ? 1 : 0]);
            const auto b = read(operands[implicit ? 0 : ins.operand_count_visible == 3 ? 2 : 1]);
            body << "    const auto product = b2::multiply<" << bits << ", "
                 << (ins.mnemonic == ZYDIS_MNEMONIC_IMUL ? "true" : "false") << ">(flags, " << a << ", " << b
                 << ");\n    flags = product.flags;\n";
            if (implicit) {
                ZydisDecodedOperand dest{};
                dest.type = ZYDIS_OPERAND_TYPE_REGISTER;
                dest.reg.value = bits == 8 ? ZYDIS_REGISTER_AX : accumulator;
                write(dest, "static_cast<std::uint32_t>(product.value)");
                if (bits != 8) {
                    dest.reg.value = bits == 16 ? ZYDIS_REGISTER_DX : ZYDIS_REGISTER_EDX;
                    write(dest, "static_cast<std::uint32_t>(product.value >> " + std::to_string(bits) + ')');
                }
            } else write(operands[0], "static_cast<std::uint32_t>(product.value)");
            break;
        }
        case ZYDIS_MNEMONIC_DIV: case ZYDIS_MNEMONIC_IDIV: {
            const auto bits = operands[0].size;
            type(bits);
            const auto accumulator = bits == 8 ? ZYDIS_REGISTER_AL : bits == 16 ? ZYDIS_REGISTER_AX : ZYDIS_REGISTER_EAX;
            const auto dividend = bits == 8 ? read_reg(ZYDIS_REGISTER_AX) :
                "(std::uint64_t(" + read_reg(bits == 16 ? ZYDIS_REGISTER_DX : ZYDIS_REGISTER_EDX) + ") << " +
                std::to_string(bits) + ") | " + read_reg(accumulator);
            body << "    const auto quotient = b2::divide<" << bits << ", "
                 << (ins.mnemonic == ZYDIS_MNEMONIC_IDIV ? "true" : "false") << ">(" << dividend << ", " << read(operands[0]) << ");\n";
            ZydisDecodedOperand dest{};
            dest.type = ZYDIS_OPERAND_TYPE_REGISTER;
            dest.reg.value = accumulator;
            write(dest, "quotient.value");
            dest.reg.value = bits == 8 ? ZYDIS_REGISTER_AH : bits == 16 ? ZYDIS_REGISTER_DX : ZYDIS_REGISTER_EDX;
            write(dest, "quotient.remainder"); break;
        }
        case ZYDIS_MNEMONIC_MOVSB: case ZYDIS_MNEMONIC_MOVSW: case ZYDIS_MNEMONIC_MOVSD:
        case ZYDIS_MNEMONIC_STOSB: case ZYDIS_MNEMONIC_STOSW: case ZYDIS_MNEMONIC_STOSD: {
            if (!string_move && !string_fill) throw std::runtime_error("SIMD move needs its own lowering");
            const auto bits = (ins.mnemonic == ZYDIS_MNEMONIC_MOVSB || ins.mnemonic == ZYDIS_MNEMONIC_STOSB) ? 8 :
                              (ins.mnemonic == ZYDIS_MNEMONIC_MOVSW || ins.mnemonic == ZYDIS_MNEMONIC_STOSW) ? 16 : 32;
            uses_flags = true;
            used.insert(7); dirty.insert(7);
            if (repeats) { used.insert(1); dirty.insert(1); }
            body << "    const auto count = " << (repeats ? "ecx" : "1U") << ";\n"
                 << "    const bool descending = (flags & b2::direction) != 0;\n";
            if (string_move) {
                used.insert(6); dirty.insert(6);
                body << "    memory.move_elements<" << type(bits) << ">(edi, esi, count, descending);\n";
            } else {
                used.insert(0);
                body << "    memory.fill_elements<" << type(bits) << ">(edi, static_cast<" << type(bits) << ">(eax), count, descending);\n";
            }
            body << "    const auto step = descending ? 0U - count * " << bits / 8 << "U : count * " << bits / 8 << "U;\n"
                 << "    edi += step;\n";
            if (string_move) body << "    esi += step;\n";
            if (repeats) body << "    ecx = 0;\n";
            break;
        }
        case ZYDIS_MNEMONIC_CMPSB: case ZYDIS_MNEMONIC_CMPSW: case ZYDIS_MNEMONIC_CMPSD:
        case ZYDIS_MNEMONIC_SCASB: case ZYDIS_MNEMONIC_SCASW: case ZYDIS_MNEMONIC_SCASD: {
            if(!string_compare && !string_scan) throw std::runtime_error("SIMD compare needs its own lowering");
            const auto bits=ins.mnemonic==ZYDIS_MNEMONIC_CMPSB || ins.mnemonic==ZYDIS_MNEMONIC_SCASB?8:
                ins.mnemonic==ZYDIS_MNEMONIC_CMPSW || ins.mnemonic==ZYDIS_MNEMONIC_SCASW?16:32;
            used.insert(7); dirty.insert(7);
            if(string_compare) { used.insert(6); dirty.insert(6); }
            else used.insert(0);
            uses_flags = writes_flags = true;
            if (repeats) { used.insert(1); dirty.insert(1); body << "    while (ecx != 0) {\n"; }
            body << "    const auto a = ";
            if(string_compare) body << "memory.load<" << type(bits) << ">(esi)";
            else body << "eax";
            body << ";\n    const auto b = memory.load<" << type(bits) << ">(edi);\n"
                 << "    flags = b2::sub_flags<" << bits << ">(flags, a, b);\n"
                 << "    const auto step = (flags & b2::direction) ? 0U-" << bits/8 << "U : " << bits/8 << "U;\n";
            if(string_compare) body << "    esi += step;\n";
            body << "    edi += step;\n";
            if (repeats) {
                body << "    --ecx;\n    if ((flags & b2::zero) "
                     << ((ins.attributes & ZYDIS_ATTRIB_HAS_REPNE) ? "!= 0" : "== 0") << ") break;\n    }\n";
            }
            break;
        }
        case ZYDIS_MNEMONIC_CLD: case ZYDIS_MNEMONIC_STD:
            uses_flags = writes_flags = true;
            body << "    flags " << (ins.mnemonic == ZYDIS_MNEMONIC_CLD ? "&= ~" : "|= ") << "b2::direction;\n";
            break;
        case ZYDIS_MNEMONIC_RET: {
            returns=true;
            if (ins.operand_width != 32) throw std::runtime_error("Unsupported return width");
            used.insert(4); dirty.insert(4);
            body << "    const auto return_address = memory.load<std::uint32_t>(esp);\n"
                 << "    esp += " << (4U + (ins.operand_count_visible ? static_cast<unsigned>(operands[0].imm.value.u) : 0U))
                 << "U;\n    cpu.eip = return_address;\n    goto done;\n";
            terminator = true; break;
        }
        case ZYDIS_MNEMONIC_NOP: break;
        default:
            if (ins.meta.category == ZYDIS_CATEGORY_COND_BR && instruction.target()) {
                body << "    if (" << condition(ins.mnemonic) << ") goto " << label(*instruction.target()) << ";\n";
            } else if (ins.meta.category == ZYDIS_CATEGORY_SETCC) {
                body << "    const std::uint32_t value = (" << condition(ins.mnemonic) << ") ? 1U : 0U;\n";
                write(operands[0], "value");
            } else if (ins.meta.category == ZYDIS_CATEGORY_CMOV) {
                body << "    const auto value = " << read(operands[1]) << ";\n    if (" << condition(ins.mnemonic) << ") {\n";
                write(operands[0], "value");
                body << "    }\n";
            } else if ((ins.opcode>=0xD8 && ins.opcode<=0xDF && ins.opcode_map==ZYDIS_OPCODE_MAP_DEFAULT) || ins.mnemonic==ZYDIS_MNEMONIC_FWAIT) {
                x87(instruction);
            } else throw std::runtime_error("Instruction has no native lowering yet");
        }
        if (!terminator) body << "    goto " << label(next) << ";\n";
        body << "}\n";
    }
};
}

LoweredFunction lower(const Function& function,bool fuse_floating) {
    LoweredFunction result;
    const auto api=std::ranges::find(native_apis,function.entry,&NativeApi::address);
    if(api!=native_apis.end()) {
        std::string prefix;auto pc=function.entry;
        while(prefix.size()<api->prefix.size() && function.instructions.contains(pc)) {
            const auto& instruction=function.instructions.at(pc);
            prefix+=hex_bytes(Bytes(instruction.bytes).first(instruction.decoded.length));pc+=instruction.decoded.length;
        }
        if(prefix!=api->prefix) throw std::runtime_error("Native API entry signature changed: "+std::string(api->name));
        result.native_api=api->name;
        result.source=std::format("void native_platform(Cpu&,Memory&,std::uint32_t);\nvoid guest_{:08X}(Cpu& cpu,Memory& memory) {{\n"
            "    if(b2::checkpoint_due(cpu)) b2::checkpoint(cpu,0x{:08X}U);\n    native_platform(cpu,memory,0x{:08X}U);\n}}\n",function.entry,function.entry,function.entry);
        return result;
    }
    Emitter emitter;
    for(const auto& edge : function.edges)
        if(edge.to && (edge.kind=="jump" || edge.kind=="conditional" || edge.kind=="table-jump") && *edge.to<=edge.from)
            emitter.loop_headers.insert(*edge.to);
    emitter.calls=!emitter.loop_headers.empty();
    if(fuse_floating) {
        std::set<std::uint32_t> targets{function.entry};
        for(const auto& edge:function.edges) if(edge.to) targets.insert(*edge.to);
        for(auto current=function.instructions.begin();current!=function.instructions.end();) {
            std::vector<const Instruction*> run;
            auto next=current;
            while(next!=function.instructions.end() && run.size()<32 && batchable_x87(next->second) &&
                  (run.empty() || (!targets.contains(next->first) && next->first==run.back()->address+run.back()->decoded.length))) {
                run.push_back(&next->second);++next;
            }
            if(run.size()>=4) emitter.floating_runs.emplace(current->first,std::move(run));
            current=next==current?std::next(current):next;
        }
    }
    for (const auto& [pc, instruction] : function.instructions) {
        try { emitter.instruction(instruction); }
        catch (const std::exception& error) {
            result.issues.push_back({pc, hex_bytes(Bytes(instruction.bytes).first(instruction.decoded.length)),
                                     instruction.text, error.what()});
        }
    }
    if (!result.issues.empty()) return result;
    std::ostringstream out;
    for(const auto& [name,declaration] : emitter.native_declarations) out << declaration;
    out << "void guest_" << std::format("{:08X}", function.entry) << "(Cpu& cpu, [[maybe_unused]] Memory& memory) {\n";
    out << "    if(b2::checkpoint_due(cpu)) b2::checkpoint(cpu," << constant(function.entry) << ");\n";
    for (const auto index : emitter.used)
        out << "    auto " << registers[index] << " = cpu.registers[" << index << "];\n";
    if (emitter.uses_flags) out << "    auto flags = cpu.flags;\n";
    if (emitter.calls) {
        out << "    const auto commit = [&] {\n";
        for (const auto index : emitter.dirty) out << "        cpu.registers[" << index << "] = " << registers[index] << ";\n";
        if (emitter.writes_flags) out << "        cpu.flags = flags;\n";
        out << "    };\n";
        if (emitter.restores) {
            out << "    const auto restore = [&] {\n";
            for (const auto index : emitter.used) out << "        " << registers[index] << " = cpu.registers[" << index << "];\n";
            if (emitter.uses_flags) out << "        flags = cpu.flags;\n";
            out << "    };\n";
        }
    }
    out << "    goto " << label(function.entry) << ";\n";
    out << emitter.body.str();
    if(emitter.returns) {
        out << "done:\n";
        for (const auto index : emitter.dirty)
            out << "    cpu.registers[" << index << "] = " << registers[index] << ";\n";
        if (emitter.writes_flags) out << "    cpu.flags = flags;\n";
    }
    out << "}\n";
    result.source = out.str();
    result.floating=emitter.floating;
    result.native_helpers=std::move(emitter.native_helpers);
    return result;
}

std::string recompile(Xbe& image, std::uint32_t address, std::uint32_t instruction_budget) {
    if (!image.supported()) throw std::runtime_error("Native generation requires the verified target executable");
    const auto function = recover(image, address, instruction_budget);
    if (function.budget_exhausted || function.unresolved)
        throw std::runtime_error("Function discovery is incomplete; inspect the analysis report");
    const auto generated = lower(function);
    if (!generated.issues.empty()) {
        const auto& issue = generated.issues.front();
        throw std::runtime_error(hex32(issue.address) + " [" + issue.bytes + "]: " + issue.instruction + ": " + issue.reason);
    }
    if (std::ranges::any_of(function.edges, [](const Edge& edge) { return edge.kind == "call"; }))
        throw std::runtime_error("This function calls other functions; use batch recompilation to recover its dependencies");
    if (generated.floating) throw std::runtime_error("Floating-point functions require batch generation of their native helpers");
    return "// Generated from XBE SHA256 " + image.sha256 + "\n#include \"cpu.h\"\nnamespace b2 {\n" + generated.source +
        "extern const CompiledFunction " + std::format("compiled_{:#x}", address) + '{' + constant(address) + ", \"" + image.sha256 +
        "\", guest_" + std::format("{:08X}", address) + "};\n}\n";
}
}
