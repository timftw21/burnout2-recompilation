#include <winsock2.h>
#include <ws2tcpip.h>
#include "host.h"
#include <bcrypt.h>
#include <iphlpapi.h>
#include <array>
#include <algorithm>
#include <memory>
#include <stdexcept>
#include <intrin.h>
#include <format>
#include <climits>
namespace b2 {
std::string cpu_name() {
    std::array<char,49> brand{};int info[4];__cpuid(info,0x80000000);
    if(unsigned(info[0])>=0x80000004) for(unsigned i=0;i<3;++i) {__cpuid(info,0x80000002+i);std::memcpy(brand.data()+i*16,info,16);}
    return brand[0]?brand.data():"Unknown x64 CPU";
}
std::string build_description() {
    const auto* module=reinterpret_cast<const std::byte*>(GetModuleHandleW(nullptr));
    const auto* dos=reinterpret_cast<const IMAGE_DOS_HEADER*>(module);
    const auto* image=reinterpret_cast<const IMAGE_NT_HEADERS64*>(module+dos->e_lfanew);
    return std::format("PE {:08X}, image {} bytes",image->FileHeader.TimeDateStamp,image->OptionalHeader.SizeOfImage);
}
std::filesystem::path executable_path() {
    std::array<wchar_t,32768> path{};const auto size=GetModuleFileNameW(nullptr,path.data(),static_cast<DWORD>(path.size()));
    if(!size || size==path.size()) throw std::runtime_error("Cannot locate executable");return std::wstring_view(path.data(),size);
}
void random_bytes(void* output,std::size_t size) {
    if(size>ULONG_MAX || BCryptGenRandom(nullptr,static_cast<PUCHAR>(output),static_cast<ULONG>(size),BCRYPT_USE_SYSTEM_PREFERRED_RNG)<0)
        throw std::runtime_error("Native random source failed");
}
std::uint32_t ethernet_status() {
    MIB_IF_TABLE2* table=nullptr;
    if(GetIfTable2(&table)!=NO_ERROR) throw std::runtime_error("Native Ethernet status query failed");
    std::unique_ptr<MIB_IF_TABLE2,decltype(&FreeMibTable)> interfaces(table,FreeMibTable);
    for(unsigned i=0;i<table->NumEntries;++i) {
        const auto& link=table->Table[i];
        if(link.Type!=IF_TYPE_ETHERNET_CSMACD || !link.InterfaceAndOperStatusFlags.HardwareInterface ||
           link.OperStatus!=IfOperStatusUp || link.MediaConnectState!=MediaConnectStateConnected) continue;
        const auto speed=std::min(link.TransmitLinkSpeed,link.ReceiveLinkSpeed);
        return 1|(speed>=100000000?2U:speed>=10000000?4U:0U);
    }
    return 0;
}
std::uint32_t dos_error(std::uint32_t status) {
    using Convert=ULONG(WINAPI*)(LONG);
    static const auto convert=reinterpret_cast<Convert>(GetProcAddress(GetModuleHandleW(L"ntdll.dll"),"RtlNtStatusToDosError"));
    if(!convert) throw std::runtime_error("Native NTSTATUS conversion is unavailable");return convert(static_cast<LONG>(status));
}
}
