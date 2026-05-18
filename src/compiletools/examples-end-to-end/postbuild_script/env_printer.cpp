// ct-exemarker — see README.md for context.

#include <cstdlib>
#include <iostream>

int main()
{
    const char* value = std::getenv("DEMO_ENV_VAR");
    std::cout << "DEMO_ENV_VAR=" << (value ? value : "(unset)") << std::endl;
    return 0;
}
