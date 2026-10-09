import { renderMarkdown } from "./markdown.js";
import { actionTitle, assertLocalServiceUrl, loadSettings } from "./templates.js";

const $ = (id) => document.getElementById(id);

let currentAsk = null;
let currentController = null;
let currentSessionId = "";
let currentOpenUrl = "";
let answerText = "";
let lastStreamedMessageId = "";
let streamedMessageIds = new Set();
let contextExpanded = false;
let activeTabContext = null;
let activeAssistantMode = "chat";
let requestVersion = 0;
let saveTimer = 0;
let modeSwitchPending = false;
let unresolvedServerSessionId = "";

const TAB_STATES_KEY = "tabStates";
const MAX_TAB_STATES = 20;
const MAX_PAGE_CONTEXT_CHARS = 40000;

function show(el, visible) {
  el.classList.toggle("hidden", !visible);
}

function resizeQuestionBox() {
  const el = $("customQuestion");
  el.style.height = "auto";
  const maxHeight = 132;
  el.style.height = `${Math.min(el.scrollHeight, maxHeight)}px`;
}

function setStatus(text) {
  $("statusLine").textContent = text;
  const dot = $("statusDot");
  if (!dot) return;
  dot.classList.toggle("active", /Подготовка|Останавливаю|Читаю|Подключение|отвечает/.test(text));
  dot.classList.toggle("done", /Готово|Скопировано/.test(text));
}

function setLastError(error) {
  const el = $("lastError");
  const message = error?.message || "";
  el.textContent = message ? `Последняя ошибка: ${message}` : "";
  show(el, Boolean(message));
}

function askSourceLabel(ask) {
  return ask?.sourceType === "page" || ask?.action === "page" ? "Текущая страница" : "Текущее выделение";
}

function renderAnswer() {
  $("answer").innerHTML = renderMarkdown(answerText);
}

function normalizeStateUrl(value) {
  const raw = String(value || "").trim();
  if (!raw) return "";
  try {
    const url = new URL(raw);
    url.hash = "";
    return url.href;
  } catch {
    return raw.split("#")[0];
  }
}

function tabStateKey(context = activeTabContext, mode = activeAssistantMode) {
  const tabId = context?.tabId;
  if (tabId == null) return "";
  const url = normalizeStateUrl(context.pageUrl || context.url || "");
  return `${mode}:${tabId}:${url}`;
}

function createStateSnapshot() {
  if (!currentAsk) return null;
  return {
    ask: currentAsk,
    sessionId: currentSessionId,
    openUrl: currentOpenUrl,
    answerText,
    contextExpanded,
    assistantMode: activeAssistantMode,
    status: $("statusLine").textContent || "",
    running: Boolean(currentController || unresolvedServerSessionId),
    serverSessionUnresolved: Boolean(unresolvedServerSessionId),
    updatedAt: Date.now(),
  };
}

async function saveCurrentTabState() {
  if (saveTimer) {
    clearTimeout(saveTimer);
    saveTimer = 0;
  }
  const key = tabStateKey(currentAsk || activeTabContext);
  const snapshot = createStateSnapshot();
  if (!key || !snapshot) return;
  const stored = await chrome.storage.local.get(TAB_STATES_KEY);
  const tabStates = stored[TAB_STATES_KEY] || {};
  tabStates[key] = snapshot;
  const entries = Object.entries(tabStates).sort((a, b) => (b[1].updatedAt || 0) - (a[1].updatedAt || 0));
  await chrome.storage.local.set({
    [TAB_STATES_KEY]: Object.fromEntries(entries.slice(0, MAX_TAB_STATES)),
  });
}

function scheduleSaveCurrentTabState() {
  if (saveTimer) clearTimeout(saveTimer);
  saveTimer = setTimeout(() => {
    saveTimer = 0;
    saveCurrentTabState().catch(() => {});
  }, 350);
}

function restoreTabState(snapshot) {
  if (!snapshot?.ask) return false;
  if (saveTimer) {
    clearTimeout(saveTimer);
    saveTimer = 0;
  }
  if (currentController) {
    currentController.abort();
    currentController = null;
  }
  requestVersion += 1;
  currentAsk = snapshot.ask;
  currentSessionId = snapshot.sessionId || "";
  unresolvedServerSessionId = (snapshot.serverSessionUnresolved || snapshot.running) ? currentSessionId : "";
  currentOpenUrl = snapshot.openUrl || "";
  answerText = snapshot.answerText || "";
  lastStreamedMessageId = "";
  streamedMessageIds = new Set();
  contextExpanded = Boolean(snapshot.contextExpanded);
  $("subtitle").textContent = actionTitle(currentAsk.action);
  $("sourceKicker").textContent = askSourceLabel(currentAsk);
  $("askTitle").textContent = currentAsk.pageTitle || "Текущая веб-страница";
  $("askUrl").textContent = currentAsk.pageUrl || "";
  $("askUrl").href = currentAsk.pageUrl || "#";
  $("selectionPreview").textContent = selectedPreview(currentAsk.selectedText);
  updateContextToggle();
  $("customQuestion").value = "";
  resizeQuestionBox();
  renderAnswer();
  $("copyBtn").disabled = !answerText.trim();
  $("continueBtn").disabled = !currentOpenUrl;
  $("stopBtn").disabled = true;
  $("askBtn").disabled = false;
  show($("emptyState"), false);
  show($("workState"), true);
  show($("answerState"), true);
  setStatus(snapshot.running ? "Ответ был прерван в другой вкладке, можно продолжить" : (snapshot.status || "Готово"));
  return true;
}

async function restoreStateForActiveTab() {
  const key = tabStateKey();
  if (!key) {
    resetPanel("Выделите текст на странице и спросите через контекстное меню");
    return false;
  }
  const stored = await chrome.storage.local.get(TAB_STATES_KEY);
  const snapshot = stored[TAB_STATES_KEY]?.[key];
  if (snapshot && restoreTabState(snapshot)) return true;
  resetPanel("Для этой вкладки ещё нет вопросов");
  return false;
}

function askMatchesActiveTab(ask) {
  if (!ask?.selectedText) return false;
  if (ask.tabId == null || !activeTabContext?.tabId) return true;
  if (ask.tabId !== activeTabContext.tabId) return false;
  const askUrl = normalizeStateUrl(ask.pageUrl || "");
  const activeUrl = normalizeStateUrl(activeTabContext.url || "");
  return !askUrl || !activeUrl || askUrl === activeUrl;
}

function modeCopy() {
  if (activeAssistantMode === "code") {
    return {
      eyebrow: "Проект Code",
      title: "Начать задачу Code с текущей страницы",
      description: "Считывает контекст страницы и передаёт его настроенному локальному проекту для поиска кода, планирования изменений и проверки поведения страницы.",
      capture: "Прочитать текущую страницу",
      placeholder: "Опишите, что нужно найти, изменить или проверить",
      subtitle: "Выделите содержимое страницы и передайте его проекту Code",
      continueLabel: "Перейти в рабочую область Code",
      quickLabels: ["Проверить страницу", "Найти проблему", "Составить план"],
    };
  }
  return {
    eyebrow: "Обычный чат",
    title: "Передать текущую страницу локальному Claude",
    description: "Выделите код или текст и спросите через контекстное меню либо просто прочитайте текущую страницу. Обычный чат только анализирует содержимое страницы и не пишет в проект.",
    capture: "Прочитать текущую страницу",
    placeholder: "Введите вопрос, чтобы продолжить спрашивать о текущей вкладке",
    subtitle: "Выделите текст на странице и спросите через контекстное меню",
    continueLabel: "Продолжить в веб-версии",
    quickLabels: ["Итог страницы", "Объяснить главное", "Составить список"],
  };
}

function renderAssistantMode() {
  const copy = modeCopy();
  document.body.dataset.assistantMode = activeAssistantMode;
  document.querySelectorAll("[data-assistant-mode]").forEach((button) => {
    const selected = button.dataset.assistantMode === activeAssistantMode;
    button.setAttribute("aria-pressed", selected ? "true" : "false");
    button.disabled = modeSwitchPending;
  });
  $("modeEyebrow").textContent = copy.eyebrow;
  $("emptyTitle").textContent = copy.title;
  $("emptyDescription").textContent = copy.description;
  $("emptyCapturePageBtn").textContent = copy.capture;
  $("customQuestion").placeholder = copy.placeholder;
  $("continueBtn").title = copy.continueLabel;
  $("continueBtn").setAttribute("aria-label", copy.continueLabel);
  $("continueBtn").dataset.tooltip = copy.continueLabel;
  document.querySelectorAll("[data-chat-question]").forEach((button, index) => {
    button.textContent = copy.quickLabels[index] || button.textContent;
  });
  if (!currentAsk) $("subtitle").textContent = copy.subtitle;
}

async function setAssistantMode(mode, { persist = true } = {}) {
  const next = mode === "code" ? "code" : "chat";
  if (next === activeAssistantMode || modeSwitchPending) return;
  modeSwitchPending = true;
  renderAssistantMode();
  try {
    if (currentController || unresolvedServerSessionId) {
      setStatus("Останавливаю текущую задачу...");
      const stopped = await stopAsk();
      if (!stopped) {
        if (!persist) await chrome.storage.sync.set({ assistantMode: activeAssistantMode });
        return;
      }
    }
    await saveCurrentTabState().catch(() => {});
    activeAssistantMode = next;
    if (persist) await chrome.storage.sync.set({ assistantMode: next });
    await restoreStateForActiveTab();
  } finally {
    modeSwitchPending = false;
    renderAssistantMode();
  }
}

function activeTabLabel() {
  return activeTabContext?.title || activeTabContext?.url || "Текущая вкладка";
}

function resetPanel(message = modeCopy().subtitle) {
  requestVersion += 1;
  if (saveTimer) {
    clearTimeout(saveTimer);
    saveTimer = 0;
  }
  if (currentController) {
    currentController.abort();
    currentController = null;
  }
  currentAsk = null;
  currentSessionId = "";
  currentOpenUrl = "";
  answerText = "";
  lastStreamedMessageId = "";
  streamedMessageIds = new Set();
  $("subtitle").textContent = message;
  $("customQuestion").value = "";
  resizeQuestionBox();
  $("answer").innerHTML = "";
  $("copyBtn").disabled = true;
  $("continueBtn").disabled = true;
  $("stopBtn").disabled = true;
  $("askBtn").disabled = false;
  show($("workState"), false);
  show($("answerState"), false);
  show($("emptyState"), true);
}

function resetForDifferentTab() {
  saveCurrentTabState().then(() => restoreStateForActiveTab()).catch(() => {
    resetPanel("Для этой вкладки ещё нет вопросов");
  });
  chrome.storage.local.remove("pendingAsk").catch(() => {});
}

function selectedPreview(text) {
  const value = String(text || "").trim();
  return value.length > 3500 ? value.slice(0, 3500) + "\n\n...предпросмотр обрезан" : value;
}

async function getActiveTab() {
  if (activeTabContext?.tabId != null) {
    try {
      return await chrome.tabs.get(activeTabContext.tabId);
    } catch {}
  }
  const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
  return tabs?.[0] || null;
}

function extractReadablePageText() {
  const blockedTags = new Set(["SCRIPT", "STYLE", "NOSCRIPT", "TEMPLATE", "SVG", "CANVAS"]);
  const walker = document.createTreeWalker(document.body || document.documentElement, NodeFilter.SHOW_TEXT, {
    acceptNode(node) {
      const text = node.nodeValue || "";
      if (!text.trim()) return NodeFilter.FILTER_REJECT;
      const parent = node.parentElement;
      if (!parent || blockedTags.has(parent.tagName)) return NodeFilter.FILTER_REJECT;
      const style = window.getComputedStyle(parent);
      if (style && (style.display === "none" || style.visibility === "hidden")) return NodeFilter.FILTER_REJECT;
      return NodeFilter.FILTER_ACCEPT;
    },
  });
  const parts = [];
  let total = 0;
  while (walker.nextNode()) {
    const text = (walker.currentNode.nodeValue || "").replace(/\s+/g, " ").trim();
    if (!text) continue;
    parts.push(text);
    total += text.length + 1;
    if (total > 60000) break;
  }
  const headings = Array.from(document.querySelectorAll("h1,h2,h3"))
    .map((el) => (el.innerText || el.textContent || "").replace(/\s+/g, " ").trim())
    .filter(Boolean)
    .slice(0, 24);
  const metaDescription = document.querySelector('meta[name="description"]')?.content || "";
  return {
    title: document.title || "",
    url: location.href,
    description: metaDescription.trim(),
    headings,
    text: parts.join("\n"),
  };
}

function pageContextText(result) {
  const headings = Array.isArray(result?.headings) && result.headings.length
    ? `Заголовки страницы:\n${result.headings.map((h) => `- ${h}`).join("\n")}\n\n`
    : "";
  const description = result?.description ? `Описание страницы: ${result.description}\n\n` : "";
  const body = String(result?.text || "").trim();
  const combined = `${description}${headings}Текст страницы:\n${body}`.trim();
  return combined.length > MAX_PAGE_CONTEXT_CHARS
    ? combined.slice(0, MAX_PAGE_CONTEXT_CHARS) + "\n\n...текст страницы обрезан"
    : combined;
}

async function captureCurrentPage() {
  if (currentController || unresolvedServerSessionId) return false;
  setStatus("Читаю текущую страницу...");
  try {
    const tab = await getActiveTab();
    if (!tab?.id) throw new Error("Не удалось найти текущую вкладку");
    const url = tab.url || "";
    if (!/^https?:|^file:/.test(url)) {
      throw new Error("Этот тип страницы не поддерживает чтение");
    }
    if (!chrome.scripting?.executeScript) {
      throw new Error("У расширения нет разрешения на чтение страниц. Обновите расширение Claude Code Web на chrome://extensions и повторите");
    }
    const [injection] = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: extractReadablePageText,
    });
    const result = injection?.result || {};
    const text = pageContextText(result);
    if (!text.trim()) throw new Error("Не удалось прочитать видимый текст");
    renderAsk({
      id: crypto.randomUUID(),
      action: "page",
      sourceType: "page",
      selectedText: text,
      pageUrl: result.url || tab.url || "",
      pageTitle: result.title || tab.title || "Текущая страница",
      tabId: tab.id,
      windowId: tab.windowId ?? null,
      createdAt: Date.now(),
    });
    return true;
  } catch (error) {
    setStatus(`Ошибка чтения: ${error.message || error}`);
    setLastError(error);
    return false;
  }
}

async function startQuickPageAsk(question) {
  const ready = await captureCurrentPage();
  if (!ready || !currentAsk) return;
  $("customQuestion").value = question;
  resizeQuestionBox();
  await sendAsk();
}

async function startNewAsk() {
  if (currentController || unresolvedServerSessionId) {
    setStatus("Останавливаю текущую задачу...");
    if (!await stopAsk()) return;
  }
  const key = tabStateKey(currentAsk || activeTabContext);
  if (key) {
    const stored = await chrome.storage.local.get(TAB_STATES_KEY);
    const states = stored[TAB_STATES_KEY] || {};
    delete states[key];
    await chrome.storage.local.set({ [TAB_STATES_KEY]: states });
  }
  resetPanel(modeCopy().subtitle);
}

function renderAsk(ask) {
  if (!askMatchesActiveTab(ask)) {
    resetForDifferentTab();
    return;
  }
  requestVersion += 1;
  currentAsk = ask;
  currentSessionId = "";
  currentOpenUrl = "";
  answerText = "";
  lastStreamedMessageId = "";
  streamedMessageIds = new Set();
  $("subtitle").textContent = actionTitle(ask.action);
  $("sourceKicker").textContent = askSourceLabel(ask);
  $("askTitle").textContent = ask.pageTitle || "Текущая веб-страница";
  $("askUrl").textContent = ask.pageUrl || "";
  $("askUrl").href = ask.pageUrl || "#";
  $("selectionPreview").textContent = selectedPreview(ask.selectedText);
  contextExpanded = false;
  updateContextToggle();
  $("customQuestion").value = "";
  resizeQuestionBox();
  $("answer").innerHTML = "";
  $("copyBtn").disabled = true;
  $("continueBtn").disabled = true;
  show($("emptyState"), false);
  setLastError(null);
  show($("workState"), true);
  show($("answerState"), false);
  chrome.storage.local.remove("pendingAsk").catch(() => {});
  saveCurrentTabState().catch(() => {});
  if (ask.action !== "custom" && ask.action !== "page") sendAsk();
}

function getEventText(obj) {
  if (obj.type === "stream_event") {
    if (obj.event?.type === "message_start") {
      lastStreamedMessageId = obj.event.message?.id || "";
      if (lastStreamedMessageId) streamedMessageIds.add(lastStreamedMessageId);
      return "";
    }
    if (obj.event?.type === "content_block_delta") {
      const delta = obj.event.delta || {};
      return delta.type === "text_delta" ? (delta.text || "") : "";
    }
    return "";
  }
  if (obj.type === "assistant" && Array.isArray(obj.message?.content)) {
    if (obj.message.id && streamedMessageIds.has(obj.message.id)) return "";
    return obj.message.content
      .filter((block) => block && block.type === "text")
      .map((block) => block.text || "")
      .join("\n");
  }
  if (obj.type === "error") {
    throw new Error(obj.message || "Claude returned an error");
  }
  return "";
}

async function sendAsk() {
  if (!currentAsk || currentController || unresolvedServerSessionId) return;
  if (!askMatchesActiveTab(currentAsk)) {
    resetForDifferentTab();
    return;
  }
  const ask = currentAsk;
  const thisRequestVersion = requestVersion;
  const settings = await loadSettings();
  if (thisRequestVersion !== requestVersion) return;
  if (!settings.token) {
    setStatus("Сначала укажите Token на странице настроек");
    chrome.runtime.openOptionsPage();
    return;
  }
  if (activeAssistantMode === "code" && !String(settings.cwd || "").trim()) {
    setStatus("В режиме Code нужно сначала настроить каталог проекта");
    setLastError(new Error("Укажите каталог проекта Code в настройках расширения"));
    chrome.runtime.openOptionsPage();
    return;
  }
  let serviceUrl = "";
  try {
    serviceUrl = assertLocalServiceUrl(settings.serviceUrl);
  } catch (error) {
    if (thisRequestVersion !== requestVersion) return;
    setStatus(error.message || String(error));
    chrome.runtime.openOptionsPage();
    return;
  }
  const question = $("customQuestion").value.trim();
  if (ask.action === "custom" && !question) {
    setStatus("Введите свой вопрос");
    return;
  }
  if (ask.action === "page" && !question) {
    setStatus("Введите вопрос о текущей странице");
    return;
  }
  if (question) {
    $("customQuestion").value = "";
    resizeQuestionBox();
  }
  saveCurrentTabState().catch(() => {});

  if (!currentSessionId) currentSessionId = crypto.randomUUID();
  unresolvedServerSessionId = currentSessionId;
  currentController = new AbortController();
  let requestCompleted = false;
  answerText = "";
  $("answer").innerHTML = "";
  $("stopBtn").disabled = false;
  $("askBtn").disabled = true;
  $("copyBtn").disabled = true;
  $("continueBtn").disabled = true;
  show($("answerState"), true);
  setStatus("Подключение к Claude Code Web...");
  saveCurrentTabState().catch(() => {});

  try {
    const resp = await fetch(`${serviceUrl}/api/extension/ask`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Claude-Web-Extension-Token": settings.token,
      },
      body: JSON.stringify({
        action: ask.action,
        selected_text: ask.selectedText,
        context_type: ask.sourceType || (ask.action === "page" ? "page" : "selection"),
        question,
        page_url: ask.pageUrl,
        page_title: ask.pageTitle,
        cwd: activeAssistantMode === "code" ? settings.cwd : null,
        model: $("modelSelect").value || settings.model || null,
        permission_mode: activeAssistantMode === "code" ? (settings.permissionMode || "default") : "readonly",
        workspace_mode: activeAssistantMode,
        session_id: currentSessionId,
      }),
      signal: currentController.signal,
    });
    if (!resp.ok || !resp.body) {
      let detail = `HTTP ${resp.status}`;
      try {
        const data = await resp.json();
        detail = data.detail || detail;
      } catch {}
      throw new Error(detail);
    }

    setStatus("Claude отвечает...");
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const chunks = buffer.split("\n\n");
      buffer = chunks.pop() || "";
      for (const chunk of chunks) {
        const line = chunk.split("\n").find((item) => item.startsWith("data: "));
        if (!line) continue;
        const obj = JSON.parse(line.slice(6));
        if (obj.type === "extension_meta") {
          if (thisRequestVersion !== requestVersion) return;
          currentSessionId = obj.session_id || "";
          currentOpenUrl = obj.open_url || "";
          $("continueBtn").disabled = !currentOpenUrl;
          scheduleSaveCurrentTabState();
          continue;
        }
        const text = getEventText(obj);
        if (text) {
          if (thisRequestVersion !== requestVersion) return;
          answerText += text;
          renderAnswer();
          scheduleSaveCurrentTabState();
        }
        if (obj.type === "done") {
          setStatus("Готово");
          saveCurrentTabState().catch(() => {});
        }
      }
    }
    requestCompleted = true;
    if (!answerText.trim()) setStatus("Готово, но без текста");
    else setStatus("Готово");
    saveCurrentTabState().catch(() => {});
  } catch (error) {
    if (thisRequestVersion !== requestVersion) return;
    if (error.name === "AbortError") setStatus("Остановлено");
    else setStatus(`Ошибка: ${error.message || error}`);
    saveCurrentTabState().catch(() => {});
  } finally {
    if (thisRequestVersion !== requestVersion) return;
    if (requestCompleted) unresolvedServerSessionId = "";
    currentController = null;
    $("stopBtn").disabled = !unresolvedServerSessionId;
    $("askBtn").disabled = Boolean(unresolvedServerSessionId);
    $("copyBtn").disabled = !answerText.trim();
    $("continueBtn").disabled = !currentOpenUrl;
    saveCurrentTabState().catch(() => {});
  }
}

async function stopAsk() {
  const controller = currentController;
  if (controller) controller.abort();
  const sessionId = unresolvedServerSessionId || currentSessionId;
  let stopped = false;
  try {
    const settings = await loadSettings();
    if (sessionId && settings.token) {
      const serviceUrl = assertLocalServiceUrl(settings.serviceUrl);
      const response = await fetch(`${serviceUrl}/api/extension/stop/${encodeURIComponent(sessionId)}`, {
        method: "POST",
        headers: { "X-Claude-Web-Extension-Token": settings.token },
      });
      if (!response.ok && response.status !== 404) throw new Error(`HTTP ${response.status}`);
    }
    stopped = true;
    if (unresolvedServerSessionId === sessionId) unresolvedServerSessionId = "";
    setStatus("Остановлено");
    return true;
  } catch (error) {
    setStatus(`Не удалось остановить: ${error.message || error}`);
    return false;
  } finally {
    if (stopped && currentController === controller) currentController = null;
    $("stopBtn").disabled = !unresolvedServerSessionId;
    $("askBtn").disabled = Boolean(unresolvedServerSessionId);
    $("copyBtn").disabled = !answerText.trim();
    $("continueBtn").disabled = !currentOpenUrl;
    saveCurrentTabState().catch(() => {});
  }
}

async function copyAnswer() {
  if (!answerText.trim()) return;
  await navigator.clipboard.writeText(answerText);
  setStatus("Скопировано");
  saveCurrentTabState().catch(() => {});
}

async function openContinue() {
  if (currentOpenUrl) {
    await chrome.tabs.create({ url: currentOpenUrl });
  }
}

function updateContextToggle() {
  const preview = $("selectionPreview");
  preview.classList.toggle("expanded", contextExpanded);
  preview.classList.toggle("collapsed", !contextExpanded);
  const label = contextExpanded ? "Свернуть контекст" : "Развернуть контекст";
  $("toggleContextBtn").title = label;
  $("toggleContextBtn").setAttribute("aria-label", label);
  $("toggleContextTextBtn").textContent = contextExpanded ? "Свернуть" : "Развернуть";
}

function toggleContext() {
  contextExpanded = !contextExpanded;
  updateContextToggle();
  saveCurrentTabState().catch(() => {});
}

function renderModelSelect(settings) {
  const modelSelect = $("modelSelect");
  const value = (settings.model || "").trim();
  if (value && Array.from(modelSelect.options).some((option) => option.value === value)) {
    modelSelect.value = value;
  } else {
    modelSelect.value = "";
  }
}

async function loadPendingAsk() {
  const { pendingAsk, lastExtensionError, activeTabContext: storedActiveTabContext } = await chrome.storage.local.get([
    "pendingAsk",
    "lastExtensionError",
    "activeTabContext",
  ]);
  activeTabContext = storedActiveTabContext || activeTabContext;
  if (pendingAsk?.selectedText) {
    renderAsk(pendingAsk);
    return;
  }
  if (lastExtensionError?.message && Date.now() - lastExtensionError.createdAt < 60000) {
    setLastError(lastExtensionError);
  }
  await restoreStateForActiveTab();
}

$("askBtn").addEventListener("click", sendAsk);
$("stopBtn").addEventListener("click", stopAsk);
$("copyBtn").addEventListener("click", copyAnswer);
$("continueBtn").addEventListener("click", openContinue);
$("capturePageBtn").addEventListener("click", captureCurrentPage);
$("openOptionsBtn").addEventListener("click", () => {
  saveCurrentTabState().catch(() => {});
  chrome.runtime.openOptionsPage();
});
$("emptyOptionsBtn").addEventListener("click", () => {
  saveCurrentTabState().catch(() => {});
  chrome.runtime.openOptionsPage();
});
$("emptyCapturePageBtn").addEventListener("click", captureCurrentPage);
$("newAskBtn").addEventListener("click", startNewAsk);
document.querySelectorAll("[data-chat-question]").forEach((button) => {
  button.addEventListener("click", () => {
    const question = activeAssistantMode === "code" ? button.dataset.codeQuestion : button.dataset.chatQuestion;
    startQuickPageAsk(question || "");
  });
});
document.querySelectorAll("[data-assistant-mode]").forEach((button) => {
  button.addEventListener("click", () => setAssistantMode(button.dataset.assistantMode));
});
$("toggleContextBtn").addEventListener("click", toggleContext);
$("toggleContextTextBtn").addEventListener("click", toggleContext);
$("customQuestion").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    sendAsk();
  }
});
$("customQuestion").addEventListener("input", resizeQuestionBox);
$("modelSelect").addEventListener("change", () => {
  chrome.storage.sync.set({ model: $("modelSelect").value }).catch(() => {});
});
const initialSettings = await loadSettings();
activeAssistantMode = initialSettings.assistantMode === "code" ? "code" : "chat";
renderAssistantMode();
renderModelSelect(initialSettings);
resizeQuestionBox();

chrome.storage.onChanged.addListener((changes, area) => {
  if (area === "sync" && changes.assistantMode?.newValue) {
    const nextMode = changes.assistantMode.newValue === "code" ? "code" : "chat";
    if (nextMode !== activeAssistantMode) {
      setAssistantMode(nextMode, { persist: false }).catch(() => {});
    }
  }
  if (area === "local" && changes.activeTabContext?.newValue) {
    const nextContext = changes.activeTabContext.newValue;
    const previousContext = activeTabContext;
    const switchedTab = !previousContext || previousContext.tabId !== nextContext.tabId;
    const switchedUrl = !previousContext || normalizeStateUrl(previousContext.url) !== normalizeStateUrl(nextContext.url);
    saveCurrentTabState().catch(() => {});
    activeTabContext = nextContext;
    if (currentAsk && (switchedTab || switchedUrl) && !askMatchesActiveTab(currentAsk)) {
      resetForDifferentTab();
    } else if (switchedTab || switchedUrl) {
      restoreStateForActiveTab().catch(() => {});
    }
  }
  if (area === "local" && changes.pendingAsk?.newValue) {
    renderAsk(changes.pendingAsk.newValue);
  }
  if (area === "local" && changes.lastExtensionError?.newValue) {
    setLastError(changes.lastExtensionError.newValue);
  }
});

loadPendingAsk();
