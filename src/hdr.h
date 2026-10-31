#pragma once
#include "render.h"
#include "frame_processing.h"

namespace b2 {
// Expand the original SDR image only at presentation; guest blending and
// framebuffer history keep their original formats and values.
class FrameHdr {
    template<class T> using Ptr=Microsoft::WRL::ComPtr<T>;
public:
    FrameHdr(ID3D11Device*,ID3D11DeviceContext*,unsigned width,unsigned height,std::uint64_t& compilations);
    void publish(ID3D11ShaderResourceView* image,ID3D11ShaderResourceView* scene,const FrameLayers::Images*,float paper_white,float peak);
    void invalidate() {ready_=false;}
    bool ready() const {return ready_;}
    ID3D11ShaderResourceView* view() const {return view_.Get();}
    std::size_t bytes() const {return std::size_t(width_)*height_*8;}
    HdrImage readback();
private:
    ID3D11Device* device_;
    ID3D11DeviceContext* context_;
    unsigned width_,height_;
    bool ready_=false;
    Ptr<ID3D11Texture2D> image_;
    Ptr<ID3D11ShaderResourceView> view_;
    Ptr<ID3D11RenderTargetView> target_;
    Ptr<ID3D11VertexShader> vertex_;
    Ptr<ID3D11PixelShader> pixel_;
    Ptr<ID3D11Buffer> constants_;
    Ptr<ID3D11RasterizerState> raster_;
    Ptr<ID3D11DepthStencilState> depth_;
};
std::string hdr_color_source();
std::string hdr_shader_source();
HdrImage read_hdr_image(ID3D11Device*,ID3D11DeviceContext*,ID3D11Texture2D*);
Image hdr_sdr_image(const HdrImage&,float paper_white);
}
