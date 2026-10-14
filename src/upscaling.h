#pragma once
#include <cstdint>
#include <memory>
#include <filesystem>
#include <string>

struct ID3D11Device;
struct ID3D11DeviceContext;
struct ID3D11Texture2D;
struct ID3D11ShaderResourceView;

namespace b2 {
enum class UpscaleMethod {off,dlss,fsr,xess};
struct UpscaleFrame {
    float jitter_x=0,jitter_y=0,elapsed_ms=0;
    float camera_near=0,camera_far=0,vertical_fov=0;
    bool reset=false;
};
// The renderer supplies jittered colour, normalized device depth, screen-space
// motion in input pixels and a responsive mask. No synthetic motion is used.
class FrameUpscaling {
public:
    FrameUpscaling(ID3D11Device*,ID3D11DeviceContext*,UpscaleMethod,
                   unsigned input_width,unsigned input_height,unsigned output_width,unsigned output_height);
    ~FrameUpscaling();
    FrameUpscaling(const FrameUpscaling&)=delete;
    FrameUpscaling& operator=(const FrameUpscaling&)=delete;
    void publish(ID3D11Texture2D* colour,ID3D11Texture2D* depth,ID3D11Texture2D* motion,
                 ID3D11Texture2D* responsive,const UpscaleFrame&);
    void prepare_frames(std::uint64_t& shader_compilations);
    void publish_frame(ID3D11Texture2D* colour,ID3D11ShaderResourceView* depth,
                       ID3D11ShaderResourceView* responsive,const UpscaleFrame&);
    ID3D11Texture2D* image() const;
    ID3D11ShaderResourceView* view() const;
private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};
std::string check_upscaling(const std::filesystem::path&);
}
