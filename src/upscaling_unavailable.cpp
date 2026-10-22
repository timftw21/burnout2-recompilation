#include "upscaling.h"
#include <stdexcept>
namespace b2 {
struct FrameUpscaling::Impl {};
namespace {[[noreturn]] void unavailable(){throw std::runtime_error("DLSS, FSR and XeSS reconstruction require the Windows D3D12 interop backend.");}}
FrameUpscaling::FrameUpscaling(ID3D11Device*,ID3D11DeviceContext*,UpscaleMethod,unsigned,unsigned,unsigned,unsigned) {unavailable();}
FrameUpscaling::~FrameUpscaling()=default;
void FrameUpscaling::publish(ID3D11Texture2D*,ID3D11Texture2D*,ID3D11Texture2D*,ID3D11Texture2D*,const UpscaleFrame&) {unavailable();}
void FrameUpscaling::prepare_frames(std::uint64_t&) {unavailable();}
void FrameUpscaling::publish_frame(ID3D11Texture2D*,ID3D11ShaderResourceView*,ID3D11ShaderResourceView*,const UpscaleFrame&) {unavailable();}
ID3D11Texture2D* FrameUpscaling::image() const {unavailable();}
ID3D11ShaderResourceView* FrameUpscaling::view() const {unavailable();}
ID3D11Texture2D* FrameUpscaling::input(unsigned) const {unavailable();}
std::string check_upscaling(const std::filesystem::path&) {unavailable();}
}
