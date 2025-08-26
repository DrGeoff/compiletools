// This file does NOT define FEATURE_A_ENABLED
// So FEATURE_B_ENABLED should NOT be defined in config.h
// And module_b.h should NOT be included by core.h
// But macro state pollution could cause incorrect inclusion

#include "config.h"
#include "core.h"

int main() {
    return 0;
}