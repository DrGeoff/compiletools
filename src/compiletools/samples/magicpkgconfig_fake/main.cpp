// Simple test for pkg-config with fake packages

//#CXXFLAGS=-std=c++17
// Testing multiple fake packages on one line
//#PKG-CONFIG=conditional nested

void conditional_function();
void nested_function();

int main(int argc, char* argv[])
{
    // Simple test that doesn't require actual libraries
    conditional_function();
    nested_function();
    return 0;
}

// Stub implementations (no real library needed)
void conditional_function() {}
void nested_function() {}
