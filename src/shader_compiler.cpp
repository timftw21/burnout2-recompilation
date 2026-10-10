#include "shaders.h"
#include "xbe.h"
#include "gui_shaders.h"
#include <d3dcompiler.h>
#include <wrl/client.h>
#include <algorithm>
#include <format>
#include <map>
#include <sstream>
#include <stdexcept>

namespace b2 {
namespace {
using Microsoft::WRL::ComPtr;
struct Key {
    ShaderKind kind;VertexProgram program{};
    auto operator<=>(const Key&) const = default;
};
std::map<Key,std::vector<std::byte>> bytecode;
std::string source(ShaderKind kind,const VertexProgram* program) {
    if(kind==ShaderKind::vertex || kind==ShaderKind::launch) {
        if(!program) throw std::runtime_error("A vertex shader requires its original program");
        return translate_vertex_program(*program,kind==ShaderKind::launch).source;
    }
    if(kind==ShaderKind::gui_vertex) return std::string(gui_vertex_source);
    if(kind==ShaderKind::gui_pixel) return std::string(gui_pixel_source);
    return shader_source(kind);
}
void array(std::ostringstream& output,std::string_view name,Bytes bytes) {
    output<<"alignas(4) const unsigned char "<<name<<"[]={\n";
    for(std::size_t i=0;i<bytes.size();++i) {
        if(i && i%24==0) output<<'\n';
        output<<std::format("0x{:02X},",std::to_integer<unsigned>(bytes[i]));
    }
    output<<"\n};\n";
}
void program(std::ostringstream& output,const StaticVertex& value) {
    output<<"StaticVertex{VertexProgram{VertexTokens{{";
    for(const auto& token:value.program.tokens) {
        output<<std::format("{{0x{:08X},0x{:08X},0x{:08X},0x{:08X}}},",token[0],token[1],token[2],token[3]);
        if(token[3]&1) break;
    }
    output<<"}},0},"<<(value.launch?"true":"false")<<"},\n";
}
}
ShaderBinary shader_binary(ShaderKind kind,const VertexProgram* program) {
    Key key{kind};if(program) key.program=normalize_vertex_program(*program);
    if(const auto found=bytecode.find(key);found!=bytecode.end()) return {found->second,false};
    const auto hlsl=source(kind,program?&key.program:nullptr);
    ComPtr<ID3DBlob> code,errors,compact;
    auto status=D3DCompile(hlsl.data(),hlsl.size(),"b2-offline",nullptr,nullptr,"main",shader_profile(kind),
        D3DCOMPILE_ENABLE_STRICTNESS|D3DCOMPILE_OPTIMIZATION_LEVEL3,0,&code,&errors);
    if(FAILED(status)) throw std::runtime_error(std::format("Offline shader {}: {}",unsigned(kind),
        errors?static_cast<const char*>(errors->GetBufferPointer()):"unknown compiler error"));
    status=D3DStripShader(code->GetBufferPointer(),code->GetBufferSize(),
        D3DCOMPILER_STRIP_DEBUG_INFO|D3DCOMPILER_STRIP_REFLECTION_DATA,&compact);
    if(FAILED(status)) throw std::runtime_error("Could not strip offline shader metadata");
    const auto* data=static_cast<const std::byte*>(compact->GetBufferPointer());
    auto [stored,inserted]=bytecode.emplace(std::move(key),std::vector(data,data+compact->GetBufferSize()));
    return {stored->second,inserted};
}
std::span<const StaticVertex> static_vertex_shaders() {return {};}
std::string compile_shaders(const std::filesystem::path& executable,const std::filesystem::path& destination,bool replace) {
    Xbe image(executable);if(!image.supported()) throw std::runtime_error("Shader generation requires the verified executable");
    if(destination.extension()!=L".cpp") throw std::runtime_error("Shader output must be a .cpp file");
    if(std::filesystem::exists(destination) && std::filesystem::equivalent(destination,executable))
        throw std::runtime_error("Shader output cannot replace the original executable");
    // Original 214D90 reads {WORD version, WORD count}, then copies count*16
    // bytes verbatim. These title records and 2153F0's three passthrough arrays
    // are verified in the supported image; no file-wide instruction scanning.
    constexpr std::array title_programs{0x0033CE38U,0x0033E160U,0x0033E238U,0x0033E300U,0x0033E3A8U,
        0x0033E618U,0x0033E8C0U,0x0033EA68U,0x0033EC70U,0x0033EED8U,0x0033F040U,0x0033F278U,
        0x0033F3F0U,0x0033F498U,0x0033F540U,0x0033F798U,0x0033FA78U};
    std::vector<StaticVertex> vertices;
    constexpr std::array sdk_addresses{0x00223108U,0x002231C8U,0x00223278U};
    const auto add=[&](std::uint32_t address,unsigned count,bool launch) {
        if(!count || count>136) throw std::runtime_error("Invalid original shader length");
        const auto bytes=image.data(address,count*16);StaticVertex value{{},launch};
        for(unsigned slot=0;slot<count;++slot) for(unsigned word=0;word<4;++word) value.program.tokens[slot][word]=u32(bytes,slot*16+word*4);
        if(!(value.program.tokens[count-1][3]&1)) throw std::runtime_error("Original shader lacks its final marker at "+hex32(address));
        value.program=normalize_vertex_program(value.program);
        for(unsigned i=0;i<sdk_addresses.size();++i) if(address==sdk_addresses[i] && value.program!=screen_vertex_programs()[i])
            throw std::runtime_error("SDK screen-position metadata differs from original bytes at "+hex32(address));
        shader_binary(launch?ShaderKind::launch:ShaderKind::vertex,&value.program);
        if(std::ranges::none_of(vertices,[&](const StaticVertex& prior){return prior.launch==launch && prior.program==value.program;}))
            vertices.push_back(value);
    };
    for(const auto address:title_programs) {
        const auto header=image.data(address,4);const auto version=u16(header,0);
        if(version!=0x2078) throw std::runtime_error("Original shader version changed at "+hex32(address));
        add(address+4,u16(header,2),false);
    }
    constexpr std::array sdk_lengths{12U,11U,12U};
    for(unsigned i=0;i<sdk_addresses.size();++i) add(sdk_addresses[i],sdk_lengths[i],false);
    std::ostringstream output;output<<"// Generated ahead of time from the verified XBE and native HLSL.\n#include \"shaders.h\"\n#include <stdexcept>\nnamespace b2 {namespace {\n";
    std::size_t total=0;
    for(unsigned i=0;i<unsigned(ShaderKind::count);++i) {
        if(i==unsigned(ShaderKind::vertex) || i==unsigned(ShaderKind::launch)) continue;
        const auto binary=shader_binary(ShaderKind(i));total+=binary.bytes.size();array(output,"builtin_"+std::to_string(i),binary.bytes);
    }
    for(unsigned i=0;i<vertices.size();++i) {
        const auto& value=vertices[i];const auto binary=shader_binary(value.launch?ShaderKind::launch:ShaderKind::vertex,&value.program);
        total+=binary.bytes.size();array(output,"vertex_"+std::to_string(i),binary.bytes);
    }
    output<<std::format("const std::array<StaticVertex,{}> programs{{{{\n",vertices.size());
    for(const auto& value:vertices) program(output,value);
    output<<"}};\n}\nstd::span<const StaticVertex> static_vertex_shaders(){return programs;}\nShaderBinary shader_binary(ShaderKind kind,const VertexProgram* program){\nswitch(kind){\n";
    for(unsigned i=0;i<unsigned(ShaderKind::count);++i) {
        if(i==unsigned(ShaderKind::vertex) || i==unsigned(ShaderKind::launch)) continue;
        output<<std::format("case ShaderKind({}):return {{Bytes(reinterpret_cast<const std::byte*>(builtin_{}),sizeof(builtin_{})),false}};\n",i,i,i);
    }
    output<<"default:break;}\nif(!program) throw std::runtime_error(\"Missing static shader program\");\nconst auto key=normalize_vertex_program(*program);\n";
    for(unsigned i=0;i<vertices.size();++i)
        output<<std::format("if(kind==ShaderKind::{} && key==programs[{}].program) return {{Bytes(reinterpret_cast<const std::byte*>(vertex_{}),sizeof(vertex_{})),false}};\n",
            vertices[i].launch?"launch":"vertex",i,i,i);
    output<<"throw std::runtime_error(\"Vertex program is missing from the compiled shader bank\");}\n}\n";
    write_text(destination,output.str(),replace);
    return std::format("{{\"format\":\"b2-shaders-aot-v1\",\"sha256\":{},\"title_records\":{},\"sdk_programs\":3,\"vertex_programs\":{},\"builtin_shaders\":{},\"bytecode_bytes\":{},\"runtime_compiler\":false}}",
        json(image.sha256),title_programs.size(),vertices.size(),unsigned(ShaderKind::count)-2,total);
}
}
