#include "presentation.h"
#include "cpu.h"
#include "file.h"
#include <algorithm>
#include <cmath>
#include <ostream>

namespace b2 {
namespace {
using Matrix=std::array<float,16>;
using Quaternion=std::array<float,4>;

bool rotation(const Matrix& matrix,Quaternion& q,std::array<float,3>& scale) {
    float r[3][3];
    for(unsigned i=0;i<3;++i) {
        float length=0;
        for(unsigned j=0;j<3;++j) length+=matrix[i*4+j]*matrix[i*4+j];
        if(!(length>1e-12f) || !std::isfinite(length)) return false;
        scale[i]=std::sqrt(length);
        for(unsigned j=0;j<3;++j) r[i][j]=matrix[i*4+j]/scale[i];
    }
    const float determinant=r[0][0]*(r[1][1]*r[2][2]-r[1][2]*r[2][1])-
        r[0][1]*(r[1][0]*r[2][2]-r[1][2]*r[2][0])+r[0][2]*(r[1][0]*r[2][1]-r[1][1]*r[2][0]);
    // A pose must be rigid (possibly scaled/reflected), not a projection or
    // shear matrix. Preserve unsupported transforms exactly.
    if(std::abs(std::abs(determinant)-1.f)>0.002f) return false;
    if(determinant<0) {scale[2]=-scale[2];for(auto& v:r[2]) v=-v;}
    const float trace=r[0][0]+r[1][1]+r[2][2];
    if(trace>0) {
        const float s=2*std::sqrt(1+trace);
        q={(r[2][1]-r[1][2])/s,(r[0][2]-r[2][0])/s,(r[1][0]-r[0][1])/s,s*0.25f};
    } else {
        unsigned i=0;if(r[1][1]>r[i][i]) i=1;if(r[2][2]>r[i][i]) i=2;
        const unsigned j=(i+1)%3,k=(i+2)%3;
        const float s=2*std::sqrt(1+r[i][i]-r[j][j]-r[k][k]);
        q[i]=s*0.25f;q[j]=(r[i][j]+r[j][i])/s;q[k]=(r[i][k]+r[k][i])/s;q[3]=(r[k][j]-r[j][k])/s;
    }
    return true;
}
bool interpolate(const Matrix& a,const Matrix& b,float fraction,Matrix& output) {
    if(a==b) return false;
    Quaternion p,q;std::array<float,3> sa,sb;
    if(!rotation(a,p,sa) || !rotation(b,q,sb) || (sa[2]<0)!=(sb[2]<0)) return false;
    float distance=0;
    for(unsigned i=12;i<15;++i) {const auto d=b[i]-a[i];distance+=d*d;}
    // Camera cuts and teleports must not sweep across the track.
    if(!std::isfinite(distance) || distance>100.f*100.f) return false;
    float dot=0;for(unsigned i=0;i<4;++i) dot+=p[i]*q[i];
    float norm=0;
    for(unsigned i=0;i<4;++i) {q[i]=p[i]*(1-fraction)+q[i]*(dot<0?-fraction:fraction);norm+=q[i]*q[i];}
    norm=1/std::sqrt(norm);for(auto& v:q) v*=norm;
    const auto [x,y,z,w]=q;
    const float r[3][3]={{1-2*(y*y+z*z),2*(x*y-z*w),2*(x*z+y*w)},
        {2*(x*y+z*w),1-2*(x*x+z*z),2*(y*z-x*w)},
        {2*(x*z-y*w),2*(y*z+x*w),1-2*(x*x+y*y)}};
    output=b;
    for(unsigned i=0;i<3;++i) {
        const auto scale=sa[i]+(sb[i]-sa[i])*fraction;
        for(unsigned j=0;j<3;++j) output[i*4+j]=r[i][j]*scale;
        output[12+i]=a[12+i]+(b[12+i]-a[12+i])*fraction;
    }
    // RwMatrix flags occupy padding, not a fourth basis component. A changed
    // pose is no longer the cached identity, even if its endpoint was one.
    output[3]=std::bit_cast<float>(std::bit_cast<std::uint32_t>(b[3])&~0x20000U);
    return true;
}
void original(Cpu& cpu,Memory& memory,std::uint32_t entry,std::uint32_t argument=0,std::uint32_t second=0) {
    const auto saved=cpu;
    const auto stack=cpu.registers[esp]-0x1000;
    memory.store(stack,entry);memory.store(stack+4,argument);
    memory.store(stack+8,second);
    cpu.registers[esp]=stack;
    invoke_native(cpu,memory,entry);
    const auto quantum=cpu.quantum_remaining;
    cpu=saved;cpu.quantum_remaining=quantum;
}
}
void Presentation::reset() {
    for(auto& pose:poses_) pose.address=0;
    for(auto& frame:frames_) frame.address=0;
    for(auto& preview:previews_) preview.address=0;
    for(auto& camera:cameras_) camera={};
    for(auto& item:ui_) item.node=0;
    for(auto& source:shadow_sources_) source.record=0;
    applied_count_=0;drawing_=syncing_=clock_started_=false;
    blur_={};sparks_={};ghost_position_=0;shadow_count_=0;
}
void Presentation::forget(std::uint32_t address) {
    for(auto& pose:poses_) if(pose.address==address) {pose.identity=UINT32_MAX;pose.step=0;pose.applied=false;}
}
void Presentation::frame(Cpu& cpu,std::uint32_t address,bool destroy) {
    if(!address || syncing_) return;
    auto* timing=cpu.game_timing;
    if(!timing || !timing->started) return;
    if(destroy) {
        for(auto& frame:frames_) if(frame.address==address) frame={};
        forget(address+0x10);
        for(auto& camera:cameras_) if(camera.frame==address) camera={};
        return;
    }
    if(drawing_ || !timing->update || timing->loading) return;
    Frame* available=nullptr;
    for(auto& frame:frames_) {
        if(frame.address==address) {frame.touched=timing->step;return;}
        if(!available && (!frame.address || frame.touched+2<timing->step)) available=&frame;
    }
    if(!available) throw NativeBoundary(0xE90D0,"presentation frame capacity exceeded");
    *available={address,timing->step};
}
void Presentation::camera(Cpu& cpu,std::uint32_t wrapper,unsigned controller) {
    const auto& timing=*cpu.game_timing;
    if(!wrapper || drawing_ || !timing.started || !timing.update || timing.loading) return;
    auto found=std::ranges::find(cameras_,wrapper,&Camera::wrapper);
    if(found==cameras_.end()) {
        found=std::ranges::find_if(cameras_,[&](const auto& value){return !value.wrapper || value.touched+2<timing.step;});
        if(found==cameras_.end()) throw NativeBoundary(cpu.eip,"presentation camera capacity exceeded");
        *found={};found->wrapper=wrapper;
    }
    found->cut|=found->controller!=controller;found->controller=controller;
    found->touched=timing.step;
}
void Presentation::camera_cut(Cpu& cpu,std::uint32_t wrapper) {
    if(drawing_ || !cpu.game_timing->update) return;
    for(auto& camera:cameras_) if(camera.wrapper==wrapper) camera.cut=true;
}
void Presentation::cameras(Cpu& cpu,Memory& memory) {
    const auto& timing=*cpu.game_timing;
    for(auto& camera:cameras_) if(camera.wrapper && camera.touched+1>=timing.step) {
        // Replay uses the controller at wrapper+98. Its visibility scratch
        // array overlaps the driving camera's first mode record at +170.
        // Read the active controller's mode/shot, not that changing scratch.
        const auto address=memory.load<std::uint32_t>(camera.wrapper),slot=memory.load<std::uint32_t>(camera.wrapper+0x228);
        if(!address) continue;
        if(slot>2) throw NativeBoundary(0xADCA0,"unsupported camera mode stack");
        const auto frame=memory.load<std::uint32_t>(address+4);
        auto mode=camera.controller==1?
            memory.load<std::uint32_t>(camera.wrapper+0xF0)|(memory.load<std::uint32_t>(camera.wrapper+0x11C)<<8):
            memory.load<std::uint32_t>(camera.wrapper+0x170+slot*0xBC);
        if(camera.controller==2) {
            // B58C0 owns the trackside replay/demo cameras, including B5100's
            // zoom. Adjacent records can describe the same continuous shot.
            const auto record=memory.load<std::uint32_t>(camera.wrapper+0x4A4);
            mode=record?memory.load<std::uint32_t>(record+0x10):memory.load<std::uint32_t>(camera.wrapper+0x4A0);
        }
        const auto projection=memory.load<std::uint32_t>(address+0x14);
        const bool cut=camera.cut || address!=camera.address || frame!=camera.frame || mode!=camera.mode || projection!=camera.projection || camera.step+1<timing.step;
        camera.cut=false;
        if(cut) {
            forget(frame+0x10);camera.paired=false;
            // Projected HUD labels must not slide across an intentional cut.
            for(auto& item:ui_) item.node=0;
        }
        std::array<float,3> current{};
        unsigned i=0;for(const auto offset:{0x68U,0x6CU,0x80U}) current[i++]=std::bit_cast<float>(memory.load<std::uint32_t>(address+offset));
        if(!cut && camera.step!=timing.step) {camera.previous=camera.current;camera.paired=true;}
        camera.address=address;camera.frame=frame;camera.mode=mode;camera.projection=projection;camera.step=timing.step;camera.current=current;
        if(!timing.render || !camera.paired || current==camera.previous) continue;
        const auto fraction=float(double(timing.remainder)/733333333.0);
        for(i=0;i<3;++i) current[i]=camera.previous[i]+(current[i]-camera.previous[i])*fraction;
        // View width, height and near clipping change together in A04A0.
        // Rebuild projection through the original setter, at the same fraction
        // as the pose, then restore all three before the next simulation step.
        for(i=0;i<2;++i) {
            memory.store<std::uint32_t>(address+0x68+i*4,std::bit_cast<std::uint32_t>(current[i]));
            memory.store<std::uint32_t>(address+0x70+i*4,std::bit_cast<std::uint32_t>(1.f/current[i]));
        }
        camera.view_scale=std::bit_cast<float>(memory.load<std::uint32_t>(camera.wrapper+0x210));
        memory.store(camera.wrapper+0x210,std::bit_cast<std::uint32_t>(1.f/current[0]));
        syncing_=true;original(cpu,memory,0xE8490,address,std::bit_cast<std::uint32_t>(current[2]));syncing_=false;
        camera.applied=true;
    }
}
bool Presentation::matrix(Cpu& cpu,Memory& memory,std::uint32_t address,std::uint32_t identity,Matrix* output,const Matrix* source) {
    if(!drawing_ || !address) return false;
    const auto& timing=*cpu.game_timing;
    auto index=((address>>4)*2654435761U)&(poses_.size()-1);
    Pose* found=nullptr;unsigned available=static_cast<unsigned>(poses_.size());
    for(unsigned probe=0;probe<poses_.size();++probe,index=(index+1)&(poses_.size()-1)) {
        auto& candidate=poses_[index];
        // A copied shadow can still reference an old owner after a prop slot
        // is reused. Keep both identities until that snapshot stops drawing.
        if(candidate.address==address && candidate.identity==identity) {found=&candidate;break;}
        if(!candidate.applied && (!candidate.address || candidate.step+2<timing.step) && available==poses_.size()) available=static_cast<unsigned>(index);
        if(!candidate.address) break;
    }
    if(!found && available<poses_.size()) {index=available;found=&poses_[index];}
    if(!found) throw NativeBoundary(cpu.eip,"presentation pose capacity exceeded");
    auto& pose=*found;
    if(!pose.applied) {
        Matrix current;
        if(source) current=*source;
        else std::memcpy(current.data(),memory.access(address,64),64);
        if(pose.address!=address || pose.identity!=identity || pose.step+1<timing.step) {
            pose={};pose.address=address;pose.identity=identity;pose.current=current;pose.step=timing.step;
        } else if(pose.step!=timing.step) {
            pose.previous=pose.current;pose.current=current;pose.step=timing.step;pose.paired=true;
        }
    }
    if(!timing.render || !pose.paired) return false;
    Matrix blended;
    if(!interpolate(pose.previous,pose.current,float(double(timing.remainder)/733333333.0),blended)) return false;
    if(output) {*output=blended;return true;}
    // Ghost playback rebuilds its draw record for each view. Reapply the same
    // pose even if this address has already been drawn, without sampling twice.
    std::memcpy(memory.access(address,64),blended.data(),64);
    if(!pose.applied) {pose.applied=true;applied_[applied_count_++]=static_cast<std::uint16_t>(index);}
    return true;
}
void Presentation::car(Cpu& cpu,Memory& memory,std::uint32_t car) {
    if(!car) return;
    const auto model=memory.load<std::uint32_t>(car+0xCEC),body=memory.load<std::uint32_t>(car+8);
    if(!model || !body) return;
    matrix(cpu,memory,model+0xD00,car);
    matrix(cpu,memory,memory.load<std::uint32_t>(body+0x10),car);
    const auto parts=memory.load<std::uint32_t>(model+0x2C),wheels=memory.load<std::uint32_t>(model+0x30);
    if(parts>8 || wheels>4) throw NativeBoundary(0x85AC0,"unsupported vehicle pose layout");
    for(unsigned i=0;i<parts;++i) matrix(cpu,memory,model+0xEC0+i*64,car);
    for(unsigned i=0;i<wheels;++i) matrix(cpu,memory,memory.load<std::uint32_t>(body+0xBB8+i*4),car);
}
void Presentation::begin(Cpu& cpu,Memory& memory) {
    auto& timing=*cpu.game_timing;
    timing.loading_started=false;drawing_=true;
    ui_fraction_=float(double(timing.remainder)/733333333.0);
    if(timing.render) {
        const auto rate=memory.load<std::uint32_t>(0x352720)?50U:60U;
        timing.render_frames=clock_started_?double(timing.last_cycles-render_cycles_)*rate/733333333.0:1;
        // Draw-owned animation consumes elapsed game time, including the title's
        // slow-motion scale, without changing the fixed physics/replay delta.
        timing.render_delta=float(timing.render_frames*std::bit_cast<float>(memory.load<std::uint32_t>(0x598188)));
        // The original clock advances after an update's draw. Extra displays
        // therefore see one more clock tick than that draw did.
        timing.render_offset=(ui_fraction_-(timing.update?0.f:1.f))*
            std::bit_cast<float>(memory.load<std::uint32_t>(0x598188));
        timing.scenery_animated=false;
        render_cycles_=timing.last_cycles;clock_started_=true;
    }
    // Each original update/draw boundary is visited even during catch-up, so
    // histories retain consecutive simulation poses rather than display poses.
    // Paused updates still advance the menu/scheduler, but do not dirty scene
    // frames. Keep their stationary histories alongside the car's history so
    // the camera and car resume at the same presentation fraction.
    const auto scene=memory.load<std::uint32_t>(0x34AB5C);
    if(scene && memory.load<std::uint32_t>(scene+8)) {
        for(auto& camera:cameras_) if(camera.wrapper && camera.touched+1>=timing.step) camera.touched=timing.step;
        for(auto& frame:frames_) if(frame.address && frame.touched+1>=timing.step) frame.touched=timing.step;
    }
    cameras(cpu,memory);
    for(auto& frame:frames_) if(frame.address && frame.touched+1>=timing.step) {
        frame.applied=matrix(cpu,memory,frame.address+0x10,frame.address);
        if(frame.applied) {
            syncing_=true;original(cpu,memory,0xE90D0,frame.address);syncing_=false;
        }
    }
    if(!timing.loading) for(unsigned i=0;i<4;++i) car(cpu,memory,memory.load<std::uint32_t>(0x2FE370+i*4));
    if(!timing.loading) {
        // BAF50 caches the race clock for fxspark. BB0D0 evaluates both ends
        // of each spark from that time, its birth time and original velocity.
        // Sample the clock alongside the poses; paused/slow-motion updates
        // then need no special prediction or extra particle simulation.
        constexpr auto address=0x3DB098U;
        const auto current=std::bit_cast<float>(memory.load<std::uint32_t>(address));
        if(!sparks_.step || sparks_.step+1<timing.step || current<sparks_.current) {
            sparks_={};sparks_.current=current;sparks_.step=timing.step;
        } else if(sparks_.step!=timing.step) {
            sparks_.previous=sparks_.current;sparks_.current=current;sparks_.step=timing.step;sparks_.paired=true;
        }
        if(timing.render && sparks_.paired) {
            memory.store(address,std::bit_cast<std::uint32_t>(sparks_.previous+(sparks_.current-sparks_.previous)*ui_fraction_));
            sparks_.applied=true;
        }
    }
}
void Presentation::end(Cpu& cpu,Memory& memory) {
    if(!drawing_) return;
    restore_shadows(memory);
    if(ghost_position_) {std::memcpy(memory.access(ghost_position_,12),ghost_original_.data(),12);ghost_position_=0;}
    if(sparks_.applied) {memory.store(0x3DB098,std::bit_cast<std::uint32_t>(sparks_.current));sparks_.applied=false;}
    for(unsigned i=0;i<applied_count_;++i) {
        auto& pose=poses_[applied_[i]];
        if(!pose.applied) continue;
        std::memcpy(memory.access(pose.address,64),pose.current.data(),64);pose.applied=false;
    }
    applied_count_=0;
    for(auto& preview:previews_) if(preview.applied) {
        std::memcpy(memory.access(preview.address,24),preview.current.data(),24);preview.applied=false;
    }
    bool synchronized=false;
    if(cpu.game_timing->render) {
        syncing_=true;
        for(auto& camera:cameras_) if(camera.applied) {
            for(unsigned i=0;i<2;++i) {
                memory.store<std::uint32_t>(camera.address+0x68+i*4,std::bit_cast<std::uint32_t>(camera.current[i]));
                memory.store<std::uint32_t>(camera.address+0x70+i*4,std::bit_cast<std::uint32_t>(1.f/camera.current[i]));
            }
            original(cpu,memory,0xE8490,camera.address,std::bit_cast<std::uint32_t>(camera.current[2]));
            memory.store(camera.wrapper+0x210,std::bit_cast<std::uint32_t>(camera.view_scale));
            camera.applied=false;synchronized=true;
        }
        for(auto& frame:frames_) if(frame.applied) {
            original(cpu,memory,0xE90D0,frame.address);frame.applied=false;synchronized=true;
        }
        // Rebuild original camera/frustum and child matrices before physics can
        // observe them. The original scene synchronization has no draw calls.
        if(synchronized) original(cpu,memory,0xF2620);syncing_=false;
    }
    drawing_=false;
}
void Presentation::traffic(Cpu& cpu,Memory& memory,std::uint32_t manager) {
    if(!drawing_) return;
    // 9B380's view lists contain 26 groups of at most 30 vehicles. The
    // vehicle's first 64 bytes are shared by its geometry, wheels and shadow.
    const auto view=memory.load<std::uint32_t>(cpu.registers[esp]+4);
    if(view>=4) throw NativeBoundary(0x9B380,"unsupported traffic view");
    for(unsigned group=view*26;group<(view+1)*26;++group) {
        const auto count=static_cast<std::int32_t>(memory.load<std::uint32_t>(manager+0x2DF7C+group*4));
        if(count<=0) continue;
        if(count>30) throw NativeBoundary(0x9B380,"unsupported traffic draw list");
        for(unsigned i=0;i<static_cast<unsigned>(count);++i) matrix(cpu,memory,memory.load<std::uint32_t>(manager+0x2AEBC+(group*30+i)*4),manager);
    }
}
void Presentation::ghost(Cpu& cpu,Memory& memory,std::uint32_t address) {
    if(!drawing_) return;
    // 7B280 builds a render record, not a matrix: the body starts at +10,
    // wheels at +70 and articulated parts at +1B0. Keep their poses together.
    const auto model=memory.load<std::uint32_t>(0x4BA93C),body=memory.load<std::uint32_t>(0x4B9C58);
    const auto parts=memory.load<std::uint32_t>(model+0x2C),wheels=memory.load<std::uint32_t>(body+0xB88);
    if(parts>8 || wheels>4) throw NativeBoundary(0x7B280,"unsupported ghost pose layout");
    matrix(cpu,memory,address+0x10,model);
    for(unsigned i=0;i<wheels;++i) matrix(cpu,memory,address+0x70+i*64,model);
    for(unsigned i=0;i<parts;++i) matrix(cpu,memory,address+0x1B0+i*64,model);
    // 884B0 projects the separate position at ghost+5E0. 7B280 copied it
    // before smoothing the render record at +5F0, so use its displayed body.
    if(cpu.game_timing->render) {
        if(!ghost_position_) {
            ghost_position_=address-0x10;
            std::memcpy(ghost_original_.data(),memory.access(ghost_position_,12),12);
        }
        std::memcpy(memory.access(address-0x10,12),memory.access(address+0x40,12),12);
    }
}
void Presentation::shadow_source(Memory& memory,std::uint32_t manager,std::uint32_t body) {
    const auto index=memory.load<std::uint32_t>(manager+4);
    if(index>=shadow_sources_.size()) return; // 789F0 also refuses a full buffer.
    auto& source=shadow_sources_[index];
    source.record=manager+8+index*0x30;source.body=body;
    source.address=memory.load<std::uint32_t>(body+0x10);
    std::memcpy(source.pose.data(),memory.access(source.address,64),64);
}
void Presentation::shadows(Cpu& cpu,Memory& memory,std::uint32_t manager) {
    if(!drawing_) return;
    // 826E7 records the source while 789F0 copies each shadow. Later vehicle
    // updates can add/recycle bodies, and drawing can compact matrix slots.
    // Neither the live list nor its matrices still describe this snapshot.
    const auto count=memory.load<std::uint32_t>(manager+4);
    if(!count) return;
    if(count>shadows_.size()) throw NativeBoundary(0x78C10,"physics shadow capacity exceeded");
    for(unsigned i=0;i<count;++i) {
        const auto record=manager+8+i*0x30;
        const auto& source=shadow_sources_[i];
        if(source.record!=record) continue;
        Matrix pose;
        if(!matrix(cpu,memory,source.address,source.body,&pose,&source.pose)) continue;
        auto& saved=shadows_[shadow_count_++];saved.address=record;
        std::memcpy(saved.original.data(),memory.access(record,48),48);
        std::memcpy(memory.access(record,12),pose.data()+12,12);
        // Match 789F0's orientation choice, including overturned bodies.
        const std::array right=std::abs(pose[1])<=0.8f?std::array{pose[0],pose[1],pose[2]}:
            std::array{pose[10],0.f,-pose[8]};
        std::memcpy(memory.access(record+0x10,12),right.data(),12);
    }
}
void Presentation::restore_shadows(Memory& memory) {
    for(unsigned i=0;i<shadow_count_;++i) {
        const auto& saved=shadows_[i];std::memcpy(memory.access(saved.address,48),saved.original.data(),48);
    }
    shadow_count_=0;
}
void Presentation::preview(Cpu& cpu,Memory& memory,std::uint32_t address) {
    if(!drawing_) return;
    const auto step=cpu.game_timing->step;
    const auto asset=memory.load<std::uint32_t>(address+0x10);
    const auto model=asset?memory.load<std::uint32_t>(asset+0x8C):0;
    auto found=std::ranges::find(previews_,address,&Preview::address);
    if(found==previews_.end()) found=std::ranges::find_if(previews_,[&](const auto& value){return !value.address || value.step+2<step;});
    if(found==previews_.end()) throw NativeBoundary(0x2E280,"presentation preview capacity exceeded");
    auto& preview=*found;
    if(preview.applied) return;
    std::array<float,6> current;std::memcpy(current.data(),memory.access(address,24),24);
    if(preview.address!=address || preview.model!=model || preview.step+1<step) {
        preview={};preview.address=address;preview.model=static_cast<std::uint32_t>(model);preview.current=current;preview.step=step;
    } else if(preview.step!=step) {
        preview.previous=preview.current;preview.current=current;preview.step=step;preview.paired=true;
    }
    if(!preview.paired) return;
    const float fraction=float(double(cpu.game_timing->remainder)/733333333.0);
    for(unsigned i:{0U,1U,2U,3U,5U}) {
        auto delta=preview.current[i]-preview.previous[i];
        if(i<2) delta=std::remainder(delta,360.f);
        current[i]=preview.previous[i]+delta*fraction;
    }
    std::memcpy(memory.access(address,24),current.data(),24);preview.applied=true;
}
unsigned Presentation::active_poses() const {
    unsigned count=applied_count_;
    for(const auto& preview:previews_) count+=preview.applied;
    return count;
}
void Presentation::ui(Cpu& cpu,Memory& memory,std::uint32_t node,std::uint32_t parameters) {
    // Frontend page changes hide/reconfigure persistent nodes without freeing
    // them. Their new visibility and layout must not blend with the old page.
    if(!node) {for(auto& item:ui_) item.node=0;return;}
    const auto& timing=*cpu.game_timing;
    if(parameters && (!drawing_ || !timing.render)) return;
    auto index=((node>>3)*2654435761U)&(ui_.size()-1);
    Ui* found=nullptr;Ui* available=nullptr;
    for(unsigned probe=0;probe<ui_.size();++probe,index=(index+1)&(ui_.size()-1)) {
        auto& item=ui_[index];
        if(item.node==node) {found=&item;break;}
        if(!available && (!item.node || item.step+2<timing.step)) available=&item;
        if(!item.node) break;
    }
    // C14A0 can reuse a node immediately. A new item must never inherit the
    // previous occupant's position, colour or animation history.
    if(!parameters) {if(found) {found->step=UINT64_MAX;found->paired=false;}return;}
    if(!found) found=available;
    if(!found) throw NativeBoundary(0xC1306,"presentation UI capacity exceeded");
    const auto ram=memory.primary();
    // Font callbacks keep their measured layout at payload+8 (1E720/229D0).
    // BFBF0 instead reads four UVs and a texture at payload+10. A different
    // image/crop must not inherit the previous trophy's shape while scrolling.
    const auto callback=ram.load<std::uint32_t>(node+0x50);
    const std::array identity{ram.load<std::uint32_t>(node+0x48),callback,
        ram.load<std::uint32_t>(node+0x28),ram.load<std::uint32_t>(node+0x2C),ram.load<std::uint32_t>(node+0x30),
        callback==0xBFBF0?ram.load<std::uint32_t>(node+0x34):0U,
        callback==0xBFBF0?ram.load<std::uint32_t>(node+0x38):0U};
    auto* output=ram.access(parameters,32);
    // 235C6 advances the Burnout text's wipe/blur amount on original updates;
    // 22A60 only reads it. Keep it at the same display fraction as the text.
    auto* blur=identity[1]==0x22A60?ram.access(node+0x40,4):nullptr;
    std::array<float,9> current{};std::memcpy(current.data(),output,32);
    if(blur) std::memcpy(&current[8],blur,4);
    auto& item=*found;
    // Menu panels mirror their signed rectangle when the slide changes sides.
    // Blending opposite orientations collapses the panel through the screen.
    const auto flipped=(std::bit_cast<std::uint32_t>(item.current[2])^std::bit_cast<std::uint32_t>(current[2])) |
        (std::bit_cast<std::uint32_t>(item.current[3])^std::bit_cast<std::uint32_t>(current[3]));
    if(item.node!=node || item.identity!=identity || (flipped&0x80000000U) || (item.step+1!=timing.step && item.step!=timing.step) ||
       (item.step==timing.step && std::memcmp(current.data(),item.current.data(),sizeof(current)))) {
        item={};item.node=node;item.identity=identity;item.current=current;item.step=timing.step;
    } else if(item.step!=timing.step) {
        item.previous=item.current;item.current=current;item.step=timing.step;item.paired=true;
    }
    if(!item.paired || timing.render_frames>=1 || !std::memcmp(item.previous.data(),current.data(),sizeof(current))) return;
    // C1210 supplies only float position, size and colour here. Sample those
    // visual values before the widescreen transform; the original font/image
    // callback draws sharp geometry and its own blur at that position. Text,
    // textures and event state are never interpolated.
    // Keep the live guest x87 stack resident; all throwing reads precede SSE.
    const auto mxcsr=_mm_getcsr();_mm_setcsr(0x1F80);
    const auto fraction=_mm_set1_ps(ui_fraction_);
    for(unsigned i=0;i<8;i+=4) {
        const auto previous=_mm_loadu_ps(item.previous.data()+i),next=_mm_loadu_ps(current.data()+i);
        _mm_storeu_ps(current.data()+i,_mm_add_ps(previous,_mm_mul_ps(_mm_sub_ps(next,previous),fraction)));
    }
    if(blur) {
        const auto previous=_mm_load_ss(&item.previous[8]),next=_mm_load_ss(&current[8]);
        _mm_store_ss(&current[8],_mm_add_ss(previous,_mm_mul_ss(_mm_sub_ss(next,previous),fraction)));
    }
    _mm_setcsr(mxcsr);
    std::memcpy(output,current.data(),32);
    if(blur) std::memcpy(blur,&current[8],4); // C1309 restores this after the callback.
}
void title_ui(Cpu& cpu,Memory& memory,std::uint32_t node,std::uint32_t parameters) {
    if(cpu.presentation && cpu.game_timing) cpu.presentation->ui(cpu,memory,node,parameters);
}
void Presentation::capture_ui(std::ostream& output,const Memory& memory,std::uint64_t step) const {
    output<<'[';bool first=true;
    for(const auto& item:ui_) if(item.node && item.step<=step && item.step+1>=step) {
        if(!first) output<<',';first=false;
        output<<std::format("{{\"node\":{},\"step\":{},\"paired\":{},\"previous\":{},\"current\":{},\"source\":{},\"parent\":{}}}",
            json(hex32(item.node)),item.step,item.paired,
            json(hex_bytes(std::as_bytes(std::span(item.previous)))),json(hex_bytes(std::as_bytes(std::span(item.current)))),
            json(hex_bytes({static_cast<const std::byte*>(memory.access(item.node,0x54)),0x54})),
            json(hex_bytes({static_cast<const std::byte*>(memory.access(item.identity[0],0x40)),0x40})));
    }
    output<<']';
}
std::uint32_t Presentation::blur_alpha(double frames,std::uint64_t step,std::uint32_t alpha,std::uint32_t filter) {
    // A97C0's two original view filters must keep independent histories.
    if(filter!=0x4431F8 && filter!=0x443858) throw NativeBoundary(0xA97FF,"unsupported blur filter");
    auto& blur=blur_[filter==0x443858];
    alpha&=255U;
    // A9750/A9790 clear history for one original frame; A9C65 restores the
    // strength after drawing. Extra displays must not shorten that clear.
    if(!alpha) blur.clear_step=step;
    if(blur.clear_step==step) alpha=0;
    if(!alpha || alpha==255 || frames==1) {blur.error=0;return alpha;}
    // A97C0 blends the previous displayed image. Preserve its original decay
    // per second: several short displays must retain the same total history
    // as one original 50/60 Hz frame. Zero/full strength remain exact.
    // Carry byte rounding forward so very high refresh rates do not change
    // the decay again through repeated rounding of a near-opaque blend.
    const auto value=std::pow(double(alpha)/255,frames)*255+blur.error;
    const auto rounded=static_cast<std::uint32_t>(std::clamp(value+0.5,0.0,255.0));
    blur.error=value-rounded;
    return rounded;
}
std::uint32_t title_blur_alpha(Cpu& cpu,std::uint32_t alpha,std::uint32_t filter) {
    if(!cpu.presentation || !cpu.game_timing || !cpu.game_timing->started) return alpha;
    HostFloatingScope floating(cpu.floating);
    return cpu.presentation->blur_alpha(cpu.game_timing->render_frames,cpu.game_timing->step,alpha,filter);
}
void title_presentation(Cpu& cpu,Memory& memory,std::uint32_t site,std::uint32_t object) {
    if(!cpu.presentation || !cpu.game_timing) return;
    // Frame notifications only record integer addresses; keep the guest x87
    // stack resident on this frequently called path.
    switch(site) {
    case 0:cpu.presentation->reset();return;
    case 0xE90D0:cpu.presentation->frame(cpu,object);return;
    case 0xE9040:case 0xE8F60:cpu.presentation->frame(cpu,object,true);return;
    case 0xADCA0:case 0xA6130:cpu.presentation->camera(cpu,object,site==0xA6130);return;
    case 0xB58C0:cpu.presentation->camera(cpu,object,2);return;
    case 0xA9790:cpu.presentation->camera_cut(cpu,object-8);return;
    case 0x826E7:cpu.presentation->shadow_source(memory,object,memory.load<std::uint32_t>(cpu.registers[ebx]));return;
    case 0x795F5:cpu.presentation->restore_shadows(memory);return;
    }
    HostFloatingScope floating(cpu.floating);
    switch(site) {
    case 1:cpu.presentation->begin(cpu,memory);break;
    case 2:cpu.presentation->end(cpu,memory);break;
    case 0x9B380:cpu.presentation->traffic(cpu,memory,object);break;
    case 0x7BEDE:cpu.presentation->ghost(cpu,memory,object);break;
    case 0x78C10:cpu.presentation->shadows(cpu,memory,object);break;
    case 0x8EBC5: {
        // 8EB10 compacts dead physics props before this point. Bind the
        // remaining matrix slot to its actual body, including slot reuse.
        const auto manager=cpu.registers[ebx];
        const auto body=memory.load<std::uint32_t>(manager+0xD4C+(object-manager-0x14C)/16);
        cpu.presentation->matrix(cpu,memory,object,body);
        break;
    }
    case 0x2E280:cpu.presentation->preview(cpu,memory,object);break;
    }
}
}
