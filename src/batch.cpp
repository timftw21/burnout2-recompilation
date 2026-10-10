#include "batch.h"
#include <algorithm>
#include <deque>
#include <format>
#include <set>
#include <sstream>
#include <stdexcept>

namespace b2 {
namespace {
struct Unit {
    std::vector<std::uint32_t> calls;
    std::vector<std::uint32_t> kernel;
    std::vector<LoweringIssue> issues;
    std::string source, error,native_api;
    std::map<std::string,std::string> native_helpers;
    std::size_t instruction_count = 0;
    bool truncated = false, unresolved = false, ready = false, emit = false;
    std::vector<std::uint32_t> indirect;
    std::vector<std::uint32_t> static_callbacks;
    std::vector<std::uint32_t> indirect_slots;
    std::vector<std::pair<std::uint32_t,std::uint32_t>> callback_stores;
    bool floating = false;
    bool clock = false, traps = false;
};
constexpr std::uint32_t total_instruction_limit = 2097152;
constexpr std::size_t source_limit = 256 * 1024 * 1024;
std::string symbol(std::uint32_t address) { return std::format("guest_{:08X}", address); }
void addresses(std::ostream& out, std::span<const std::uint32_t> values) {
    out << '[';
    for (std::size_t i = 0; i < values.size(); ++i) {
        if (i) out << ',';
        out << json(hex32(values[i]));
    }
    out << ']';
}
}

Batch recompile_batch(Xbe& image, std::span<const std::uint32_t> seeds,
                      std::uint32_t function_limit, std::uint32_t instruction_limit,
                      std::uint32_t shard_count, std::string_view header_name) {
    if (!image.supported()) throw std::runtime_error("Batch generation requires the verified target executable");
    if (seeds.empty() || seeds.size() > 65536) throw std::runtime_error("A batch needs 1..65536 seed addresses");
    if (!function_limit || function_limit > 65536) throw std::runtime_error("Function limit must be 1..65536");
    if (!instruction_limit || instruction_limit > 4096) throw std::runtime_error("Instruction limit must be 1..4096 per function");
    if(shard_count>512 || (shard_count && header_name.empty())) throw std::runtime_error("Sharding needs 1..512 files and a header name");
    Decoder decoder;
    std::map<std::uint32_t, Unit> units;
    std::set<std::uint32_t> scheduled;
    std::deque<std::uint32_t> pending;
    std::set<std::uint32_t> called_slots;
    std::map<std::uint32_t,std::set<std::uint32_t>> installed_callbacks;
    const auto schedule = [&](std::uint32_t address) {
        if (scheduled.insert(address).second) pending.push_back(address);
    };
    for (const auto seed : seeds) schedule(seed);
    std::uint32_t instruction_count = 0;
    std::size_t generated_bytes = 0;
    std::map<std::string, unsigned> unsupported;
    while (!pending.empty() && units.size() < function_limit && instruction_count < total_instruction_limit) {
        const auto address = pending.front();
        pending.pop_front();
        Unit unit;
        const auto decoded_before = decoder.decoded_count();
        try {
            const auto function = recover(image, address,
                std::min(instruction_limit, total_instruction_limit - instruction_count), decoder);
            unit.truncated = function.budget_exhausted;
            unit.unresolved = function.unresolved;
            unit.static_callbacks=function.static_callbacks;
            for(auto callback:unit.static_callbacks) schedule(callback);
            unit.indirect_slots=function.indirect_slots;
            unit.callback_stores=function.callback_stores;
            for(const auto& [slot,callback]:unit.callback_stores) {
                installed_callbacks[slot].insert(callback);
                if(called_slots.contains(slot)) schedule(callback);
            }
            for(auto slot:unit.indirect_slots) {
                called_slots.insert(slot);
                if(installed_callbacks.contains(slot)) for(auto callback:installed_callbacks.at(slot)) schedule(callback);
            }
            for (const auto& edge : function.edges) if (edge.kind == "call" && edge.to) {
                unit.calls.push_back(*edge.to);
                schedule(*edge.to);
            }
            for(const auto& edge : function.edges)
                if((edge.kind=="call" || edge.kind=="jump") && !edge.to) unit.indirect.push_back(edge.from);
            for (const auto& edge : function.edges) if (edge.kind.starts_with("kernel-") && edge.to) unit.kernel.push_back(*edge.to);
            std::ranges::sort(unit.kernel);
            unit.kernel.erase(std::unique(unit.kernel.begin(), unit.kernel.end()), unit.kernel.end());
            for (const auto& [pc, instruction] : function.instructions) {
                unit.clock |= instruction.decoded.mnemonic == ZYDIS_MNEMONIC_RDTSC;
                unit.traps |= instruction.decoded.mnemonic == ZYDIS_MNEMONIC_INT3 || instruction.decoded.mnemonic == ZYDIS_MNEMONIC_INT;
            }
            std::ranges::sort(unit.calls);
            unit.calls.erase(std::unique(unit.calls.begin(), unit.calls.end()), unit.calls.end());
            auto lowered = lower(function);
            unit.source = std::move(lowered.source);
            // A small unfused reference for the matrix routine sampled in races.
            // Offline diagnostics compare identical original instructions, not a
            // replacement mathematical formula or a different precision model.
            if(function.entry==0x00227BD9) {
                auto reference=lower(function,false);
                const auto name=reference.source.find("void guest_00227BD9(");
                if(name==std::string::npos || !reference.issues.empty()) throw std::runtime_error("Matrix reference lowering failed");
                reference.source.replace(name,std::string_view("void guest_00227BD9(").size(),"void reference_matrix(");
                unit.source+=reference.source;
            }
            unit.native_helpers=std::move(lowered.native_helpers);
            unit.floating=lowered.floating;
            unit.native_api=std::move(lowered.native_api);
            if(!unit.native_api.empty()) {
                unit.unresolved=false;unit.calls.clear();unit.indirect.clear();unit.kernel.clear();unit.clock=unit.traps=false;
            }
            unit.issues = std::move(lowered.issues);
            for (const auto& issue : unit.issues)
                ++unsupported[ZydisMnemonicGetString(function.instructions.at(issue.address).decoded.mnemonic)];
            unit.emit = !unit.truncated && unit.issues.empty();
            unit.ready = unit.emit && !unit.unresolved;
            generated_bytes += unit.source.size();
        } catch (const std::exception& error) { unit.error = error.what(); unit.ready = false; }
        // Failed recovery still consumes the global work budget.
        unit.instruction_count = static_cast<std::size_t>(decoder.decoded_count() - decoded_before);
        instruction_count += static_cast<std::uint32_t>(unit.instruction_count);
        if (generated_bytes > source_limit) throw std::runtime_error("Generated batch exceeds the 256 MiB source limit");
        units.emplace(address, std::move(unit));
    }
    // Keep proven closed coverage separate from executable partial coverage.
    // Missing dependencies are explicit diagnostic stops, never interpreted.
    bool changed;
    do {
        changed = false;
        for (auto& [address, unit] : units) if (unit.ready) {
            for (const auto target : unit.calls) {
                const auto found = units.find(target);
                if (found == units.end() || !found->second.ready) {
                    unit.ready = false;
                    changed = true;
                    break;
                }
            }
        }
    } while (changed);
    // Only external entry wrappers switch floating state; direct native calls
    // share the resident x87 stack and the guest MXCSR.
    do {
        changed=false;
        for(auto& [address,unit] : units) if(unit.emit && !unit.floating)
            for(const auto target : unit.calls) if(units.contains(target) && units.at(target).emit && units.at(target).floating) {
                unit.floating=true; changed=true; break;
            }
    } while(changed);

    Batch result;
    result.complete = pending.empty() && std::ranges::all_of(units, [](const auto& pair) { return pair.second.ready; });
    const auto ready_count = std::ranges::count_if(units, [](const auto& pair) { return pair.second.ready; });
    const auto emitted_count = std::ranges::count_if(units, [](const auto& pair) { return pair.second.emit; });
    result.discovered = units.size();
    result.compiled = static_cast<std::size_t>(emitted_count);
    result.instructions = instruction_count;
    std::size_t ready_instructions = 0;
    std::set<std::uint32_t> required_kernel;
    bool needs_clock = false, needs_exceptions = false;
    for (const auto& [address, unit] : units) if (unit.emit) {
        required_kernel.insert(unit.kernel.begin(), unit.kernel.end());
        needs_clock |= unit.clock;
        needs_exceptions |= unit.traps;
    }
    std::set<std::uint32_t> missing;
    for(const auto& [address,unit] : units) if(unit.emit) for(const auto target : unit.calls)
        if(!units.contains(target) || !units.at(target).emit) missing.insert(target);
    if (emitted_count) {
        std::ostringstream source, declarations;
        std::map<std::string,std::string> helpers;
        source << "// Generated from XBE SHA256 " << image.sha256 << "\n#include \"cpu.h\"\nnamespace b2 {\n";
        declarations << "#pragma once\n#include \"cpu.h\"\nnamespace b2 {\n";
        for(const auto& [address,unit] : units) if(unit.emit) {
            declarations << "void " << symbol(address) << "(Cpu&,Memory&);\n";
            if(unit.floating) declarations << "void entry_" << std::format("{:08X}",address) << "(Cpu&,Memory&);\n";
        }
        for(const auto address : missing) declarations << "void " << symbol(address) << "(Cpu&,Memory&);\n";
        declarations << "}\n";
        result.header=declarations.str();
        if(!shard_count) {
            const auto start=result.header.find("namespace b2 {")+15;
            source << result.header.substr(start,result.header.size()-start-2);
        }
        std::vector<std::ostringstream> pieces(shard_count);
        for(auto& piece : pieces) piece << "// Generated from XBE SHA256 " << image.sha256
            << "\n#include \"cpu.h\"\nnamespace b2 {\n";
        const auto shard_for=[&](std::uint32_t address) { return ((address>>4)^(address>>13)^(address>>22))%shard_count; };
        if(shard_count) {
            std::vector<std::set<std::uint32_t>> dependencies(shard_count);
            for(const auto& [address,unit] : units) if(unit.emit)
                dependencies[shard_for(address)].insert(unit.calls.begin(),unit.calls.end());
            for(std::uint32_t i=0;i<shard_count;++i) for(const auto address : dependencies[i])
                pieces[i] << "void " << symbol(address) << "(Cpu&,Memory&);\n";
        }
        for (const auto& [address, unit] : units) if (unit.emit) {
            auto& destination=shard_count?pieces[shard_for(address)]:source;
            destination << unit.source;
            helpers.insert(unit.native_helpers.begin(),unit.native_helpers.end());
            if(unit.floating) destination << "void entry_" << std::format("{:08X}",address)
                << "(Cpu& cpu,Memory& memory) { FloatingScope scope(cpu.floating); " << symbol(address) << "(cpu,memory); }\n";
            ready_instructions += unit.instruction_count;
        }
        for(const auto address : missing) {
            source << "void " << symbol(address) << "(Cpu& cpu,[[maybe_unused]] Memory& memory) { cpu.eip="
                << std::format("0x{:08X}U",address) << "; throw NativeBoundary(cpu.eip,\"direct callee was not compiled\"); }\n";
        }
        if(shard_count) for(auto& piece : pieces) { piece << "}\n"; result.shards.push_back(piece.str()); }
        if(shard_count) for(const auto& [address,unit] : units) if(unit.emit)
            source << "void " << (unit.floating?std::format("entry_{:08X}",address):symbol(address)) << "(Cpu&,Memory&);\n";
        source << "namespace {\nconst std::array<CompiledFunction, " << emitted_count << "> functions{{\n";
        for (const auto& [address, unit] : units) if (unit.emit)
            source << "    {" << std::format("0x{:08X}U", address) << ", \"" << image.sha256 << "\", "
                   << (unit.floating?std::format("entry_{:08X}",address):symbol(address)) << "},\n";
        source << "}};\n}\nconst std::span<const CompiledFunction> compiled_batch{functions};\n}\n";
        result.source = source.str();
        result.assembly="; Native instruction helpers generated from verified XBE "+image.sha256+"\noption casemap:none\n.code\n";
        for(const auto& [name,assembly] : helpers) result.assembly+=assembly;
        result.assembly+="END\n";
        std::size_t total=result.source.size()+result.header.size()+result.assembly.size();
        for(const auto& piece : result.shards) total+=piece.size();
        if (total > source_limit) throw std::runtime_error("Generated batch exceeds the 256 MiB source limit");
    }
    std::ostringstream report;
    report << "{\"format\":\"b2-batch-v1\",\"sha256\":" << json(image.sha256) << ",\"seeds\":";
    addresses(report, seeds);
    report << ",\"complete\":" << (result.complete ? "true" : "false")
           << ",\"function_limit\":" << function_limit << ",\"instruction_limit_per_function\":" << instruction_limit
           << ",\"total_instruction_limit\":" << total_instruction_limit << ",\"functions_discovered\":" << units.size()
           << ",\"instructions_discovered\":" << instruction_count << ",\"functions_compiled\":" << emitted_count
           << ",\"closed_functions\":" << ready_count << ",\"shard_count\":" << shard_count
           << ",\"instructions_compiled\":" << ready_instructions << ",\"pending_functions\":[";
    bool first = true;
    for (const auto address : pending) {
        if (!first) report << ',';
        first = false;
        report << json(hex32(address));
    }
    report << "],\"missing_direct_functions\":[";
    first=true;
    for(const auto address : missing) { if(!first) report << ','; first=false; report << json(hex32(address)); }
    report << "],\"unsupported_instructions\":{";
    first = true;
    for (const auto& [mnemonic, count] : unsupported) {
        if (!first) report << ',';
        first = false;
        report << json(mnemonic) << ':' << count;
    }
    report << "},\"runtime_requirements\":{\"timestamp_counter\":" << (needs_clock ? "true" : "false")
        << ",\"exception_dispatch\":" << (needs_exceptions ? "true" : "false") << ",\"kernel_ordinals\":[";
    first = true;
    for (const auto ordinal : required_kernel) {
        if (!first) report << ',';
        first = false;
        const auto import = std::ranges::find(image.imports, ordinal, &KernelImport::ordinal);
        report << "{\"ordinal\":" << ordinal << ",\"name\":" << json(import->name) << '}';
    }
    report << "]},\"functions\":[";
    first = true;
    for (const auto& [address, unit] : units) {
        if (!first) report << ',';
        first = false;
        report << "{\"entry\":" << json(hex32(address)) << ",\"instruction_count\":" << unit.instruction_count
               << ",\"compiled\":" << (unit.emit ? "true" : "false") << ",\"closed\":" << (unit.ready?"true":"false")
               << ",\"floating_point\":" << (unit.floating?"true":"false")
               << ",\"budget_exhausted\":" << (unit.truncated ? "true" : "false")
               << ",\"unresolved_control_flow\":" << (unit.unresolved ? "true" : "false") << ",\"calls\":";
        addresses(report, unit.calls);
        report << ",\"unresolved_indirect_sites\":";
        addresses(report,unit.indirect);
        report << ",\"static_callback_targets\":";
        addresses(report,unit.static_callbacks);
        report << ",\"indirect_pointer_slots\":";
        addresses(report,unit.indirect_slots);
        report << ",\"callback_installations\":[";
        for(std::size_t i=0;i<unit.callback_stores.size();++i) {
            if(i) report<<',';
            report << "{\"slot\":" << json(hex32(unit.callback_stores[i].first)) << ",\"target\":" << json(hex32(unit.callback_stores[i].second)) << '}';
        }
        report << ']';
        report << ",\"kernel_ordinals\":[";
        for (std::size_t i = 0; i < unit.kernel.size(); ++i) {
            if (i) report << ',';
            report << unit.kernel[i];
        }
        report << ']';
        report << ",\"timestamp_counter\":" << (unit.clock ? "true" : "false")
            << ",\"exception_dispatch\":" << (unit.traps ? "true" : "false");
        report << ",\"blocked_by\":[";
        bool first_blocked = true;
        for (const auto target : unit.calls) {
            const auto found = units.find(target);
            if (found != units.end() && found->second.ready) continue;
            if (!first_blocked) report << ',';
            first_blocked = false;
            report << json(hex32(target));
        }
        report << "],\"issues\":[";
        for (std::size_t i = 0; i < unit.issues.size(); ++i) {
            const auto& issue = unit.issues[i];
            if (i) report << ',';
            report << "{\"address\":" << json(hex32(issue.address)) << ",\"bytes\":" << json(issue.bytes)
                   << ",\"instruction\":" << json(issue.instruction) << ",\"reason\":" << json(issue.reason) << '}';
        }
        report << ']';
        if (!unit.error.empty()) report << ",\"error\":" << json(unit.error);
        if(!unit.native_api.empty()) report << ",\"native_platform_api\":" << json(unit.native_api);
        report << '}';
    }
    result.report = report.str() + "]}";
    return result;
}
}
