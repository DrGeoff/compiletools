#ifndef EVENT_HANDLER_HPP_78505A7195AD8C70004A32069928687B
#define EVENT_HANDLER_HPP_78505A7195AD8C70004A32069928687B

#include <exception>
#include "memory_buffer.hpp"

namespace game_engine { namespace core {

// Base class for event handlers
struct event_handler {
protected:
    struct handler_base {
        virtual ~handler_base() {}
        virtual void handle_data(const memory_buffer& buf) = 0;
        virtual void handle_close() = 0;
    };
};

} }

#endif /* EVENT_HANDLER_HPP_78505A7195AD8C70004A32069928687B */
