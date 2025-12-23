// Another system that includes event_handler
#pragma once

#include "../core/event_handler.hpp"

namespace game_engine { namespace systems {

struct audio_system : public core::event_handler {
    void play_sound(int id);
    void stop_sound(int id);
};

} }
