// ct-exemarker
// Phase 5: header units. `import <h>;` is the bridge between
// pre-modules `#include` and full modular code -- it gives most of the
// compile-time win without rewriting the standard library headers as
// named modules. compiletools auto-detects the header-unit imports,
// precompiles each unique header once, and threads the resulting
// .gcm/.pcm into every importing TU.
import <vector>;
import <cstdio>;

int main() {
    std::vector<int> v{2, 3, 5, 7, 11};
    std::printf("vec_size=%zu front=%d\n", v.size(), v.front());
    return 0;
}
