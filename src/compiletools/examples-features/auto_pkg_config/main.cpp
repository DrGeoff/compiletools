// Auto-pkg-config smoke fixture: a system-style include with no explicit
// //#PKG-CONFIG annotation. With PKG_CONFIG_PATH pointed at ../pkgs and
// CT_AUTO_PKG_CONFIG=1, inference should attribute autoinfer/api.h to the
// autoinfer package and add -lautoinfer / -I.../pkgs/autoinfer-include.
#include <autoinfer/api.h>

int main() {
    return autoinfer_answer() == 42 ? 0 : 1;
}
