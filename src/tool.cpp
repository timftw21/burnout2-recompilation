#include "decoder.h"
#include "disc.h"
#include "recompiler.h"
#include "batch.h"
#include "render.h"
#include "assets.h"
#include "dsp_compiler.h"
#include "shaders.h"
#include <charconv>
#include <chrono>
#include <format>
#include <set>
#include <iostream>
#include <sstream>
#include <stdexcept>

namespace {
std::uint32_t number(std::wstring_view text) {
    auto value = b2::utf8(text);
    int base = 10;
    if (value.starts_with("0x") || value.starts_with("0X")) { base = 16; value.erase(0, 2); }
    std::uint32_t result;
    const auto converted = std::from_chars(value.data(), value.data() + value.size(), result, base);
    if (converted.ec != std::errc{} || converted.ptr != value.data() + value.size())
        throw std::runtime_error("Invalid unsigned number: " + value);
    return result;
}
constexpr auto usage =
    "Burnout 2 native tools (C++23)\n"
    "  b2-tool list ISO [DIRECTORY]\n"
    "  b2-tool extract ISO DISC_PATH OUTPUT\n"
    "  b2-tool inspect XBE\n"
    "  b2-tool verify XBE\n"
    "  b2-tool check-image XBE\n"
    "  b2-tool compile-effects IMAGE OUTPUT.cpp [--replace]\n"
    "  b2-tool compile-shaders XBE OUTPUT.cpp [--replace]\n"
    "  b2-tool data XBE ADDRESS BYTE_COUNT\n"
    "  b2-tool check-render OUTPUT_DIRECTORY [--warp] [--debug]\n"
    "  b2-tool render-texture FILE NV097_FORMAT WIDTH HEIGHT OUTPUT.png [--warp]\n"
    "  b2-tool inspect-textures DICTIONARY\n"
    "  b2-tool render-dictionary DICTIONARY OUTPUT_DIRECTORY [--warp]\n"
    "  b2-tool compare-images ACTUAL REFERENCE DIFFERENCE.png TOLERANCE\n"
    "  b2-tool decode XBE ADDRESS BYTE_COUNT\n"
    "  b2-tool analyse XBE ADDRESS INSTRUCTION_BUDGET\n"
    "  b2-tool recompile XBE ADDRESS INSTRUCTION_BUDGET OUTPUT.cpp [--replace]\n"
    "  b2-tool batch XBE SEEDS.txt FUNCTION_LIMIT INSTRUCTION_LIMIT OUTPUT.cpp [--replace] [--allow-partial] [--shards COUNT] [--report REPORT.json]\n"
    "Addresses accept decimal or 0x-prefixed hexadecimal.\n"
    "Decode is capped at 64 KiB; analysis at 4096 instructions.\n"
    "Batch seeds: address, entry, or table START END per line; # starts a comment.\n"
    "Batch caps: 65536 functions, 2097152 instructions, 256 MiB source; 1..512 optional shards.\n"
    "A partial batch exits 3 unless explicitly allowed; its report lists every blocker.\n";
}

int wmain(int argc, wchar_t** argv) {
    try {
        if (argc == 1 || (argc == 2 && std::wstring_view(argv[1]) == L"--help")) {
            std::cout << usage;
            return 0;
        }
        const auto command = std::wstring_view(argv[1]);
        const auto started = std::chrono::steady_clock::now();
        int result = 0;
        std::string output;
        if (command == L"compile-shaders" && (argc==4 || (argc==5 && std::wstring_view(argv[4])==L"--replace"))) {
            output=b2::compile_shaders(argv[2],argv[3],argc==5);
        } else if (command == L"compile-effects" && (argc==4 || (argc==5 && std::wstring_view(argv[4])==L"--replace"))) {
            output=b2::compile_effects(argv[2],argv[3],argc==5);
        } else if (command == L"check-render" && argc >= 3 && argc <= 5) {
            bool warp = false, debug = false;
            for (int i=3;i<argc;++i) {
                if (std::wstring_view(argv[i]) == L"--warp" && !warp) warp = true;
                else if (std::wstring_view(argv[i]) == L"--debug" && !debug) debug = true;
                else throw std::runtime_error("Unknown or duplicate rendering option");
            }
            output = b2::check_render(argv[2],warp,debug);
            if (output.find("\"passed\":false") != std::string::npos) result = 3;
        } else if (command == L"render-texture" && (argc == 7 || (argc == 8 && std::wstring_view(argv[7]) == L"--warp"))) {
            const std::filesystem::path destination(argv[6]);
            if (destination.extension() != L".png") throw std::runtime_error("Texture output must be a .png file");
            output = b2::render_texture(argv[2],static_cast<b2::TextureFormat>(number(argv[3])),number(argv[4]),number(argv[5]),destination,argc==8);
        } else if (command == L"inspect-textures" && argc == 3) {
            output = b2::TextureDictionary(argv[2]).report();
        } else if (command == L"render-dictionary" && (argc == 4 || (argc == 5 && std::wstring_view(argv[4]) == L"--warp"))) {
            output = b2::render_dictionary(argv[2],argv[3],argc==5);
            if (output.find("\"passed\":false") != std::string::npos) result = 3;
        } else if (command == L"compare-images" && argc == 6) {
            const std::filesystem::path destination(argv[4]);
            if (destination.extension() != L".png") throw std::runtime_error("Difference output must be a .png file");
            output = b2::compare_images(argv[2],argv[3],destination,number(argv[5]));
            if (output.find("\"passed\":false") != std::string::npos) result = 3;
        } else if (command == L"list" && (argc == 3 || argc == 4)) {
            b2::Disc disc(argv[2]);
            const auto entries = disc.list(argc == 4 ? b2::utf8(argv[3]) : "");
            output = "{\"format\":\"b2-disc-v1\",\"entries\":[";
            for (std::size_t i = 0; i < entries.size(); ++i) {
                const auto& entry = entries[i];
                if (i) output += ',';
                output += "{\"name\":" + b2::json(entry.name) + ",\"size\":" + std::to_string(entry.size) +
                          ",\"sector\":" + std::to_string(entry.sector) + ",\"directory\":" +
                          (entry.directory() ? "true" : "false") + '}';
            }
            output += "]}";
        } else if (command == L"extract" && argc == 5) {
            b2::Disc disc(argv[2]);
            const auto path = b2::utf8(argv[3]);
            const auto entry = disc.find(path);
            disc.extract(path, argv[4]);
            output = "{\"format\":\"b2-extract-v1\",\"file\":" + b2::json(path) +
                     ",\"bytes\":" + std::to_string(entry.size) + '}';
        } else if ((command == L"inspect" || command == L"verify" || command == L"check-image") && argc == 3) {
            b2::Xbe image(argv[2]);
            output = command == L"check-image" ? b2::check_image(image) : image.report();
            if (command == L"verify" && !image.supported()) result = 2;
        } else if (command == L"batch" && argc >= 7 && argc <= 13) {
            bool replace = false, allow_partial = false;
            std::uint32_t shard_count=0;
            std::filesystem::path report_path;
            for (int i = 7; i < argc; ++i) {
                const std::wstring_view option(argv[i]);
                if (option == L"--replace" && !replace) replace = true;
                else if (option == L"--allow-partial" && !allow_partial) allow_partial = true;
                else if (option == L"--report" && report_path.empty() && i + 1 < argc) report_path = argv[++i];
                else if (option == L"--shards" && !shard_count && i + 1 < argc) {
                    shard_count=number(argv[++i]);
                    if(!shard_count || shard_count>512) throw std::runtime_error("Shard count must be 1..512");
                }
                else throw std::runtime_error("Unknown or duplicate batch option");
            }
            b2::Xbe image(argv[2]);
            if (!image.supported()) { std::cout << image.report() << '\n'; return 2; }
            b2::File seed_file(argv[3]);
            if (seed_file.size() > 1024 * 1024) throw std::runtime_error("Seed file exceeds 1 MiB");
            const auto seed_bytes = seed_file.read(0, static_cast<std::size_t>(seed_file.size()));
            std::istringstream lines(std::string(reinterpret_cast<const char*>(seed_bytes.data()), seed_bytes.size()));
            std::vector<std::uint32_t> seeds;
            std::set<std::uint32_t> unique_seeds;
            const auto seed=[&](std::uint32_t address) {
                image.code_section(address);
                if(unique_seeds.insert(address).second) seeds.push_back(address);
                if(seeds.size()>65536) throw std::runtime_error("Seed file exceeds 65536 unique addresses");
            };
            std::string line;
            while (std::getline(lines, line)) {
                line = line.substr(0, line.find('#'));
                std::istringstream tokens(line);
                std::string value, extra;
                if (!(tokens >> value)) continue;
                if(value=="table") {
                    std::string first,last;
                    if(!(tokens>>first>>last) || (tokens>>extra)) throw std::runtime_error("Table seed needs START END addresses");
                    const auto start=number(std::wstring(first.begin(),first.end()));
                    const auto end=number(std::wstring(last.begin(),last.end()));
                    if(end<=start || (start&3U) || (end&3U) || end-start>128*1024)
                        throw std::runtime_error("Pointer table must be aligned and at most 128 KiB");
                    for(auto cursor=start;cursor<end;) {
                        const auto size=std::min<std::uint32_t>(4096,end-cursor);
                        const auto data=image.data(cursor,size);
                        for(std::uint32_t offset=0;offset<size;offset+=4) {
                            const auto target=b2::u32(data,offset);
                            if(target && target!=UINT32_MAX) seed(target);
                        }
                        cursor+=size;
                    }
                    continue;
                }
                if (tokens >> extra) throw std::runtime_error("Expected one address per seed line");
                seed(value == "entry" ? image.entry : number(std::wstring(value.begin(), value.end())));
            }
            const std::filesystem::path destination(argv[6]);
            auto assembly_path=destination; assembly_path+=L".asm";
            auto header_path=destination; header_path+=L".h";
            std::vector<std::filesystem::path> outputs{destination,assembly_path};
            std::vector<std::filesystem::path> shard_paths;
            if(shard_count) {
                outputs.push_back(header_path);
                for(std::uint32_t i=0;i<shard_count;++i) {
                    auto path=destination; path+=std::format(L".{:02}.cpp",i);
                    shard_paths.push_back(path); outputs.push_back(path);
                }
            }
            if (destination.extension() != L".cpp") throw std::runtime_error("Native output must be a .cpp file");
            for(const auto& path : outputs) for(const auto input : {argv[2],argv[3]})
                if(std::filesystem::exists(path) && std::filesystem::equivalent(path,input))
                    throw std::runtime_error("Native output cannot replace a batch input");
            if (!report_path.empty()) {
                if (report_path.extension() != L".json") throw std::runtime_error("Batch report must be a .json file");
                auto protected_paths=outputs; protected_paths.emplace_back(argv[2]); protected_paths.emplace_back(argv[3]);
                for (const auto& input : protected_paths)
                    if (std::filesystem::absolute(report_path).lexically_normal() == std::filesystem::absolute(input).lexically_normal() ||
                        (std::filesystem::exists(report_path) && std::filesystem::exists(input) && std::filesystem::equivalent(report_path, input)))
                        throw std::runtime_error("Batch report cannot replace another input or output");
            }
            auto batch = b2::recompile_batch(image, seeds, number(argv[4]), number(argv[5]),shard_count,
                shard_count?b2::utf8(header_path.filename().wstring()):std::string{});
            const bool publish = !batch.source.empty() && (batch.complete || allow_partial);
            if (publish) {
                for(const auto& path : outputs) if(!replace && std::filesystem::exists(path))
                    throw std::runtime_error("Native output already exists; use --replace explicitly");
                b2::write_text(assembly_path,batch.assembly,replace);
                if(shard_count) {
                    b2::write_text(header_path,batch.header,replace);
                    for(std::uint32_t i=0;i<shard_count;++i) b2::write_text(shard_paths[i],batch.shards[i],replace);
                }
                b2::write_text(destination, batch.source, replace);
            }
            batch.report.pop_back();
            std::size_t source_bytes=batch.source.size()+batch.assembly.size()+(shard_count?batch.header.size():0);
            for(const auto& piece : batch.shards) source_bytes+=piece.size();
            output = batch.report + ",\"source_bytes\":" + std::to_string(source_bytes) +
                     ",\"source_written\":" + (publish ? "true" : "false") + '}';
            if (!report_path.empty()) {
                output.pop_back();
                const auto elapsed = std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - started).count();
                b2::write_text(report_path, output + ",\"elapsed_ms\":" + std::to_string(elapsed) + '}'+ '\n', replace);
                output = "{\"format\":\"b2-batch-summary-v1\",\"complete\":" + std::string(batch.complete ? "true" : "false") +
                    ",\"functions_discovered\":" + std::to_string(batch.discovered) + ",\"functions_compiled\":" + std::to_string(batch.compiled) +
                    ",\"instructions_discovered\":" + std::to_string(batch.instructions) + ",\"report\":" + b2::json(b2::utf8(report_path.wstring())) + '}';
            }
            if (!batch.complete && !allow_partial) result = 3;
            if (batch.source.empty()) result = 3;
        } else if (((command == L"decode" || command == L"analyse" || command == L"data") && argc == 5) ||
                   (command == L"recompile" && (argc == 6 || (argc == 7 && std::wstring_view(argv[6]) == L"--replace")))) {
            b2::Xbe image(argv[2]);
            // Applying address-specific research requires the exact audited image.
            if (!image.supported()) {
                std::cout << image.report() << '\n';
                return 2;
            }
            const auto address = number(argv[3]);
            const auto limit = number(argv[4]);
            if (command == L"recompile") {
                const std::filesystem::path destination(argv[5]);
                if (destination.extension() != L".cpp") throw std::runtime_error("Native output must be a .cpp file");
                if (std::filesystem::exists(destination) && std::filesystem::equivalent(destination, argv[2]))
                    throw std::runtime_error("Native output cannot replace the input executable");
                const auto source = b2::recompile(image, address, limit);
                b2::write_text(destination, source, argc == 7);
                output = "{\"format\":\"b2-native-v1\",\"sha256\":" + b2::json(image.sha256) +
                         ",\"entry\":" + b2::json(b2::hex32(address)) + ",\"source_bytes\":" + std::to_string(source.size()) + '}';
            } else if (command == L"data") output = "{\"format\":\"b2-data-v1\",\"sha256\":" + b2::json(image.sha256) +
                ",\"address\":" + b2::json(b2::hex32(address)) + ",\"bytes\":" + b2::json(b2::hex_bytes(image.data(address,limit))) + '}';
            else output = command == L"decode" ? b2::disassemble(image, address, limit) : b2::analyse(image, address, limit);
        } else {
            throw std::runtime_error("Unknown command or incorrect arguments; use --help");
        }
        const auto elapsed = std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - started).count();
        output.pop_back();
        std::cout << output << ",\"elapsed_ms\":" << elapsed << "}\n";
        return result;
    } catch (const std::exception& error) {
        std::cout << "{\"error\":" << b2::json(error.what()) << "}\n";
        return 1;
    }
}
