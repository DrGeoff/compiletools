#pragma once

#include <filesystem>
#include <string>

inline const std::string& serialise_test_filename()
{
    static const std::string fn =
        (std::filesystem::temp_directory_path() / "serialise_test.txt").string();
    return fn;
}
