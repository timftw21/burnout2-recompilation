#include "presenter_options.h"
#include "vulkan_presenter.h"

#include <cstddef>
#include <string>
#include <vector>

extern "C" __declspec(dllexport) int __cdecl b2r_presenter_main(
    int argc,
    const wchar_t* const* argv) {
    std::vector<std::wstring> args;
    args.reserve(argc > 0 ? static_cast<size_t>(argc) : 1u);
    if (argc <= 0 || argv == nullptr) {
        args.emplace_back(L"b2_presenter");
    } else {
        for (int index = 0; index < argc; ++index) {
            args.emplace_back(argv[index] ? argv[index] : L"");
        }
    }
    return b2r::host::run_vulkan_presenter(args);
}

int main() {
    return b2r::host::run_vulkan_presenter(b2r::host::command_line_args());
}
