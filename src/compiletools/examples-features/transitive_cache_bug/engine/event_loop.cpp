// Models a source file using event_loop
#include "event_loop.hpp"

namespace game_engine {

void event_loop::run() {
    running_ = true;
    while (running_) {
        // Process events
    }
}

void event_loop::stop() {
    running_ = false;
}

bool event_loop::is_running() const {
    return running_;
}

void event_loop::insert(core::background_task* task) {
    // Add task
}

void event_loop::remove(core::background_task* task) {
    // Remove task
}

}
