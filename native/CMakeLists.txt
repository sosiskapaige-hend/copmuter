# AgentRuntime — нативное ядро агента.
#
# Windows: AgentRuntime.dll (для P/Invoke из C#) + agent_host.exe (автономный запуск).
# Linux/macOS: тестовый бинарь ядра (dev-режим: голова без рук).
#
# Сборка:
#   cmake -S native -B build/native -DCMAKE_BUILD_TYPE=Release
#   cmake --build build/native -j
# Без CMake (только ядро и тесты):
#   g++ -std=c++20 -O2 -Iinclude tests/test_core.cpp src/*.cpp -o build/agent_tests
cmake_minimum_required(VERSION 3.20)
project(agent_runtime LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
if(MSVC)
    add_compile_options(/W4 /permissive- /utf-8 /EHsc)
else()
    add_compile_options(-Wall -Wextra)
endif()

set(AGENT_INCLUDE_DIR ${CMAKE_CURRENT_SOURCE_DIR}/include)

# --- переносимое ядро: разбор, маршрутизация, реестры, оптимизатор, пакеты, ожидания
set(AGENT_CORE_SOURCES
    src/core_intent.cpp
    src/core_intent_parse.cpp
    src/core_registry.cpp
    src/core_optimizer.cpp
    src/core_batch.cpp
    src/core_router.cpp
    src/core_runtime.cpp
    src/core_execute.cpp
    src/core_util.cpp
    src/core_wait.cpp
    src/core_tool_call.cpp
    src/core_png.cpp
    src/core_agent_loop.cpp
)

add_library(agent_core STATIC ${AGENT_CORE_SOURCES})
target_include_directories(agent_core PUBLIC ${AGENT_INCLUDE_DIR})

if(WIN32)
    # --- платформенный слой Windows + C ABI для C#
    add_library(agent_runtime SHARED
        ${AGENT_CORE_SOURCES}
        src/platform_win32.cpp
        src/ipc_win32.cpp
        src/abi.cpp
    )
    target_include_directories(agent_runtime PRIVATE ${AGENT_INCLUDE_DIR})
    target_link_libraries(agent_runtime PRIVATE
        ole32 uuid shell32 shlwapi user32 gdi32 advapi32 dxgi d3d11 psapi
    )
    set_target_properties(agent_runtime PROPERTIES
        OUTPUT_NAME "AgentRuntime"
        WINDOWS_EXPORT_ALL_SYMBOLS OFF
    )
    # Автономный хост: тот же рантайм без UI (отладка, CI, скрипты).
    add_executable(agent_host src/host_main.cpp ${AGENT_CORE_SOURCES} src/platform_win32.cpp
                              src/ipc_win32.cpp)
    target_include_directories(agent_host PRIVATE ${AGENT_INCLUDE_DIR})
    target_link_libraries(agent_host PRIVATE ole32 uuid shell32 user32 gdi32 advapi32 dxgi d3d11 psapi)
else()
    # --- dev-платформа (Linux/macOS) + POSIX-канал
    add_library(agent_runtime STATIC ${AGENT_CORE_SOURCES} src/platform_linux.cpp src/ipc_posix.cpp
                src/abi.cpp)
    target_include_directories(agent_runtime PUBLIC ${AGENT_INCLUDE_DIR})
endif()

# --- тесты ядра
add_executable(agent_scenarios tests/test_scenarios.cpp ${CORE_SOURCES})
target_include_directories(agent_scenarios PRIVATE include tests)

add_executable(agent_tests tests/test_core.cpp)
target_link_libraries(agent_tests PRIVATE agent_runtime)
if(UNIX)
    target_link_libraries(agent_tests PRIVATE pthread)
endif()
add_test(NAME agent_core COMMAND agent_tests)
set_tests_properties(agent_core PROPERTIES ENVIRONMENT "AGENT_REPO_ROOT=${CMAKE_SOURCE_DIR}")

# --- микро-бенчмарк маршрутизатора (ТЗ §22: задержка решения в микросекундах)
add_custom_target(agent_bench COMMAND agent_tests --bench 20000
                  DEPENDS agent_tests
                  COMMENT "Бенчмарк маршрутизатора")
