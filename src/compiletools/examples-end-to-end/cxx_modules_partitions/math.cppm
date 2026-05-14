export module math;

// Primary interface unit re-exports both partitions so importers of
// `math` see add() and mul() without having to import the partitions
// themselves.
export import :basic;
export import :advanced;
