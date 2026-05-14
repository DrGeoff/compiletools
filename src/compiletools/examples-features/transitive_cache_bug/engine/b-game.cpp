// Processed second alphabetically - hits polluted cache
// This executable FAILS to link (missing -lz)
#include "systems/task_scheduler.hpp"

// task_scheduler.hpp -> event_handler.hpp -> memory_buffer.hpp
// But due to cache pollution, memory_buffer.hpp is MISSING
// Therefore -lz is not linked

int main() {
    game_engine::systems::task_scheduler scheduler;
    // This calls compressBound from zlib
    // Will fail: "undefined reference to compressBound"
    auto bound = scheduler.estimate_compressed_size(1024);
    return (int)bound;
}
