#include "diagnostics.h"
#include "cpu.h"
#include "file.h"
#include "target.h"
#include <Windows.h>
#include <algorithm>
#include <format>
#include <cstdio>
#include <cstdlib>

namespace b2 {
namespace {
HANDLE log_file=INVALID_HANDLE_VALUE;
std::filesystem::path log_path;
DWORD main_thread=0;
const Cpu* guest=nullptr;
LONG handling_fault=0;
struct WatchUpdate {HANDLE thread;void* address;unsigned slot;DWORD error=0;};
DWORD WINAPI update_watch(void* parameter) {
    auto& update=*static_cast<WatchUpdate*>(parameter);
    if(SuspendThread(update.thread)==DWORD(-1)) {update.error=GetLastError();return 0;}
    CONTEXT context{};context.ContextFlags=CONTEXT_DEBUG_REGISTERS;
    if(!GetThreadContext(update.thread,&context)) update.error=GetLastError();
    else if(update.address && (context.Dr7&(3ULL<<(update.slot*2)))) update.error=ERROR_BUSY;
    else {
        auto& address=update.slot==0?context.Dr0:update.slot==1?context.Dr1:update.slot==2?context.Dr2:context.Dr3;
        address=reinterpret_cast<DWORD64>(update.address);
        context.Dr6=0;context.Dr7&=~((3ULL<<(update.slot*2))|(15ULL<<(16+update.slot*4)));
        if(update.address) context.Dr7|=(1ULL<<(update.slot*2))|(1ULL<<(16+update.slot*4)); // One-byte, write-only watch.
        if(!SetThreadContext(update.thread,&context)) update.error=GetLastError();
    }
    if(ResumeThread(update.thread)==DWORD(-1)) update.error=GetLastError();
    return 0;
}
volatile LONG watch_hits=0;
std::array<void*,2> watch_test_addresses{};
LONG CALLBACK check_watch_exception(EXCEPTION_POINTERS* exception) {
    const auto& context=*exception->ContextRecord;
    if(exception->ExceptionRecord->ExceptionCode!=EXCEPTION_SINGLE_STEP ||
        !(context.Dr6&3) || context.Dr0!=reinterpret_cast<DWORD64>(watch_test_addresses[0]) ||
        context.Dr1!=reinterpret_cast<DWORD64>(watch_test_addresses[1])) return EXCEPTION_CONTINUE_SEARCH;
    InterlockedIncrement(&watch_hits);exception->ContextRecord->Dr6=0;return EXCEPTION_CONTINUE_EXECUTION;
}

void append(std::string_view text) noexcept {
    if(log_file==INVALID_HANDLE_VALUE) return;
    DWORD written=0;
    WriteFile(log_file,text.data(),static_cast<DWORD>(text.size()),&written,nullptr);
}
void shutdown() {
    if(log_file==INVALID_HANDLE_VALUE) return;
    append("{\"type\":\"session_end\"}\n");CloseHandle(log_file);log_file=INVALID_HANDLE_VALUE;
}
struct GuestSnapshot {
    bool valid=false;
    std::uint32_t eip=0,flags=0,registers[8]{};
    std::uint32_t history[64]{};
    unsigned history_count=0;
};
GuestSnapshot snapshot() noexcept {
    GuestSnapshot result;
    // Never read a concurrently running guest from an audio/driver thread.
    if(GetCurrentThreadId()!=main_thread || !guest) return result;
    __try {
        result.eip=guest->eip;result.flags=guest->flags;
        for(unsigned i=0;i<8;++i) result.registers[i]=guest->registers[i];
        if(const auto trace=guest->diagnostic) {
            const auto count=std::min<std::uint64_t>(trace->visits,64);
            result.history_count=static_cast<unsigned>(count);
            for(unsigned i=0;i<count;++i) result.history[i]=trace->history[(trace->visits-count+i)%64];
        }
        result.valid=true;
    } __except(EXCEPTION_EXECUTE_HANDLER) {result.valid=false;}
    return result;
}
LONG WINAPI fatal(EXCEPTION_POINTERS* exception) {
    if(InterlockedExchange(&handling_fault,1)) return EXCEPTION_EXECUTE_HANDLER;
    // The file is already open. Keep this path bounded and avoid heap allocation,
    // locks and symbol loading in the process that has just faulted.
    const auto& failure=*exception->ExceptionRecord;
    MEMORY_BASIC_INFORMATION region{};
    const auto queried=VirtualQuery(failure.ExceptionAddress,&region,sizeof(region));
    const auto address=reinterpret_cast<std::uintptr_t>(failure.ExceptionAddress);
    const auto base=queried?reinterpret_cast<std::uintptr_t>(region.AllocationBase):0;
    const auto cpu=snapshot();char text[8192];
    auto length=std::snprintf(text,sizeof(text),
        "{\"type\":\"native_crash\",\"thread\":%lu,\"code\":\"0x%08lX\",\"native_address\":\"0x%016llX\","
        "\"module_base\":\"0x%016llX\",\"module_offset\":\"0x%016llX\",\"guest_available\":%s,"
        "\"guest_eip\":\"0x%08X\",\"flags\":\"0x%08X\",\"registers\":[",
        GetCurrentThreadId(),failure.ExceptionCode,static_cast<unsigned long long>(address),
        static_cast<unsigned long long>(base),static_cast<unsigned long long>(address-base),cpu.valid?"true":"false",cpu.eip,cpu.flags);
    for(unsigned i=0;i<8;++i) length+=std::snprintf(text+length,sizeof(text)-length,"%s\"0x%08X\"",i?",":"",cpu.registers[i]);
    length+=std::snprintf(text+length,sizeof(text)-length,"],\"history\":[");
    for(unsigned i=0;i<cpu.history_count;++i) length+=std::snprintf(text+length,sizeof(text)-length,"%s\"0x%08X\"",i?",":"",cpu.history[i]);
    length+=std::snprintf(text+length,sizeof(text)-length,"],\"exception_parameters\":[");
    for(unsigned i=0;i<std::min<DWORD>(failure.NumberParameters,EXCEPTION_MAXIMUM_PARAMETERS);++i)
        length+=std::snprintf(text+length,sizeof(text)-length,"%s\"0x%016llX\"",i?",":"",static_cast<unsigned long long>(failure.ExceptionInformation[i]));
    const auto& context=*exception->ContextRecord;
    length+=std::snprintf(text+length,sizeof(text)-length,"],\"native_registers\":[");
    const DWORD64 registers[]={context.Rax,context.Rcx,context.Rdx,context.Rbx,context.Rsp,context.Rbp,context.Rsi,context.Rdi,
        context.R8,context.R9,context.R10,context.R11,context.R12,context.R13,context.R14,context.R15};
    for(unsigned i=0;i<std::size(registers);++i)
        length+=std::snprintf(text+length,sizeof(text)-length,"%s\"0x%016llX\"",i?",":"",static_cast<unsigned long long>(registers[i]));
    length+=std::snprintf(text+length,sizeof(text)-length,"],\"debug_status\":\"0x%016llX\",\"watch_addresses\":[",
        static_cast<unsigned long long>(context.Dr6));
    const DWORD64 watches[]={context.Dr0,context.Dr1,context.Dr2,context.Dr3};
    for(unsigned i=0;i<std::size(watches);++i)
        length+=std::snprintf(text+length,sizeof(text)-length,"%s\"0x%016llX\"",i?",":"",static_cast<unsigned long long>(watches[i]));
    length+=std::snprintf(text+length,sizeof(text)-length,"],\"native_stack\":[");
    __try {
        const auto stack=reinterpret_cast<const DWORD64*>(context.Rsp);
        for(unsigned i=0;i<16;++i)
            length+=std::snprintf(text+length,sizeof(text)-length,"%s\"0x%016llX\"",i?",":"",static_cast<unsigned long long>(stack[i]));
    } __except(EXCEPTION_EXECUTE_HANDLER) {}
    length+=std::snprintf(text+length,sizeof(text)-length,"]}\n");
    if(length>0 && length<static_cast<int>(sizeof(text))) append({text,static_cast<std::size_t>(length)});
    if(log_file!=INVALID_HANDLE_VALUE) FlushFileBuffers(log_file);
    return EXCEPTION_EXECUTE_HANDLER;
}
}
void initialize_diagnostics() noexcept {
    if(log_file!=INVALID_HANDLE_VALUE) return;
    try {
        wchar_t module[32768];const auto size=GetModuleFileNameW(nullptr,module,std::size(module));
        if(!size || size==std::size(module)) return;
        const auto directory=std::filesystem::path(std::wstring_view(module,size)).parent_path()/"logs";
        std::filesystem::create_directories(directory);
        SYSTEMTIME time{};GetSystemTime(&time);
        log_path=directory/std::format("b2-{:04}{:02}{:02}-{:02}{:02}{:02}-{}.log",
            time.wYear,time.wMonth,time.wDay,time.wHour,time.wMinute,time.wSecond,GetCurrentProcessId());
        log_file=CreateFileW(log_path.c_str(),FILE_APPEND_DATA,FILE_SHARE_READ,nullptr,CREATE_NEW,FILE_ATTRIBUTE_NORMAL,nullptr);
        if(log_file==INVALID_HANDLE_VALUE) return;
        main_thread=GetCurrentThreadId();SetUnhandledExceptionFilter(fatal);std::atexit(shutdown);
        diagnostic_record(std::format("{{\"type\":\"session_start\",\"pid\":{},\"utc\":\"{:04}-{:02}-{:02}T{:02}:{:02}:{:02}Z\",\"target_sha256\":{},\"command\":{}}}",
            GetCurrentProcessId(),time.wYear,time.wMonth,time.wDay,time.wHour,time.wMinute,time.wSecond,json(target::sha256),json(utf8(GetCommandLineW()))));
    } catch(...) {} // Logging must not replace the original failure.
}
void diagnostic_record(std::string_view record) noexcept {
    if(record.size()>256*1024) {append("{\"type\":\"log_error\",\"message\":\"Record exceeds 256 KiB\"}\n");return;}
    try {std::string line(record);line+='\n';append(line);} catch(...) {}
}
void diagnostic_guest(const Cpu* cpu) noexcept {guest=cpu;}
const std::filesystem::path& diagnostic_log() noexcept {return log_path;}
void diagnostic_watch(unsigned slot,void* address) {
    if(slot>=4) throw std::runtime_error("A memory watch requires one of four hardware slots");
    WatchUpdate update{OpenThread(THREAD_GET_CONTEXT|THREAD_SET_CONTEXT|THREAD_SUSPEND_RESUME,FALSE,GetCurrentThreadId()),address,slot};
    if(!update.thread) throw std::runtime_error("Cannot open the current thread for a memory watch");
    const auto helper=CreateThread(nullptr,0,update_watch,&update,0,nullptr);
    if(!helper) {CloseHandle(update.thread);throw std::runtime_error("Cannot start the memory-watch setup thread");}
    WaitForSingleObject(helper,INFINITE);CloseHandle(helper);CloseHandle(update.thread);
    if(update.error) throw std::runtime_error(std::format("Cannot configure memory watch: Windows error {}",update.error));
}
std::string check_memory_watch(bool crash) {
    volatile std::uint8_t bytes[2]={5,5};watch_hits=0;
    watch_test_addresses={const_cast<std::uint8_t*>(&bytes[0]),const_cast<std::uint8_t*>(&bytes[1])};
    if(crash) {
        SetErrorMode(SEM_FAILCRITICALERRORS|SEM_NOGPFAULTERRORBOX);
        diagnostic_watch(0,watch_test_addresses[0]);bytes[0]=0xBA;
        throw std::runtime_error("Expected memory-watch fault was not raised");
    }
    const auto handler=AddVectoredExceptionHandler(1,check_watch_exception);
    if(!handler) throw std::runtime_error("Cannot install the isolated memory-watch check handler");
    bool first=false,second=false;
    try {
        diagnostic_watch(0,watch_test_addresses[0]);first=true;
        diagnostic_watch(1,watch_test_addresses[1]);second=true;
        bytes[0]=0xBA;bytes[1]=0xFF;
        if(watch_hits!=2 || bytes[0]!=0xBA || bytes[1]!=0xFF) throw std::runtime_error("Memory watches missed an overwrite");
        diagnostic_watch(0,nullptr);first=false;diagnostic_watch(1,nullptr);second=false;
        bytes[0]=5;bytes[1]=5;
        if(watch_hits!=2) throw std::runtime_error("Released memory watches still fired");
    } catch(...) {
        if(first) diagnostic_watch(0,nullptr);if(second) diagnostic_watch(1,nullptr);
        RemoveVectoredExceptionHandler(handler);throw;
    }
    RemoveVectoredExceptionHandler(handler);watch_test_addresses={};
    return "{\"format\":\"b2-memory-watch-check-v1\",\"passed\":true,\"game_booted\":false,\"overwrite_traps\":2,\"released\":true}";
}
}
