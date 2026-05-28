#include "createfile.hpp"
#include "filename.hpp"
#include <filesystem>
#include <fstream>
#include <stdexcept>

void create_file()
{
    std::filesystem::path filePath(serialise_test_filename());
    if (!std::filesystem::exists(filePath))
    {
        std::ofstream file(filePath);
        if (!file)
        {
            throw std::runtime_error("Error: Could not create or open the file");
        }
    }
}
