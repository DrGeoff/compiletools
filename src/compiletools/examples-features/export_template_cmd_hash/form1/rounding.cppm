export module myproj.util.rounding;

namespace myproj {

export template <typename T>
inline T roundUp(T value, T multiple) {
    return ((value + multiple - 1) / multiple) * multiple;
}

export template <typename T>
inline T roundDown(T value, T multiple) {
    return (value / multiple) * multiple;
}

}
