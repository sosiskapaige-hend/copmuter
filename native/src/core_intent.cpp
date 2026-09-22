// Intent Engine + App Registry (портативная часть).
#include "agent/intent.h"

#include <algorithm>
#include <cctype>
#include <cstring>

namespace agent {
namespace {

// --------------------------------------------------------------------------
//  UTF-8 helpers: нижний регистр для ASCII и кириллицы без ICU.
// --------------------------------------------------------------------------
struct Rune {
    uint32_t cp;
    uint8_t len;
};

inline Rune decode(std::string_view s, size_t i) {
    const auto c = static_cast<unsigned char>(s[i]);
    if (c < 0x80) return {c, 1};
    if ((c & 0xE0) == 0xC0 && i + 1 < s.size())
        return {static_cast<uint32_t>(((c & 0x1F) << 6) | (s[i + 1] & 0x3F)), 2};
    if ((c & 0xF0) == 0xE0 && i + 2 < s.size())
        return {static_cast<uint32_t>(((c & 0x0F) << 12) | ((s[i + 1] & 0x3F) << 6) |
                                      (s[i + 2] & 0x3F)),
                3};
    if ((c & 0xF8) == 0xF0 && i + 3 < s.size())
        return {static_cast<uint32_t>(((c & 0x07) << 18) | ((s[i + 1] & 0x3F) << 12) |
                                      ((s[i + 2] & 0x3F) << 6) | (s[i + 3] & 0x3F)),
                4};
    return {c, 1};
}

inline void append_utf8(std::string& out, uint32_t cp) {
    if (cp < 0x80) {
        out.push_back(static_cast<char>(cp));
    } else if (cp < 0x800) {
        out.push_back(static_cast<char>(0xC0 | (cp >> 6)));
        out.push_back(static_cast<char>(0x80 | (cp & 0x3F)));
    } else if (cp < 0x10000) {
        out.push_back(static_cast<char>(0xE0 | (cp >> 12)));
        out.push_back(static_cast<char>(0x80 | ((cp >> 6) & 0x3F)));
        out.push_back(static_cast<char>(0x80 | (cp & 0x3F)));
    } else {
        out.push_back(static_cast<char>(0xF0 | (cp >> 18)));
        out.push_back(static_cast<char>(0x80 | ((cp >> 12) & 0x3F)));
        out.push_back(static_cast<char>(0x80 | ((cp >> 6) & 0x3F)));
        out.push_back(static_cast<char>(0x80 | (cp & 0x3F)));
    }
}

inline uint32_t cyrillic_lower(uint32_t cp) {
    // А-Я → а-я; Ё → е сразу (нормализация).
    if (cp >= 0x410 && cp <= 0x42F) return cp + 0x20;
    if (cp == 0x401) return 0x435;             // Ё → е
    if (cp == 0x451) return 0x435;             // ё → е
    return cp;
}

inline int strnicmp_local(const char* a, const char* b, size_t n) {
    for (size_t i = 0; i < n; ++i) {
        const char ca = static_cast<char>(std::tolower(static_cast<unsigned char>(a[i])));
        const char cb = static_cast<char>(std::tolower(static_cast<unsigned char>(b[i])));
        if (ca != cb) return ca < cb ? -1 : 1;
    }
    return 0;
}

const char* kTranslitTable[][2] = {
    {"а", "a"}, {"б", "b"}, {"в", "v"}, {"г", "g"}, {"д", "d"}, {"е", "e"}, {"ж", "zh"},
    {"з", "z"}, {"и", "i"}, {"й", "y"}, {"к", "k"}, {"л", "l"}, {"м", "m"}, {"н", "n"},
    {"о", "o"}, {"п", "p"}, {"р", "r"}, {"с", "s"}, {"т", "t"}, {"у", "u"}, {"ф", "f"},
    {"х", "h"}, {"ц", "c"}, {"ч", "ch"}, {"ш", "sh"}, {"щ", "sch"}, {"ъ", ""}, {"ы", "y"},
    {"ь", ""}, {"э", "e"}, {"ю", "yu"}, {"я", "ya"},
};

}  // namespace

std::string normalize(std::string_view text) {
    std::string out;
    out.reserve(text.size());
    bool prev_space = true;
    for (size_t i = 0; i < text.size();) {
        Rune r = decode(text, i);
        i += r.len;
        uint32_t cp = r.cp;
        if (cp >= 'A' && cp <= 'Z') cp += 32;
        cp = cyrillic_lower(cp);
        const bool alnum = (cp >= 'a' && cp <= 'z') || (cp >= '0' && cp <= '9') ||
                           (cp >= 0x430 && cp <= 0x44F) || cp == '+' || cp == '#' ||
                           cp == '.' || cp == '_' || cp == '-' || cp == '/' || cp == ':' ||
                           cp == '\\' || cp == '%';
        if (!alnum) {
            if (!prev_space) {
                out.push_back(' ');
                prev_space = true;
            }
            continue;
        }
        append_utf8(out, cp);
        prev_space = false;
    }
    while (!out.empty() && out.back() == ' ') out.pop_back();
    return out;
}

void normalize_into(std::string_view text, std::string& out) {
    out.assign(normalize(text));
}

std::vector<std::string_view> tokens(std::string_view normalized) {
    std::vector<std::string_view> out;
    size_t start = 0;
    while (start < normalized.size()) {
        size_t sp = normalized.find(' ', start);
        if (sp == std::string_view::npos) sp = normalized.size();
        if (sp > start) out.push_back(normalized.substr(start, sp - start));
        start = sp + 1;
    }
    return out;
}

bool find_phrase_stemmed(std::string_view text, std::string_view phrase, size_t* pos) {
    const std::vector<std::string_view> tw = tokens(text);
    const std::vector<std::string_view> pw = tokens(phrase);
    if (tw.empty() || pw.empty() || pw.size() > tw.size()) return false;
    for (size_t i = 0; i + pw.size() <= tw.size(); ++i) {
        bool ok = true;
        for (size_t j = 0; j < pw.size(); ++j) {
            if (stem(tw[i + j]) != stem(pw[j])) {
                ok = false;
                break;
            }
        }
        if (!ok) continue;
        if (pos) *pos = static_cast<size_t>(tw[i].data() - text.data());
        return true;
    }
    return false;
}

bool same_phrase_stemmed(std::string_view a, std::string_view b) {
    const std::vector<std::string_view> ta = tokens(a);
    const std::vector<std::string_view> tb = tokens(b);
    if (ta.empty() || ta.size() != tb.size()) return false;
    for (size_t i = 0; i < ta.size(); ++i)
        if (stem(ta[i]) != stem(tb[i])) return false;
    return true;
}

bool starts_with_word(std::string_view text, std::string_view word) {
    if (text.size() < word.size()) return false;
    if (text.compare(0, word.size(), word) != 0) return false;
    return text.size() == word.size() || text[word.size()] == ' ';
}

std::string_view stem(std::string_view w) {
    if (w.size() <= 4) return w;
    static const char* kEndings[] = {"иями", "ями", "ами", "ого", "его", "ому", "ему",
                                     "ать", "ять", "еть", "ить", "ешь", "ишь", "ете",
                                     "ите", "ует", "ает", "ием", "ии", "ий", "ый", "ой",
                                     "ая", "яя", "ое", "ее", "ов", "ев", "ей", "ам",
                                     "ям", "ах", "ях", "ом", "ем", "ы", "и", "а", "я",
                                     "о", "е", "у", "ю", "ь"};
    for (const char* e : kEndings) {
        const size_t n = std::strlen(e);
        if (w.size() > n + 3 && w.compare(w.size() - n, n, e) == 0) {
            return w.substr(0, w.size() - n);
        }
    }
    return w;
}

size_t levenshtein(std::string_view a, std::string_view b, size_t limit) {
    if (a == b) return 0;
    if (a.empty()) return b.size();
    if (b.empty()) return a.size();
    if (a.size() > b.size()) std::swap(a, b);
    if (b.size() - a.size() > limit) return limit + 1;
    std::vector<size_t> prev(a.size() + 1), cur(a.size() + 1);
    for (size_t i = 0; i <= a.size(); ++i) prev[i] = i;
    for (size_t j = 1; j <= b.size(); ++j) {
        cur[0] = j;
        size_t row_min = cur[0];
        for (size_t i = 1; i <= a.size(); ++i) {
            const size_t cost = (a[i - 1] == b[j - 1]) ? 0 : 1;
            cur[i] = std::min({prev[i] + 1, cur[i - 1] + 1, prev[i - 1] + cost});
            row_min = std::min(row_min, cur[i]);
        }
        if (row_min > limit) return limit + 1;
        std::swap(prev, cur);
    }
    return prev[a.size()];
}

double similarity(std::string_view a, std::string_view b) {
    if (a.empty() || b.empty()) return 0.0;
    if (a == b) return 1.0;
    const std::string_view sa = stem(a), sb = stem(b);
    if (sa == sb && sa.size() >= 4) return 0.97;
    if (sa.size() >= 5 && sb.size() >= 5 &&
        (sa.compare(0, 5, sb.substr(0, 5)) == 0))
        return 0.9;
    const size_t dist = levenshtein(a, b, 4);
    const size_t max_len = std::max(a.size(), b.size());
    if (dist > 4 || max_len == 0) return 0.0;
    return 1.0 - double(dist) / double(max_len);
}

std::string translit(std::string_view ru) {
    std::string out;
    out.reserve(ru.size());
    for (size_t i = 0; i < ru.size();) {
        Rune r = decode(ru, i);
        i += r.len;
        if (r.cp < 0x80) {
            out.push_back(static_cast<char>(r.cp));
            continue;
        }
        bool matched = false;
        for (const auto& pair : kTranslitTable) {
            std::string_view src(pair[0]);
            Rune sr = decode(src, 0);
            if (sr.cp == r.cp) {
                out += pair[1];
                matched = true;
                break;
            }
        }
        if (!matched) append_utf8(out, r.cp);
    }
    return out;
}

bool looks_like_url(std::string_view s) {
    if (s.find("://") != std::string_view::npos) return true;
    if (s.rfind("www.", 0) == 0) return true;
    std::string n = normalize(s);
    static const char* kTlds[] = {".ru", ".com", ".org", ".net", ".io", ".dev", ".рф",
                                  ".google", ".youtube"};
    for (const char* t : kTlds)
        if (n.size() > std::strlen(t) && n.compare(n.size() - std::strlen(t), std::strlen(t), t) == 0)
            return true;
    return false;
}

bool looks_like_path(std::string_view s) {
    return s.find('/') != std::string_view::npos || s.find('\\') != std::string_view::npos ||
           s.find(':') != std::string_view::npos;
}

bool looks_like_file(std::string_view s) {
    const size_t dot = s.rfind('.');
    if (dot == std::string_view::npos || dot + 1 >= s.size()) return false;
    if (dot == 0) return false;
    const std::string_view ext = s.substr(dot + 1);
    if (ext.size() > 5) return false;
    for (char c : ext)
        if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9')))
            return false;
    static const char* kExts[] = {".txt", ".md", ".py", ".js", ".ts", ".json", ".csv",
                                  ".log", ".png", ".jpg", ".pdf", ".docx", ".xlsx",
                                  ".html", ".css", ".cpp", ".h", ".cs", ".rs", ".go",
                                  ".java", ".sh", ".bat", ".ps1", ".exe", ".lnk"};
    for (const char* e : kExts) {
        const size_t n = std::strlen(e);
        if (s.size() >= n && strnicmp_local(s.data() + s.size() - n, e, n) == 0) return true;
    }
    return false;
}

bool looks_like_number(std::string_view s) {
    if (s.empty() || s.size() > 3) return false;
    return std::all_of(s.begin(), s.end(), [](char c) { return c >= '0' && c <= '9'; });
}

bool is_question(std::string_view phrase) {
    std::string n = normalize(phrase);
    if (n.empty()) return false;
    if (n.back() == '?') return true;
    static const char* kStart[] = {"что", "как", "почему", "зачем", "когда", "где", "кто",
                                   "сколько", "какой", "какая", "какие", "чем", "можно ли",
                                   "расскажи", "объясни", "подскажи", "помоги"};
    for (const char* w : kStart)
        if (starts_with_word(n, w)) return true;
    return false;
}

bool is_complex(std::string_view phrase) {
    std::string n = normalize(phrase);
    static const char* kMarkers[] = {"проанализируй", "разберись", "исследуй", "сравни",
                                     "составь план", "разложи", "организуй", "настрой",
                                     "установи", "скачай", "разверн", "почини", "исправь",
                                     "затем", "после этого", "потом", "шаг за шагом",
                                     "автоматизируй", "проверь и"};
    for (const char* m : kMarkers)
        if (n.find(m) != std::string::npos) return true;
    // проект/сайт/приложение = задача разработки
    static const char* kNouns[] = {"сайт", "проект", "приложение", "бота", "бота", "парсер",
                                   "скрипт", "калькулятор", "игру"};
    for (const char* w : kNouns) {
        const size_t pos = n.find(w);
        if (pos != std::string::npos && (pos == 0 || n[pos - 1] == ' ')) return true;
    }
    return false;
}

// ---------------------------------------------------------------------------
//  AppRegistry
// ---------------------------------------------------------------------------
void AppRegistry::add(AppInfo app) {
    index_[app.key] = apps_.size();
    for (const std::string& a : app.aliases) {
        const std::string key = normalize(a);
        if (!key.empty()) alias_[key] = app.key;
    }
    const std::string norm_name = normalize(app.display_name);
    if (!norm_name.empty()) alias_[norm_name] = app.key;
    if (app.kind == "settings" && !app.protocol.empty()) alias_[normalize(app.key)] = app.key;
    apps_.push_back(std::move(app));
}

AppInfo* AppRegistry::get(std::string_view key) {
    auto it = index_.find(std::string(key));
    return it == index_.end() ? nullptr : &apps_[it->second];
}

const AppInfo* AppRegistry::get(std::string_view key) const {
    auto it = index_.find(std::string(key));
    return it == index_.end() ? nullptr : &apps_[it->second];
}

AppRegistry::Lookup AppRegistry::find(std::string_view phrase) {
    Lookup out;
    const std::string n = normalize(phrase);
    if (n.empty()) return out;
    // 1) точный алиас
    auto it = alias_.find(n);
    if (it != alias_.end()) {
        out.app = get(it->second);
        out.score = 1.0f;
        out.matched = "alias";
        return out;
    }
    // 2) стем-совпадение ("телеграм" → "телеграмма"?), префикс слова
    for (const auto& [alias, key] : alias_) {
        if (alias.size() < 4) continue;
        if (alias.rfind(n, 0) == 0 || n.rfind(alias, 0) == 0) {
            if (!out.app || alias.size() > out.matched.size()) {
                out.app = get(key);
                out.score = 0.93f;
                out.matched = alias;
            }
        }
    }
    if (out.app) return out;
    // 3) нечёткое сравнение по словам фразы (короткие команды — 1–3 слова)
    const auto words = tokens(n);
    const size_t limit = words.size() > 3 ? 3 : words.size();
    for (size_t w = 0; w < limit; ++w) {
        const std::string_view word = words[w];
        if (word.size() < 4) continue;
        for (const auto& [alias, key] : alias_) {
            if (alias.empty()) continue;
            if (alias.front() != word.front()) continue;      // быстрый отсев
            const double s = similarity(word, alias);
            if (s > out.score && s >= 0.72) {
                out.app = get(key);
                out.score = float(s);
                out.matched = alias;
            }
        }
        if (out.score >= 0.9) break;
    }
    return out;
}

std::vector<std::pair<const AppInfo*, float>> AppRegistry::suggest(std::string_view phrase,
                                                                   size_t limit) const {
    std::vector<std::pair<const AppInfo*, float>> out;
    const std::string n = normalize(phrase);
    if (n.empty()) return out;
    const auto words = tokens(n);
    const std::string_view first = words.empty() ? std::string_view() : words.front();
    for (const AppInfo& a : apps_) {
        float best = 0.0f;
        for (const std::string& alias_raw : a.aliases) {
            const std::string alias = normalize(alias_raw);
            if (alias.empty()) continue;
            if (!first.empty() && alias.front() != first.front()) continue;
            best = std::max(best, float(similarity(first.empty() ? n : first, alias)));
        }
        if (best >= 0.5f) out.emplace_back(&a, best);
    }
    std::sort(out.begin(), out.end(), [](const auto& x, const auto& y) { return x.second > y.second; });
    if (out.size() > limit) out.resize(limit);
    return out;
}

void AppRegistry::learn_alias(std::string_view phrase, std::string_view key) {
    const std::string n = normalize(phrase);
    if (n.size() < 3) return;
    alias_[n] = std::string(key);
    if (AppInfo* a = get(key)) {
        if (std::find(a->aliases.begin(), a->aliases.end(), n) == a->aliases.end())
            a->aliases.push_back(n);
    }
}

void AppRegistry::note_use(std::string_view key, bool ok, double ms, Method m) {
    if (AppInfo* a = get(key)) {
        ++a->uses;
        ok ? ++a->ok_count : ++a->fail_count;
        a->avg_ms = a->avg_ms * 0.7 + ms * 0.3;
        if (ok) a->last_method = m;
    }
}

void AppRegistry::set_path(std::string_view key, std::string path, bool installed) {
    if (AppInfo* a = get(key)) {
        a->path = std::move(path);
        a->installed = installed;
        a->verified_at_ms = now_ms();
    }
}

std::vector<const AppInfo*> AppRegistry::all() const {
    std::vector<const AppInfo*> out;
    out.reserve(apps_.size());
    for (const AppInfo& a : apps_) out.push_back(&a);
    return out;
}

std::vector<const AppInfo*> AppRegistry::installed() const {
    std::vector<const AppInfo*> out;
    for (const AppInfo& a : apps_)
        if (a.installed) out.push_back(&a);
    return out;
}

std::string AppRegistry::brief_list(size_t limit) const {
    std::string out;
    size_t n = 0;
    for (const AppInfo* a : all()) {
        if (n++ >= limit) break;
        out += "- ";
        out += a->display_name.empty() ? a->key : a->display_name;
        out += " (";
        out += a->key;
        out += ")";
        if (!a->path.empty()) {
            out += " — ";
            out += a->path;
        }
        out += '\n';
    }
    return out;
}

std::string Intent::label() const {
    const std::string_view act = action.view();
    std::string target = slots[static_cast<size_t>(SlotId::Target)].str();
    if (target.empty()) target = slots[static_cast<size_t>(SlotId::Query)].str();
    if (act == "launch_app") return "Открываю " + (target.empty() ? std::string("приложение") : target);
    if (act == "open_url") return "Открываю сайт";
    if (act == "open_folder") return "Открываю папку";
    if (act == "open_path") return "Открываю файл";
    if (act == "web_search") return "Ищу в интернете";
    if (act == "youtube_search") return "Ищу видео";
    if (act == "create_folder") return "Создаю папку " + target;
    if (act == "create_file") return "Создаю файл " + target;
    if (act == "read_file") return "Читаю файл " + target;
    if (act == "delete_path") return "Удаляю " + target;
    if (act == "move_path") return "Перемещаю";
    if (act == "copy_path") return "Копирую";
    if (act == "find_files") return "Ищу файлы";
    if (act == "screenshot") return "Делаю снимок экрана";
    if (act == "set_wallpaper") return "Меняю обои";
    if (act == "volume") return "Меняю громкость";
    if (act == "power") return "Системное действие";
    if (act == "show_desktop") return "Показываю рабочий стол";
    if (act == "send_keys") return "Нажимаю клавиши";
    if (act == "type_text") return "Печатаю текст";
    if (act == "kill_process") return "Закрываю программу";
    if (act == "run_command") return "Выполняю команду";
    if (act == "settings_page") return "Открываю настройки";
    if (act == "compound") return "Выполняю несколько действий";
    if (act == "code_task") return "Готовлю код";
    if (act == "chat") return "Отвечаю";
    return std::string(act);
}

}  // namespace agent
