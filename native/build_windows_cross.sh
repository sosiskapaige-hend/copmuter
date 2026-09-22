#!/usr/bin/env bash
# Кросс-проверка Windows-сборки (zig cc/clang, mingw-w64 заголовки).
#
# Зачем: на Linux нельзя запустить Windows-код, но можно проверить, что он
# КОМПИЛИРУЕТСЯ и ЛИНКУЕТСЯ под x86_64-windows — то есть что в platform_win32.cpp,
# ipc_win32.cpp и ABI нет ошибок, которые всплыли бы только на машине пользователя.
# Дополнительно сверяем список экспортируемых функций с C ABI в runtime.h:
# если функция объявлена, но не вышла из DLL, C# упадёт с EntryPointNotFound.
#
#   ./native/build_windows_cross.sh            — компиляция + линковка + проверка экспортов
#   ./native/build_windows_cross.sh --clean    — то же и удалить каталог сборки
#
# Требуется zig: python3 -m pip install --user ziglang
set -euo pipefail

cd "$(dirname "$0")"

ZIG="${ZIG:-}"
if [[ -z "$ZIG" ]]; then
  for candidate in zig /home/user/.local/lib/python3.11/site-packages/ziglang/zig; do
    if command -v "$candidate" >/dev/null 2>&1 || [[ -x "$candidate" ]]; then ZIG="$candidate"; break; fi
  done
fi
if [[ -z "$ZIG" ]]; then
  echo "не найден zig (pip install --user ziglang)" >&2
  exit 2
fi

TARGET=x86_64-windows-gnu
OBJ_DIR="${OBJ_DIR:-/tmp/copmuter_wincross}"
mkdir -p "$OBJ_DIR"
CXXFLAGS=(-target "$TARGET" -std=c++20 -O2 -Wall -Wextra -Iinclude)
LIBS=(-luser32 -lgdi32 -lshell32 -lole32 -loleaut32 -ladvapi32 -lshlwapi -lpsapi
      -ldxgi -ld3d11 -luuid -lversion -lwinmm -lgdiplus -lws2_32)

CORE=(
  core_intent core_intent_parse core_registry core_optimizer core_batch core_router
  core_runtime core_execute core_util core_wait core_tool_call core_png core_agent_loop
  platform_win32 ipc_win32 abi
)
# host_main и тесты — отдельные точки входа.
EXTRA=(host_main)

echo "== компиляция под $TARGET"
for f in "${CORE[@]}" "${EXTRA[@]}"; do
  printf '   %-22s' "$f"
  if "$ZIG" c++ "${CXXFLAGS[@]}" -c "src/$f.cpp" -o "$OBJ_DIR/$f.o" 2> "$OBJ_DIR/$f.err"; then
    echo "ok"
  else
    echo "ОШИБКА"
    grep -E "error:" "$OBJ_DIR/$f.err" | head -20
    exit 1
  fi
done

echo "== линковка"
"$ZIG" c++ -target "$TARGET" -shared -o "$OBJ_DIR/AgentRuntime.dll" \
  $(for f in "${CORE[@]}"; do echo "$OBJ_DIR/$f.o"; done) "${LIBS[@]}" \
  > "$OBJ_DIR/link.log" 2>&1 || { echo "линковка не прошла:"; grep -E "undefined|error:" "$OBJ_DIR/link.log" | head -20; exit 1; }
"$ZIG" c++ -target "$TARGET" -o "$OBJ_DIR/agent_host.exe" \
  $(for f in "${CORE[@]}" "${EXTRA[@]}"; do echo "$OBJ_DIR/$f.o"; done) "${LIBS[@]}" \
  >> "$OBJ_DIR/link.log" 2>&1 || { echo "agent_host.exe не собрался:"; grep -E "undefined|error:" "$OBJ_DIR/link.log" | head -20; exit 1; }
echo "   AgentRuntime.dll + agent_host.exe: ok"

echo "== сверка экспортов с C ABI из runtime.h"
python3 tools/check_exports.py "$OBJ_DIR/AgentRuntime.dll" include/agent/runtime.h

if [[ "${1:-}" == "--clean" ]]; then
  rm -rf "$OBJ_DIR"
else
  echo "(объектные файлы: $OBJ_DIR, удалить: rm -rf)"
fi
echo
echo "Кросс-проверка пройдена. На Windows собирайте MSVC: native\\build_windows.bat"
