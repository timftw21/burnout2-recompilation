#include "cpu.h"
#include "file.h"
#include "cpu_checks.h"
#include "xbe.h"
#include "xbox.h"
#include "audio.h"
#include "spatial.h"
#include "effects.h"
#include "dsp.h"
#include "app.h"
#include "input.h"
#include "diagnostics.h"
#include <algorithm>
#include <charconv>
#include <chrono>
#include <cmath>
#include <format>
#include <iostream>
#include <sstream>

extern "C" {
std::uint32_t b2_add8_flags(std::uint32_t, std::uint32_t);
std::uint32_t b2_add16_flags(std::uint32_t, std::uint32_t);
std::uint32_t b2_add32_flags(std::uint32_t, std::uint32_t);
std::uint32_t b2_compare8_flags(std::uint32_t, std::uint32_t);
std::uint32_t b2_compare16_flags(std::uint32_t, std::uint32_t);
std::uint32_t b2_compare32_flags(std::uint32_t, std::uint32_t);
std::uint32_t b2_and8_flags(std::uint32_t, std::uint32_t);
std::uint32_t b2_and16_flags(std::uint32_t, std::uint32_t);
std::uint32_t b2_and32_flags(std::uint32_t, std::uint32_t);
}

namespace {
const b2::CompiledFunction& compiled(std::uint32_t address) {
    const auto found = std::ranges::lower_bound(b2::compiled_batch, address, {}, &b2::CompiledFunction::address);
    if (found == b2::compiled_batch.end() || found->address != address)
        throw std::runtime_error("Required diagnostic function was not compiled: " + b2::hex32(address));
    return *found;
}
constexpr std::uint32_t stack_address = 0x1000, return_address = 0xABCDEF00;
constexpr std::uint64_t inputs[] = {
    0, 0x8000000000000000, 0x3FF0000000000000, 0xBFF0000000000000,
    0x7FEFFFFFFFFFFFFF, 0xFFEFFFFFFFFFFFFF, 0x0010000000000000, 1,
    0x8000000000000001, 0x7FF0000000000000, 0xFFF0000000000000,
    0x7FF8000000000000, 0x7FF0000000000001, 0xFFF8000000001234};

b2::Cpu initial_cpu() {
    b2::Cpu cpu;
    for (unsigned i = 0; i < cpu.registers.size(); ++i) cpu.registers[i] = 0x13570000U + i;
    cpu.registers[b2::esp] = stack_address;
    cpu.flags = 0x202;
    return cpu;
}

template<unsigned Bits> unsigned validate_flags(
    std::uint32_t (*add)(std::uint32_t, std::uint32_t),
    std::uint32_t (*compare)(std::uint32_t, std::uint32_t),
    std::uint32_t (*logical)(std::uint32_t, std::uint32_t)) {
    constexpr std::uint32_t edges[] = {0, 1, 15, 16, 0x7F, 0x80, 0xFF, 0x100,
        0x7FFF, 0x8000, 0xFFFF, 0x10000, 0x7FFFFFFF, 0x80000000, 0xFFFFFFFF};
    constexpr auto original_flags = 0x202 | b2::arithmetic_flags;
    unsigned count = 0;
    const auto check = [&](std::string_view operation, std::uint32_t model, std::uint32_t host,
                           std::uint32_t defined, std::uint32_t a, std::uint32_t b) {
        if ((model & defined) != (host & defined) || (model & ~defined) != (original_flags & ~defined))
            throw std::runtime_error(std::format("{}-bit {} flags differ from host CPU for {}, {}",
                Bits, operation, b2::hex32(a), b2::hex32(b)));
    };
    for (const auto a : edges) for (const auto b : edges) {
        check("add", b2::add_flags<Bits>(original_flags, a, b), add(a, b), b2::arithmetic_flags, a, b);
        check("compare", b2::sub_flags<Bits>(original_flags, a, b), compare(a, b), b2::arithmetic_flags, a, b);
        check("logical", b2::logic_flags<Bits>(original_flags, a & b), logical(a, b),
              b2::arithmetic_flags & ~b2::auxiliary, a, b);
        count += 3;
    }
    return count;
}

unsigned validate() {
    const auto& compiled_function = compiled(B2_FINITE_ADDRESS);
    for (const auto bits : inputs) {
        std::array<std::byte, 32> bytes{};
        b2::Memory memory(stack_address, bytes);
        memory.store<std::uint32_t>(stack_address, return_address);
        memory.store<std::uint64_t>(stack_address + 4, bits);
        const auto original_bytes = bytes;
        const auto original = initial_cpu();
        auto cpu = original;
        compiled_function.run(cpu, memory);
        const std::uint32_t finite = std::isfinite(std::bit_cast<double>(bits)) ? 1 : 0;
        const auto exponent_word = static_cast<std::uint16_t>((bits >> 48) & 0x7FF0);
        const auto expected_flags = (original.flags & ~b2::arithmetic_flags) |
            (b2_compare16_flags(exponent_word, 0x7FF0) & b2::arithmetic_flags);
        auto expected = original.registers;
        expected[b2::eax] = finite;
        expected[b2::ecx] = finite;
        expected[b2::esp] += 4;
        if (cpu.registers != expected || cpu.flags != expected_flags || cpu.eip != return_address || bytes != original_bytes)
            throw std::runtime_error(std::format("Native CPU mismatch for 0x{:016X}: eax {}, flags {} (expected {})",
                bits, cpu.registers[b2::eax], b2::hex32(cpu.flags), b2::hex32(expected_flags)));
    }
    return validate_flags<8>(b2_add8_flags, b2_compare8_flags, b2_and8_flags) +
           validate_flags<16>(b2_add16_flags, b2_compare16_flags, b2_and16_flags) +
           validate_flags<32>(b2_add32_flags, b2_compare32_flags, b2_and32_flags);
}

std::uint32_t iterations(std::string_view text,std::uint32_t limit=10000000) {
    std::uint32_t result;
    const auto parsed = std::from_chars(text.data(), text.data() + text.size(), result);
    if (parsed.ec != std::errc{} || parsed.ptr != text.data() + text.size() || !result || result > limit)
        throw std::runtime_error(std::format("Iterations must be 1..{}",limit));
    return result;
}
using b2::MemorySlice;
std::uint32_t diagnostic_number(std::string_view text) {
    const bool hexadecimal=text.starts_with("0x");if(hexadecimal) text.remove_prefix(2);
    std::uint32_t result=0;const auto parsed=std::from_chars(text.data(),text.data()+text.size(),result,hexadecimal?16:10);
    if(parsed.ec!=std::errc{} || parsed.ptr!=text.data()+text.size()) throw std::runtime_error("Invalid diagnostic number");
    return result;
}
MemorySlice memory_slice(std::string_view value) {
    const auto split=value.find(':');
    if(split==value.npos) throw std::runtime_error("Memory slice requires ADDRESS:BYTES");
    const MemorySlice slice{diagnostic_number(value.substr(0,split)),diagnostic_number(value.substr(split+1))};
    if(!slice.size || slice.size>4096 || slice.address>=64*1024*1024 || slice.size>64*1024*1024-slice.address)
        throw std::runtime_error("Memory slice must contain 1..4096 bytes within Xbox RAM");
    return slice;
}
std::string inspect_effects(const std::filesystem::path& executable,const std::filesystem::path& source,
                           const std::filesystem::path& output) {
    b2::Xbe image(executable);b2::File file(source);
    if(!image.supported()) throw std::runtime_error("Effects inspection requires the verified executable");
    if(file.size()<0x818 || file.size()>65536) throw std::runtime_error("Effects image must contain 0x818..65536 bytes");
    auto bytes=file.read(0,static_cast<std::size_t>(file.size()));
    const auto decoded=b2::decode_effects(bytes);
    const auto words=b2::u32(bytes,0x804),data_words=b2::u32(bytes,0x80C);
    const auto metadata=0x818ULL+4ULL*(words+static_cast<std::uint64_t>(data_words));
    if(words>4096 || data_words>4096 || metadata+8>bytes.size()) throw std::runtime_error("Invalid DSP image sections");
    const auto count=b2::u32(bytes,static_cast<std::size_t>(metadata));
    const auto descriptor_bytes=8ULL+32ULL*count,seed_offset=metadata+descriptor_bytes;
    if(!count || count>64 || seed_offset+8ULL*count>bytes.size()) throw std::runtime_error("Invalid DSP effect descriptors");
    if(std::filesystem::exists(output)) throw std::runtime_error("Effects output directory already exists");
    std::vector<std::byte> image_bytes(image.image_size),scratch(65536);
    image.load(image_bytes);b2::Memory memory(image.base,image_bytes);
    constexpr std::uint32_t data_address=0x01000000,scratch_address=0x01100000,stack=scratch_address+0xFFF0;
    memory.map(data_address,bytes);memory.map(scratch_address,scratch);
    b2::Cpu cpu;
    const auto call=[&](std::uint32_t entry,std::initializer_list<std::uint32_t> arguments) {
        cpu.registers[b2::esp]=stack-static_cast<std::uint32_t>(arguments.size()*4);
        const auto start=cpu.registers[b2::esp];memory.store<std::uint32_t>(start,return_address);
        unsigned index=0;for(auto value:arguments) memory.store<std::uint32_t>(start+4+index++*4,value);
        cpu.eip=entry;b2::invoke_native(cpu,memory,entry);
        if(cpu.eip!=return_address || cpu.registers[b2::esp]!=stack+4)
            throw std::runtime_error("Unexpected SDK effects-decoder return");
        if(entry==0x002363AA && cpu.registers[b2::eax]) throw std::runtime_error("SDK effect decoding failed");
    };
    call(0x0023637D,{scratch_address});
    const auto seeds=data_address+static_cast<std::uint32_t>(seed_offset);
    call(0x002363AA,{scratch_address,seeds,count*8,seeds,0});
    std::uint32_t code_offset=0x818;
    std::ostringstream report;
    report<<"{\"format\":\"b2-effects-inspection-v1\",\"source_sha256\":"<<b2::json(b2::hash(file,0,file.size()))
        <<",\"game_booted\":false,\"program_words\":"<<words<<",\"data_words\":"<<data_words<<",\"effects\":[";
    std::vector<std::pair<std::uint32_t,std::uint32_t>> programs;
    for(unsigned i=0;i<count;++i) {
        const auto descriptor=static_cast<std::size_t>(metadata)+8+i*32;
        const auto size=b2::u32(bytes,descriptor+4);
        if(!size || (size&3U) || code_offset+static_cast<std::uint64_t>(size)>0x818ULL+words*4ULL)
            throw std::runtime_error("DSP effect program exceeds its declared section");
        const auto address=data_address+code_offset;
        call(0x002363AA,{seeds+i*8,address,size,address,0});programs.emplace_back(code_offset,size);
        const auto& native=decoded.effects[i];
        if(native.file_offset!=code_offset || native.code.size()!=size ||
           !std::ranges::equal(native.code,std::span(bytes).subspan(code_offset,size)))
            throw std::runtime_error("Native effects decoder disagrees with the original SDK");
        if(i) report<<',';
        report<<"{\"index\":"<<i<<",\"file_offset\":"<<code_offset<<",\"code_bytes\":"<<size
            <<",\"descriptor\":"<<b2::json(b2::hex_bytes(std::span(bytes).subspan(descriptor,32)))
            <<",\"program\":"<<b2::json("effect-"+std::to_string(i)+".bin")<<'}';
        code_offset+=size;
    }
    report<<"],\"native_decoder_matches_sdk\":true}";std::filesystem::create_directories(output);
    for(unsigned i=0;i<programs.size();++i) {
        const auto [offset,size]=programs[i];std::ofstream stream(output/("effect-"+std::to_string(i)+".bin"),std::ios::binary);
        stream.write(reinterpret_cast<const char*>(bytes.data()+offset),size);
        if(!stream) throw std::runtime_error("Cannot save decoded DSP effect");
    }
    b2::write_text(output/"effects.json",report.str(),false);return report.str();
}
bool check_boot(const std::filesystem::path& path,std::uint32_t budget,const std::filesystem::path& disc,
                const std::filesystem::path& frame,std::span<const MemorySlice> slices,
                std::uint32_t break_address,std::uint32_t break_hit) {
    b2::Xbe image(path);
    if(!image.supported()) throw std::runtime_error("Boot diagnostics require the verified target executable");
    constexpr std::uint32_t memory_size=64*1024*1024;
    if(static_cast<std::uint64_t>(image.base)+image.image_size+image.stack_commit>memory_size)
        throw std::runtime_error("Executable and startup stack do not fit Xbox memory");
    std::vector<std::byte> bytes(memory_size);
    image.load(std::span(bytes).subspan(image.base,image.image_size));
    b2::Memory memory(0,bytes);
    b2::Xbox xbox(image,memory,bytes,disc);
    if(break_address) compiled(break_address);
    const auto boot=xbox.run(budget,break_address,break_hit);
    const bool frame_saved=!frame.empty() && xbox.save_frame(frame);
    const auto& diagnostic=boot.diagnostic;
    const auto& cpu=boot.cpu;
    std::ostringstream report;
    report << "{\"format\":\"b2-boot-check-v1\",\"source_sha256\":" << b2::json(image.sha256)
        << ",\"entry\":" << b2::json(b2::hex32(image.entry)) << ",\"entry_returned\":" << (boot.entry_returned?"true":"false")
        << ",\"main_reached\":" << (boot.main_reached?"true":"false") << ",\"threads_created\":" << boot.threads_created
        << ",\"active_thread\":" << boot.active_thread
        << ",\"vblank_callbacks\":" << boot.vblank_callbacks
        << ",\"graphics_device_created\":" << (boot.graphics_device_created?"true":"false")
        << ",\"graphics_initialized\":" << (boot.graphics_initialized?"true":"false")
        << ",\"audio_device_created\":" << (boot.audio_device_created?"true":"false") << ",\"audio_buffers\":" << boot.audio_buffers
        << ",\"audio_effects\":" << boot.audio_effects << ",\"audio_error\":" << b2::json(boot.audio_error)
        << ",\"graphics_adapter\":" << b2::json(boot.graphics_adapter)
        << ",\"frame_saved\":" << (frame_saved?"true":"false")
        << ",\"gpu\":{\"submissions\":" << boot.graphics.submissions << ",\"packets\":" << boot.graphics.packets
        << ",\"methods\":" << boot.graphics.methods << ",\"draws\":" << boot.graphics.draws
        << ",\"vertices\":" << boot.graphics.vertices << ",\"clears\":" << boot.graphics.clears
        << ",\"flips\":" << boot.graphics.flips << ",\"fences\":" << boot.graphics.fences << '}'
        << ",\"compiled_functions\":" << b2::compiled_batch.size() << ",\"visits\":" << diagnostic.visits
        << ",\"boundary\":" << b2::json(boot.boundary) << ",\"eip\":" << b2::json(b2::hex32(cpu.eip))
        << ",\"flags\":" << b2::json(b2::hex32(cpu.flags)) << ",\"registers\":[";
    for(unsigned i=0;i<cpu.registers.size();++i) { if(i) report << ','; report << b2::json(b2::hex32(cpu.registers[i])); }
    report << "],\"tls_index\":";
    if(image.tls) report << std::bit_cast<std::int32_t>(memory.load<std::uint32_t>(image.tls->index_address));
    else report << "null";
    report << ",\"stack_words\":[";
    for(unsigned i=0;i<boot.stack_word_count;++i) {if(i) report<<',';report<<b2::json(b2::hex32(boot.stack_words[i]));}
    report << "],\"disc_mounted\":" << (disc.empty()?"false":"true") << ",\"history\":[";
    const auto count=std::min<std::uint64_t>(diagnostic.visits,diagnostic.history.size());
    for(std::uint64_t i=0;i<count;++i) {
        if(i) report << ',';
        report << b2::json(b2::hex32(diagnostic.history[(diagnostic.visits-count+i)%diagnostic.history.size()]));
    }
    report << "],\"threads\":[";
    for(unsigned i=0;i<boot.threads.size();++i) {
        if(i) report<<',';const auto& thread=boot.threads[i];
        report<<"{\"id\":"<<thread.id<<",\"routine\":"<<b2::json(b2::hex32(thread.routine))
            <<",\"system\":"<<b2::json(b2::hex32(thread.system))<<",\"eip\":"<<b2::json(b2::hex32(thread.eip))
            <<",\"stack\":"<<b2::json(b2::hex32(thread.stack))<<",\"wait_object\":"<<b2::json(b2::hex32(thread.wait_object))
            <<",\"state\":"<<b2::json(thread.state)<<",\"suspended\":"<<(thread.suspended?"true":"false")<<'}';
    }
    report << "],\"service_calls\":[";
    bool first=true;
    for(unsigned ordinal=1;ordinal<boot.service_calls.size();++ordinal) if(boot.service_calls[ordinal]) {
        if(!first) report << ',';
        first=false;
        report << "{\"ordinal\":" << ordinal << ",\"calls\":" << boot.service_calls[ordinal] << '}';
    }
    report<<"],\"io\":[";
    for(unsigned i=0;i<boot.io.size();++i) {
        if(i) report<<',';const auto& io=boot.io[i];
        report<<"{\"ordinal\":"<<io.ordinal<<",\"status\":"<<b2::json(b2::hex32(io.status))<<",\"information\":"<<io.information
            <<",\"callsite\":"<<b2::json(b2::hex32(io.callsite))<<",\"path\":"<<b2::json(io.path)
            <<",\"options\":"<<io.options<<",\"requested\":"<<io.requested<<",\"offset\":"<<io.offset
            <<",\"buffer\":"<<b2::json(b2::hex32(io.buffer))<<'}';
    }
    report<<"],\"allocations\":[";
    for(unsigned i=0;i<boot.allocations.size();++i) {
        if(i) report<<',';const auto& block=boot.allocations[i];
        report<<"{\"address\":"<<b2::json(b2::hex32(block.address))<<",\"size\":"<<block.size
            <<",\"committed\":"<<block.committed<<",\"protect\":"<<block.protect<<'}';
    }
    report<<"],\"last_failed_allocation\":[";
    for(unsigned i=0;i<boot.last_failed_allocation.size();++i) {if(i) report<<',';report<<boot.last_failed_allocation[i];}
    report<<"],\"memory\":[";
    for(unsigned i=0;i<slices.size();++i) {
        if(i) report<<',';const auto& slice=slices[i];
        report<<"{\"address\":"<<b2::json(b2::hex32(slice.address))<<",\"bytes\":"
            <<b2::json(b2::hex_bytes(std::span(bytes).subspan(slice.address,slice.size)))<<'}';
    }
    std::cout << report.str() << "]}\n";
    return boot.entry_returned && boot.main_reached && boot.boundary.empty();
}
}

int main(int argc, char** argv) {
    b2::initialize_diagnostics();
    try {
        if(argc==1) return b2::run_app();
        if (argc == 2 && std::string_view(argv[1]) == "--help") {
            std::cout << "Burnout 2 native CPU diagnostics\n"
                         "  b2 --check-cpu [XBE]\n  b2 --check-input\n  b2 --bench-cpu [ITERATIONS]\n"
                         "  b2 --check-boot XBE [VISIT_BUDGET] [--disc ISO] [--frame PNG] [--memory ADDRESS:BYTES] [--break ADDRESS[:HIT]]\n"
                         "  b2 --run-game XBE [--disc ISO] [--seconds N] [--frame PNG] [--controls SCRIPT] [--memory ADDRESS:BYTES] [--trace-io]\n"
                         "  b2 --check-window PNG [--warp]\n  b2 --check-audio [EFFECTS_IMAGE OUTPUT_DIRECTORY]\n"
                         "  b2 --check-effects IMAGE\n  b2 --check-spatial XBE [OUTPUT_DIRECTORY]\n"
                         "  b2 --inspect-effects XBE IMAGE OUTPUT_DIRECTORY\n"
                         "  b2 --check-storage XBE OUTPUT_DIRECTORY\n"
                         "  b2 --check-replays XBE ISO OUTPUT_DIRECTORY\n"
                         "  b2 --inspect-audio CAPTURE.bin EFFECTS_IMAGE OUTPUT_DIRECTORY\n";
            return 0;
        }
        const auto command = std::string_view(argv[1]);
        if(command=="--check-storage" && argc==4) {std::cout<<b2::check_storage(argv[2],argv[3])<<'\n';return 0;}
        if(command=="--check-replays" && argc==5) {std::cout<<b2::check_replays(argv[2],argv[3],argv[4])<<'\n';return 0;}
        if(command=="--inspect-audio" && argc==5) {
            const auto report=b2::Audio::inspect_capture(argv[2],argv[3],argv[4]);std::cout<<report<<'\n';
            return report.find("\"passed\":true")!=std::string::npos?0:1;
        }
        if(command=="--inspect-effects" && argc==5) {std::cout<<inspect_effects(argv[2],argv[3],argv[4])<<'\n';return 0;}
        if(command=="--check-audio" && argc==2) {std::cout<<b2::check_audio()<<'\n';return 0;}
        if(command=="--check-audio" && argc==4) {std::cout<<b2::check_audio(argv[2],argv[3])<<'\n';return 0;}
        if(command=="--check-spatial" && argc==3) {std::cout<<b2::check_spatial(argv[2])<<'\n';return 0;}
        if(command=="--check-spatial" && argc==4) {std::cout<<b2::check_spatial(argv[2],argv[3])<<'\n';return 0;}
        if(command=="--check-effects" && argc==3) {const auto report=b2::check_effects(argv[2]);std::cout<<report<<'\n';
            return report.find("\"passed\":false")==std::string::npos?0:3;}
        if(command=="--check-window" && (argc==3 || (argc==4 && std::string_view(argv[3])=="--warp"))) {
            std::cout<<b2::check_window(argv[2],argc==4)<<'\n';return 0;
        }
        if(command=="--run-game" && argc>=3) {
            std::filesystem::path disc,frame,controls;unsigned seconds=0;std::vector<MemorySlice> slices;bool trace_io=false;
            for(int index=3;index<argc;++index) {
                const auto option=std::string_view(argv[index]);
                if(option=="--trace-io") {trace_io=true;continue;}
                if(index+1>=argc) throw std::runtime_error("Missing live-run option value");
                if(option=="--disc") disc=argv[++index];
                else if(option=="--frame") frame=argv[++index];
                else if(option=="--seconds") seconds=iterations(argv[++index]);
                else if(option=="--controls") controls=argv[++index];
                else if(option=="--memory") {
                    if(slices.size()==4) throw std::runtime_error("At most four memory slices are supported");
                    slices.push_back(memory_slice(argv[++index]));
                }
                else throw std::runtime_error("Unknown live-run option");
            }
            return b2::run_app(argv[2],disc,seconds,frame,controls,slices,trace_io);
        }
        if(command=="--check-input" && argc==2) {std::cout<<b2::check_input()<<'\n';return 0;}
        if(command=="--check-boot" && argc>=3) {
            std::uint32_t budget=1000000,break_address=0,break_hit=1;
            std::filesystem::path disc,frame;std::vector<MemorySlice> slices;int index=3;
            if(index<argc && !std::string_view(argv[index]).starts_with("--")) budget=iterations(argv[index++],200000000);
            while(index<argc) {
                const auto option=std::string_view(argv[index++]);
                if(index>=argc) throw std::runtime_error("Missing boot option value");
                if(option=="--disc") disc=argv[index++];
                else if(option=="--frame") frame=argv[index++];
                else if(option=="--memory") {
                    if(slices.size()==4) throw std::runtime_error("At most four memory slices are supported");
                    slices.push_back(memory_slice(argv[index++]));
                }
                else if(option=="--break") {
                    const auto value=std::string_view(argv[index++]);const auto split=value.find(':');
                    break_address=diagnostic_number(value.substr(0,split));
                    break_hit=split==value.npos?1:diagnostic_number(value.substr(split+1));
                    if(!break_address || !break_hit) throw std::runtime_error("Breakpoint address and hit count must be positive");
                }
                else throw std::runtime_error("Invalid boot arguments; use --help");
            }
            return check_boot(argv[2],budget,disc,frame,slices,break_address,break_hit)?0:3;
        }
        if ((command != "--check-cpu" || (argc != 2 && argc != 3)) &&
            (command != "--bench-cpu" || (argc != 2 && argc != 3)))
            throw std::runtime_error("Unknown command or incorrect arguments; use --help");
        const auto flag_cases = validate();
        auto additional = b2::check_cpu_batch();
        if(command=="--check-cpu" && argc==3) additional.memory_cases+=b2::check_cpu_image(argv[2]);
        const auto& compiled_function = compiled(B2_FINITE_ADDRESS);
        const auto prefix = "{\"source_sha256\":" + b2::json(compiled_function.source_sha256) +
                            ",\"guest_address\":" + b2::json(b2::hex32(compiled_function.address));
        if (command == "--check-cpu") {
            std::cout << prefix << ",\"format\":\"b2-cpu-check-v2\",\"passed_cases\":" << std::size(inputs) + additional.game_cases
                      << ",\"instruction_cases\":" << flag_cases + additional.instruction_cases
                      << ",\"memory_cases\":" << additional.memory_cases
                      << ",\"floating_cases\":" << additional.floating_cases
                      << ",\"control_cases\":" << additional.control_cases
                      << ",\"compiled_functions\":" << b2::compiled_batch.size() << "}\n";
            return 0;
        }
        const auto count = argc == 3 ? iterations(argv[2]) : 1000000;
        std::array<std::byte, 32> bytes{};
        b2::Memory memory(stack_address, bytes);
        auto cpu = initial_cpu();
        memory.store<std::uint32_t>(stack_address, return_address);
        std::uint64_t checksum = 0;
        const auto run = [&](std::uint32_t calls) {
            for (std::uint32_t i = 0; i < calls; ++i) {
                cpu.registers[b2::esp] = stack_address;
                memory.store<std::uint64_t>(stack_address + 4, inputs[i % std::size(inputs)]);
                compiled_function.run(cpu, memory);
                checksum += cpu.registers[b2::eax];
            }
        };
        run(10000);
        checksum = 0;
        const auto start = std::chrono::steady_clock::now();
        run(count);
        const auto ns = std::chrono::duration<double, std::nano>(std::chrono::steady_clock::now() - start).count();
        const auto chain = b2::benchmark_call_chain(count);
        const auto matrix=b2::benchmark_matrix(count);
        std::cout << prefix << ",\"format\":\"b2-cpu-bench-v2\",\"calls\":" << count
                  << ",\"ns_per_call\":" << ns / count << ",\"elapsed_ms\":" << ns / 1000000
                  << ",\"checksum\":" << checksum << ",\"includes_input_setup\":true,\"call_chain\":{\"guest_address\":\"0x00012BC0\",\"calls\":"
                  << count << ",\"ns_per_call\":" << chain.ns_per_call << ",\"checksum\":" << chain.checksum
                  << ",\"includes_input_setup\":true},\"matrix\":{\"guest_address\":\"0x00227BD9\",\"calls\":" << count
                  << ",\"fused_ns_per_call\":" << matrix.fused.ns_per_call << ",\"unfused_ns_per_call\":" << matrix.unfused.ns_per_call
                  << ",\"fused_checksum\":" << matrix.fused.checksum << ",\"unfused_checksum\":" << matrix.unfused.checksum
                  << ",\"includes_input_setup\":true,\"game_booted\":false},\"compiled_functions\":" << b2::compiled_batch.size() << "}\n";
        return 0;
    } catch (const std::exception& error) {
        b2::diagnostic_record("{\"type\":\"error\",\"message\":"+b2::json(error.what())+'}');
        std::cout << "{\"error\":" << b2::json(error.what()) << "}\n";
        return 1;
    }
}
