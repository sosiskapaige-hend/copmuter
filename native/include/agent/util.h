// Мелкие утилиты, общие для ядра и платформ.
#pragma once

#include <string>
#include <string_view>

#include "agent/platform.h"

namespace agent {

// URL-кодирование (для поисковых ссылок — «ютуб results?search_query=…»).
std::string url_encode(std::string_view s);
// Прямая поисковая ссылка: google | yandex | bing | duckduckgo | youtube.
std::string search_url_for(std::string_view query, std::string_view engine);
// «youtube.com» → «https://youtube.com»; уже готовые схемы не трогаем.
std::string normalize_url(std::string_view raw);
// Склейка пути с учётом разделителя базовой части.
std::string join_path(std::string_view base, std::string_view name);
// Сколько объектов внутри папки (с ограничением — для оценки риска удаления).
int count_items(IPlatform& platform, std::string_view path);

// Мини-чтение аргументов JSON (плоские ключи; полный парсер здесь не нужен):
// args_json приходит от модели/агентного цикла, ключи известны заранее.
std::string json_get_str(std::string_view json, std::string_view key, std::string def = {});
int json_get_int(std::string_view json, std::string_view key, int def = 0);
double json_get_num(std::string_view json, std::string_view key, double def = 0.0);
bool json_get_bool(std::string_view json, std::string_view key, bool def = false);

}  // namespace agent
