#include "vertex_program.h"
#include <format>
#include <stdexcept>

namespace b2 {
namespace {
unsigned bits(std::uint32_t value,unsigned first,unsigned count) { return (value>>first)&((1U<<count)-1); }
std::string lanes(unsigned mask) {
    std::string result;
    for(unsigned i=0;i<4;++i) if(mask&(8U>>i)) result+="xyzw"[i];
    return result;
}
constexpr std::string_view helpers=R"(
float nv_mul(float a,float b) {
 if(a==0 || b==0) return 0;
 float result=a*b;
 return isinf(result) && isfinite(a) && isfinite(b) ? (asuint(result)&0x80000000 ? -asfloat(0x7F7FFFFF) : asfloat(0x7F7FFFFF)) : result;
}
float4 nv_mul4(float4 a,float4 b) { return float4(nv_mul(a.x,b.x),nv_mul(a.y,b.y),nv_mul(a.z,b.z),nv_mul(a.w,b.w)); }
float nv_slt(float a,float b) { return a<b || (asuint(a)==0x80000000 && asuint(b)==0) ? 1 : 0; }
float4 nv_slt4(float4 a,float4 b) { return float4(nv_slt(a.x,b.x),nv_slt(a.y,b.y),nv_slt(a.z,b.z),nv_slt(a.w,b.w)); }
float nv_rcc(float x) {
 float value=1/x;
 return (asuint(value)&0x80000000) ? clamp(value,-asfloat(0x5F800000),-asfloat(0x1F800000)) : clamp(value,asfloat(0x1F800000),asfloat(0x5F800000));
}
float4 nv_log(float x) {
 float value=abs(x);
 if(value==0) return float4(-asfloat(0x7F800000),1,-asfloat(0x7F800000),1);
 if(isinf(value)) return float4(value,1,value,1);
 float exponent=floor(log2(value));
 return float4(exponent,value/exp2(exponent),log2(value),1);
}
float4 nv_lit(float4 x) {
 return float4(1,max(x.x,0),x.x>0 && x.y>0 ? pow(x.y,clamp(x.w,-127.99609375,127.99609375)) : 0,1);
}
)";
std::string mac_operation(unsigned opcode) {
    switch(opcode) {
    case 1:return "a";
    case 2:return "nv_mul4(a,b)";
    case 3:return "a+c";
    case 4:return "nv_mul4(a,b)+c";
    case 5:return "dot(nv_mul4(a,b).xyz,float3(1,1,1)).xxxx";
    case 6:return "(dot(nv_mul4(a,b).xyz,float3(1,1,1))+b.w).xxxx";
    case 7:return "dot(nv_mul4(a,b),float4(1,1,1,1)).xxxx";
    case 8:return "float4(1,nv_mul(a.y,b.y),a.z,b.w)";
    case 9:return "float4(a.x<b.x?a.x:b.x,a.y<b.y?a.y:b.y,a.z<b.z?a.z:b.z,a.w<b.w?a.w:b.w)";
    case 10:return "float4(a.x>b.x?a.x:b.x,a.y>b.y?a.y:b.y,a.z>b.z?a.z:b.z,a.w>b.w?a.w:b.w)";
    case 11:return "nv_slt4(a,b)";
    case 12:return "1-nv_slt4(a,b)";
    case 13:return "floor(a.x+0.001).xxxx";
    default:throw std::runtime_error("Invalid NV2A vector operation");
    }
}
std::string ilu_operation(unsigned opcode) {
    switch(opcode) {
    case 1:return "c";
    case 2:return "(1/c.x).xxxx";
    case 3:return "nv_rcc(c.x).xxxx";
    case 4:return "rsqrt(abs(c.x)).xxxx";
    case 5:return "float4(exp2(floor(c.x)),c.x-floor(c.x),exp2(c.x),1)";
    case 6:return "nv_log(c.x)";
    case 7:return "nv_lit(c)";
    default:throw std::runtime_error("Invalid NV2A scalar operation");
    }
}
}
namespace {
std::string draw_source(const std::string& body) {
    std::string source(helpers);
    source+=R"(
cbuffer Program:register(b1) { float4 k[192]; float4 viewport; float4 fog_params; float4 uv_scales[4]; uint4 input_formats[4];
 uint4 fixed_flags;uint4 fixed_texgen[4];uint4 fixed_matrices; };
float4 vertex_input(float4 v,uint format) {
 if(format==0) return v;
 if(format==0x40) return v.bgra;
 if(format==0x72) return float4(v.xy,0,v.z);
 if(format==0x16) {
  uint2 halves=uint2(round(v.xy*65535));uint bits=halves.x|(halves.y<<16);
  int3 values=int3(int(bits<<21)>>21,int(bits<<10)>>21,int(bits)>>22);
  return float4(max(float3(values)/float3(1023,1023,511),-1),1);
 }
 if((format&15)==5) {uint4 bits=uint4(round(v*65535));v=float4(int4(bits)-int4(bits>=32768)*65536);}
 uint size=(format>>4)&15;
 if(size==1) return float4(v.x,0,0,1);
 if(size==2) return float4(v.xy,0,1);
 if(size==3) return float4(v.xyz,1);
 return v;
}
struct I {
)";
    for(unsigned i=0;i<16;++i) source+=std::format("float4 v{}:TEXCOORD{};\n",i,i);
    source+="};\nstruct P { float4 p:SV_Position; float4 c:COLOR0; float4 s:COLOR1; float4 t0:TEXCOORD0; float4 t1:TEXCOORD1; float4 t2:TEXCOORD2; float4 t3:TEXCOORD3; float fog:TEXCOORD4; };\nP main(I input) { float4 v[16];\n";
    for(unsigned i=0;i<16;++i) source+=std::format("v[{}]=vertex_input(input.v{},input_formats[{}][{}]);\n",i,i,i/4,i%4);
    source+=body+R"(
P p; float w=o[0].w;
w=(asuint(w)&0x80000000) ? clamp(w,-asfloat(0x5F800000),-asfloat(0x1F800000)) : clamp(w,asfloat(0x1F800000),asfloat(0x5F800000));
float2 screen=trunc(o[0].xy*16)/16-viewport.w;
p.p=float4((screen.x*viewport.x-1)*w,(1-screen.y*viewport.y)*w,o[0].z*viewport.z*w,w);
p.c=saturate(o[3]); p.s=saturate(o[4]);
p.t0=o[9]*uv_scales[0]; p.t1=o[10]*uv_scales[1]; p.t2=o[11]*uv_scales[2]; p.t3=o[12]*uv_scales[3];
float d=o[5].x,mode=fog_params.z;
float fog=mode==0 ? fog_params.x+d*fog_params.y-1 : mode==1 ? fog_params.x+exp2(d*fog_params.y*16)-1.5 : fog_params.x+exp2(-d*d*fog_params.y*fog_params.y*32)-1.5;
p.fog=fog_params.w==0 ? 1 : fog_params.w==2 ? abs(fog) : fog;
return p; }
)";
    return source;
}
}
void validate_fixed_transform(const FixedTransform& state) {
    if(state.skin>6) throw std::runtime_error("Reserved NV097 skin mode");
    if(state.fog && state.fog_source!=0 && state.fog_source!=1 && state.fog_source!=2 && state.fog_source!=3 && state.fog_source!=6)
        throw std::runtime_error("Unsupported NV097 fixed fog source");
    for(const auto& stage:state.texgen) for(unsigned lane=0;lane<4;++lane) {
        const auto mode=stage[lane];
        if(mode!=0 && mode!=0x2400 && mode!=0x2401 && mode!=0x2402 && mode!=0x8511 && mode!=0x8512)
            throw std::runtime_error("Unsupported NV097 texgen mode");
        if((mode==0x2402 && lane>=2) || ((mode==0x8511 || mode==0x8512) && lane>=3))
            throw std::runtime_error("Invalid NV097 texgen component");
    }
}
std::string fixed_transform_source(std::uint32_t skin) {
    if(skin>6) throw std::runtime_error("Reserved NV097 skin mode");
    const auto matrix=[](std::string_view vector,unsigned row) {
        return std::format("float4(dot({},k[{}]),dot({},k[{}]),dot({},k[{}]),dot({},k[{}]))",
            vector,row,vector,row+1,vector,row+2,vector,row+3);
    };
    std::string body="float4 o[13]; [unroll] for(int i=0;i<13;++i) o[i]=float4(0,0,0,1);\n";
    body+="float4 eye=0;float3 normal=0;\n";
    const auto count=skin?(skin+3)/2:1;
    std::string remainder="1.0";
    for(unsigned bone=0;bone<count;++bone) {
        const auto weight=!skin?"1.0":((skin&1) && bone+1==count)?remainder:std::format("v[1].{}","xyzw"[bone]);
        body+=std::format("eye+={}*({});normal+=({}).xyz*({});\n",
            matrix("v[0]",8+bone*8),weight,matrix("float4(v[2].xyz,0)",12+bone*8),weight);
        remainder+="-v[1]."+std::string(1,"xyzw"[bone]);
    }
    body+="if(fixed_flags.x) normal*=rsqrt(max(dot(normal,normal),1e-30));\n";
    body+="float4 q="+matrix(skin?"eye":"v[0]",0)+";\n";
    body+=R"(
float qw=(asuint(q.w)&0x80000000) ? clamp(q.w,-asfloat(0x5F800000),-asfloat(0x1F800000)) : clamp(q.w,asfloat(0x1F800000),asfloat(0x5F800000));
o[0]=float4(q.xy/qw+k[59].xy,q.z/qw,qw);
o[3]=v[3];o[4]=v[4];o[5]=v[5];
)";
    for(unsigned stage=0;stage<4;++stage) {
        body+=std::format("float4 tex{}=v[{}];\n",stage,9+stage);
        for(unsigned lane=0;lane<4;++lane) {
            const auto mode=std::format("fixed_texgen[{}][{}]",stage,lane),destination=std::format("tex{}.{}",stage,"xyzw"[lane]);
            body+=std::format("if({}==0x2400) {}=dot(eye,k[{}]);else if({}==0x2401) {}=dot(v[0],k[{}]);\n",
                mode,destination,64+stage*8+lane,mode,destination,64+stage*8+lane);
            if(lane<3) {
                body+=std::format("else if({}==0x8511) {}=normal.{};else if({}==0x8512{} ) {{\n",
                    mode,destination,"xyz"[lane],mode,lane<2?" || "+mode+"==0x2402":"");
                body+="float3 reflection=reflect(eye.xyz*rsqrt(max(dot(eye.xyz,eye.xyz),1e-30)),normal);\n";
                body+=std::format("{}=reflection.{};\n",destination,"xyz"[lane]);
                if(lane<2) body+=std::format("if({}==0x2402) {}={}/(2*max(length(reflection+float3(0,0,1)),1e-15))+0.5;\n",mode,destination,destination);
                body+="}\n";
            }
        }
        body+=std::format("o[{}]=fixed_matrices[{}]?{}:tex{};\n",9+stage,stage,matrix(std::format("tex{}",stage),68+stage*8),stage);
    }
    body+=R"(
if(fixed_flags.y) {
 if(fixed_flags.z==0) o[5].x=saturate(v[4].w);
 else if(fixed_flags.z==1) o[5].x=length(eye.xyz);
 else if(fixed_flags.z==2) o[5].x=dot(eye.xyz,k[57].xyz)+k[57].w;
 else if(fixed_flags.z==3) o[5].x=abs(dot(eye.xyz,k[57].xyz)+k[57].w);
})";
    return draw_source(body);
}
VertexProgram normalize_vertex_program(const VertexProgram& program) {
    if(program.start>=program.tokens.size()) throw std::runtime_error("NV2A vertex start exceeds 136 slots");
    VertexProgram result;
    for(unsigned slot=program.start;slot<program.tokens.size();++slot) {
        result.tokens[slot-program.start]=program.tokens[slot];
        if(program.tokens[slot][3]&1) return result;
    }
    throw std::runtime_error("NV2A vertex program has no final instruction within 136 slots");
}
VertexTranslation translate_vertex_program(const VertexProgram& program,bool launch) {
    if(program.start>=program.tokens.size()) throw std::runtime_error("NV2A vertex start exceeds 136 slots");
    VertexTranslation result;
    std::string body="float4 r[12]; float4 o[13]; int a0=0;\n[unroll] for(int i=0;i<12;++i) r[i]=0;\n[unroll] for(int i=0;i<13;++i) o[i]=float4(0,0,0,1);\n";
    bool final=false;
    for(unsigned slot=program.start;slot<program.tokens.size();++slot) {
        const auto& t=program.tokens[slot];
        const auto mac=bits(t[1],21,4),ilu=bits(t[1],25,3),input=bits(t[1],9,4),constant=bits(t[1],13,8);
        const bool relative=bits(t[3],1,1)!=0;
        const auto temporary=bits(t[3],20,4),mm=bits(t[3],24,4),im=bits(t[3],16,4),om=bits(t[3],12,4);
        const auto output=bits(t[3],3,8);
        const bool output_register=bits(t[3],11,1)!=0,scalar_output=bits(t[3],2,1)!=0;
        if(mac>13) throw std::runtime_error(std::format("NV2A slot {} has reserved MAC opcode {}",slot,mac));
        const auto source=[&](unsigned type,unsigned index,unsigned swizzle,bool negative) {
            std::string value;
            if(type==1) {
                if(index>12) throw std::runtime_error(std::format("NV2A slot {} reads reserved R{}",slot,index));
                value=index==12?"o[0]":std::format("r[{}]",index);
            } else if(type==2) value=std::format("v[{}]",input);
            else if(type==3) {
                if(!relative && constant>=192) throw std::runtime_error(std::format("NV2A slot {} reads invalid constant {}",slot,constant));
                value=relative?std::format("((a0+{}>=0 && a0+{}<192)?k[a0+{}]:float4(0,0,0,0))",constant,constant,constant):std::format("k[{}]",constant);
            } else throw std::runtime_error(std::format("NV2A slot {} has an absent required input",slot));
            std::string swz;
            for(unsigned lane=0;lane<4;++lane) swz+="xyzw"[(swizzle>>(6-lane*2))&3];
            return (negative?"-":"")+std::string("(")+value+")."+swz;
        };
        const auto write=[&](std::string destination,std::string_view value,unsigned mask) {
            if(mask) body+=destination+'.'+lanes(mask)+'='+std::string(value)+'.'+lanes(mask)+";\n";
        };
        const auto write_temp=[&](unsigned index,std::string_view value,unsigned mask) {
            if(index>12) throw std::runtime_error(std::format("NV2A slot {} writes reserved R{}",slot,index));
            write(index==12?"o[0]":std::format("r[{}]",index),value,mask);
            if(index==12) result.output_masks[0]|=mask;
        };
        body+=std::format("{{ // NV2A slot {}\n",slot);
        if(mac) body+="float4 a="+source(bits(t[2],26,2),bits(t[2],28,4),bits(t[1],0,8),bits(t[1],8,1)!=0)+";\n";
        if(mac>=2 && mac!=3 && mac!=13)
            body+="float4 b="+source(bits(t[2],11,2),bits(t[2],13,4),bits(t[2],17,8),bits(t[2],25,1)!=0)+";\n";
        if(mac==3 || mac==4 || ilu)
            body+="float4 c="+source(bits(t[3],28,2),bits(t[2],0,2)*4+bits(t[3],30,2),bits(t[2],2,8),bits(t[2],10,1)!=0)+";\n";
        // Both results use the old input bank, including paired reads of R1/a0.
        if(mac) body+="float4 m="+mac_operation(mac)+";\n";
        if(ilu) body+="float4 s="+ilu_operation(ilu)+";\n";
        if(mac==13) {
            if(mm || (om && !scalar_output)) throw std::runtime_error(std::format("NV2A slot {} has conflicting ARL outputs",slot));
            body+="a0=int(m.x);\n";
        } else if(mac && mm && !(ilu && temporary==1)) write_temp(temporary,"m",mm);
        if(ilu && im) write_temp(mac?1:temporary,"s",im);
        if(om && (scalar_output?ilu:mac)) {
            if(output_register) {
                if(output>=13 || output==1 || output==2) throw std::runtime_error(std::format("NV2A slot {} writes reserved output {}",slot,output));
                write(std::format("o[{}]",output),scalar_output?"s":"m",om); result.output_masks[output]|=om;
            } else {
                if(output>=192) throw std::runtime_error(std::format("NV2A slot {} writes invalid constant {}",slot,output));
                result.writes_context=true; write(std::format("k[{}]",output),scalar_output?"s":"m",om);
            }
        }
        body+="}\n"; ++result.instructions;
        if(t[3]&1) {final=true;break;}
    }
    if(!final) throw std::runtime_error("NV2A vertex program has no final instruction within 136 slots");
    if(result.writes_context && !launch) throw std::runtime_error("A context-writing program requires the transform launch path");
    result.source=std::string(helpers);
    if(launch) {
        result.source+="RWStructuredBuffer<float4> k:register(u0); StructuredBuffer<float4> inputs:register(t0);\n[numthreads(1,1,1)] void main(uint3 id:SV_DispatchThreadID) { float4 v[16]; [unroll] for(int i=0;i<16;++i) v[i]=inputs[i];\n"+body+"}\n";
    } else {
        if((result.output_masks[0]&14)!=14) throw std::runtime_error("NV2A vertex program does not write complete position XYZ");
        result.source=draw_source(body);
    }
    return result;
}
}
