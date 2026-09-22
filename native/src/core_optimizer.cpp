// Execution Optimizer: лестница способов + обучение на статистике (ТЗ §11).
#include <algorithm>
#include <cmath>
#include <cstdlib>
#include <cstdio>
#include <sstream>

#include "agent/core.h"

namespace agent {
namespace {

// Базовая лестница: чем меньше индекс — тем быстрее и надёжнее способ.
// Порядок может корректироваться опытом конкретной машины (см. note()).
struct Ladder {
    const char* capability;
    std::initializer_list<Method> methods;
};

const Ladder kLadders[] = {
    {"launch_app", {Method::CachedExe, Method::WinApi, Method::Shell, Method::Shortcut,
                    Method::Cli, Method::Uia, Method::Vision}},
    {"open_url", {Method::WinApi, Method::Shell, Method::Cli, Method::Uia, Method::Vision}},
    {"open_folder", {Method::WinApi, Method::Shell, Method::Uia}},
    {"open_path", {Method::WinApi, Method::Shell, Method::Uia}},
    {"file_op", {Method::WinApi, Method::Cli, Method::Shell}},
    {"delete_op", {Method::WinApi, Method::Shell}},
    {"run_command", {Method::WinApi, Method::Cli, Method::Shell}},
    {"kill_process", {Method::WinApi, Method::Cli}},
    {"read_processes", {Method::WinApi, Method::Cli}},
    {"screenshot", {Method::WinApi, Method::Shell, Method::Vision}},
    {"find_element", {Method::Uia, Method::WinApi, Method::Vision}},
    {"click", {Method::WinApi, Method::Uia, Method::Vision}},
    {"type_text", {Method::WinApi, Method::Uia}},
    {"press_key", {Method::WinApi, Method::Uia}},
    {"clipboard", {Method::WinApi, Method::Cli}},
    {"wallpaper", {Method::WinApi, Method::Shell}},
    {"settings_page", {Method::WinApi, Method::Shell, Method::Uia}},
    {"volume", {Method::WinApi, Method::Cli, Method::Uia}},
    {"power", {Method::WinApi, Method::Cli}},
    {"show_desktop", {Method::WinApi, Method::Uia}},
    {"window_op", {Method::WinApi, Method::Shell, Method::Uia}},
    {"browser_search", {Method::WinApi, Method::Shell, Method::Cli}},
    {"app_discovery", {Method::WinApi, Method::Cli}},
    {"computer_state", {Method::WinApi, Method::Cli}},
    {"verify", {Method::WinApi, Method::Cli}},
};

const std::initializer_list<Method>* ladder_for(std::string_view capability) {
    for (const Ladder& l : kLadders) {
        if (capability == l.capability) return &l.methods;
    }
    return nullptr;
}

// Синонимы имён способов между слоями («shell» в лаунчере ↔ «Shell» в лестнице)
int static_rank(std::string_view capability, Method m) {
    const auto* ladder = ladder_for(capability);
    if (!ladder) return 20;
    int i = 0;
    for (Method lm : *ladder) {
        if (lm == m) return i;
        ++i;
    }
    return static_cast<int>(ladder->size()) + 3;
}

}  // namespace

double MethodStat::score() const {
    if (!attempts) return 0.0;
    // Базовая ставка: успех важнее скорости, но медленные способы штрафуются.
    const double sr = success_rate();
    const double speed = ema_ms > 0 ? std::min(1.0, 250.0 / ema_ms) : 1.0;
    return sr * 0.7 + speed * 0.3;
}

Optimizer::Optimizer(size_t ladder_hint) { stats_.reserve(ladder_hint); }

std::vector<Method> Optimizer::order(std::string_view capability,
                                     std::span<const Method> available) const {
    std::vector<std::pair<double, Method>> scored;
    scored.reserve(available.size());
    for (Method m : available) {
        const int rank = static_rank(capability, m);
        double base = std::max(0.0, 1.0 - rank * 0.06);
        const MethodStat* st = stat(capability, m);
        if (st && st->attempts >= 2) {
            const double learned = st->score();
            base = base * 0.35 + learned * 0.65;
            if (st->attempts >= 4 && st->success_rate() <= 0.25) base *= 0.5;
        }
        scored.emplace_back(base, m);
    }
    std::stable_sort(scored.begin(), scored.end(),
                     [](const auto& a, const auto& b) { return a.first > b.first; });
    std::vector<Method> out;
    out.reserve(scored.size());
    for (const auto& [score, m] : scored) out.push_back(m);
    return out;
}

Method Optimizer::best(std::string_view capability, std::span<const Method> available) const {
    const std::vector<Method> ordered = order(capability, available);
    return ordered.empty() ? Method::Llm : ordered.front();
}

void Optimizer::note(std::string_view capability, Method m, bool ok, double ms) {
    MethodStat& st = stats_[Key{std::string(capability), m}];
    ++st.attempts;
    if (ok) {
        ++st.ok;
        st.last_ok_ms = now_ms();
    } else {
        ++st.fallbacks;
    }
    st.total_ms += ms;
    const double alpha = st.attempts == 1 ? 1.0 : 0.3;
    st.ema_ms = st.attempts == 1 ? ms : st.ema_ms * (1.0 - alpha) + ms * alpha;
}

const MethodStat* Optimizer::stat(std::string_view capability, Method m) const {
    auto it = stats_.find(Key{std::string(capability), m});
    return it == stats_.end() ? nullptr : &it->second;
}

std::string Optimizer::benchmark_json(std::string_view capability) const {
    std::string out = "[";
    bool first = true;
    for (const auto& [key, st] : stats_) {
        if (key.cap != capability) continue;
        if (!first) out += ",";
        first = false;
        out += "{\"method\":\"" + std::string(to_string(key.m)) + "\",\"attempts\":" +
               std::to_string(st.attempts) + ",\"ok\":" + std::to_string(st.ok) +
               ",\"success_rate\":" + std::to_string(st.success_rate()) +
               ",\"avg_ms\":" + std::to_string(st.avg_ms()) +
               ",\"ema_ms\":" + std::to_string(st.ema_ms) + "}";
    }
    out += "]";
    return out;
}

std::string Optimizer::stats_json() const {
    std::string out = "{";
    bool first = true;
    for (const auto& [key, st] : stats_) {
        if (!first) out += ",";
        first = false;
        out += "\"" + json_escape(key.cap) + ":" + to_string(key.m) + "\":{\"attempts\":" +
               std::to_string(st.attempts) + ",\"ok\":" + std::to_string(st.ok) +
               ",\"fallbacks\":" + std::to_string(st.fallbacks) + ",\"ema_ms\":" +
               std::to_string(st.ema_ms) + ",\"last_ok_ms\":" + std::to_string(st.last_ok_ms) + "}";
    }
    out += "}";
    return out;
}

std::string Optimizer::save_json() const {
    // Плоский формат «ключ\tзначения»: читается и пишется за микросекунды,
    // без парсера JSON в горячем пути. SQLite-хранилище Windows-сборки
    // использует этот же текст как значение одной строки.
    std::ostringstream oss;
    oss << "# agent-native optimizer stats v1\n";
    for (const auto& [key, st] : stats_) {
        oss << key.cap << '\t' << int(key.m) << '\t' << st.attempts << '\t' << st.ok << '\t'
            << st.fallbacks << '\t' << st.total_ms << '\t' << st.ema_ms << '\t'
            << st.last_ok_ms << '\n';
    }
    return oss.str();
}

void Optimizer::load_json(std::string_view text) {
    size_t pos = 0;
    while (pos < text.size()) {
        size_t nl = text.find('\n', pos);
        if (nl == std::string_view::npos) nl = text.size();
        std::string_view line = text.substr(pos, nl - pos);
        pos = nl + 1;
        if (line.empty() || line[0] == '#') continue;
        std::vector<std::string_view> fields;
        size_t start = 0;
        while (start <= line.size()) {
            size_t tab = line.find('\t', start);
            if (tab == std::string_view::npos) tab = line.size();
            fields.push_back(line.substr(start, tab - start));
            if (tab == line.size()) break;
            start = tab + 1;
        }
        if (fields.size() < 7) continue;
        MethodStat st;
        st.attempts = static_cast<uint32_t>(std::strtoul(std::string(fields[2]).c_str(), nullptr, 10));
        st.ok = static_cast<uint32_t>(std::strtoul(std::string(fields[3]).c_str(), nullptr, 10));
        st.fallbacks = static_cast<uint32_t>(std::strtoul(std::string(fields[4]).c_str(), nullptr, 10));
        st.total_ms = std::strtod(std::string(fields[5]).c_str(), nullptr);
        st.ema_ms = std::strtod(std::string(fields[6]).c_str(), nullptr);
        st.last_ok_ms = static_cast<int64_t>(
            std::strtoll(std::string(fields.size() > 7 ? fields[7] : fields[6]).c_str(), nullptr, 10));
        stats_[Key{std::string(fields[0]),
                   static_cast<Method>(std::atoi(std::string(fields[1]).c_str()))}] = st;
    }
}

}  // namespace agent
