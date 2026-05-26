// frontend.cpp — definitions for the shared frontend scaffolding.
//
// A light layer over terminal.h: it needs only <format>/<string>/<string_view>
// plus the terminal primitives, so (unlike terminal.cpp) it does NOT use the
// PCH — pch.h bundles the heavy POSIX headers terminal.cpp needs, which this
// file does not. ct-cake pulls this in via the adjacent-.cpp rule for anything
// that #includes "frontend.h", so frontend.o is compiled once and linked into
// every program.
//
// CAS: this TU's object lives in cas-objdir, shared by all five programs.
#include "frontend.h"
#include "terminal.h"

#include <format>

namespace frontend {

std::string splash_screen(std::string_view title, std::string_view body,
                          std::string_view keys, std::string_view verb) {
    return std::format("\x1b[2J\x1b[H\n{}\n\n{}\n\n{}\n\n"
                       "        ----  press any key to {}  ----\n",
                       title, body, keys, verb);
}

bool wait_for_start(std::string_view screen) {
    term::write_frame(screen);
    for (;;) {
        const char key = term::read_key();
        if (key == 'q') return false;
        if (key != '\0') return true;
        term::sleep_ms(20);
    }
}

bool run_splash(std::string_view title, std::string_view body,
                std::string_view keys, std::string_view verb) {
    return wait_for_start(splash_screen(title, body, keys, verb));
}

}  // namespace frontend
