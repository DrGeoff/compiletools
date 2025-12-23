#include "task_scheduler.hpp"
// Note: memory_buffer.hpp is included transitively via task_scheduler.hpp -> event_handler.hpp

namespace game_engine { namespace systems {

void task_scheduler::insert(core::background_task* task) {
    tasks_.emplace_back(true, task);
    ++active_count_;
}

// Use zlib through memory_buffer (accessed transitively via event_handler.hpp)
// This creates a link-time dependency on -lz
size_t task_scheduler::estimate_compressed_size(size_t raw_size) {
    return core::memory_buffer::compress_bound(raw_size);
}

void task_scheduler::remove(core::background_task* task) {
    // Remove the task from the list
}

size_t task_scheduler::size() const {
    return tasks_.size();
}

bool task_scheduler::empty() const {
    return tasks_.empty();
}

bool task_scheduler::active() const {
    return active_count_ > 0;
}

void task_scheduler::poll_once() {
    if (!active()) return;
    // Poll next active task
}

void task_scheduler::on_progression() {
    // Reset all tasks to active
}

} }
