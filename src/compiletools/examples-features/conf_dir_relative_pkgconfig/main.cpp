// Fixture for the "relative path in conf-file prepend-PKG-CONFIG-PATH"
// bug. The flavor selected by --config=ct.conf.d/flavor-{a,b}.conf
// determines which flavored.pc supplies -DFLAVORED_FLAVOR.
//
// See test_conf_relative_paths_bug.py for the exposure.
//#PKG-CONFIG=flavored

#ifndef FLAVORED_FLAVOR
#error "FLAVORED_FLAVOR must be supplied by flavored.pc via pkg-config"
#endif

int main()
{
    return FLAVORED_FLAVOR == 'A' ? 0 : 1;
}
