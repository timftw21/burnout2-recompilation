#pragma once
#include "render.h"
#include "frame_processing.h"
#include <d3d11.h>

namespace b2 {
// Presentation-only GTAO. The game's colour/depth resources remain original.
class FrameAmbientOcclusion {
    template<class T> using Ptr=Microsoft::WRL::ComPtr<T>;
public:
    FrameAmbientOcclusion(ID3D11Device*,ID3D11DeviceContext*,unsigned width,unsigned height,unsigned quality,unsigned samples,std::uint64_t& compilations);
    void capture_depth(ID3D11ShaderResourceView*);
    void publish(ID3D11ShaderResourceView* scene,const FrameLayers::Images*,float near_plane,float far_plane,float tan_x,float tan_y,float strength);
    void invalidate() {ready_=false;}
    bool ready() const {return ready_;}
    ID3D11Texture2D* image() const {return image_[1].Get();}
    ID3D11Texture2D* scene() const {return image_[0].Get();}
    ID3D11ShaderResourceView* scene_view() const {return view_[0].Get();}
    ID3D11ShaderResourceView* view() const {return view_[1].Get();}
    std::size_t bytes() const {return std::size_t(width_)*height_*20;}
    Image readback();
private:
    ID3D11Device* device_;
    ID3D11DeviceContext* context_;
    unsigned width_,height_;
    bool ready_=false;
    Ptr<ID3D11Texture2D> depth_,visibility_;
    Ptr<ID3D11ShaderResourceView> depth_view_,visibility_view_;
    Ptr<ID3D11UnorderedAccessView> depth_output_,visibility_output_;
    std::array<Ptr<ID3D11Texture2D>,2> image_;
    std::array<Ptr<ID3D11ShaderResourceView>,2> view_;
    std::array<Ptr<ID3D11RenderTargetView>,2> target_;
    Ptr<ID3D11ComputeShader> depth_shader_,main_shader_;
    Ptr<ID3D11VertexShader> vertex_;
    Ptr<ID3D11PixelShader> composite_;
    Ptr<ID3D11Buffer> constants_,depth_constants_;
    Ptr<ID3D11RasterizerState> raster_;
    Ptr<ID3D11DepthStencilState> depth_state_;
    void unbind();
};
std::string ambient_occlusion_shader(unsigned quality,bool composite);
}
