// main.cpp — links one.cpp and two.cpp via SOURCE magic.
// The correct link order is: -llibnext -llibbase
// (libnext before libbase, since libnext depends on libbase).

//#SOURCE=one.cpp
//#SOURCE=two.cpp

void from_one();
void from_two();

int main() {
    from_one();
    from_two();
    return 0;
}
