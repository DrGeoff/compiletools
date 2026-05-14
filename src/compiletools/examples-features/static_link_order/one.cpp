// one.cpp — only needs libbase directly.
// When discovered before two.cpp, naive concatenation puts -llibbase
// before -llibnext on the link line, which breaks static linking
// because libbase symbols are discarded before libnext references them.

//#LDFLAGS=-llibbase

void use_base();

void from_one() {
    use_base();
}
