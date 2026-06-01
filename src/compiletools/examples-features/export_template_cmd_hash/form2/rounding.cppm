export module myproj.util.rounding;

export namespace myproj {

template <typename T>
inline T roundUp(T value, T multiple) {
    return ((value + multiple - 1) / multiple) * multiple;
}

template <typename T>
inline T roundDown(T value, T multiple) {
    return (value / multiple) * multiple;
}

}
