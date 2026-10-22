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
    if(instruction.address==0xADED5 || instruction.address==0xADFE6 || instruction.address==0xAE18F ||
       instruction.address==0x8C932 || instruction.address==0xB9748) return false;
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
bool batchable_mmx_copy(const Instruction& instruction) {
    const auto& ins=instruction.decoded;
    if(ins.opcode_map!=ZYDIS_OPCODE_MAP_0F || ins.address_width!=32 ||
       (ins.mnemonic!=ZYDIS_MNEMONIC_MOVQ && ins.mnemonic!=ZYDIS_MNEMONIC_MOVNTQ) ||
       (ins.attributes&(ZYDIS_ATTRIB_HAS_LOCK|ZYDIS_ATTRIB_HAS_REP|ZYDIS_ATTRIB_HAS_REPE|ZYDIS_ATTRIB_HAS_REPNE))) return false;
    for(unsigned i=0;i<ins.operand_count_visible;++i) {
        const auto& operand=instruction.operands[i];
        if(operand.type==ZYDIS_OPERAND_TYPE_MEMORY) {
            if(operand.mem.type!=ZYDIS_MEMOP_TYPE_MEM || operand.size!=64) return false;
        } else if(operand.type!=ZYDIS_OPERAND_TYPE_REGISTER || operand.reg.value<ZYDIS_REGISTER_MM0 || operand.reg.value>ZYDIS_REGISTER_MM7) return false;
    }
    return true;
}

// Verified scene-frame boundaries: update original state at 50/60 Hz, draw at
// the selected display rate. Catch-up steps omit only the drawing portion.
struct TimingGate {std::uint32_t address,target;std::string_view bytes;bool render;};
constexpr TimingGate timing_gates[]={
    {0x1115C,0x11161,"E84F620C00",false},
    {0x133B3,0x13428,"E8080C0000",false},{0x13428,0x13482,"8B0DC03E4400",true},
    {0x14DF1,0x150BB,"8B3D4CA04B00",false},{0x150BB,0x150C9,"8B4610",true},
    {0x15423,0x15491,"E8B8030000",false},{0x15491,0x1549F,"8B4610",true},
    {0x163C3,0x163C8,"E818F4FFFF",false},{0x163C8,0x163D6,"8B4610",true},
    {0x16843,0x1684B,"E898EFFFFF",false},{0x1684B,0x16859,"8B4610",true},
    {0x1A823,0x1A828,"E8D8F2FFFF",false},{0x1A828,0x1A836,"8B4610",true}
};
constexpr std::pair<std::uint32_t,std::string_view> presentation_sites[]={
    {0xE90D0,"8B442404"},{0xE9040,"57"},{0xE8F60,"8B442404"},
    {0x9B380,"55"},{0x7BEDE,"A1B03E4400"},{0x2E280,"55"},
    {0xADCA0,"56"},{0xA6130,"55"},
    {0x3E2D0,"56"},{0x3F815,"8B8EDC170000"},
    {0xADE20,"83EC3C"},{0xADE38,"E8B318F9FF"},{0xADE3D,"A1A8CA4C00"},
    {0xADED5,"D90588815900"},{0xADFE6,"D82588815900"},{0xAE18F,"D82588815900"},
    {0x13485,"83C40C"},{0x13488,"5E"},
    {0x8C8D0,"83EC24"},{0x8C932,"D90588815900"},{0x8C9FF,"C3"},
    {0x8CF03,"8B5708"},
    {0x8EBC5,"8B4C2424"},
    {0xA97FF,"89442410"},{0xB9748,"D90588815900"},
    {0xADEB4,"D91F"},{0xADEEC,"8BD8"}
};
struct MusicSite {std::uint32_t address;std::string_view bytes;std::uint32_t repeat_target=0;};
constexpr MusicSite music_sites[]={
    {0x462DA,"8B0D48B34C00"},{0x46510,"6A00"},
    {0x4788F,"8BCE"},{0x46EDA,"6A06"},{0x47190,"DB81B0000000"},{0x47220,"56"},
    {0xC9333,"FF0D08C25700"},{0xCA348,"42",0xCA349},
    {0x21066E,"8B00",0x210670}
};
constexpr std::pair<std::uint32_t,std::string_view> pause_row_sites[]={
    {0x46DDF,"83BE8800000005"},{0x46DFC,"C70305000000"},{0x46E04,"C7868800000004000000"},
    {0x46E84,"8D84D6A0000000"},{0x46EB6,"8D84D6A0000000"},
    {0x47C87,"8B8CD6B0000000"},{0x47C8E,"8D04D6"},{0x47CB4,"8D88A0000000"},{0x47CE5,"8D8CCEA0000000"}
};
class Emitter {
public:
    bool portable=false;
    std::set<unsigned> used, dirty;
    bool uses_flags = false, writes_flags = false;
    bool calls = false;
    bool restores = false;
    bool returns = false;
    bool floating = false;
    bool extended_world = false;
    std::set<std::uint32_t> labels;
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
        if(extended_world && lea && operand.mem.base==ZYDIS_REGISTER_ESI && operand.mem.index==ZYDIS_REGISTER_NONE && operand.mem.disp.value==0x2C8) {
            const auto base=read_reg(operand.mem.base);
            return "(cpu.world_view && cpu.world_view->manager=="+base+" ? cpu.world_view->list : std::uint32_t("+base+"+0x2C8U))";
        }
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
        case ZYDIS_OPERAND_TYPE_MEMORY: {
            const auto original="ram.load<" + type(operand.size) + ">(" + address(operand) + ')';
            if(extended_world && operand.size==32 && operand.mem.base==ZYDIS_REGISTER_ESI) {
                const auto base=read_reg(operand.mem.base),guard="cpu.world_view && cpu.world_view->manager=="+base;
                if(operand.mem.index==ZYDIS_REGISTER_NONE && operand.mem.disp.value==0x310)
                    return "("+guard+" ? cpu.world_view->count : "+original+")";
                if(operand.mem.index!=ZYDIS_REGISTER_NONE && operand.mem.scale==4 && operand.mem.disp.value==0xD0)
                    return "("+guard+" ? cpu.world_view->sectors["+read_reg(operand.mem.index)+"] : "+original+")";
            }
            return original;
        }
        default: throw std::runtime_error("Unsupported operand type");
        }
    }
    void write(const ZydisDecodedOperand& operand, const std::string& value) {
        if (operand.type == ZYDIS_OPERAND_TYPE_MEMORY) {
            body << "    ram.store<" << type(operand.size) << ">(" << address(operand)
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
        if (scalar) return "b2::scalar_vector(ram.load<std::uint32_t>("+address(operand)+"))";
        return "b2::load_vector(ram,"+address(operand)+')';
    }
    void vector_write(const ZydisDecodedOperand& operand, const std::string& value, bool scalar = false) {
        if (operand.type == ZYDIS_OPERAND_TYPE_REGISTER)
            body << "    b2::set_xmm(cpu.floating," << xmm_register(operand.reg.value) << ',' << value << ");\n";
        else if (operand.type == ZYDIS_OPERAND_TYPE_MEMORY)
            body << "    b2::store_vector(ram," << address(operand) << ',' << value << ',' << (scalar?"true":"false") << ");\n";
        else throw std::runtime_error("Unsupported SIMD destination");
    }
    void mmx(const Instruction& instruction) {
        const auto& ins=instruction.decoded;
        const ZydisDecodedOperand* memory_operand=nullptr;
        const ZydisDecodedOperand* immediate=nullptr;
        const ZydisDecodedOperand* register_operand=nullptr;
        std::string pointer;
        for(unsigned i=0;i<ins.operand_count_visible;++i) {
            const auto& operand=instruction.operands[i];
            if(operand.type==ZYDIS_OPERAND_TYPE_MEMORY) memory_operand=&operand;
            else if(operand.type==ZYDIS_OPERAND_TYPE_IMMEDIATE && operand.size==8) immediate=&operand;
            else if(operand.type==ZYDIS_OPERAND_TYPE_REGISTER && ins.mnemonic==ZYDIS_MNEMONIC_MOVD &&
                operand.reg.value>=ZYDIS_REGISTER_EAX && operand.reg.value<=ZYDIS_REGISTER_EDI) register_operand=&operand;
            else if(i==1 && operand.type==ZYDIS_OPERAND_TYPE_REGISTER && ins.mnemonic==ZYDIS_MNEMONIC_CVTPS2PI)
                pointer="cpu.floating.xmm["+std::to_string(xmm_register(operand.reg.value))+"].data()";
            else if(operand.type!=ZYDIS_OPERAND_TYPE_REGISTER || operand.reg.value<ZYDIS_REGISTER_MM0 || operand.reg.value>ZYDIS_REGISTER_MM7)
                throw std::runtime_error("Expected an MMX register or memory operand");
        }
        const auto width=ins.mnemonic==ZYDIS_MNEMONIC_MOVD?4U:8U;
        if(memory_operand) {
            if(memory_operand->size!=width*8) throw std::runtime_error("Unexpected MMX memory width");
            pointer="ram.access("+address(*memory_operand)+','+std::to_string(width)+')';
        }
        const bool register_write=register_operand==&instruction.operands[0];
        if(register_operand) {
            body << "    std::uint32_t mmx_word=" << (register_write?"0U":read(*register_operand)) << ";\n";
            pointer="&mmx_word";
        }
        floating=true;
        // Guest XMM/GPR operands live in C++ state; present their original
        // bits as memory while MMX continues to share the resident x87 state.
        const unsigned modrm=!pointer.empty()?(ins.raw.modrm.reg<<3)|1:(3U<<6)|(ins.raw.modrm.reg<<3)|ins.raw.modrm.rm;
        const bool empty=ins.mnemonic==ZYDIS_MNEMONIC_EMMS;
        const auto helper=(empty?std::string("b2_mmx_77"):std::format("b2_mmx_{:02X}{:02X}",ins.opcode,modrm))+
            (immediate?std::format("_{:02X}",immediate->imm.value.u):"");
        native_helpers[helper]=helper+" PROC\n    db 00Fh,"+std::format("0{:02X}h",ins.opcode)+
            (empty?"":std::format(",0{:02X}h",modrm))+(immediate?std::format(",0{:02X}h",immediate->imm.value.u):"")+"\n    ret\n"+helper+" ENDP\n";
        if(portable) native_helpers[helper]=std::format("extern \"C\" B2_ASM void {}({}) {{b2::portable_mmx<{},{},{}>({});}}\n",
            helper,pointer.empty()?"":"void* p",ins.opcode,modrm,immediate?immediate->imm.value.u:0,pointer.empty()?"":"p");
        native_declarations[helper]="extern \"C\" B2_ASM void "+helper+'('+(!pointer.empty()?"void*":"")+");\n";
        body << "    " << helper << '(' << pointer << ");\n";
        if(register_write) write(*register_operand,"mmx_word");
    }
    void x87(const Instruction& instruction) {
        const auto& ins=instruction.decoded;
        const std::string_view mnemonic=ZydisMnemonicGetString(ins.mnemonic);
        if(portable) {
            constexpr std::string_view supported[]={"fwait","fnstsw","fnstcw","fldcw","fnsave","frstor","fnstenv","fldenv","fnclex","fninit","fnop",
                "fld","fst","fstp","fild","fist","fistp","fisttp","fadd","faddp","fiadd","fsub","fsubp","fsubr","fsubrp","fisub","fisubr",
                "fmul","fmulp","fimul","fdiv","fdivp","fdivr","fdivrp","fidiv","fidivr","fsqrt","fchs","fabs","fxch","ffree","fld1","fldz",
                "fldl2t","fldl2e","fldpi","fldlg2","fldln2","fcom","fcomp","fcompp","fucom","fucomp","fucompp","fcomi","fcomip","fucomi","fucomip",
                "ftst","f2xm1","fyl2x","fptan","fpatan","fyl2xp1","fsincos","frndint","fscale","fsin","fcos","fdecstp","fincstp"};
            if(std::ranges::find(supported,mnemonic)==std::end(supported)) throw std::runtime_error("Unsupported ARM x87 instruction");
        }
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
        if(portable) native_helpers[helper]=std::format("extern \"C\" B2_ASM {} {}({}) {{return {}b2::portable_x87<{},{}>({});}}\n",
            status||compare_flags?"std::uint32_t":"void",helper,memory?"void* p":"",status||compare_flags?"":"(void)",ins.opcode,modrm,memory?"p":"");
        native_declarations[helper]="extern \"C\" B2_ASM "+std::string(status||compare_flags?"std::uint32_t ":"void ")+helper+'('+(memory?"void*":"")+");\n";
        const bool control=waiting || mnemonic=="fnstsw" || mnemonic=="fnstcw" || mnemonic=="fldcw" ||
            mnemonic=="fnsave" || mnemonic=="frstor" || mnemonic=="fnstenv" || mnemonic=="fldenv" ||
            mnemonic=="fnclex" || mnemonic=="fninit" || mnemonic=="fnop";
        if (memory) {
            if (!memory->size || memory->size%8) throw std::runtime_error("Unsupported x87 memory width");
            body << "    const auto fp_address=" << address(*memory) << ";\n"
                 << "    auto* fp_memory=";
            if(instruction.address==0xADED5 || instruction.address==0xADFE6 || instruction.address==0xAE18F)
                body << "cpu.game_timing ? static_cast<void*>(&cpu.game_timing->loading_delta) : ";
            if(instruction.address==0x8C932 || instruction.address==0xB9748)
                body << "cpu.presentation && cpu.game_timing && cpu.game_timing->started ? static_cast<void*>(&cpu.game_timing->render_delta) : ";
            body << "ram.access(fp_address," << memory->size/8 << ");\n";
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
        struct Range {ZydisDecodedOperand operand;std::int64_t first,last;};
        std::vector<Range> ranges;
        std::map<std::string,unsigned> groups;
        std::vector<unsigned> operand_groups;
        for(const auto* instruction:run) for(unsigned i=0;i<instruction->decoded.operand_count_visible;++i) {
            auto operand=instruction->operands[i];
            if(operand.type!=ZYDIS_OPERAND_TYPE_MEMORY) continue;
            const auto displacement=operand.mem.disp.has_displacement?operand.mem.disp.value:0;
            operand.mem.disp.has_displacement=false;
            const auto [group,inserted]=groups.try_emplace(address(operand),static_cast<unsigned>(ranges.size()));
            if(inserted) ranges.push_back({operand,displacement,displacement+operand.size/8});
            auto& range=ranges[group->second];
            range.first=std::min(range.first,displacement);range.last=std::max(range.last,displacement+operand.size/8);
            operand_groups.push_back(group->second);
        }
        const auto helper=std::format("b2_x87_run_{:08X}_{:08X}",run.front()->address,run.back()->address);
        std::string precise,fast,last_data,last_instruction,ready;
        unsigned operands=0;
        body << "    if(!cpu.diagnostic) {\n"
             << "        struct Operand {void* pointer;std::uint32_t address;std::uint16_t selector;};\n"
             << "        static_assert(sizeof(Operand)==16);\n";
        for(unsigned i=0;i<ranges.size();++i) {
            auto operand=ranges[i].operand;operand.mem.disp.has_displacement=true;operand.mem.disp.value=ranges[i].first;
            body << "        const auto fp_range_address_" << i << '=' << address(operand) << ";\n"
                 << "        auto* fp_range_" << i << "=static_cast<std::byte*>(memory.try_access(fp_range_address_" << i << ','
                 << ranges[i].last-ranges[i].first << "ULL));\n";
            if(!ready.empty()) ready+=" && ";ready+=std::format("fp_range_{}",i);
        }
        body << "        if(" << (ranges.empty()?"true":ready) << ") {\n";
        std::ostringstream pointers;
        for(const auto* instruction:run) {
            const auto& ins=instruction->decoded;
            const ZydisDecodedOperand* memory=nullptr;
            for(unsigned i=0;i<ins.operand_count_visible;++i)
                if(instruction->operands[i].type==ZYDIS_OPERAND_TYPE_MEMORY) memory=&instruction->operands[i];
            if(memory) {
                const auto offset=operands*16;
                const auto group=operand_groups[operands];
                const auto displacement=(memory->mem.disp.has_displacement?memory->mem.disp.value:0)-ranges[group].first;
                pointers << "            {fp_range_" << group << '+' << displacement << "ULL,std::uint32_t(fp_range_address_" << group << '+'
                         << displacement << "ULL),cpu."
                         << (memory->mem.segment==ZYDIS_REGISTER_SS?"ss":memory->mem.segment==ZYDIS_REGISTER_FS?"fs":memory->mem.segment==ZYDIS_REGISTER_GS?"gs":"ds")
                         << "_selector},\n";
                ++operands;
                const auto pointer=std::format("    mov rax,qword ptr [rcx+{}]\n",offset);
                last_data=std::format("    mov r8d,dword ptr [rcx+{}]\n    mov dword ptr [rdx+{}],r8d\n"
                    "    mov r8w,word ptr [rcx+{}]\n    mov word ptr [rdx+{}],r8w\n",
                    offset+8,offsetof(FloatingState,data),offset+12,offsetof(FloatingState,data_selector));
                precise+=pointer+last_data;fast+=pointer;
            }
            const auto original_modrm=(ins.raw.modrm.mod<<6)|(ins.raw.modrm.reg<<3)|ins.raw.modrm.rm;
            last_instruction=std::format("    mov dword ptr [rdx+{}],0{:08X}h\n    mov word ptr [rdx+{}],0{:04X}h\n",
                offsetof(FloatingState,instruction),instruction->address,offsetof(FloatingState,opcode),
                ((ins.opcode&7U)<<8)|original_modrm);
            const auto operation=std::format("    db 0{:02X}h,0{:02X}h\n",ins.opcode,memory?(ins.raw.modrm.reg<<3):original_modrm);
            precise+=last_instruction+operation;fast+=operation;
        }
        // Validated blocks cannot fault on memory. When all x87 exceptions are
        // masked, only their final provenance is observable. Keep instruction-
        // by-instruction publication for controls that permit a floating trap.
        native_helpers[helper]=helper+" PROC FRAME\n    sub rsp,8\n    .allocstack 8\n    .endprolog\n"
            "    fnstcw word ptr [rsp]\n    movzx r8d,word ptr [rsp]\n    and r8d,3Fh\n    cmp r8d,3Fh\n    jne "+helper+"_precise\n"+
            fast+last_data+last_instruction+"    add rsp,8\n    ret\n"+helper+"_precise:\n"+
            precise+"    add rsp,8\n    ret\n"+helper+" ENDP\n";
        native_declarations[helper]="extern \"C\" B2_ASM void "+helper+"(const void*,FloatingState*);\n";
        if(operands) body << "        const std::array<Operand," << operands << "> fp_operands{{\n" << pointers.str() << "        }};\n";
        // Failed range checks fall through to the original compiled sequence:
        // earlier stores, floating state and the failing instruction stay exact.
        body << "            cpu.floating.code_selector=cpu.cs_selector;\n            " << helper
             << '(' << (operands?"fp_operands.data()":"nullptr") << ",&cpu.floating);\n"
             << "            goto " << label(run.back()->address+run.back()->decoded.length) << ";\n        }\n    }\n";
    }
    void mmx_run(const std::vector<const Instruction*>& run) {
        struct Range {ZydisDecodedOperand operand;std::int64_t first,last;};
        std::vector<Range> ranges;
        std::map<std::string,unsigned> groups;
        std::vector<unsigned> operands;
        for(const auto* instruction:run) for(unsigned i=0;i<instruction->decoded.operand_count_visible;++i) {
            auto operand=instruction->operands[i];
            if(operand.type!=ZYDIS_OPERAND_TYPE_MEMORY) continue;
            const auto displacement=operand.mem.disp.has_displacement?operand.mem.disp.value:0;
            operand.mem.disp.has_displacement=false;
            const auto key=address(operand);
            const auto [group,inserted]=groups.try_emplace(key,static_cast<unsigned>(ranges.size()));
            if(inserted) ranges.push_back({operand,displacement,displacement});
            auto& range=ranges[group->second];
            range.first=std::min(range.first,displacement);range.last=std::max(range.last,displacement);
            operands.push_back(group->second);
        }
        const auto helper=std::format("b2_mmx_run_{:08X}_{:08X}",run.front()->address,run.back()->address);
        std::string assembly=helper+" PROC\n",ready;
        body << "    if(!cpu.diagnostic) {\n        const std::array<void*," << ranges.size() << "> mmx_operands{{\n";
        for(unsigned i=0;i<ranges.size();++i) {
            auto operand=ranges[i].operand;operand.mem.disp.has_displacement=true;operand.mem.disp.value=ranges[i].first;
            body << "            memory.try_access(" << address(operand) << ',' << ranges[i].last-ranges[i].first+8 << "ULL),\n";
            if(!ready.empty()) ready+=" && ";ready+=std::format("mmx_operands[{}]",i);
        }
        body << "        }};\n        if(" << (ranges.empty()?"true":ready) << ") {\n            " << helper << "(mmx_operands.data());\n"
             << "            goto " << label(run.back()->address+run.back()->decoded.length) << ";\n        }\n    }\n";
        unsigned index=0;
        for(const auto* instruction:run) {
            const auto& ins=instruction->decoded;
            const ZydisDecodedOperand* memory=nullptr;
            for(unsigned i=0;i<ins.operand_count_visible;++i) if(instruction->operands[i].type==ZYDIS_OPERAND_TYPE_MEMORY) memory=&instruction->operands[i];
            if(memory) {
                const auto group=operands[index++];
                const auto offset=static_cast<std::uint32_t>((memory->mem.disp.has_displacement?memory->mem.disp.value:0)-ranges[group].first);
                assembly+=std::format("    mov rax,qword ptr [rcx+{}]\n",group*8);
                if(offset<=0x7FFFFFFF) {if(offset) assembly+=std::format("    lea rax,[rax+{}]\n",offset);}
                else assembly+=std::format("    mov r8d,0{:08X}h\n    add rax,r8\n",offset);
            }
            assembly+=std::format("    db 00Fh,0{:02X}h,0{:02X}h\n",ins.opcode,
                memory?ins.raw.modrm.reg<<3:(3U<<6)|(ins.raw.modrm.reg<<3)|ins.raw.modrm.rm);
        }
        // GPRs and flags are unchanged. Keep original MMX/NT stores in order;
        // failed range preflight falls through to the individual instructions.
        native_helpers[helper]=assembly+"    ret\n"+helper+" ENDP\n";
        native_declarations[helper]="extern \"C\" B2_ASM void "+helper+"(const void*);\n";
        floating=true;
    }
    void instruction(const Instruction& instruction, bool falls_through) {
        extended_world=instruction.address>=0x91001 && instruction.address<0x9158F;
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
        if(labels.contains(instruction.address)) body << label(instruction.address) << ": ";
        body << "{ // " << instruction.text << '\n';
        if(instruction.address>=0x1AC00 && instruction.address<0x1CFE0 && ins.mnemonic==ZYDIS_MNEMONIC_CALL &&
           operands[0].type==ZYDIS_OPERAND_TYPE_MEMORY && operands[0].mem.disp.value==4) {
            const auto bytes=hex_bytes(Bytes(instruction.bytes).first(ins.length));
            if(bytes!="FF5004" && bytes!="FF5204") throw std::runtime_error("HUD constructor call signature changed");
            calls=restores=true;(void)read_reg(ZYDIS_REGISTER_ECX);(void)read_reg(ZYDIS_REGISTER_ESP);
            body << "    if(cpu.hud_layout) {commit(); b2::title_enhancement(cpu,memory,0x1AC27U); restore();}\n";
        }
        if(instruction.address==0x1D121) {
            calls=restores=true;
            body << "    if(cpu.hud_layout) {commit(); b2::title_enhancement(cpu,memory,0x1D121U); restore();}\n";
        }
        if(instruction.address==0x26870) {
            dirty.insert(2);used.insert(2);
            // Use the ordinary tachometer dimensions before the common HUD
            // correction; the native wide variant scales width/height .9/1.2.
            body << "    if(cpu.hud_layout) edx&=~4U;\n";
        }
        // Reset presentation at the original page-wide hide and frontend exit.
        // These reuse existing UI nodes, so allocation hooks cannot detect them.
        if(instruction.address==0x3E2D0 || instruction.address==0x3F815)
            body << "    if(cpu.presentation) b2::title_ui(cpu,memory,0,0);\n";
        if(instruction.address==0xC14C7) {
            const auto node=read_reg(ZYDIS_REGISTER_EAX);
            body << "    if(cpu.presentation) b2::title_ui(cpu,memory," << node << "-8U,0);\n";
        }
        if(instruction.address==0xC1309) {
            const auto node=read_reg(ZYDIS_REGISTER_ESI);
            body << "    if(ui_blur) ram.store<std::uint32_t>(" << node << "+0x40U,ui_blur_value);\n";
        }
        if(instruction.address==0xC1306 || instruction.address==0xC11ED) {
            (void)read_reg(ZYDIS_REGISTER_ESP);
            if(instruction.address==0xC1306) {
                const auto node=read_reg(ZYDIS_REGISTER_ESI);
                body << "    ui_blur=cpu.presentation && ram.load<std::uint32_t>(" << node << "+0x50U)==0x22A60U;\n"
                     << "    if(ui_blur) ui_blur_value=ram.load<std::uint32_t>(" << node << "+0x40U);\n";
                body << "    if(cpu.presentation) b2::title_ui(cpu,memory," << node << ",ram.load<std::uint32_t>(esp));\n";
            }
            body << "    if(hud_transform[0]!=1) {\n";
            if(instruction.address==0xC1306) {
                body << "    const auto p=ram.load<std::uint32_t>(esp);\n"
                     << "    ram.store<std::uint32_t>(p,std::bit_cast<std::uint32_t>(std::bit_cast<float>(ram.load<std::uint32_t>(p))*hud_transform[0]+hud_transform[1]));\n"
                     << "    ram.store<std::uint32_t>(p+8U,std::bit_cast<std::uint32_t>(std::bit_cast<float>(ram.load<std::uint32_t>(p+8U))*hud_transform[0]));\n";
            } else {
                body << "    for(const auto p:{esp,esp+8U}) ram.store<std::uint32_t>(p,std::bit_cast<std::uint32_t>(std::bit_cast<float>(ram.load<std::uint32_t>(p))*hud_transform[0]+hud_transform[1]));\n";
            }
            body << "    }\n";
        }
        if(instruction.address==0x11036) {
            if(hex_bytes(Bytes(instruction.bytes).first(ins.length))!="B970683D00")
                throw std::runtime_error("Original main-loop boundary changed");
            dirty.insert(1);used.insert(1);
            body << "    if(cpu.game_timing) {\n    b2::title_timing(cpu,memory);\n"
                 << "    if(!cpu.game_timing->update) {ecx=ram.load<std::uint32_t>(0x34AB5CU);goto i_00011144;}\n    }\n";
        }
        for(const auto& gate:timing_gates) {
            if(gate.render && (instruction.address==gate.address || instruction.address==gate.target)) {
                calls=restores=true;
                body << "    if(cpu.presentation) {commit(); b2::title_presentation(cpu,memory,"
                     << (instruction.address==gate.address?1:2) << "); restore();}\n";
            }
            if(instruction.address==gate.address)
                body << "    if(cpu.game_timing && !cpu.game_timing->" << (gate.render?"render":"update")
                     << ") goto " << label(gate.target) << ";\n";
        }
        // The menu keeps E8520's three cdecl arguments until its epilogue.
        // A catch-up iteration skipped that call and has no arguments to pop.
        if(instruction.address==0x13485)
            body << "    if(cpu.game_timing && !cpu.game_timing->render) goto i_00013488;\n";
        if(instruction.address==0x8C8D0)
            body << "    if(cpu.presentation && cpu.game_timing && cpu.game_timing->started) {\n"
                 << "    if(cpu.game_timing->scenery_animated) goto i_0008C9FF;\n"
                 << "    cpu.game_timing->scenery_animated=true;\n    }\n";
        if(instruction.address==0x8CF03) {
            (void)read_reg(ZYDIS_REGISTER_ESP);
            // Material animation reads D7340's absolute game timer. Smooth
            // its visual sample without changing that timer or physics.
            body << "    if(cpu.presentation && cpu.game_timing && cpu.game_timing->started && ram.load<std::uint32_t>(0x2FFDA0U)) {\n"
                 << "    const auto mxcsr=_mm_getcsr();_mm_setcsr(0x1F80);\n"
                 << "    ram.store<std::uint32_t>(esp+44U,std::bit_cast<std::uint32_t>(std::bit_cast<float>(ram.load<std::uint32_t>(esp+44U))+cpu.game_timing->render_offset));\n"
                 << "    _mm_setcsr(mxcsr);\n    }\n";
        }
        if(instruction.address==0xA97FF) {
            dirty.insert(0);used.insert(0);used.insert(1);
            body << "    eax=b2::title_blur_alpha(cpu,eax,ecx);\n";
        }
        if(instruction.address==0x215CE0) {
            if(hex_bytes(Bytes(instruction.bytes).first(ins.length))!="8B442404") throw std::runtime_error("Framebuffer request signature changed");
            calls=restores=true;(void)read_reg(ZYDIS_REGISTER_ESP);
            // A97C0 requests scene history; loading fades and other users of
            // GetBackBuffer(-1) need the complete previous display, with HUD.
            body << "    if(cpu.presentation && ram.load<std::uint32_t>(esp+4U)==0xFFFFFFFFU && ram.load<std::uint32_t>(esp)!=0xA99F9U) {\n"
                 << "    commit(); b2::title_enhancement(cpu,memory,0x215CE0U); restore();}\n";
        }
        if(instruction.address==0x81158 || instruction.address==0x81369 || instruction.address==0x817F7) {
            const auto bytes=instruction.address==0x81158?"E8F3720600":instruction.address==0x81369?"E8E2700600":"E8546C0600";
            if(hex_bytes(Bytes(instruction.bytes).first(ins.length))!=bytes) throw std::runtime_error("Race HUD boundary signature changed");
            calls=restores=true;
            body << "    if(cpu.presentation && (ram.load<std::uint32_t>(0x4431F8U) || ram.load<std::uint32_t>(0x443858U))) {\n"
                 << "    commit(); b2::title_enhancement(cpu,memory," << constant(instruction.address) << "); restore();}\n";
        }
        if(instruction.address==0xADEB4)
            body << "    if(cpu.game_timing) cpu.game_timing->loading_fade_error=0;\n";
        if(instruction.address==0xADEEC) {
            dirty.insert(0);used.insert(0);
            body << "    if(cpu.game_timing) eax=b2::title_loading_fade(cpu,memory);\n";
        }
        if(instruction.address==0xE90D0 || instruction.address==0xE9040 || instruction.address==0xE8F60 ||
           instruction.address==0x9B380 || instruction.address==0x7BEDE || instruction.address==0x2E280 ||
           instruction.address==0xADCA0 || instruction.address==0xA6130 || instruction.address==0x8EBC5) {
            calls=restores=true;
            const auto object=instruction.address==0x9B380?read_reg(ZYDIS_REGISTER_ECX):
                instruction.address==0x7BEDE?read_reg(ZYDIS_REGISTER_EDI):
                instruction.address==0x8EBC5?read_reg(ZYDIS_REGISTER_EBP)+"-0x38U":
                "ram.load<std::uint32_t>("+read_reg(ZYDIS_REGISTER_ESP)+
                (instruction.address==0x2E280 || instruction.address==0xADCA0 || instruction.address==0xA6130?"+8U)":"+4U)");
            body << "    if(cpu.presentation) {commit(); b2::title_presentation(cpu,memory,"
                 << constant(instruction.address) << ',' << object << "); restore();}\n";
        }
        if(instruction.address==0xADE20 || instruction.address==0xADE38) {
            calls=restores=true;
            body << "    if(cpu.game_timing) {commit(); b2::title_loading(cpu,memory,"
                 << (instruction.address==0xADE38?"true":"false") << "); restore();";
            if(instruction.address==0xADE38) body << " goto i_000ADE3D;";
            body << "}\n";
        }
        if(instruction.address==0x4D2A9) {
            if(hex_bytes(Bytes(instruction.bytes).first(ins.length))!="F60500B34C0004") throw std::runtime_error("Track menu signature changed");
            calls=restores=true;(void)read_reg(ZYDIS_REGISTER_ESI);
            body << "    commit(); b2::title_track(cpu,memory,0x0004D2A9U,esi); restore();\n";
        }
        if(instruction.address==0x4CCFD) {
            if(hex_bytes(Bytes(instruction.bytes).first(ins.length))!="8B7C8630") throw std::runtime_error("Track menu row signature changed");
            calls=restores=true;(void)read_reg(ZYDIS_REGISTER_ESI);
            body << "    commit(); b2::title_track(cpu,memory,0x0004CCFDU,esi); restore();\n";
        }
        if(instruction.address==0x4CEE0) {
            if(hex_bytes(Bytes(instruction.bytes).first(ins.length))!="B9B0854B00") throw std::runtime_error("Track confirmation signature changed");
            calls=restores=true;(void)read_reg(ZYDIS_REGISTER_ESI);
            body << "    commit(); b2::title_track(cpu,memory,0x0004CEE0U,esi-4U); restore();\n";
        }
        if(instruction.address==0xC91F7) {
            if(hex_bytes(Bytes(instruction.bytes).first(ins.length))!="A110C25700") throw std::runtime_error("Music file table signature changed");
            calls=restores=true;dirty.insert(7);used.insert(7);
            body << "    commit(); b2::title_track(cpu,memory,0x000C91F7U); restore();\n";
        }
        for(const auto& site:music_sites) if(instruction.address==site.address) {
            calls=restores=true;
            if(site.address==0x462DA || site.address==0x4788F || site.address==0x46EDA || site.address==0x21066E) (void)read_reg(ZYDIS_REGISTER_ESI);
            if(site.address==0x47190 || site.address==0x47220) (void)read_reg(ZYDIS_REGISTER_ECX);
            if(site.address==0x21066E) (void)read_reg(ZYDIS_REGISTER_EBP);
            body << "    commit(); ";
            if(site.repeat_target) body << "const auto repeat_music=";
            body << "b2::title_music(cpu,memory," << constant(site.address) << "); restore();\n";
            if(site.repeat_target) body << "    if(repeat_music) goto " << label(site.repeat_target) << ";\n";
        }
        if(instruction.address==0x90F68 || instruction.address==0x90F7E || instruction.address==0x90FDC ||
           instruction.address==0x914BF || instruction.address==0x9158F || instruction.address==0xA7306) {
            calls=restores=true;
            body << "    if(cpu.draw_distance>1) { commit(); b2::title_enhancement(cpu,memory,"
                 << constant(instruction.address) << "); restore(); }\n";
        }
        if(instruction.address==0xC31C4) {
            (void)read_reg(ZYDIS_REGISTER_ESP);
            body << "    if(cpu.draw_distance>1) ram.store<std::uint32_t>(esp+0x10U,std::bit_cast<std::uint32_t>(std::bit_cast<float>(ram.load<std::uint32_t>(esp+0x10U))/cpu.draw_distance));\n";
        }
        if(instruction.address==0xC3A2E) {
            (void)read_reg(ZYDIS_REGISTER_EBP);
            body << "    if(cpu.draw_distance>1) ram.store<std::uint32_t>(ebp-4U,std::bit_cast<std::uint32_t>(std::bit_cast<float>(ram.load<std::uint32_t>(ebp-4U))/cpu.draw_distance));\n";
        }
        if(instruction.address==0x9B3DC) {
            (void)read_reg(ZYDIS_REGISTER_ECX);
            // The traffic renderer recomputes both linear LOD thresholds from
            // the camera each pass. Scale those, not the shared vehicle state.
            body << "    if(cpu.draw_distance>1) for(const auto offset:{0x2E280U,0x2E284U}) ram.store<std::uint32_t>(ecx+offset,std::bit_cast<std::uint32_t>(std::bit_cast<float>(ram.load<std::uint32_t>(ecx+offset))*cpu.draw_distance));\n";
        }
        if(instruction.address==0x9BA2C) {
            (void)read_reg(ZYDIS_REGISTER_ECX);
            // Keep the original damaged/scripted-vehicle exemptions, material
            // alpha and fade curve. Only visual range follows the viewing camera.
            body << "    ram.store<std::uint32_t>(ecx+0x1E4U,b2::title_traffic_fade(cpu,memory,ecx));\n"
                 << "    goto i_0009BA8D;\n";
        }
        if(loop_headers.contains(instruction.address))
            body << "    if(b2::checkpoint_due(cpu)) { commit(); b2::checkpoint(cpu," << constant(instruction.address) << "); }\n";
        if(const auto run=floating_runs.find(instruction.address);run!=floating_runs.end()) {
            if(batchable_mmx_copy(instruction)) mmx_run(run->second);else x87_run(run->second);
        }
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
                if(!locked) body << "    ram.store<" << type(bits) << ">(location,static_cast<" << type(bits) << ">(a+b));\n";
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
                << "    ram.access(location,6);\n    ram.store<std::uint16_t>(location,cpu.gdt_limit);\n"
                << "    ram.store<std::uint32_t>(location+2,cpu.gdt_base);\n";
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
        case ZYDIS_MNEMONIC_MOVD: case ZYDIS_MNEMONIC_CVTPS2PI:
        case ZYDIS_MNEMONIC_PACKSSDW: case ZYDIS_MNEMONIC_PADDD:
        case ZYDIS_MNEMONIC_PSLLD: case ZYDIS_MNEMONIC_PSRAW: case ZYDIS_MNEMONIC_PSRLD:
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
            const auto mapped="ram.access("+address(memory_operand)+",8)";
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
            body << "    b2::stream_vector(ram," << address(operands[0]) << ',' << vector(operands[1]) << ");\n";
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
        case ZYDIS_MNEMONIC_MOV: case ZYDIS_MNEMONIC_MOVZX: {
            // The title also inlines its aspect setter during race setup/reset.
            constexpr std::pair<std::uint32_t,std::string_view> aspect_stores[]={
                {0xB8739,"C700398E633F"},{0xB8741,"C700398EE33F"},
                {0x7FF2E,"C705F4314400398EE33F"},{0x7FF38,"C70554384400398EE33F"},
                {0x7FF44,"C705F4314400398E633F"},{0x7FF4E,"C70554384400398E633F"},
                {0x804DC,"C705F4314400398EE33F"},{0x804E6,"C70554384400398EE33F"},
                {0x804F2,"C705F4314400398E633F"},{0x804FC,"C70554384400398E633F"}};
            const auto aspect=std::ranges::find(aspect_stores,instruction.address,&std::pair<std::uint32_t,std::string_view>::first);
            // Pause Options originally indexes five embedded rows. Follow its
            // descriptor so an extra row leaves the screen object intact.
            if(instruction.address==0x46DFC) body << "    const auto value = " << read(operands[0]) << ";\n";
            else if(instruction.address==0x46E04)
                body << "    const auto value = ram.load<std::uint32_t>(" << read_reg(ZYDIS_REGISTER_ESI) << "+0x80U)-1U;\n";
            else if(instruction.address==0x47C87)
                body << "    const auto value = ram.load<std::uint32_t>(ram.load<std::uint32_t>(" << read_reg(ZYDIS_REGISTER_ESI)
                     << "+0x84U)+" << read_reg(ZYDIS_REGISTER_EDX) << "*8U+0x10U);\n";
            else if(aspect!=std::end(aspect_stores)) {
                if(hex_bytes(Bytes(instruction.bytes).first(ins.length))!=aspect->second)
                    throw std::runtime_error("Camera aspect signature changed");
                // Keep vertical FOV and the original split-view layout; widen
                // the actual camera/frustum rather than stretching a 16:9 view.
                body << "    const auto value = cpu.ultrawide?" << (operands[1].imm.value.u==0x3F638E39?"0x3F955555U":"0x40155555U") << ':' << read(operands[1]) << ";\n";
            } else if(instruction.address==0xA97CE) {
                if(hex_bytes(Bytes(instruction.bytes).first(ins.length))!="8B01")
                    throw std::runtime_error("Motion blur enable signature changed");
                // Use the effect's own disabled path, including its x87 cleanup.
                // Its state is untouched and no history draw is submitted.
                body << "    const auto value = cpu.motion_blur?" << read(operands[1]) << ":0U;\n";
            } else if(instruction.address==0x8D40C || instruction.address==0x8D734) {
                const auto expected=instruction.address==0x8D40C?"B804000000":"B8C0000000";
                if(hex_bytes(Bytes(instruction.bytes).first(ins.length))!=expected)
                    throw std::runtime_error("World alpha reference signature changed");
                // Converted B1 materials retain their source alpha cutoff.
                // Keep the title's state cache and draw submission unchanged.
                body << "    const auto value = cpu.world_materials?cpu.world_materials->alpha(ram.load<std::uint32_t>("
                     << read_reg(ZYDIS_REGISTER_EBP) << "-4U)," << read(operands[1]) << "):" << read(operands[1]) << ";\n";
            } else if(instruction.address==0x43CD9 || instruction.address==0x43D2C || instruction.address==0x43D79) {
                if(hex_bytes(Bytes(instruction.bytes).first(ins.length))!="895C0204")
                    throw std::runtime_error("Online account menu availability signature changed");
                // Keep Sign In, New Account and Manage Account(s) visible with
                // the title's disabled flag, including its normal input guard.
                body << "    const auto value = 1U;\n";
            } else if(instruction.address==0x4CCFD)
                body << "    const auto value = ram.load<std::uint32_t>(ram.load<std::uint32_t>(" << read_reg(ZYDIS_REGISTER_ESI)
                     << "+0x14U)+" << read_reg(ZYDIS_REGISTER_EAX) << "*4U+4U);\n";
            else body << "    const auto value = " << read(operands[1]) << ";\n";
            write(operands[0], "value"); break;
        }
        case ZYDIS_MNEMONIC_MOVSX:
            body << "    const auto value = b2::sign_extend<" << operands[1].size << ">(" << read(operands[1]) << ");\n";
            type(operands[1].size); write(operands[0], "value"); break;
        case ZYDIS_MNEMONIC_LEA:
            if(instruction.address==0x46E84 || instruction.address==0x46EB6 || instruction.address==0x47C8E || instruction.address==0x47CE5)
                body << "    const auto value = std::uint32_t(ram.load<std::uint32_t>(" << read_reg(ZYDIS_REGISTER_ESI)
                     << "+0x84U)+" << read_reg(operands[1].mem.index) << "*8U);\n";
            else if(instruction.address==0x47CB4) body << "    const auto value = " << read_reg(ZYDIS_REGISTER_EAX) << ";\n";
            else body << "    const auto value = " << address(operands[1], true) << ";\n";
            write(operands[0], "value"); break;
        case ZYDIS_MNEMONIC_ADD: case ZYDIS_MNEMONIC_SUB: case ZYDIS_MNEMONIC_CMP:
        case ZYDIS_MNEMONIC_ADC: case ZYDIS_MNEMONIC_SBB:
        case ZYDIS_MNEMONIC_AND: case ZYDIS_MNEMONIC_OR: case ZYDIS_MNEMONIC_XOR: case ZYDIS_MNEMONIC_TEST: {
            if(instruction.address==0xC92D3) {
                if(hex_bytes(Bytes(instruction.bytes).first(ins.length))!="83FE0C") throw std::runtime_error("Burnout 1 playlist length signature changed");
                calls=restores=true;
                body << "    commit(); const auto music_count=b2::title_track(cpu,memory,0x000C92D3U)?1U:12U; restore();\n";
            }
            if(instruction.address==0x84850) {
                if(hex_bytes(Bytes(instruction.bytes).first(ins.length))!="837D0803") throw std::runtime_error("Respawn search limit signature changed");
                calls=restores=true;(void)read_reg(ZYDIS_REGISTER_EDX);
                // B1 can prohibit more than three consecutive respawn slots.
                body << "    commit(); const auto imported_respawn=b2::title_track(cpu,memory,0x00084850U); restore();\n";
            }
            body << "    const std::uint32_t a = " << read(operands[0]) << ";\n"
                 << "    const std::uint32_t b = " << (instruction.address==0x46DDF?
                    "ram.load<std::uint32_t>("+read_reg(ZYDIS_REGISTER_ESI)+"+0x80U)":instruction.address==0xC92D3?"music_count":
                    instruction.address==0x84850?"(imported_respawn?edx:3U)":read(operands[1])) << ";\n";
            const auto bits = operands[0].size;
            const auto m = ins.mnemonic;
            if (m == ZYDIS_MNEMONIC_ADD || m == ZYDIS_MNEMONIC_SUB || m == ZYDIS_MNEMONIC_CMP ||
                m == ZYDIS_MNEMONIC_ADC || m == ZYDIS_MNEMONIC_SBB) {
                const bool addition = m == ZYDIS_MNEMONIC_ADD || m == ZYDIS_MNEMONIC_ADC;
                const bool with_carry = m == ZYDIS_MNEMONIC_ADC || m == ZYDIS_MNEMONIC_SBB;
                if(m==ZYDIS_MNEMONIC_CMP) flags("sub_flags",bits);
                else {
                    type(bits);
                    uses_flags = writes_flags = true;
                    body << "    const auto result=b2::arithmetic<" << bits << ',' << (addition?"false":"true") << ">(flags,a,b"
                         << (with_carry?",flags&b2::carry":"") << ");\n    flags=result.flags;\n";
                    write(operands[0],"result.value");
                }
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
                 << "U;\n    ram.store<" << type(width) << ">(esp, static_cast<" << type(width) << ">(value));\n";
            break;
        }
        case ZYDIS_MNEMONIC_PUSHF: case ZYDIS_MNEMONIC_PUSHFD: {
            const unsigned width=ins.operand_width;
            if(width!=16 && width!=32) throw std::runtime_error("Unsupported flags stack width");
            used.insert(4); dirty.insert(4); uses_flags=true;
            body << "    esp-=" << width/8 << "U;\n    ram.store<" << type(width)
                << ">(esp,static_cast<" << type(width) << ">((flags&~0x30000U)|2U));\n";
            break;
        }
        case ZYDIS_MNEMONIC_POPF: case ZYDIS_MNEMONIC_POPFD: {
            const unsigned width=ins.operand_width;
            if(width!=16 && width!=32) throw std::runtime_error("Unsupported flags stack width");
            used.insert(4); dirty.insert(4); uses_flags=writes_flags=true;
            body << "    const auto value=ram.load<" << type(width) << ">(esp);\n    esp+=" << width/8
                << "U;\n    flags=b2::pop_flags(flags,value," << width << ",cpu.cs_selector&3U);\n";
            break;
        }
        case ZYDIS_MNEMONIC_POP: {
            const auto width = ins.operand_width;
            if (width != 16 && width != 32) throw std::runtime_error("Unsupported pop width");
            used.insert(4); dirty.insert(4);
            body << "    const auto value = ram.load<" << type(width) << ">(esp);\n    esp += " << width / 8 << "U;\n";
            write(operands[0], "value"); break;
        }
        case ZYDIS_MNEMONIC_CALL: {
            if (ins.operand_width != 32 || operands[0].type==ZYDIS_OPERAND_TYPE_POINTER)
                throw std::runtime_error("Only 32-bit near native calls are supported");
            calls = restores = true;
            const bool indirect=!instruction.target() && !instruction.kernel_ordinal;
            if(indirect) body << "    const auto target=" << read(operands[0]) << ";\n";
            used.insert(4); dirty.insert(4);
            body << "    esp -= 4U;\n    ram.store<std::uint32_t>(esp, " << constant(next)
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
            body << "    esp = ebp;\n    ebp = ram.load<std::uint32_t>(esp);\n    esp += 4U;\n";
            break;
        case ZYDIS_MNEMONIC_INC: case ZYDIS_MNEMONIC_DEC: {
            const bool increment = ins.mnemonic == ZYDIS_MNEMONIC_INC;
            type(operands[0].size);
            uses_flags = writes_flags = true;
            body << "    const auto a = " << read(operands[0]) << ";\n"
                 << "    const auto result=b2::arithmetic<" << operands[0].size << ',' << (increment?"false":"true") << ">(flags,a,1U);\n"
                 << "    flags=(result.flags&~b2::carry)|(flags&b2::carry);\n";
            write(operands[0],"result.value"); break;
        }
        case ZYDIS_MNEMONIC_NOT: case ZYDIS_MNEMONIC_NEG:
            body << "    const auto a = " << read(operands[0]) << ";\n";
            if (ins.mnemonic == ZYDIS_MNEMONIC_NEG) {
                type(operands[0].size);
                uses_flags = writes_flags = true;
                body << "    const auto result=b2::arithmetic<" << operands[0].size << ",true>(flags,0U,a);\n    flags=result.flags;\n";
            }
            write(operands[0], ins.mnemonic == ZYDIS_MNEMONIC_NOT ? "~a" : "result.value"); break;
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
            if(string_compare) body << "ram.load<" << type(bits) << ">(esi)";
            else body << "eax";
            body << ";\n    const auto b = ram.load<" << type(bits) << ">(edi);\n"
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
            body << "    const auto return_address = ram.load<std::uint32_t>(esp);\n"
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
        if (!terminator && !falls_through) body << "    goto " << label(next) << ";\n";
        body << "}\n";
    }
};
}

LoweredFunction lower(const Function& function,bool fuse_floating,bool portable) {
    LoweredFunction result;
    auto api=std::ranges::find(native_apis,function.entry,&NativeApi::address);
    if(api==native_apis.end()) {
        const auto entry=function.instructions.find(function.entry);
        // Recovery follows an SDK jump thunk into its target. Keep that
        // native binding instead of translating the old hardware path.
        if(entry!=function.instructions.end() && entry->second.decoded.mnemonic==ZYDIS_MNEMONIC_JMP && entry->second.target())
            api=std::ranges::find(native_apis,*entry->second.target(),&NativeApi::address);
    }
    if(api!=native_apis.end()) {
        std::string prefix;auto pc=api->address;
        while(prefix.size()<api->prefix.size() && function.instructions.contains(pc)) {
            const auto& instruction=function.instructions.at(pc);
            prefix+=hex_bytes(Bytes(instruction.bytes).first(instruction.decoded.length));pc+=instruction.decoded.length;
        }
        if(prefix!=api->prefix) throw std::runtime_error("Native API entry signature changed: "+std::string(api->name));
        result.native_api=api->name;
        result.source=std::format("void native_platform(Cpu&,Memory&,std::uint32_t);\nvoid guest_{:08X}(Cpu& cpu,Memory& memory) {{\n"
            "    if(b2::checkpoint_due(cpu)) b2::checkpoint(cpu,0x{:08X}U);\n    native_platform(cpu,memory,0x{:08X}U);\n}}\n",function.entry,function.entry,api->address);
        return result;
    }
    Emitter emitter;
    emitter.portable=portable;
    emitter.labels.insert(function.entry);
    for(const auto& site:music_sites) if(const auto found=function.instructions.find(site.address);found!=function.instructions.end()) {
        if(hex_bytes(Bytes(found->second.bytes).first(found->second.decoded.length))!=site.bytes ||
           (site.repeat_target && !function.instructions.contains(site.repeat_target)))
            throw std::runtime_error("Original music menu or playlist boundary changed");
        emitter.labels.insert(site.address);
        if(site.repeat_target) emitter.labels.insert(site.repeat_target);
    }
    for(const auto& [address,bytes]:pause_row_sites) if(const auto found=function.instructions.find(address);found!=function.instructions.end()) {
        if(hex_bytes(Bytes(found->second.bytes).first(found->second.decoded.length))!=bytes)
            throw std::runtime_error("Original pause Options row access changed");
    }
    for(const auto& gate:timing_gates) if(const auto found=function.instructions.find(gate.address);found!=function.instructions.end()) {
        if(hex_bytes(Bytes(found->second.bytes).first(found->second.decoded.length))!=gate.bytes ||
           !function.instructions.contains(gate.target))
            throw std::runtime_error("Original scene update/render boundary changed");
        emitter.labels.insert(gate.address);emitter.labels.insert(gate.target);
    }
    if(function.instructions.contains(0x11036)) emitter.labels.insert(0x11144);
    for(const auto& [address,bytes]:presentation_sites) if(const auto found=function.instructions.find(address);found!=function.instructions.end()) {
        if(hex_bytes(Bytes(found->second.bytes).first(found->second.decoded.length))!=bytes)
            throw std::runtime_error("Original presentation/loading instruction signature changed");
        emitter.labels.insert(address);
    }
    // These edits apply only to the verified US title's camera, world visibility
    // scenery fading and traffic LOD. Collision and spawning remain original.
    constexpr std::pair<std::uint32_t,std::string_view> enhancement_sites[]={
        {0x1D121,"DDD8"},{0x26870,"F6C204"},{0xC1306,"FF5650"},{0xC1309,"8B36"},{0xC11ED,"E87E1B0000"},{0xC14C7,"C20800"},
        {0x90F68,"B9B0854B00"},{0x90F7E,"8B8618010000"},{0x90FDC,"8B8618010000"},
        {0x914BF,"8B8618010000"},{0x9158F,"5F"},
        {0x9B3DC,"8D642400"},{0xA7306,"C20800"},
        {0xC31C4,"D9442410"},{0xC3A2E,"D945FC"},
        {0x9BA15,"8B4E0C"},{0x9BA18,"8B81E8010000"},{0x9BA24,"3991A0010000"},
        {0x9BA2C,"8B4508"},{0x9BA42,"8D9C98E08E0200"},{0x9BA49,"D903"},
        {0x9BA6D,"D835D01A2C00"},{0x9BA73,"D999E4010000"},{0x9BA8D,"D981E0010000"},
        {0x9BEBC,"8B15B03E4400"},{0x9BEC4,"8B4804"},{0x9BEC7,"83C140"}};
    for(const auto& [address,bytes]:enhancement_sites) if(const auto found=function.instructions.find(address);found!=function.instructions.end()) {
        if(hex_bytes(Bytes(found->second.bytes).first(found->second.decoded.length))!=bytes)
            throw std::runtime_error("World enhancement instruction signature changed");
        emitter.labels.insert(address);
    }
    for(const auto& edge : function.edges) {
        if(!edge.to) continue;
        emitter.labels.insert(*edge.to);
        if((edge.kind=="jump" || edge.kind=="conditional" || edge.kind=="table-jump") && *edge.to<=edge.from)
            emitter.loop_headers.insert(*edge.to);
    }
    // Consecutive instructions execute in source order. Only real branches and
    // non-contiguous successors need labels and explicit jumps.
    for(auto current=function.instructions.begin();current!=function.instructions.end();++current) {
        const auto next=current->first+current->second.decoded.length;
        const auto following=std::next(current);
        if(following==function.instructions.end() || following->first!=next) emitter.labels.insert(next);
    }
    emitter.calls=!emitter.loop_headers.empty();
    if(fuse_floating && !portable) {
        auto targets=emitter.labels;
        for(const auto& edge:function.edges) if(edge.to) targets.insert(*edge.to);
        for(auto current=function.instructions.begin();current!=function.instructions.end();) {
            std::vector<const Instruction*> run;
            auto next=current;
            const bool mmx=batchable_mmx_copy(current->second);
            while(next!=function.instructions.end() && run.size()<32 && (mmx?batchable_mmx_copy(next->second):batchable_x87(next->second)) &&
                  (run.empty() || (!targets.contains(next->first) && next->first==run.back()->address+run.back()->decoded.length))) {
                run.push_back(&next->second);++next;
            }
            if(run.size()>=4) {
                emitter.labels.insert(run.back()->address+run.back()->decoded.length);
                emitter.floating_runs.emplace(current->first,std::move(run));
            }
            current=next==current?std::next(current):next;
        }
    }
    for(auto current=function.instructions.begin();current!=function.instructions.end();++current) {
        const auto& [pc,instruction]=*current;
        const auto following=std::next(current);
        const bool falls_through=following!=function.instructions.end() && following->first==pc+instruction.decoded.length;
        try { emitter.instruction(instruction,falls_through); }
        catch (const std::exception& error) {
            result.issues.push_back({pc, hex_bytes(Bytes(instruction.bytes).first(instruction.decoded.length)),
                                     instruction.text, error.what()});
        }
    }
    if (!result.issues.empty()) return result;
    std::ostringstream out;
    for(const auto& [name,declaration] : emitter.native_declarations) out << declaration;
    out << "void guest_" << std::format("{:08X}", function.entry) << "(Cpu& cpu, [[maybe_unused]] Memory& memory) {\n";
    out << "    [[maybe_unused]] const auto ram=memory.primary();\n";
    out << "    if(b2::checkpoint_due(cpu)) b2::checkpoint(cpu," << constant(function.entry) << ");\n";
    if(function.entry==0x4C9F0) {
        std::string prefix;auto pc=function.entry;
        while(prefix.size()<30 && function.instructions.contains(pc)) {
            const auto& instruction=function.instructions.at(pc);
            prefix+=hex_bytes(Bytes(instruction.bytes).first(instruction.decoded.length));pc+=instruction.decoded.length;
        }
        if(prefix!="558BECD94508568BF18B4608D84604") throw std::runtime_error("Intro update signature changed");
        out << "    b2::skip_title_intro(cpu,memory);\n";
    }
    if(function.entry==0x4CC40) {
        if(hex_bytes(Bytes(function.instructions.at(function.entry).bytes).first(3))!="8B4110")
            throw std::runtime_error("Track selection signature changed");
        out << "    b2::title_track(cpu,memory,0x0004CC40U,cpu.registers[b2::ecx]);\n";
    }
    if(function.entry==0x18030) {
        std::string prefix;auto pc=function.entry;
        while(prefix.size()<22 && function.instructions.contains(pc)) {
            const auto& instruction=function.instructions.at(pc);
            prefix+=hex_bytes(Bytes(instruction.bytes).first(instruction.decoded.length));pc+=instruction.decoded.length;
        }
        if(prefix!="558BEC51833D48BB2F0001") throw std::runtime_error("Race record signature changed");
        out << "    if(b2::title_track(cpu,memory,0x00018030U)) { cpu.eip=ram.load<std::uint32_t>(cpu.registers[b2::esp]); cpu.registers[b2::esp]+=8; return; }\n";
    }
    if(function.entry==0xB5100) {
        if(hex_bytes(Bytes(function.instructions.at(function.entry).bytes).first(3))!="83EC10")
            throw std::runtime_error("Replay camera zoom signature changed");
        out << "    if(b2::title_track(cpu,memory,0x000B5100U,cpu.registers[b2::ecx])) { cpu.eip=ram.load<std::uint32_t>(cpu.registers[b2::esp]); cpu.registers[b2::esp]+=8; return; }\n";
    }
    constexpr std::pair<std::uint32_t,std::string_view> track_entries[]={
        {0x8BCD0,"568BF18B4608"},{0x90D30,"558BEC83EC208B4508"},{0xC93C0,"83EC24B808000000"}};
    for(const auto& [entry,bytes]:track_entries) if(function.entry==entry) {
        std::string prefix;auto pc=entry;
        while(prefix.size()<bytes.size() && function.instructions.contains(pc)) {
            const auto& instruction=function.instructions.at(pc);
            prefix+=hex_bytes(Bytes(instruction.bytes).first(instruction.decoded.length));pc+=instruction.decoded.length;
        }
        if(prefix!=bytes) throw std::runtime_error("Imported track entry signature changed");
        out << "    b2::title_track(cpu,memory," << constant(entry) << ",cpu.registers[b2::ecx]);\n";
    }
    constexpr std::pair<std::uint32_t,std::string_view> enhancement_entries[]={
        {0x8EEB0,"8B5110568B7124"},{0xE8450,"8B44240489442404"},
        {0xA6E00,"558BEC51A1B03E4400"}};
    for(const auto& [entry,bytes]:enhancement_entries) if(function.entry==entry) {
        std::string prefix;auto pc=entry;
        while(prefix.size()<bytes.size() && function.instructions.contains(pc)) {
            const auto& instruction=function.instructions.at(pc);
            prefix+=hex_bytes(Bytes(instruction.bytes).first(instruction.decoded.length));pc+=instruction.decoded.length;
        }
        if(prefix!=bytes) throw std::runtime_error("World enhancement entry signature changed");
        out << "    if(cpu.draw_distance>1) b2::title_enhancement(cpu,memory," << constant(entry) << ");\n";
    }
    constexpr std::uint32_t timed_functions[]={0x0021F9C0,0x00227BD9,0x00219750,0x000ED570,0x00109B40,
        0x00090420,0x00090660,0x00090A50,0x000BDEE0,0x0010DA90,0x00219370};
    if(std::ranges::find(timed_functions,function.entry)!=std::end(timed_functions))
        out << "    std::optional<b2::PerfScope> timing;\n    if(cpu.profile) timing.emplace(cpu.profile,b2::PerfPhase::guest," << constant(function.entry) << ");\n";
    if(function.entry==0x1CFE0) {
        if(hex_bytes(Bytes(function.instructions.at(function.entry).bytes).first(1))!="55")
            throw std::runtime_error("HUD layout entry signature changed");
        out << "    if(cpu.hud_layout) cpu.hud_layout->active=0;\n";
    }
    if(function.entry==0x1D5C0) {
        if(hex_bytes(Bytes(function.instructions.at(function.entry).bytes).first(4))!="8B44240C")
            throw std::runtime_error("HUD driving-message entry signature changed");
        // Centred driving messages keep their screen position. Right-aligned
        // near-miss/traffic messages belong to the boost meter and must retain
        // its local scale; the original constructor still aligns each box.
        out << "    if(cpu.hud_layout) {\n"
            << "    const auto sp=cpu.registers[b2::esp],manager=ram.load<std::uint32_t>(sp+4U);\n"
            << "    const auto queue=ram.load<std::uint32_t>(manager+60U)+ram.load<std::uint32_t>(sp+8U)*16U;\n"
            << "    const auto scale=cpu.hud_layout->transform(queue)[0];\n"
            << "    if(scale!=1 && ram.load<std::uint32_t>(sp+24U)==0x3F000000U) ram.store<std::uint32_t>(sp+16U,std::bit_cast<std::uint32_t>(std::bit_cast<float>(ram.load<std::uint32_t>(sp+16U))/scale));\n    }\n";
    }
    if(function.entry==0xC1210 || function.entry==0xC1160)
        out << "    const auto hud_transform=cpu.hud_layout?cpu.hud_layout->transform(cpu.registers[b2::ecx]):std::array<float,2>{1,0};\n";
    if(function.entry==0xC1210)
        out << "    std::uint32_t ui_blur_value=0;bool ui_blur=false;\n";
    if(function.entry==0xC1160)
        // The original clip cache compares only the rectangle pointer. The
        // same rectangle at another widget anchor also needs a state update.
        out << "    if(cpu.hud_layout) {\n"
            << "    if(hud_transform!=cpu.hud_layout->clip_transform && ram.load<std::uint32_t>(cpu.registers[b2::ecx]+12U)) ram.store<std::uint32_t>(cpu.registers[b2::esp]+4U,0);\n"
            << "    cpu.hud_layout->clip_transform=hud_transform;\n    }\n";
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
