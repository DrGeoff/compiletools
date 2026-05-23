// terminal.h — a tiny POSIX-terminal facade.
//
// Declarations only: no <termios.h>, no game types. Everything heavy lives in
// terminal.cpp behind pch.h, so importers (game.cpp) stay light and the
// simulation never learns what a terminal is.
#pragma once

#include <string_view>

namespace term {

// Puts stdin into non-blocking cbreak mode for its lifetime and restores the
// previous settings on destruction (also on SIGINT/SIGTERM and normal exit).
// A no-op when stdin is not a TTY. Non-copyable, non-movable.
class RawMode {
public:
    RawMode();
    ~RawMode();
    RawMode(const RawMode&) = delete;
    RawMode& operator=(const RawMode&) = delete;
    bool active() const noexcept { return active_; }

private:
    bool active_;
};

// True if stdin is connected to an interactive terminal.
bool stdin_is_tty() noexcept;

// Non-blocking read of one key; returns the character, or '\0' if none ready.
char read_key() noexcept;

// Clear the screen and move the cursor home.
void clear() noexcept;

// Hide / show the cursor (so redraws don't flicker a blinking caret).
void hide_cursor() noexcept;
void show_cursor() noexcept;

// Write a fully-rendered frame (already containing newlines) to stdout.
void write_frame(std::string_view frame) noexcept;

// Sleep for the given milliseconds (POSIX nanosleep; no pthread dependency).
void sleep_ms(int milliseconds) noexcept;

// Visible terminal height in rows; falls back to 24 if it can't be queried.
int rows() noexcept;

// Visible terminal width in columns; falls back to 80 if it can't be queried.
int cols() noexcept;

}  // namespace term
