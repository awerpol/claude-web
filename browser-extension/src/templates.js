export const ACTIONS = {
  explain: {
    label: "Объяснить",
    menuTitle: "Объяснить выделенное",
    title: "Объяснить выделенное",
  },
  review: {
    label: "Проверить",
    menuTitle: "Проверить этот код",
    title: "Проверка кода",
  },
  rewrite: {
    label: "Переписать",
    menuTitle: "Переписать выделенное",
    title: "Переписать выделенное",
  },
  test: {
    label: "Тесты",
    menuTitle: "Создать тесты",
    title: "Создать тесты",
  },
  custom: {
    label: "Свой вопрос",
    menuTitle: "Свой вопрос",
    title: "Свой вопрос",
  },
  page: {
    label: "Текущая страница",
    menuTitle: "Спросить о текущей странице",
    title: "Текущая страница",
  },
};

export const DEFAULT_SETTINGS = {
  serviceUrl: "http://127.0.0.1:8765",
  token: "",
  assistantMode: "chat",
  cwd: "",
  model: "",
  permissionMode: "default",
};

export function normalizeServiceUrl(url) {
  return String(url || DEFAULT_SETTINGS.serviceUrl).trim().replace(/\/+$/, "");
}

export function assertLocalServiceUrl(url) {
  const parsed = new URL(normalizeServiceUrl(url));
  if (!["http:", "https:"].includes(parsed.protocol)) {
    throw new Error("Адрес сервиса должен быть http/https");
  }
  if (!["127.0.0.1", "localhost", "::1"].includes(parsed.hostname)) {
    throw new Error("Адрес сервиса должен указывать только на локальный localhost/127.0.0.1");
  }
  return parsed.toString().replace(/\/+$/, "");
}

export async function loadSettings() {
  const stored = await chrome.storage.sync.get(DEFAULT_SETTINGS);
  return {
    ...DEFAULT_SETTINGS,
    ...stored,
    serviceUrl: normalizeServiceUrl(stored.serviceUrl),
  };
}

export function actionTitle(action) {
  return (ACTIONS[action] || ACTIONS.custom).title;
}
