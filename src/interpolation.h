#pragma once
#include <array>
#include <cstdint>
#include <d3d11_1.h>
#include <wrl/client.h>

namespace b2 {
struct MotionCamera {
    std::array<float,16> view{};
    float near_plane=0,far_plane=0;
    std::uint32_t identifier=0,projection=0;
};
// Retain the final screen-space suffix as a clipped affine RGB operation.
// Floating bases track the affine transform; normalized bases track its bounds.
// All bases preserve endpoint alpha for destination-alpha blends.
class FrameLayers {
    template<class T> using Ptr=Microsoft::WRL::ComPtr<T>;
public:
    static constexpr unsigned image_count=5,basis_count=image_count-1;
    struct Images {
        std::array<Ptr<ID3D11Texture2D>,image_count> textures;
        std::array<Ptr<ID3D11ShaderResourceView>,image_count> views;
        std::array<Ptr<ID3D11RenderTargetView>,basis_count> targets;
    };
    static Images history(ID3D11Device*,unsigned width,unsigned height,bool render_targets);
    FrameLayers(ID3D11Device*,ID3D11DeviceContext*,unsigned width,unsigned height,unsigned samples,std::uint64_t& compilations);
    void begin(ID3D11Texture2D*,ID3D11ShaderResourceView*,float uniform_alpha=-1);
    void invalidate() {valid_=false;}
    bool valid() const {return valid_;}
    void publish(Images&);
    ID3D11RenderTargetView* target(unsigned i) const {return images_.targets[i].Get();}
    std::size_t bytes() const {return bytes_;}
private:
    ID3D11DeviceContext* context_;
    Images images_;
    std::array<Ptr<ID3D11Texture2D>,basis_count> multisample_;
    Ptr<ID3D11VertexShader> vertex_;
    Ptr<ID3D11PixelShader> initialize_;
    Ptr<ID3D11RasterizerState> raster_;
    Ptr<ID3D11DepthStencilState> depth_;
    std::size_t bytes_=0;
    bool valid_=false;
};
// GPU image interpolation; no game state, simulation or CPU instruction decoding.
class FrameInterpolation {
public:
    FrameInterpolation(ID3D11Device*,ID3D11DeviceContext*,unsigned width,unsigned height,std::uint64_t& compilations,bool interpolation_support=true,bool separate_layers=false,unsigned samples=1,bool capture_depth=false);
    void publish(ID3D11Texture2D*,bool reset,FrameLayers* layers=nullptr,ID3D11ShaderResourceView* depth=nullptr,const MotionCamera* camera=nullptr);
    void bind(float phase);
    bool ready() const {return frames_>=2;}
    bool published() const {return frames_!=0;}
    std::size_t bytes() const {return bytes_;}
    ID3D11ShaderResourceView* previous() const {return frame_[0].image_view.Get();}
    ID3D11ShaderResourceView* current() const {return frame_[1].image_view.Get();}
    ID3D11ShaderResourceView* backward_field() const {return interpolation_support_?resolved_[1].view.Get():flow_[1][0].view.Get();}
    ID3D11ShaderResourceView* scene_cut() const {return cut_.view.Get();}
    ID3D11ShaderResourceView* scene(bool current) const {return frame_[unsigned(current)].scene();}
    ID3D11ShaderResourceView* depth(bool current) const {const auto& frame=frame_[unsigned(current)];return frame.depth_valid?frame.depth.view.Get():nullptr;}
    const MotionCamera& camera(bool current) const {return frame_[unsigned(current)].camera;}
private:
    template<class T> using Ptr=Microsoft::WRL::ComPtr<T>;
    static constexpr unsigned levels=5;
    struct Flow {Ptr<ID3D11ShaderResourceView> view;Ptr<ID3D11UnorderedAccessView> output;};
    struct Frame {Ptr<ID3D11Texture2D> image,pyramid;Ptr<ID3D11ShaderResourceView> image_view,pyramid_view;Ptr<ID3D11RenderTargetView> pyramid_target;
        std::array<std::array<Flow,2>,levels> gradients;
        FrameLayers::Images layers;
        Flow depth;
        bool layered=false,depth_valid=false;
        MotionCamera camera;
        ID3D11ShaderResourceView* scene() const {return layered?layers.views[0].Get():image_view.Get();}
    };
    ID3D11DeviceContext* context_;
    std::array<Frame,2> frame_;
    std::array<std::array<Flow,levels>,2> flow_;
    std::array<Flow,2> resolved_;
    Flow cut_;
    Ptr<ID3D11VertexShader> vertex_;
    Ptr<ID3D11PixelShader> downsample_,interpolate_;
    Ptr<ID3D11ComputeShader> estimate_,resolve_,detect_cut_,gradient_,depth_;
    Ptr<ID3D11Buffer> constants_;
    std::array<Ptr<ID3D11Buffer>,levels> compute_constants_;
    Ptr<ID3D11SamplerState> sampler_;
    Ptr<ID3D11RasterizerState> raster_;
    Ptr<ID3D11DepthStencilState> depth_state_;
    unsigned width_,height_;
    bool interpolation_support_;
    std::size_t bytes_=0;
    unsigned frames_=0;
    struct Constants {float phase;unsigned level,first,layers,width,height,padding[2];};
    void constants(float phase);
    void compute_constants(unsigned level=0);
    void unbind(bool target=true);
};
}
