// ct-exemarker
//
// Probes the embedded build-time path strings that ct-cake's Round 3
// -ffile-prefix-map injection scrubs. With the auto-injected
// -ffile-prefix-map=<gitroot>=<target> in place:
//
//   * __FILE__ expands to the *workspace-relative* path, not the
//     absolute path on the building host.
//   * DWARF debug info (visible via `readelf --debug-dump=decodedline`
//     or `strings bin/<variant>/path_probe | grep ffile_prefix_map`)
//     records the workspace-relative path, not the absolute path.
//   * The compiler's per-TU CAS object key uses the workspace-relative
//     path too, so two users with the repo cloned at different absolute
//     locations get byte-identical .o files and the same CAS hit.
//
// Run build.sh and inspect the output; the printed __FILE__ should
// look like "./path_probe.cpp" (with `--ffile-prefix-map-target=.`,
// the default), regardless of the absolute path of the workspace.

#include <cstdio>

int main()
{
    std::printf("__FILE__ = %s\n", __FILE__);
    return 0;
}
