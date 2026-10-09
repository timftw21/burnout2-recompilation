#include "gpu.h"
#include "cpu.h"
#include <d3d11.h>
#include <wrl/client.h>
#include <algorithm>
#include <chrono>
#include <deque>
#include <map>
#include <sstream>
#include <tuple>

namespace b2 {
using Microsoft::WRL::ComPtr;
namespace {
float real(std::uint32_t bits) {return std::bit_cast<float>(bits);}
unsigned attribute_bytes(std::uint32_t format) {
    const auto size=(format>>4)&15,type=format&15;
    if(format==0x16 || format==0x40) return 4;
    if(format==0x72) return 12;
    if(!size || size>4) throw std::runtime_error("Invalid NV097 vertex attribute size");
    switch(type) {case 2:return size*4;case 1:case 5:return size*2;case 4:return size;}
    throw std::runtime_error("Unsupported NV097 vertex attribute format");
}
void checked_gpu(HRESULT status) {if(FAILED(status)) throw std::runtime_error(std::format("GPU completion query failed: 0x{:08X}",static_cast<unsigned>(status)));}
}
struct Gpu::Impl {
    Renderer& renderer;Memory& memory;Gpu& owner;
    std::uint32_t semaphore_base;
    std::array<std::array<std::uint32_t,2048>,8> registers{};
    std::array<std::uint32_t,8> objects{};
    VertexProgram program;
    VertexConstants constants{};
    ProgramVertex current{};
    std::uint32_t program_load=0,constant_load=0,primitive=0,target=0;
    std::vector<std::uint32_t> indices,inline_words;
    struct Draw {
        RenderState state;VertexConstants constants;ProgramView view;ProgramVertex current;
        std::array<VertexStream,16> streams{};
        std::uint32_t layout=0,count=0,stream_count=0;
        std::vector<std::uint32_t> indices;
        std::vector<std::uint32_t> packed;
    };
    std::vector<Draw> draws;
    unsigned draw_count=0;
    struct Surface {std::uint32_t identifier,width,height;std::uint64_t revision=0;bool color_cleared=false;};
    Surface* active_surface=nullptr;
    std::uint64_t surface_revision=0;
    std::map<std::array<std::uint32_t,4>,Surface> targets;
    std::map<std::array<std::uint32_t,5>,std::uint32_t> textures;
    struct Cubemap {std::uint32_t identifier;std::array<std::uint64_t,6> revisions;};
    std::map<std::array<std::uint32_t,5>,Cubemap> cubemaps;
    struct Fence {ComPtr<ID3D11Query> query;std::uint32_t address=0,value=0;};
    std::array<Fence,64> fences;
    unsigned fence_head=0,fence_count=0;
    GpuStats statistics;
    std::filesystem::path capture_requested,capture_directory;
    std::ostringstream capture_trace;
    unsigned capture_draws=0;
    bool capture_truncated=false;
    Impl(Renderer& r,Memory& m,Gpu& o,std::uint32_t base):renderer(r),memory(m),owner(o),semaphore_base(base) {
        for(auto& attribute:current.attributes) attribute={0,0,0,1};
        current.attributes[3]={1,1,1,1};draws.resize(256);indices.reserve(4096);inline_words.reserve(4096);
        const D3D11_QUERY_DESC description{D3D11_QUERY_EVENT,0};
        for(auto& fence:fences) checked_gpu(renderer.native_device()->CreateQuery(&description,&fence.query));
    }
    std::uint32_t reg(unsigned method) const {return registers[0][method/4];}
    void flush() {
        if(!draw_count) return;
        renderer.begin(0,1,false);
        try {
            for(const auto& draw:std::span(draws).first(draw_count)) {
                renderer.draw_streams(draw.state,draw.layout,draw.constants,draw.view,
                    std::span(draw.streams).first(draw.stream_count),draw.current,draw.count,draw.indices);
                statistics.vertices+=draw.indices.empty()?draw.count:draw.indices.size();++statistics.draws;
            }
        } catch(...) {renderer.end();draw_count=0;throw;}
        renderer.end();draw_count=0;
    }
    bool select_target(std::uint32_t clear_flags=0) {
        const auto horizontal=reg(0x200),vertical=reg(0x204);
        const auto clip_width=horizontal>>16,clip_height=vertical>>16;
        if(!clip_width || !clip_height)
            throw std::runtime_error(std::format("Unsupported NV097 surface clip H=0x{:08X}, V=0x{:08X}, format=0x{:08X}, pitch=0x{:08X}, offset=0x{:08X}",
                horizontal,vertical,reg(0x208),reg(0x20C),reg(0x210)));
        const auto color=reg(0x208)&15,zeta=(reg(0x208)>>4)&15;
        if((color!=3 && color!=4 && color!=5 && color!=8) || (zeta!=1 && zeta!=2))
            throw std::runtime_error(std::format("Unsupported NV097 surface format=0x{:08X}, pitch=0x{:08X}, color=0x{:08X}, depth=0x{:08X}",
                reg(0x208),reg(0x20C),reg(0x210),reg(0x214)));
        const auto format=reg(0x208),type=(format>>8)&15;
        if(type!=1 && type!=2) throw std::runtime_error("Unsupported NV097 surface layout");
        const auto width=type==2?1U<<((format>>16)&15):(horizontal&0xFFFF)+clip_width;
        const auto height=type==2?1U<<((format>>24)&15):(vertical&0xFFFF)+clip_height;
        const auto clear_h=reg(0x1D98),clear_v=reg(0x1D9C);
        const bool complete_clear=(clear_flags&0xF0)==0xF0 && !(clear_h&0xFFFF) && !(clear_v&0xFFFF) &&
            (clear_h>>16)+1>=width && (clear_v>>16)+1>=height;
        const std::array key{reg(0x210),reg(0x214),format,reg(0x20C)};
        auto found=targets.find(key);
        if(found==targets.end() && !key[1]) {
            // A uniform full-allocation clear has the same bytes in pitched and
            // Morton order. Migrate that storage instead of creating a second,
            // uncleared image for the game's clear-then-render layout switch.
            const auto packed_pitch=width*(color==3?2U:4U);
            for(auto alias=targets.begin();alias!=targets.end();++alias) {
                const auto& [alias_key,surface]=*alias;
                const auto alias_type=(alias_key[2]>>8)&15;
                if(alias_key[0]!=key[0] || alias_key[1] || alias_type==type ||
                    (alias_key[2]&255)!=(format&255) || surface.width!=width || surface.height!=height ||
                    (type==1 && (key[3]&0xFFFF)!=packed_pitch) ||
                    (alias_type==1 && (alias_key[3]&0xFFFF)!=packed_pitch)) continue;
                if(!complete_clear && !surface.color_cleared)
                    throw std::runtime_error("Nonuniform surface layout alias requires pixel remapping");
                flush();auto node=targets.extract(alias);node.key()=key;
                found=targets.insert(std::move(node)).position;break;
            }
        }
        if(found==targets.end()) {
            flush();found=targets.emplace(key,Surface{renderer.create_target(width,height,color==3?SurfaceColor::rgb565:SurfaceColor::rgba8,
                zeta==1?SurfaceDepth::z16:SurfaceDepth::z24s8),width,height}).first;
        } else if(width>found->second.width || height>found->second.height) {
            flush();auto& surface=found->second;
            const auto w=std::max(width,surface.width),h=std::max(height,surface.height);
            renderer.grow_target(surface.identifier,w,h);surface.width=w;surface.height=h;surface.color_cleared=false;
        }
        if(target!=found->second.identifier) {flush();target=found->second.identifier;renderer.set_target(target);}
        active_surface=&found->second;
        return complete_clear && (clear_h>>16)+1>=active_surface->width && (clear_v>>16)+1>=active_surface->height;
    }
    std::uint32_t texture(unsigned stage,ProgramView& view) {
        const auto method=0x1B00+stage*0x40,format=reg(method+4),image=reg(method+0x1C),pitch=reg(method+0x10)>>16;
        const auto code=(format>>8)&255;
        const bool linear=code==0x12 || code==0x1E;
        const auto width=linear?image>>16:1U<<((format>>20)&15),height=linear?image&0xFFFF:1U<<((format>>24)&15);
        const auto depth=((format>>4)&15)==3?1U<<(format>>28):1U;
        const auto mips=(format>>16)&15;
        if(!width || !height || width>4096 || height>4096 || !mips || depth>2048)
            throw std::runtime_error("Invalid NV097 texture dimensions");
        const bool cube=(format&4)!=0;
        const auto address=reg(method)&0x0FFFFFFF;
        const std::array key{address,format,image,pitch,reg(method+0x20)};
        if(linear) view.texture_scales[stage]={1.0f/width,1.0f/height,1.0f/depth,1};
        // Sampling a GPU-produced surface must see its current GPU contents,
        // rather than the original RAM allocation used for ordinary textures.
        const auto surface_at=[&](std::uint32_t offset)->Surface* {
            for(auto& [surface_key,surface]:targets) {
                const auto surface_format=surface_key[2],surface_color=surface_format&15,surface_type=(surface_format>>8)&15;
                const bool matching_color=surface_color==3?code==5:(code==6 || code==7 || code==0x12 || code==0x1E);
                if(surface_key[0]==offset && surface.width==width && surface.height==height && matching_color &&
                    ((surface_type==1)==linear) && (!linear || (surface_key[3]&0xFFFF)==pitch)) return &surface;
            }
            return nullptr;
        };
        if(!cube && depth==1 && mips==1) if(const auto surface=surface_at(address)) return surface->identifier;
        unsigned bpp=0,block=0;
        switch(code) {case 5:bpp=2;break;case 6:case 7:case 0x12:case 0x1E:bpp=4;break;
            case 0xC:block=8;break;case 0xE:case 0xF:block=16;break;
            default:throw std::runtime_error(std::format("Unsupported NV097 texture format 0x{:02X}",code));}
        std::uint64_t size=0;
        for(unsigned level=0;level<mips;++level) {
            const auto w=std::max(1U,width>>level),h=std::max(1U,height>>level),d=std::max(1U,depth>>level);
            size+=(block?std::uint64_t((w+3)/4)*((h+3)/4)*block:std::uint64_t(linear?pitch:w*bpp)*h)*d;
        }
        if(cube) {
            const auto face_stride=(size+127)&~std::uint64_t(127);
            if(depth==1 && mips==1) {
                std::array<std::uint32_t,6> faces{};
                std::array<std::uint64_t,6> revisions{};
                unsigned matched=0;
                for(unsigned face=0;face<faces.size();++face) {
                    const auto offset=std::uint64_t(address)+face*face_stride;
                    if(offset>UINT32_MAX) throw std::runtime_error("Cubemap address exceeds memory");
                    if(const auto surface=surface_at(static_cast<std::uint32_t>(offset))) {
                        faces[face]=surface->identifier;revisions[face]=surface->revision;++matched;
                    }
                }
                if(matched && matched!=faces.size()) throw std::runtime_error("Partially GPU-produced cubemap is unsupported");
                if(matched) {
                    auto found=cubemaps.find(key);
                    if(found==cubemaps.end() || found->second.revisions!=revisions) {
                        // Execute pending producers before copying their faces;
                        // subsequent car draws reuse this snapshot until a write.
                        flush();
                        const auto identifier=renderer.copy_cubemap(faces,found==cubemaps.end()?0:found->second.identifier);
                        found=cubemaps.insert_or_assign(key,Cubemap{identifier,revisions}).first;
                    }
                    return found->second.identifier;
                }
            }
            size=face_stride*6;
        }
        if(!size || size>64*1024*1024) throw std::runtime_error("NV097 texture payload exceeds its bound");
        auto found=textures.find(key);
        if(found==textures.end()) found=textures.emplace(key,renderer.upload({width,height,static_cast<TextureFormat>(code),
            Bytes(static_cast<const std::byte*>(memory.access(address,static_cast<std::size_t>(size))),static_cast<std::size_t>(size)),
            mips,linear?pitch:0,cube,depth})).first;
        return found->second;
    }
    RenderState state(ProgramView& view) {
        RenderState s;
        const auto horizontal=reg(0x200),vertical=reg(0x204);
        s.scissor_test=true;s.scissor={std::int32_t(horizontal&0xFFFF),std::int32_t(vertical&0xFFFF),
            std::int32_t((horizontal&0xFFFF)+(horizontal>>16)),std::int32_t((vertical&0xFFFF)+(vertical>>16))};
        s.primitive=primitive;s.blend=reg(0x304)!=0;s.depth_test=reg(0x30C)!=0;s.depth_write=reg(0x35C)!=0;
        s.alpha_test=reg(0x300)!=0;s.cull=reg(0x308)!=0;s.front_ccw=reg(0x3A0)==0x901;
        s.depth_function=reg(0x354);s.alpha_function=reg(0x33C);s.alpha_reference=reg(0x340);
        s.blend_source=reg(0x344);s.blend_destination=reg(0x348);s.blend_equation=reg(0x350);
        s.cull_face=reg(0x39C);s.color_mask=reg(0x358);s.stencil_test=reg(0x32C)!=0;
        s.stencil_write_mask=reg(0x360)&255;s.stencil_function=reg(0x364);s.stencil_reference=reg(0x368)&255;s.stencil_read_mask=reg(0x36C)&255;
        s.stencil_fail=reg(0x370);s.stencil_depth_fail=reg(0x374);s.stencil_pass=reg(0x378);
        s.polygon_offset=reg(0x338)!=0;s.polygon_offset_scale=real(reg(0x384));s.polygon_offset_bias=real(reg(0x388));
        s.fog_enable=reg(0x2A4)!=0;const auto fog_color=reg(0x2A8);
        s.fog_color=(fog_color&0xFF00FF00U)|((fog_color&255)<<16)|((fog_color>>16)&255);s.fog_mode=reg(0x29C);
        s.fog_parameters={real(reg(0x9C0)),real(reg(0x9C4))};
        s.combiner_control=reg(0x1E60);s.final_inputs={reg(0x288),reg(0x28C)};s.final_factors={reg(0x1E20),reg(0x1E24)};
        for(unsigned i=0;i<8;++i) {
            s.color_inputs[i]=reg(0xAC0+i*4);s.alpha_inputs[i]=reg(0x260+i*4);
            s.color_outputs[i]=reg(0x1E40+i*4);s.alpha_outputs[i]=reg(0xAA0+i*4);
            s.factor0[i]=reg(0xA60+i*4);s.factor1[i]=reg(0xA80+i*4);
        }
        for(unsigned i=0;i<4;++i) {
            const auto method=0x1B00+i*0x40;
            s.texture_modes[i]=(reg(0x1E70)>>(i*5))&31;
            s.texture_sources[i]=i<2?0:(reg(0x1E78)>>(8+i*4))&15;
            s.texture_clip[i]=(reg(0x17F8)>>(i*4))&15;s.texture_address[i]=reg(method+8);
            s.texture_filter[i]=reg(method+0x14);s.texture_control[i]=reg(method+12);
            s.alpha_kill[i]=(s.texture_control[i]&4)!=0;
            const auto code=(reg(method+4)>>8)&255;
            s.texture_opaque[i]=code==7 || code==0x1E;
            if((reg(method+12)&0x40000000U) && s.texture_modes[i]) s.textures[i]=texture(i,view);
        }
        return s;
    }
    void draw(std::uint32_t first,std::uint32_t count,std::span<const std::uint32_t> elements={},bool inlined=false) {
        if(!primitive || !count || count>32768 || first>0xFFFFFF-count) throw std::runtime_error("Invalid NV097 draw range");
        select_target();
        ProgramView view{};
        const auto render_state=state(view); // Resolving feedback can flush pending draws.
        auto& draw=draws[draw_count];draw.stream_count=0;draw.view=view;draw.packed.clear();
        draw.current=current;draw.constants=constants;draw.count=count;
        draw.view.depth_scale=constants[58][2];
        if(draw.view.depth_scale<=0) throw std::runtime_error("NV097 viewport depth scale was not initialized");
        draw.state=render_state;
        const auto mode=reg(0x1E94)&3;
        std::uint32_t shader;
        if(mode==2) shader=renderer.prepare_program(program);
        else if(mode==0) {
            if(reg(0x314)) throw std::runtime_error("Fixed lighting requires the corresponding shader path");
            FixedTransform fixed{};fixed.skin=reg(0x328);fixed.normalize=reg(0x3A4)!=0;
            fixed.fog=reg(0x2A4)!=0;fixed.fog_source=reg(0x2A0);
            for(unsigned stage=0;stage<4;++stage) {
                fixed.texture_matrix[stage]=reg(0x420+stage*4)!=0;
                for(unsigned lane=0;lane<4;++lane) fixed.texgen[stage][lane]=reg(0x3C0+stage*16+lane*4);
            }
            shader=renderer.prepare_fixed(fixed);
        } else throw std::runtime_error("Unsupported NV097 transform mode");
        VertexLayout layout{};
        std::array<std::uint32_t,16> bases{},strides{};
        unsigned inline_stride=0;
        for(unsigned attribute=0;attribute<16;++attribute) {
            const auto format=reg(0x1760+attribute*4),stride=format>>8,type=format&255;
            if(!((type>>4)&15)) continue;
            const auto bytes=attribute_bytes(type);
            if(inlined) {layout[attribute]={0,inline_stride,type};inline_stride+=(bytes+3)&~3U;continue;}
            if(!stride || stride>2048 || stride<bytes) throw std::runtime_error("Invalid NV097 vertex stream stride");
            const auto address=reg(0x1720+attribute*4)&0x0FFFFFFF;
            unsigned stream=0;
            for(;stream<draw.stream_count;++stream) if(strides[stream]==stride && address>=bases[stream] && address-bases[stream]+bytes<=stride) break;
            if(stream==draw.stream_count) {bases[stream]=address;strides[stream]=stride;++draw.stream_count;}
            layout[attribute]={stream,address-bases[stream],type};
        }
        if(inlined) {
            if(!inline_stride || inline_words.size()*4!=std::size_t(count)*inline_stride)
                throw std::runtime_error("NV097 inline vertex payload differs from its layout");
            draw.packed=inline_words;draw.stream_count=1;
            draw.streams[0]={std::as_bytes(std::span(draw.packed)),inline_stride};
        }
        for(unsigned stream=0;!inlined && stream<draw.stream_count;++stream) {
            unsigned final_bytes=0;
            for(const auto& attribute:layout) if(attribute.stream==stream) final_bytes=std::max(final_bytes,attribute.offset+attribute_bytes(attribute.format));
            const auto size=std::uint64_t(count-1)*strides[stream]+final_bytes;
            const auto address=std::uint64_t(bases[stream])+std::uint64_t(first)*strides[stream];
            if(address>UINT32_MAX || size>8*1024*1024) throw std::runtime_error("NV097 vertex stream exceeds its bound");
            draw.streams[stream]={Bytes(static_cast<const std::byte*>(memory.access(static_cast<std::uint32_t>(address),static_cast<std::size_t>(size))),static_cast<std::size_t>(size)),strides[stream]};
        }
        draw.layout=renderer.prepare_layout(shader,layout);renderer.prepare(draw.state);
        if(!capture_directory.empty()) capture_draw(first,draw,shader);
        draw.indices.assign(elements.begin(),elements.end());
        if(draw.state.color_mask) {active_surface->revision=++surface_revision;active_surface->color_cleared=false;}
        if(++draw_count==draws.size()) flush();
    }
    void capture_draw(std::uint32_t first,const Draw& draw,std::uint32_t shader) {
        if(capture_trace.tellp()>32*1024*1024 || capture_draws==1024) {capture_truncated=true;return;}
        capture_trace<<std::format("{{\"type\":\"draw\",\"draw\":{},\"first\":{},\"count\":{},\"target\":{},\"shader\":{},\"program_start\":{},\"registers\":{},\"program\":{},\"constants\":{},\"current\":{},\"textures\":[",
            capture_draws++,first,draw.count,target,shader,program.start,json(hex_bytes(std::as_bytes(std::span(registers[0])))),
            json(hex_bytes(std::as_bytes(std::span(program.tokens)))),json(hex_bytes(std::as_bytes(std::span(constants)))),
            json(hex_bytes(std::as_bytes(std::span(current.attributes)))));
        for(unsigned i=0;i<4;++i) {
            if(i) capture_trace<<',';const auto& scale=draw.view.texture_scales[i];
            capture_trace<<std::format("{{\"resource\":{},\"scale\":[{},{},{},{}]}}",draw.state.textures[i],scale[0],scale[1],scale[2],scale[3]);
        }
        capture_trace<<"],\"streams\":[";
        for(unsigned i=0;i<draw.stream_count;++i) {
            if(i) capture_trace<<',';const auto& stream=draw.streams[i];
            capture_trace<<std::format("{{\"stride\":{},\"size\":{},\"prefix\":{}}}",stream.stride,stream.bytes.size(),json(hex_bytes(stream.bytes.first(std::min<std::size_t>(128,stream.bytes.size())))));
        }
        capture_trace<<"]}\n";
    }
    void capture_flip() {
        if(!capture_directory.empty()) {
            save_png(renderer.read_target(0),capture_directory/"frame.png");
            for(const auto& [key,surface]:targets) {
                const auto name=std::format("target-{}-{:08X}.png",surface.identifier,key[0]);
                save_png(renderer.read_target(surface.identifier),capture_directory/name);
                capture_trace<<std::format("{{\"type\":\"surface\",\"resource\":{},\"color\":{},\"depth\":{},\"format\":{},\"pitch\":{},\"width\":{},\"height\":{},\"image\":{}}}\n",
                    surface.identifier,json(hex32(key[0])),json(hex32(key[1])),json(hex32(key[2])),json(hex32(key[3])),surface.width,surface.height,json(name));
            }
            capture_trace<<std::format("{{\"type\":\"end\",\"draws\":{},\"truncated\":{}}}\n",capture_draws,capture_truncated);
            write_text(capture_directory/"draws.jsonl",capture_trace.view(),false);
            capture_directory.clear();capture_trace=std::ostringstream{};
        }
        if(!capture_requested.empty()) {
            capture_directory=std::move(capture_requested);capture_requested.clear();
            std::filesystem::create_directories(capture_directory);
            capture_draws=0;capture_truncated=false;
            capture_trace<<"{\"type\":\"header\",\"format\":\"b2-gpu-frame-v1\",\"game_inputs_automated\":false}\n";
        }
    }
    void fence(std::uint32_t address,std::uint32_t value) {
        flush();if(fence_count==fences.size()) poll(true);
        auto& result=fences[(fence_head+fence_count)%fences.size()];result.address=address;result.value=value;
        renderer.native_context()->End(result.query.Get());++fence_count;++statistics.fences;
    }
    void poll(bool wait) {
        const auto deadline=std::chrono::steady_clock::now()+std::chrono::seconds(5);
        if(wait) renderer.native_context()->Flush();
        while(fence_count) {
            BOOL finished=FALSE;
            const auto status=renderer.native_context()->GetData(fences[fence_head].query.Get(),&finished,sizeof(finished),wait?0:D3D11_ASYNC_GETDATA_DONOTFLUSH);
            checked_gpu(status);
            if(status!=S_OK || !finished) {
                if(!wait) return;
                if(std::chrono::steady_clock::now()>=deadline) throw std::runtime_error("Native GPU completion timed out");
                SwitchToThread();continue;
            }
            const auto& completed=fences[fence_head];if(completed.address) memory.store<std::uint32_t>(completed.address,completed.value);
            fence_head=(fence_head+1)%static_cast<unsigned>(fences.size());--fence_count;
        }
    }
    void method(unsigned subchannel,unsigned method,std::uint32_t data) {
        ++statistics.methods;
        if(method==0) {objects[subchannel]=data;return;}
        registers[subchannel][method/4]=data;
        if(subchannel!=0) {
            if(method>=0x300) throw std::runtime_error(std::format("Unimplemented GPU object 0x{:X}, subchannel {} method 0x{:X}",objects[subchannel],subchannel,method));
            return;
        }
        if(method==0x100 && !data) return;
        if(objects[0]!=0xD) throw std::runtime_error(std::format("NV097 object 0x{:X} is unbound at method 0x{:X}, value 0x{:X}",objects[0],method,data));
        if(method==0x17FC) {
            if(data) {if(primitive) throw std::runtime_error("Nested NV097 begin/end");primitive=data;indices.clear();inline_words.clear();}
            else {
                if(!inline_words.empty()) {
                    if(!indices.empty()) throw std::runtime_error("NV097 inline arrays and elements are mixed");
                    unsigned stride=0;
                    for(unsigned attribute=0;attribute<16;++attribute) {
                        const auto format=reg(0x1760+attribute*4)&255;
                        if((format>>4)&15) stride+=(attribute_bytes(format)+3)&~3U;
                    }
                    if(!stride || (inline_words.size()*4)%stride) throw std::runtime_error("Incomplete NV097 inline vertex");
                    draw(0,static_cast<std::uint32_t>(inline_words.size()*4/stride),{},true);
                }
                if(!indices.empty()) {const auto maximum=*std::ranges::max_element(indices);draw(0,maximum+1,indices);}
                primitive=0;indices.clear();
            }
        } else if(method==0x1810) draw(data&0xFFFFFF,(data>>24)+1);
        else if(method==0x1800) {indices.push_back(data&0xFFFF);indices.push_back(data>>16);}
        else if(method==0x1808) indices.push_back(data);
        else if(method==0x1818) inline_words.push_back(data);
        else if(method>=0x1880 && method<=0x18FC) {
            const auto attribute=(method-0x1880)/8,lane=((method-0x1880)/4)%2;
            if(!lane) current.attributes[attribute]={real(data),0,0,1};else current.attributes[attribute][1]=real(data);
        } else if(method>=0x1900 && method<=0x193C) current.attributes[(method-0x1900)/4]={
            float(static_cast<std::int16_t>(data)),float(static_cast<std::int16_t>(data>>16)),0,1};
        else if(method>=0x1940 && method<=0x197C) current.attributes[(method-0x1940)/4]={
            float(data&255)/255,float((data>>8)&255)/255,float((data>>16)&255)/255,float(data>>24)/255};
        else if(method>=0x1980 && method<=0x19FC) {
            const auto attribute=(method-0x1980)/8,lane=((method-0x1980)/4)%2*2;
            current.attributes[attribute][lane]=float(static_cast<std::int16_t>(data));
            current.attributes[attribute][lane+1]=float(static_cast<std::int16_t>(data>>16));
        } else if(method>=0x1A00 && method<=0x1AFC) current.attributes[(method-0x1A00)/16][((method-0x1A00)/4)%4]=real(data);
        else if(method>=0x440 && method<=0x47C) constants[4+(method-0x440)/16][((method-0x440)/4)%4]=real(data);
        else if(method>=0x480 && method<=0x57C) {
            const auto index=(method-0x480)/4;constants[8+(index/16)*8+(index%16)/4][index%4]=real(data);
        } else if(method>=0x580 && method<=0x67C) {
            const auto index=(method-0x580)/4;constants[12+(index/16)*8+(index%16)/4][index%4]=real(data);
        } else if(method>=0x680 && method<=0x6BC) constants[(method-0x680)/16][((method-0x680)/4)%4]=real(data);
        else if(method>=0x6C0 && method<=0x7BC) {
            const auto index=(method-0x6C0)/4;constants[68+(index/16)*8+(index%16)/4][index%4]=real(data);
        } else if(method>=0x840 && method<=0x93C) {
            const auto index=(method-0x840)/4;constants[64+(index/16)*8+(index%16)/4][index%4]=real(data);
        } else if(method>=0x9D0 && method<=0x9DC) constants[57][(method-0x9D0)/4]=real(data);
        else if(method>=0xA50 && method<=0xA5C) constants[56][(method-0xA50)/4]=real(data);
        else if(method>=0xA20 && method<=0xA2C) constants[59][(method-0xA20)/4]=real(data);
        else if(method>=0xAF0 && method<=0xAFC) constants[58][(method-0xAF0)/4]=real(data);
        else if(method==0x1E9C) program_load=data;
        else if(method==0x1EA0) program.start=data;
        else if(method==0x1EA4) constant_load=data;
        else if(method>=0xB00 && method<=0xB7C) {
            if(program_load>=program.tokens.size()) throw std::runtime_error("NV097 program upload outside instruction memory");
            const auto lane=((method-0xB00)/4)%4;program.tokens[program_load][lane]=data;if(lane==3) ++program_load;
        } else if(method>=0xB80 && method<=0xBFC) {
            if(constant_load>=constants.size()) throw std::runtime_error("NV097 constant upload outside context memory");
            const auto lane=((method-0xB80)/4)%4;constants[constant_load][lane]=real(data);if(lane==3) ++constant_load;
        } else if(method==0x1E90) {
            flush();auto launch=program;launch.start=data;ProgramVertex input{};
            for(unsigned lane=0;lane<4;++lane) input.attributes[0][lane]=real(reg(0x1E80+lane*4));
            renderer.launch_program(renderer.prepare_program(launch,true),constants,input);
        } else if(method==0x1D94) {
            flush();const auto complete_clear=select_target(data);
            const auto h=reg(0x1D98),v=reg(0x1D9C);
            const std::array<std::int32_t,4> rectangle{std::int32_t(h&0xFFFF),std::int32_t(v&0xFFFF),
                std::int32_t((h>>16)+1),std::int32_t((v>>16)+1)};
            const auto value=reg(0x1D8C),zeta=(reg(0x208)>>4)&15;
            renderer.clear_buffers(data,reg(0x1D90),zeta==1?float(value&0xFFFF)/65535:float(value>>8)/16777215,static_cast<std::uint8_t>(value),rectangle);
            if(data&0xF0) {active_surface->revision=++surface_revision;active_surface->color_cleared=complete_clear;}
            if(!capture_directory.empty()) {
                if(capture_trace.tellp()>32*1024*1024) capture_truncated=true;
                else capture_trace<<std::format("{{\"type\":\"clear\",\"target\":{},\"flags\":{},\"color\":{},\"format\":{},\"rectangle\":[{},{},{},{}]}}\n",
                    target,data,json(hex32(reg(0x1D90))),json(hex32(reg(0x208))),rectangle[0],rectangle[1],rectangle[2],rectangle[3]);
            }
            ++statistics.clears;
        } else if(method==0x1D70) {
            if(reg(0x1A4)!=8 || reg(0x1D6C)>92) throw std::runtime_error("Unbound NV097 semaphore DMA range");
            fence(semaphore_base+reg(0x1D6C),data);
        } else if(method==0x110) {flush();fence(0,0);poll(true);}
        else if(method==0x12C) {flush();select_target();renderer.copy_to_main();target=0;active_surface=nullptr;++statistics.flips;capture_flip();if(owner.flip) owner.flip();}
        if(indices.size()>32768 || inline_words.size()>2*1024*1024) throw std::runtime_error("NV097 inline draw exceeds its bound");
    }
};
Gpu::Gpu(Renderer& renderer,Memory& memory,std::uint32_t semaphore_base):impl_(std::make_unique<Impl>(renderer,memory,*this,semaphore_base)) {}
Gpu::~Gpu()=default;
void Gpu::capture_frame(const std::filesystem::path& directory) {
    if(directory.empty() || !impl_->capture_requested.empty() || !impl_->capture_directory.empty())
        throw std::runtime_error("A frame capture is already pending");
    impl_->capture_requested=directory;
}
void Gpu::submit(std::uint32_t begin,std::uint32_t end) {
    auto& g=*impl_;
    if(end<begin || ((end-begin)&3) || end-begin>4*1024*1024) throw std::runtime_error("Invalid native GPU submission range");
    ++g.statistics.submissions;
    while(begin<end) {
        const auto header=g.memory.load<std::uint32_t>(begin);begin+=4;++g.statistics.packets;
        if(header&0xA0030003U) throw std::runtime_error(std::format("Unsupported NV2A packet 0x{:08X} at 0x{:08X}",header,begin-4));
        const auto count=(header>>18)&0x7FF,first=header&0x1FFC,subchannel=(header>>13)&7;
        if(count>(end-begin)/4 || (!(header&0x40000000U) && count && first+(count-1)*4>=0x2000))
            throw std::runtime_error("NV2A packet extends beyond its submission or method bank");
        for(unsigned word=0;word<count;++word) {
            const auto method=first+((header&0x40000000U)?0:word*4);
            g.method(subchannel,method,g.memory.load<std::uint32_t>(begin));begin+=4;
        }
    }
    g.flush();g.renderer.native_context()->Flush();g.poll(false);
}
bool Gpu::busy() {auto& g=*impl_;g.poll(false);return g.fence_count!=0;}
void Gpu::wait() {auto& g=*impl_;g.flush();g.fence(0,0);g.poll(true);}
void Gpu::snapshot() {
    auto& g=*impl_;g.flush();
    // Main holds the last presented frame. The bound surface may belong to an
    // unfinished frame or an offscreen pass when execution stops.
    if(g.statistics.flips) g.renderer.set_target(0);
    else if(g.target) g.renderer.copy_to_main();
    g.target=0;
}
GpuStats Gpu::stats() const {return impl_->statistics;}
}
