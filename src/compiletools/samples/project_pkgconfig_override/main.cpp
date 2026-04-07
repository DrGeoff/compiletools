// Demonstrates project-level pkg-config override.
// The ct.conf.d/pkgconfig/conditional.pc in this directory overrides
// any system-level or test conditional.pc package.

//#PKG-CONFIG=conditional

int main()
{
    return 0;
}
