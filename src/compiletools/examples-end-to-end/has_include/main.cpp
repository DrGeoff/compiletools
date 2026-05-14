// Test __has_include with a local header that exists (angle-bracket via -I path)
#if __has_include(<has_include/optional_feature.h>)
#include "optional_feature.h"
#endif

// Test __has_include with a header that does not exist
#if __has_include(<has_include/nonexistent_feature.h>)
#include "nonexistent_feature.h"
#endif

// Test __has_include with a system header (should exist on any compiler)
#if __has_include(<cstddef>)
#include "stdheader_extras.h"
#endif

int main() {
    return 0;
}
