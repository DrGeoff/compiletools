// TU that does NOT mention APP_NAME directly but pulls it in via a
// transitive header. Validates that the per-TU scope filter walks
// transitive headers, not just the TU's own bytes.
#include "header_ref.hpp"
#include <stdio.h>

int main() {
    printf("%s\n", app_name());
    return 0;
}
