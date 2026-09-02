/* Definition of the autoinfer fixture library (Stage 6): autoinfer/api.h only
 * declares autoinfer_answer(); parse-only tests never link, but the e2e
 * matrix cell for the auto_pkg_config example compiles this into a real
 * libautoinfer.a (published through a generated autoinfer.pc whose
 * Libs: -L<ws>/lib -lautoinfer shadows this file's Libs: -lautoinfer) so the
 * positive cell is a genuine link test and the --no-auto-pkg-config negative
 * cell fails at the #include. */
int autoinfer_answer(void) { return 42; }
