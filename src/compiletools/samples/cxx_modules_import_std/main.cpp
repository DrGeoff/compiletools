// ct-exemarker
// Phase 4: `import std;` -- the standard library module. compiletools
// auto-detects the importer's compiler and pulls the correct
// system-provided std-module source into the build (gcc:
// `<include>/c++/<ver>/bits/std.cc`; clang: `<install>/share/libc++/v1/std.cppm`).
import std;

int main() {
    std::println("add(2,3)={}", 2 + 3);
    return 0;
}
