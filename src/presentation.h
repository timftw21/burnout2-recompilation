#pragma once
#include <array>
#include <cstdint>
#include <iosfwd>

namespace b2 {
struct Cpu;
class Memory;

// Original simulation poses are retained separately from the temporary poses
// used by the title's own drawing code. No image history or GPU pass is needed.
class Presentation {
public:
    void reset();
    void frame(Cpu&,std::uint32_t address,bool destroy=false);
    void camera(Cpu&,std::uint32_t wrapper,unsigned controller=0);
    void camera_cut(Cpu&,std::uint32_t wrapper);
    void begin(Cpu&,Memory&);
    void end(Cpu&,Memory&);
    void traffic(Cpu&,Memory&,std::uint32_t manager);
    void ghost(Cpu&,Memory&,std::uint32_t matrix);
    void shadow_source(Memory&,std::uint32_t manager,std::uint32_t body);
    void shadows(Cpu&,Memory&,std::uint32_t manager);
    void restore_shadows(Memory&);
    void preview(Cpu&,Memory&,std::uint32_t parameters);
    void ui(Cpu&,Memory&,std::uint32_t node,std::uint32_t parameters);
    bool matrix(Cpu&,Memory&,std::uint32_t address,std::uint32_t identity=0,std::array<float,16>* output=nullptr,const std::array<float,16>* source=nullptr);
    std::uint32_t blur_alpha(double frames,std::uint64_t step,std::uint32_t alpha,std::uint32_t filter);
    unsigned active_poses() const;
    void capture_ui(std::ostream&,const Memory&,std::uint64_t step) const;
private:
    using Matrix=std::array<float,16>;
    struct Pose {
        std::uint32_t address=0,identity=0;
        std::uint64_t step=0;
        Matrix previous{},current{};
        bool paired=false,applied=false;
    };
    struct Frame {std::uint32_t address=0;std::uint64_t touched=0;bool applied=false;};
    struct Preview {
        std::uint32_t address=0,model=0;std::uint64_t step=0;
        std::array<float,6> previous{},current{};
        bool paired=false,applied=false;
    };
    struct Blur {std::uint64_t clear_step=UINT64_MAX;double error=0;};
    struct Ui {
        std::uint32_t node=0;
        std::array<std::uint32_t,7> identity{};
        std::uint64_t step=0;
        std::array<float,9> previous{},current{};
        bool paired=false;
    };
    struct Camera {
        std::uint32_t wrapper=0,address=0,frame=0,mode=0,projection=0;
        std::uint64_t touched=0,step=0;
        std::array<float,3> previous{},current{};
        unsigned controller=0;
        float view_scale=0;
        bool paired=false,applied=false,cut=false;
    };
    struct Shadow {std::uint32_t address=0;std::array<std::uint32_t,12> original{};};
    struct ShadowSource {std::uint32_t record=0,address=0,body=0;Matrix pose{};};
    struct EffectClock {std::uint64_t step=0;float previous=0,current=0;bool paired=false,applied=false;};
    std::array<Pose,4096> poses_{};
    std::array<std::uint16_t,4096> applied_{};
    std::array<Frame,1024> frames_{};
    std::array<Preview,16> previews_{};
    std::array<Camera,16> cameras_{};
    std::array<Blur,2> blur_{};
    std::array<Ui,1024> ui_{};
    std::array<Shadow,128> shadows_{};
    std::array<ShadowSource,128> shadow_sources_{};
    EffectClock sparks_{};
    std::array<std::uint32_t,3> ghost_original_{};
    std::uint32_t ghost_position_=0;
    unsigned shadow_count_=0;
    unsigned applied_count_=0;
    std::uint64_t render_cycles_=0;
    bool clock_started_=false;
    bool drawing_=false,syncing_=false;
    float ui_fraction_=0;
    void car(Cpu&,Memory&,std::uint32_t address);
    void cameras(Cpu&,Memory&);
    void forget(std::uint32_t matrix);
};
}
