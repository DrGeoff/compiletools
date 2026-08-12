#pragma once

/* Middle link: reads CHAIN_BASE from chain_base.h and defines the macro
   chain_gate.h reads. Resolving this needs CHAIN_BASE to already be known,
   which only happens after chain_base.h has been processed. */
#if CHAIN_BASE >= 7
#define CHAIN_MID 9
#else
#define CHAIN_MID 1
#endif
