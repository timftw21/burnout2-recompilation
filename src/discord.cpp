#include "discord.h"
#include "file.h"
#include <algorithm>
#include <array>
#include <atomic>
#include <charconv>
#include <chrono>
#include <condition_variable>
#include <ctime>
#include <format>
#include <mutex>
#include <thread>
#ifdef _WIN32
#include <Windows.h>
#else
#include <cerrno>
#include <cstdlib>
#include <fcntl.h>
#include <poll.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>
#endif

namespace b2 {
namespace {
using Clock=std::chrono::steady_clock;
constexpr auto retry_delay=std::chrono::seconds(5),update_delay=std::chrono::seconds(15);
constexpr unsigned max_payload=64*1024;
constexpr std::string_view presence_icon="https://raw.githubusercontent.com/timftw21/burnout2-recompilation/7005684a8c85b82dd49c23fdddadd9ce4a867f33/resources/b2.png";
enum class Status {disabled,unconfigured,waiting,connecting,connected,rejected};

// Only top-level RPC string fields are needed; nested/user data is ignored.
std::string_view field(std::string_view text,std::string_view name) {
    unsigned depth=0;
    for(std::size_t i=0;i<text.size();) {
        const auto c=text[i++];
        if(c=='{' || c=='[') ++depth;
        else if(c=='}' || c==']') {if(!depth) return {};--depth;}
        else if(c=='"') {
            const auto first=i;bool escaped=false;
            while(i<text.size() && text[i]!='"') {
                if(text[i++]=='\\') {escaped=true;if(i==text.size()) return {};++i;}
            }
            if(i==text.size()) return {};
            const auto key=text.substr(first,i++-first);
            if(depth!=1 || escaped || key!=name) continue;
            const auto space=[&]{while(i<text.size() && (text[i]==' ' || text[i]=='\r' || text[i]=='\n' || text[i]=='\t')) ++i;};
            space();if(i==text.size() || text[i++]!=':') continue;
            space();if(i==text.size() || text[i++]!='"') return {};
            const auto value=i;
            while(i<text.size() && text[i]!='"') {if(text[i++]=='\\') return {};}
            return i<text.size()?text.substr(value,i-value):std::string_view{};
        }
    }
    return {};
}
struct Connection {
#ifdef _WIN32
    HANDLE pipe=INVALID_HANDLE_VALUE,event=CreateEventW(nullptr,TRUE,FALSE,nullptr);
    bool open() {
        if(!event) return false;
        for(unsigned i=0;i<10;++i) {
            const auto path=std::format(L"\\\\?\\pipe\\discord-ipc-{}",i);
            pipe=CreateFileW(path.c_str(),GENERIC_READ|GENERIC_WRITE,0,nullptr,OPEN_EXISTING,
                FILE_FLAG_OVERLAPPED|SECURITY_SQOS_PRESENT|SECURITY_IDENTIFICATION,nullptr);
            if(pipe!=INVALID_HANDLE_VALUE) return true;
        }
        return false;
    }
    bool valid() const {return pipe!=INVALID_HANDLE_VALUE;}
    void close() {if(valid()) CloseHandle(pipe);pipe=INVALID_HANDLE_VALUE;}
    bool transfer(void* data,unsigned size,unsigned& done,bool writing) {
        OVERLAPPED operation{};operation.hEvent=event;ResetEvent(event);
        const auto complete=writing?WriteFile(pipe,data,size,nullptr,&operation):ReadFile(pipe,data,size,nullptr,&operation);
        if(!complete && GetLastError()!=ERROR_IO_PENDING) return false;
        if(!complete && WaitForSingleObject(event,100)!=WAIT_OBJECT_0) {
            CancelIoEx(pipe,&operation);DWORD cancelled=0;GetOverlappedResult(pipe,&operation,&cancelled,TRUE);return false;
        }
        DWORD count=0;if(!GetOverlappedResult(pipe,&operation,&count,FALSE)) return false;
        done=count;return true;
    }
    bool receive(void* data,unsigned size,unsigned& done) {
        DWORD available=0;done=0;
        if(!PeekNamedPipe(pipe,nullptr,0,nullptr,&available,nullptr)) return false;
        return !available || transfer(data,std::min(size,unsigned(available)),done,false);
    }
    bool send(const void* data,unsigned size) {
        auto* bytes=static_cast<const char*>(data);
        while(size) {unsigned done=0;if(!transfer(const_cast<char*>(bytes),size,done,true) || !done) return false;bytes+=done;size-=done;}
        return true;
    }
    unsigned pid() const {return GetCurrentProcessId();}
    ~Connection() {close();if(event) CloseHandle(event);}
#else
    int socket=-1;
    bool open() {
        std::array<std::string_view,5> prefixes{};unsigned count=0;
        for(const auto* variable:{"XDG_RUNTIME_DIR","TMPDIR","TMP","TEMP"}) if(const auto* value=std::getenv(variable);value && *value)
            prefixes[count++]=value;
        prefixes[count++]="/tmp";
        for(const auto prefix:std::span(prefixes).first(count)) for(unsigned i=0;i<10;++i) {
            sockaddr_un address{};address.sun_family=AF_UNIX;
            const auto path=std::format("{}/discord-ipc-{}",prefix,i);
            if(path.size()>=sizeof(address.sun_path)) continue;
            std::memcpy(address.sun_path,path.c_str(),path.size()+1);
            socket=::socket(AF_UNIX,SOCK_STREAM,0);if(socket<0) continue;
            if(fcntl(socket,F_SETFL,O_NONBLOCK)<0 || fcntl(socket,F_SETFD,FD_CLOEXEC)<0) {close();continue;}
#ifdef __APPLE__
            int no_signal=1;
            if(setsockopt(socket,SOL_SOCKET,SO_NOSIGPIPE,&no_signal,sizeof(no_signal))) {close();continue;}
#endif
            if(!::connect(socket,reinterpret_cast<sockaddr*>(&address),sizeof(address))) return true;
            if(errno==EINPROGRESS) {
                pollfd pending{socket,POLLOUT,0};int error=0;socklen_t length=sizeof(error);
                if(::poll(&pending,1,100)>0 && !getsockopt(socket,SOL_SOCKET,SO_ERROR,&error,&length) && !error) return true;
            }
            close();
        }
        return false;
    }
    bool valid() const {return socket>=0;}
    void close() {if(valid()) ::close(socket);socket=-1;}
    bool receive(void* data,unsigned size,unsigned& done) {
        const auto count=::recv(socket,data,size,0);done=0;
        if(count<0) return errno==EAGAIN || errno==EWOULDBLOCK || errno==EINTR;
        if(!count) return false;
        done=static_cast<unsigned>(count);return true;
    }
    bool send(const void* data,unsigned size) {
        auto* bytes=static_cast<const char*>(data);const auto deadline=Clock::now()+std::chrono::milliseconds(100);
        while(size) {
#ifdef __APPLE__
            const auto done=::send(socket,bytes,size,0);
#else
            const auto done=::send(socket,bytes,size,MSG_NOSIGNAL);
#endif
            if(done>0) {bytes+=done;size-=static_cast<unsigned>(done);continue;}
            if(done==0 || (errno!=EINTR && errno!=EAGAIN && errno!=EWOULDBLOCK) || Clock::now()>=deadline) return false;
            pollfd pending{socket,POLLOUT,0};if(::poll(&pending,1,10)<0 && errno!=EINTR) return false;
        }
        return true;
    }
    unsigned pid() const {return static_cast<unsigned>(getpid());}
    ~Connection() {close();}
#endif
    bool frame(unsigned opcode,std::string_view payload) {
        if(payload.size()>max_payload) return false;
        const std::array<std::uint32_t,2> header{opcode,static_cast<std::uint32_t>(payload.size())};
        return send(header.data(),sizeof(header)) && send(payload.data(),static_cast<unsigned>(payload.size()));
    }
};
}
struct DiscordPresence::Impl {
    std::string application;
    bool configured=false;
    std::atomic<unsigned> desired{0};
    std::atomic<bool> stopping{false};
    std::atomic<Status> status{Status::disabled};
    std::mutex mutex;std::condition_variable changed;
    std::thread worker;
    explicit Impl(std::string_view id):application(id) {
        std::uint64_t number=0;const auto parsed=std::from_chars(id.data(),id.data()+id.size(),number);
        configured=number && parsed.ec==std::errc{} && parsed.ptr==id.data()+id.size();
    }
    ~Impl() {
        if(worker.joinable()) {
            {std::lock_guard lock(mutex);stopping=true;}
            changed.notify_one();worker.join();
        }
    }
    void run() {
        Connection connection;
        std::array<char,max_payload+8> input{};unsigned received=0,sent=0,observed=0;
        std::uint64_t sequence=0;std::string pending;
        auto next_connect=Clock::time_point{},next_update=Clock::time_point{},handshake_deadline=Clock::time_point{};
        bool ready=false;
        auto started=std::time(nullptr);
        const auto activity=[&](unsigned value) {
            const auto body=value?std::format("{{\"type\":0,\"details\":{},\"assets\":{{\"large_image\":{},\"large_text\":\"Burnout 2 - Point of Impact\"}},\"timestamps\":{{\"start\":{}}}}}",
                json(value&4?"Adjusting settings":value&2?"In game":"Ready to play"),json(presence_icon),started):"null";
            pending=std::to_string(++sequence);
            return connection.frame(1,std::format("{{\"cmd\":\"SET_ACTIVITY\",\"args\":{{\"pid\":{},\"activity\":{}}},\"nonce\":{}}}",connection.pid(),body,json(pending)));
        };
        const auto disconnect=[&] {connection.close();received=sent=0;ready=false;pending.clear();next_connect=Clock::now()+retry_delay;};
        while(!stopping.load(std::memory_order_relaxed)) {
            const auto value=desired.load(std::memory_order_relaxed);
            const auto now=Clock::now();
            if(value && (!observed || ((value&2) && !(observed&2)))) started=std::time(nullptr);
            if(!value) {
                if(ready) activity(0);
                if(connection.valid()) disconnect();
                status=Status::disabled;next_connect={};
            } else if(!connection.valid() && now>=next_connect) {
                if(connection.open() && connection.frame(0,std::format("{{\"v\":1,\"client_id\":{}}}",json(application)))) {
                    status=Status::connecting;handshake_deadline=now+retry_delay;
                } else {disconnect();status=Status::waiting;}
            }
            if(value && connection.valid()) {
                unsigned count=0;
                if(!connection.receive(input.data()+received,static_cast<unsigned>(input.size())-received,count)) {disconnect();status=Status::waiting;}
                else {
                    received+=count;unsigned consumed=0;
                    while(received-consumed>=8) {
                        std::array<std::uint32_t,2> header{};std::memcpy(header.data(),input.data()+consumed,8);
                        if(header[1]>max_payload) {disconnect();status=Status::rejected;break;}
                        if(received-consumed<8+header[1]) break;
                        const std::string_view payload(input.data()+consumed+8,header[1]);consumed+=8+header[1];
                        if(header[0]==3) {if(!connection.frame(4,payload)) {disconnect();status=Status::waiting;break;}}
                        else if(header[0]==2 || (header[0]==1 && field(payload,"evt")=="ERROR")) {disconnect();status=Status::rejected;break;}
                        else if(header[0]==1) {
                            if(!ready && field(payload,"evt")=="READY") {ready=true;next_update={};}
                            else if(ready && field(payload,"cmd")=="SET_ACTIVITY" && !pending.empty() && field(payload,"nonce")==pending) {status=Status::connected;pending.clear();}
                        } else if(header[0]!=4) {disconnect();status=Status::rejected;break;}
                    }
                    if(connection.valid() && consumed) {received-=consumed;std::memmove(input.data(),input.data()+consumed,received);}
                }
                if(connection.valid() && (!ready || !pending.empty()) && now>=handshake_deadline) {disconnect();status=Status::waiting;}
                if(connection.valid() && ready && value!=sent && now>=next_update) {
                    if(activity(value)) {sent=value;next_update=now+update_delay;handshake_deadline=now+retry_delay;}
                    else {disconnect();status=Status::waiting;}
                }
            }
            observed=value;
            std::unique_lock lock(mutex);
            const auto wake=[&]{return stopping.load(std::memory_order_relaxed) || desired.load(std::memory_order_relaxed)!=value;};
            if(value) changed.wait_for(lock,std::chrono::milliseconds(100),wake);
            else changed.wait(lock,wake);
        }
        if(ready) activity(0);
    }
};
DiscordPresence::DiscordPresence(std::string_view application):impl_(std::make_unique<Impl>(application)) {}
DiscordPresence::~DiscordPresence()=default;
void DiscordPresence::update(bool enabled,bool playing,bool settings) {
    auto& state=*impl_;
    if(enabled && !state.configured) {state.status=Status::unconfigured;return;}
    const auto value=enabled?(1U|(playing?2U:0U)|(settings?4U:0U)):0U;
    if(state.desired.load(std::memory_order_relaxed)==value) {
        if(!enabled && state.status==Status::unconfigured) state.status=Status::disabled;
        return;
    }
    {std::lock_guard lock(state.mutex);state.desired.store(value,std::memory_order_relaxed);}
    if(enabled && !state.worker.joinable()) state.worker=std::thread([&state]{state.run();});
    state.changed.notify_one();
}
std::string_view DiscordPresence::status() const {
    switch(impl_->status.load(std::memory_order_relaxed)) {
    case Status::disabled:return "Off";
    case Status::unconfigured:return "Discord Rich Presence is unavailable in this build.";
    case Status::waiting:return "Waiting for Discord desktop...";
    case Status::connecting:return "Connecting to Discord...";
    case Status::connected:return "Connected to Discord";
    case Status::rejected:return "Discord could not accept the activity. Retrying...";
    }
    return {};
}
}
