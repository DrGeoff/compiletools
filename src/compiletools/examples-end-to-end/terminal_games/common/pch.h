// pch.h — precompiled header for terminal.cpp.
//
// Bundles the heavy POSIX + libc headers the terminal layer needs. ct-cake
// compiles this once and caches the result in cas-pchdir; every rebuild of
// terminal.cpp reuses it. Declared via `//#PCH=pch.h` in terminal.cpp.
#pragma once

#include <termios.h>
#include <unistd.h>
#include <sys/ioctl.h>
#include <csignal>
#include <cstdlib>
#include <cstdio>
#include <cstring>
#include <ctime>
#include <string_view>
