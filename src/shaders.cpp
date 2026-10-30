#include "shaders.h"
#include "ambient_occlusion.h"
#include <d3dcommon.h>
#include <atomic>
#include <format>
#include <stdexcept>

namespace b2 {
namespace {

class ShaderBlob final : public ID3DBlob {
    Bytes bytes_;std::atomic<ULONG> references_{1};
public:
    explicit ShaderBlob(Bytes bytes):bytes_(bytes){}
    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID iid,void** result) override {
        if(!result) return E_POINTER;
        *result=nullptr;
        if(iid!=__uuidof(IUnknown) && iid!=__uuidof(ID3DBlob)) return E_NOINTERFACE;
        *result=this;AddRef();return S_OK;
    }
    ULONG STDMETHODCALLTYPE AddRef() override {return ++references_;}
    ULONG STDMETHODCALLTYPE Release() override {
        const auto count=--references_;if(!count) delete this;return count;
    }
    void* STDMETHODCALLTYPE GetBufferPointer() override {return const_cast<std::byte*>(bytes_.data());}
    SIZE_T STDMETHODCALLTYPE GetBufferSize() override {return bytes_.size();}
};
constexpr std::string_view combiner_source=R"(
struct P {float4 p:SV_Position;float4 c:COLOR0;float4 s:COLOR1;
 float4 t0:TEXCOORD0;float4 t1:TEXCOORD1;float4 t2:TEXCOORD2;float4 t3:TEXCOORD3;float fog:TEXCOORD4;};
cbuffer Factors:register(b0) {
 float4 factor0[8];float4 factor1[8];float4 final0;float4 final1;float4 fog;float4 alpha;
 uint4 combiners[8];uint4 stages[4];uint4 controls;uint4 coverage;
};
SamplerState sm0:register(s0);SamplerState sm1:register(s1);SamplerState sm2:register(s2);SamplerState sm3:register(s3);
Texture2D tx0:register(t0);Texture2D tx1:register(t1);Texture2D tx2:register(t2);Texture2D tx3:register(t3);
TextureCube cube0:register(t4);TextureCube cube1:register(t5);TextureCube cube2:register(t6);TextureCube cube3:register(t7);
Texture3D vol0:register(t8);Texture3D vol1:register(t9);Texture3D vol2:register(t10);Texture3D vol3:register(t11);
float4 finish(float4 result,float2 position) {
 // Common geometry keeps full coverage. Changed representations occupy
 // complementary samples, retaining their original depth and blend state.
 [branch] if((coverage.x&coverage.y)!=0xFFFFFFFF) {
  uint2 xy=uint2(position);uint x=xy.x^xy.y,y=xy.y;
  uint rank=((x&1)<<5)|((y&1)<<4)|((x&2)<<2)|((y&2)<<1)|((x&4)>>1)|((y&4)>>2);
  if(((rank<32?coverage.x:coverage.y)&(1u<<(rank&31)))==0) discard;
 }
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
 return finish(result,p.p.xy);
})";
}
ID3DBlob* shader_blob(ShaderKind kind) {return new ShaderBlob(shader_binary(kind).bytes);}
const char* shader_profile(ShaderKind kind) {
    if(kind==ShaderKind::point_geometry) return "gs_5_0";
    if(kind==ShaderKind::launch ||
       kind==ShaderKind::flow_depth || kind==ShaderKind::flow_depth_ms ||
       (kind>=ShaderKind::ao_low && kind<=ShaderKind::ao_high)) return "cs_5_0";
    if(kind==ShaderKind::clear_pixel || kind==ShaderKind::display_pixel || kind==ShaderKind::gui_pixel ||
       (kind>=ShaderKind::combiner0 && kind<=ShaderKind::combiner8) ||
       kind==ShaderKind::fxaa ||
       kind==ShaderKind::copy_multisample || kind==ShaderKind::copy_multisample_stencil ||
       (kind>=ShaderKind::color && kind<=ShaderKind::modulate3d) ||
       (kind>=ShaderKind::layer_combiner0 && kind<=ShaderKind::layer_modulate3d) ||
       kind==ShaderKind::layer_begin || kind==ShaderKind::layer_begin_ms || kind==ShaderKind::ao_composite ||
       kind==ShaderKind::feedback || kind==ShaderKind::feedback_ms) return "ps_5_0";
    return "vs_5_0";
}
std::string shader_source(ShaderKind kind) {
    if(kind==ShaderKind::feedback || kind==ShaderKind::feedback_ms) {
        auto source=shader_source(ShaderKind::modulate2d);
        const std::string_view signature="float4 main(P p):SV_Target";
        source.replace(source.find(signature),signature.size(),"float4 shade(P p)");
        source+='\n';
        source+=kind==ShaderKind::feedback_ms?
            "#define MULTISAMPLE 1\nTexture2DMS<float4> scene:register(t12);\n":
            "Texture2D<float4> scene:register(t12);\n";
        return source+R"(
float4 main(P p
#ifdef MULTISAMPLE
 ,uint sample_index:SV_SampleIndex
#endif
):SV_Target {
 // Default linear interpolation keeps history sampling at the pixel centre;
 // SV_SampleIndex selects only the destination's individual MSAA samples.
 float4 history=shade(p);
#ifdef MULTISAMPLE
 float4 current=scene.Load(int2(p.p.xy),sample_index);
#else
 float4 current=scene.Load(int3(int2(p.p.xy),0));
#endif
 float3 result=lerp(current.rgb,history.rgb,history.a);
 // Quantize the completed blend, not its source: fixed-function RGB8
 // blending can round the source before multiplication and retain a ghost.
 // 257 thresholds resolve even a 1/255 alpha decrement, without repeating
 // in step with the existing 8-bit alpha quantizer or common frame ratios.
 uint2 xy=uint2(p.p.xy);uint x=xy.x^xy.y,y=xy.y;
 uint rank=((x&1)<<7)|((y&1)<<6)|((x&2)<<4)|((y&2)<<3)|((x&4)<<1)|(y&4)|((x&8)>>2)|((y&8)>>3);
 float threshold=(float((rank+coverage.z-1)%257)+0.5)/257;
 return float4(floor(saturate(result)*255+threshold)/255,current.a);
})";
    }
    if(kind>=ShaderKind::ao_low && kind<=ShaderKind::ao_composite)
        return ambient_occlusion_shader(unsigned(kind)-unsigned(ShaderKind::ao_low)+1,kind==ShaderKind::ao_composite);

    if(kind==ShaderKind::layer_begin || kind==ShaderKind::layer_begin_ms) return
        std::string(kind==ShaderKind::layer_begin_ms?"#define MULTISAMPLE\n":"")+R"(
#ifdef MULTISAMPLE
Texture2DMS<float4> scene:register(t0);
#else
Texture2D<float4> scene:register(t0);
#endif
struct O {float4 black:SV_Target0;float4 white:SV_Target1;float4 lower:SV_Target2;float4 upper:SV_Target3;};
O main(float4 position:SV_Position,uint sample:SV_SampleIndex) {
#ifdef MULTISAMPLE
 float alpha=scene.Load(int2(position.xy),sample).a;
#else
 float alpha=scene.Load(int3(position.xy,0)).a;
#endif
 O o;o.black=o.lower=float4(0,0,0,alpha);o.white=o.upper=float4(1,1,1,alpha);return o;
})";

    if(kind==ShaderKind::flow_depth || kind==ShaderKind::flow_depth_ms) return
        std::string(kind==ShaderKind::flow_depth_ms?"#define MULTISAMPLE 1\nTexture2DMS<float> source:register(t0);":"Texture2D<float> source:register(t0);")+R"(
RWTexture2D<float2> depth:register(u0);
cbuffer Size:register(b0){uint width,height;uint2 padding;}
[numthreads(8,8,1)] void main(uint3 id:SV_DispatchThreadID) {
 if(id.x>=width || id.y>=height) return;
 uint w,h,samples=1;
#ifdef MULTISAMPLE
 source.GetDimensions(w,h,samples);
#else
 source.GetDimensions(w,h);
#endif
 int2 first=id.xy*uint2(w,h)/uint2(width,height),last=(id.xy+1)*uint2(w,h)/uint2(width,height);
 float2 value=float2(1,0);
 [loop] for(int y=first.y;y<max(first.y+1,last.y);++y) [loop] for(int x=first.x;x<max(first.x+1,last.x);++x)
 [loop] for(uint s=0;s<samples;++s) {
  int2 at=clamp(int2(x,y),0,int2(w,h)-1);
#ifdef MULTISAMPLE
  float sample_depth=source.Load(at,s);
#else
  float sample_depth=source.Load(int3(at,0));
#endif
  value=float2(min(value.x,sample_depth),max(value.y,sample_depth));
 }
 depth[id.xy]=value;
})";
    if(kind>=ShaderKind::layer_combiner0 && kind<=ShaderKind::layer_modulate3d) {
        const auto original=kind<=ShaderKind::layer_combiner8?
            ShaderKind(unsigned(ShaderKind::combiner0)+unsigned(kind)-unsigned(ShaderKind::layer_combiner0)):
            ShaderKind(unsigned(ShaderKind::color)+unsigned(kind)-unsigned(ShaderKind::layer_color));
        auto source=shader_source(original);
        const auto position=source.find("float4 main(P p):SV_Target");
        if(position==std::string::npos) throw std::runtime_error("Cannot add the presentation-layer shader outputs");
        source.replace(position,std::string_view("float4 main(P p):SV_Target").size(),"float4 shade(P p)");
        return source+R"(
struct O {float4 black:SV_Target0;float4 white:SV_Target1;float4 lower:SV_Target2;float4 upper:SV_Target3;};
O main(P p) {O o;o.black=o.white=o.lower=o.upper=saturate(shade(p));return o;})";
    }
    if(kind==ShaderKind::point_geometry) return R"(
cbuffer Points:register(b4) {float4 point_info;}
struct P {float4 p:SV_Position;float4 c:COLOR0;float4 s:COLOR1;
 float4 t0:TEXCOORD0;float4 t1:TEXCOORD1;float4 t2:TEXCOORD2;float4 t3:TEXCOORD3;
 float fog:TEXCOORD4;float size:TEXCOORD5;};
[maxvertexcount(4)]
void main(point P input[1],inout TriangleStream<P> output) {
 P center=input[0];
 if(center.p.w<=0 || any(abs(center.p.xy)>center.p.w) || center.p.z<0 || center.p.z>center.p.w) return;
 float size=clamp(point_info.w<0?center.size:point_info.w,1,63.875);
 [unroll] for(uint i=0;i<4;++i) {
  float2 uv=float2(i>>1,i&1);P corner=center;
  corner.p.xy+=(uv*2-1)*float2(point_info.x,-point_info.y)*size*center.p.w;
  if(point_info.z) corner.t3=float4(uv,1,1);
  output.Append(corner);
 }
})";
    if(kind>=ShaderKind::color && kind<=ShaderKind::modulate3d) {
        // Exact reductions of pass-through and single-stage multiplication.
        // Keep the shared bindings/alpha comparison, with no register decoding.
        std::string source(combiner_source.substr(0,combiner_source.find("struct R")));
        if(kind==ShaderKind::color)
            return source+"float4 main(P p):SV_Target {return finish((controls.x&255)?saturate(p.c):max(p.c,0),p.p.xy);}";
        const auto sample=kind==ShaderKind::modulate2d?"tx0.Sample(sm0,p.t0.xy/p.t0.w)":
            kind==ShaderKind::modulate_cube?"cube0.Sample(sm0,p.t0.xyz)":"vol0.Sample(sm0,p.t0.xyz/p.t0.w)";
        return source+std::format("float4 main(P p):SV_Target {{float4 t={};if(stages[0].w&2) t.a=1;if((stages[0].w&1) && t.a==0) discard;return finish(saturate(max(p.c,0)*t),p.p.xy);}}",sample);
    }
    if(kind>=ShaderKind::combiner0 && kind<=ShaderKind::combiner8)
        return std::format("#define COMBINER_COUNT {}\n",unsigned(kind)-unsigned(ShaderKind::combiner0))+std::string(combiner_source);
    if(kind>=ShaderKind::fixed0 && kind<=ShaderKind::fixed6) return fixed_transform_source(unsigned(kind)-unsigned(ShaderKind::fixed0));
    switch(kind) {
    case ShaderKind::passthrough:return R"(
struct V {float4 p:POSITION;float4 c:COLOR0;float4 s:COLOR1;
 float4 t0:TEXCOORD0;float4 t1:TEXCOORD1;float4 t2:TEXCOORD2;float4 t3:TEXCOORD3;float fog:TEXCOORD4;};
struct P {float4 p:SV_Position;float4 c:COLOR0;float4 s:COLOR1;
 float4 t0:TEXCOORD0;float4 t1:TEXCOORD1;float4 t2:TEXCOORD2;float4 t3:TEXCOORD3;float fog:TEXCOORD4;float size:TEXCOORD5;};
P main(V v) {P p;p.p=v.p;p.c=v.c;p.s=v.s;p.t0=v.t0;p.t1=v.t1;p.t2=v.t2;p.t3=v.t3;p.fog=v.fog;p.size=1;return p;})";
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
    case ShaderKind::copy_multisample:return R"(
Texture2DMS<float4> source_colour:register(t0);Texture2DMS<float> source_depth:register(t1);
struct Output {float4 colour:SV_Target;float depth:SV_Depth;};
Output main(float4 position:SV_Position,uint sample:SV_SampleIndex) {
 Output result;result.colour=source_colour.Load(int2(position.xy),sample);
 result.depth=source_depth.Load(int2(position.xy),sample);return result;
})";
    case ShaderKind::copy_multisample_stencil:return R"(
Texture2DMS<uint2> source_stencil:register(t2);cbuffer C:register(b0){uint bit_mask;uint3 padding;}
void main(float4 position:SV_Position,uint sample:SV_SampleIndex) {
 if((source_stencil.Load(int2(position.xy),sample).y&bit_mask)==0) discard;
})";
    case ShaderKind::fxaa:return R"(
Texture2D image:register(t0);SamplerState filter:register(s0);
cbuffer C:register(b0){float2 texel;float quality;float spare;}
float luminance(float2 uv) {return dot(image.SampleLevel(filter,uv,0).rgb,float3(0.299,0.587,0.114));}
float4 main(float4 position:SV_Position,float2 uv:TEXCOORD0):SV_Target {
 float4 centre=image.SampleLevel(filter,uv,0);
 float m=dot(centre.rgb,float3(0.299,0.587,0.114));
 float n=luminance(uv-float2(0,texel.y)),s=luminance(uv+float2(0,texel.y));
 float w=luminance(uv-float2(texel.x,0)),e=luminance(uv+float2(texel.x,0));
 float low=min(m,min(min(n,s),min(w,e))),high=max(m,max(max(n,s),max(w,e)));
 float contrast=high-low;
 float threshold=quality<1.5?0.25:quality<2.5?0.166667:0.125;
 float minimum=quality<1.5?0.083333:quality<2.5?0.0625:0.03125;
 if(contrast<max(minimum,high*threshold)) return centre;
 float nw=luminance(uv-texel),ne=luminance(uv+float2(texel.x,-texel.y));
 float sw=luminance(uv+float2(-texel.x,texel.y)),se=luminance(uv+texel);
 float horizontal=abs(nw+sw-2*w)+2*abs(n+s-2*m)+abs(ne+se-2*e);
 float vertical=abs(nw+ne-2*n)+2*abs(w+e-2*m)+abs(sw+se-2*s);
 bool along_x=horizontal>=vertical;
 float side_a=along_x?n:w,side_b=along_x?s:e;
 float gradient_a=abs(side_a-m),gradient_b=abs(side_b-m);
 bool first=gradient_a>=gradient_b;
 float average=0.5*(m+(first?side_a:side_b));
 float limit=0.25*max(gradient_a,gradient_b);
 float2 normal=along_x?float2(0,texel.y):float2(texel.x,0);
 if(first) normal=-normal;
 float2 tangent=along_x?float2(texel.x,0):float2(0,texel.y);
 float2 origin=uv+normal*0.5,negative=origin-tangent,positive=origin+tangent;
 float difference_a=luminance(negative)-average,difference_b=luminance(positive)-average;
 bool done_a=abs(difference_a)>=limit,done_b=abs(difference_b)>=limit;
 uint steps=quality<1.5?4:quality<2.5?8:12;
 [loop] for(uint step=1;step<steps && (!done_a || !done_b);++step) {
  if(!done_a) {negative-=tangent;difference_a=luminance(negative)-average;done_a=abs(difference_a)>=limit;}
  if(!done_b) {positive+=tangent;difference_b=luminance(positive)-average;done_b=abs(difference_b)>=limit;}
 }
 float distance_a=along_x?uv.x-negative.x:uv.y-negative.y;
 float distance_b=along_x?positive.x-uv.x:positive.y-uv.y;
 bool nearer_a=distance_a<distance_b;
 float edge=0.5-min(distance_a,distance_b)/(distance_a+distance_b);
 if(((nearer_a?difference_a:difference_b)<0)==(m<average)) edge=0;
 float local=(2*(n+s+w+e)+nw+ne+sw+se)/12;
 float subpixel=saturate(abs(local-m)/contrast);
 subpixel=subpixel*subpixel*(3-2*subpixel);subpixel*=subpixel;
 subpixel*=quality<1.5?0.25:quality<2.5?0.5:0.75;
 float2 shifted=uv+normal*max(edge,subpixel);
 return float4(image.SampleLevel(filter,shifted,0).rgb,centre.a);
})";
    default:throw std::runtime_error("Shader source requires a vertex program or the pinned GUI backend");
    }
}
}
