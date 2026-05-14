// Test classified via #include "unit_test.hpp" (the default
// testmarker). The body sleeps briefly to give a non-trivial wall-clock
// signature so the TESTPREFIX wrapper command (timeout, valgrind, etc.)
// has something to wrap.
#include "unit_test.hpp"

#include <chrono>
#include <thread>

int main()
{
    std::this_thread::sleep_for(std::chrono::milliseconds(50));
    return 0;
}
