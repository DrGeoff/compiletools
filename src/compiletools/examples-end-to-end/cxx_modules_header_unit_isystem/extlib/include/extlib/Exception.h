#pragma once
#include <stdexcept>
#include <string>
namespace extlib {
struct Exception : public std::runtime_error {
    using std::runtime_error::runtime_error;
};
}
