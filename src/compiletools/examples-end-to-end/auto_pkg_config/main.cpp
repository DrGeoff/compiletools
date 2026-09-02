// Auto-pkg-config end-to-end fixture: a system-style include with no
// explicit //#PKG-CONFIG annotation. With PKG_CONFIG_PATH pointed at the
// shared pkgs fixtures (../../examples-features/pkgs) and CT_AUTO_PKG_CONFIG=1
// (the production default), inference attributes autoinfer/api.h to the
// autoinfer package and injects -lautoinfer / -I.../autoinfer-include through
// the ordinary PKG-CONFIG magic path. test_examples_end_to_end_cross_backend
// builds this example across backends; the negative cell proves the build
// fails with --no-auto-pkg-config.
#include <autoinfer/api.h>

int main() {
    return autoinfer_answer() == 42 ? 0 : 1;
}
