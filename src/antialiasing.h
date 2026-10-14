#pragma once
#include "render.h"
#include <d3d11.h>
#include <wrl/client.h>

namespace b2 {
// Spatial AA of completed game images. Original render targets and feedback
// remain available unchanged; only presentation/history use the filtered image.
class FrameAntialiasing {
public:
    FrameAntialiasing(ID3D11Device*,ID3D11DeviceContext*,unsigned width,unsigned height,unsigned quality,std::uint64_t& compilations);
    void publish(ID3D11ShaderResourceView* source);
    void invalidate() {ready_=false;}
    bool ready() const {return ready_;}
    ID3D11Texture2D* image() const {return image_.Get();}
    ID3D11ShaderResourceView* view() const {return view_.Get();}
    std::size_t bytes() const {return std::size_t(width_)*height_*4;}
    Image readback();
private:
    template<class T> using Ptr=Microsoft::WRL::ComPtr<T>;
    ID3D11Device* device_;
    ID3D11DeviceContext* context_;
    unsigned width_,height_;
    bool ready_=false;
    Ptr<ID3D11Texture2D> image_;
    Ptr<ID3D11ShaderResourceView> view_;
    Ptr<ID3D11RenderTargetView> target_;
    Ptr<ID3D11VertexShader> vertex_;
    Ptr<ID3D11PixelShader> pixel_;
    Ptr<ID3D11SamplerState> sampler_;
    Ptr<ID3D11Buffer> constants_;
    Ptr<ID3D11RasterizerState> raster_;
    Ptr<ID3D11DepthStencilState> depth_;
};

// Grow multisample colour/depth/stencil without discarding individual samples.
// D3D11 forbids ordinary partial copies of multisampled resources.
class MultisampleCopy {
public:
    MultisampleCopy(ID3D11Device*,ID3D11DeviceContext*,std::uint64_t& compilations);
    void grow(ID3D11ShaderResourceView* color,ID3D11ShaderResourceView* depth,ID3D11ShaderResourceView* stencil,
              ID3D11RenderTargetView*,ID3D11DepthStencilView*,unsigned width,unsigned height);
private:
    template<class T> using Ptr=Microsoft::WRL::ComPtr<T>;
    ID3D11DeviceContext* context_;
    Ptr<ID3D11VertexShader> vertex_;
    Ptr<ID3D11PixelShader> pixel_,stencil_;
    Ptr<ID3D11RasterizerState> raster_;
    Ptr<ID3D11DepthStencilState> depth_;
    std::array<Ptr<ID3D11DepthStencilState>,8> stencil_states_;
    std::array<Ptr<ID3D11Buffer>,8> stencil_masks_;
};
}
