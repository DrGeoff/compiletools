// ct-exemarker
//
// Demonstrates the *intentional* cyclic-library failure that ct-cake
// reports when two TUs assert opposite hard link-order constraints
// via multi-package //#PKG-CONFIG annotations.
//
//   asserts_alpha_before_beta.cpp says: -lcycle_alpha BEFORE -lcycle_beta
//   asserts_beta_before_alpha.cpp says: -lcycle_beta  BEFORE -lcycle_alpha
//
// Both edges are *hard* (multi-pkg), so the topological sort cannot
// cancel either side and ct-cake raises LDFLAGSCycleError. See README.md
// for the expected diagnostic.

int alpha_value();
int beta_value();

int main()
{
    return alpha_value() + beta_value();
}
