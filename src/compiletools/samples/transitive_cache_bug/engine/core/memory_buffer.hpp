// Models libs/base/range.hpp - uses #pragma once
#pragma once

//#PKG-CONFIG=zlib

#include <zlib.h>

namespace game_engine { namespace core {

// A simple memory buffer class that uses zlib for compression
struct memory_buffer {
    char* data;
    size_t size;

    // Compress data using zlib - this creates a link-time dependency on -lz
    static unsigned long compress_bound(unsigned long source_len) {
        return ::compressBound(source_len);
    }
};

} }
