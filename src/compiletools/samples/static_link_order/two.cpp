// two.cpp — needs libnext, which itself depends on libbase.
// The LDFLAGS order declares that libnext must come before libbase.

//#LDFLAGS=-llibnext -llibbase

void use_next();

void from_two() {
    use_next();
}
