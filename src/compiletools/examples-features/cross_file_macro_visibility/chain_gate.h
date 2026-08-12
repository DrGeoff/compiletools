#pragma once

/* Top of the chain: reads CHAIN_MID, which chain_mid.h only defines correctly
   once CHAIN_BASE is known. The false branch selects a different library, so
   a regression is a mislink rather than a warning. */
#if CHAIN_MID >= 5
//#CXXFLAGS=-DCHAIN_HIGH=1
//#LDFLAGS=-lchain_high
#else
//#CXXFLAGS=-DCHAIN_LOW=1
//#LDFLAGS=-lchain_low
#endif
