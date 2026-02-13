// Use traditional #ifndef guard
#ifndef EVENT_LOOP_HPP_48085B47130C44B3D54B6DCFDE23874A
#define EVENT_LOOP_HPP_48085B47130C44B3D54B6DCFDE23874A

#include <functional>
#include "core/event_handler.hpp"
#include "core/background_task.hpp"

namespace game_engine {

// The main event loop for the game engine
struct event_loop : public core::event_handler {
    void run();
    void stop();
    bool is_running() const;

    void insert(core::background_task* task);
    void remove(core::background_task* task);

private:
    bool running_ = false;
};

}

#endif /* EVENT_LOOP_HPP_48085B47130C44B3D54B6DCFDE23874A */
