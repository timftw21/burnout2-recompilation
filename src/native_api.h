#pragma once
#include <array>
#include <cstdint>
#include <string_view>

namespace b2 {
struct NativeApi {std::uint32_t address;std::string_view name,prefix;};
// Independently checked original entry bytes and caller ABIs for the supported XBE.
inline constexpr std::array native_apis{
    NativeApi{0x000E1DB7,"XMountUtilityDrive","558BEC81EC14010000"},
    NativeApi{0x0021A400,"D3DDevice_Initialize","81EC680100006A04"},
    NativeApi{0x002169A0,"D3DDevice_SetTile","8B15B856220083EC18"},
    NativeApi{0x0021AE80,"D3D_KickPushBuffer","51894C24008B4108"},
    NativeApi{0x0021B1C0,"D3D_ReservePushBuffer","83EC08568B35B8562200"},
    NativeApi{0x00216870,"D3DDevice_IsBusy","A1B85622008B801C050000"},
    NativeApi{0x0021B080,"D3D_WaitFence","568B35B85622008B4630"},
    NativeApi{0x0028D27D,"XInitDevices","E9BAF2FFFF"},
    NativeApi{0x0028D282,"XGetDevices","56FF15503D2900"},
    NativeApi{0x0028D2A4,"XGetDeviceChanges","558BEC568B7508"},
    NativeApi{0x0028CF40,"XInputOpen","558BEC518B4D08"},
    NativeApi{0x0028CF96,"XInputClose","8B4C2404E8AD1E0000"},
    NativeApi{0x0028CFA2,"XInputGetCapabilities","558BEC83EC48"},
    NativeApi{0x0028D180,"XInputGetState","535633DBFF15503D2900"},
    NativeApi{0x0028D1EC,"XInputSetState","8B4C24048D81A3000000"},
    NativeApi{0x0022FC18,"DirectSoundCreate","558BEC5156"},
    NativeApi{0x0022D9CA,"DirectSoundDoWork","56E8C5E6FFFF0FB6F0A1C0AC2400"},
    NativeApi{0x0022C131,"DirectSoundUseFullHrtf","56E85EFFFFFF0FB6F0E80E460000"},
    NativeApi{0x0022D6E6,"DirectSoundDownloadEffectsImage","558BECFF75188B4508"},
    NativeApi{0x0022D70D,"DirectSoundGetEffectData","558BECFF75188B4508"},
    NativeApi{0x0022D734,"DirectSoundSetEffectData","558BECFF751C8B4508"},
    NativeApi{0x0022D75E,"DirectSoundCommitEffectData","8B4424048BC883C0F8F7D9"},
    NativeApi{0x0022D792,"DirectSoundSetMixBinHeadroom","8B442404FF74240C8BC8FF74240C"},
    NativeApi{0x0022F956,"DirectSoundSetDistanceFactor","FF74240CD944240C8B44240851"},
    NativeApi{0x0022FA1D,"DirectSoundSetPosition","558BECFF7518D94514"},
    NativeApi{0x0022F97A,"DirectSoundSetOrientation","558BECFF7524D94520"},
    NativeApi{0x0022F3F5,"DirectSoundCommitDeferredSettings","8B4424048BC883C0F8F7D9"},
    NativeApi{0x0022F8EA,"DirectSoundBufferCreate","FF7424108B442408FF7424108BC8"},
    NativeApi{0x0022C11B,"DirectSoundBufferRelease","8B4424048D48E4F7D81BC0"},
    NativeApi{0x0022EF4B,"DirectSoundBufferSetData","8B442404FF74240C8BC8FF74240C"},
    NativeApi{0x0022EF2F,"DirectSoundBufferSetFormat","8B442404FF7424088BC883C0E4"},
    NativeApi{0x0022D7D2,"DirectSoundBufferSetVolume","8B442404FF7424088BC883C0E4"},
    NativeApi{0x0022D85E,"DirectSoundBufferSetHeadroom","8B442404FF7424088BC883C0E4"},
    NativeApi{0x0022D87A,"DirectSoundBufferSetMixBinVolumes","8B442404FF7424088BC883C0E4"},
    NativeApi{0x0022E738,"DirectSoundBufferSetFrequency","8B442404FF7424088BC883C0E4"},
    NativeApi{0x0022E754,"DirectSoundBufferSetOutputBuffer","8B442404FF7424088BC883C0E4"},
    NativeApi{0x0022E770,"DirectSoundBufferSetMixBins","8B442404FF7424088BC883C0E4"},
    NativeApi{0x0022E78C,"DirectSoundBufferSetAllParameters","8B442404FF74240C8BC8FF74240C"},
    NativeApi{0x0022E8C4,"DirectSoundBufferSetRolloffCurve","FF7424108B442408FF7424108BC8"},
    NativeApi{0x0022E8E8,"DirectSoundBufferSetI3DL2Source","8B442404FF74240C8BC8FF74240C"},
    NativeApi{0x0022D8F6,"DirectSoundBufferSetLoopRegion","8B442404FF74240C8BC8FF74240C"},
    NativeApi{0x0022E908,"DirectSoundBufferSetPlayRegion","8B442404FF74240C8BC8FF74240C"},
    NativeApi{0x0022D896,"DirectSoundBufferPlay","FF7424108B442408FF7424108BC8"},
    NativeApi{0x0022D8BA,"DirectSoundBufferStop","8B4424048BC883C0E4F7D9"},
    NativeApi{0x0022D8D2,"DirectSoundBufferStopEx","FF7424108B442408FF7424108BC8"},
    NativeApi{0x0022D916,"DirectSoundBufferGetStatus","8B442404FF7424088BC883C0E4"},
    NativeApi{0x0022D932,"DirectSoundBufferGetPosition","8B442404FF74240C8BC8FF74240C"},
    NativeApi{0x0022D952,"DirectSoundBufferSetPosition","8B442404FF7424088BC883C0E4"}
};
}
