#ifndef AUTOINFER_API_H
#define AUTOINFER_API_H

#ifdef __cplusplus
extern "C" {
#endif

/* Declared, not defined: the definition lives in libautoinfer.a
 * (examples-features/pkgs/autoinfer-src/autoinfer.c), which the e2e matrix
 * builds into each cell's workspace. Parse-only tests never link, so the
 * declaration alone is all they need. */
int autoinfer_answer(void);

#ifdef __cplusplus
}
#endif

#endif
