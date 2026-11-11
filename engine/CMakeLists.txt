cmake_minimum_required(VERSION 3.20)
project(touchstone_engine LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

find_package(Python REQUIRED COMPONENTS Interpreter Development.Module)
find_package(pybind11 CONFIG REQUIRED)

pybind11_add_module(touchstone_engine src/bindings.cpp)
target_include_directories(touchstone_engine PRIVATE include)
target_compile_options(touchstone_engine PRIVATE -O3 -Wall -Wextra)
install(TARGETS touchstone_engine DESTINATION .)
