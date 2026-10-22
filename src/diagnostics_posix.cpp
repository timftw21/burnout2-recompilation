#include "diagnostics.h"
#include "cpu.h"
#include "file.h"
#include "host.h"
#include "target.h"
#include <array>
#include <atomic>
#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <ctime>
#include <format>
#include <fcntl.h>
#ifdef __APPLE__
#include <pthread.h>
#else
#include <sys/syscall.h>
#endif
#include <ucontext.h>
#include <unistd.h>
namespace b2 {
namespace {
int log_file=-1;std::filesystem::path log_path;
thread_local const Cpu* guest=nullptr;
std::atomic_flag handling=ATOMIC_FLAG_INIT;
alignas(16) thread_local std::array<std::byte,64*1024> signal_stack;
void install_signal_stack() noexcept {
    thread_local bool attempted=false;if(attempted) return;attempted=true;
    stack_t stack{};stack.ss_sp=signal_stack.data();stack.ss_size=signal_stack.size();sigaltstack(&stack,nullptr);
}
void append(std::string_view text) noexcept {
    if(log_file<0) return;
    while(!text.empty()) {const auto size=write(log_file,text.data(),text.size());if(size<0) {if(errno==EINTR) continue;break;}text.remove_prefix(size);}
}
void shutdown() {append("{\"type\":\"session_end\"}\n");if(log_file>=0) close(log_file);log_file=-1;}
void fatal(int signal,siginfo_t* info,void* context) {
    if(handling.test_and_set()) _exit(128+signal);
    const auto* cpu=guest;
#ifdef __APPLE__
    std::uint64_t thread=0;pthread_threadid_np(nullptr,&thread);
#if defined(__aarch64__)
    const auto address=static_cast<ucontext_t*>(context)->uc_mcontext->__ss.__pc;
#else
    const auto address=static_cast<ucontext_t*>(context)->uc_mcontext->__ss.__rip;
#endif
#else
    const auto thread=static_cast<std::uint64_t>(syscall(SYS_gettid));
    const auto& registers=static_cast<ucontext_t*>(context)->uc_mcontext.gregs;
    const auto address=registers[REG_RIP];
#endif
    char buffer[1024];const auto size=std::snprintf(buffer,sizeof(buffer),
        "{\"type\":\"native_crash\",\"signal\":%d,\"thread\":%llu,\"fault_address\":\"0x%016llX\",\"native_address\":\"0x%016llX\",\"guest_available\":%s,\"guest_eip\":\"0x%08X\",\"flags\":\"0x%08X\"}\n",
        signal,static_cast<unsigned long long>(thread),static_cast<unsigned long long>(reinterpret_cast<std::uintptr_t>(info->si_addr)),
        static_cast<unsigned long long>(address),cpu?"true":"false",cpu?cpu->eip:0,cpu?cpu->flags:0);
    if(size>0 && std::size_t(size)<sizeof(buffer)) append({buffer,std::size_t(size)});
    // The handler is reset before delivery, preserving the native core dump.
    raise(signal);
}
}
void initialize_diagnostics() noexcept {
    if(log_file>=0) return;
    try {
        const auto directory=executable_path().parent_path()/"logs";std::filesystem::create_directories(directory);
        const auto now=std::time(nullptr);tm utc{};gmtime_r(&now,&utc);
        log_path=directory/std::format("b2-{:04}{:02}{:02}-{:02}{:02}{:02}-{}.log",utc.tm_year+1900,utc.tm_mon+1,utc.tm_mday,utc.tm_hour,utc.tm_min,utc.tm_sec,getpid());
        log_file=open(log_path.c_str(),O_WRONLY|O_APPEND|O_CREAT|O_EXCL|O_CLOEXEC,0600);if(log_file<0) return;
        std::atexit(shutdown);install_signal_stack();
        struct sigaction action{};action.sa_sigaction=fatal;action.sa_flags=SA_SIGINFO|SA_ONSTACK|SA_RESETHAND;sigemptyset(&action.sa_mask);
        for(int signal:{SIGSEGV,SIGBUS,SIGILL,SIGFPE,SIGABRT}) sigaction(signal,&action,nullptr);
#if defined(__APPLE__) && defined(__aarch64__)
        constexpr auto platform="macos-arm64";
#elif defined(__APPLE__)
        constexpr auto platform="macos-x86_64";
#else
        constexpr auto platform="linux-x86_64";
#endif
        diagnostic_record(std::format("{{\"type\":\"session_start\",\"pid\":{},\"platform\":{},\"target_sha256\":{}}}",getpid(),json(platform),json(target::sha256)));
    } catch(...) {}
}
void diagnostic_record(std::string_view text) noexcept {
    if(text.size()>256*1024) {append("{\"type\":\"log_error\",\"message\":\"Record exceeds 256 KiB\"}\n");return;}
    append(text);append("\n");
}
void diagnostic_guest(const Cpu* cpu) noexcept {if(cpu) install_signal_stack();guest=cpu;}
const std::filesystem::path& diagnostic_log() noexcept {return log_path;}
void diagnostic_watch(unsigned,void*) {throw std::runtime_error("Hardware memory watches are Windows-only; use a native debugger.");}
std::string check_memory_watch(bool) {throw std::runtime_error("Hardware memory-watch diagnostics are Windows-only.");}
}
