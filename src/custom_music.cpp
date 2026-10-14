#include "custom_music.h"
#include "file.h"
#include <Windows.h>
#include <mfapi.h>
#include <mfidl.h>
#include <mfreadwrite.h>
#include <wrl/client.h>
#include <algorithm>
#include <array>
#include <cstring>
#include <cwctype>
#include <fstream>
#include <format>
#include <limits>
#include <vector>

namespace b2 {
namespace {
using Microsoft::WRL::ComPtr;
constexpr std::string_view cache_marker="Burnout 2 custom music cache v1\n";
void checked(HRESULT result,std::string_view operation) {
    if(FAILED(result)) throw std::runtime_error(std::format("{} failed (0x{:08X})",operation,static_cast<unsigned>(result)));
}
struct Media {
    std::array<HMODULE,3> modules{};
    Media() {
        // Delay loading keeps ordinary gameplay available on Windows editions
        // without Media Foundation. Only importing music needs these services.
        constexpr std::array names{L"mfplat.dll",L"mfreadwrite.dll",L"mf.dll"};
        for(unsigned i=0;i<names.size();++i) {
            modules[i]=LoadLibraryExW(names[i],nullptr,LOAD_LIBRARY_SEARCH_SYSTEM32);
            if(!modules[i]) {
                for(auto module:modules) if(module) FreeLibrary(module);
                throw std::runtime_error("Windows Media Foundation is unavailable. Install the Media Feature Pack to import music.");
            }
        }
        const auto initialized=CoInitializeEx(nullptr,COINIT_MULTITHREADED);
        if(FAILED(initialized)) {for(auto module:modules) FreeLibrary(module);checked(initialized,"Music import initialization");}
        try {checked(MFStartup(MF_VERSION,MFSTARTUP_FULL),"Windows media initialization");}
        catch(...) {CoUninitialize();for(auto module:modules) FreeLibrary(module);throw;}
    }
    ~Media() {MFShutdown();CoUninitialize();for(auto module:modules) FreeLibrary(module);}
};
bool music_file(const std::filesystem::path& path) {
    auto extension=path.extension().wstring();
    std::ranges::transform(extension,extension.begin(),[](wchar_t value){return std::towlower(value);});
    return extension==L".mp3" || extension==L".wav" || extension==L".wma" || extension==L".flac" ||
        extension==L".m4a" || extension==L".aac";
}
std::filesystem::path safe_directory(const std::filesystem::path& path) {
    const auto absolute=std::filesystem::absolute(path).lexically_normal();
    if(std::filesystem::weakly_canonical(absolute)!=absolute) throw std::runtime_error("Music cache must not cross a link or junction.");
    return absolute;
}
bool owned_cache(const std::filesystem::path& directory) {
    std::ifstream file(directory/"cache.txt",std::ios::binary);
    std::string marker(cache_marker.size(),'\0');file.read(marker.data(),marker.size());
    return file && marker==cache_marker;
}
void remove_cache(const std::filesystem::path& directory,const std::filesystem::path& parent) {
    if(safe_directory(directory).parent_path()!=parent || !owned_cache(directory))
        throw std::runtime_error("Refusing to remove an unrecognized music cache.");
    for(const auto& entry:std::filesystem::recursive_directory_iterator(directory)) {
        if(GetFileAttributesW(entry.path().c_str())&FILE_ATTRIBUTE_REPARSE_POINT)
            throw std::runtime_error("Music cache contains a link or junction.");
    }
    std::filesystem::remove_all(directory);
}
ComPtr<IMFMediaType> wma_type() {
    ComPtr<IMFCollection> types;
    checked(MFTranscodeGetAudioOutputAvailableTypes(MFAudioFormat_WMAudioV8,MFT_ENUM_FLAG_ALL,nullptr,&types),"Finding the Xbox-compatible WMA encoder");
    DWORD count=0;checked(types->GetElementCount(&count),"Reading WMA formats");
    ComPtr<IMFMediaType> selected;UINT32 smallest_block=UINT32_MAX;
    for(DWORD i=0;i<count;++i) {
        ComPtr<IUnknown> object;ComPtr<IMFMediaType> type;
        checked(types->GetElement(i,&object),"Reading a WMA format");checked(object.As(&type),"Reading WMA media type");
        UINT32 rate=0,channels=0,bytes=0,block=0;
        type->GetUINT32(MF_MT_AUDIO_SAMPLES_PER_SECOND,&rate);
        type->GetUINT32(MF_MT_AUDIO_NUM_CHANNELS,&channels);
        type->GetUINT32(MF_MT_AUDIO_AVG_BYTES_PER_SECOND,&bytes);
        type->GetUINT32(MF_MT_AUDIO_BLOCK_ALIGNMENT,&block);
        if(rate==44100 && channels==2 && block && (std::uint64_t(bytes)*8+500)/1000==128) {
            // Windows reports several layouts for nominal 128 kbps. Prefer
            // the shorter legacy blocks: the original Xbox reader truncates
            // the larger-block layout at an input-buffer boundary.
            if(block<smallest_block) {smallest_block=block;selected=type;}
        }
    }
    if(selected) return selected;
    throw std::runtime_error("Windows has no compatible 128 kbps, 44.1 kHz stereo WMA encoder.");
}
unsigned convert(const std::filesystem::path& source,const std::filesystem::path& destination,
    const std::function<bool()>& keep_running) {
    ComPtr<IMFSourceReader> reader;
    checked(MFCreateSourceReaderFromURL(source.c_str(),nullptr,&reader),"Opening "+utf8(source.filename().wstring()));
    checked(reader->SetStreamSelection(MF_SOURCE_READER_ALL_STREAMS,FALSE),"Selecting music streams");
    checked(reader->SetStreamSelection(MF_SOURCE_READER_FIRST_AUDIO_STREAM,TRUE),"Selecting the audio stream");
    ComPtr<IMFMediaType> pcm;checked(MFCreateMediaType(&pcm),"Creating the PCM format");
    checked(pcm->SetGUID(MF_MT_MAJOR_TYPE,MFMediaType_Audio),"Setting audio type");
    checked(pcm->SetGUID(MF_MT_SUBTYPE,MFAudioFormat_PCM),"Setting PCM type");
    checked(reader->SetCurrentMediaType(MF_SOURCE_READER_FIRST_AUDIO_STREAM,nullptr,pcm.Get()),"Decoding music to PCM");
    pcm.Reset();checked(reader->GetCurrentMediaType(MF_SOURCE_READER_FIRST_AUDIO_STREAM,&pcm),"Reading the decoded audio format");
    UINT32 rate=0,alignment=0;
    checked(pcm->GetUINT32(MF_MT_AUDIO_SAMPLES_PER_SECOND,&rate),"Reading the source sample rate");
    checked(pcm->GetUINT32(MF_MT_AUDIO_BLOCK_ALIGNMENT,&alignment),"Reading the source audio alignment");
    if(!rate || !alignment) throw std::runtime_error("Music decoder returned an invalid audio format.");
    ComPtr<IMFSinkWriter> writer;
    checked(MFCreateSinkWriterFromURL(destination.c_str(),nullptr,nullptr,&writer),"Creating the imported WMA file");
    const auto output=wma_type();DWORD stream=0;
    checked(writer->AddStream(output.Get(),&stream),"Selecting WMA output");
    checked(writer->SetInputMediaType(stream,pcm.Get(),nullptr),"Connecting the music converter");
    checked(writer->BeginWriting(),"Starting music conversion");
    std::uint64_t frames=0;
    while(true) {
        if(!keep_running()) throw std::runtime_error("Music import cancelled.");
        DWORD flags=0;LONGLONG timestamp=0;ComPtr<IMFSample> sample;
        checked(reader->ReadSample(MF_SOURCE_READER_FIRST_AUDIO_STREAM,0,nullptr,&flags,&timestamp,&sample),"Reading music");
        if(flags&(MF_SOURCE_READERF_ERROR|MF_SOURCE_READERF_CURRENTMEDIATYPECHANGED)) throw std::runtime_error("Music changed format while importing.");
        if(sample) {
            DWORD bytes=0;checked(sample->GetTotalLength(&bytes),"Reading the music packet length");
            if(bytes>1024*1024 || bytes%alignment) throw std::runtime_error("Music decoder returned an invalid PCM packet.");
            checked(sample->SetSampleTime(static_cast<LONGLONG>(frames*10000000/rate)),"Setting music packet time");
            checked(sample->SetSampleDuration(static_cast<LONGLONG>((frames+bytes/alignment)*10000000/rate-frames*10000000/rate)),"Setting music packet duration");
            frames+=bytes/alignment;
            if(frames/rate>UINT32_MAX/1000) throw std::runtime_error("Music exceeds the Xbox soundtrack duration limit.");
            checked(writer->WriteSample(stream,sample.Get()),"Encoding music");
        }
        if(flags&MF_SOURCE_READERF_ENDOFSTREAM) break;
    }
    if(!frames) throw std::runtime_error("Music file contains no audio.");
    checked(writer->Finalize(),"Finishing the imported WMA file");
    return static_cast<unsigned>((frames*1000+rate-1)/rate);
}
void name(std::vector<std::byte>& database,std::size_t offset,const std::wstring& source) {
    auto count=std::min<std::size_t>(source.size(),31);
    if(count && source[count-1]>=0xD800 && source[count-1]<=0xDBFF) --count;
    std::memcpy(database.data()+offset,source.data(),count*sizeof(wchar_t));
}
}
unsigned custom_music_count(const std::filesystem::path& directory) {
    if(!owned_cache(directory)) return 0;
    std::ifstream file(directory/"ST.DB",std::ios::binary);
    std::array<std::byte,1024> header{};file.read(reinterpret_cast<char*>(header.data()),header.size());
    if(!file || u32(header,0)!=1 || u32(header,4)!=1 || u32(header,512)!=0x21371) return 0;
    const auto count=u32(header,520);return count<=500?count:0;
}
unsigned import_custom_music(const std::filesystem::path& source,const std::filesystem::path& destination,
    const std::function<bool(unsigned,unsigned,std::string_view)>& progress) {
    const auto input=std::filesystem::canonical(source),output=safe_directory(destination),parent=output.parent_path();
    if(!std::filesystem::is_directory(input)) throw std::runtime_error("Choose a folder containing music files.");
    for(auto cursor=input;!cursor.empty();cursor=cursor.parent_path()) {
        if(cursor==output) throw std::runtime_error("Choose your original music folder, not the imported cache.");
        if(cursor==cursor.parent_path()) break;
    }
    std::vector<std::filesystem::path> files;
    for(const auto& entry:std::filesystem::directory_iterator(input)) {
        if(entry.is_regular_file() && music_file(entry.path())) {
            if(files.size()==500) throw std::runtime_error("A custom soundtrack can contain at most 500 songs.");
            files.push_back(entry.path());
        }
    }
    if(files.empty()) throw std::runtime_error("No supported music found. Choose MP3, WAV, WMA, FLAC, M4A or AAC files.");
    std::ranges::sort(files);
    if(std::filesystem::exists(output) && !owned_cache(output)) throw std::runtime_error("The music cache contains unrecognized files; choose another cache directory.");
    std::filesystem::create_directories(parent);
    GUID id{};checked(CoCreateGuid(&id),"Creating the import identifier");
    const auto suffix=std::format("{:08X}-{:04X}-{:04X}",id.Data1,id.Data2,id.Data3);
    const auto temporary=parent/(output.filename().wstring()+L"-import-"+utf8_path(suffix).wstring());
    const auto previous=parent/(output.filename().wstring()+L"-previous-"+utf8_path(suffix).wstring());
    if(std::filesystem::exists(temporary) || std::filesystem::exists(previous)) throw std::runtime_error("Music import directory already exists.");
    std::filesystem::create_directory(temporary);write_text(temporary/"cache.txt",cache_marker,false);
    try {
        Media media;
        const auto groups=(files.size()+5)/6;
        std::vector<std::byte> database(0xCA00+groups*512);
        const auto put=[&](std::size_t offset,std::uint32_t value){std::memcpy(database.data()+offset,&value,4);};
        put(0,1);put(4,1);put(8,2);put(12,1);
        put(512,0x21371);put(516,1);put(520,static_cast<unsigned>(files.size()));
        const auto collection=input.filename().wstring();
        name(database,512+352,collection.empty()?L"Custom music":collection);
        std::uint64_t total_time=0;
        for(unsigned i=0;i<files.size();++i) {
            const auto title=utf8(files[i].filename().wstring());
            const auto keep_running=[&]{return !progress || progress(i,static_cast<unsigned>(files.size()),title);};
            if(!keep_running()) throw std::runtime_error("Music import cancelled.");
            const auto group=i/6,slot=i%6,song=((group+1)<<16)|slot;
            const auto folder=temporary/std::format("{:04x}",group+1);std::filesystem::create_directories(folder);
            const auto duration=convert(files[i],folder/std::format("{:08x}.WMA",song),keep_running);
            const auto record=0xCA00+group*512;
            put(512+12+group*4,group);put(record,0x31073);put(record+4,1);put(record+8,group);
            put(record+16+slot*4,song);put(record+40+slot*4,duration);
            name(database,record+64+slot*64,files[i].stem().wstring());total_time+=duration;
        }
        if(total_time>UINT32_MAX) throw std::runtime_error("Custom soundtrack total duration exceeds the Xbox limit.");
        put(512+348,static_cast<unsigned>(total_time));
        put(412,((static_cast<unsigned>(files.size())/6+1)<<16)|(static_cast<unsigned>(files.size())%6));
        write_text(temporary/"ST.DB",std::string_view(reinterpret_cast<const char*>(database.data()),database.size()),false);
        if(progress && !progress(static_cast<unsigned>(files.size()),static_cast<unsigned>(files.size()),{})) throw std::runtime_error("Music import cancelled.");
        if(safe_directory(temporary).parent_path()!=parent || safe_directory(previous).parent_path()!=parent)
            throw std::runtime_error("Music import paths left their cache directory.");
        const bool existed=std::filesystem::exists(output);
        if(existed) std::filesystem::rename(output,previous);
        try {std::filesystem::rename(temporary,output);}
        catch(...) {if(existed) std::filesystem::rename(previous,output);throw;}
        if(existed) remove_cache(previous,parent);
        return static_cast<unsigned>(files.size());
    } catch(...) {
        if(std::filesystem::exists(temporary)) remove_cache(temporary,parent);
        throw;
    }
}
MusicImport::~MusicImport() {cancel();}
void MusicImport::start(const std::filesystem::path& source,const std::filesystem::path& destination) {
    if(worker_.joinable()) {worker_.request_stop();worker_.join();}
    {std::lock_guard lock(mutex_);progress_={};}
    worker_=std::jthread([this,source,destination](std::stop_token stop) {
        try {
            const auto count=import_custom_music(source,destination,[&](unsigned completed,unsigned total,std::string_view file) {
                if(stop.stop_requested()) return false;
                std::lock_guard lock(mutex_);progress_.completed=completed;progress_.total=total;
                if(progress_.file!=file) progress_.file=file;return true;
            });
            std::lock_guard lock(mutex_);progress_.completed=progress_.total=count;progress_.succeeded=true;
            progress_.message=std::format("Imported {} songs. Select your soundtrack in the game's Music Selection menu.",count);progress_.finished=true;
        } catch(const std::exception& error) {
            std::lock_guard lock(mutex_);progress_.message=error.what();progress_.finished=true;
        }
    });
}
void MusicImport::cancel() {if(worker_.joinable()) worker_.request_stop();}
MusicImportProgress MusicImport::status() const {std::lock_guard lock(mutex_);return progress_;}
}
