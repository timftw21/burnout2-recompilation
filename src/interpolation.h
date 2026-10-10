#pragma once
#include <array>
#include <cstdint>
#include <d3d11.h>
#include <wrl/client.h>

namespace b2 {
// GPU image interpolation; no game state, simulation or CPU instruction decoding.
class FrameInterpolation {
public:
    FrameInterpolation(ID3D11Device*,ID3D11DeviceContext*,unsigned width,unsigned height,std::uint64_t& compilations);
    void publish(ID3D11Texture2D*,bool reset);
    void bind(float phase);
    bool ready() const {return frames_>=2;}
    bool published() const {return frames_!=0;}
    ID3D11ShaderResourceView* previous() const {return frame_[0].image_view.Get();}
    ID3D11ShaderResourceView* current() const {return frame_[1].image_view.Get();}
private:
    template<class T> using Ptr=Microsoft::WRL::ComPtr<T>;
    struct Frame {Ptr<ID3D11Texture2D> image,luma;Ptr<ID3D11ShaderResourceView> image_view,luma_view;Ptr<ID3D11RenderTargetView> luma_target;};
    struct Flow {Ptr<ID3D11ShaderResourceView> view;Ptr<ID3D11UnorderedAccessView> output;};
    ID3D11DeviceContext* context_;
    std::array<Frame,2> frame_;
    std::array<std::array<Flow,4>,2> flow_;
    Flow cut_;
    Ptr<ID3D11VertexShader> vertex_;
    Ptr<ID3D11PixelShader> luma_,interpolate_;
    Ptr<ID3D11ComputeShader> estimate_,detect_cut_;
    Ptr<ID3D11Buffer> constants_;
    Ptr<ID3D11SamplerState> sampler_;
    Ptr<ID3D11RasterizerState> raster_;
    Ptr<ID3D11DepthStencilState> depth_;
    unsigned frames_=0;
    void constants(float phase,unsigned level=0,bool first=false);
    void unbind();
};
}
