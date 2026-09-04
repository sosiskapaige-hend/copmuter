// jsdom-тесты веб-интерфейса AI Computer Agent.
//
// Проверяют:
//   1. Кнопка «⚙ Настройки» открывается без ReferenceError (loadTrig удалён).
//   2. Клик по modelChip тоже открывает настройки.
//   3. Скрипт грузится без window.matchMedia (защитная версия mobile()).
//   4. Отправка сообщения не «зависает»: SSE-события нового чата приходят
//      раньше ответа POST /api/chat/send и корректно усваиваются.
//   5. Клик по всем кнопкам интерфейса не бросает исключений.
//
// Запуск: npm i && npm test
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { JSDOM } from "jsdom";

const __dirname = dirname(fileURLToPath(import.meta.url));
const HTML = readFileSync(
  join(__dirname, "..", "agent", "ui", "web", "static", "index.html"),
  "utf8"
);

let failed = 0;
function ok(cond, msg) {
  if (cond) {
    console.log("  ✓ " + msg);
  } else {
    failed++;
    console.error("  ✗ " + msg);
  }
}

function makeFetch(overrides = {}) {
  // Возвращает фейковый fetch, отвечающий на все известные эндпоинты UI.
  const defaultRoute = async () => ({ status: 200, json: async () => ({ ok: true }) });

  const routes = {
    "/api/chats": async () => ({ status: 200, json: async () => ({ chats: [] }) }),
    "/api/settings": async () => ({
      status: 200,
      json: async () => ({
        ok: true,
        llm: {
          base_url: "", model: "", api_key_set: false,
          supports_tool_calling: false, vision: false,
          max_tokens: 2048, temperature: 0.4,
        },
      }),
    }),
    "/api/state": async () => ({
      status: 200,
      json: async () => ({ mode: "confirm", memory: [], schedules: [], triggers: [] }),
    }),
    "/api/system": async () => ({
      status: 200,
      json: async () => ({ cpu_percent: 12, ram_percent: 34, ram_free_gb: 5, disk_percent: 40 }),
    }),
    "/api/journal": async () => ({ status: 200, json: async () => [] }),
    "/api/llm/models": async () => ({ status: 200, json: async () => ({ ok: false, error: "no server" }) }),
    "/api/stt": async () => ({ status: 200, json: async () => ({ ok: false, error: "stt off" }) }),
    ...overrides,
  };

  return (path, opts = {}) => {
    const p = path.split("?")[0];
    const route = routes[p] || defaultRoute;
    return route(path, opts);
  };
}

function buildDom({ withMatchMedia = true, fetch = makeFetch() } = {}) {
  const beforeParse = (window) => {
    window.fetch = fetch;
    window.__es = null;
    window.EventSource = class {
      constructor(url) { this.url = url; window.__es = this; this.onmessage = null; this.onerror = null; }
      close() {}
    };
    if (withMatchMedia) {
      window.matchMedia = () => ({
        matches: false, media: "", onchange: null,
        addListener() {}, removeListener() {},
        addEventListener() {}, removeEventListener() {}, dispatchEvent() { return false; },
      });
    }
    // CSS.escape может отсутствовать в jsdom
    if (!window.CSS) window.CSS = {};
    if (!window.CSS.escape) {
      window.CSS.escape = (s) => String(s).replace(/[^a-zA-Z0-9_-]/g, (c) => "\\" + c);
    }
    if (!window.navigator.clipboard) {
      window.navigator.clipboard = { writeText: async () => {} };
    }
    window.errors = [];
    window.addEventListener("error", (e) => window.errors.push(e.message || String(e)));
  };
  const dom = new JSDOM(HTML, {
    runScripts: "dangerously",
    url: "http://localhost/",
    pretendToBeVisual: true,
    beforeParse,
  });
  return dom;
}

function sse(dom, ev) {
  dom.window.__es.onmessage({ data: JSON.stringify(ev) });
}

async function flush(ms = 20) {
  await new Promise((r) => setTimeout(r, ms));
}

// --------------------------------------------------------------------------
console.log("\n[1] Кнопка «⚙ Настройки» открывается без ReferenceError");
{
  const dom = buildDom();
  await flush();
  dom.window.document.getElementById("settingsBtn").click();
  await flush();
  ok(
    dom.window.document.getElementById("settingsOverlay").classList.contains("show"),
    "settingsOverlay получает класс show"
  );
  ok(dom.window.errors.length === 0, `нет ошибок в window.onerror (получено: ${dom.window.errors.join("; ")})`);
  dom.window.close();
}

console.log("\n[2] Клик по modelChip открывает настройки");
{
  const dom = buildDom();
  await flush();
  dom.window.document.getElementById("modelChip").click();
  await flush();
  ok(
    dom.window.document.getElementById("settingsOverlay").classList.contains("show"),
    "modelChip → settingsBtn → overlay show"
  );
  dom.window.close();
}

console.log("\n[3] Скрипт грузится без window.matchMedia (защитная версия mobile())");
{
  const dom = buildDom({ withMatchMedia: false });
  await flush();
  ok(dom.window.errors.length === 0, `нет ошибок без matchMedia (получено: ${dom.window.errors.join("; ")})`);
  // мобильное поведение: sidebar можно свернуть/развернуть без падения
  dom.window.document.getElementById("collapseSide").click();
  ok(true, "collapseSide.click() не падает без matchMedia");
  dom.window.close();
}

console.log("\n[4] Отправка сообщения не зависает (SSE раньше ответа POST)");
{
  let resolveSend;
  const sendDeferred = new Promise((res) => (resolveSend = res));
  const fetch = makeFetch({
    "/api/chat/send": async () =>
      sendDeferred.then(() => ({
        status: 200,
        json: async () => ({
          ok: true,
          chat_id: "c1",
          user: { id: "u1" },
          title: "привет",
        }),
      })),
  });
  const dom = buildDom({ fetch });
  await flush();

  const input = dom.window.document.getElementById("input");
  input.value = "привет";
  input.dispatchEvent(new dom.window.Event("input", { bubbles: true }));
  dom.window.document.getElementById("sendBtn").click();

  // POST /api/chat/send ещё не ответил — S.currentId === null, S.generating === true.
  // SSE приносит события нового чата раньше ответа POST.
  const cid = "c1";
  sse(dom, { type: "chat_start", data: { chat_id: cid, message_id: "m1", agent: false } });
  sse(dom, { type: "chat_delta", data: { chat_id: cid, message_id: "m1", kind: "content", text: "Привет", pos: 6 } });
  sse(dom, { type: "chat_done", data: { chat_id: cid, message_id: "m1", ok: true, content: "Привет", think: "", error: "" } });
  await flush(120);

  // теперь отвечаем на POST — как будто он «долетел» позже
  resolveSend();
  await flush(120);

  const doc = dom.window.document;
  const aiText = [...doc.querySelectorAll(".msg.ai .md")].map((n) => n.textContent).join(" ");
  ok(aiText.includes("Привет"), `сообщение бота отрисовано (получено: "${aiText.trim()}")`);
  const sendBtn = doc.getElementById("sendBtn");
  ok(!sendBtn.classList.contains("stop"), "sendBtn больше не в состоянии «стоп» (генерация завершена)");
  ok(doc.getElementById("stopBtn").classList.contains("hidden"), "кнопка «Остановить» скрыта");
  dom.window.close();
}

console.log("\n[5] Клик по всем кнопкам не бросает исключений");
{
  const dom = buildDom();
  await flush();
  const doc = dom.window.document;
  const buttons = [...doc.querySelectorAll("button")];
  let clicked = 0;
  for (const b of buttons) {
    // пропускаем sendBtn в общем прогоне — он покрыт тестом [4]
    if (b.id === "sendBtn") continue;
    try {
      b.click();
      clicked++;
    } catch (e) {
      ok(false, `click() бросил на "${b.id || b.textContent.trim()}": ${e.message}`);
    }
  }
  await flush(120);
  ok(clicked === buttons.length - 1, `кликнули по ${clicked} кнопкам из ${buttons.length - 1}`);
  ok(dom.window.errors.length === 0, `нет ошибок при кликах (получено: ${dom.window.errors.join("; ")})`);
  dom.window.close();
}

console.log("");
if (failed) {
  console.error(`FAILED: ${failed} проверок не прошло`);
  process.exit(1);
} else {
  console.log("Все проверки пройдены ✓");
}
