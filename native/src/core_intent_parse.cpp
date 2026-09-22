// Разбор фразы в намерение: глагол + объект (без regex, микросекунды).
#include <algorithm>
#include <cstring>

#include "agent/intent.h"

namespace agent {

namespace {

// Имя папки/файла — короткое, без «и <глагол>»: «отчёт по проекту и сохрани его» это
// задача для модели, а не имя каталога.
bool clause_like(const std::string& s) {
    if (s.empty()) return true;
    int words = 1;
    for (size_t i = 1; i < s.size(); ++i)
        if (s[i] == ' ' && s[i - 1] != ' ') ++words;
    static const char* kConjVerbs[] = {"и сохрани",  "и напиши",   "и запиши",   "и отправь",
                                       "и прикрепи", "и запусти",  "и открой",   "и добавь",
                                       "и расскажи", "и сделай",   "и создай",   "и удали",
                                       "и переименуй", "и переведи", "а потом",   "затем "};
    for (const char* c : kConjVerbs)
        if (s.find(c) != std::string::npos) return true;
    // «сделай что-нибудь с проектом» — задача, а не имя каталога.
    static const char* kVague[] = {"что-нибудь", "что-то", "всякое", "кое-что", "как-нибудь"};
    for (const char* v : kVague)
        if (s.find(v) != std::string::npos) return true;
    return words > 5;
}

}  // namespace

namespace {

enum class Verb : uint8_t {
    Unknown = 0, Launch, Open, CreateFolder, CreateFile, Delete, Read, List, Move, Copy,
    SearchWeb, SearchVideo, Screenshot, Wallpaper, Volume, Power, Keys, Type, Kill, Run,
    Settings, FindFiles, Code, ShowDesktop, Focus, ClickElement,
};

struct VerbRule {
    const char* word;
    Verb verb;
};

// Глаголы в повелительном наклонении и инфинитиве (нормализованные).
const VerbRule kVerbs[] = {
    {"открой", Verb::Open},        {"открыть", Verb::Open},
    {"запусти", Verb::Launch},     {"запустить", Verb::Launch},
    {"включи", Verb::Launch},      {"вруби", Verb::Launch},
    {"стартани", Verb::Launch},    {"активируй", Verb::Launch},
    {"переключись на", Verb::Focus}, {"переключись", Verb::Focus},
    {"переключи", Verb::Focus},      {"сфокусируйся на", Verb::Focus},
    {"сфокусируйся", Verb::Focus},   {"перейди на", Verb::Focus},
    {"выйди на", Verb::Focus},       {"активируй окно", Verb::Focus},
    {"переключи окно", Verb::Focus}, {"переключись в", Verb::Focus},
    {"найди", Verb::SearchWeb},    {"поищи", Verb::SearchWeb},
    {"погугли", Verb::SearchWeb},  {"загугли", Verb::SearchWeb},
    {"создай", Verb::CreateFolder},{"создать", Verb::CreateFolder},
    {"сделай", Verb::CreateFolder},{"создай папку", Verb::CreateFolder},
    {"удали", Verb::Delete},       {"удалить", Verb::Delete},
    {"снеси", Verb::Delete},       {"стереть", Verb::Delete},
    {"прочитай", Verb::Read},      {"покажи содержимое", Verb::Read},
    {"покажи", Verb::List},        {"перечисли", Verb::List},
    {"перемести", Verb::Move},     {"перенеси", Verb::Move},
    {"скопируй", Verb::Copy},      {"копируй", Verb::Copy},
    {"сделай скриншот", Verb::Screenshot}, {"сними", Verb::Screenshot},
    {"скриншот", Verb::Screenshot},{"снимок", Verb::Screenshot},
    {"поменяй обои", Verb::Wallpaper}, {"смени обои", Verb::Wallpaper},
    {"поставь обои", Verb::Wallpaper},
    {"громкость", Verb::Volume},   {"громче", Verb::Volume}, {"тише", Verb::Volume},
    {"звук", Verb::Volume},
    {"выключи", Verb::Power},      {"перезагрузи", Verb::Power},
    {"заблокируй", Verb::Power},   {"усыпи", Verb::Power},
    // «нажми кнопку ОК» — это клик по элементу (зрение/UIA), а не нажатие клавиши
    // с именем «кнопку ок»: самая частая ошибка разбора в таких фразах.
    {"нажми кнопку", Verb::ClickElement}, {"нажми на кнопку", Verb::ClickElement},
    {"нажми на", Verb::ClickElement},     {"кликни по", Verb::ClickElement},
    {"кликни", Verb::ClickElement},       {"щелкни по", Verb::ClickElement},
    {"щелкни", Verb::ClickElement},       {"ткни в", Verb::ClickElement},
    {"нажми", Verb::Keys},         {"жми", Verb::Keys},
    {"напечатай", Verb::Type},     {"введи", Verb::Type}, {"напиши", Verb::Type},
    {"закрой", Verb::Kill},        {"убей", Verb::Kill}, {"останови", Verb::Kill},
    {"выполни", Verb::Run},        {"запусти команду", Verb::Run},
    {"запусти в терминале", Verb::Run},
    {"настрой", Verb::Settings},   {"открой настройки", Verb::Settings},
    {"ищи файл", Verb::FindFiles}, {"найди файл", Verb::FindFiles},
    {"найди файлы", Verb::FindFiles},
    {"покажи рабочий стол", Verb::ShowDesktop}, {"сверни всё", Verb::ShowDesktop},
};

// Сайты, на которые ведёт «открой ютуб».
struct SiteRule {
    const char* alias;
    const char* url;
};

const SiteRule kSites[] = {
    {"ютуб", "https://youtube.com"},        {"youtube", "https://youtube.com"},
    {"гугл", "https://google.com"},         {"google", "https://google.com"},
    {"яндекс", "https://ya.ru"},            {"yandex", "https://ya.ru"},
    {"вк", "https://vk.com"},               {"вконтакте", "https://vk.com"},
    {"github", "https://github.com"},       {"гитхаб", "https://github.com"},
    {"wikipedia", "https://ru.wikipedia.org"}, {"википедия", "https://ru.wikipedia.org"},
    {"почта", "https://mail.google.com"},   {"gmail", "https://mail.google.com"},
    {"карты", "https://maps.google.com"},   {"карта", "https://maps.google.com"},
    {"twitch", "https://twitch.tv"},        {"твич", "https://twitch.tv"},
    {"ozon", "https://ozon.ru"},            {"озон", "https://ozon.ru"},
    {"авито", "https://avito.ru"},          {"avito", "https://avito.ru"},
};

const char* kYoutubeWords[] = {"ютуб", "youtube", "ютюб", "ютьюб", "ютубе"};

bool contains_word(std::string_view text, const char* word) {
    const size_t pos = text.find(word);
    if (pos == std::string_view::npos) return false;
    const bool left_ok = (pos == 0) || text[pos - 1] == ' ';
    const size_t end = pos + std::strlen(word);
    const bool right_ok = (end >= text.size()) || text[end] == ' ';
    return left_ok && right_ok;
}

Verb verb_of(std::string_view word) {
    for (const VerbRule& r : kVerbs) {
        if (word == r.word) return r.verb;
    }
    return Verb::Unknown;
}

// Регистр «системных» объектов (питание/показ рабочего стола).
bool object_is_system(std::string_view object, const char* const* words, size_t n) {
    for (size_t i = 0; i < n; ++i) {
        if (contains_word(object, words[i])) return true;
    }
    return false;
}

std::string strip_prefixes(std::string n) {
    static const char* kPrefix[] = {"пожалуйста", "агент", "мне", "быстро", "сейчас",
                                    "немедленно", "срочно", "плиз"};
    bool changed = true;
    while (changed) {
        changed = false;
        for (const char* p : kPrefix) {
            if (starts_with_word(n, p)) {
                n.erase(0, std::strlen(p));
                while (!n.empty() && n.front() == ' ') n.erase(0, 1);
                changed = true;
                break;
            }
        }
    }
    return n;
}

std::string strip_tail_filler(std::string n) {
    static const char* kTail[] = {"пожалуйста", "плиз", "please", "сейчас", "быстро",
                                  "по имени", "через веб-интерфейс", "через терминал",
                                  "с помощью агента", "при помощи агента"};
    bool changed = true;
    while (changed) {
        changed = false;
        for (const char* t : kTail) {
            const size_t tl = std::strlen(t);
            if (n.size() > tl && n.compare(n.size() - tl, tl, t) == 0 &&
                (n.size() == tl || n[n.size() - tl - 1] == ' ')) {
                n.erase(n.size() - tl);
                while (!n.empty() && n.back() == ' ') n.pop_back();
                changed = true;
                break;
            }
        }
    }
    return n;
}

// «найди видео про котиков на ютубе» → «котиков»
std::string clean_query(std::string q) {
    static const char* kStart[] = {"видео", "ролик", "ролики", "про", "о", "об", "по",
                                   "насчёт", "насчет", "в интернете", "в сети", "мне"};
    bool changed = true;
    while (changed) {
        changed = false;
        for (const char* w : kStart) {
            if (starts_with_word(q, w)) {
                q.erase(0, std::strlen(w));
                while (!q.empty() && q.front() == ' ') q.erase(0, 1);
                changed = true;
                break;
            }
        }
    }
    static const char* kTail[] = {"на ютубе", "в ютубе", "на youtube", "в youtube",
                                  "ютубе", "ютуб", "youtube", "в интернете", "в сети",
                                  "в гугле", "в яндексе"};
    changed = true;
    while (changed) {
        changed = false;
        for (const char* w : kTail) {
            const size_t wl = std::strlen(w);
            if (q.size() > wl && q.compare(q.size() - wl, wl, w) == 0 &&
                (q.size() == wl || q[q.size() - wl - 1] == ' ')) {
                q.erase(q.size() - wl);
                while (!q.empty() && q.back() == ' ') q.pop_back();
                changed = true;
                break;
            }
        }
    }
    // «найди на ютубе» → пустой запрос
    for (const char* w : kYoutubeWords)
        if (q == w) return std::string();
    return q;
}

const char* site_url(const std::string& object) {
    for (const SiteRule& s : kSites) {
        if (contains_word(object, s.alias)) return s.url;
    }
    return nullptr;
}

}  // namespace

// ---------------------------------------------------------------------------
//  Составная команда
// ---------------------------------------------------------------------------
std::vector<std::string> IntentEngine::split_compound(std::string_view phrase) {
    static const char* kSeps[] = {" и ", " затем ", " потом ", ", ", "; ", " после этого "};
    std::vector<std::string> parts{normalize(phrase)};

    auto trim_inplace = [](std::string& t) {
        while (!t.empty() && t.front() == ' ') t.erase(0, 1);
        while (!t.empty() && t.back() == ' ') t.pop_back();
    };

    for (const char* sep : kSeps) {
        std::vector<std::string> next;
        for (const std::string& p : parts) {
            const size_t at = p.find(sep);
            if (at == std::string::npos) {
                next.push_back(p);
                continue;
            }
            std::string left = p.substr(0, at);
            std::string right = p.substr(at + std::strlen(sep));
            trim_inplace(left);
            trim_inplace(right);
            const auto rw = tokens(right);
            const auto lw = tokens(left);
            const bool right_has_verb = !rw.empty() && verb_of(rw.front()) != Verb::Unknown;
            const bool left_has_verb = !lw.empty() && verb_of(lw.front()) != Verb::Unknown;
            if (right_has_verb) {
                // «открой дискорд и запусти телегу» — две команды
                if (!left.empty()) next.push_back(left);
                next.push_back(right);
            } else if (left_has_verb && rw.size() <= 2 &&
                       (verb_of(lw.front()) == Verb::Open || verb_of(lw.front()) == Verb::Launch ||
                        verb_of(lw.front()) == Verb::Kill)) {
                // «открой Discord и Telegram» — тот же глагол для второго объекта
                if (!left.empty()) next.push_back(left);
                next.push_back(std::string(lw.front()) + " " + right);
            } else {
                next.push_back(p);
            }
        }
        parts.swap(next);
        if (parts.size() > 4) break;      // защита от «и...и...и»
    }
    std::vector<std::string> out;
    for (std::string p : parts) {
        trim_inplace(p);
        if (!p.empty()) out.push_back(p);
    }
    return out;
}

// ---------------------------------------------------------------------------
//  Память удачных формулировок
// ---------------------------------------------------------------------------
void IntentEngine::remember(std::string_view phrase, std::string_view action,
                            std::string_view target) {
    const std::string key = normalize(phrase);
    if (key.size() < 4) return;
    learned_[key] = Learned{std::string(action), std::string(target)};
}

bool IntentEngine::recall(std::string_view phrase, Intent& out) const {
    auto it = learned_.find(normalize(phrase));
    if (it == learned_.end()) return false;
    out.action = it->second.action;
    out.raw = phrase;
    out.set(SlotId::Target, it->second.target);
    out.set(SlotId::Query, it->second.target);
    out.confidence = 0.99f;
    out.source = static_cast<uint8_t>(IntentSource::Memory);
    ++stats_.memory_hits;
    return true;
}

// ---------------------------------------------------------------------------
//  Разбор одной команды
// ---------------------------------------------------------------------------
Intent IntentEngine::launch_or_search(std::string_view phrase, std::string_view object,
                                      float conf) const {
    Intent it;
    it.raw = phrase;
    it.confidence = conf;
    it.source = static_cast<uint8_t>(IntentSource::Rule);
    const std::string obj(object);

    if (const char* url = site_url(obj)) {
        it.action = "open_url";
        it.set(SlotId::Url, url);
        return it;
    }
    if (looks_like_url(obj)) {
        it.action = "open_url";
        it.set(SlotId::Url, obj);
        return it;
    }
    if (apps_) {
        AppRegistry::Lookup hit = apps_->find(obj);
        if (hit.app && hit.score >= 0.72f) {
            it.action = "launch_app";
            it.set(SlotId::Target, hit.app->key);   // канонический ключ приложения
            it.set(SlotId::Args, hit.app->args);
            it.app_index = 0;
            it.confidence = std::min(0.99f, conf * hit.score + 0.15f);
            it.source = static_cast<uint8_t>(IntentSource::Registry);
            return it;
        }
    }
    if (looks_like_file(obj) || looks_like_path(obj)) {
        it.action = "open_path";
        it.set(SlotId::Target, obj);
        return it;
    }
    // неизвестное имя: пусть платформа попробует оболочку/путь, иначе — модель
    it.action = "launch_app";
    it.set(SlotId::Target, obj);
    it.confidence = conf * 0.7f;
    return it;
}

Intent IntentEngine::parse_place_or_path(std::string_view action, std::string_view phrase,
                                         std::string_view object) const {
    Intent it;
    it.raw = phrase;
    it.action = action;
    it.confidence = 0.9f;
    it.source = static_cast<uint8_t>(IntentSource::Rule);
    const std::string obj(object);
    const std::string norm = normalize(obj);

    // «удали папку 123 с рабочего стола»
    if (places_) {
        for (const auto& [label, path] : *places_) {
            size_t pos = 0;
            if (!find_phrase_stemmed(norm, label, &pos)) continue;
            it.set(SlotId::Place, label);
            std::string name = norm.substr(0, pos);
            // «удали папку 123 с рабочего стола» → цель «123», а не «123 с»
            static const char* kStopTail[] = {"с", "из", "на", "в", "к", "по", "для", "и", "а"};
            std::string cut;
            {
                std::vector<std::string_view> words = tokens(name);
                while (!words.empty()) {
                    const std::string_view w = words.back();
                    bool stop = false;
                    for (const char* t : kStopTail)
                        if (w == t) stop = true;
                    if (!stop) break;
                    words.pop_back();
                }
                for (size_t i = 0; i < words.size(); ++i) {
                    if (i) cut += ' ';
                    cut.append(words[i].data(), words[i].size());
                }
            }
            name = cut;
            static const char* kPre[] = {"папку ", "папка ", "каталог ", "директорию ", "файл ",
                                         "файлы ", "документ ", "из папки ", "с папки "};
            for (const char* p : kPre) {
                if (starts_with_word(name, p)) {
                    name.erase(0, std::strlen(p));
                }
            }
            if (!name.empty()) it.set(SlotId::Target, name);
            return it;
        }
    }
    it.set(SlotId::Target, obj);
    return it;
}

Intent IntentEngine::parse_single(std::string_view phrase, bool allow_compound) const {
    const double t0 = now_us();
    ++stats_.parsed;

    Intent it;
    it.raw = phrase;
    it.action = "agent_task";
    it.confidence = 0.3f;
    it.source = static_cast<uint8_t>(IntentSource::Fallback);

    std::string n = strip_prefixes(normalize(phrase));
    if (n.empty()) {
        stats_.total_us += now_us() - t0;
        return it;
    }

    // 1) память (заученная формулировка)
    if (recall(phrase, it)) {
        stats_.total_us += now_us() - t0;
        return it;
    }

    // Глагол — самое длинное совпадение с начала фразы: «сделай скриншот» должна
    // выигрывать у «сделай», «открой настройки дисплея» — у «открой».
    Verb verb = Verb::Unknown;
    size_t verb_len = 0;
    for (const VerbRule& r : kVerbs) {
        const size_t rl = std::strlen(r.word);
        if (rl <= verb_len) continue;
        if (starts_with_word(n, r.word)) {
            verb = r.verb;
            verb_len = rl;
        }
    }

    std::string object = n;
    if (verb != Verb::Unknown) {
        object = n.substr(verb_len);
        while (!object.empty() && object.front() == ' ') object.erase(0, 1);
    } else if (starts_with_word(n, "громкость") || starts_with_word(n, "звук")) {
        verb = Verb::Volume;
        object = n.substr(n.find(' ') == std::string::npos ? n.size() : n.find(' ') + 1);
    } else if (starts_with_word(n, "скриншот") || starts_with_word(n, "снимок экрана")) {
        verb = Verb::Screenshot;
        object.clear();
    }
    object = strip_tail_filler(object);

    const std::string obj_norm = normalize(object);

    switch (verb) {
        case Verb::Launch:
        case Verb::Open: {
            // «открой загрузки» / «открой рабочий стол» — сама фраза называет место
            if (places_ && !obj_norm.empty()) {
                for (const auto& [label, path] : *places_) {
                    if (same_phrase_stemmed(obj_norm, label)) {
                        it.action = "open_folder";
                        it.set(SlotId::Place, label);
                        it.confidence = 0.93f;
                        stats_.total_us += now_us() - t0;
                        return it;
                    }
                }
            }
            // «открой папку загрузки» / «открой файл notes.txt»
            static const char* kFolderPre[] = {"папку", "папка", "каталог", "директорию",
                                               "folder"};
            static const char* kFilePre[] = {"файл", "файлы", "документ"};
            for (const char* p : kFolderPre) {
                if (starts_with_word(obj_norm, p)) {
                    std::string inner = obj_norm.substr(std::strlen(p));
                    while (!inner.empty() && inner.front() == ' ') inner.erase(0, 1);
                    if (places_ && !inner.empty()) {
                        for (const auto& [label, path] : *places_) {
                            if (starts_with_word(inner, label) || inner == label) {
                                it.action = "open_folder";
                                it.set(SlotId::Place, label);
                                it.confidence = 0.93f;
                                stats_.total_us += now_us() - t0;
                                return it;
                            }
                        }
                    }
                    Intent sub = parse_place_or_path("open_folder", phrase, inner);
                    sub.raw = phrase;
                    stats_.total_us += now_us() - t0;
                    return sub;
                }
            }
            for (const char* p : kFilePre) {
                if (starts_with_word(obj_norm, p)) {
                    std::string inner = obj_norm.substr(std::strlen(p));
                    while (!inner.empty() && inner.front() == ' ') inner.erase(0, 1);
                    Intent sub = parse_place_or_path("open_path", phrase, inner);
                    sub.raw = phrase;
                    stats_.total_us += now_us() - t0;
                    return sub;
                }
            }
            // «открой проводник» → реестр приложений (explorer)
            if (apps_) {
                AppRegistry::Lookup hit = apps_->find(obj_norm);
                if (hit.app && hit.score >= 0.8f) {
                    it.action = "launch_app";
                    it.set(SlotId::Target, hit.app->key);   // канонический ключ: telegram, vscode…
                    it.app_index = 0;
                    it.confidence = std::min(0.99f, 0.8f + hit.score * 0.2f);
                    it.source = static_cast<uint8_t>(IntentSource::Registry);
                    stats_.registry_hits++;
                    stats_.total_us += now_us() - t0;
                    return it;
                }
            }
            Intent sub = launch_or_search(phrase, obj_norm, verb == Verb::Launch ? 0.9f : 0.88f);
            sub.raw = phrase;
            if (sub.action.view() == "launch_app" && sub.source ==
                    static_cast<uint8_t>(IntentSource::Rule)) {
                ++stats_.fallbacks;
            }
            stats_.total_us += now_us() - t0;
            return sub;
        }
        case Verb::SearchWeb:
        case Verb::SearchVideo: {
            const bool youtube = verb == Verb::SearchVideo ||
                                 object_is_system(obj_norm, kYoutubeWords,
                                                  sizeof(kYoutubeWords) / sizeof(char*));
            std::string q = clean_query(obj_norm);
            if (q.empty()) {
                // «найди видео на ютубе» без запроса — открываем сам сервис
                Intent site = launch_or_search(phrase, youtube ? "ютуб" : "гугл", 0.85f);
                site.raw = phrase;
                stats_.total_us += now_us() - t0;
                return site;
            }
            it.action = youtube ? "youtube_search" : "web_search";
            it.set(SlotId::Query, q);
            it.confidence = 0.95f;
            it.source = static_cast<uint8_t>(IntentSource::Rule);
            stats_.total_us += now_us() - t0;
            return it;
        }
        case Verb::CreateFolder:
        case Verb::CreateFile:
        case Verb::Delete:
        case Verb::Read:
        case Verb::List:
        case Verb::Move:
        case Verb::Copy:
        case Verb::FindFiles: {
            static const char* kFolderPre[] = {"папку", "папка", "каталог", "директорию",
                                               "folder"};
            static const char* kFilePre[] = {"файл", "файлы", "документ"};
            std::string inner = obj_norm;
            std::string_view action = "create_folder";
            switch (verb) {
                case Verb::CreateFolder:
                    for (const char* p : kFolderPre)
                        if (starts_with_word(inner, p)) {
                            inner.erase(0, std::strlen(p));
                            while (!inner.empty() && inner.front() == ' ') inner.erase(0, 1);
                            break;
                        }
                    if (starts_with_word(inner, "файл")) {
                        action = "create_file";
                        inner.erase(0, std::strlen("файл"));
                        while (!inner.empty() && inner.front() == ' ') inner.erase(0, 1);
                    } else {
                        action = "create_folder";
                    }
                    break;
                case Verb::CreateFile: action = "create_file"; break;
                case Verb::Delete: action = "delete_path"; break;
                case Verb::Read: action = "read_file"; break;
                case Verb::List: action = "list_dir"; break;
                case Verb::Move: action = "move_path"; break;
                case Verb::Copy: action = "copy_path"; break;
                case Verb::FindFiles: action = "find_files"; break;
                default: break;
            }
            for (const char* p : kFolderPre) {
                if (starts_with_word(inner, p)) {
                    inner.erase(0, std::strlen(p));
                    while (!inner.empty() && inner.front() == ' ') inner.erase(0, 1);
                }
            }
            for (const char* p : kFilePre) {
                if (starts_with_word(inner, p) && action != std::string_view("create_file")) {
                    inner.erase(0, std::strlen(p));
                    while (!inner.empty() && inner.front() == ' ') inner.erase(0, 1);
                }
            }
            // «создай файл notes.txt с текстом привет»
            if (action == std::string_view("create_file")) {
                static const char* kContent[] = {"с текстом", "с содержимым", "текст",
                                                 "и запиши", "и напиши"};
                for (const char* c : kContent) {
                    const size_t pos = inner.find(c);
                    if (pos != std::string::npos && pos > 0) {
                        it.set(SlotId::Content, inner.substr(pos + std::strlen(c) + 1));
                        inner = inner.substr(0, pos);
                        while (!inner.empty() && inner.back() == ' ') inner.pop_back();
                        break;
                    }
                }
            }
            // Фраза-клауза («сделай отчёт по проекту и сохрани его») — не имя, а задача.
            if (static_cast<int>(verb) != static_cast<int>(Verb::FindFiles) &&
                !looks_like_path(inner) && clause_like(inner)) {
                Intent task;
                task.action = "agent_task";
                task.raw = phrase;
                task.confidence = 0.5f;
                task.source = static_cast<uint8_t>(IntentSource::Rule);
                stats_.total_us += now_us() - t0;
                return task;
            }
            Intent sub = parse_place_or_path(action, phrase, inner);
            sub.raw = phrase;
            if (action == std::string_view("create_file") && !it.slot(SlotId::Content).empty())
                sub.set(SlotId::Content, it.slot(SlotId::Content).view());
            stats_.total_us += now_us() - t0;
            return sub;
        }
        case Verb::Focus: {
            // «переключись на Discord» — не запуск, а переключение на окно (ТЗ: multi-window).
            std::string target = strip_tail_filler(obj_norm);
            static const char* kPre[] = {"на ", "к ", "в "};
            for (const char* pre : kPre) {
                if (starts_with_word(target, pre)) {
                    target.erase(0, std::strlen(pre));
                    while (!target.empty() && target.front() == ' ') target.erase(0, 1);
                    break;
                }
            }
            it.action = "focus_window";
            it.set(SlotId::Target, target);
            it.confidence = 0.9f;
            break;
        }
        case Verb::ClickElement: {
            std::string target = strip_tail_filler(obj_norm);
            static const char* kPre[] = {"на ", "по ", "в "};
            for (const char* pre : kPre) {
                if (starts_with_word(target, pre) && std::strlen(pre) < target.size()) {
                    target.erase(0, std::strlen(pre));
                    while (!target.empty() && target.front() == ' ') target.erase(0, 1);
                    break;
                }
            }
            it.action = "click_element";
            it.set(SlotId::Target, target);
            it.confidence = 0.85f;
            break;
        }
        case Verb::Screenshot:
            it.action = "screenshot";
            it.confidence = 0.95f;
            break;
        case Verb::Wallpaper: {
            std::string img = strip_tail_filler(obj_norm);
            static const char* kPreWall[] = {"на картинку ", "на изображение ", "на фото ", "картинку ",
                                             "картинка ", "изображение ", "фото ", "из файла ",
                                             "файл ", "на "};
            bool again = true;
            while (again) {
                again = false;
                for (const char* p : kPreWall) {
                    if (starts_with_word(img, p)) {
                        img.erase(0, std::strlen(p));
                        again = true;
                    }
                }
            }
            it.action = "set_wallpaper";
            it.set(SlotId::Target, img);
            it.confidence = 0.9f;
            break;
        }
        case Verb::Volume: {
            it.action = "volume";
            if (obj_norm.find("громче") != std::string::npos ||
                obj_norm.find("прибавь") != std::string::npos) {
                it.set(SlotId::Args, "up");
            } else if (obj_norm.find("тише") != std::string::npos ||
                       obj_norm.find("убавь") != std::string::npos) {
                it.set(SlotId::Args, "down");
            } else if (obj_norm.find("выключи") != std::string::npos ||
                       obj_norm.find("отключи") != std::string::npos ||
                       obj_norm.find("mute") != std::string::npos) {
                it.set(SlotId::Args, "mute");
            } else {
                for (const std::string_view w : tokens(obj_norm)) {
                    if (looks_like_number(w)) {
                        it.set(SlotId::Value, w);
                        break;
                    }
                }
                it.set(SlotId::Args, it.slot(SlotId::Value).empty() ? "get" : "set");
            }
            it.confidence = 0.9f;
            break;
        }
        case Verb::Power: {
            static const char* kShutdown[] = {"выключи", "выключить", "shutdown"};
            static const char* kRestart[] = {"перезагруз", "restart", "reboot"};
            static const char* kLock[] = {"заблокир", "lock"};
            static const char* kSleep[] = {"спящий", "усыпи", "sleep"};
            static const char* kLogoff[] = {"выйди из системы", "заверши сеанс", "logoff"};
            static const char* kMonitor[] = {"монитор", "экран", "дисплей"};
            if (starts_with_word(n, "выключи") && object_is_system(n, kMonitor, 3)) {
                it.set(SlotId::Args, "monitor-off");
            } else if (object_is_system(n, kShutdown, 3)) {
                it.set(SlotId::Args, "shutdown");
            } else if (object_is_system(n, kRestart, 3)) {
                it.set(SlotId::Args, "restart");
            } else if (object_is_system(n, kLock, 2)) {
                it.set(SlotId::Args, "lock");
            } else if (object_is_system(n, kSleep, 3)) {
                it.set(SlotId::Args, "sleep");
            } else if (object_is_system(n, kLogoff, 3)) {
                it.set(SlotId::Args, "logoff");
            } else {
                it.set(SlotId::Args, "shutdown");
            }
            it.action = "power";
            it.confidence = 0.95f;
            break;
        }
        case Verb::Keys:
            it.action = "send_keys";
            it.set(SlotId::Key, strip_tail_filler(obj_norm));
            it.confidence = 0.88f;
            break;
        case Verb::Type:
            it.action = "type_text";
            it.set(SlotId::Content, strip_tail_filler(object));
            it.confidence = 0.85f;
            break;
        case Verb::Kill:
            it.action = "kill_process";
            it.set(SlotId::Target, strip_tail_filler(obj_norm));
            it.confidence = 0.9f;
            break;
        case Verb::Run:
            it.action = "run_command";
            it.set(SlotId::Content, object);
            it.confidence = 0.9f;
            break;
        case Verb::Settings:
            it.action = "settings_page";
            if (starts_with_word(obj_norm, "настройки")) {
                std::string page = obj_norm.substr(std::strlen("настройки"));
                while (!page.empty() && page.front() == ' ') page.erase(0, 1);
                it.set(SlotId::Args, page);
            } else {
                it.set(SlotId::Args, obj_norm);
            }
            it.confidence = 0.9f;
            break;
        case Verb::ShowDesktop:
            it.action = "show_desktop";
            it.confidence = 0.92f;
            break;
        case Verb::Code:
            it.action = "code_task";
            it.set(SlotId::Target, obj_norm);
            it.confidence = 0.8f;
            break;
        case Verb::Unknown:
        default:
            break;
    }

    // «посмотри на экран и скажи, что открыто» — задача для зрения, не для текстовой модели
    if (verb == Verb::Unknown && !is_question(n)) {
        static const char* kScreenWords[] = {"на экран", "на экране", "скриншот"};
        static const char* kLookWords[] = {"посмотри", "взгляни", "глянь", "проверь", "что открыто",
                                           "что на экране", "определи"};
        if (object_is_system(n, kScreenWords, 3) && object_is_system(n, kLookWords, 7)) {
            it.action = "analyze_screen";
            it.set(SlotId::Query, strip_tail_filler(n));
            it.confidence = 0.8f;
            it.source = static_cast<uint8_t>(IntentSource::Rule);
            stats_.total_us += now_us() - t0;
            return it;
        }
    }

    if (verb == Verb::Unknown) {
        if (is_question(n)) {
            it.action = "chat";
            it.confidence = 0.8f;
            it.source = static_cast<uint8_t>(IntentSource::Rule);
        } else if (is_complex(n)) {
            it.action = "agent_task";      // сложная задача → планирование моделью
            it.confidence = 0.5f;
        } else {
            ++stats_.fallbacks;
        }
    }

    // «напиши калькулятор на Python» / «сделай сайт на JS» — это код, а не папка и не текст
    const bool speaks_code =
        n.find("python") != std::string::npos || n.find("питон") != std::string::npos ||
        n.find("javascript") != std::string::npos || n.find("джаваскрипт") != std::string::npos ||
        n.find("java") != std::string::npos || n.find("c++") != std::string::npos ||
        n.find("c#") != std::string::npos || n.find("код") != std::string::npos ||
        n.find("скрипт") != std::string::npos || n.find("программ") != std::string::npos ||
        n.find("функци") != std::string::npos;
    const std::string_view act = it.action.view();
    if ((act == "create_folder" || act == "type_text") && (is_complex(n) || speaks_code)) {
        it.action = "code_task";
        if (it.slot(SlotId::Target).empty()) it.set(SlotId::Target, obj_norm);
        it.confidence = 0.75f;
        it.source = static_cast<uint8_t>(IntentSource::Rule);
    }
    stats_.total_us += now_us() - t0;
    return it;
}

Intent IntentEngine::parse(std::string_view phrase) const {
    const std::string n = normalize(phrase);
    if (n.empty()) return parse_single(phrase, false);

    // заученная фраза целиком — самый быстрый путь
    Intent direct;
    if (recall(phrase, direct)) return direct;

    const std::vector<std::string> parts = split_compound(phrase);
    if (parts.size() > 1) {
        Intent compound;
        compound.action = "compound";
        compound.raw = phrase;
        compound.confidence = 0.95f;
        compound.source = static_cast<uint8_t>(IntentSource::Rule);
        // Сервис, названный в целом предложении, относится ко всем его частям:
        // «открой ютуб и найди видео про котиков» — искать надо на YouTube, а не в Google.
        const bool wants_video = contains_word(n, "ютуб") || contains_word(n, "youtube") ||
                                 contains_word(n, "ютюб") || contains_word(n, "ютьюб") ||
                                 contains_word(n, "ютубе");
        for (const std::string& p : parts) {
            Intent sub = parse_single(p, false);
            if (sub.action.view() == "agent_task") {
                compound.confidence = 0.5f;   // составная команда с непонятной частью
            }
            if (wants_video && sub.action.view() == "web_search") {
                sub.action = "youtube_search";
                sub.confidence = 0.9f;
            }
            compound.parts.push_back(std::move(sub));
        }
        ++stats_.compounds;
        return compound;
    }
    return parse_single(phrase, false);
}

// ---------------------------------------------------------------------------
//  Каталог известных приложений
// ---------------------------------------------------------------------------
namespace {

AppInfo app(std::string key, std::string name, std::string kind,
            std::vector<std::string> aliases, std::vector<std::string> exe,
            std::string protocol = {}, std::string appid = {}, std::string args = {}) {
    AppInfo a;
    a.key = std::move(key);
    a.display_name = std::move(name);
    a.kind = std::move(kind);
    a.aliases = std::move(aliases);
    a.exe = std::move(exe);
    a.protocol = std::move(protocol);
    a.appid = std::move(appid);
    a.args = std::move(args);
    return a;
}

}  // namespace

void add_builtin_apps(AppRegistry& reg) {
    // --- браузеры ---
    reg.add(app("chrome", "Google Chrome", "app",
                {"chrome", "хром", "гугл хром", "браузер", "browser", "гугл"},
                {"chrome.exe", "google-chrome", "chrome"}));
    reg.add(app("edge", "Microsoft Edge", "app", {"edge", "эдж", "эдж браузер", "microsoft edge"},
                {"msedge.exe", "microsoft-edge"}));
    reg.add(app("firefox", "Mozilla Firefox", "app", {"firefox", "фаерфокс", "мозила", "мозилла"},
                {"firefox.exe", "firefox"}));
    reg.add(app("yandex-browser", "Яндекс Браузер", "app", {"яндекс браузер", "яндекс"},
                {"browser.exe", "yandex-browser"}));
    // --- мессенджеры и связь ---
    reg.add(app("telegram", "Telegram", "app",
                {"telegram", "телега", "тг", "телеграм", "телеграмм", "телега десктоп"},
                {"Telegram.exe", "telegram-desktop"},
                "tg://"));
    reg.add(app("whatsapp", "WhatsApp", "app", {"whatsapp", "ватсап", "вотсап"},
                {"WhatsApp.exe", "whatsapp"}));
    reg.add(app("discord", "Discord", "app", {"discord", "дискорд", "дис"},
                {"Discord.exe", "discord"},
                "discord://"));
    reg.add(app("slack", "Slack", "app", {"slack", "слак"}, {"slack.exe", "slack"}));
    reg.add(app("zoom", "Zoom", "app", {"zoom", "зум"}, {"Zoom.exe", "zoom"}));
    reg.add(app("teams", "Microsoft Teams", "app", {"teams", "тимс", "майкрософт тимс"},
                {"Teams.exe", "teams"}));
    // --- разработка ---
    reg.add(app("vscode", "Visual Studio Code", "app",
                {"vscode", "vs code", "вс код", "вс kод", "код", "студия", "code",
                 "vs codium"},
                {"Code.exe", "code", "codium"}));
    reg.add(app("cursor", "Cursor", "app", {"cursor", "курсор"}, {"Cursor.exe", "cursor"}));
    reg.add(app("pycharm", "PyCharm", "app", {"pycharm", "пайчарм"}, {"pycharm64.exe", "pycharm"}));
    reg.add(app("idea", "IntelliJ IDEA", "app", {"idea", "идэа", "intellij"},
                {"idea64.exe", "idea"}));
    reg.add(app("terminal", "Терминал", "app",
                {"terminal", "терминал", "консоль", "командная строка", "cmd", "консолька"},
                {"wt.exe", "WindowsTerminal.exe", "cmd.exe", "gnome-terminal", "konsole"}));
    reg.add(app("powershell", "PowerShell", "app", {"powershell", "pwsh", "павершелл"},
                {"powershell.exe", "pwsh"}));
    reg.add(app("git-bash", "Git Bash", "app", {"git bash", "гит баш"}, {"git-bash.exe", "bash"}));
    // --- работа ---
    reg.add(app("word", "Microsoft Word", "app", {"word", "ворд", "ворд документ"},
                {"WINWORD.EXE", "word"}));
    reg.add(app("excel", "Microsoft Excel", "app", {"excel", "эксель", "таблица"},
                {"EXCEL.EXE", "excel"}));
    reg.add(app("powerpoint", "PowerPoint", "app", {"powerpoint", "повер поинт", "презентация"},
                {"POWERPNT.EXE", "powerpoint"}));
    reg.add(app("notion", "Notion", "app", {"notion", "ноушен"}, {"Notion.exe"}));
    reg.add(app("obsidian", "Obsidian", "app", {"obsidian", "обсидиан"}, {"Obsidian.exe"}));
    // --- медиа ---
    reg.add(app("spotify", "Spotify", "app", {"spotify", "спотифай", "музыка", "спотик"},
                {"Spotify.exe", "spotify"}, "spotify:"));
    reg.add(app("vlc", "VLC", "app", {"vlc", "влс", "плеер"}, {"vlc.exe", "vlc"}));
    reg.add(app("steam", "Steam", "app", {"steam", "стим"}, {"steam.exe", "steam"}));
    reg.add(app("obs", "OBS Studio", "app", {"obs", "обс", "обс студия"}, {"obs64.exe", "obs"}));
    reg.add(app("photoshop", "Adobe Photoshop", "app", {"photoshop", "фотошоп", "фш"},
                {"Photoshop.exe"}));
    reg.add(app("figma", "Figma", "app", {"figma", "фигма"}, {"Figma.exe"}));
    // --- системное ---
    reg.add(app("explorer", "Проводник", "app",
                {"explorer", "проводник", "мои файлы", "мой компьютер", "этот компьютер",
                 "файлы", "файловый менеджер", "windows explorer"},
                {"explorer.exe"}, "file://"));
    reg.add(app("taskmgr", "Диспетчер задач", "app",
                {"taskmgr", "диспетчер задач", "диспетчер", "task manager"},
                {"Taskmgr.exe"}, "", "", ""));
    reg.add(app("settings", "Параметры Windows", "settings",
                {"settings", "настройки", "параметры", "настройки windows"},
                {"SystemSettings.exe"}, "ms-settings:"));
    reg.add(app("control", "Панель управления", "app",
                {"панель управления", "control panel"}, {"control.exe"}));
    reg.add(app("calc", "Калькулятор", "app", {"calc", "калькулятор", "calculator"},
                {"calc.exe", "gnome-calculator", "kcalc"}));
    reg.add(app("notepad", "Блокнот", "app", {"notepad", "блокнот", "notepad++"},
                {"notepad.exe", "gedit", "kate"}));
    reg.add(app("paint", "Paint", "app", {"paint", "пейнт", "графический редактор"},
                {"mspaint.exe"}));
    reg.add(app("cmd", "CMD", "app", {"cmd", "командная строка"}, {"cmd.exe"}));
    reg.add(app("regedit", "Редактор реестра", "app", {"regedit", "реестр", "редактор реестра"},
                {"regedit.exe"}));
    reg.add(app("snipping", "Ножницы", "app", {"ножницы", "snipping tool", "screenshot tool"},
                {"SnippingTool.exe", "ScreenSketch.exe"}));
    // --- прочее ---
    reg.add(app("winrar", "WinRAR", "app", {"winrar", "винрар", "архив"}, {"WinRAR.exe"}));
    reg.add(app("7zip", "7-Zip", "app", {"7zip", "7-zip", "семиз"}, {"7zFM.exe"}));
    reg.add(app("anydesk", "AnyDesk", "app", {"anydesk", "энидеск"}, {"AnyDesk.exe"}));
    reg.add(app("nvim", "Neovim", "app", {"nvim", "neovim", "вим"}, {"nvim.exe", "nvim"}));
}

int discover_apps_into(AppRegistry& reg) {
    // 1) встроенный каталог (~45 известных приложений) — мгновенно, без диска;
    // 2) реальное обнаружение делает платформа (реестр Windows, меню «Пуск»,
    //    PATH, App Paths, протоколы, Store/UWP) и помечает записи installed.
    add_builtin_apps(reg);
    int found = 0;
    for (const AppInfo* a : reg.all())
        if (a->installed) ++found;
    return found;
}

}  // namespace agent
