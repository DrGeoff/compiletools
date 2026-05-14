// Forces -lcycle_beta BEFORE -lcycle_alpha as a hard ordering.
// Combined with asserts_alpha_before_beta.cpp this produces an
// unbreakable 2-cycle in the LDFLAGS topological sort.
//#PKG-CONFIG=cycle-beta cycle-alpha

int beta_value() { return 2; }
