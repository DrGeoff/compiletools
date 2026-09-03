// Auto-inference fixture: a vendored copy of another project's header living
// inside autoinfer-umbrella's exported include dir. There is deliberately no
// autoinfer-vendored.pc, so a translation unit declaring
// //#PKG-CONFIG=autoinfer-vendored has an unsatisfiable declaration and the
// pc-index resolver would otherwise attribute this header to the umbrella.
#pragma once

int autoinfer_vendored_api(void);
