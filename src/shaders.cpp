#include "shaders.h"
#include <d3dcommon.h>
#include <Windows.h>
#include <format>
#include <stdexcept>

namespace b2 {
namespace {
class ShaderBlob final : public ID3DBlob {
    Bytes bytes_;LONG references_=1;
public:
    explicit ShaderBlob(Bytes bytes):bytes_(bytes){}
    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID iid,void** result) override {
        if(!result) return E_POINTER;
        *result=nullptr;
        if(iid!=__uuidof(IUnknown) && iid!=__uuidof(ID3DBlob)) return E_NOINTERFACE;
        *result=this;AddRef();return S_OK;
    }
    ULONG STDMETHODCALLTYPE AddRef() override {return InterlockedIncrement(&references_);}
    ULONG STDMETHODCALLTYPE Release() override {
        const auto count=InterlockedDecrement(&references_);if(!count) delete this;return count;
    }
    void* STDMETHODCALLTYPE GetBufferPointer() override {return const_cast<std::byte*>(bytes_.data());}
    SIZE_T STDMETHODCALLTYPE GetBufferSize() override {return bytes_.size();}
};
constexpr std::string_view combiner_source=R"(
struct P {float4 p:SV_Position;float4 c:COLOR0;float4 s:COLOR1;
 float4 t0:TEXCOORD0;float4 t1:TEXCOORD1;float4 t2:TEXCOORD2;float4 t3:TEXCOORD3;float fog:TEXCOORD4;};
cbuffer Factors:register(b0) {
 float4 factor0[8];float4 factor1[8];float4 final0;float4 final1;float4 fog;float4 alpha;
 uint4 combiners[8];uint4 stages[4];uint4 controls;
};
SamplerState sm0:register(s0);SamplerState sm1:register(s1);SamplerState sm2:register(s2);SamplerState sm3:register(s3);
Texture2D tx0:register(t0);Texture2D tx1:register(t1);Texture2D tx2:register(t2);Texture2D tx3:register(t3);
TextureCube cube0:register(t4);TextureCube cube1:register(t5);TextureCube cube2:register(t6);TextureCube cube3:register(t7);
Texture3D vol0:register(t8);Texture3D vol1:register(t9);Texture3D vol2:register(t10);Texture3D vol3:register(t11);
float4 finish(float4 result) {
 float a=round(saturate(result.a)*255);uint func=controls.w;
 bool accepted=func==0x207 || (func==0x201 && a<alpha.x) || (func==0x202 && a==alpha.x) ||
  (func==0x203 && a<=alpha.x) || (func==0x204 && a>alpha.x) || (func==0x205 && a!=alpha.x) || (func==0x206 && a>=alpha.x);
 if(!accepted) discard;return result;
}
struct R {float4 fog,c,s,t0,t1,t2,t3,r0,r1,sum,ef;};
float4 mapped(float4 v,uint word) {
 uint m=word>>5;
 if(m==0) return max(v,0);if(m==1) return 1-saturate(v);
 if(m==2) return 2*max(v,0)-1;if(m==3) return 1-2*max(v,0);
 if(m==4) return max(v,0)-0.5;if(m==5) return 0.5-max(v,0);
 return m==6?v:-v;
}
float4 source(R r,uint word,uint stage,bool final) {
 uint reg=word&15;
 if(reg==1) return final?final0:factor0[(controls.x&0x1000)?stage:0];
 if(reg==2) return final?final1:factor1[(controls.x&0x10000)?stage:0];
 if(reg==3) return r.fog;if(reg==4) return r.c;if(reg==5) return r.s;
 if(reg==8) return r.t0;if(reg==9) return r.t1;if(reg==10) return r.t2;if(reg==11) return r.t3;
 if(reg==12) return r.r0;if(reg==13) return r.r1;if(reg==14) return r.sum;if(reg==15) return r.ef;
 return 0;
}
float3 rgb(R r,uint word,uint stage,bool final) {
 float4 v=source(r,word,stage,final);return mapped((word&16)?v.aaaa:v,word).rgb;
}
float alp(R r,uint word,uint stage,bool final) {
 float4 v=source(r,word,stage,final);return mapped((word&16)?v.aaaa:v.bbbb,word).x;
}
float4 outmap(float4 v,uint flags) {
 uint m=flags&0x38;if(m==8) return v-0.5;if(m==16) return v*2;
 if(m==24) return (v-0.5)*2;if(m==32) return v*4;if(m==48) return v*0.5;return v;
}
void write_rgb(inout R r,uint destination,float3 v) {
 v=clamp(v,-1,1);if(destination==4) r.c.rgb=v;if(destination==5) r.s.rgb=v;
 if(destination==8) r.t0.rgb=v;if(destination==9) r.t1.rgb=v;if(destination==10) r.t2.rgb=v;if(destination==11) r.t3.rgb=v;
 if(destination==12) r.r0.rgb=v;if(destination==13) r.r1.rgb=v;
}
void write_alpha(inout R r,uint destination,float v) {
 v=clamp(v,-1,1);if(destination==4) r.c.a=v;if(destination==5) r.s.a=v;
 if(destination==8) r.t0.a=v;if(destination==9) r.t1.a=v;if(destination==10) r.t2.a=v;if(destination==11) r.t3.a=v;
 if(destination==12) r.r0.a=v;if(destination==13) r.r1.a=v;
}
// Texture inputs are signature registers and cannot be indexed dynamically in
// shader model 5. Expand the four fixed slots before the compiler sees them.
#define TEXTURE(N) { \
 uint mode=stages[N].x;float4 coord=p.t##N; \
 if(mode!=0) { \
  float4 sampled=0;bool enabled=true; \
  if(mode==4) sampled=coord; \
  else if(mode==5) { \
   uint clip=stages[N].z; \
   if((clip&1)?coord.x>=0:coord.x<0) discard;if((clip&2)?coord.y>=0:coord.y<0) discard; \
   if((clip&4)?coord.z>=0:coord.z<0) discard;if((clip&8)?coord.w>=0:coord.w<0) discard; \
  } else if(mode==3) sampled=cube##N.Sample(sm##N,coord.xyz); \
  else if(mode==2) sampled=vol##N.Sample(sm##N,coord.xyz/coord.w); \
  else { \
   float2 uv=coord.xy/coord.w; \
   if(mode==15 || mode==16) { \
    uint previous=stages[N].y;enabled=stages[previous].x!=0 && stages[previous].x!=5; \
    float4 value=source(r,8+previous,0,false);uv=mode==15?value.ar:value.gb; \
   } \
   if(enabled) sampled=tx##N.Sample(sm##N,uv); \
  } \
  if(enabled && mode!=4 && mode!=5) { \
   if(stages[N].w&2) sampled.a=1; \
   if((stages[N].w&1) && sampled.a==0) discard; \
  } \
  r.t##N=sampled; \
 } \
}
float4 main(P p):SV_Target {
 R r=(R)0;r.fog=float4(fog.rgb,saturate(p.fog));r.c=p.c;r.s=p.s;
 r.t0=r.t1=r.t2=r.t3=float4(0,0,0,1);
 TEXTURE(0) TEXTURE(1) TEXTURE(2) TEXTURE(3)
 r.r0.a=stages[0].x?r.t0.a:1;
 // A separate build-time shader for each count removes unused stages.
 [unroll] for(uint stage=0;stage<COMBINER_COUNT;++stage) {
  uint4 words=combiners[stage];uint cf=words.z>>12,af=words.w>>12;
  float3 c0=rgb(r,(words.x>>24)&255,stage,false),c1=rgb(r,(words.x>>16)&255,stage,false);
  float3 c2=rgb(r,(words.x>>8)&255,stage,false),c3=rgb(r,words.x&255,stage,false);
  float a0=alp(r,(words.y>>24)&255,stage,false),a1=alp(r,(words.y>>16)&255,stage,false);
  float a2=alp(r,(words.y>>8)&255,stage,false),a3=alp(r,words.y&255,stage,false);
  float3 cab=(cf&2)?dot(c0,c1).xxx:c0*c1,ccd=(cf&1)?dot(c2,c3).xxx:c2*c3;
  float aab=a0*a1,acd=a2*a3;
  bool mux=(controls.x&0x100)?r.r0.a>=0.5:(uint(r.r0.a*255)&1)!=0;
  float3 cs=(cf&4)?(mux?ccd:cab):cab+ccd;float asum=(af&4)?(mux?acd:aab):aab+acd;
  write_rgb(r,(words.z>>4)&15,outmap(float4(cab,0),cf).rgb);
  if(cf&0x80) write_alpha(r,(words.z>>4)&15,outmap(float4(cab,0),cf).b);
  write_rgb(r,words.z&15,outmap(float4(ccd,0),cf).rgb);
  if(cf&0x40) write_alpha(r,words.z&15,outmap(float4(ccd,0),cf).b);
  write_rgb(r,(words.z>>8)&15,outmap(float4(cs,0),cf).rgb);
  write_alpha(r,(words.w>>4)&15,outmap(aab.xxxx,af).x);
  write_alpha(r,words.w&15,outmap(acd.xxxx,af).x);
  write_alpha(r,(words.w>>8)&15,outmap(asum.xxxx,af).x);
 }
 float4 result=r.r0;
 if(controls.y || controls.z) {
  uint flags=controls.z;
  float3 sum=((flags&0x40)?1-r.s.rgb:r.s.rgb)+((flags&0x20)?1-r.r0.rgb:r.r0.rgb);
  r.sum=float4((flags&0x80)?saturate(sum):sum,0);
  r.ef=float4(rgb(r,(controls.z>>24)&255,0,true)*rgb(r,(controls.z>>16)&255,0,true),0);
  result=float4(rgb(r,controls.y&255,0,true)+lerp(rgb(r,(controls.y>>8)&255,0,true),
   rgb(r,(controls.y>>16)&255,0,true),rgb(r,(controls.y>>24)&255,0,true)),alp(r,(controls.z>>8)&255,0,true));
 }
 return finish(result);
})";
}
ID3DBlob* shader_blob(ShaderKind kind) {return new ShaderBlob(shader_binary(kind).bytes);}
const char* shader_profile(ShaderKind kind) {
    if(kind==ShaderKind::launch || kind==ShaderKind::flow_estimate || kind==ShaderKind::flow_cut) return "cs_5_0";
    if(kind==ShaderKind::clear_pixel || kind==ShaderKind::display_pixel || kind==ShaderKind::gui_pixel ||
       (kind>=ShaderKind::combiner0 && kind<=ShaderKind::combiner8) ||
       kind==ShaderKind::flow_luma || kind==ShaderKind::flow_interpolate ||
       (kind>=ShaderKind::color && kind<=ShaderKind::modulate3d)) return "ps_5_0";
    return "vs_5_0";
}
std::string shader_source(ShaderKind kind) {
    if(kind>=ShaderKind::color && kind<=ShaderKind::modulate3d) {
        // Exact reductions of pass-through and single-stage multiplication.
        // Keep the shared bindings/alpha comparison, with no register decoding.
        std::string source(combiner_source.substr(0,combiner_source.find("struct R")));
        if(kind==ShaderKind::color)
            return source+"float4 main(P p):SV_Target {return finish((controls.x&255)?saturate(p.c):max(p.c,0));}";
        const auto sample=kind==ShaderKind::modulate2d?"tx0.Sample(sm0,p.t0.xy/p.t0.w)":
            kind==ShaderKind::modulate_cube?"cube0.Sample(sm0,p.t0.xyz)":"vol0.Sample(sm0,p.t0.xyz/p.t0.w)";
        return source+std::format("float4 main(P p):SV_Target {{float4 t={};if(stages[0].w&2) t.a=1;if((stages[0].w&1) && t.a==0) discard;return finish(saturate(max(p.c,0)*t));}}",sample);
    }
    if(kind>=ShaderKind::combiner0 && kind<=ShaderKind::combiner8)
        return std::format("#define COMBINER_COUNT {}\n",unsigned(kind)-unsigned(ShaderKind::combiner0))+std::string(combiner_source);
    if(kind>=ShaderKind::fixed0 && kind<=ShaderKind::fixed6) return fixed_transform_source(unsigned(kind)-unsigned(ShaderKind::fixed0));
    switch(kind) {
    case ShaderKind::passthrough:return R"(
struct V {float4 p:POSITION;float4 c:COLOR0;float4 s:COLOR1;
 float4 t0:TEXCOORD0;float4 t1:TEXCOORD1;float4 t2:TEXCOORD2;float4 t3:TEXCOORD3;float fog:TEXCOORD4;};
struct P {float4 p:SV_Position;float4 c:COLOR0;float4 s:COLOR1;
 float4 t0:TEXCOORD0;float4 t1:TEXCOORD1;float4 t2:TEXCOORD2;float4 t3:TEXCOORD3;float fog:TEXCOORD4;};
P main(V v) {P p;p.p=v.p;p.c=v.c;p.s=v.s;p.t0=v.t0;p.t1=v.t1;p.t2=v.t2;p.t3=v.t3;p.fog=v.fog;return p;})";
    case ShaderKind::clear_vertex:return R"(
cbuffer C:register(b0){float4 rgba;float4 parameters;}
float4 main(uint id:SV_VertexID):SV_Position {float2 p=float2((id<<1)&2,id&2);return float4(p*float2(2,-2)+float2(-1,1),parameters.x,1);})";
    case ShaderKind::clear_pixel:return "cbuffer C:register(b0){float4 rgba;float4 parameters;}\nfloat4 main():SV_Target{return rgba;}";
    case ShaderKind::display_vertex:return R"(
struct P {float4 p:SV_Position;float2 uv:TEXCOORD0;};
P main(uint id:SV_VertexID) {P o;o.uv=float2((id<<1)&2,id&2);o.p=float4(o.uv*float2(2,-2)+float2(-1,1),0,1);return o;})";
    case ShaderKind::display_pixel:return R"(
Texture2D image:register(t0);SamplerState filter:register(s0);
float4 main(float4 p:SV_Position,float2 uv:TEXCOORD0):SV_Target{return image.Sample(filter,uv);})";
    case ShaderKind::flow_luma:return R"(
Texture2D image:register(t0);SamplerState filter:register(s0);
float main(float4 p:SV_Position,float2 uv:TEXCOORD0):SV_Target {
 float2 d=float2(0.25/160,0.25/120);
 float3 c=(image.Sample(filter,uv+d).rgb+image.Sample(filter,uv-d).rgb+
  image.Sample(filter,uv+float2(d.x,-d.y)).rgb+image.Sample(filter,uv+float2(-d.x,d.y)).rgb)*0.25;
 return dot(c,float3(0.299,0.587,0.114));})";
    case ShaderKind::flow_estimate:return R"(
Texture2D<float> source:register(t0),destination:register(t1);
Texture2D<float4> coarse:register(t2);RWTexture2D<float4> motion:register(u0);SamplerState filter:register(s0);
cbuffer C:register(b0){float phase;uint level;uint first;uint spare;}
[numthreads(8,8,1)] void main(uint3 id:SV_DispatchThreadID) {
 uint width,height;motion.GetDimensions(width,height);if(id.x>=width || id.y>=height) return;
 float2 size=float2(width,height),step=1/size,uv=(float2(id.xy)+0.5)*step;
 float2 v=first?float2(0,0):coarse.SampleLevel(filter,uv,0).xy;
 float residual=0,energy=0,bestResidual=1e9,bestEnergy=0,relaxation=1;float2 best=v;
 [loop] for(uint iteration=0;iteration<6;iteration++) {
  float a=0.0005,b=0,c=0.0005;float2 rhs=0;residual=0;energy=0;
  [unroll] for(int y=-2;y<=2;y++) [unroll] for(int x=-2;x<=2;x++) {
   float2 p=uv+float2(x,y)*step,q=p+v;
   float s=source.SampleLevel(filter,p,level),d=destination.SampleLevel(filter,q,level),error=d-s;
   float2 g=0.5*float2(destination.SampleLevel(filter,q+float2(step.x,0),level)-destination.SampleLevel(filter,q-float2(step.x,0),level),
    destination.SampleLevel(filter,q+float2(0,step.y),level)-destination.SampleLevel(filter,q-float2(0,step.y),level));
   float weight=1/(1+abs(error)*8);a+=weight*g.x*g.x;b+=weight*g.x*g.y;c+=weight*g.y*g.y;
   rhs-=weight*g*error;residual+=abs(error);energy+=dot(g,g);
  }
  // Keep measured candidates: the last Newton step has not yet been evaluated.
  // Back off a step that worsens the match instead of warping with false confidence.
  if(residual<=bestResidual) {best=v;bestResidual=residual;bestEnergy=energy;}
  else {v=best;relaxation*=0.5;continue;}
  float determinant=a*c-b*b;
  float2 delta=float2(c*rhs.x-b*rhs.y,a*rhs.y-b*rhs.x)/max(determinant,1e-8);
  v=clamp(v+clamp(delta,-1.5,1.5)*step*relaxation,-0.25,0.25);
 }
 v=best;float confidence=saturate(1-bestResidual/(25*0.12))*saturate(bestEnergy/0.002);
 if(any(uv+v<0) || any(uv+v>1)) confidence=0;
 motion[id.xy]=float4(v,confidence,bestResidual/25);
})";
    case ShaderKind::flow_cut:return R"(
Texture2D<float> previous:register(t0),current:register(t1);Texture2D<float4> motion:register(t2);
RWTexture2D<float> cut:register(u0);SamplerState filter:register(s0);groupshared float2 differences[256];
[numthreads(16,16,1)] void main(uint3 id:SV_GroupThreadID,uint index:SV_GroupIndex) {
 float2 uv=(float2(id.xy)+0.5)/16;float p=previous.SampleLevel(filter,uv,0);
 float raw=abs(current.SampleLevel(filter,uv,0)-p);
 float warped=abs(current.SampleLevel(filter,uv+motion.SampleLevel(filter,uv,0).xy,0)-p);
 differences[index]=float2(raw,warped);GroupMemoryBarrierWithGroupSync();
 [unroll] for(uint stride=128;stride>0;stride>>=1) {if(index<stride) differences[index]+=differences[index+stride];GroupMemoryBarrierWithGroupSync();}
 if(index==0) cut[uint2(0,0)]=(differences[0].x>256*0.20 && differences[0].y>256*0.15)?1:0;
})";
    case ShaderKind::flow_interpolate:return R"(
Texture2D previous:register(t0),current:register(t1);Texture2D<float4> forward:register(t2),backward:register(t3);
Texture2D<float> cut:register(t4);SamplerState filter:register(s0);
cbuffer C:register(b0){float phase;uint level;uint first;uint spare;}
float4 main(float4 p:SV_Position,float2 uv:TEXCOORD0):SV_Target {
 float4 latest=current.Sample(filter,uv);if(cut.Load(int3(0,0,0))>0.5) return latest;
 float2 a=uv,b=uv;
 [unroll] for(uint i=0;i<3;i++) {a=uv-phase*forward.SampleLevel(filter,a,0).xy;b=uv-(1-phase)*backward.SampleLevel(filter,b,0).xy;}
 float4 f=forward.SampleLevel(filter,a,0),r=backward.SampleLevel(filter,b,0);
 if(any(a<0)||any(a>1)||any(b<0)||any(b>1)||min(f.z,r.z)<0.2 || length((f.xy+r.xy)*float2(160,120))>1.5) return latest;
 float4 old=previous.Sample(filter,a),next=current.Sample(filter,b);
 if(dot(abs(old.rgb-next.rgb),float3(0.299,0.587,0.114))>0.12) return latest;
 return lerp(old,next,phase);
})";
    default:throw std::runtime_error("Shader source requires a vertex program or the pinned GUI backend");
    }
}
}
