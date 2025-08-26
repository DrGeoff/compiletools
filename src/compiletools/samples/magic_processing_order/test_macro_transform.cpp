#include <iostream>
#include "macro_header.h"

// This file should trigger the magic processing order bug in DirectMagicFlags
// when it processes the conditional magic comments in the included header

int main() {
    std::cout << "Testing magic processing order bug" << std::endl;
    return 0;
}