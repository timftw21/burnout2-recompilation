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
    if(kind==ShaderKind::launch || kind==ShaderKind::flow_estimate || kind==ShaderKind::flow_resolve || kind==ShaderKind::flow_cut) return "cs_5_0";
    if(kind==ShaderKind::clear_pixel || kind==ShaderKind::display_pixel || kind==ShaderKind::gui_pixel ||
       (kind>=ShaderKind::combiner0 && kind<=ShaderKind::combiner8) ||
       kind==ShaderKind::flow_colour || kind==ShaderKind::flow_interpolate ||
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
    case ShaderKind::flow_colour:return R"(
Texture2D image:register(t0);SamplerState filter:register(s0);
cbuffer C:register(b0){float phase;uint level;uint first;uint spare;uint2 grid;uint2 padding;}
float4 main(float4 p:SV_Position,float2 uv:TEXCOORD0):SV_Target {
 float2 d=0.25/float2(grid);
 float3 c=(image.Sample(filter,uv+d).rgb+image.Sample(filter,uv-d).rgb+
  image.Sample(filter,uv+float2(d.x,-d.y)).rgb+image.Sample(filter,uv+float2(-d.x,d.y)).rgb)*0.25;
 return float4(c,1);})";
    case ShaderKind::flow_estimate:return R"(
Texture2D source:register(t0),destination:register(t1);
Texture2D<float4> coarse:register(t2);RWTexture2D<float4> motion:register(u0);SamplerState filter:register(s0);
Texture2D original_source:register(t3),original_destination:register(t4);
cbuffer C:register(b0){float phase;uint level;uint first;uint spare;}
float full_error(float2 uv,float2 v,float2 pixel) {
 float error=0;
 const float2 offsets[5]={float2(0,0),float2(2,0),float2(-2,0),float2(0,2),float2(0,-2)};
 [unroll] for(uint i=0;i<5;i++) {
  float2 p=uv+offsets[i]*pixel;
  error+=dot(abs(original_source.SampleLevel(filter,p,0).rgb-original_destination.SampleLevel(filter,p+v,0).rgb),
    float3(0.299,0.587,0.114))*(i==0?0.5:0.125);
 }
 return error;
}
[numthreads(8,8,1)] void main(uint3 id:SV_DispatchThreadID) {
 uint width,height;motion.GetDimensions(width,height);if(id.x>=width || id.y>=height) return;
 float2 size=float2(width,height),step=1/size,uv=(float2(id.xy)+0.5)*step;
 float3 centre=source.SampleLevel(filter,uv,level).rgb;
 float4 inherited=0;
 if(!first) {
  uint coarse_width,coarse_height;coarse.GetDimensions(coarse_width,coarse_height);
  float2 coarse_size=float2(coarse_width,coarse_height),position=uv*coarse_size-0.5,fraction=frac(position);
  int2 base=int2(floor(position));float total=0;
  [unroll] for(uint i=0;i<4;i++) {
   int2 cell=clamp(base+int2(i&1,i>>1),int2(0,0),int2(coarse_width-1,coarse_height-1));
   float3 difference=source.SampleLevel(filter,(float2(cell)+0.5)/coarse_size,level+1).rgb-centre;
   float separation=length(difference-dot(difference,float3(0.299,0.587,0.114)));
   float weight=(i&1?fraction.x:1-fraction.x)*(i&2?fraction.y:1-fraction.y)/(1+separation*separation*400);
   inherited+=coarse.Load(int3(cell,0))*weight;total+=weight;
  }
  inherited/=max(total,0.0001);
 }
 float2 v=inherited.xy;
 float residual=0,energy=0,bestResidual=1e9,bestEnergy=0,relaxation=1;float2 best=v;
 [loop] for(uint iteration=0;iteration<6;iteration++) {
  float a=0.0005,b=0,c=0.0005;float2 rhs=0;residual=0;energy=0;float support=0;
  [unroll] for(int y=-2;y<=2;y++) [unroll] for(int x=-2;x<=2;x++) {
   float2 p=uv+float2(x,y)*step,q=p+v;
   float3 s=source.SampleLevel(filter,p,level).rgb,d=destination.SampleLevel(filter,q,level).rgb,error=d-s;
   float3 gx=0.5*(destination.SampleLevel(filter,q+float2(step.x,0),level).rgb-destination.SampleLevel(filter,q-float2(step.x,0),level).rgb);
   float3 gy=0.5*(destination.SampleLevel(filter,q+float2(0,step.y),level).rgb-destination.SampleLevel(filter,q-float2(0,step.y),level).rgb);
   // A motion window must not borrow constraints from the other side of an
   // object boundary, including boundaries whose colours have equal luminance.
   float3 separation=s-centre;
   float local=saturate(1-length(separation-dot(separation,float3(0.299,0.587,0.114)))/0.12);
   float weight=local*local/(1+dot(abs(error),float3(0.299,0.587,0.114))*8);
   const float3 channels=float3(0.299,0.587,0.114);
   a+=weight*dot(gx*channels,gx);b+=weight*dot(gx*channels,gy);c+=weight*dot(gy*channels,gy);
   rhs-=weight*float2(dot(gx*channels,error),dot(gy*channels,error));
   residual+=weight*dot(abs(error),channels);energy+=weight*(dot(gx*channels,gx)+dot(gy*channels,gy));support+=weight;
  }
  residual*=25/max(support,0.001);energy*=25/max(support,0.001);
  // Keep measured candidates: the last Newton step has not yet been evaluated.
  // Back off a step that worsens the match instead of warping with false confidence.
  if(residual<=bestResidual) {best=v;bestResidual=residual;bestEnergy=energy;}
  else {v=best;relaxation*=0.5;continue;}
  float determinant=a*c-b*b;
  float2 delta=float2(c*rhs.x-b*rhs.y,a*rhs.y-b*rhs.x)/max(determinant,1e-8);
  v=clamp(v+clamp(delta,-1.5,1.5)*step*relaxation,-0.25,0.25);
 }
 v=best;float detail_confidence=max(saturate(bestEnergy/0.002),inherited.z);
 float confidence=saturate(1-bestResidual/(25*0.12))*detail_confidence;
 if(level==0) {
  uint full_width,full_height;original_source.GetDimensions(full_width,full_height);
  float2 pixel=1/float2(full_width,full_height);
  float error=full_error(uv,v,pixel);
  float2 spacing=max(pixel,round(step*0.5/pixel)*pixel);
  // Resolve downsampling/subpixel errors once per source pair, using the
  // original detail. Keep continuous motion when integer candidates fit worse.
  [unroll] for(uint refinement=0;refinement<2 && error<0.12;refinement++) {
   float2 centre=round(v/pixel)*pixel;
   [unroll] for(int y=-1;y<=1;y++) [unroll] for(int x=-1;x<=1;x++) {
    float2 candidate=centre+float2(x,y)*spacing;float candidate_error=full_error(uv,candidate,pixel);
    if(candidate_error+0.000001<error) {v=candidate;error=candidate_error;}
   }
   spacing=pixel;
  }
  bestResidual=error*25;confidence=detail_confidence;
 }
 if(any(uv+v<0) || any(uv+v>1)) confidence=0;
 motion[id.xy]=float4(v,confidence,bestResidual/25);
})";
    case ShaderKind::flow_resolve:return R"(
Texture2D source:register(t0),destination:register(t1);
Texture2D<float4> field:register(t2),opposite:register(t3);
RWTexture2D<float4> resolved:register(u0);SamplerState filter:register(s0);
float photo_error(float2 uv,float2 v,float2 pixel) {
 float error=0;const float2 offsets[5]={float2(0,0),float2(2,0),float2(-2,0),float2(0,2),float2(0,-2)};
 [unroll] for(uint i=0;i<5;i++) {
  float2 p=uv+offsets[i]*pixel;
  error+=dot(abs(source.SampleLevel(filter,p,0).rgb-destination.SampleLevel(filter,p+v,0).rgb),
   float3(0.299,0.587,0.114))*(i==0?0.5:0.125);
 }
 return error;
}
[numthreads(8,8,1)] void main(uint3 id:SV_DispatchThreadID) {
 uint width,height;resolved.GetDimensions(width,height);if(id.x>=width || id.y>=height) return;
 uint full_width,full_height;source.GetDimensions(full_width,full_height);
 float2 size=float2(width,height),full_size=float2(full_width,full_height),pixel=1/full_size,uv=(float2(id.xy)+0.5)/size;
 float3 colour=source.SampleLevel(filter,uv,0).rgb;
 float4 own=field.Load(int3(id.xy,0)),best=own,hidden=own;best.z=hidden.z=0;
 float best_error=0.12;float4 consensus=0;float total=0;
 const int2 offsets[21]={int2(0,0),int2(2,0),int2(-2,0),int2(0,2),int2(0,-2),
  int2(2,2),int2(-2,2),int2(2,-2),int2(-2,-2),int2(4,0),int2(-4,0),int2(0,4),int2(0,-4),
  int2(8,0),int2(-8,0),int2(0,8),int2(0,-8),int2(8,8),int2(-8,8),int2(8,-8),int2(-8,-8)};
 // Transfer only verified motion from the same surface. A covered pixel has
 // no endpoint match of its own; colour and distance choose nearby support.
 [unroll] for(uint i=0;i<21;i++) {
  int2 cell=clamp(int2(id.xy)+offsets[i]*2,int2(0,0),int2(width-1,height-1));
  float4 candidate=field.Load(int3(cell,0));if(candidate.z<0.2 || candidate.w>=0.02) continue;
  float2 location=(float2(cell)+0.5)/size;
  float4 reverse=opposite.SampleLevel(filter,location+candidate.xy,0);
  if(reverse.z<0.2 || length((candidate.xy+reverse.xy)*full_size)>1.5) continue;
  float3 difference=source.SampleLevel(filter,location,0).rgb-colour;
  float separation=length(difference-dot(difference,float3(0.299,0.587,0.114)));
  if(separation>=0.12) continue;
  float error=photo_error(uv,candidate.xy,pixel);
  float distance=dot(float2(offsets[i]),float2(offsets[i]));
  float weight=min(candidate.z,reverse.z)/(1+distance*0.0625+separation*separation*400);
  consensus+=candidate*weight;total+=weight;
  if(error+0.000001<best_error) {best=candidate;best_error=error;best.w=error;}
 }
 if(total>0) {
  consensus/=total;float error=photo_error(uv,consensus.xy,pixel);
  float2 rounded=round(consensus.xy/pixel)*pixel;float rounded_error=photo_error(uv,rounded,pixel);
  if(rounded_error+0.000001<error) {consensus.xy=rounded;error=rounded_error;}
  consensus.w=error;
  // Equal local matches are ambiguous on smooth detail. Prefer motion
  // supported across the surface, while retaining a better individual fit.
  if(error<=best_error+0.00001) best=consensus;
  hidden=consensus;
 }
 resolved[id.xy]=best.z>=0.2?best:hidden.z>=0.2?hidden:best;
})";
    case ShaderKind::flow_cut:return R"(
Texture2D previous:register(t0),current:register(t1);Texture2D<float4> motion:register(t2);
RWTexture2D<float> cut:register(u0);SamplerState filter:register(s0);groupshared float2 differences[256];
[numthreads(16,16,1)] void main(uint3 id:SV_GroupThreadID,uint index:SV_GroupIndex) {
 float2 uv=(float2(id.xy)+0.5)/16;float3 p=previous.SampleLevel(filter,uv,0).rgb;
 float raw=dot(abs(current.SampleLevel(filter,uv,0).rgb-p),float3(0.299,0.587,0.114));
 float warped=dot(abs(current.SampleLevel(filter,uv+motion.SampleLevel(filter,uv,0).xy,0).rgb-p),float3(0.299,0.587,0.114));
 differences[index]=float2(raw,warped);GroupMemoryBarrierWithGroupSync();
 [unroll] for(uint stride=128;stride>0;stride>>=1) {if(index<stride) differences[index]+=differences[index+stride];GroupMemoryBarrierWithGroupSync();}
 if(index==0) cut[uint2(0,0)]=(differences[0].x>256*0.20 && differences[0].y>256*0.15)?1:0;
})";
    case ShaderKind::flow_interpolate:return R"(
Texture2D previous:register(t0),current:register(t1);Texture2D<float4> forward:register(t2),backward:register(t3);
Texture2D<float> cut:register(t4);SamplerState filter:register(s0);
cbuffer C:register(b0){float phase;uint level;uint first;uint spare;}
float match(Texture2D source,Texture2D destination,float2 uv,float2 v,float2 pixel) {
 float error=0;
 const float2 offsets[5]={float2(0,0),float2(1,0),float2(-1,0),float2(0,1),float2(0,-1)};
 [unroll] for(uint i=0;i<5;i++) {
  float2 p=uv+offsets[i]*pixel;
  float3 difference=abs(source.SampleLevel(filter,p,0).rgb-destination.SampleLevel(filter,p+v,0).rgb);
  error+=dot(difference,float3(0.299,0.587,0.114))*(i==0?0.5:0.125);
 }
 return error;
}
float4 motion_at(Texture2D<float4> field,Texture2D source,Texture2D destination,float2 uv,float2 pixel) {
 uint width,height;field.GetDimensions(width,height);
 float2 position=uv*float2(width,height)-0.5,fraction=frac(position);int2 base=int2(floor(position));
 float3 colour=source.SampleLevel(filter,uv,0).rgb;float4 best=0;float total=0;
 [unroll] for(uint i=0;i<4;i++) {
  int2 cell=clamp(base+int2(i&1,i>>1),int2(0,0),int2(width-1,height-1));
  float2 location=(float2(cell)+0.5)/float2(width,height);
  float difference=dot(abs(source.SampleLevel(filter,location,0).rgb-colour),float3(0.299,0.587,0.114));
  float agreement=1/(1+difference*difference*400);
  float weight=(i&1?fraction.x:1-fraction.x)*(i&2?fraction.y:1-fraction.y)*agreement*agreement;
  best+=field.Load(int3(cell,0))*weight;total+=weight;
 }
 if(total<0.001) return float4(0,0,0,0);
 best/=total;float error=match(source,destination,uv,best.xy,pixel);
 best.w=error;
 if(error<0.00002 || error>=0.12) return best;
 [unroll] for(uint i=0;i<4;i++) {
  float4 candidate=field.Load(int3(clamp(base+int2(i&1,i>>1),int2(0,0),int2(width-1,height-1)),0));
  float candidate_error=match(source,destination,uv,candidate.xy,pixel);
  if(candidate.z>=0.2 && candidate_error+0.000001<error) {best=candidate;error=candidate_error;}
 }
 float2 rounded=round(best.xy/pixel)*pixel;float rounded_error=match(source,destination,uv,rounded,pixel);
 if(rounded_error+0.000001<error) {best.xy=rounded;error=rounded_error;}
 best.w=error;return best;
}
float4 main(float4 p:SV_Position,float2 uv:TEXCOORD0):SV_Target {
 float4 latest=current.SampleLevel(filter,uv,0);if(cut.Load(int3(0,0,0))>0.5) return latest;
 uint width,height;current.GetDimensions(width,height);float2 size=float2(width,height),pixel=1/size;
 if(match(previous,current,uv,float2(0,0),pixel)<0.0005) return latest;
 if(dot(abs(previous.SampleLevel(filter,uv,0).rgb-latest.rgb),float3(0.299,0.587,0.114))<0.0005) {
  uint neighbours=0;bool edge=false;
  const float2 offsets[4]={float2(1,0),float2(-1,0),float2(0,1),float2(0,-1)};
  [unroll] for(uint i=0;i<4;i++) {
   float3 old=previous.SampleLevel(filter,uv+offsets[i]*pixel,0).rgb;
   float3 next=current.SampleLevel(filter,uv+offsets[i]*pixel,0).rgb;
   neighbours+=dot(abs(old-next),float3(0.299,0.587,0.114))<0.0005;
   edge=edge||(min(dot(abs(old-latest.rgb),float3(0.299,0.587,0.114)),dot(abs(next-latest.rgb),float3(0.299,0.587,0.114)))>0.12);
  }
  if(neighbours>=2&&edge) return latest;
 }
 float4 fallback=phase<0.5?previous.SampleLevel(filter,uv,0):latest;
 float2 a=uv,b=uv;
 [unroll] for(uint i=0;i<3;i++) {
  a=uv-phase*motion_at(forward,previous,current,a,pixel).xy;
  b=uv-(1-phase)*motion_at(backward,current,previous,b,pixel).xy;
 }
 float4 f=motion_at(forward,previous,current,a,pixel),r=motion_at(backward,current,previous,b,pixel);
 // Validate each warp at its matching endpoint. Testing only f(a)+r(b)
 // can accept unrelated foreground/background correspondences at an overlap.
 float4 reverse=motion_at(backward,current,previous,a+f.xy,pixel);
 float4 opposite=motion_at(forward,previous,current,b+r.xy,pixel);
 bool stable_old=all(a>=0)&&all(a<=1)&&f.z>=0.2&&length((a+phase*f.xy-uv)*size)<=0.75;
 bool stable_next=all(b>=0)&&all(b<=1)&&r.z>=0.2&&length((b+(1-phase)*r.xy-uv)*size)<=0.75;
 // Resolved motion already has bidirectional support from its source surface.
 // A sharp endpoint match can remain valid when the reverse lookup lands at
 // a boundary with no usable local gradient.
 bool valid_old=stable_old&&(f.w<0.01||(f.w<0.12&&reverse.z>=0.2&&length((f.xy+reverse.xy)*size)<=1.5));
 bool valid_next=stable_next&&(r.w<0.01||(r.w<0.12&&opposite.z>=0.2&&length((r.xy+opposite.xy)*size)<=1.5));
 float4 old=previous.SampleLevel(filter,a,0),next=current.SampleLevel(filter,b,0);
 if(!valid_old&&!valid_next) {
  if(phase<0.5?f.z<0.2:r.z<0.2) return fallback;
  if(stable_old&&f.w>=0.12) return old;
  if(stable_next&&r.w>=0.12) return next;
  return fallback;
 }
 if(!valid_old) return next;if(!valid_next) return old;
 float difference=dot(abs(old.rgb-next.rgb),float3(0.299,0.587,0.114));
 // Average only aligned detail. Blending discrepant samples softens edges;
 // the nearer warped source retains detail and the intended display time.
 return difference<0.008&&length((a+f.xy-b)*size)<=0.75?lerp(old,next,phase):(phase<0.5?old:next);
})";
    default:throw std::runtime_error("Shader source requires a vertex program or the pinned GUI backend");
    }
}
}
