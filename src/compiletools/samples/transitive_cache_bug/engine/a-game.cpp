// Processed first alphabetically - pollutes the event_handler.hpp cache
// This executable builds successfully (no zlib dependency)
#include "systems/audio_system.hpp"
#include "systems/render_system.hpp"
#include "systems/input_system.hpp"
#include "event_loop.hpp"

// These includes all go through event_handler.hpp
// After processing, event_handler.hpp is cached with its #ifndef guard defined
// This causes memory_buffer.hpp to be missing from subsequent lookups

int main() {
    // Simple main - no zlib usage
    return 0;
}
