// terminal.cpp -- the POSIX-terminal facade implementation, a trimmed copy of
// terminal_games/common/terminal.cpp. That original hides these system headers
// behind a PCH; this example deliberately uses neither PCH nor modules so the
// //#GIT= lesson stays front and center, so the headers are included directly.
#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <ctime>
#include <sys/ioctl.h>
#include <termios.h>
#include <unistd.h>

#include "terminal.h"

namespace {

// The single stdin's saved settings. There is only ever one controlling
// terminal, so this lives at file scope rather than inside RawMode -- which is
// what keeps <termios.h> out of terminal.h.
termios g_original{};
bool g_raw_active = false;

void restore_terminal() noexcept {
    if (g_raw_active) {
        ::tcsetattr(STDIN_FILENO, TCSANOW, &g_original);
        term::show_cursor();
        g_raw_active = false;
    }
}

extern "C" void on_signal(int signum) {
    restore_terminal();
    ::signal(signum, SIG_DFL);
    ::raise(signum);
}

}  // namespace

namespace term {

bool stdin_is_tty() noexcept { return ::isatty(STDIN_FILENO) == 1; }

RawMode::RawMode() : active_(false) {
    if (!stdin_is_tty()) return;
    if (::tcgetattr(STDIN_FILENO, &g_original) != 0) return;

    termios raw = g_original;
    raw.c_lflag &= static_cast<tcflag_t>(~(ICANON | ECHO));
    raw.c_cc[VMIN] = 0;   // read() returns immediately ...
    raw.c_cc[VTIME] = 0;  // ... with whatever is available (non-blocking)
    if (::tcsetattr(STDIN_FILENO, TCSANOW, &raw) != 0) return;

    g_raw_active = true;
    active_ = true;
    std::atexit(restore_terminal);
    ::signal(SIGINT, on_signal);
    ::signal(SIGTERM, on_signal);
    hide_cursor();
}

RawMode::~RawMode() { restore_terminal(); }

char read_key() noexcept {
    char c = 0;
    return ::read(STDIN_FILENO, &c, 1) == 1 ? c : '\0';
}

void clear() noexcept { write_frame("\x1b[2J\x1b[H"); }

void hide_cursor() noexcept { write_frame("\x1b[?25l"); }
void show_cursor() noexcept { write_frame("\x1b[?25h"); }

void write_frame(std::string_view frame) noexcept {
    ::fwrite(frame.data(), 1, frame.size(), stdout);
    ::fflush(stdout);
}

void sleep_ms(int milliseconds) noexcept {
    if (milliseconds <= 0) return;
    const timespec ts{milliseconds / 1000, (milliseconds % 1000) * 1'000'000L};
    ::nanosleep(&ts, nullptr);
}

int rows() noexcept {
    winsize ws{};
    if (::ioctl(STDOUT_FILENO, TIOCGWINSZ, &ws) == 0 && ws.ws_row > 0)
        return ws.ws_row;
    return 24;
}

int cols() noexcept {
    winsize ws{};
    if (::ioctl(STDOUT_FILENO, TIOCGWINSZ, &ws) == 0 && ws.ws_col > 0)
        return ws.ws_col;
    return 80;
}

}  // namespace term
