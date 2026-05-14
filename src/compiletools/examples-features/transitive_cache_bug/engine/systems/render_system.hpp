// Another system that includes event_handler
#pragma once

#include "../core/event_handler.hpp"

namespace game_engine { namespace systems {

struct render_system : public core::event_handler {
    void begin_frame();
    void end_frame();
};

} }
