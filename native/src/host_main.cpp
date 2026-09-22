// agent_host — автономный запуск ядра без C#/UI: отладка, CI, скрипты.
//
//   agent_host "открой ютуб и найди видео про котиков"
//   agent_host --preview "удали папку 123 с рабочего стола"
//   agent_host --metrics
//   agent_host --ai /tmp/agent_ai.sock --task "напиши калькулятор на python"
//   agent_host --ai /tmp/agent_ai.sock --ask "просто спроси план (отладка)"
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

#include "agent/ipc.h"
#include "agent/runtime.h"

namespace {

void usage() {
    std::printf(
        "agent_host [--state-dir DIR] [--no-preload] [--preview] [--metrics] [--tools]\n"
        "           [--ai SOCKET] [--ask TEXT] [текст команды...]\n");
}

}  // namespace

int main(int argc, char** argv) {
    agent::RuntimeConfig cfg;
    std::string socket;
    std::string ask;
    bool preview = false;
    bool task_mode = false;
    bool metrics = false;
    bool tools = false;
    int task_steps = 0;
    std::vector<std::string> words;
    for (int i = 1; i < argc; ++i) {
        const std::string arg = argv[i];
        if (arg == "--state-dir" && i + 1 < argc) cfg.state_dir = argv[++i];
        else if (arg == "--no-preload") cfg.preload = false;
        else if (arg == "--preview") preview = true;
        else if (arg == "--metrics") metrics = true;
        else if (arg == "--tools") tools = true;
        else if (arg == "--steps" && i + 1 < argc) task_steps = std::atoi(argv[++i]);
        else if (arg == "--ai" && i + 1 < argc) { socket = argv[++i]; cfg.ai_socket = socket; }
        else if (arg == "--task") task_mode = true;
        else if (arg == "--ask" && i + 1 < argc) ask = argv[++i];
        else if (arg == "-h" || arg == "--help") { usage(); return 0; }
        else words.push_back(arg);
    }

    agent::AgentRuntime runtime(cfg, agent::make_platform());
    runtime.set_event_sink([](const agent::Event& ev) {
        std::printf("[%s] %s %s (%d мс)\n", ev.status.c_str(), ev.tool.c_str(), ev.message.c_str(),
                    int(ev.ms));
    });
    std::string error;
    if (!runtime.start(error)) {
        std::fprintf(stderr, "не удалось запустить ядро: %s\n", error.c_str());
        return 1;
    }

    if (metrics) {
        std::printf("%s\n", runtime.metrics_json().c_str());
        return 0;
    }
    if (tools) {
        std::printf("%s\n", runtime.tools_json().c_str());
        return 0;
    }

    std::string phrase;
    for (size_t i = 0; i < words.size(); ++i) {
        if (i) phrase += ' ';
        phrase += words[i];
    }

    if (!ask.empty()) {
        // Ручная проверка агентного пути: фраза уходит в Python-мозг по каналу.
        if (socket.empty()) {
            std::fprintf(stderr, "укажите --ai <адрес канала>\n");
            return 2;
        }
        agent::AiLink link;
        if (!link.connect(socket, 5000, error)) {
            std::fprintf(stderr, "%s\n", error.c_str());
            return 3;
        }
        const std::string request =
            "{\"id\":1,\"type\":\"plan\",\"task\":\"" + agent::json_escape(ask) +
            "\",\"tools\":" + runtime.tools_json() + ",\"state\":{}," +
            "\"max_steps\":8}";
        const agent::AiReply reply = link.request(request, 180000);
        if (!reply.ok) {
            std::fprintf(stderr, "%s\n", reply.error.c_str());
            return 4;
        }
        std::printf("%s\n", reply.json.c_str());
        return 0;
    }

    if (phrase.empty()) {
        usage();
        return 2;
    }

    if (preview) {
        std::printf("%s\n", runtime.preview(phrase).c_str());
        return 0;
    }

    // Полный проход по задаче: простое делает ядро, сложное — агентный цикл с мозгом.
    if (task_mode || !cfg.ai_socket.empty()) {
        const std::string result = runtime.run_task(phrase, task_steps);
        std::printf("%s\n", result.c_str());
        return result.find("\"ok\":true") != std::string::npos ? 0 : 1;
    }

    const agent::FastOutcome outcome = runtime.execute(phrase);
    std::printf("%s\n", outcome.to_json().c_str());
    if (outcome.needs_llm) {
        std::printf("(нужна модель: маршрут %s, шагов %zu)\n",
                    agent::to_string(outcome.route), outcome.actions);
        return 10;   // ненулевой код = «ядро само не справилось»
    }
    return outcome.ok ? 0 : 1;
}
