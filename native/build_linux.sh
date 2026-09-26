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

CORE_OBJS=()
pids=()
echo "== компиляция модулей ядра"
for src in "${CORE_SOURCES[@]}"; do
  obj="$BUILD/$(basename "$src" .cpp).o"
  CORE_OBJS+=("$obj")
  if [[ ! -f "$obj" || "$src" -nt "$obj" ]]; then
    $CXX $CXXFLAGS -Iinclude -c "$src" -o "$obj" &
    pids+=($!)
  fi
done

for pid in "${pids[@]}"; do
  wait "$pid"
done

echo "== сборка agent_tests"
$CXX $CXXFLAGS -Iinclude tests/test_core.cpp "${CORE_OBJS[@]}" -lpthread -o "$BUILD/agent_tests"

echo "== сборка agent_scenarios"
$CXX $CXXFLAGS -Iinclude -Itests tests/test_scenarios.cpp "${CORE_OBJS[@]}" -lpthread -o "$BUILD/agent_scenarios"

echo "== сборка agent_host"
$CXX $CXXFLAGS -Iinclude src/host_main.cpp "${CORE_OBJS[@]}" -lpthread -o "$BUILD/agent_host"

echo "готово: $BUILD/agent_tests, $BUILD/agent_scenarios, $BUILD/agent_host"

if [[ "${1:-}" == "--cross" ]]; then
  echo
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
