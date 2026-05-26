// frontend.h — shared terminal-game frontend scaffolding, built on terminal.h.
//
// The five programs share one splash flow (clear the screen, show a title +
// instructions, wait for a key) and one ANSI vocabulary for their renderers.
// That shared logic lives here so it compiles once into frontend.o and links
// into every program — the second object (after terminal.o) the CAS serves to
// all five. Each game still supplies its own title/instructions and draws its
// own scene; only the identical scaffolding is here.
#pragma once

#include <string>
#include <string_view>

namespace frontend {

// ANSI escape vocabulary shared by every renderer.
inline constexpr std::string_view CURSOR_HOME = "\x1b[H";   // cursor to top-left
inline constexpr std::string_view CLEAR_EOL   = "\x1b[K";   // erase to end of line
inline constexpr std::string_view RESET       = "\x1b[0m";  // reset all attributes

// Compose a standard splash screen: clear the display, then lay out the
// (already-styled) title banner, the body text, the key help, and a
// "press any key to <verb>" footer, with uniform spacing between sections.
// title/keys may carry their own ANSI styling (e.g. a coloured banner).
std::string splash_screen(std::string_view title, std::string_view body,
                          std::string_view keys, std::string_view verb = "begin");

// Show `screen`, then block until the player presses a key: false if they
// pressed 'q' (abort), true on any other key (start). Polls at the games'
// cadence. Only used on the interactive path (stdin is a TTY).
bool wait_for_start(std::string_view screen);

// Convenience: splash_screen(title, body, keys, verb) then wait_for_start(...).
bool run_splash(std::string_view title, std::string_view body,
                std::string_view keys, std::string_view verb = "begin");

}  // namespace frontend
