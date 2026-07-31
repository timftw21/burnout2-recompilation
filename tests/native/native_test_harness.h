#pragma once

#include <array>
#include <cstddef>
#include <iostream>
#include <string_view>

namespace b2r::test {

struct TestContext {
    int failure_count = 0;

    void expect(bool condition, std::string_view message) {
        if (!condition) {
            std::cerr << "FAILED: " << message << '\n';
            ++failure_count;
        }
    }
};

struct TestCase {
    std::string_view name;
    void (*run)(TestContext&);
};

template <std::size_t Size>
int run_test_cases(
    int argc,
    char** argv,
    const std::array<TestCase, Size>& test_cases
) {
    const std::string_view requested = argc > 1 ? argv[1] : "";
    bool matched = false;
    int failure_count = 0;
    for (const TestCase& test_case : test_cases) {
        if (!requested.empty() && requested != test_case.name) {
            continue;
        }
        matched = true;
        TestContext context{};
        test_case.run(context);
        failure_count += context.failure_count;
        std::cout << test_case.name << ": "
                  << (context.failure_count == 0 ? "passed" : "failed") << '\n';
    }
    if (!matched) {
        std::cerr << "unknown native test case: " << requested << '\n';
        return 2;
    }
    return failure_count == 0 ? 0 : 1;
}

}  // namespace b2r::test
