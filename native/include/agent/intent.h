// Intent Engine + Application Registry (portable, безregex, микросекунды).
//
// Ключевая идея: не пытаться «понять» фразу целиком, а за один проход
//   разобрать её на глагол (что делать) + объект (с чем)
// и сверить объект с таблицами (приложения, места, URL/путь/файл).
// Такой разбор детерминирован, объясним, тестируем и стоит доли микросекунды —
// именно он обслуживает 80–90% команд без единого обращения к модели.
#pragma once

#include <cstdint>
#include <functional>
#include <memory>
#include <string>
#include <string_view>
#include <unordered_map>
#include <vector>

#include "core.h"

namespace agent {

// ---------------------------------------------------------------------------
//  Нормализация текста (UTF-8: ASCII + кириллица, без внешних зависимостей)
// ---------------------------------------------------------------------------
std::string normalize(std::string_view text);       // lower, ё→е, чистка пунктуации
void normalize_into(std::string_view text, std::string& out);
std::vector<std::string_view> tokens(std::string_view normalized);
bool starts_with_word(std::string_view text, std::string_view word);
// «телеграм» → «телеграмм»: простой стеммер для русских окончаний.
std::string_view stem(std::string_view word);
size_t levenshtein(std::string_view a, std::string_view b, size_t limit = 3);
double similarity(std::string_view a, std::string_view b);
std::string translit(std::string_view ru);          // «телега» → «telega»
// Совпадение с учётом русских падежей: «на рабочем столе» ↔ «рабочий стол».
bool find_phrase_stemmed(std::string_view text, std::string_view phrase, size_t* pos = nullptr);
bool same_phrase_stemmed(std::string_view a, std::string_view b);

// ---------------------------------------------------------------------------
//  Приложения
// ---------------------------------------------------------------------------
struct AppInfo {
    std::string key;                 // telegram, vscode, chrome, explorer
    std::string display_name;        // Telegram
    std::string kind = "app";        // app | settings | folder | shell | power | store
    std::vector<std::string> aliases;
    std::vector<std::string> exe;    // имена процессов
    std::string path;                // подтверждённый путь/команда (кеш)
    std::string protocol;            // telegram:, ms-settings:display, shell:...
    std::string appid;               // Store: shell:AppsFolder\...
    std::string args;
    bool installed = false;
    uint32_t uses = 0;
    uint32_t ok_count = 0;
    uint32_t fail_count = 0;
    double avg_ms = 0.0;
    Method last_method = Method::WinApi;
    std::int64_t verified_at_ms = 0;
};

class AppRegistry {
public:
    void add(AppInfo app);
    AppInfo* get(std::string_view key);
    const AppInfo* get(std::string_view key) const;
    // Поиск по алиасу/имени/осколку: точное совпадение → префикс → стем → нечёткий.
    struct Lookup {
        AppInfo* app = nullptr;
        float score = 0.0f;
        std::string matched;
    };
    Lookup find(std::string_view phrase);
    std::vector<std::pair<const AppInfo*, float>> suggest(std::string_view phrase, size_t limit = 5) const;
    void learn_alias(std::string_view phrase, std::string_view key);
    void note_use(std::string_view key, bool ok, double ms, Method m);
    void set_path(std::string_view key, std::string path, bool installed = true);
    std::vector<const AppInfo*> all() const;
    std::vector<const AppInfo*> installed() const;
    size_t size() const { return apps_.size(); }
    // Загрузка/сохранение кеша (JSON). Хранилище — SQLite в Windows-сборке.
    bool load(std::string_view json);
    std::string save() const;
    std::string brief_list(size_t limit = 200) const;

private:
    std::unordered_map<std::string, size_t> index_;
    std::unordered_map<std::string, std::string> alias_;   // нормализованный алиас → key
    std::vector<AppInfo> apps_;
};

// Каталог «известных» приложений (то, что можно распознать до первого запуска).
void add_builtin_apps(AppRegistry& reg);
// Обнаружение установленного ПО (реализуется платформой, см. IPlatform::discover_apps).
int discover_apps_into(AppRegistry& reg);

// ---------------------------------------------------------------------------
//  Движок намерений
// ---------------------------------------------------------------------------
enum class IntentSource : uint8_t { Rule = 0, Registry, Memory, Fallback };

class IntentEngine {
public:
    explicit IntentEngine(AppRegistry* apps = nullptr) : apps_(apps) {}
    void set_apps(AppRegistry* apps) { apps_ = apps; }
    void set_places(const std::vector<std::pair<std::string, std::string>>* places) { places_ = places; }

    Intent parse(std::string_view phrase) const;
    // Разделение составной команды («открой Discord и Telegram», «создай папку и открой её»).
    static std::vector<std::string> split_compound(std::string_view phrase);

    // Память удачных формулировок: «вруби мой плеер» → spotify.
    void remember(std::string_view phrase, std::string_view action, std::string_view target);
    bool recall(std::string_view phrase, Intent& out) const;
    size_t learned() const { return learned_.size(); }

    // Метрики разбора (для бенчмарка и UI).
    struct Stats {
        uint64_t parsed = 0;
        uint64_t memory_hits = 0;
        uint64_t registry_hits = 0;
        uint64_t fallbacks = 0;
        uint64_t compounds = 0;
        double total_us = 0.0;
        double avg_us() const { return parsed ? total_us / double(parsed) : 0.0; }
    };
    const Stats& stats() const { return stats_; }

private:
    Intent parse_single(std::string_view phrase, bool allow_compound) const;
    Intent parse_place_or_path(std::string_view action, std::string_view phrase,
                               std::string_view object) const;
    Intent launch_or_search(std::string_view phrase, std::string_view object, float conf) const;

    AppRegistry* apps_ = nullptr;
    const std::vector<std::pair<std::string, std::string>>* places_ = nullptr;
    struct Learned {
        std::string action;
        std::string target;
    };
    std::unordered_map<std::string, Learned> learned_;
    mutable Stats stats_;
};

// Служебные проверки «формы» объекта.
bool looks_like_url(std::string_view s);
bool looks_like_path(std::string_view s);
bool looks_like_file(std::string_view s);
bool looks_like_number(std::string_view s);
bool is_question(std::string_view phrase);
bool is_complex(std::string_view phrase);

}  // namespace agent
