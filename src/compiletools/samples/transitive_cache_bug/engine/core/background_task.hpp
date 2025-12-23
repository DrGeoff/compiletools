#pragma once

namespace game_engine { namespace core {

// Interface for background tasks that run during idle time
struct background_task {
    virtual ~background_task() {}
    virtual bool on_idle(unsigned int counter) = 0;
};

} }
