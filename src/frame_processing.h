#pragma once
#include <array>
#include <cstdint>
#include <d3d11_1.h>
#include "com_ptr.h"

namespace b2 {
// Feedback shaders control rounding so frame history can retain ordinary RGBA8 storage.
inline constexpr DXGI_FORMAT frame_color_format=DXGI_FORMAT_R8G8B8A8_UNORM;
inline constexpr unsigned frame_color_bytes=4;
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
    FrameLayers(ID3D11Device*,ID3D11DeviceContext*,unsigned width,unsigned height,unsigned samples,std::uint64_t& compilations,bool readable=false);
    void begin(ID3D11Texture2D*,ID3D11ShaderResourceView*,float uniform_alpha=-1);
    void invalidate() {valid_=false;}
    bool valid() const {return valid_;}
    const Images& resolved();
    ID3D11RenderTargetView* target(unsigned i) const {return images_.targets[i].Get();}
    ID3D11Texture2D* scene() const {return images_.textures[0].Get();}
    ID3D11ShaderResourceView* scene_view() const {return images_.views[0].Get();}
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
}
