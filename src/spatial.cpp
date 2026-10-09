#include "spatial.h"
#include "xbe.h"
#include <algorithm>
#include <chrono>
#include <cmath>
#include <format>
#include <numbers>

namespace b2 {
namespace {
constexpr std::uint32_t base=0x70000000,listener=base+0x100,source=base+0x200,
    output=base+0x300,points=base+0x400,stack=base+0x1FC0,sentinel=0xFFFFFFF0;
float real(std::uint32_t value){return std::bit_cast<float>(value);}
float amplitude(std::int64_t db){return db<=-10000?0:std::pow(10.0f,static_cast<float>(db)/2000);}
}
Spatial::Spatial(Memory& memory):memory_(memory) {
    memory_.map(base,scratch_);
    // The SDK constructor copies this exact environmental preset, rather than
    // the similarly named silent default found in later SDK headers.
    std::memcpy(environment_listener_.data(),memory_.access(0x00237BD8,48),48);
    // Original full-quality mathematical dispatch initialization; no hardware.
    call(0x0023074D,{});
}
void Spatial::call(std::uint32_t entry,std::initializer_list<std::uint32_t> arguments) {
    Cpu cpu;cpu.fs_base=base;cpu.eip=entry;
    cpu.registers[esp]=stack-static_cast<std::uint32_t>(arguments.size()*4);
    memory_.store<std::uint32_t>(cpu.registers[esp],sentinel);
    unsigned index=0;for(auto value:arguments) memory_.store<std::uint32_t>(cpu.registers[esp]+4+index++*4,value);
    invoke_native(cpu,memory_,entry);
    if(cpu.eip!=sentinel || cpu.registers[esp]!=stack+4) throw std::runtime_error("Unexpected original 3D audio calculator ABI");
}
void Spatial::validate(const SpatialParameters& parameters) {
    if(parameters[0]!=76 || parameters[7]>360 || parameters[8]<parameters[7] || parameters[8]>360 || parameters[15]>2)
        throw std::runtime_error("Invalid original 3D audio parameter layout, cone or mode");
    const auto outside=std::bit_cast<std::int32_t>(parameters[12]);
    if(outside>0 || outside<-10000) throw std::runtime_error("Invalid 3D audio cone volume");
    for(unsigned i=1;i<parameters.size();++i) {
        if(i==7 || i==8 || i==12 || i==15) continue;
        if(!std::isfinite(real(parameters[i]))) throw std::runtime_error("Non-finite 3D audio parameter");
    }
    if(real(parameters[13])<=0 || real(parameters[14])<real(parameters[13]) ||
        real(parameters[16])<=0 || real(parameters[17])<0 || real(parameters[18])<0)
        throw std::runtime_error("Invalid 3D audio distance or factor");
}
void Spatial::validate(const EnvironmentParameters& parameters) {
    for(unsigned i:{0U,1U,2U,3U,5U,7U}) {
        const auto db=std::bit_cast<std::int32_t>(parameters[i]);
        if(db<-10000 || db>((i==0 || i==2)?1000:0)) throw std::runtime_error("Invalid environmental audio volume");
    }
    for(unsigned i:{4U,6U,8U}) {
        const auto value=real(parameters[i]);
        if(!std::isfinite(value) || value<0 || value>(i==4?10.0f:1.0f)) throw std::runtime_error("Invalid environmental audio factor");
    }
}
SpatialResult Spatial::calculate(const SpatialListener& value,const SpatialParameters& parameters,std::span<const std::byte> curve,
                                 const EnvironmentParameters* environment,bool mute_at_max) {
    validate(parameters);
    if(environment) validate(*environment);
    if(curve.size()%4 || curve.size()>4096) throw std::runtime_error("Invalid 3D audio distance curve size");
    for(std::size_t offset=0;offset<curve.size();offset+=4) {
        const auto gain=real(u32(curve,offset));
        if(!std::isfinite(gain) || gain<0 || gain>1) throw std::runtime_error("Invalid 3D audio distance curve gain");
    }
    std::fill(scratch_.begin()+0x100,scratch_.begin()+0x338,std::byte{});
    const auto store=[&](std::uint32_t address,float number) {
        if(!std::isfinite(number)) throw std::runtime_error("Non-finite 3D audio listener parameter");
        memory_.store<std::uint32_t>(address,std::bit_cast<std::uint32_t>(number));
    };
    memory_.store<std::uint32_t>(listener,UINT32_MAX);memory_.store<std::uint32_t>(listener+4,UINT32_MAX);
    for(unsigned i=0;i<3;++i) {
        store(listener+8+i*4,value.position[i]);store(listener+0x14+i*4,value.velocity[i]);
        store(listener+0x20+i*4,value.front[i]);store(listener+0x2C+i*4,value.up[i]);
    }
    if(value.distance<=0 || value.rolloff<0 || value.doppler<0) throw std::runtime_error("Invalid 3D audio listener factor");
    store(listener+0x38,value.distance);store(listener+0x3C,value.rolloff);store(listener+0x40,value.doppler);
    memory_.store<std::uint32_t>(source,UINT32_MAX);memory_.store<std::uint32_t>(source+4,UINT32_MAX);
    std::memcpy(memory_.access(source+8,72),parameters.data()+1,72);
    if(!curve.empty()) std::memcpy(memory_.access(points,curve.size()),curve.data(),curve.size());
    memory_.store<std::uint32_t>(source+0x50,curve.empty()?0:points);
    memory_.store<std::uint32_t>(source+0x54,static_cast<std::uint32_t>(curve.size()/4));
    memory_.store<std::uint32_t>(source+0x58,mute_at_max?4U:0U);
    if(environment) {
        memory_.store<std::uint32_t>(listener+0x84,UINT32_MAX);
        std::memcpy(memory_.access(listener+0x88,48),environment_listener_.data(),48);
        memory_.store<std::uint32_t>(source+0x7C,UINT32_MAX);
        std::memcpy(memory_.access(source+0x80,36),environment->data(),36);
    }
    // Standalone math has FS:+58 == 0, so its original guard has no kernel work.
    call(0x0022EC59,{listener,source});
    call(0x0022CEDB,{listener,source,environment?listener+0x84:0,environment?source+0x7C:0,output});
    SpatialResult result;
    for(unsigned i=0;i<2;++i) result.adjustments[i]=std::bit_cast<std::int32_t>(memory_.load<std::uint32_t>(output+4+i*4));
    result.adjustments[2]=std::bit_cast<std::int32_t>(memory_.load<std::uint32_t>(output+0x24));
    const auto volume=std::clamp(std::int64_t(result.adjustments[0])+result.adjustments[1],std::int64_t(-10000),std::int64_t(0));
    result.gain=amplitude(volume);
    result.pitch=std::exp2(result.adjustments[2]/4096.0);
    result.distance=real(memory_.load<std::uint32_t>(source+0x68));
    for(unsigned i=0;i<2;++i) result.angles[i]=real(memory_.load<std::uint32_t>(source+0x74+i*4));
    for(unsigned i=0;i<2;++i) result.front_back[i]=amplitude(std::bit_cast<std::int32_t>(memory_.load<std::uint32_t>(output+0x14+i*4)));
    if(environment) for(unsigned i=0;i<2;++i) {
        result.environment_gain[i]=amplitude(std::bit_cast<std::int32_t>(memory_.load<std::uint32_t>(output+0x1C+i*4)));
        const auto coefficient=memory_.load<std::uint32_t>(output+0x30+i*4);
        if(coefficient>65535) throw std::runtime_error("Invalid original environmental audio filter coefficient");
        result.environment_pole[i]=coefficient/65536.0f;
    }
    if(parameters[15]!=2) {
        const auto pan=std::clamp(std::sin(result.angles[0]*std::numbers::pi_v<float>/180),-1.0f,1.0f);
        // The SDK gives paired 3D bins the same volume (232FED/233139).
        // Stereo placement must retain that level at the listener's centre.
        const auto maximum=1+std::abs(pan);
        result.pan={std::sqrt((1-pan)/maximum),std::sqrt((1+pan)/maximum)};
    }
    if(!std::isfinite(result.gain) || !std::isfinite(result.pitch) || result.pitch<=0 ||
        !std::isfinite(result.distance) || !std::isfinite(result.pan[0]) || !std::isfinite(result.pan[1]))
        throw std::runtime_error("Original 3D audio calculation produced invalid output");
    return result;
}
std::string check_spatial(const std::filesystem::path& executable) {
    Xbe image(executable);if(!image.supported()) throw std::runtime_error("3D audio checks require the verified executable");
    std::vector<std::byte> bytes(image.image_size);image.load(bytes);Memory memory(image.base,bytes);Spatial spatial(memory);
    SpatialParameters parameters{};parameters[0]=76;parameters[7]=parameters[8]=360;
    const auto set=[&](unsigned index,float value){parameters[index]=std::bit_cast<std::uint32_t>(value);};
    set(11,1);set(13,5);set(14,100);set(16,1);set(17,1);set(18,1);
    SpatialListener listener_value;unsigned checks=0;
    const auto require=[&](bool condition){++checks;if(!condition) throw std::runtime_error("Original 3D audio check failed at case "+std::to_string(checks));};
    auto result=spatial.calculate(listener_value,parameters,{});require(result.gain==1 && result.pitch==1 && result.distance==0 && result.pan==std::array<float,2>{1,1});
    set(3,10);result=spatial.calculate(listener_value,parameters,{});require(result.distance==10 && result.gain<1 && result.gain>0);
    const std::array<float,1> unity{1};result=spatial.calculate(listener_value,parameters,std::as_bytes(std::span(unity)));require(result.gain==1);
    const std::array<float,1> descending{0.5f};result=spatial.calculate(listener_value,parameters,std::as_bytes(std::span(descending)));
    require(result.gain<1 && result.gain>0.5f);
    set(3,100);result=spatial.calculate(listener_value,parameters,std::as_bytes(std::span(descending)));require(std::abs(result.gain-0.5f)<0.001f);
    parameters[15]=2;result=spatial.calculate(listener_value,parameters,std::as_bytes(std::span(descending)));require(result.gain==1 && result.pitch==1 && result.pan[0]==1 && result.pan[1]==1);
    parameters[15]=0;set(3,0);set(1,10);result=spatial.calculate(listener_value,parameters,std::as_bytes(std::span(unity)));
    const auto right=result;set(1,-10);const auto left=spatial.calculate(listener_value,parameters,std::as_bytes(std::span(unity)));
    require(right.pan[1]>right.pan[0] && left.pan[0]>left.pan[1]);
    EnvironmentParameters environment{};environment[0]=std::bit_cast<std::uint32_t>(std::int32_t(-2000));
    environment[2]=std::bit_cast<std::uint32_t>(std::int32_t(-1000));
    result=spatial.calculate(listener_value,parameters,std::as_bytes(std::span(unity)),&environment);
    require(std::abs(result.environment_gain[0]-0.1f)<0.000001f && std::abs(result.environment_gain[1]-std::sqrt(0.1f))<0.000001f);
    require(result.environment_pole[0]==0 && result.environment_pole[1]>0 && result.environment_pole[1]<1);
    environment[1]=std::bit_cast<std::uint32_t>(std::int32_t(-6000));
    result=spatial.calculate(listener_value,parameters,std::as_bytes(std::span(unity)),&environment);
    require(result.environment_pole[0]>0 && result.environment_pole[0]<1 && std::abs(result.environment_gain[0]-0.1f)<0.000001f);
    parameters[15]=2;result=spatial.calculate(listener_value,parameters,{},&environment);
    require(result.environment_gain[0]==1 && result.environment_gain[1]==1 && result.environment_pole==std::array<float,2>{});
    parameters[15]=0;set(1,100);result=spatial.calculate(listener_value,parameters,std::as_bytes(std::span(unity)),&environment,true);
    require(result.gain==0);
    set(1,-10);
    const auto started=std::chrono::steady_clock::now();
    for(unsigned i=0;i<1000;++i) result=spatial.calculate(listener_value,parameters,std::as_bytes(std::span(unity)));
    const auto elapsed=std::chrono::duration<double,std::milli>(std::chrono::steady_clock::now()-started).count();
    return std::format("{{\"format\":\"b2-spatial-check-v1\",\"passed\":true,\"game_booted\":false,\"cases\":{},\"calculations\":1000,\"processing_ms\":{},\"right_angles\":[{},{}],\"left_angles\":[{},{}],\"runtime_decoder\":false}}",checks,elapsed,right.angles[0],right.angles[1],left.angles[0],left.angles[1]);
}
}
