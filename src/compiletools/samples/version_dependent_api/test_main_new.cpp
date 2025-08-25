#include "api_config_new.h"
#include <iostream>

int main() {
#if defined(USE_LEGACY_API)
    std::cout << "Using legacy API system (< 1.27.13)" << std::endl;
#elif defined(MYAPP_ENABLE_V2_SYSTEM)
    std::cout << "Using modern V2 system (>= 1.27.13)" << std::endl;
#else
    std::cout << "No API system detected" << std::endl;
#endif
    return 0;
}