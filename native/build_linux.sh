#!/usr/bin/env bash
# Сборка ядра и тестов без CMake (g++/clang++ на Linux или macOS).
#
#   ./native/build_linux.sh          — ядро + тесты + хост в native/build/
#   ./native/build_linux.sh --run    — то же и сразу прогнать тесты и бенчмарк
#   ./native/build_linux.sh --cross  — плюс кросс-проверка Windows-сборки (нужен zig)
set -euo pipefail

cd "$(dirname "$0")"
CXX="${CXX:-g++}"
CXXFLAGS="${CXXFLAGS:--std=c++20 -O2 -Wall -Wextra}"
BUILD=build
mkdir -p "$BUILD"

CORE_SOURCES=(
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
  src/platform_linux.cpp
  src/ipc_posix.cpp
  src/abi.cpp
)

echo "== ядро и тесты"
$CXX $CXXFLAGS -Iinclude tests/test_core.cpp "${CORE_SOURCES[@]}" -lpthread -o "$BUILD/agent_tests"

echo "== agent_scenarios (обязательные сценарии ТЗ)"
$CXX $CXXFLAGS -Iinclude -Itests tests/test_scenarios.cpp "${CORE_SOURCES[@]}" -lpthread \
  -o "$BUILD/agent_scenarios"

echo "== agent_host"
$CXX $CXXFLAGS -Iinclude src/host_main.cpp "${CORE_SOURCES[@]}" -lpthread -o "$BUILD/agent_host"

echo "готово: $BUILD/agent_tests, $BUILD/agent_scenarios, $BUILD/agent_host"

if [[ "${1:-}" == "--cross" ]]; then
  echo
  # Windows-код здесь не запустить, но можно убедиться, что он компилируется и
  # линкуется под x86_64-windows, а DLL отдаёт ровно тот C ABI, что ждёт C#.
  ./build_windows_cross.sh
fi

if [[ "${1:-}" == "--run" ]]; then
  echo
  AGENT_REPO_ROOT="$(cd .. && pwd)" "$BUILD/agent_scenarios"
  echo
  AGENT_REPO_ROOT="$(cd .. && pwd)" "$BUILD/agent_tests"
  echo
  "$BUILD/agent_tests" --bench 20000
fi
