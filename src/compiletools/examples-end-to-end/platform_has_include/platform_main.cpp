// Platform-specific implementation selection via __has_include.
// On Linux, __has_include(<unistd.h>) is true so linux_func is used.
// On Windows, __has_include(<windows.h>) is true so windows_func is used.
// Both define platform_func() so a duplicate would cause a linker error.
#if __has_include(<windows.h>)
#include "windows_func.hpp"
#elif __has_include(<unistd.h>)
#include "linux_func.hpp"
#endif

int main() {
    return platform_func();
}
