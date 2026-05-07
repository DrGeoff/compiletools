// A TU that DOES reference APP_NAME directly. The per-TU scope filter
// must keep APP_NAME in the macro_state_hash so changing -DAPP_NAME=...
// produces a distinct object file.
#include <stdio.h>

#ifndef APP_NAME
#define APP_NAME "default"
#endif

int main() {
    const char* name = APP_NAME;
    printf("%s\n", name);
    return 0;
}
