#include "dsp_compiler.h"
#include <algorithm>
#include <format>
#include <map>
#include <set>
#include <sstream>
#include <stdexcept>

namespace b2 {
namespace {
// Encoding fields follow the DSP56300 Family Manual. Only the forms present in
// this title's effects are emitted; every unknown word is a build-time error.
struct Pattern {
    std::string_view bits;
    std::uint32_t mask=0,value=0;
    explicit Pattern(std::string_view text):bits(text) {
        if(bits.size()!=24) throw std::runtime_error("Invalid DSP encoding pattern");
        for(char bit:bits){mask=(mask<<1)|(bit=='0' || bit=='1');value=(value<<1)|(bit=='1');}
    }
    bool matches(std::uint32_t opcode) const{return (opcode&mask)==value;}
    unsigned field(char name,std::uint32_t opcode) const {
        unsigned result=0;
        for(unsigned i=0;i<24;++i) if(bits[i]==name) result=(result<<1)|((opcode>>(23-i))&1);
        return result;
    }
};
std::string literal(std::uint32_t value){return std::format("0x{:X}U",value);}
std::int32_t signed24(std::uint32_t value){return static_cast<std::int32_t>(value<<8)>>8;}
std::string reg(unsigned value){return std::format("d.read({})",value);}
std::string immediate(std::uint32_t value){return "(static_cast<std::int64_t>(Dsp::signed24("+literal(value)+"))*0x1000000)";}
std::string condition(unsigned value) {
    switch(value) {case 1:return "!d.less()";case 9:return "d.less()";case 10:return "d.zero";default:throw std::runtime_error("Unbound DSP condition code");}
}
std::uint64_t fingerprint(Bytes bytes) {
    std::uint64_t result=14695981039346656037ULL;
    for(auto byte:bytes){result^=std::to_integer<unsigned>(byte);result*=1099511628211ULL;}
    return result;
}
enum class Flow {next,jump,branch,call,loop,ret};
struct Instruction {
    unsigned address=0,size=1,target=0;
    std::uint32_t opcode=0;
    std::string body,test,count;
    Flow flow=Flow::next;
};
std::string alu(std::uint32_t byte) {
    if(!byte) return {};
    const auto destination=(byte>>3)&1U;
    if(byte&128) {
        constexpr unsigned pairs[][2]={{4,4},{6,6},{5,4},{7,6},{4,7},{6,4},{5,6},{7,5}};
        const auto& pair=pairs[(byte>>4)&7U];
        return std::format("d.multiply({}, {}, {}, {}, {}, {});",destination,reg(pair[0]),reg(pair[1]),
            (byte&2)!=0,(byte&1)!=0,(byte&4)!=0);
    }
    const auto kind=(byte>>4)&7U,operation=byte&7U;
    if(kind==1 && operation==3) return std::format("d.clear({});",destination);
    if(kind==2 && operation==2) return std::format("d.shift({0},{0},1,true);",destination);
    if(kind==3 && operation==2) return std::format("d.shift({0},{0},1);",destination);
    if(kind==3 && operation==6) return std::format("d.negate({});",destination);
    if((kind==1 || kind>=4) && (operation==0 || operation==4)) {
        constexpr unsigned sources[]={4,6,5,7};
        const auto source=kind==1?std::format("d.accumulators[{}]",1-destination):std::format("d.operand({})",sources[kind-4]);
        return std::format("d.add({}, {}, {});",destination,source,operation==4);
    }
    throw std::runtime_error("Unbound DSP parallel arithmetic");
}
Instruction decode(Bytes bytes,unsigned pc) {
    Instruction out;out.address=pc;out.opcode=u32(bytes,pc*4);
    const auto opcode=out.opcode;
    if(opcode&0xFF000000) throw std::runtime_error("DSP program has a non-24-bit word");
    const auto extension=[&](){out.size=2;return u32(bytes,(pc+1)*4)&0xFFFFFF;};
    const auto match=[&](std::string_view text){return Pattern(text).matches(opcode);};
    const auto field=[&](std::string_view text,char name){return Pattern(text).field(name,opcode);};
    const auto move=[&](unsigned destination,std::string source){return std::format("d.write({}, {});",destination,source);};
    const auto memory_move=[&](unsigned number,std::string address,bool read) {
        return read?move(number,"d.load("+address+")"):"d.store("+address+", "+reg(number)+");";
    };
    if(opcode==0) return out;
    if(opcode==12){out.flow=Flow::ret;return out;}
    if(opcode>=0x100000) {
        const auto group=opcode>>20,low=opcode&255U,upper=opcode>>8;
        out.body=alu(low);
        if(group==2 || group==3) {
            if(upper==0x2000) return out;
            if((upper&0xFFF0)==0x2020) {out.body="if("+condition(upper&15)+") {"+out.body+"}";return out;}
            if((upper&0xFC00)==0x2000) {
                if(low) throw std::runtime_error("DSP parallel register/ALU conflict needs verification");
                out.body=move((opcode>>8)&31,reg((opcode>>13)&31));return out;
            }
            auto value=(opcode>>8)&255U;const auto destination=(opcode>>16)&31U;
            if(destination==4 || destination==5 || destination==6 || destination==7 || destination==14 || destination==15) value<<=16;
            out.body+=move(destination,literal(value));return out;
        }
        if(group>=4 && group<=7) {
            if(low) throw std::runtime_error("DSP parallel memory/ALU conflict needs verification");
            const auto number=((opcode>>16)&7U)|((opcode>>17)&24U);
            if(number<4 || (opcode&(1U<<19))) throw std::runtime_error("Unbound DSP memory space");
            const bool read=(opcode&0x8000)!=0;
            const auto ea=(opcode>>8)&63U,mode=ea>>3,index=ea&7U;
            std::string address;
            if(!(opcode&0x4000)) address=literal(ea);
            else if(mode==6) {
                const auto value=extension();
                if(index==4 && read){out.body=move(number,literal(value));return out;}
                if(index!=0) throw std::runtime_error("Unbound DSP extended address");
                address=literal(value);
            } else address=std::format("d.ea({},{})",index,mode);
            out.body=memory_move(number,address,read);return out;
        }
        throw std::runtime_error("Unbound DSP parallel move class");
    }
    constexpr std::string_view short_displacement="0000001aaaaaaRRR1asWDDDD",long_displacement="0000101s01110RRR1WDDDDDD";
    if(match(short_displacement) || match(long_displacement)) {
        const auto pattern=match(short_displacement)?short_displacement:long_displacement;
        if(field(pattern,'s')) throw std::runtime_error("Unbound DSP Y memory");
        const auto raw=pattern==short_displacement?field(pattern,'a'):extension();
        const auto offset=pattern==short_displacement?static_cast<std::int32_t>(raw<<25)>>25:signed24(raw);
        const auto address=std::format("({}+{})",reg(16+field(pattern,'R')),literal(static_cast<std::uint32_t>(offset)));
        out.body=memory_move(field(pattern,'D'),address,field(pattern,'W')!=0);return out;
    }
    constexpr std::string_view movec_reg="00000100W1eeeeee101ddddd",movec_ea="00000101W1MMMRRR0s1ddddd";
    if(match(movec_reg)) {
        auto source=field(movec_reg,'e'),destination=opcode&63U;
        if(!field(movec_reg,'W')) std::swap(source,destination);
        out.body=move(destination,reg(source));return out;
    }
    if(match(movec_ea)) {
        if(field(movec_ea,'M')!=6 || field(movec_ea,'R')!=4 || !field(movec_ea,'W'))
            throw std::runtime_error("Unbound DSP control-register address");
        out.body=move(opcode&63U,literal(extension()));return out;
    }
    constexpr std::string_view movep_ea="0000100sW1MMMRRR1Spppppp",movep_reg="0000100sW1dddddd00pppppp";
    if(match(movep_ea) || match(movep_reg)) {
        const auto pattern=match(movep_ea)?movep_ea:movep_reg;
        if(field(pattern,'s') || !field(pattern,'W')) throw std::runtime_error("Unbound DSP peripheral transfer");
        const auto source=pattern==movep_reg?reg(field(pattern,'d')):literal(extension());
        if(pattern==movep_ea && (field(pattern,'M')!=6 || field(pattern,'R')!=4))
            throw std::runtime_error("Unbound DSP peripheral source");
        out.body="d.store("+literal(0xFFFFC0+field(pattern,'p'))+", "+source+");";return out;
    }
    constexpr std::string_view bit_clear="0000101011DDDDDD010bbbbb",bit_set="0000101011DDDDDD011bbbbb";
    if(match(bit_clear) || match(bit_set)) {
        const auto pattern=match(bit_clear)?bit_clear:bit_set;
        const auto number=field(pattern,'D'),mask=1U<<field(pattern,'b');
        out.body=move(number,reg(number)+(pattern==bit_set?" | ":" & ")+literal(pattern==bit_set?mask:~mask));return out;
    }
    constexpr std::string_view branch_clear_reg="0000110011DDDDDD100bbbbb",branch_set_reg="0000110011DDDDDD101bbbbb",
        branch_clear_port="0000110011pppppp0S0bbbbb",branch_set_port="0000110011pppppp0S1bbbbb";
    for(const auto pattern:{branch_clear_reg,branch_set_reg,branch_clear_port,branch_set_port}) if(match(pattern)) {
        out.flow=Flow::branch;out.target=(pc+extension())&0xFFFFFF;
        const bool port=pattern==branch_clear_port || pattern==branch_set_port,set=pattern==branch_set_reg || pattern==branch_set_port;
        if(port && field(pattern,'S')) throw std::runtime_error("Unbound DSP Y peripheral");
        const auto source=port?"d.load("+literal(0xFFFFC0+field(pattern,'p'))+")":reg(field(pattern,'D'));
        out.test="(("+source+" & "+literal(1U<<field(pattern,'b'))+") "+(set?"!=":"==")+" 0)";return out;
    }
    if(opcode==0x0D1080 || opcode==0x0D10C0 || (opcode&0xFFFFF0)==0x0D1040) {
        out.target=(pc+extension())&0xFFFFFF;
        if(opcode==0x0D1080) out.flow=Flow::call;
        else if(opcode==0x0D10C0) out.flow=Flow::jump;
        else {out.flow=Flow::branch;out.test=condition(opcode&15);}
        return out;
    }
    constexpr std::string_view call_short="00000101000010aaaa0aaaaa",jump_short="00000101000011aaaa0aaaaa";
    if(match(call_short) || match(jump_short)) {
        const auto pattern=match(call_short)?call_short:jump_short;
        const auto displacement=static_cast<std::int32_t>(field(pattern,'a')<<23)>>23;
        out.target=(pc+static_cast<std::uint32_t>(displacement))&0xFFFFFF;
        out.flow=pattern==call_short?Flow::call:Flow::jump;return out;
    }
    constexpr std::string_view loop_imm="00000110iiiiiiii1001hhhh",loop_reg="0000011011DDDDDD00010000";
    if(match(loop_imm) || match(loop_reg)) {
        const auto pattern=match(loop_imm)?loop_imm:loop_reg;
        out.target=(pc+extension()+1)&0xFFFFFF;out.flow=Flow::loop;
        out.count=pattern==loop_imm?literal(field(pattern,'i')|(field(pattern,'h')<<8)):reg(field(pattern,'D'));
        return out;
    }
    constexpr std::string_view lua_relative="0000010000aaaRRRaaaadddd";
    if(match(lua_relative)) {
        const auto raw=field(lua_relative,'a');const auto offset=static_cast<std::int32_t>(raw<<25)>>25;
        const auto destination=((opcode&8)?24:16)+(opcode&7);
        out.body=move(destination,reg(16+field(lua_relative,'R'))+" + "+literal(static_cast<std::uint32_t>(offset)));return out;
    }
    constexpr std::string_view asl_imm="0000110000011101SiiiiiiD",asl_reg="0000110000011110010SsssD";
    if(match(asl_imm) || match(asl_reg)) {
        const auto pattern=match(asl_imm)?asl_imm:asl_reg;
        constexpr unsigned sources[]={0,0,12,13,4,6,5,7};
        const auto count=pattern==asl_imm?literal(field(pattern,'i')):"("+reg(sources[field(pattern,'s')])+"&63U)";
        out.body=std::format("d.shift({},{},{});",field(pattern,'S'),field(pattern,'D'),count);return out;
    }
    constexpr std::string_view mpy_immediate="000000010100000111qqdk00";
    if(match(mpy_immediate)) {
        constexpr unsigned sources[]={4,6,5,7};
        out.body=std::format("d.multiply({}, {}, {}, false, false, {});",field(mpy_immediate,'d'),literal(extension()),
            reg(sources[field(mpy_immediate,'q')]),field(mpy_immediate,'k')!=0);return out;
    }
    for(const auto operation:{0U,4U,5U,6U}) {
        const auto short_pattern=std::format("0000000101iiiiii1000d{:03b}",operation);
        const auto long_pattern=std::format("00000001010000001100d{:03b}",operation);
        if(match(short_pattern) || match(long_pattern)) {
            const auto pattern=match(short_pattern)?std::string_view(short_pattern):std::string_view(long_pattern);
            const auto value=pattern==short_pattern?field(pattern,'i'):extension(),destination=field(pattern,'d');
            if(operation==6) out.body=std::format("d.logical_and({},{});",destination,literal(value));
            else out.body=std::format("d.add({}, {}, {}, {});",destination,immediate(value),operation!=0,operation!=5);
            return out;
        }
    }
    throw std::runtime_error("Unbound DSP instruction word");
}
std::string routine_name(unsigned effect,unsigned entry){return std::format("effect_{}_{:X}",effect,entry);}
std::string label(unsigned address){return std::format("L{:X}",address);}
}
std::string compile_effects(const std::filesystem::path& source,const std::filesystem::path& output,bool replace) {
    File file(source);if(file.size()>65536) throw std::runtime_error("DSP image exceeds 64 KiB build limit");
    const auto image=decode_effects(file.read(0,static_cast<std::size_t>(file.size())));
    std::ostringstream code,report;unsigned total=0;
    code<<"// Generated from local game media by the native DSP compiler.\n#include \"dsp.h\"\n#include <stdexcept>\nnamespace b2 {\nnamespace {\n";
    report<<"{\"format\":\"b2-static-effects-v1\",\"source_sha256\":"<<json(hash(file,0,file.size()))<<",\"effects\":[";
    for(unsigned effect=0;effect<image.effects.size();++effect) {
        const auto& program=image.effects[effect];std::map<unsigned,Instruction> instructions;std::set<unsigned> entries{0};
        for(unsigned pc=0;pc<program.code.size()/4;) {
            Instruction instruction;
            try {instruction=decode(program.code,pc);}
            catch(const std::exception& error){throw std::runtime_error(std::format("Effect {} P:{:06X} {:06X}: {}",effect,pc,u32(program.code,pc*4),error.what()));}
            if(instruction.flow==Flow::call) entries.insert(instruction.target);
            pc+=instruction.size;instructions.emplace(instruction.address,std::move(instruction));
        }
        const auto end=static_cast<unsigned>(program.code.size()/4);
        for(const auto& [pc,instruction]:instructions) if(instruction.flow!=Flow::next && instruction.flow!=Flow::ret &&
            !instructions.contains(instruction.target) && instruction.target!=end)
            throw std::runtime_error(std::format("Effect {} P:{:06X} targets an instruction interior",effect,pc));
        for(auto entry:entries) code<<"void "<<routine_name(effect,entry)<<"(Dsp&);\n";
        for(auto entry:entries) {
            std::set<unsigned> reached;std::vector<unsigned> pending{entry};
            bool reaches_end=false;
            while(!pending.empty()) {
                const auto pc=pending.back();pending.pop_back();
                if(pc==end){reaches_end=true;continue;}
                if(!reached.insert(pc).second) continue;
                const auto& instruction=instructions.at(pc);
                if(instruction.flow!=Flow::ret && instruction.flow!=Flow::jump) pending.push_back(pc+instruction.size);
                if(instruction.flow==Flow::jump || instruction.flow==Flow::branch || instruction.flow==Flow::loop) pending.push_back(instruction.target);
            }
            std::map<unsigned,std::vector<unsigned>> loop_ends;
            code<<"void "<<routine_name(effect,entry)<<"(Dsp& d) {\n(void)d;\n";
            for(auto pc:reached) if(instructions.at(pc).flow==Flow::loop) {
                loop_ends[instructions.at(pc).target].push_back(pc);
                code<<"unsigned loop_"<<pc<<"=0;\n";
            }
            code<<"goto "<<label(entry)<<";\n";
            for(auto pc:reached) {
                const auto& instruction=instructions.at(pc);
                code<<label(pc)<<":\n";
                for(auto origin:loop_ends[pc]) code<<"if(loop_"<<origin<<" && --loop_"<<origin<<") {d.guard("<<pc<<");goto "<<label(origin+instructions.at(origin).size)<<";}\n";
                code<<"{ "<<instruction.body<<" }\n";
                switch(instruction.flow) {
                case Flow::ret:code<<"return;\n";continue;
                case Flow::jump:if(instruction.target<=pc) code<<"d.guard("<<pc<<");\n";code<<"goto "<<label(instruction.target)<<";\n";continue;
                case Flow::branch:code<<"if("<<instruction.test<<") {";
                    if(instruction.target<=pc) code<<"d.guard("<<pc<<");";
                    code<<"goto "<<label(instruction.target)<<";}\n";break;
                case Flow::call:code<<routine_name(effect,instruction.target)<<"(d);\n";break;
                case Flow::loop:code<<"loop_"<<pc<<"="<<instruction.count<<"&0xFFFFU; if(!loop_"<<pc<<") goto "<<label(instruction.target)<<";\n";break;
                default:break;
                }
                code<<"goto "<<label(pc+instruction.size)<<";\n";
            }
            if(reaches_end) code<<label(end)<<":return;\n";
            code<<"}\n";
        }
        if(effect) report<<',';
        report<<"{\"index\":"<<effect<<",\"words\":"<<program.code.size()/4<<",\"instructions\":"<<instructions.size()<<",\"routines\":"<<entries.size()<<'}';
        total+=static_cast<unsigned>(instructions.size());
    }
    code<<"}\nbool compiled_effects_match(const EffectsImage& image) {\nif(image.effects.size()!="<<image.effects.size()<<") return false;\n";
    for(unsigned i=0;i<image.effects.size();++i) {
        code<<"{const auto& bytes=image.effects["<<i<<"].code; if(bytes.size()!="<<image.effects[i].code.size()<<") return false;\n"
            <<"std::uint64_t hash=14695981039346656037ULL;for(auto byte:bytes){hash^=std::to_integer<unsigned>(byte);hash*=1099511628211ULL;}\n"
            <<"if(hash!="<<fingerprint(image.effects[i].code)<<"ULL) return false;}\n";
    }
    code<<"return true;\n}\nvoid run_effect(Dsp& d,unsigned index) {d.active_effect=index;d.work_remaining=100000;switch(index){\n";
    for(unsigned i=0;i<image.effects.size();++i) code<<"case "<<i<<":"<<routine_name(i,0)<<"(d);return;\n";
    code<<"default:throw std::runtime_error(\"Invalid static DSP effect\");}}\n}\n";
    write_text(output,code.str(),replace);
    report<<"],\"instructions\":"<<total<<",\"runtime_decoder\":false,\"unsupported\":0}";return report.str();
}
}
