// Общие утилиты ядра.
#include <ctime>

#include "agent/util.h"

#include <cstdio>
#include <cstring>

namespace agent {

// Время суток: журнал отладки читают глазами, а steady_clock показывает только
// «сколько прошло с запуска».
std::string wall_clock_text() {
    using namespace std::chrono;
    const auto now = system_clock::now();
    const auto ms = duration_cast<milliseconds>(now.time_since_epoch()) % 1000;
    const std::time_t t = system_clock::to_time_t(now);
    std::tm tm{};
#ifdef _WIN32
    localtime_s(&tm, &t);
#else
    localtime_r(&t, &tm);
#endif
    char buf[32];
    std::snprintf(buf, sizeof(buf), "%04d-%02d-%02d %02d:%02d:%02d.%03d", tm.tm_year + 1900,
                  tm.tm_mon + 1, tm.tm_mday, tm.tm_hour, tm.tm_min, tm.tm_sec, int(ms.count()));
    return std::string(buf);
}

std::string url_encode(std::string_view s) {
    static const char* hex = "0123456789ABCDEF";
    std::string out;
    out.reserve(s.size() * 3);
    for (unsigned char c : s) {
        const bool safe = (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
                          (c >= '0' && c <= '9') || c == '-' || c == '_' || c == '.' || c == '~';
        if (safe) {
            out.push_back(char(c));
        } else if (c == ' ') {
            out.push_back('+');
        } else {
            out.push_back('%');
            out.push_back(hex[c >> 4]);
            out.push_back(hex[c & 0x0F]);
        }
    }
    return out;
}

std::string search_url_for(std::string_view query, std::string_view engine) {
    const std::string q = url_encode(query);
    if (engine == "youtube") return "https://www.youtube.com/results?search_query=" + q;
    if (engine == "yandex") return "https://yandex.ru/search/?text=" + q;
    if (engine == "bing") return "https://www.bing.com/search?q=" + q;
    if (engine == "duckduckgo") return "https://duckduckgo.com/?q=" + q;
    return "https://www.google.com/search?q=" + q;
}

std::string normalize_url(std::string_view raw) {
    std::string s(raw);
    while (!s.empty() && (s.back() == ' ' || s.back() == '.')) s.pop_back();
    if (s.empty()) return s;
    const bool has_scheme = s.find("://") != std::string::npos ||
                            s.rfind("mailto:", 0) == 0 || s.rfind("ms-settings:", 0) == 0 ||
                            s.rfind("shell:", 0) == 0 || s.rfind("file:", 0) == 0 ||
                            s.rfind("tg:", 0) == 0 || s.rfind("discord:", 0) == 0;
    if (!has_scheme) s = "https://" + s;
    return s;
}

std::string join_path(std::string_view base, std::string_view name) {
    if (name.empty()) return std::string(base);
    if (base.empty()) return std::string(name);
    std::string out(base);
    const char last = out.back();
    if (last != '/' && last != '\\') out += (out.find('\\') != std::string::npos ? '\\' : '/');
    out.append(name.data(), name.size());
    return out;
}

namespace {

// Ищем "key" : значение — с учётом экранирования кавычек внутри строки.
size_t find_value(std::string_view json, std::string_view key) {
    const std::string pattern = "\"" + std::string(key) + "\"";
    size_t pos = 0;
    while ((pos = json.find(pattern, pos)) != std::string_view::npos) {
        size_t at = pos + pattern.size();
        while (at < json.size() && (json[at] == ' ' || json[at] == '\t')) ++at;
        if (at < json.size() && json[at] == ':') {
            ++at;
            while (at < json.size() && (json[at] == ' ' || json[at] == '\t')) ++at;
            return at;
        }
        pos += pattern.size();
    }
    return std::string_view::npos;
}

}  // namespace

std::string json_get_str(std::string_view json, std::string_view key, std::string def) {
    const size_t at = find_value(json, key);
    if (at == std::string_view::npos || at >= json.size()) return def;
    if (json[at] == 'n' && json.compare(at, 4, "null") == 0) return def;
    if (json[at] != '"') {
        // число/булево как строка
        size_t end = at;
        while (end < json.size() && json[end] != ',' && json[end] != '}' && json[end] != '\n') ++end;
        std::string raw(json.substr(at, end - at));
        while (!raw.empty() && (raw.back() == ' ' || raw.back() == '\t')) raw.pop_back();
        return raw.empty() ? def : raw;
    }
    std::string out;
    for (size_t i = at + 1; i < json.size(); ++i) {
        const char c = json[i];
        if (c == '\\' && i + 1 < json.size()) {
            const char nxt = json[i + 1];
            switch (nxt) {
                case 'n': out.push_back('\n'); break;
                case 't': out.push_back('\t'); break;
                case 'r': out.push_back('\r'); break;
                case '\"': out.push_back('"'); break;
                case '\\': out.push_back('\\'); break;
                case '/': out.push_back('/'); break;
                case 'u': {
                    // \uXXXX: покрываем латиницу и кириллицу (частый случай в ответах модели)
                    if (i + 5 < json.size()) {
                        unsigned code = 0;
                        bool ok = true;
                        for (int k = 0; k < 4; ++k) {
                            const char h = json[i + 2 + k];
                            unsigned d = 0;
                            if (h >= '0' && h <= '9') d = unsigned(h - '0');
                            else if (h >= 'a' && h <= 'f') d = unsigned(h - 'a' + 10);
                            else if (h >= 'A' && h <= 'F') d = unsigned(h - 'A' + 10);
                            else { ok = false; break; }
                            code = code * 16 + d;
                        }
                        if (ok) {
                            if (code < 0x80) {
                                out.push_back(char(code));
                            } else if (code < 0x800) {
                                out.push_back(char(0xC0 | (code >> 6)));
                                out.push_back(char(0x80 | (code & 0x3F)));
                            } else {
                                out.push_back(char(0xE0 | (code >> 12)));
                                out.push_back(char(0x80 | ((code >> 6) & 0x3F)));
                                out.push_back(char(0x80 | (code & 0x3F)));
                            }
                            i += 5;
                            continue;
                        }
                    }
                    out.push_back(nxt);
                    break;
                }
                default: out.push_back(nxt); break;
            }
            ++i;
            continue;
        }
        if (c == '"') break;
        out.push_back(c);
    }
    return out;
}

int json_get_int(std::string_view json, std::string_view key, int def) {
    const std::string raw = json_get_str(json, key);
    if (raw.empty()) return def;
    try {
        return std::stoi(raw);
    } catch (...) {
        return def;
    }
}

double json_get_num(std::string_view json, std::string_view key, double def) {
    const std::string raw = json_get_str(json, key);
    if (raw.empty()) return def;
    try {
        return std::stod(raw);
    } catch (...) {
        return def;
    }
}

bool json_get_bool(std::string_view json, std::string_view key, bool def) {
    const std::string raw = json_get_str(json, key);
    if (raw.empty()) return def;
    return raw == "true" || raw == "1" || raw == "yes" || raw == "True";
}

int count_items(IPlatform& platform, std::string_view path) {
    int n = 0;
    for (const FileEntry& e : platform.list_dir(path)) {
        ++n;
        if (e.is_dir) {
            for (const FileEntry& inner : platform.list_dir(e.path)) {
                ++n;
                if (n > 5000) return n;
            }
        }
        if (n > 5000) break;
    }
    return n;
}

}  // namespace agent
