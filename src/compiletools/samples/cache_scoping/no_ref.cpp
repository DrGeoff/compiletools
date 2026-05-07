// A TU that does NOT reference the unused cmdline-D macro anywhere.
// Used by test_hunter_cache_scoping to prove the per-TU scope filter
// excludes unused cmdline-D macros from the object file's
// macro_state_hash. (The macro name is omitted from this comment
// because CmdlineMacroIndex does word-boundary identifier scans and
// would flag a literal name in a comment as a reference.)
#include <stdio.h>

int main() {
    printf("hello\n");
    return 0;
}
