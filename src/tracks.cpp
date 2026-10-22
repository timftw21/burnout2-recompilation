#include "tracks.h"
#include "disc.h"
#include <algorithm>
#include <array>
#include <bit>
#include <cmath>
#include <cstring>
#include <format>
#include <map>
#include <stdexcept>

namespace b2 {
namespace {
constexpr unsigned sector_bytes=0x3C000,master_base=0x8381C000;
using Data=std::vector<std::byte>;
void require(bool condition,std::string_view message) {
    if(!condition) throw std::runtime_error("Interstate import: "+std::string(message));
}
void put(Data& data,unsigned offset,std::uint32_t value) {
    require(offset<=data.size() && data.size()-offset>=4,"output field exceeds its bounds");
    std::memcpy(data.data()+offset,&value,4);
}
float real(Bytes data,unsigned offset) {return std::bit_cast<float>(u32(data,offset));}
void put(Data& data,unsigned offset,float value) {put(data,offset,std::bit_cast<std::uint32_t>(value));}
unsigned aligned(unsigned value,unsigned alignment=16) {return (value+alignment-1)&~(alignment-1);}
std::string text(Bytes data,unsigned offset,unsigned maximum) {
    require(offset<=data.size() && maximum<=data.size()-offset,"source name exceeds its bounds");
    const auto begin=reinterpret_cast<const char*>(data.data()+offset);
    const auto end=std::find(begin,begin+maximum,'\0');
    require(end!=begin+maximum,"unterminated source name");return {begin,end};
}
std::string lower(std::string value) {
    for(auto& c:value) if(c>='A' && c<='Z') c+=32;return value;
}
Data read(Disc& disc,const DiscEntry& entry,unsigned offset,unsigned bytes) {
    require(bytes<=8*1024*1024,"source read exceeds 8 MiB");
    Data data(bytes);disc.read(entry,offset,data);return data;
}
void save(const std::filesystem::path& path,Bytes bytes) {
    std::ofstream file(path,std::ios::binary|std::ios::trunc);
    file.write(reinterpret_cast<const char*>(bytes.data()),static_cast<std::streamsize>(bytes.size()));
    if(!file) throw std::runtime_error("Cannot write imported track: "+path_text(path));
}
struct PendingImport {
    std::filesystem::path directory;bool published=false;
    ~PendingImport() {
        if(published) return;
        std::error_code error;
        for(const auto name:{"interstate.pss","interstate.psw","interstate.bgd","interstate.bcd","import.json"}) std::filesystem::remove(directory/name,error);
        std::filesystem::remove(directory,error);
    }
};
struct Texture {std::string name;unsigned width,height,mips,format,raster,filter;bool alpha;Data pixels;};
std::vector<Texture> textures(Disc& disc,const DiscEntry& entry,std::string_view selected={}) {
    // RW 3.1 Xbox dictionaries put a size word before EACH mip level.
    const auto data=read(disc,entry,0,entry.size);
    require(u32(data,0)==0x16 && u32(data,4)+12==data.size() && u32(data,12)==1,"unsupported Burnout texture dictionary");
    const auto count=u16(data,24);require(count && count<=512,"invalid texture count");
    unsigned cursor=28;std::vector<Texture> result;
    for(unsigned i=0;i<count;++i) {
        require(u32(data,cursor)==0x15 && u32(data,cursor+12)==1,"invalid native texture chunk");
        const auto end=cursor+12+u32(data,cursor+4),header=cursor+24;
        require(end<=data.size() && u32(data,header)==5,"unsupported native texture platform");
        if(!selected.empty() && lower(text(data,header+8,32))!=selected) {cursor=end;continue;}
        Texture texture{text(data,header+8,32),u16(data,header+80),u16(data,header+82),
            unsigned(data[header+85]),unsigned(data[header+87]),u32(data,header+72),u32(data,header+4),u32(data,header+76)!=0,{}};
        require(texture.width && texture.height && texture.width<=1024 && texture.height<=1024 &&
            std::has_single_bit(texture.width) && std::has_single_bit(texture.height) && texture.mips && texture.mips<=11,
            "invalid texture dimensions or mip count");
        require(texture.format==0x0C || texture.format==0x0E || texture.format==0x0F,"unsupported Burnout texture encoding");
        auto offset=header+88;unsigned width=texture.width,height=texture.height;
        for(unsigned mip=0;mip<texture.mips;++mip) {
            const auto size=u32(data,offset);offset+=4;
            const auto expected=std::max(1U,(width+3)/4)*std::max(1U,(height+3)/4)*(texture.format==0x0C?8U:16U);
            require(size==expected && offset<=end && size<=end-offset,"invalid texture mip size");
            texture.pixels.insert(texture.pixels.end(),data.begin()+offset,data.begin()+offset+size);offset+=size;
            width=std::max(1U,width/2);height=std::max(1U,height/2);
        }
        require(offset==header+u32(data,cursor+16),"unexpected native mip payload");
        result.push_back(std::move(texture));cursor=end;
    }
    return result;
}
struct Mesh {Data vertices;std::vector<std::uint16_t> indices;};
struct Section {Data bounds;std::map<unsigned,Mesh> meshes;std::map<unsigned,std::vector<unsigned>> instances;};
struct Prop {unsigned material;Mesh mesh;};
Bytes chunk(Bytes parent,unsigned kind) {
    for(unsigned at=0;at<parent.size();) {
        require(parent.size()-at>=12,"truncated object chunk");const auto size=u32(parent,at+4);
        require(size<=parent.size()-at-12,"object chunk exceeds its parent");
        if(u32(parent,at)==kind) return parent.subspan(at+12,size);
        at+=12+size;
    }
    throw std::runtime_error("Interstate import: missing object chunk");
}
Mesh barriers(Disc& disc) {
    const auto entry=disc.find("tracks/usa2/chev.dff");const auto data=read(disc,entry,0,entry.size);
    const auto clump=chunk(data,0x10),frames=chunk(chunk(clump,0x0E),1);
    require(u32(chunk(clump,1),0)==1 && u32(frames,0)==2,"unsupported barrier instances");
    for(unsigned frame=0;frame<2;++frame) for(unsigned field=0;field<12;++field)
        require(real(frames,4+frame*56+field*4)==(field<9 && field%4==0?1.0f:0.0f),"barrier frame is not in world coordinates");
    const auto geometries=chunk(clump,0x1A),geometry=chunk(geometries,0x0F),vertices=chunk(geometry,1);
    require(u32(chunk(geometries,1),0)==1 && u32(vertices,0)==0x3D && u32(vertices,12)==1,"unsupported barrier geometry");
    const auto materials=chunk(geometry,8),material=chunk(materials,7),texture=chunk(chunk(material,6),2);
    require(u32(chunk(materials,1),0)==1 && text(texture,0,static_cast<unsigned>(texture.size()))=="d_arrws","unexpected barrier material");
    const auto count=u32(vertices,8),triangles=u32(vertices,4),uv=28+count*4,indices=uv+count*8,positions=indices+triangles*8+24;
    require(count && count<=65535 && triangles==712 && positions+count*24==vertices.size(),"invalid barrier vertex extent");
    Mesh result;result.vertices.resize(count*24);result.indices.reserve(triangles*3);
    for(unsigned i=0;i<count;++i) {
        std::memcpy(result.vertices.data()+i*24,vertices.data()+positions+i*12,12);
        std::memcpy(result.vertices.data()+i*24+12,vertices.data()+uv+i*8,8);
        put(result.vertices,i*24+20,u32(vertices,28+i*4));
    }
    for(unsigned i=0;i<triangles;++i) for(unsigned field:{2U,0U,6U}) {
        require(u16(vertices,indices+i*8+4)==0,"barrier triangle uses another material");
        const auto index=u16(vertices,indices+i*8+field);require(index<count,"barrier index exceeds its vertices");result.indices.push_back(index);
    }
    return result;
}
struct EnvironmentMesh {std::string texture;Mesh mesh;};
std::vector<EnvironmentMesh> environment(Disc& disc,std::string_view filename,bool dome) {
    const auto entry=disc.find("tracks/usa2/"+std::string(filename));const auto data=read(disc,entry,0,entry.size);
    const auto clump=chunk(data,0x10),frames=chunk(chunk(clump,0x0E),1);
    const auto atomic=chunk(chunk(clump,0x14),1);const auto frame=u32(atomic,0);
    require(u32(chunk(clump,1),0)==1 && u32(atomic,4)==0 && frame<u32(frames,0),"unsupported environment instance");
    // These two clumps have one atomic in an identity frame. Other frames in
    // skys.dff are unused; applying them would rotate the authored skyline.
    for(unsigned field=0;field<12;++field)
        require(real(frames,4+frame*56+field*4)==(field<9 && field%4==0?1.0f:0.0f),"environment frame is not in world coordinates");
    const auto geometries=chunk(clump,0x1A),geometry=chunk(geometries,0x0F),vertices=chunk(geometry,1);
    require(u32(chunk(geometries,1),0)==1 && (u32(vertices,0)==0x3D || u32(vertices,0)==0x7D) &&
        u32(vertices,12)==1,"unsupported environment geometry");
    const auto count=u32(vertices,8),triangles=u32(vertices,4),uv=28+count*4,indices=uv+count*8,positions=indices+triangles*8+24;
    require(count && count<=65535 && positions+count*24==vertices.size(),"invalid environment vertex extent");
    const auto materials=chunk(geometry,8);const auto material_count=u32(chunk(materials,1),0);
    require(material_count==(dome?1U:3U) && triangles==(dome?528U:481U),"unexpected environment mesh count");
    std::vector<EnvironmentMesh> result;unsigned material_at=12+u32(materials,4);
    for(unsigned material_index=0;material_index<material_count;++material_index) {
        require(u32(materials,material_at)==7,"invalid environment material");
        const auto material=materials.subspan(material_at+12,u32(materials,material_at+4)),properties=chunk(material,1);
        EnvironmentMesh output;
        if(u32(properties,12)) {const auto name=chunk(chunk(material,6),2);output.texture=lower(text(name,0,static_cast<unsigned>(name.size())));}
        else output.texture="interstate_white";
        output.mesh.vertices.resize(count*24);
        const auto radius=real(vertices,indices+triangles*8+12);
        require(radius>0 && std::isfinite(radius),"invalid environment radius");
        for(unsigned i=0;i<count;++i) {
            for(unsigned axis=0;axis<3;++axis) put(output.mesh.vertices,i*24+axis*4,real(vertices,positions+i*12+axis*4)/(dome?radius:1.0f));
            std::memcpy(output.mesh.vertices.data()+i*24+12,vertices.data()+uv+i*8,8);
            unsigned colour=0;
            for(unsigned channel=0;channel<4;++channel) {
                const auto value=unsigned(vertices[28+i*4+channel])*unsigned(properties[4+channel])/255;
                colour|=value<<((channel==0?2:channel==2?0:channel)*8);
            }
            put(output.mesh.vertices,i*24+20,colour);
        }
        for(unsigned triangle=0;triangle<triangles;++triangle) {
            if(u16(vertices,indices+triangle*8+4)!=material_index) continue;
            auto& strip=output.mesh.indices;const auto first=u16(vertices,indices+triangle*8+2);
            if(!strip.empty()) {strip.push_back(strip.back());strip.push_back(first);if(strip.size()&1) strip.push_back(first);}
            for(unsigned field:{2U,0U,6U}) {
                const auto index=u16(vertices,indices+triangle*8+field);require(index<count,"environment index exceeds its vertices");strip.push_back(index);
            }
        }
        require(!output.mesh.indices.empty(),"environment material has no triangles");
        result.push_back(std::move(output));material_at+=12+u32(materials,material_at+4);
    }
    return result;
}
unsigned append(Data& output,Bytes bytes,unsigned alignment=16) {
    const auto offset=aligned(static_cast<unsigned>(output.size()),alignment);
    output.resize(offset);output.insert(output.end(),bytes.begin(),bytes.end());return offset;
}
void collision(Disc& disc,const DiscEntry& hit,unsigned& cursor,Data& sector) {
    const auto header=read(disc,hit,cursor,48);cursor+=48;
    const auto vertices=u32(header,0),triangles=u32(header,16),vertex_extra=u32(header,8),triangle_extra=u32(header,24);
    require(vertices<=16384 && triangles<=16384 && vertex_extra==4 && !triangle_extra,"unsupported collision section");
    const auto size=(vertices+triangles)*16+vertices*vertex_extra;
    auto source=read(disc,hit,cursor,size);cursor+=size;
    // Surface numbers are game-specific. Match the original rolling-resistance
    // groups (B1 B9EB8, B2 5FB90), and the solid/non-solid boundary in BA9B4 /
    // 5EC06. In particular, B1's 14/15 are road; B2 rejects them as walls.
    for(unsigned i=0;i<triangles;++i) {
        const auto at=vertices*16+i*16+12,flags=u32(source,at);auto surface=flags&0xFFFFU;
        if(surface>=7 && surface<=11) --surface;
        else if(surface>=12 && surface<=15) surface=11;
        else if(surface>=26 && surface<=30) --surface;
        put(source,at,(flags&0xFFFF0000U)|surface);
    }
    const auto v=append(sector,Bytes(source).first(vertices*16));
    const auto t=append(sector,Bytes(source).subspan(vertices*16,triangles*16));
    for(unsigned i=0;i<triangles;++i) for(unsigned j=0;j<3;++j)
        require(u32(source,vertices*16+i*16+j*4)<vertices,"collision triangle references a missing vertex");
    const auto tree_bytes=u32(header,32);require(tree_bytes>=16 && tree_bytes<=128*1024,"invalid collision tree size");
    const auto tree=read(disc,hit,cursor,tree_bytes);cursor+=tree_bytes;
    const auto leaves=u32(tree,0);require(leaves>1 && leaves<=8192 && 16+(leaves-1)*16+leaves*4==tree.size(),"unsupported collision tree layout");
    Data converted(tree.begin(),tree.begin()+16+(leaves-1)*16);
    const auto tree_offset=aligned(static_cast<unsigned>(sector.size()));
    put(converted,0,leaves-1);put(converted,4,tree_offset+16);put(converted,8,0U);put(converted,12,0U);
    for(unsigned i=0;i<leaves-1;++i) for(unsigned child=0;child<2;++child) {
        const auto node=16+i*16,kind=unsigned(tree[node+2+child]);const auto index=u16(tree,node+4+child*2);
        require(u16(tree,node)<=8 && !(u16(tree,node)&3),"invalid collision split axis");
        if(kind==2) {require(index<leaves-1,"invalid collision branch");converted[node+2+child]=std::byte{0xFF};}
        else {
            require(kind==1 && index<leaves,"invalid collision leaf");
            const auto leaf=16+(leaves-1)*16+index*4;const auto count=u16(tree,leaf),first=u16(tree,leaf+2);
            require(count<255 && first+count<=triangles,"collision leaf exceeds its triangles");
            converted[node+2+child]=std::byte(count);std::memcpy(converted.data()+node+4+child*2,&first,2);
        }
    }
    const auto b=append(sector,converted);
    put(sector,0x80,vertices);put(sector,0x84,v);put(sector,0x8C,t);
    put(sector,0x90,triangles);put(sector,0x94,t);put(sector,0x9C,b);
    put(sector,0xA0,static_cast<unsigned>(converted.size()));put(sector,0xA4,b);put(sector,0xB0,b);
}
unsigned configuration(Bytes data) {
    unsigned offset=0x160;
    constexpr unsigned strides[]={16,20,36,44,36,60,36,272,68,336,176,32,44};
    for(unsigned i=0;i<std::size(strides);++i) offset+=u32(data,0x104+i*4)*strides[i];
    require(offset<=data.size() && 0x3E0<=data.size()-offset,"invalid race configuration extent");return offset;
}
struct Traffic {
    Data indices,distances,attributes,curves,blocks,spawns,transitions;
};
Traffic traffic_data(Bytes source,unsigned points,unsigned frames,Data& positions) {
    Traffic result;
    const auto vertices=u32(source,0x44),records=points+vertices*40+u32(source,0x48)*920;
    const auto source_paths=u32(source,0x40),road_count=u32(source,0x6C)-1;
    struct Lane {unsigned first,count,density,heavy;};
    std::vector<Lane> lanes;
    for(unsigned i=0;i<u32(source,0x4C);++i) {
        const auto record=records+i*2396;
        if(u32(source,record)!=1) continue; // Continuous generators, not triggered sequences.
        const auto path=u32(source,record+4);
        require(path<source_paths,"traffic generator references a missing path");
        const auto descriptor=0x1B4+path*16,first=u32(source,descriptor+8),count=u32(source,descriptor+4);
        require(count>=2 && first+count<=vertices,"traffic lane exceeds its source points");
        unsigned previous=0,heavy=0;float gap=unsigned(source[record+10]);
        for(unsigned group=0;group<3;++group) {
            const auto threshold=unsigned(source[record+0x2E0+group*2]),kind=unsigned(source[record+0x2E1+group*2]);
            require(threshold>=previous && threshold<=100 && kind<3,"invalid traffic mixture");
            const auto weight=threshold-previous;previous=threshold;
            gap+=weight*0.005f*(unsigned(source[record+0x304+kind*2])+unsigned(source[record+0x305+kind*2]));
            if(kind==2) heavy+=weight;
        }
        require(previous==100 && gap>0,"incomplete traffic generator");
        // B2 expresses occupancy as vehicle length per 500 m. Its native
        // generator uses 4 m half-length and sqrt(U+.5), then smooths intervals.
        const auto density=std::clamp(unsigned(std::lround(3956.17f/gap)),1U,255U);
        lanes.push_back({first,count,density,heavy});
    }
    require(!lanes.empty() && lanes.size()<=320,"unsupported traffic lane count");
    for(const auto& lane:lanes) {
        const auto base=static_cast<unsigned>(positions.size()/16),curve=static_cast<unsigned>(result.curves.size());
        result.curves.resize(curve+32);
        put(result.curves,curve,lane.count-1);put(result.curves,curve+4,0x20002U);
        put(result.curves,curve+8,static_cast<unsigned>(result.indices.size()/4));
        put(result.curves,curve+12,static_cast<unsigned>(result.distances.size()/8));
        put(result.curves,curve+16,static_cast<unsigned>(result.attributes.size()/48));
        positions.resize(positions.size()+lane.count*32);
        for(unsigned i=0;i<lane.count;++i) {
            const auto point=points+(lane.first+i)*16;
            const auto before=points+(lane.first+(i?i-1:i))*16,after=points+(lane.first+std::min(i+1,lane.count-1))*16;
            const auto dx=real(source,after)-real(source,before),dz=real(source,after+8)-real(source,before+8),norm=std::hypot(dx,dz);
            require(norm>0 && std::isfinite(norm),"traffic lane has an invalid tangent");
            // B2 represents lanes with paired edges. Use its six-metre lane
            // width while retaining the exact source centre and elevation.
            for(unsigned side=0;side<2;++side) for(unsigned axis=0;axis<3;++axis) {
                const auto lateral=axis==0?dz/norm:axis==2?-dx/norm:0.0f;
                put(positions,(base+i*2+side)*16+axis*4,real(source,point+axis*4)+lateral*(side?-3.0f:3.0f));
            }
            const auto index=static_cast<unsigned>(result.indices.size());result.indices.resize(index+4);
            put(result.indices,index,(base+i*2)|((base+i*2+1)<<16));
            if(i+1==lane.count) continue;
            const auto distance=static_cast<unsigned>(result.distances.size());result.distances.resize(distance+8);
            put(result.distances,distance,real(source,point+28)-real(source,points+lane.first*16+12));
            put(result.distances,distance+4,6.0f);
            const auto attr=static_cast<unsigned>(result.attributes.size());result.attributes.resize(attr+48);
            std::fill_n(result.attributes.begin()+attr,24,std::byte{0xFF});
            result.attributes[attr+24]=std::byte{100};result.attributes[attr+30]=std::byte{100};
            result.attributes[attr+35]=std::byte(lane.density);
            result.attributes[attr+36]=std::byte(100-lane.heavy);result.attributes[attr+40]=std::byte(lane.heavy);
            const auto speed=unsigned(source[points+vertices*16+(lane.first+i)*24]);
            result.attributes[attr+42]=std::byte(std::clamp(unsigned(std::lround(speed/0.44704f)),1U,255U));
        }
    }
    require(positions.size()/16<=65535 && result.attributes.size()/48<=16000,"traffic exceeds native route capacity");
    // Use the original B2 spatial activation system. Each block covers about
    // 150 m of lap progress and only retains lanes within its 350 m window.
    struct Range {unsigned lane,begin,end;};
    std::vector<std::vector<Range>> ranges;
    for(unsigned begin=0;begin<road_count;) {
        unsigned end=begin+1;
        while(end<road_count && real(source,frames+end*80+12)-real(source,frames+begin*80+12)<150) ++end;
        const auto block=static_cast<unsigned>(result.blocks.size());result.blocks.resize(block+32);
        put(result.blocks,block+16,begin);put(result.blocks,block+20,end);
        put(result.blocks,block+24,350.0f);put(result.blocks,block+28,300.0f);
        auto& active=ranges.emplace_back();
        for(unsigned lane=0;lane<lanes.size();++lane) {
            const auto& path=lanes[lane];unsigned first=UINT32_MAX;
            for(unsigned i=0;i<path.count;++i) {
                bool near=false;
                if(i+1<path.count) for(unsigned road=begin;road<=end && !near;++road) {
                    const auto point=points+(path.first+i)*16,frame=frames+road*80;
                    const auto dx=real(source,point)-real(source,frame),dz=real(source,point+8)-real(source,frame+8);
                    near=dx*dx+dz*dz<350*350;
                }
                if(near && first==UINT32_MAX) first=i;
                if(!near && first!=UINT32_MAX) {active.push_back({lane,first,i});first=UINT32_MAX;}
            }
        }
        put(result.blocks,block,static_cast<unsigned>(active.size()));
        put(result.blocks,block+4,static_cast<unsigned>(result.spawns.size()/8));
        for(const auto& range:active) {
            const auto at=static_cast<unsigned>(result.spawns.size());result.spawns.resize(at+8);
            put(result.spawns,at,range.lane);put(result.spawns,at+4,range.begin|(range.end<<16));
        }
        begin=end;
    }
    for(unsigned block=0;block<ranges.size();++block) {
        const auto& previous=ranges[(block+ranges.size()-1)%ranges.size()];const auto& current=ranges[block];
        const auto start=static_cast<unsigned>(result.transitions.size()/8);
        for(unsigned lane=0;lane<lanes.size();++lane) {
            unsigned first=0,operation=0;
            for(unsigned i=0;i<lanes[lane].count;++i) {
                const auto inside=[&](const auto& set){return std::ranges::any_of(set,[&](const Range& r){return r.lane==lane && i>=r.begin && i<r.end;});};
                const auto was=inside(previous),now=inside(current);const auto next=was==now?0U:now?2U:1U;
                if(next==operation) continue;
                if(operation) {
                    const auto at=static_cast<unsigned>(result.transitions.size());result.transitions.resize(at+8);
                    put(result.transitions,at,lane|((operation-1)<<16));put(result.transitions,at+4,first|(i<<16));
                }
                first=i;operation=next;
            }
        }
        put(result.blocks,block*32+8,static_cast<unsigned>(result.transitions.size()/8)-start);
        put(result.blocks,block*32+12,start);
    }
    require(result.spawns.size()/8<=2048,"traffic exceeds native generator capacity");
    return result;
}
struct Cameras {Data records,opening;};
Cameras camera_data(Disc& source,Disc& target) {
    const auto entry=source.find("tracks/usa2/usa2rpc.sgd");const auto original=read(source,entry,0,entry.size);
    require(text(original,0,12)=="SGDVER 7" && u32(original,0x2C)==44 && u32(original,0x5C)==47 &&
        u32(original,0x40)==1 && u32(original,0x44)==438 && u32(original,0x48)==0,
        "unsupported Interstate replay camera data");
    const auto first=0x1B4+u32(original,0x40)*16+u32(original,0x44)*40;
    require(first+47*44<=original.size(),"replay cameras exceed the source file");
    Cameras result;
    result.records=Data(original.begin()+first,original.begin()+first+47*44);
    unsigned initial=0;while(initial<47 && !(std::to_integer<unsigned>(result.records[initial*44+26])&0x80)) ++initial;
    require(initial<47,"missing authored starting camera");const auto at=initial*44;
    // B1's starting camera is stationary and follows the waiting car (8B530).
    // Keep B2's opening duration, using the authored pose throughout. Convert
    // replay cameras to B2's native modes along B1's original road.
    const auto example=target.find("tracks/forward/fwyl/fwyl_0.bcd");
    const auto prototype=read(target,example,0,example.size);
    require(text(prototype,0,12)=="B2RIDV 0" && u32(prototype,12)==2 && u32(prototype,16)==722,
        "unsupported native opening camera layout");
    result.opening=Data(20+12+722*32+60);
    std::memcpy(result.opening.data(),prototype.data(),20);put(result.opening,12,1U);
    put(result.opening,20,0U);put(result.opening,24,721U);put(result.opening,28,1U);
    const auto x=real(result.records,at+12),y=real(result.records,at+16),z=real(result.records,at+20);
    const auto magnitude=std::sqrt(x*x+y*y+z*z);
    require(std::isfinite(magnitude) && magnitude>0.99f && magnitude<1.01f,"invalid authored camera direction");
    const auto yaw=std::atan2(x,z)*0.5f,pitch=-std::asin(y/magnitude)*0.5f;
    const auto cy=std::cos(yaw),sy=std::sin(yaw),cp=std::cos(pitch),sp=std::sin(pitch);
    for(unsigned i=0;i<722;++i) {
        const auto frame=32+i*32;
        put(result.opening,frame,cy*sp);put(result.opening,frame+4,sy*cp);
        put(result.opening,frame+8,-sy*sp);put(result.opening,frame+12,cy*cp);
        std::memcpy(result.opening.data()+frame+16,result.records.data()+at,12);
        put(result.opening,frame+28,float(std::to_integer<unsigned>(result.records[at+32])));
    }
    const auto tail=32+722*32;
    std::memcpy(result.opening.data()+tail,"B2RIDV 0",8);
    std::memcpy(result.opening.data()+tail+20,"B2RIDV 0",8);
    std::memcpy(result.opening.data()+tail+40,"B2RTDV 1",8);
    Data records,positions;const auto road=static_cast<unsigned>(original.size())-438*80;
    const auto add_position=[&](const std::array<float,3>& position) {
        const auto index=static_cast<unsigned>(positions.size()/12);positions.resize(positions.size()+12);
        for(unsigned axis=0;axis<3;++axis) put(positions,index*12+axis*4,position[axis]);return index;
    };
    const auto gate=[&](Data& record,unsigned point,unsigned camera) {
        const auto frame=road+point*80,source_camera=camera*44;
        const auto left=real(original,frame+28),right=real(original,frame+44),split=real(original,frame+72);
        auto low=left,high=right;
        if(!(std::to_integer<unsigned>(result.records[source_camera+26])&0x20) && split>left && split<right) {
            if(std::to_integer<unsigned>(result.records[source_camera+30])) high=split;else low=split;
        }
        const auto half=(high-low)*0.5f,center=(high+low)*0.5f;
        require(half>0 && point<437,"invalid source camera activation plane");
        const auto rx=real(original,frame+32),rz=real(original,frame+40);
        const auto fx=real(original,frame+16),fz=real(original,frame+24);
        const auto px=real(original,frame)-rx*center,pz=real(original,frame+8)-rz*center;
        // 89940 uses an ellipse crossed in the forward direction, guarded by
        // its road index. Put the gate on the source camera's road cross-section.
        put(record,0x38,point);put(record,0x44,rz/half);put(record,0x4C,rx/half);
        put(record,0x48,fz/half);put(record,0x50,fx/half);
        put(record,0x54,-(px*rx+pz*rz)/half);put(record,0x58,-(px*fx+pz*fz)/half);
    };
    for(unsigned i=0;i<47;++i) {
        const auto source_camera=i*44,point=unsigned(u16(result.records,source_camera+24));
        const auto kind=std::to_integer<unsigned>(result.records[source_camera+27]);
        require(point<437 && (kind==0 || kind==2 || kind==5 || kind==6 || kind==8),"unsupported source camera mode");
        Data record(104);put(record,0x10,i+1);
        const auto fov=std::to_integer<unsigned>(result.records[source_camera+32]);
        put(record,0x14,float(fov));put(record,0x18,0.5f);
        for(const auto offset:{0x1CU,0x20U,0x24U}) put(record,offset,0.5f);
        put(record,0x28,5.f);put(record,0x2C,10.f);put(record,0x30,2.f);
        const auto mode=kind==0?0x20U:kind==2?0x100U:kind==5?0x40U:0x200U;
        const auto zoom=std::to_integer<unsigned>(result.records[source_camera+33]);
        if(zoom) put(record,0x18,0.4f/float(zoom));
        put(record,0x34,0x800U|mode|(zoom?1U:0U));gate(record,point,i);
        const auto dx=real(result.records,source_camera+12),dy=real(result.records,source_camera+16),dz=real(result.records,source_camera+20);
        const auto norm=std::sqrt(dx*dx+dy*dy+dz*dz);
        require(norm>0.99f && norm<1.01f,"invalid source camera orientation");
        const auto a=std::atan2(dx,dz)*0.5f,b=-std::asin(dy/norm)*0.5f;
        put(record,0,std::cos(a)*std::sin(b));put(record,4,std::sin(a)*std::cos(b));
        put(record,8,-std::sin(a)*std::sin(b));put(record,12,std::cos(a)*std::cos(b));
        if(mode!=0x200) {
            std::array<float,3> position{};
            for(unsigned axis=0;axis<3;++axis) position[axis]=real(result.records,source_camera+axis*4);
            const auto index=add_position(position);put(record,0x3C,index);put(record,0x40,index);
            records.insert(records.end(),record.begin(),record.end());continue;
        }
        auto end=point+1;
        for(unsigned next=i+1;next<47;++next) if(u16(result.records,next*44+24)>point) {end=u16(result.records,next*44+24);break;}
        require(end<437,"unclosed source camera path");
        const auto signed_byte=[&](unsigned offset) {return float(static_cast<std::int8_t>(std::to_integer<unsigned char>(result.records[source_camera+offset])))*0.25f;};
        const auto lateral=signed_byte(40),longitudinal=signed_byte(41),height=signed_byte(42);
        const auto first=static_cast<unsigned>(positions.size()/12);
        for(unsigned p=point;p<=end;++p) {
            const auto frame=road+p*80;std::array<float,3> position{};
            for(unsigned axis=0;axis<3;++axis) position[axis]=real(original,frame+axis*4)+
                real(original,frame+16+axis*4)*longitudinal+real(original,frame+32+axis*4)*lateral;
            position[1]+=height;add_position(position);
        }
        put(record,0x3C,first);put(record,0x40,static_cast<unsigned>(positions.size()/12)-1);
        records.insert(records.end(),record.begin(),record.end());
        // Native spline cameras use the following record as their end gate.
        // A disabled marker also handles B1's alternate cameras at one road index.
        put(record,0x34,0xC00U);gate(record,end,i);
        records.insert(records.end(),record.begin(),record.end());
    }
    put(result.opening,tail+52,static_cast<unsigned>(records.size()/104));
    put(result.opening,tail+56,static_cast<unsigned>(positions.size()/12));
    result.opening.insert(result.opening.end(),records.begin(),records.end());
    result.opening.insert(result.opening.end(),positions.begin(),positions.end());
    require(result.opening.size()<0x20000,"camera data exceeds the native loading buffer");
    return result;
}
Data race_data(Disc& source,Disc& target,float& length,Bytes cameras) {
    const auto entry=source.find("tracks/usa2/usa21.sgd");const auto original=read(source,entry,0,entry.size);
    require(text(original,0,12)=="SGDVER 7" && u32(original,0x0C)==0x140 && u32(original,0x3C)==80 &&
        u32(original,0x70)+0x74==original.size(),"unsupported Interstate race data");
    const auto paths=u32(original,0x40),vertices=u32(original,0x44),source_count=u32(original,0x6C);
    require(paths<=128 && vertices<=65535 && source_count>=3 && source_count<=4096,"invalid route dimensions");
    const auto points=0x1B4+paths*16,frames=static_cast<unsigned>(original.size())-source_count*80;
    // B1 repeats the starting point at the end; B2 closes the index array.
    const auto count=source_count-1;
    require(!std::memcmp(original.data()+frames,original.data()+frames+count*80,12),"lap route is not closed");
    unsigned spine=UINT32_MAX;
    for(unsigned i=0;i<paths;++i) if(u32(original,0x1B4+i*16)==3) {
        require(spine==UINT32_MAX && u32(original,0x1B4+i*16+4)==source_count,"ambiguous Interstate lap route");
        spine=u32(original,0x1B4+i*16+8);
    }
    require(spine<vertices && source_count<=vertices-spine,"missing lap route");
    const auto example=target.find("tracks/forward/fwyl/fwyl1_01.bgd");auto prototype=read(target,example,0,example.size);
    const auto setup=configuration(prototype);Data result(0x160);
    std::memcpy(result.data(),prototype.data(),0x104);
    for(unsigned player=0;player<4;++player) {
        std::memcpy(result.data()+0x20+player*16,original.data()+0x78+player*12,12);
        std::memcpy(result.data()+0x60+player*16,original.data()+0xA8+player*12,12);
    }
    put(result,0xA0,0xB0U);put(result,0x104,count*2);put(result,0x108,count);put(result,0x10C,1U);
    put(result,0x100,0x3E0U);
    Data positions(count*32),indices((count+1)*4),distances(count*8),parameters(count*8),curve(32);
    for(unsigned i=0;i<count;++i) {
        const auto frame=frames+i*80,point=points+(spine+i)*16;
        require(!std::memcmp(original.data()+frame,original.data()+point,16),"lap frames differ from route points");
        const auto left=real(original,frame+28),right=real(original,frame+44);
        require(std::isfinite(left) && std::isfinite(right) && right>left && right-left<128,"invalid route boundaries");
        for(unsigned side=0;side<2;++side) for(unsigned axis=0;axis<3;++axis)
            put(positions,i*32+side*16+axis*4,real(original,frame+axis*4)-real(original,frame+32+axis*4)*(side?right:left));
        put(indices,i*4,(i*2)|((i*2+1)<<16));
        const auto end=real(original,frame+80+12);
        require(std::isfinite(end) && end>real(original,frame+12),"invalid lap distance");
        put(distances,i*8,end);put(distances,i*8+4,right-left);
        std::memcpy(parameters.data()+i*8,prototype.data()+0x53E98,8);
        if(i+1==count) length=end;
    }
    put(indices,count*4,u32(indices,0));
    put(curve,0,count);put(curve,4,0x00040100U);
    Data ai_indices((count+1)*4),ai_distances(count*8),ai_attributes(count*32,std::byte{0xFF}),ai_curve(32);
    const auto ai_base=static_cast<unsigned>(positions.size()/16);positions.resize(positions.size()+count*16);
    for(unsigned i=0;i<count;++i) {
        const auto frame=frames+i*80;const auto left=real(original,frame+28),right=real(original,frame+44);
        const auto disabled=(u32(original,frame+76)&0x4000U)!=0;
        float selected=left;std::array<float,3> position{};
        // B2 respawns on an AI lane edge. Use the outermost authored traffic
        // centreline inside B1's road, keeping cars clear of barriers/medians.
        for(unsigned path=0;path<paths;++path) {
            const auto descriptor=0x1B4+path*16,type=u32(original,descriptor);
            if(type!=1 && type!=2) continue;
            const auto first=u32(original,descriptor+8),n=u32(original,descriptor+4);
            for(unsigned j=0;j+1<n;++j) {
                const auto a=points+(first+j)*16,b=a+16;float before=0,after=0;
                for(unsigned axis=0;axis<3;++axis) {
                    const auto forward=real(original,frame+16+axis*4),center=real(original,frame+axis*4);
                    before+=(real(original,a+axis*4)-center)*forward;
                    after+=(real(original,b+axis*4)-center)*forward;
                }
                if(before*after>0 || std::abs(after-before)<0.001f) continue;
                const auto t=before/(before-after);std::array<float,3> point{};float lateral=0;
                for(unsigned axis=0;axis<3;++axis) {
                    point[axis]=std::lerp(real(original,a+axis*4),real(original,b+axis*4),t);
                    lateral+=(real(original,frame+axis*4)-point[axis])*real(original,frame+32+axis*4);
                }
                if(std::abs(point[1]-real(original,frame+4))>2 || lateral<=selected || lateral>=right) continue;
                selected=lateral;position=point;
            }
        }
        require(disabled || selected>left,std::format("no authored respawn lane at road point {}",i));
        if(selected==left) {
            selected=right;
            for(unsigned axis=0;axis<3;++axis) position[axis]=real(positions,i*32+16+axis*4);
        }
        for(unsigned axis=0;axis<3;++axis) put(positions,(ai_base+i)*16+axis*4,position[axis]);
        put(ai_indices,i*4,(i*2)|((ai_base+i)<<16));
        put(ai_distances,i*8,real(distances,i*8));put(ai_distances,i*8+4,selected-left);
        put(ai_attributes,i*32,0U);put(ai_attributes,i*32+4,disabled?0U:2U);
        put(ai_attributes,i*32+24,0xFFFF0000U|i);
    }
    put(ai_indices,count*4,u32(ai_indices,0));put(ai_curve,0,count);put(ai_curve,4,0x40101U);
    const auto traffic=traffic_data(original,points,frames,positions);
    put(result,0x104,static_cast<unsigned>(positions.size()/16));
    put(result,0x110,count);put(result,0x114,1U);
    put(result,0x118,static_cast<unsigned>(traffic.attributes.size()/48));
    put(result,0x11C,static_cast<unsigned>(traffic.curves.size()/32));
    for(const auto& bytes:{positions,indices,distances,parameters,curve}) result.insert(result.end(),bytes.begin(),bytes.end());
    for(const auto& bytes:{ai_indices,ai_distances,ai_attributes,ai_curve}) result.insert(result.end(),bytes.begin(),bytes.end());
    for(const auto* bytes:{&traffic.indices,&traffic.distances,&traffic.attributes,&traffic.curves}) result.insert(result.end(),bytes->begin(),bytes->end());
    // B1 stores checkpoint planes and per-lap times together in its 920-byte
    // triggers. B2 separates them into 68-byte planes and 272-byte events.
    const auto triggers=points+vertices*40;
    require(u32(original,0x48)==32 && triggers+32*920<=original.size(),"invalid Interstate triggers");
    unsigned prototype_events=0x160;constexpr unsigned strides[]={16,20,36,44,36,60,36};
    for(unsigned i=0;i<std::size(strides);++i) prototype_events+=u32(prototype,0x104+i*4)*strides[i];
    const auto prototype_gates=prototype_events+u32(prototype,0x120)*272;
    const auto prototype_event=u32(prototype,prototype_gates+0x24);
    require(u32(prototype,0x124) && prototype_event<u32(prototype,0x120) &&
        u32(prototype,prototype_events+prototype_event*272+12)==0,"unexpected checkpoint event template");
    Data gates,events;
    for(unsigned i=0;i<32;++i) {
        const auto trigger=triggers+i*920;if(u32(original,trigger)!=0) continue;
        const auto gate=static_cast<unsigned>(gates.size());gates.resize(gate+68);
        std::memcpy(gates.data()+gate,original.data()+trigger+0x10,12);
        std::memcpy(gates.data()+gate+0x10,original.data()+trigger+0x1C,16);
        put(gates,gate+0x20,8U);
        for(unsigned lap=0;lap<8;++lap) {
            const auto bonus=u32(original,trigger+0x2C+lap*4),event=static_cast<unsigned>(events.size());
            require(bonus<=600000,"invalid original checkpoint time");put(gates,gate+0x24+lap*4,event/272);
            events.insert(events.end(),prototype.begin()+prototype_events+prototype_event*272,
                prototype.begin()+prototype_events+(prototype_event+1)*272);put(events,event,bonus);
        }
    }
    require(gates.size()==6*68,"Interstate must contain its start/finish and five checkpoints");
    put(result,0x120,static_cast<unsigned>(events.size()/272));put(result,0x124,6U);
    result.insert(result.end(),events.begin(),events.end());result.insert(result.end(),gates.begin(),gates.end());
    put(result,0x134,static_cast<unsigned>(cameras.size()/44));
    result.insert(result.end(),cameras.begin(),cameras.end());
    const auto config=static_cast<unsigned>(result.size());
    result.insert(result.end(),prototype.begin()+setup,prototype.begin()+setup+0x3E0);
    // Do not retain pointers to the template's race triggers.
    put(result,config+0x2CC,0U);std::fill(result.begin()+config+0x2D0,result.begin()+config+0x3E0,std::byte{});
    put(result,0x13C,static_cast<unsigned>(traffic.blocks.size()/32));put(result,0x140,32U);
    put(result,0x144,static_cast<unsigned>(traffic.spawns.size()/8));put(result,0x148,8U);
    put(result,0x14C,static_cast<unsigned>(traffic.transitions.size()/8));put(result,0x150,8U);put(result,0x158,16U);
    for(const auto* bytes:{&traffic.blocks,&traffic.spawns,&traffic.transitions}) result.insert(result.end(),bytes->begin(),bytes->end());
    put(result,0x138,static_cast<unsigned>(result.size()));
    require(configuration(result)==config,"converted race arrays are inconsistent");return result;
}
}

void validate_interstate(const std::filesystem::path& directory) {
    File manifest_file(directory/"import.json");require(manifest_file.size()<4096,"invalid import manifest size");
    const auto bytes=manifest_file.read(0,static_cast<unsigned>(manifest_file.size()));
    const std::string manifest(reinterpret_cast<const char*>(bytes.data()),bytes.size());
    require(manifest.contains("\"format\":\"b2-interstate-import-v5\""),"unsupported import version; import Interstate again");
    for(const auto name:{"interstate.pss","interstate.psw","interstate.bgd","interstate.bcd"}) {
        const auto key='"'+std::string(name)+"_sha256\":\"";const auto at=manifest.find(key);
        require(at!=std::string::npos && at+key.size()+64<manifest.size(),"missing imported asset hash");
        const auto expected=manifest.substr(at+key.size(),64);File file(directory/name);
        require(file.size()<=16*1024*1024 && hash(file,0,file.size())==expected,"imported asset is incomplete or changed; import Interstate again");
    }
}

std::string import_interstate(const std::filesystem::path& burnout,const std::filesystem::path& burnout2,
                             const std::filesystem::path& destination) {
    require(!std::filesystem::exists(destination),"destination already exists");
    Disc source(burnout),target(burnout2);
    require(source.sha256(source.find("default.xbe"))=="42BD7D38FEB39C17783197C4433435BFC06D8F5872D6F4B45048D9DAAC82013B",
        "the Burnout disc does not match the supported US Xbox release");
    require(target.sha256(target.find("default.xbe"))=="B5207C40C11D7C5B5E129EF555F7632FA3584A4240BD0421C2B1B9AD9BFED7BF",
        "the Burnout 2 disc does not match the supported release");
    const auto world=source.find("tracks/usa2/usa2.xbw"),hit=source.find("tracks/usa2/usa2.hit");
    const auto header=read(source,world,0,72);const auto count=u32(header,0),source_names=u32(header,24);auto names=source_names;
    require(count==59 && names==74 && u32(header,20)==count*300+u32(header,12)*88,"unsupported Interstate world layout");
    const auto metadata=read(source,world,72+names*16,u32(header,4));
    const auto materials=read(source,world,72,names*16);
    auto dictionary=textures(source,source.find("tracks/usa2/usa2.dic"));
    std::vector<unsigned> material_textures(names);
    std::vector<std::uint8_t> alpha_reference(names);
    for(unsigned i=0;i<names;++i) {
        const auto name=lower(text(materials,i*16,16));
        const auto found=std::ranges::find_if(dictionary,[&](const auto& texture){return lower(texture.name)==name;});
        require(found!=dictionary.end(),"a world material has no source texture: "+name);
        material_textures[i]=static_cast<unsigned>(found-dictionary.begin());
    }
    std::vector<Section> sections(count);std::map<unsigned,Prop> props;
    unsigned vertex_bytes=0,index_count=0,mesh_count=0,instances=0;
    const auto add_mesh=[&](Section& section,unsigned mesh) {
        require(mesh+40<=metadata.size(),"mesh exceeds source metadata");
        const auto material=u32(metadata,mesh+20),indices=u32(metadata,mesh+12),indices_at=u32(metadata,mesh+16);
        require(material<names && indices>=3 && indices<=32768 && indices_at+indices*2<=metadata.size(),"invalid world mesh");
        unsigned vertices=0;for(unsigned k=0;k<indices;++k) vertices=std::max(vertices,unsigned(u16(metadata,indices_at+k*2))+1);
        const auto extended=u32(metadata,mesh+36);require(extended<=1,"unsupported world vertex layout");
        const auto stride=extended?36U:24U;
        const auto raw=read(source,world,72+names*16+u32(header,4)+u32(metadata,mesh+4),vertices*stride);
        auto& output=section.meshes[material];const auto base=static_cast<unsigned>(output.vertices.size()/24);
        require(base+vertices<=65535,"merged world mesh exceeds 16-bit indices");
        if(!output.indices.empty()) {
            output.indices.push_back(output.indices.back());output.indices.push_back(static_cast<std::uint16_t>(base+u16(metadata,indices_at)));
            if(output.indices.size()&1) output.indices.push_back(output.indices.back());
        }
        for(unsigned k=0;k<indices;++k) output.indices.push_back(static_cast<std::uint16_t>(base+u16(metadata,indices_at+k*2)));
        output.vertices.resize((base+vertices)*24);
        for(unsigned k=0;k<vertices;++k) {
            const auto at=(base+k)*24;const auto* original=raw.data()+k*stride;
            std::memcpy(output.vertices.data()+at+12,original+16,8);
            for(unsigned axis=0;axis<3;++axis) {
                float value=real(raw,k*stride+axis*4);
                require(std::isfinite(value),"non-finite world position");put(output.vertices,at+axis*4,value);
            }
            put(output.vertices,at+20,u32(raw,k*stride+12));
        }
        vertex_bytes+=vertices*24;index_count+=indices;++mesh_count;
    };
    for(unsigned i=0;i<count;++i) {
        const auto offset=i*300;const auto meshes=u32(metadata,offset+0x118)+u32(metadata,offset+0x11C),first=u32(metadata,offset+0x124);
        require(meshes<=80 && first>=u32(header,20) && first+meshes*40<=metadata.size(),"invalid world mesh list");
        auto& section=sections[i];section.bounds=Data(128);
        std::memcpy(section.bounds.data(),metadata.data()+offset+0x98,64);
        std::memcpy(section.bounds.data()+64,metadata.data()+offset+0x58,64);
        put(section.bounds,12,real(metadata,offset+0xDC)-real(metadata,offset+0x9C));
        for(unsigned j=0;j<meshes;++j) {
            const auto mesh=first+j*40,material=u32(metadata,mesh+20);
            require(material<names,"invalid source material");
            // B1 9E2D7/9E3DC and 9DC22: the two world mesh lists use
            // distinct alpha cutoffs, even when a texture has an alpha channel.
            const auto reference=j<u32(metadata,offset+0x118)?240U:160U;
            require(!alpha_reference[material] || alpha_reference[material]==reference,
                "source material appears in incompatible alpha passes");
            alpha_reference[material]=static_cast<std::uint8_t>(reference);
            add_mesh(section,mesh);
        }
        const auto prop_count=u32(metadata,offset+0x120),props_at=u32(metadata,offset+0x128);
        require(prop_count<=64 && props_at+prop_count*88<=metadata.size(),"invalid world instances");
        for(unsigned j=0;j<prop_count;++j) {
            const auto instance=props_at+j*88,mesh=u32(metadata,instance);
            if(!props.contains(mesh)) {
                Section model;add_mesh(model,mesh);require(model.meshes.size()==1,"invalid shared world mesh");
                auto& entry=*model.meshes.begin();props.emplace(mesh,Prop{entry.first,std::move(entry.second)});
            }
            section.instances[mesh].push_back(instance);++instances;
        }
        for(auto& [material,mesh]:section.meshes) {
            std::map<std::array<std::uint32_t,6>,std::uint16_t> unique;Data compact;
            for(auto& index:mesh.indices) {
                std::array<std::uint32_t,6> vertex;std::memcpy(vertex.data(),mesh.vertices.data()+index*24,24);
                const auto [entry,inserted]=unique.try_emplace(vertex,static_cast<std::uint16_t>(unique.size()));
                if(inserted) compact.insert(compact.end(),mesh.vertices.begin()+index*24,mesh.vertices.begin()+index*24+24);
                index=entry->second;
            }
            mesh.vertices=std::move(compact);
        }
    }
    require(instances==u32(header,12),"world instance count differs from its header");
    require(mesh_count==u32(header,16),"world mesh count differs from its header");
    const auto wall=barriers(source);const auto wall_material=names++;
    alpha_reference.push_back(4);
    material_textures.push_back(static_cast<unsigned>(dictionary.size()));
    for(unsigned frame=1;frame<=5;++frame) {
        auto wall_textures=textures(source,source.find("misc/texture.dic"),std::format("d_arrws{}",frame));
        require(wall_textures.size()==1,"missing original barrier animation frame");
        dictionary.push_back(std::move(wall_textures.front()));
    }
    auto sky=environment(source,"sky.dff",true),horizon=environment(source,"skys.dff",false);
    dictionary.push_back({"interstate_white",4,4,1,12,0x500,0x11102,false,
        {std::byte{0xFF},std::byte{0xFF},std::byte{},std::byte{},std::byte{},std::byte{},std::byte{},std::byte{}}});
    for(unsigned i=0;i<wall.indices.size();i+=3) {
        std::array<float,3> center{};
        for(unsigned corner=0;corner<3;++corner) for(unsigned axis=0;axis<3;++axis)
            center[axis]+=real(wall.vertices,wall.indices[i+corner]*24+axis*4)/3;
        unsigned containing=UINT32_MAX;
        for(unsigned s=0;s<count && containing==UINT32_MAX;++s) {
            bool inside=true;
            for(unsigned plane=0;plane<4;++plane) {
                const auto at=64+plane*16;float distance=-real(sections[s].bounds,at+12);
                for(unsigned axis=0;axis<3;++axis) distance+=real(sections[s].bounds,at+axis*4)*center[axis];
                inside&=distance>=0;
            }
            if(inside) containing=s;
        }
        require(containing!=UINT32_MAX,"barrier lies outside the original world sections");
        auto& mesh=sections[containing].meshes[wall_material];const auto base=static_cast<unsigned>(mesh.vertices.size()/24);
        if(!mesh.indices.empty()) {
            mesh.indices.push_back(mesh.indices.back());mesh.indices.push_back(static_cast<std::uint16_t>(base));
            if(mesh.indices.size()&1) mesh.indices.push_back(mesh.indices.back());
        }
        for(unsigned corner=0;corner<3;++corner) {
            const auto vertex=wall.indices[i+corner]*24;mesh.vertices.insert(mesh.vertices.end(),wall.vertices.begin()+vertex,wall.vertices.begin()+vertex+24);
            mesh.indices.push_back(static_cast<std::uint16_t>(base+corner));
        }
    }
    const auto cameras=camera_data(source,target);
    float length=0;const auto race=race_data(source,target,length,cameras.records);
    unsigned start=UINT32_MAX;
    for(unsigned i=0;i<count;++i) {
        bool inside=true;
        for(unsigned plane=0;plane<4;++plane) {
            const auto at=64+plane*16;float distance=-real(sections[i].bounds,at+12);
            for(unsigned axis=0;axis<3;++axis) distance+=real(sections[i].bounds,at+axis*4)*real(race,0x20+axis*4);
            inside&=distance>=0;
        }
        if(inside) {require(start==UINT32_MAX,"starting grid lies in overlapping stream sections");start=i;}
    }
    require(start!=UINT32_MAX,"starting grid lies outside the world");
    // The original single-player streamer starts with slot 8 at the car and
    // searches only the four sections on either side for road contacts.
    const auto first_section=(start+count-8)%count;
    std::vector<unsigned> collision_offsets(count);unsigned hit_cursor=4;
    require(u32(read(source,hit,0,4),0)==count,"world and collision section counts differ");
    for(unsigned i=0;i<count;++i) {
        collision_offsets[i]=hit_cursor;const auto h=read(source,hit,hit_cursor,48);
        hit_cursor+=48+(u32(h,0)+u32(h,16))*16+u32(h,0)*u32(h,8)+u32(h,16)*u32(h,24)+u32(h,32);
        require(hit_cursor<=hit.size,"collision section exceeds source file");
    }
    require(hit_cursor==hit.size,"unread collision data");
    // Append imported tables in the unused fixed master allocation.
    const auto master_entry=target.find("trackpss/forward/fwyl.pss");
    auto master=read(target,master_entry,0,master_entry.size);
    require(u32(master,0)==0xB2300008 && u32(master,4)==142 && u32(master,16)==146,"unexpected Burnout 2 master template");
    require(std::ranges::all_of(Bytes(master).subspan(0x50000,0x14000),[](auto byte){return byte==std::byte{};}),"master extension overlaps existing data");
    const auto old_materials=u32(master,12)-master_base,old_textures=u32(master,20)-master_base;
    require(u32(master,old_materials+2*52+16)==0x80000000U && u32(master,old_materials+19*52+16)==0x20000000U &&
        u32(master,old_materials+117*52+16)==0x40000U,
        "unexpected textured world or instance material class");
    const auto environment_material=142+names+static_cast<unsigned>(props.size());
    const auto material_count=environment_material+4;
    const auto material_at=0x50000U,texture_at=aligned(material_at+material_count*52),pointers_at=aligned(texture_at+static_cast<unsigned>(dictionary.size())*208);
    const auto streams_at=aligned(pointers_at+(146+static_cast<unsigned>(dictionary.size()))*4);
    const auto props_at=aligned(streams_at+count*16);
    require(props_at+props.size()*120<=imported_alpha_offset && material_count<=512,
        "imported master exceeds its fixed allocation");
    std::fill_n(master.begin()+imported_alpha_offset,material_count,std::byte{4});
    std::memmove(master.data()+material_at,master.data()+old_materials,142*52);
    std::memmove(master.data()+pointers_at,master.data()+old_textures,146*4);
    for(unsigned i=0;i<dictionary.size();++i) {
        const auto& texture=dictionary[i];const auto at=texture_at+i*208;
        std::memcpy(master.data()+at,master.data()+0x410,208);
        const auto original_base=u32(master,0x410);
        for(unsigned field:{0U,0x60U,0x94U}) {
            const auto value=u32(master,at+field);
            if(value>=original_base-0x60 && value<original_base+0x70) put(master,at+field,master_base+at+value-(original_base-0x60));
        }
        std::memset(master.data()+at+0x10,0,64);std::memcpy(master.data()+at+0x10,texture.name.data(),texture.name.size());
        put(master,at+0x50,texture.filter);put(master,at+0x6C,texture.width);put(master,at+0x70,texture.height);
        put(master,at+0x80,(texture.raster<<16)|0x8004);put(master,at+0xA4,0x010100FFU|(texture.format<<8));
        put(master,at+0xA8,texture.mips);const auto data_at=append(master,texture.pixels,128);put(master,at+0xB4,master_base+data_at);
        put(master,pointers_at+(146+i)*4,master_base+at);
    }
    for(unsigned i=0;i<names;++i) {
        const auto at=material_at+(142+i)*52;
        // Keep the source raster's alpha flag: B2's cutout class enables
        // alpha testing, as used by its own trees and bridge grilles.
        const auto flags=dictionary[material_textures[i]].alpha?0x20000000U:0x80000000U;
        put(master,at,2U);put(master,at+4,128.0f);put(master,at+16,flags);
        put(master,at+20,master_base+texture_at+material_textures[i]*208);put(master,at+32,1U);
        master[imported_alpha_offset+142+i]=std::byte(alpha_reference[i]?alpha_reference[i]:240);
    }
    unsigned prop_index=0;
    for(const auto& [source_mesh,prop]:props) {
        const auto material=142+names+prop_index,at=props_at+prop_index++*120;
        const auto material_offset=material_at+material*52;
        std::memcpy(master.data()+material_offset,master.data()+material_at+(142+prop.material)*52,52);
        // Instances supply colour through their transform and use 20-byte
        // vertices, so select their own C39A0 pipeline instead of terrain's.
        put(master,material_offset+16,0x40000U);
        master[imported_alpha_offset+material]=std::byte{240};
        std::memcpy(master.data()+at,master.data()+0x97F0,120);
        Data vertices(prop.mesh.vertices.size()/24*20);
        for(unsigned vertex=0;vertex<vertices.size()/20;++vertex)
            std::memcpy(vertices.data()+vertex*20,prop.mesh.vertices.data()+vertex*24,20);
        put(master,at,1U);put(master,at+4,master_base+append(master,vertices,128));put(master,at+8,0U);
        const auto bytes=Bytes(reinterpret_cast<const std::byte*>(prop.mesh.indices.data()),prop.mesh.indices.size()*2);
        put(master,at+12,master_base+append(master,bytes));put(master,at+16,static_cast<unsigned>(prop.mesh.indices.size()));
        put(master,at+20,material);
    }
    put(master,4,material_count);put(master,12,master_base+material_at);
    put(master,16,146+static_cast<unsigned>(dictionary.size()));put(master,20,master_base+pointers_at);
    put(master,0x18,static_cast<unsigned>(props.size()));put(master,0x1C,master_base+props_at);
    for(unsigned field:{0x20U,0x28U,0x3CU,0x44U,0x4CU,0x54U}) put(master,field,0U);
    unsigned environment_index=0;
    const auto add_environment=[&](const EnvironmentMesh& model,bool dome) {
        const auto material=environment_material+environment_index++,at=material_at+material*52;
        const auto texture=std::ranges::find_if(dictionary,[&](const auto& candidate){return lower(candidate.name)==model.texture;});
        require(texture!=dictionary.end(),"environment material has no source texture: "+model.texture);
        put(master,at,2U);put(master,at+4,128.0f);
        put(master,at+16,(texture->alpha?0x20000000U:0x80000000U)|(dome?0x20000U:0U));
        put(master,at+20,master_base+texture_at+static_cast<unsigned>(texture-dictionary.begin())*208);put(master,at+32,1U);
        master[imported_alpha_offset+material]=std::byte{240};
        Data record(24);put(record,0,1U);put(record,4,master_base+append(master,model.mesh.vertices,128));
        put(record,12,master_base+append(master,Bytes(reinterpret_cast<const std::byte*>(model.mesh.indices.data()),model.mesh.indices.size()*2)));
        put(record,16,static_cast<unsigned>(model.mesh.indices.size()));put(record,20,material);return record;
    };
    const auto sky_record=add_environment(sky.front(),true);put(master,0x38,master_base+append(master,sky_record));
    Data horizon_records;
    for(const auto& model:horizon) {const auto record=add_environment(model,false);horizon_records.insert(horizon_records.end(),record.begin(),record.end());}
    put(master,0x3C,3U);put(master,0x40,master_base+append(master,horizon_records));
    for(unsigned i=0;i<4;++i) {master[0x68+i*2]=std::byte{};master[0x69+i*2]=std::byte{};}
    put(master,0x5C,count);put(master,0x60,master_base+streams_at);put(master,0x64,master_base+streams_at+count*8);
    for(unsigned i=0;i<count;++i) for(unsigned mode=0;mode<2;++mode) {
        put(master,streams_at+mode*count*8+i*8,i*sector_bytes);put(master,streams_at+mode*count*8+i*8+4,sector_bytes);
    }
    require(master.size()<=0x6A4000,"imported textures exceed the original fixed texture allocation");
    const auto target_directory=std::filesystem::absolute(destination).lexically_normal();
    require(target_directory.has_filename(),"destination must name a track directory");
    const auto staging=target_directory.parent_path()/utf8_path(path_text(target_directory.filename())+".partial");
    require(!std::filesystem::exists(staging),"an unfinished import already exists beside the destination");
    std::filesystem::create_directories(staging.parent_path());
    require(std::filesystem::create_directory(staging),"cannot create import directory");PendingImport pending{staging};
    save(staging/"interstate.pss",master);
    std::ofstream stream(staging/"interstate.psw",std::ios::binary);
    for(unsigned i=0;i<count;++i) {
        const auto source_section=(first_section+i)%count;const auto& section=sections[source_section];
        Data sector(0x920);std::memcpy(sector.data(),section.bounds.data(),128);
        put(sector,0xAC,1.0f);put(sector,0xB4,1U);put(sector,0xB8,i);put(sector,0xC0,sector_bytes);
        auto cursor=collision_offsets[source_section];collision(source,hit,cursor,sector);
        const auto groups=append(sector,Data(props.size()*12));put(sector,0x8F0,groups);prop_index=0;
        for(const auto& [source_mesh,prop]:props) {
            const auto found=section.instances.find(source_mesh);const auto group=groups+prop_index++*12;
            if(found==section.instances.end()) continue;
            const auto& list=found->second;Data matrices(list.size()*64),bounds(list.size()*60);
            for(unsigned instance=0;instance<list.size();++instance) {
                const auto source_at=list[instance],matrix=instance*64,box=instance*60;
                std::memcpy(matrices.data()+matrix,metadata.data()+source_at+4,64);
                const auto tint=u32(metadata,source_at+68);
                put(matrices,matrix+0x1C,(tint>>16)&255);put(matrices,matrix+0x2C,(tint>>8)&255);put(matrices,matrix+0x3C,tint&255);
                std::memcpy(bounds.data()+box,metadata.data()+source_at+72,12);
                for(unsigned axis=0;axis<3;++axis) {
                    put(bounds,box+12+axis*16,1.0f);put(bounds,box+48+axis*4,real(metadata,source_at+84));
                }
            }
            put(sector,group,static_cast<unsigned>(list.size()));put(sector,group+4,append(sector,matrices));put(sector,group+8,append(sector,bounds));
        }
        std::array<std::array<unsigned,80>,17> selected{};for(auto& values:selected) values.fill(255);
        for(unsigned neighbor=0;neighbor<17;++neighbor) {
            const auto relative=int(neighbor)-8,nearby=(int(source_section)+int(count)+relative)%int(count);unsigned ordinal=0;
            for(const auto& [material,mesh]:sections[nearby].meshes) selected[neighbor][material]=ordinal++;
            const auto at=0x218+neighbor*0x54;put(sector,at,static_cast<unsigned>(relative));
            for(unsigned material=0;material<names;++material) sector[at+4+material]=std::byte(selected[neighbor][material]);
        }
        put(sector,0x204,17U);sector[0x214]=std::byte{0};sector[0x215]=std::byte(names);
        std::fill(sector.begin()+0x208,sector.begin()+0x214,std::byte{0xFF});
        for(unsigned mesh=0;mesh<3;++mesh) sector[0x208+mesh]=std::byte(mesh);
        put(sector,0x7AC,names);for(unsigned material=0;material<names;++material) put(sector,0x7B0+material*4,142+material);
        put(sector,0xBC,static_cast<unsigned>(section.meshes.size()));unsigned ordinal=0;
        for(const auto& [material,mesh]:section.meshes) {
            Data record(24);put(record,0,1U);put(record,4,append(sector,mesh.vertices));
            const auto bytes=Bytes(reinterpret_cast<const std::byte*>(mesh.indices.data()),mesh.indices.size()*2);
            put(record,12,append(sector,bytes));put(record,16,static_cast<unsigned>(mesh.indices.size()));put(record,20,142+material);
            put(sector,0xC4+ordinal++*4,append(sector,record));
        }
        require(sector.size()<=sector_bytes,std::format("world section {} needs {} bytes; streaming slots hold {}",source_section,sector.size(),sector_bytes));sector.resize(sector_bytes);
        stream.write(reinterpret_cast<const char*>(sector.data()),sector.size());require(bool(stream),"cannot write world stream");
    }
    stream.close();require(bool(stream),"cannot finish world stream");save(staging/"interstate.bgd",race);
    save(staging/"interstate.bcd",cameras.opening);
    std::array<std::string,4> hashes;unsigned file_index=0;
    for(const auto name:{"interstate.pss","interstate.psw","interstate.bgd","interstate.bcd"}) {File file(staging/name);hashes[file_index++]=hash(file,0,file.size());}
    const auto report=std::format("{{\"format\":\"b2-interstate-import-v5\",\"sections\":{},\"source_meshes\":{},\"source_instances\":{},\"source_indices\":{},\"source_vertex_bytes\":{},\"textures\":{},\"master_bytes\":{},\"world_bytes\":{},\"race_bytes\":{},\"camera_bytes\":{},\"lap_metres\":{},\"first_source_section\":{},\"checkpoints\":6,\"barrier_triangles\":712,\"barrier_frames\":5,\"music\":7,\"traffic\":true,\"traffic_lanes\":32,\"respawn_points\":378,\"source_cameras\":47,\"sky_triangles\":528,\"horizon_triangles\":481,\"interstate.pss_sha256\":\"{}\",\"interstate.psw_sha256\":\"{}\",\"interstate.bgd_sha256\":\"{}\",\"interstate.bcd_sha256\":\"{}\"}}",
        count,mesh_count,instances,index_count,vertex_bytes,dictionary.size(),master.size(),count*sector_bytes,race.size(),cameras.opening.size(),length,first_section,hashes[0],hashes[1],hashes[2],hashes[3]);
    write_text(staging/"import.json",report+'\n',false);validate_interstate(staging);
    std::filesystem::rename(staging,target_directory);pending.published=true;return report;
}
}
