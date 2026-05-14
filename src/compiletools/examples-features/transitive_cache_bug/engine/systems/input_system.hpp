// Another system that includes event_handler
#pragma once

#include "../core/event_handler.hpp"

namespace game_engine { namespace systems {

struct input_system : public core::event_handler {
    void poll_input();
    bool key_pressed(int key);
};

} }
