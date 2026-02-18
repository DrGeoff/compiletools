#define XSTR(x) STR(x)
#define STR(x) #x

#ifdef COMPILETIME_INCLUDE_FILE
#include XSTR(COMPILETIME_INCLUDE_FILE)
#else
#include "default_extra.h"
#endif

int main() { return 0; }
