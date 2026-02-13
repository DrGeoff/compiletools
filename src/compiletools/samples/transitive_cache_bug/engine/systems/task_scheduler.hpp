// Tests using #pragma once
#pragma once

#include "../core/background_task.hpp"
#include "../core/event_handler.hpp"
#include <vector>
#include <utility>

namespace game_engine { namespace systems {

// Schedules and runs background tasks during idle time
struct task_scheduler : public core::event_handler {
    void insert(core::background_task* task);
    void remove(core::background_task* task);

    size_t size() const;
    bool empty() const;
    bool active() const;

    void poll_once();
    void on_progression();

    // Estimate compressed size using zlib (via memory_buffer)
    size_t estimate_compressed_size(size_t raw_size);

private:
    std::vector<std::pair<bool, core::background_task*>> tasks_;
    size_t last_index_ = 0;
    int active_count_ = 0;
    unsigned int counter_ = 0;
};

} }
