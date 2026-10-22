#pragma once
#include <cstdint>
#include <filesystem>
#ifdef _WIN32
#include <Windows.h>
#else
#include <windows.h>
#include <aio.h>

// Host contracts used by the Xbox file services and cooperative scheduler.
// POSIX hosts implement these with descriptors, AIO and guarded contexts.
// No Windows runtime is loaded.
namespace b2 {
constexpr DWORD INVALID_FILE_ATTRIBUTES=0xFFFFFFFF,INFINITE=0xFFFFFFFF;
constexpr DWORD ERROR_SUCCESS=0,ERROR_FILE_NOT_FOUND=2,ERROR_PATH_NOT_FOUND=3,ERROR_ACCESS_DENIED=5,
    ERROR_INVALID_HANDLE=6,ERROR_NOT_ENOUGH_MEMORY=8,ERROR_INVALID_PARAMETER=87,ERROR_INVALID_NAME=123,
    ERROR_SHARING_VIOLATION=32,ERROR_HANDLE_DISK_FULL=39,ERROR_HANDLE_EOF=38,ERROR_WRITE_PROTECT=19,
    ERROR_FILE_EXISTS=80,ERROR_DISK_FULL=112,ERROR_DIR_NOT_EMPTY=145,ERROR_ALREADY_EXISTS=183,
    ERROR_OPERATION_ABORTED=995,ERROR_IO_INCOMPLETE=996,ERROR_IO_PENDING=997,ERROR_NOT_FOUND=1168;
constexpr DWORD GENERIC_READ=0x80000000,GENERIC_WRITE=0x40000000,GENERIC_ALL=0x10000000,DELETE=0x10000,WRITE_DAC=0x40000,
    FILE_READ_DATA=1,FILE_WRITE_DATA=2,FILE_APPEND_DATA=4,FILE_READ_ATTRIBUTES=0x80,FILE_WRITE_ATTRIBUTES=0x100,
    FILE_SHARE_READ=1,FILE_SHARE_WRITE=2,FILE_SHARE_DELETE=4,
    CREATE_NEW=1,CREATE_ALWAYS=2,OPEN_EXISTING=3,OPEN_ALWAYS=4,TRUNCATE_EXISTING=5,
    FILE_ATTRIBUTE_READONLY=1,FILE_ATTRIBUTE_DIRECTORY=0x10,FILE_ATTRIBUTE_NORMAL=0x80,
    FILE_ATTRIBUTE_REPARSE_POINT=0x400,FILE_FLAG_WRITE_THROUGH=0x80000000,FILE_FLAG_OVERLAPPED=0x40000000,
    FILE_FLAG_NO_BUFFERING=0x20000000,FILE_FLAG_RANDOM_ACCESS=0x10000000,FILE_FLAG_SEQUENTIAL_SCAN=0x08000000,
    FILE_FLAG_DELETE_ON_CLOSE=0x04000000,FILE_FLAG_BACKUP_SEMANTICS=0x02000000,FILE_FLAG_OPEN_REPARSE_POINT=0x00200000,
    FILE_BEGIN=0,GetFileExInfoStandard=0,REG_DWORD=4,FIBER_FLAG_FLOAT_SWITCH=1;
constexpr DWORD PAGE_NOACCESS=1,PAGE_READONLY=2,PAGE_READWRITE=4,PAGE_WRITECOPY=8,
    PAGE_EXECUTE=0x10,PAGE_EXECUTE_READ=0x20,PAGE_EXECUTE_READWRITE=0x40,PAGE_EXECUTE_WRITECOPY=0x80,
    PAGE_GUARD=0x100,PAGE_NOCACHE=0x200,PAGE_WRITECOMBINE=0x400;
constexpr DWORD MEM_COMMIT=0x1000,MEM_RESERVE=0x2000,MEM_DECOMMIT=0x4000,MEM_RELEASE=0x8000,MEM_PRIVATE=0x20000,MEM_TOP_DOWN=0x100000;
struct FILETIME {DWORD dwLowDateTime=0,dwHighDateTime=0;};
struct SYSTEMTIME {WORD wYear,wMonth,wDayOfWeek,wDay,wHour,wMinute,wSecond,wMilliseconds;};
union ULARGE_INTEGER {struct {DWORD LowPart,HighPart;};std::uint64_t QuadPart;};
struct BY_HANDLE_FILE_INFORMATION {
    DWORD dwFileAttributes;FILETIME ftCreationTime,ftLastAccessTime,ftLastWriteTime;
    DWORD dwVolumeSerialNumber,nFileSizeHigh,nFileSizeLow,nNumberOfLinks,nFileIndexHigh,nFileIndexLow;
};
struct WIN32_FILE_ATTRIBUTE_DATA {DWORD dwFileAttributes;FILETIME ftCreationTime,ftLastAccessTime,ftLastWriteTime;DWORD nFileSizeHigh,nFileSizeLow;};
struct FILE_ALLOCATION_INFO {LARGE_INTEGER AllocationSize;};
struct FILE_END_OF_FILE_INFO {LARGE_INTEGER EndOfFile;};
struct FILE_DISPOSITION_INFO {BOOL DeleteFile;};
struct FILE_BASIC_INFO {LARGE_INTEGER CreationTime,LastAccessTime,LastWriteTime,ChangeTime;DWORD FileAttributes;};
enum FILE_INFO_BY_HANDLE_CLASS {FileBasicInfo,FileAllocationInfo,FileEndOfFileInfo,FileDispositionInfo};
struct OVERLAPPED {DWORD Offset=0,OffsetHigh=0;aiocb io{};bool submitted=false;};
DWORD GetLastError();
HANDLE CreateFileW(const char*,DWORD,DWORD,void*,DWORD,DWORD,HANDLE);
BOOL CloseHandle(HANDLE);
DWORD GetFileAttributesW(const char*);
BOOL GetFileAttributesExW(const char*,DWORD,WIN32_FILE_ATTRIBUTE_DATA*);
BOOL CreateDirectoryW(const char*,void*);
BOOL GetFileInformationByHandle(HANDLE,BY_HANDLE_FILE_INFORMATION*);
BOOL GetFileSizeEx(HANDLE,LARGE_INTEGER*);
BOOL SetFilePointerEx(HANDLE,LARGE_INTEGER,LARGE_INTEGER*,DWORD);
BOOL SetFileInformationByHandle(HANDLE,FILE_INFO_BY_HANDLE_CLASS,void*,DWORD);
BOOL GetDiskFreeSpaceExW(const char*,ULARGE_INTEGER*,ULARGE_INTEGER*,ULARGE_INTEGER*);
BOOL ReadFile(HANDLE,void*,DWORD,DWORD*,OVERLAPPED*);
BOOL WriteFile(HANDLE,const void*,DWORD,DWORD*,OVERLAPPED*);
BOOL GetOverlappedResult(HANDLE,OVERLAPPED*,DWORD*,BOOL);
BOOL CancelIoEx(HANDLE,OVERLAPPED*);
BOOL FlushFileBuffers(HANDLE);
void GetSystemTimeAsFileTime(FILETIME*);
BOOL FileTimeToSystemTime(const FILETIME*,SYSTEMTIME*);
BOOL SystemTimeToFileTime(const SYSTEMTIME*,FILETIME*);
void Sleep(DWORD);
void* CreateFiberEx(std::size_t,std::size_t,DWORD,void(*)(void*),void*);
void DeleteFiber(void*);
void* ConvertThreadToFiberEx(void*,DWORD);
BOOL ConvertFiberToThread();
BOOL IsThreadAFiber();
void* GetCurrentFiber();
void SwitchToFiber(void*);
}
#endif
namespace b2 {
std::filesystem::path executable_path();
std::uint32_t ethernet_status();
std::uint32_t dos_error(std::uint32_t status);
void random_bytes(void*,std::size_t);
std::string cpu_name();
std::string build_description();
}
