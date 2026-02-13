//#PKG-CONFIG=zlib nested

#include "complex_header.h"
#include <iostream>

// This file should reproduce the magic processing order bug when DirectMagicFlags 
// processes the complex conditional magic comments in the included headers

int main() {
    std::cout << "Testing complex magic processing order scenario" << std::endl;
    return 0;
}