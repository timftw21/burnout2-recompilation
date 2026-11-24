#ifdef __APPLE__
#include <SystemConfiguration/SystemConfiguration.h>
#endif
#include "host.h"
#include <algorithm>
#include <array>
#include <cerrno>
#include <climits>
#include <chrono>
#include <cstdlib>
#include <cstring>
#include <exception>
#include <fstream>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <string>
#include <vector>
#include <fcntl.h>
#ifdef __APPLE__
#include <crt_externs.h>
#include <mach-o/dyld.h>
#include <ifaddrs.h>
#include <net/if.h>
#include <net/if_dl.h>
#include <net/if_media.h>
#include <sys/ioctl.h>
#include <sys/sockio.h>
#include <sys/sysctl.h>
#else
#include <cpuid.h>
#include <linux/if_arp.h>
#include <sys/syscall.h>
#endif
#include <openssl/rand.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/statvfs.h>
#include <sys/xattr.h>
#ifndef __APPLE__
#include <ucontext.h>
#else
extern "C" void b2_fiber_switch(void*,const void*);
#endif
#include <unistd.h>
#ifdef __APPLE__
#define st_atim st_atimespec
#define st_mtim st_mtimespec
#define st_ctim st_ctimespec
#endif

namespace b2 {
std::string cpu_name() {
#ifdef __APPLE__
    std::array<char,256> name{};auto size=name.size();
    return !sysctlbyname("machdep.cpu.brand_string",name.data(),&size,nullptr,0)?name.data():"Unknown Mac CPU";
#else
    std::array<char,49> name{};unsigned a,b,c,d;
    if(__get_cpuid_max(0x80000000,nullptr)>=0x80000004) for(unsigned i=0;i<3;++i) {
        __cpuid(0x80000002+i,a,b,c,d);const std::array words{a,b,c,d};std::memcpy(name.data()+16*i,words.data(),16);
    }
    return name[0]?name.data():"Unknown x86-64 CPU";
#endif
}
std::string build_description() {
#if defined(__APPLE__) && defined(__aarch64__)
    constexpr auto format="Mach-O arm64, ";
#elif defined(__APPLE__)
    constexpr auto format="Mach-O x86-64, ";
#else
    constexpr auto format="ELF x86-64, ";
#endif
    return format+std::to_string(std::filesystem::file_size(executable_path()))+" bytes";
}
namespace {
thread_local DWORD last_error=0;
DWORD error_code(int value) {
    switch(value) {
    case 0:return 0;case ENOENT:return ERROR_FILE_NOT_FOUND;case ENOTDIR:return ERROR_PATH_NOT_FOUND;
    case EACCES:case EPERM:case ELOOP:case EISDIR:return ERROR_ACCESS_DENIED;case EEXIST:return ERROR_ALREADY_EXISTS;
    case ENOSPC:case EDQUOT:return ERROR_DISK_FULL;case EINVAL:case EOVERFLOW:return ERROR_INVALID_PARAMETER;
    case ENOTEMPTY:return ERROR_DIR_NOT_EMPTY;case EROFS:return ERROR_WRITE_PROTECT;
    case ENOMEM:return ERROR_NOT_ENOUGH_MEMORY;case ECANCELED:return ERROR_OPERATION_ABORTED;
    case EINPROGRESS:return ERROR_IO_INCOMPLETE;default:return 31; // ERROR_GEN_FAILURE; reported by the guest service boundary.
    }
}
BOOL failed(int error=errno) {last_error=error_code(error);return FALSE;}
struct OpenInode {
    dev_t device;ino_t number;
    int append_fd=-1;
    std::vector<std::filesystem::path> pending_unlinks;
    explicit OpenInode(const struct stat& s):device(s.st_dev),number(s.st_ino) {}
    ~OpenInode(){if(append_fd>=0) close(append_fd);}
    bool matches(const struct stat& value) const {return device==value.st_dev && number==value.st_ino;}
};
bool append_only(DWORD access) {return (access&FILE_APPEND_DATA) && !(access&(FILE_WRITE_DATA|GENERIC_WRITE|GENERIC_ALL));}
struct Handle {
    int fd;bool delete_on_close,disposition;DWORD access,share;
    std::filesystem::path path;std::shared_ptr<OpenInode> inode;
};
// Share modes and delete disposition apply across host threads and hard links.
std::mutex files_mutex;
std::vector<Handle*> files;
Handle& handle(HANDLE value) {return *static_cast<Handle*>(value);}
constexpr std::int64_t epoch=11644473600LL;
FILETIME timestamp(timespec value) {
    const auto ticks=std::uint64_t(value.tv_sec+epoch)*10000000+value.tv_nsec/100;
    return {DWORD(ticks),DWORD(ticks>>32)};
}
timespec timestamp(LARGE_INTEGER value) {
    return {value.QuadPart/10000000-epoch,(value.QuadPart%10000000)*100};
}
DWORD attributes(const struct stat& value) {
    const DWORD flags=(S_ISDIR(value.st_mode)?FILE_ATTRIBUTE_DIRECTORY:0)|
        (S_ISLNK(value.st_mode)?FILE_ATTRIBUTE_REPARSE_POINT:0)|((value.st_mode&0222)?0:FILE_ATTRIBUTE_READONLY);
    return flags?flags:FILE_ATTRIBUTE_NORMAL;
}
FILETIME creation_time(int fd,const char* path,const struct stat& status) {
    std::uint64_t saved=0;
#ifdef __APPLE__
    const auto size=fd>=0?fgetxattr(fd,"b2.creation_time",&saved,sizeof(saved),0,0):getxattr(path,"b2.creation_time",&saved,sizeof(saved),0,0);
    if(size==sizeof(saved)) return {DWORD(saved),DWORD(saved>>32)};
    return timestamp(status.st_birthtimespec);
#else
    const auto size=fd>=0?fgetxattr(fd,"user.b2.creation_time",&saved,sizeof(saved)):getxattr(path,"user.b2.creation_time",&saved,sizeof(saved));
    if(size==sizeof(saved)) return {DWORD(saved),DWORD(saved>>32)};
    struct statx value{};
    if(!statx(fd>=0?fd:AT_FDCWD,fd>=0?"":path,fd>=0?AT_EMPTY_PATH:AT_SYMLINK_NOFOLLOW,STATX_BTIME,&value) && (value.stx_mask&STATX_BTIME))
        return timestamp(timespec{value.stx_btime.tv_sec,value.stx_btime.tv_nsec});
    return timestamp(status.st_ctim);
#endif
}
int set_file_times(int fd,const FILE_BASIC_INFO& value) {
    // futimens only sets access and modification times. Keep explicit Xbox
    // creation and change times on the file across close and reopen.
#ifdef __APPLE__
    if(value.CreationTime.QuadPart>0 && fsetxattr(fd,"b2.creation_time",&value.CreationTime.QuadPart,8,0,0)) return -1;
    if(value.ChangeTime.QuadPart>0 && fsetxattr(fd,"b2.change_time",&value.ChangeTime.QuadPart,8,0,0)) return -1;
#else
    if(value.CreationTime.QuadPart>0 && fsetxattr(fd,"user.b2.creation_time",&value.CreationTime.QuadPart,8,0)) return -1;
    if(value.ChangeTime.QuadPart>0 && fsetxattr(fd,"user.b2.change_time",&value.ChangeTime.QuadPart,8,0)) return -1;
#endif
    if(value.LastAccessTime.QuadPart<=0 && value.LastWriteTime.QuadPart<=0) return 0;
    std::array<timespec,2> times{{{0,UTIME_OMIT},{0,UTIME_OMIT}}};
    if(value.LastAccessTime.QuadPart>0) times[0]=timestamp(value.LastAccessTime);
    if(value.LastWriteTime.QuadPart>0) times[1]=timestamp(value.LastWriteTime);
    return futimens(fd,times.data());
}
struct Fiber {
#ifdef __APPLE__
    alignas(16) std::array<std::byte,640> context{};
#else
    ucontext_t context{};
#if defined(__x86_64__)
    alignas(16) std::array<std::byte,512> floating{};
#endif
#endif
    void* stack=nullptr;std::size_t size=0;void(*entry)(void*)=nullptr;void* argument=nullptr;
    ~Fiber(){if(stack) munmap(stack,size);}
};
thread_local Fiber* current_fiber=nullptr;
void fiber_entry() {
    auto* fiber=current_fiber;fiber->entry(fiber->argument);
    std::terminate(); // Guest thread exit must explicitly return to the scheduler.
}
}
DWORD GetLastError() {return last_error;}
HANDLE CreateFileW(const char* path,DWORD access,DWORD share,void*,DWORD mode,DWORD flags,HANDLE) {
    std::lock_guard lock(files_mutex);
    const auto normalized=std::filesystem::absolute(path).lexically_normal();
    if(access&GENERIC_ALL) access|=GENERIC_READ|GENERIC_WRITE|DELETE;
    if(flags&FILE_FLAG_DELETE_ON_CLOSE) access|=DELETE;
    const auto wants_read=bool(access&(GENERIC_READ|FILE_READ_DATA)),wants_write=bool(access&(GENERIC_WRITE|FILE_WRITE_DATA|FILE_APPEND_DATA));
    struct stat target_status{};const bool exists=!lstat(path,&target_status);
    std::shared_ptr<OpenInode> inode;
    for(const auto* file:files) {
        if(!exists || !file->inode->matches(target_status)) continue;
        inode=file->inode;
        if(file->disposition || !inode->pending_unlinks.empty()) {last_error=ERROR_ACCESS_DENIED;return INVALID_HANDLE_VALUE;}
        if((wants_read && !(file->share&FILE_SHARE_READ)) || (wants_write && !(file->share&FILE_SHARE_WRITE)) ||
           ((access&DELETE) && !(file->share&FILE_SHARE_DELETE)) ||
           ((file->access&(GENERIC_READ|FILE_READ_DATA)) && !(share&FILE_SHARE_READ)) ||
           ((file->access&(GENERIC_WRITE|FILE_WRITE_DATA|FILE_APPEND_DATA)) && !(share&FILE_SHARE_WRITE)) ||
           ((file->access&DELETE) && !(share&FILE_SHARE_DELETE))) {
            last_error=ERROR_SHARING_VIOLATION;return INVALID_HANDLE_VALUE;
        }
    }
    const auto opened=[&](int fd)->HANDLE {
        if(fd<0) {failed();return INVALID_HANDLE_VALUE;}
        struct stat status{};
        if(fstat(fd,&status)) {const int error=errno;close(fd);failed(error);return INVALID_HANDLE_VALUE;}
        if(!inode || !inode->matches(status)) inode=std::make_shared<OpenInode>(status);
        if(append_only(access) && inode->append_fd<0 && (inode->append_fd=fcntl(fd,F_DUPFD_CLOEXEC,0))<0) {
            const auto error=errno;close(fd);failed(error);return INVALID_HANDLE_VALUE;
        }
        auto* result=new Handle{fd,bool(flags&FILE_FLAG_DELETE_ON_CLOSE),false,access,share,normalized,std::move(inode)};
        files.push_back(result);return result;
    };
    int options=(wants_write?(wants_read?O_RDWR:O_WRONLY):O_RDONLY)|O_CLOEXEC|O_NOFOLLOW;
    // O_APPEND makes EOF selection and the write one operation, including aio_write.
    if(append_only(access)) options|=O_APPEND;
    struct stat status{};
    if((flags&FILE_FLAG_BACKUP_SEMANTICS) && !lstat(path,&status) && S_ISDIR(status.st_mode)) {
        // Directory contents are changed through namespace operations, not
        // read/write on the directory descriptor. Retain the guest's access
        // mask for permission/share checks and use a native directory handle.
        return opened(open(path,O_RDONLY|O_DIRECTORY|O_CLOEXEC|O_NOFOLLOW));
    }
    switch(mode) {
    case CREATE_NEW:options|=O_CREAT|O_EXCL;break;case CREATE_ALWAYS:options|=O_CREAT|O_TRUNC;break;
    case OPEN_ALWAYS:options|=O_CREAT;break;case TRUNCATE_EXISTING:options|=O_TRUNC;break;
    case OPEN_EXISTING:break;default:last_error=ERROR_INVALID_PARAMETER;return INVALID_HANDLE_VALUE;
    }
    if(flags&FILE_FLAG_WRITE_THROUGH) options|=O_SYNC;
    const auto fd=open(path,options,0666);
    if(fd<0) {failed();return INVALID_HANDLE_VALUE;}
#ifdef __APPLE__
    if(flags&FILE_FLAG_RANDOM_ACCESS) fcntl(fd,F_RDAHEAD,0);
    if(flags&FILE_FLAG_SEQUENTIAL_SCAN) fcntl(fd,F_RDAHEAD,1);
#else
    if(flags&FILE_FLAG_RANDOM_ACCESS) posix_fadvise(fd,0,0,POSIX_FADV_RANDOM);
    if(flags&FILE_FLAG_SEQUENTIAL_SCAN) posix_fadvise(fd,0,0,POSIX_FADV_SEQUENTIAL);
#endif
    return opened(fd);
}
BOOL CloseHandle(HANDLE value) {
    if(!value || value==INVALID_HANDLE_VALUE) return FALSE;
    std::lock_guard lock(files_mutex);
    auto* item=static_cast<Handle*>(value);bool okay=true;
    auto& pending=item->inode->pending_unlinks;
    if((item->delete_on_close || item->disposition) && std::ranges::find(pending,item->path)==pending.end())
        pending.push_back(std::move(item->path));
    if(item->inode.use_count()==1) for(const auto& path:pending) {
        struct stat status{};
        if(lstat(path.c_str(),&status)) {if(errno!=ENOENT) okay=failed();continue;}
        if(item->inode->matches(status) && (S_ISDIR(status.st_mode)?rmdir(path.c_str()):unlink(path.c_str()))) okay=failed();
    }
    if(close(item->fd)) okay=failed();
    std::erase(files,item);
    delete item;return okay;
}
DWORD GetFileAttributesW(const char* path) {
    std::lock_guard lock(files_mutex);
    struct stat value{};if(lstat(path,&value)) {failed();return INVALID_FILE_ATTRIBUTES;}return attributes(value);
}
BOOL GetFileAttributesExW(const char* path,DWORD,WIN32_FILE_ATTRIBUTE_DATA* output) {
    std::lock_guard lock(files_mutex);
    struct stat value{};if(lstat(path,&value)) return failed();
    *output={attributes(value),creation_time(-1,path,value),timestamp(value.st_atim),timestamp(value.st_mtim),DWORD(std::uint64_t(value.st_size)>>32),DWORD(value.st_size)};return TRUE;
}
BOOL CreateDirectoryW(const char* path,void*) {return mkdir(path,0777)?failed():TRUE;}
BOOL GetFileInformationByHandle(HANDLE file,BY_HANDLE_FILE_INFORMATION* output) {
    std::lock_guard lock(files_mutex);
    struct stat value{};if(fstat(handle(file).fd,&value)) return failed();
    *output={attributes(value),creation_time(handle(file).fd,nullptr,value),timestamp(value.st_atim),timestamp(value.st_mtim),DWORD(value.st_dev),
        DWORD(std::uint64_t(value.st_size)>>32),DWORD(value.st_size),DWORD(value.st_nlink),DWORD(std::uint64_t(value.st_ino)>>32),DWORD(value.st_ino)};return TRUE;
}
BOOL GetFileSizeEx(HANDLE file,LARGE_INTEGER* output) {
    struct stat value{};if(fstat(handle(file).fd,&value)) return failed();output->QuadPart=value.st_size;return TRUE;
}
BOOL SetFilePointerEx(HANDLE file,LARGE_INTEGER position,LARGE_INTEGER* output,DWORD method) {
    const auto result=lseek(handle(file).fd,position.QuadPart,method);
    if(result<0) return failed();if(output) output->QuadPart=result;return TRUE;
}
BOOL SetFileInformationByHandle(HANDLE file,FILE_INFO_BY_HANDLE_CLASS kind,void* data,DWORD) {
    auto& item=handle(file);
    if(kind==FileEndOfFileInfo) return ftruncate(item.fd,static_cast<FILE_END_OF_FILE_INFO*>(data)->EndOfFile.QuadPart)?failed():TRUE;
    if(kind==FileAllocationInfo) {
        const auto size=static_cast<FILE_ALLOCATION_INFO*>(data)->AllocationSize.QuadPart;
        struct stat status{};if(fstat(item.fd,&status)) return failed();
        if(size<status.st_size) return ftruncate(item.fd,size)?failed():TRUE;
        // Extending the allocation reserves storage without extending EOF.
#ifdef __APPLE__
        const auto allocated=std::uint64_t(status.st_blocks)*512;
        if(std::uint64_t(size)<=allocated) return TRUE;
        fstore_t allocation{};allocation.fst_flags=F_ALLOCATEALL;allocation.fst_posmode=F_PEOFPOSMODE;
        allocation.fst_length=std::uint64_t(size)-allocated;
        return size && fcntl(item.fd,F_PREALLOCATE,&allocation)?failed():TRUE;
#else
        return size && fallocate(item.fd,FALLOC_FL_KEEP_SIZE,0,size)?failed():TRUE;
#endif
    }
    if(kind==FileDispositionInfo) {
        std::lock_guard lock(files_mutex);
        if(!(item.access&DELETE)) {last_error=ERROR_ACCESS_DENIED;return FALSE;}
        item.disposition=static_cast<FILE_DISPOSITION_INFO*>(data)->DeleteFile!=FALSE;return TRUE;
    }
    if(!(item.access&(GENERIC_WRITE|FILE_WRITE_ATTRIBUTES))) return failed(EACCES);
    const auto& value=*static_cast<const FILE_BASIC_INFO*>(data);
    std::lock_guard lock(files_mutex);
    struct stat status{};if(fstat(item.fd,&status)) return failed();
    const mode_t original_mode=status.st_mode&07777;
    mode_t final_mode=original_mode;
    if(value.FileAttributes) final_mode=value.FileAttributes&FILE_ATTRIBUTE_READONLY?original_mode&~0222:original_mode|S_IWUSR;
    // POSIX checks current write permission for extended attributes even on
    // an already-open descriptor. Grant only the owner's bit while updating
    // a read-only save. Guest opens and metadata queries share this lock.
    const bool writes_xattr=value.CreationTime.QuadPart>0 || value.ChangeTime.QuadPart>0;
    mode_t update_mode=original_mode|(final_mode&S_IWUSR);
    if(writes_xattr && !(update_mode&S_IWUSR) && status.st_uid==geteuid()) update_mode|=S_IWUSR;
    if(update_mode!=original_mode && fchmod(item.fd,update_mode)) return failed();
    if(set_file_times(item.fd,value) || (final_mode!=update_mode && fchmod(item.fd,final_mode))) {
        const int error=errno;
        if(update_mode!=original_mode && fchmod(item.fd,original_mode)) return failed();
        return failed(error);
    }
    return TRUE;
}
BOOL GetDiskFreeSpaceExW(const char* path,ULARGE_INTEGER* available,ULARGE_INTEGER* total,ULARGE_INTEGER* free) {
    struct statvfs value{};if(statvfs(path,&value)) return failed();
    if(available) available->QuadPart=std::uint64_t(value.f_bavail)*value.f_frsize;
    if(total) total->QuadPart=std::uint64_t(value.f_blocks)*value.f_frsize;
    if(free) free->QuadPart=std::uint64_t(value.f_bfree)*value.f_frsize;return TRUE;
}
namespace {
BOOL transfer(HANDLE file,void* buffer,DWORD count,DWORD* transferred,OVERLAPPED* overlap,bool write) {
    const auto& item=handle(file);
    const bool append=write && append_only(item.access);
    if(overlap || append) {
        // Route every append to the same AIO queue for this inode. Some mounted
        // filesystems implement O_APPEND separately for each open description.
        OVERLAPPED synchronous{};auto& request=overlap?*overlap:synchronous;
        auto& io=request.io;io={};io.aio_fildes=append?item.inode->append_fd:item.fd;io.aio_buf=buffer;io.aio_nbytes=count;
        io.aio_offset=append?0:(std::uint64_t(request.OffsetHigh)<<32)|request.Offset;io.aio_sigevent.sigev_notify=SIGEV_NONE;
        if((write?aio_write(&io):aio_read(&io))) return failed();
        request.submitted=true;
        if(overlap) {last_error=ERROR_IO_PENDING;return FALSE;}
        DWORD written=0;if(!GetOverlappedResult(file,&request,&written,TRUE)) return FALSE;
        if(transferred) *transferred=written;
        return lseek(item.fd,0,SEEK_END)<0?failed():TRUE;
    }
    ssize_t result;do {result=write ? ::write(handle(file).fd,buffer,count) : ::read(handle(file).fd,buffer,count);} while(result<0 && errno==EINTR);
    if(result<0) return failed();if(transferred) *transferred=DWORD(result);return TRUE;
}
}
BOOL ReadFile(HANDLE file,void* buffer,DWORD count,DWORD* transferred,OVERLAPPED* overlap) {return transfer(file,buffer,count,transferred,overlap,false);}
BOOL WriteFile(HANDLE file,const void* buffer,DWORD count,DWORD* transferred,OVERLAPPED* overlap) {return transfer(file,const_cast<void*>(buffer),count,transferred,overlap,true);}
BOOL GetOverlappedResult(HANDLE,OVERLAPPED* overlap,DWORD* transferred,BOOL wait) {
    int error;
    while((error=aio_error(&overlap->io))==EINPROGRESS) {
        if(!wait) return failed(error);
        const aiocb* list[]={&overlap->io};if(aio_suspend(list,1,nullptr) && errno!=EINTR) return failed();
    }
    if(!overlap->submitted) {last_error=ERROR_NOT_FOUND;return FALSE;}
    const auto result=aio_return(&overlap->io);overlap->submitted=false;
    if(error) return failed(error);if(result<0) return failed();*transferred=DWORD(result);return TRUE;
}
BOOL CancelIoEx(HANDLE,OVERLAPPED* overlap) {
    if(!overlap->submitted) {last_error=ERROR_NOT_FOUND;return FALSE;}
    return aio_cancel(overlap->io.aio_fildes,&overlap->io)<0?failed():TRUE;
}
BOOL FlushFileBuffers(HANDLE file) {return fsync(handle(file).fd)?failed():TRUE;}
void GetSystemTimeAsFileTime(FILETIME* output) {timespec now{};clock_gettime(CLOCK_REALTIME,&now);*output=timestamp(now);}
BOOL FileTimeToSystemTime(const FILETIME* input,SYSTEMTIME* output) {
    const auto ticks=(std::uint64_t(input->dwHighDateTime)<<32)|input->dwLowDateTime;
    if(ticks>INT64_MAX) return FALSE;
    const time_t seconds=std::int64_t(ticks/10000000)-epoch;tm value{};if(!gmtime_r(&seconds,&value)) return FALSE;
    *output={WORD(value.tm_year+1900),WORD(value.tm_mon+1),WORD(value.tm_wday),WORD(value.tm_mday),WORD(value.tm_hour),WORD(value.tm_min),WORD(value.tm_sec),WORD(ticks%10000000/10000)};return TRUE;
}
BOOL SystemTimeToFileTime(const SYSTEMTIME* input,FILETIME* output) {
    if(input->wYear<1601 || input->wMonth<1 || input->wMonth>12 || input->wDay<1 || input->wDay>31 || input->wHour>23 || input->wMinute>59 || input->wSecond>59 || input->wMilliseconds>999) return FALSE;
    tm value{};value.tm_year=input->wYear-1900;value.tm_mon=input->wMonth-1;value.tm_mday=input->wDay;
    value.tm_hour=input->wHour;value.tm_min=input->wMinute;value.tm_sec=input->wSecond;
    const auto seconds=timegm(&value);
    if(value.tm_year!=input->wYear-1900 || value.tm_mon!=input->wMonth-1 || value.tm_mday!=input->wDay) return FALSE;
    *output=timestamp(timespec{seconds,long(input->wMilliseconds)*1000000});return TRUE;
}
void Sleep(DWORD milliseconds) {
    timespec duration{milliseconds/1000,long(milliseconds%1000)*1000000};while(nanosleep(&duration,&duration) && errno==EINTR) {}
}
void* CreateFiberEx(std::size_t,std::size_t size,DWORD,void(*entry)(void*),void* argument) {
    auto fiber=std::make_unique<Fiber>();const auto page=std::size_t(sysconf(_SC_PAGESIZE));
    size=(size+page-1)&~(page-1);fiber->size=size+2*page;
    fiber->stack=mmap(nullptr,fiber->size,PROT_NONE,MAP_PRIVATE|MAP_ANONYMOUS,-1,0);
    if(fiber->stack==MAP_FAILED) {fiber->stack=nullptr;return nullptr;}
    auto* usable=static_cast<std::byte*>(fiber->stack)+page;
    if(mprotect(usable,size,PROT_READ|PROT_WRITE)) return nullptr;
#ifdef __APPLE__
    const auto top=reinterpret_cast<std::uintptr_t>(usable)+size;
#if defined(__aarch64__)
    const auto entry_address=reinterpret_cast<std::uintptr_t>(fiber_entry);std::memcpy(fiber->context.data()+88,&entry_address,8);
    std::memcpy(fiber->context.data()+96,&top,8);
    std::uint64_t fpcr,fpsr;__asm__ volatile("mrs %0, fpcr\n\tmrs %1, fpsr":"=r"(fpcr),"=r"(fpsr));
    std::memcpy(fiber->context.data()+176,&fpcr,8);std::memcpy(fiber->context.data()+184,&fpsr,8);
#else
    const auto sp=top-16;const auto entry_address=reinterpret_cast<std::uintptr_t>(fiber_entry);std::memcpy(reinterpret_cast<void*>(sp),&entry_address,8);
    // The switch restores RSP then RETs to the initial function.
    std::memcpy(fiber->context.data()+48,&sp,8);
    __asm__ volatile("fxsave64 (%0)"::"r"(fiber->context.data()+64):"memory");
#endif
    fiber->entry=entry;fiber->argument=argument;
#else
    if(getcontext(&fiber->context)) return nullptr;
    fiber->context.uc_stack={usable,0,size};fiber->entry=entry;fiber->argument=argument;
#if defined(__x86_64__)
    __asm__ volatile("fxsave64 %0":"=m"(fiber->floating));
#endif
    makecontext(&fiber->context,fiber_entry,0);
#endif
    return fiber.release();
}
void DeleteFiber(void* fiber) {delete static_cast<Fiber*>(fiber);}
void* ConvertThreadToFiberEx(void*,DWORD) {
    if(current_fiber) return nullptr;current_fiber=new Fiber;
#if defined(__x86_64__) && !defined(__APPLE__)
    __asm__ volatile("fxsave64 %0":"=m"(current_fiber->floating));
#endif
    return current_fiber;
}
BOOL ConvertFiberToThread() {if(!current_fiber || current_fiber->stack) return FALSE;delete current_fiber;current_fiber=nullptr;return TRUE;}
BOOL IsThreadAFiber() {return current_fiber!=nullptr;}
void* GetCurrentFiber() {return current_fiber;}
void SwitchToFiber(void* target) {
    auto* previous=current_fiber;auto* next=static_cast<Fiber*>(target);
#if defined(__x86_64__) && !defined(__APPLE__)
    __asm__ volatile("fxsave64 %0":"=m"(previous->floating));
#endif
    current_fiber=next;
#ifdef __APPLE__
    b2_fiber_switch(previous->context.data(),next->context.data());
#else
    if(swapcontext(&previous->context,&next->context)) std::terminate();
#endif
#if defined(__x86_64__) && !defined(__APPLE__)
    __asm__ volatile("fxrstor64 %0"::"m"(previous->floating));
#endif
}
std::filesystem::path executable_path() {
#ifdef __APPLE__
    std::uint32_t size=0;_NSGetExecutablePath(nullptr,&size);std::string path(size,'\0');
    if(_NSGetExecutablePath(path.data(),&size)) throw std::runtime_error("Cannot locate executable");
    return std::filesystem::canonical(path.c_str());
#else
    std::array<char,32768> path{};const auto length=readlink("/proc/self/exe",path.data(),path.size());
    if(length<0 || std::size_t(length)==path.size()) throw std::runtime_error("Cannot locate executable");return std::string(path.data(),length);
#endif
}
std::filesystem::path data_root() {
    static const auto root=[] {
        const auto executable=executable_path();
        const char* home=std::getenv("HOME");
        std::filesystem::path packaged;
#ifdef __APPLE__
        const auto contents=executable.parent_path().parent_path();
        if(executable.parent_path().filename()=="MacOS" && contents.filename()=="Contents" && contents.parent_path().extension()==".app") {
            if(!home || *home!='/') throw std::runtime_error("HOME must name your user folder");
            packaged=std::filesystem::path(home)/"Library/Application Support/Burnout 2 Recompilation";
        }
#else
        // The AppImage runtime mounts the package read-only and names its mount in APPDIR.
        const char* image=std::getenv("APPIMAGE");const char* mount=std::getenv("APPDIR");
        std::string_view prefix(mount?mount:"");
        while(prefix.size()>1 && prefix.back()=='/') prefix.remove_suffix(1);
        if(image && *image && prefix.starts_with('/') && executable.native().starts_with(std::string(prefix)+'/')) {
            const char* data=std::getenv("XDG_DATA_HOME");
            if(data && *data=='/') packaged=std::filesystem::path(data)/"burnout2-recompilation";
            else if(home && *home=='/') packaged=std::filesystem::path(home)/".local/share/burnout2-recompilation";
            else throw std::runtime_error("HOME must name your user folder");
        }
#endif
        if(packaged.empty()) return executable.parent_path();
        std::filesystem::create_directories(packaged);
        return std::filesystem::canonical(packaged);
    }();
    return root;
}
std::filesystem::path resource_path(std::string_view name) {
    const auto directory=executable_path().parent_path();
#ifdef __APPLE__
    // Application packages keep data in Contents/Resources, outside the signed code folder.
    if(const auto bundled=directory.parent_path()/"Resources"/name;!std::filesystem::exists(directory/"resources"/name) && std::filesystem::exists(bundled))
        return bundled;
#endif
    return directory/"resources"/name;
}
void random_bytes(void* output,std::size_t size) {
    if(size>INT_MAX || RAND_bytes(static_cast<unsigned char*>(output),static_cast<int>(size))!=1) throw std::runtime_error("Native random source failed");
}
namespace {
std::uint32_t ethernet_link(std::int64_t mbps) {return 1|(mbps>=100?2U:mbps>=10?4U:0U);}
#ifdef __APPLE__
std::uint32_t ethernet_media_link(unsigned media,std::uint32_t baudrate) {
    switch(IFM_SUBTYPE(media)) {
    case IFM_10_T:case IFM_10_2:case IFM_10_5:case IFM_10_STP:case IFM_10_FL:return ethernet_link(10);
    case IFM_100_TX:case IFM_100_FX:case IFM_100_T4:case IFM_100_VG:case IFM_100_T2:
    case IFM_1000_SX:case IFM_1000_LX:case IFM_1000_CX:case IFM_1000_T:case IFM_2500_T:
    case IFM_10G_T:case IFM_10G_SR:case IFM_10G_LR:case IFM_10G_CX4:return ethernet_link(100);
    default:return ethernet_link(baudrate/1000000);
    }
}
std::uint32_t wired_interfaces(CFArrayRef devices,const ifaddrs* interfaces,int socket) {
    std::uint32_t result=0;
    for(CFIndex i=0;i<CFArrayGetCount(devices);++i) {
        const auto device=static_cast<SCNetworkInterfaceRef>(CFArrayGetValueAtIndex(devices,i));
        const auto type=SCNetworkInterfaceGetInterfaceType(device),name=SCNetworkInterfaceGetBSDName(device);
        if(!type || !name || !CFEqual(type,kSCNetworkInterfaceTypeEthernet)) continue;
        ifmediareq media{};
        if(!CFStringGetCString(name,media.ifm_name,sizeof(media.ifm_name),kCFStringEncodingUTF8) ||
           ioctl(socket,SIOCGIFMEDIA,&media)<0 || IFM_TYPE(media.ifm_active)!=IFM_ETHER ||
           (media.ifm_status&(IFM_AVALID|IFM_ACTIVE))!=(IFM_AVALID|IFM_ACTIVE)) continue;
        for(auto* item=interfaces;item;item=item->ifa_next) {
            if(!item->ifa_addr || item->ifa_addr->sa_family!=AF_LINK || std::strcmp(media.ifm_name,item->ifa_name) ||
               (item->ifa_flags&(IFF_UP|IFF_RUNNING|IFF_LOOPBACK))!=(IFF_UP|IFF_RUNNING)) continue;
            const auto* data=static_cast<const if_data*>(item->ifa_data);
            result=ethernet_media_link(media.ifm_active,data?data->ifi_baudrate:0);
            if(result!=1) return result;
        }
    }
    return result;
}
#else
std::uint32_t wired_interfaces(const std::filesystem::path& directory) {
    for(const auto& entry:std::filesystem::directory_iterator(directory)) {
        const auto path=entry.path();unsigned type=0,carrier=0;int speed=0;
        std::ifstream(path/"type")>>type;if(type!=ARPHRD_ETHER || !std::filesystem::exists(path/"device")) continue;
        if(std::filesystem::exists(path/"phy80211") || std::filesystem::exists(path/"wireless")) continue;
        std::ifstream events(path/"uevent");std::string line;
        while(std::getline(events,line) && line!="DEVTYPE=wlan") {}
        if(line=="DEVTYPE=wlan") continue;
        std::ifstream(path/"carrier")>>carrier;if(!carrier) continue;
        std::ifstream(path/"speed")>>speed;return ethernet_link(speed);
    }
    return 0;
}
#endif
}
std::uint32_t ethernet_status() {
#ifdef __APPLE__
    const auto devices=SCNetworkInterfaceCopyAll();if(!devices) return 0;
    ifaddrs* interfaces=nullptr;const int fd=socket(AF_INET,SOCK_DGRAM,0);
    std::uint32_t status=0;
    if(fd>=0 && !getifaddrs(&interfaces)) {status=wired_interfaces(devices,interfaces,fd);freeifaddrs(interfaces);}
    if(fd>=0) close(fd);
    CFRelease(devices);return status;
#else
    return wired_interfaces("/sys/class/net");
#endif
}
std::uint32_t dos_error(std::uint32_t status) {
    // Verified against RtlNtStatusToDosError on the Windows host for every
    // NTSTATUS returned by these Xbox services.
    switch(status) {
    case 0:return 0;case 0xC0000008:return 6;case 0xC000000D:return 87;case 0xC0000017:return 8;
    case 0xC0000022:return 5;case 0xC0000023:return 122;case 0xC0000034:return 2;case 0xC0000035:return 183;
    case 0xC000003A:return 3;case 0xC0000043:return 32;case 0xC000007F:return 112;case 0xC0000011:return 38;
    case 0xC00000A2:return 19;case 0xC0000101:return 145;case 0xC0000120:return 995;case 0x102:return 1460;
    case 0x103:return 997;case 0x80000005:return 234;case 0x80000006:return 18;
    case 0xC0000013:return 21;case 0xC0000018:return 487;case 0xC0000024:return 6;case 0xC0000040:return 8;
    case 0xC0000047:return 298;case 0xC00000BA:return 5;case 0xC00000BB:return 50;case 0xC0000103:return 267;case 0xC0000106:return 206;
    default:throw std::runtime_error("NTSTATUS has no verified DOS error mapping: "+std::to_string(status));
    }
}
}
