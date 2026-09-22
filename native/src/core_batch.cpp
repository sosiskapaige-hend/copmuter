// Batch execution: независимые действия выполняются пачкой, без обращений к модели.
#include <algorithm>
#include <condition_variable>
#include <mutex>
#include <thread>

#include "agent/core.h"

namespace agent {
namespace {

// Общее состояние одного действия: владение данными через shared_ptr, поэтому
// поток может пережить таймаут вызывающей стороны и не превратиться в «висяк»,
// роняющий процесс при уничтожении future.
struct RunState {
    ActionSpec action;
    std::mutex mu;
    std::condition_variable cv;
    bool done = false;
};

}  // namespace

std::vector<std::vector<int>> ActionQueue::groups() const {
    const size_t n = actions_.size();
    std::vector<std::vector<int>> out;
    std::vector<bool> placed(n, false);
    std::vector<bool> done(n, false);
    size_t remaining = n;
    int guard = 0;
    while (remaining > 0 && guard++ < int(n) + 2) {
        std::vector<int> ready;
        for (size_t i = 0; i < n; ++i) {
            if (placed[i]) continue;
            bool deps_ok = true;
            for (int d : actions_[i].depends_on) {
                if (d < 0 || d >= int(n) || !done[size_t(d)]) {
                    deps_ok = false;
                    break;
                }
            }
            if (deps_ok) ready.push_back(int(i));
        }
        if (ready.empty()) {                    // цикл в зависимостях — просто досылаем остаток
            for (size_t i = 0; i < n; ++i)
                if (!placed[i]) ready.push_back(int(i));
        }
        std::vector<int> group;
        bool serial_block = false;
        for (int idx : ready) {
            if (placed[size_t(idx)]) continue;
            const bool solo = !actions_[size_t(idx)].parallel || serial_block;
            if (solo) {
                group.push_back(idx);
                placed[size_t(idx)] = true;
                --remaining;
                serial_block = true;
                break;
            }
            group.push_back(idx);
            placed[size_t(idx)] = true;
            --remaining;
            if (int(group.size()) >= max_parallel_) break;
        }
        for (int idx : group) done[size_t(idx)] = true;
        out.push_back(std::move(group));
    }
    return out;
}

void ActionQueue::cancel() {
    cancelled_.store(true);
    for (ActionSpec& a : actions_) {
        if (a.state == ActionState::Queued) a.state = ActionState::Cancelled;
    }
}

BatchResult ActionQueue::run(const CallFn& call, const VerifyFn& verify, bool stop_on_error) {
    const double t0 = now_us();
    cancelled_.store(false);
    for (ActionSpec& a : actions_) {
        a.state = ActionState::Queued;
        a.ok = false;
        a.ms = 0.0;
        a.output.clear();
        a.error.clear();
    }

    // Действие выполняется в отдельном потоке, чтобы таймаут можно было
    // соблюсти даже для блокирующего системного вызова.
    auto run_one = [&](ActionSpec& a) {
        auto st = std::make_shared<RunState>();
        st->action = a;
        const auto start = now_us();

        auto attempt_call = [&](ActionSpec& target) {
            auto worker = std::make_shared<ActionSpec>(target);
            auto done_flag = std::make_shared<std::atomic<bool>>(false);
            std::thread th([worker, done_flag, &call]() {
                try {
                    call(*worker);
                } catch (...) {
                    worker->ok = false;
                    worker->error = "исключение при выполнении";
                }
                done_flag->store(true);
            });
            th.detach();
            const int64_t deadline = now_ms() + std::max(1, target.timeout_ms);
            while (!done_flag->load() && now_ms() < deadline) {
                std::this_thread::sleep_for(std::chrono::milliseconds(2));
            }
            if (!done_flag->load()) {
                target.state = ActionState::Timeout;
                target.ok = false;
                if (target.error.empty()) target.error = "таймаут";
            } else {
                target.state = worker->state == ActionState::Queued ? ActionState::Success
                                                                   : worker->state;
                target.ok = worker->ok;
                target.output = worker->output;
                target.error = worker->error;
                target.method_used = worker->method_used;
            }
        };

        a.state = ActionState::Running;
        a.ms = 0.0;
        attempt_call(a);

        // Запасные способы внутри одного действия (ТЗ §19): A не сработало → B.
        for (size_t fb = 0; fb < a.fallbacks.size() && !a.ok; ++fb) {
            ActionSpec alt = a;
            alt.tool = a.fallbacks[fb].first;
            alt.args_json = a.fallbacks[fb].second;
            attempt_call(alt);
            a.output = alt.output;
            a.error = alt.error;
            a.ok = alt.ok;
            a.method_used = alt.method_used;
            if (a.ok) a.state = ActionState::Success;
        }

        if (a.ok && a.verify != VerifyKind::None && verify) {
            std::string detail;
            const bool verified = verify(a, detail);
            if (!verified) {
                a.ok = false;
                a.state = ActionState::Failed;
                a.error = detail.empty() ? "результат не подтвердился" : detail;
            }
        }
        if (a.ok) a.state = ActionState::Success;
        else if (a.state == ActionState::Running) a.state = ActionState::Failed;
        a.ms = (now_us() - start) / 1000.0;
    };

    BatchResult result;
    const std::vector<std::vector<int>> gs = groups();
    for (const std::vector<int>& group : gs) {
        if (cancelled_.load()) break;
        if (group.size() == 1) {
            run_one(actions_[size_t(group[0])]);
        } else {
            std::vector<std::thread> threads;
            threads.reserve(group.size());
            for (int idx : group) threads.emplace_back([&, idx]() { run_one(actions_[size_t(idx)]); });
            for (std::thread& t : threads) t.join();
        }
        for (int idx : group) {
            const ActionSpec& a = actions_[size_t(idx)];
            const bool failed = (a.state == ActionState::Failed || a.state == ActionState::Timeout);
            if (failed && !a.soft && stop_on_error) {
                result.stopped_at = idx;
                break;
            }
        }
        if (result.stopped_at >= 0) {
            for (ActionSpec& a : actions_)
                if (a.state == ActionState::Queued) a.state = ActionState::Skipped;
            break;
        }
    }

    bool ok = true;
    for (const ActionSpec& a : actions_) {
        if (a.state == ActionState::Failed || a.state == ActionState::Timeout) {
            if (!a.soft) ok = false;
            if (result.error.empty()) result.error = a.error;
        }
        if (a.state == ActionState::Cancelled || a.state == ActionState::Skipped) ok = false;
    }
    result.ok = ok;
    result.actions = actions_;
    result.ms = (now_us() - t0) / 1000.0;
    return result;
}

}  // namespace agent
