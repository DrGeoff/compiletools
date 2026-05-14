// Transitive header that references APP_NAME via #ifdef. The TU that
// includes this header should have APP_NAME in its scope filter (and
// therefore its macro_state_hash) even if the TU body itself never
// mentions APP_NAME.
#pragma once

#ifdef APP_NAME
inline const char* app_name() { return APP_NAME; }
#else
inline const char* app_name() { return "default"; }
#endif
