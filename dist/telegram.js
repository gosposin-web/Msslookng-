"use strict";

const byId = id => document.getElementById(id);
const api = "/api/telegram";
const loginDialog = byId("login-dialog");
const proxyDialog = byId("proxy-dialog");
const stepControls = {
  start: ["telegram-phone", "telegram-proxy"],
  code: ["telegram-code"],
  password: ["telegram-password"]
};
const submitText = {
  start: "Получить код",
  code: "Подтвердить код",
  password: "Подключить"
};

let accounts = [];
let proxies = [];
let attempt = null;
let step = "start";
let loginBusy = false;
let proxyBusy = false;
let listRevision = 0;
let toastTimer;
let storyReady = false;
let storyLoading = false;
let storyRevision = 0;
let storyFilterRevision = 0;
let storyFeed = null;
let storyFilter = new Set();

function message(id, text = "") {
  const element = byId(id);
  element.textContent = text;
  element.hidden = !text;
}

function toast(text) {
  clearTimeout(toastTimer);
  message("telegram-toast", text);
  toastTimer = setTimeout(() => message("telegram-toast"), 3000);
}

async function request(path = "", method = "GET", data) {
  let response;
  try {
    response = await fetch(api + path, {
      method,
      headers: data ? { "Content-Type": "application/json" } : undefined,
      body: data ? JSON.stringify(data) : undefined,
      cache: "no-store",
      redirect: "error"
    });
  } catch {
    throw new Error("Связь с сервером прервалась. Повтори попытку.");
  }
  if (response.status === 204) return null;
  const result = await response.json().catch(() => null);
  if (!response.ok || !result) {
    const delay = Number(response.headers.get("Retry-After"));
    const retry = delay > 0 ? ` Подожди ${Math.ceil(delay)} сек.` : "";
    const error = new Error((result?.detail || "Сервер Telegram пока недоступен.") + retry);
    error.status = response.status;
    throw error;
  }
  return result;
}

function line(tag, className, text) {
  const element = document.createElement(tag);
  element.className = className;
  element.textContent = text;
  return element;
}

function record(name, description, action, onClick) {
  const row = line("div", "result-row", "");
  const title = line("div", "", "");
  title.append(line("div", "participant-name", name), line("div", "participant-chat", description));
  const button = line("button", "button secondary", action);
  button.type = "button";
  button.addEventListener("click", onClick);
  row.append(title, button);
  return row;
}

function renderLists() {
  const accountList = byId("connected-accounts");
  const proxyList = byId("saved-proxies");
  accountList.replaceChildren(...accounts.map(account => {
    const name = [account.first_name, account.last_name].filter(Boolean).join(" ") || "Аккаунт Telegram";
    const route = proxies.find(proxy => proxy.id === account.proxy_id)?.name || "Без прокси";
    const details = account.username ? `@${account.username} · ${route}` : route;
    return record(name, details, "Отключить", event => removeItem(event.currentTarget, "accounts", account.id));
  }));
  proxyList.replaceChildren(...proxies.map(proxy => {
    const protocol = proxy.protocol === "socks5" ? "SOCKS5" : "HTTP CONNECT";
    return record(proxy.name, `${protocol} · ${proxy.host}:${proxy.port}`, "Удалить", event => removeItem(event.currentTarget, "proxies", proxy.id));
  }));
  if (accounts.length === 0) accountList.append(line("p", "empty", "Добавь свой аккаунт, чтобы подключить Telegram."));
  if (proxies.length === 0) proxyList.append(line("p", "empty", "Можно использовать прямое подключение или добавить прокси."));
}

function updateStoryControls() {
  const select = byId("story-account");
  select.disabled = !storyReady || !accounts.length || storyLoading;
  const button = byId("load-stories");
  button.disabled = !storyReady || !select.value || storyLoading;
  button.textContent = storyLoading ? "Загружаем…" : storyFeed ? "Обновить ленту" : "Загрузить ленту";
}

function renderStoryFeed() {
  const results = byId("story-results");
  results.replaceChildren();
  let status = "Выбери аккаунт и загрузи ленту.";
  if (!storyReady || !accounts.length) status = "Подключи аккаунт Telegram, чтобы загрузить ленту.";
  if (storyLoading) status = "Запрашиваем активные сторис в Telegram…";
  if (storyFeed) {
    const matches = storyFeed.profiles.filter(profile =>
      storyFilter.size === 0 || profile.usernames.some(username => storyFilter.has(username))
    );
    const count = matches.reduce((total, profile) => total + profile.story_ids.length, 0);
    status = `Профилей ${matches.length} · Сторис ${count}`;
    if (!storyFeed.complete) status += " · Лента загружена частично";
    for (const profile of matches) {
      const row = line("div", "result-row", "");
      const title = line("div", "", "");
      const username = profile.usernames[0];
      const description = [username && `@${username}`, `Сторис ${profile.story_ids.length}`].filter(Boolean).join(" · ");
      title.append(line("div", "participant-name", profile.name), line("div", "participant-chat", description));
      row.append(title);
      const firstStory = profile.story_ids[0];
      if (/^[a-z0-9_]{1,32}$/i.test(username ?? "") && Number.isSafeInteger(firstStory) && firstStory > 0) {
        const link = line("a", "button secondary", "Открыть");
        link.href = `https://t.me/${username}/s/${firstStory}`;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        link.setAttribute("aria-label", `Открыть сторис ${profile.name} в Telegram`);
        row.append(link);
      }
      results.append(row);
    }
  }
  message("story-status", status);
  updateStoryControls();
}

function resetStoryFeed() {
  storyRevision++;
  storyLoading = false;
  storyFeed = null;
  message("story-error");
  renderStoryFeed();
}

function renderStoryAccounts(ready) {
  const select = byId("story-account");
  const previous = select.value;
  storyReady = ready;
  select.replaceChildren(...(ready ? accounts : []).map(account => {
    const name = [account.first_name, account.last_name].filter(Boolean).join(" ") || "Аккаунт Telegram";
    const handle = account.username ? ` · @${account.username}` : "";
    return new Option(name + handle, account.id);
  }));
  if (ready && accounts.some(account => account.id === previous)) select.value = previous;
  if (select.value !== previous || !ready) resetStoryFeed();
  else renderStoryFeed();
}

function parseStoryFilter(text) {
  const usernames = new Set();
  let skipped = 0;
  const headings = new Set(["username", "usernames", "profile", "channel", "url", "ссылка", "профиль", "канал"]);
  for (const raw of text.split(/[\r\n,;\t]+/)) {
    const entry = raw.trim().replace(/^"(.*)"$/, "$1").trim();
    if (!entry || headings.has(entry.toLowerCase())) continue;
    const name = entry.match(/^(?:https?:\/\/)?(?:t\.me|telegram\.me)\/([a-z0-9_]{1,32})\/?(?:\?[^\s]*)?$/i)?.[1] ?? entry.replace(/^@/, "");
    if (/^[a-z0-9_]{1,32}$/i.test(name)) usernames.add(name.toLowerCase());
    else skipped++;
  }
  return { usernames, skipped };
}

async function loadStoryFilter(event) {
  const revision = ++storyFilterRevision;
  const file = event.target.files[0];
  if (!file) return;
  let parsed;
  try {
    if (!/\.(txt|csv)$/i.test(file.name) || file.size > 128 * 1024) {
      throw new Error("Выбери TXT или CSV размером до 128 КБ.");
    }
    parsed = parseStoryFilter(await file.text());
    if (!parsed.usernames.size) throw new Error("В файле должны быть @username или ссылки t.me на профили.");
  } catch (error) {
    if (revision === storyFilterRevision) message("story-error", error.message);
    return;
  }
  if (revision !== storyFilterRevision) return;
  storyFilter = parsed.usernames;
  const skipped = parsed.skipped ? ` · Пропущено ${parsed.skipped}` : "";
  message("story-filter-status", `Профилей в фильтре ${storyFilter.size}${skipped}`);
  byId("clear-story-filter").hidden = false;
  message("story-error");
  renderStoryFeed();
}

async function loadStoryFeed() {
  const id = byId("story-account").value;
  if (!storyReady || !id || storyLoading) return;
  const revision = ++storyRevision;
  storyLoading = true;
  storyFeed = null;
  message("story-error");
  renderStoryFeed();
  try {
    const response = await request(`/accounts/${encodeURIComponent(id)}/stories`);
    if (revision !== storyRevision) return;
    if (!Array.isArray(response?.profiles)) throw new Error("Telegram не вернул ленту сторис.");
    storyFeed = response;
  } catch (error) {
    if (revision === storyRevision) message("story-error", error.message);
  } finally {
    if (revision === storyRevision) {
      storyLoading = false;
      renderStoryFeed();
    }
  }
}

async function loadLists() {
  const revision = ++listRevision;
  byId("retry-telegram").disabled = true;
  try {
    const result = await request();
    if (revision !== listRevision) return;
    if (!Array.isArray(result?.accounts) || !Array.isArray(result?.proxies)) throw new Error("Сервер Telegram пока недоступен.");
    accounts = result.accounts;
    proxies = result.proxies;
    renderLists();
    const ready = result.telegram_ready === true;
    renderStoryAccounts(ready);
    byId("connect-account").disabled = !ready;
    byId("add-proxy").disabled = !ready;
    byId("retry-telegram").hidden = ready;
    message("telegram-error", ready ? "" : "Для подключения добавь ключи приложения Telegram в настройках сервера.");
  } catch (error) {
    if (revision !== listRevision) return;
    byId("connect-account").disabled = true;
    byId("add-proxy").disabled = true;
    byId("connected-accounts").replaceChildren();
    byId("saved-proxies").replaceChildren();
    renderStoryAccounts(false);
    byId("retry-telegram").hidden = false;
    message("telegram-error", error.message);
  } finally {
    if (revision === listRevision) byId("retry-telegram").disabled = false;
  }
}

async function removeItem(button, type, id) {
  if (!confirm(type === "accounts" ? "Завершить сессию этого аккаунта в панели?" : "Удалить этот прокси из панели?")) return;
  button.disabled = true;
  try {
    await request(`/${type}/${encodeURIComponent(id)}`, "DELETE");
    await loadLists();
  } catch (error) {
    message("telegram-error", error.message);
    button.disabled = false;
  }
}

function showStep(next, focus = true) {
  step = next;
  for (const [name, fields] of Object.entries(stepControls)) {
    byId("login-" + name).hidden = name !== step;
    for (const id of fields) {
      byId(id).disabled = name !== step;
      byId(id).required = name === step && id !== "telegram-proxy";
    }
  }
  byId("submit-login").textContent = submitText[step];
  if (focus) byId(stepControls[step][0]).focus();
}

async function submitLogin(event) {
  event.preventDefault();
  if (loginBusy) return;
  loginBusy = true;
  const button = byId("submit-login");
  button.disabled = true;
  byId("cancel-login").disabled = true;
  message("login-error");
  try {
    let result;
    if (step === "start") {
      result = await request("/login", "POST", {
        phone: byId("telegram-phone").value.replace(/[()\s-]/g, ""),
        proxy_id: byId("telegram-proxy").value || null
      });
    } else {
      const field = byId("telegram-" + step);
      const value = field.value;
      field.value = "";
      result = await request(`/login/${encodeURIComponent(attempt)}/${step}`, "POST", { [step]: value });
    }
    if (result.step === "done") {
      attempt = null;
      loginDialog.close();
      await loadLists();
      toast("Аккаунт подключён.");
      return;
    }
    if (!result.id || !["code", "password"].includes(result.step)) throw new Error("Telegram не вернул следующий шаг входа.");
    attempt = result.id;
    showStep(result.step);
  } catch (error) {
    if (error.status === 410) {
      attempt = null;
      showStep("start");
    }
    message("login-error", error.message);
  } finally {
    loginBusy = false;
    button.disabled = false;
    byId("cancel-login").disabled = false;
  }
}

async function submitProxy(event) {
  event.preventDefault();
  if (proxyBusy) return;
  proxyBusy = true;
  const button = byId("submit-proxy");
  button.disabled = true;
  button.textContent = "Проверяем соединение…";
  byId("cancel-proxy").disabled = true;
  message("proxy-error");
  try {
    await request("/proxies", "POST", {
      name: byId("proxy-name").value.trim(),
      protocol: byId("proxy-protocol").value,
      host: byId("proxy-host").value.trim(),
      port: Number(byId("proxy-port").value),
      username: byId("proxy-user").value,
      password: byId("proxy-password").value
    });
    proxyDialog.close();
    await loadLists();
    toast("Прокси подключён.");
  } catch (error) {
    message("proxy-error", error.message);
  } finally {
    byId("proxy-password").value = "";
    proxyBusy = false;
    button.disabled = false;
    button.textContent = "Проверить и сохранить";
    byId("cancel-proxy").disabled = false;
  }
}

byId("connect-account").addEventListener("click", () => {
  byId("telegram-login").reset();
  message("login-error");
  const options = [["", "Без прокси"], ...proxies.map(proxy => [proxy.id, proxy.name])];
  byId("telegram-proxy").replaceChildren(...options.map(([id, name]) => new Option(name, id)));
  showStep("start", false);
  loginDialog.showModal();
});
byId("telegram-login").addEventListener("submit", submitLogin);
byId("cancel-login").addEventListener("click", () => loginDialog.close());
loginDialog.addEventListener("cancel", event => { if (loginBusy) event.preventDefault(); });
loginDialog.addEventListener("close", () => {
  byId("telegram-login").reset();
  const id = attempt;
  attempt = null;
  if (id) request(`/login/${encodeURIComponent(id)}`, "DELETE").catch(() => {});
});

byId("add-proxy").addEventListener("click", () => {
  byId("proxy-form").reset();
  message("proxy-error");
  proxyDialog.showModal();
});
byId("proxy-form").addEventListener("submit", submitProxy);
byId("cancel-proxy").addEventListener("click", () => proxyDialog.close());
proxyDialog.addEventListener("cancel", event => { if (proxyBusy) event.preventDefault(); });
proxyDialog.addEventListener("close", () => byId("proxy-form").reset());
byId("retry-telegram").addEventListener("click", loadLists);
byId("story-account").addEventListener("change", resetStoryFeed);
byId("story-file").addEventListener("change", loadStoryFilter);
byId("load-stories").addEventListener("click", loadStoryFeed);
byId("clear-story-filter").addEventListener("click", () => {
  storyFilterRevision++;
  storyFilter.clear();
  byId("story-file").value = "";
  byId("clear-story-filter").hidden = true;
  message("story-error");
  message("story-filter-status");
  renderStoryFeed();
});

loadLists();
