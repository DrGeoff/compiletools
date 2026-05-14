// ct-exemarker
//
// Surfaces compiler / instrumentation / optimization choices baked in
// by the variant axis composition. Identifies:
//
//   * the toolchain  (gcc / clang)
//   * the optimization axis (debug vs release; NDEBUG fingerprint)
//   * the AddressSanitizer instrumentation axis (when --variant=...,asan)
//   * the C++ language standard (-std=)
//
// Cross-reference with the build.sh script: each invocation lands in
// a different bin/<canonical-variant>/ directory and prints a different
// banner.

#include <cstdio>

int main()
{
#if defined(__clang__)
    const char* toolchain = "clang " __clang_version__;
#elif defined(__GNUC__)
    const char* toolchain = "gcc " __VERSION__;
#else
    const char* toolchain = "(unknown toolchain)";
#endif

#ifdef NDEBUG
    const char* opt = "release (NDEBUG defined)";
#else
    const char* opt = "debug (NDEBUG not defined)";
#endif

#if defined(__SANITIZE_ADDRESS__) || defined(__has_feature)
#  if defined(__SANITIZE_ADDRESS__)
    const char* asan = "asan ON (gcc __SANITIZE_ADDRESS__)";
#  elif __has_feature(address_sanitizer)
    const char* asan = "asan ON (clang __has_feature)";
#  else
    const char* asan = "asan off";
#  endif
#else
    const char* asan = "asan off";
#endif

    std::printf("toolchain     : %s\n", toolchain);
    std::printf("optimization  : %s\n", opt);
    std::printf("instrumentation: %s\n", asan);
    std::printf("__cplusplus   : %ldL\n", (long)__cplusplus);
    return 0;
}
