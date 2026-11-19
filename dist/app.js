"use strict";

const byId = id => document.getElementById(id);
const storageKey = "stories-panel-demo-v1";
const usernamePattern = /^[a-z][a-z0-9_]{4,31}$/i;
const colors = ["lavender", "green", "peach", "blue"];
const pluralRules = new Intl.PluralRules("ru");
const clock = new Intl.DateTimeFormat("ru-RU", { hour: "2-digit", minute: "2-digit" });

const initialState = {
  accounts: [
    { id: "demo-1", first: "Лера", last: "Соколова", username: "demo_lera", connected: true, color: "lavender", avatar: "" },
    { id: "demo-2", first: "Даниил", last: "Орлов", username: "demo_daniil", connected: true, color: "green", avatar: "" },
    { id: "demo-3", first: "Анна", last: "Белова", username: "demo_anna", connected: false, color: "peach", avatar: "" }
  ],
  source: null,
  selectedAccount: "demo-1",
  limit: 20,
  results: [],
  events: []
};

const state = loadState();
const dialog = byId("profile-dialog");
const profileForm = byId("profile-form");
let draft = null;
let importNotice = "";
let importPending = false;
let checking = false;
let viewing = false;
let viewTimer;
let toastTimer;

function loadState() {
  try {
    const saved = JSON.parse(localStorage.getItem(storageKey));
    if (saved && Array.isArray(saved.accounts) && Array.isArray(saved.results) && Array.isArray(saved.events)) {
      return { ...initialState, ...saved };
    }
  } catch {
    // Fall back to demo data when browser storage is unavailable.
  }
  return structuredClone(initialState);
}

function saveState() {
  try {
    localStorage.setItem(storageKey, JSON.stringify(state));
    return true;
  } catch {
    return false;
  }
}

function escapeText(value) {
  return String(value ?? "").replace(/[&<>"']/g, character => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  })[character]);
}

function quantity(count, one, few, many) {
  const words = { one, few, many };
  return `${count} ${words[pluralRules.select(count)] ?? many}`;
}

function fullName(profile) {
  return [profile.first, profile.last].filter(Boolean).join(" ");
}

function avatarMarkup(profile) {
  const initials = [profile.first, profile.last].map(part => part?.[0] ?? "").join("").toUpperCase();
  const color = colors.includes(profile.color) ? profile.color : colors[0];
  const hasPhoto = /^data:image\/jpeg;base64,[A-Za-z0-9+/=]+$/.test(profile.avatar);
  const content = hasPhoto ? `<img src="${profile.avatar}" alt="">` : escapeText(initials || "?");
  return `<div class="avatar ${color}" aria-hidden="true">${content}</div>`;
}

function showToast(message) {
  const toast = byId("toast");
  clearTimeout(toastTimer);
  toast.textContent = message;
  toast.hidden = false;
  toastTimer = setTimeout(() => { toast.hidden = true; }, 3100);
}

function showPage() {
  const page = location.hash === "#stories" ? "stories" : "accounts";
  byId("accounts-page").hidden = page !== "accounts";
  byId("stories-page").hidden = page !== "stories";
  for (const link of document.querySelectorAll("[data-page]")) {
    if (link.dataset.page === page) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
}

function renderAccounts() {
  const activeCount = state.accounts.filter(account => account.connected).length;
  byId("connected-count").textContent = activeCount;
  byId("account-total").textContent = `из ${state.accounts.length} тестовых аккаунтов`;
  byId("profiles-count").textContent = quantity(state.accounts.length, "профиль", "профиля", "профилей");
  byId("account-list").innerHTML = state.accounts.map(account => `
    <article class="account-row">
      ${avatarMarkup(account)}
      <div class="account-meta">
        <div class="account-name">${escapeText(fullName(account))}</div>
        <div class="account-handle">@${escapeText(account.username)}</div>
      </div>
      <span class="pill ${account.connected ? "ready" : "off"}"><span class="status-dot" aria-hidden="true"></span>${account.connected ? "Подключён" : "Отключён"}</span>
      <button type="button" class="button secondary" data-edit="${escapeText(account.id)}" aria-label="Изменить профиль ${escapeText(fullName(account))}">Изменить</button>
    </article>
  `).join("");
}

function showProfileError(message) {
  const error = byId("profile-error");
  error.textContent = message;
  error.hidden = !message;
}

function updateAvatarPreview() {
  byId("avatar-preview").innerHTML = avatarMarkup({
    ...draft,
    first: byId("first-name").value.trim(),
    last: byId("last-name").value.trim()
  });
  byId("remove-avatar").hidden = !draft.avatar;
}

function editProfile(account) {
  draft = account ? { ...account } : {
    first: "", last: "", username: "", avatar: "", connected: true,
    color: colors[state.accounts.length % colors.length]
  };
  byId("dialog-title").textContent = account ? "Изменить профиль" : "Добавить аккаунт";
  byId("demo-sign-in").hidden = Boolean(account);
  byId("first-name").value = draft.first;
  byId("last-name").value = draft.last;
  byId("username").value = draft.username ? `@${draft.username}` : "";
  byId("avatar-file").value = "";
  byId("save-profile").textContent = account ? "Сохранить" : "Создать тестовый профиль";
  byId("save-profile").disabled = false;
  showProfileError("");
  updateAvatarPreview();
  dialog.showModal();
}

async function resizedAvatar(file) {
  if (!["image/jpeg", "image/png", "image/webp"].includes(file.type) || file.size > 8 * 1024 * 1024) {
    throw new Error("Выбери фото в формате JPG, PNG или WebP до 8 МБ.");
  }
  const url = URL.createObjectURL(file);
  try {
    const image = new Image();
    image.src = url;
    await image.decode();
    const canvas = document.createElement("canvas");
    canvas.width = canvas.height = 192;
    const edge = Math.min(image.naturalWidth, image.naturalHeight);
    const context = canvas.getContext("2d");
    context.fillStyle = "#ffffff";
    context.fillRect(0, 0, 192, 192);
    context.drawImage(image, (image.naturalWidth - edge) / 2, (image.naturalHeight - edge) / 2, edge, edge, 0, 0, 192, 192);
    return canvas.toDataURL("image/jpeg", 0.84);
  } finally {
    URL.revokeObjectURL(url);
  }
}

function saveProfile(event) {
  event.preventDefault();
  const first = byId("first-name").value.trim();
  const last = byId("last-name").value.trim();
  const username = byId("username").value.trim().replace(/^@/, "");
  if (!first) return showProfileError("Укажи имя.");
  if (!usernamePattern.test(username)) return showProfileError("Юзернейм начинается с буквы и содержит от 5 до 32 латинских букв, цифр или знаков подчёркивания.");
  if (state.accounts.some(account => account.id !== draft.id && account.username.toLowerCase() === username.toLowerCase())) {
    return showProfileError("Этот юзернейм уже есть в списке аккаунтов.");
  }
  const savedProfile = { ...draft, first, last, username };
  if (draft.id) {
    const index = state.accounts.findIndex(account => account.id === draft.id);
    state.accounts[index] = savedProfile;
  } else {
    savedProfile.id = crypto.randomUUID();
    state.accounts.push(savedProfile);
  }
  const saved = saveState();
  dialog.close();
  renderAccounts();
  renderSettings();
  showToast(saved ? "Тестовый профиль сохранён." : "Профиль изменён до закрытия страницы.");
}

function parseChats(content) {
  const links = /^(?:https?:\/\/)?(?:t\.me|telegram\.me)\/([a-z][a-z0-9_]{4,31})\/?(?:\?[^\s]*)?$/i;
  const headers = ["chat", "chats", "чат", "чаты", "ссылка", "link", "username"];
  const chats = new Set();
  let invalid = 0;
  function addEntry(entry) {
    const value = entry.trim().replace(/^"(.*)"$/, "$1").trim();
    if (!value || headers.includes(value.toLowerCase())) return;
    const username = value.match(links)?.[1] ?? value.replace(/^@/, "");
    if (usernamePattern.test(username)) chats.add(`@${username.toLowerCase()}`);
    else invalid += 1;
  }
  content.split(/[\n,;\t]+/).forEach(addEntry);
  return { chats: [...chats], invalid };
}

function renderImport() {
  const source = state.source;
  const notice = importNotice ? `<p class="import-error small">${escapeText(importNotice)}</p>` : "";
  const extra = source?.chats.length - 5;
  byId("import-info").innerHTML = notice + (source ? `
    <div class="file-details">
      <strong>${escapeText(source.name)}</strong>
      <span class="small muted">${quantity(source.chats.length, "чат", "чата", "чатов")}</span>
    </div>
    <div class="chat-list">
      ${source.chats.slice(0, 5).map(chat => `<span class="chat-tag">${escapeText(chat)}</span>`).join("")}
      ${extra > 0 ? `<span class="chat-tag">Ещё ${extra}</span>` : ""}
    </div>` : "");
}

function useSource(name, content) {
  const { chats, invalid } = parseChats(content);
  importNotice = "";
  if (!chats.length) {
    importNotice = "В файле нужны @username чатов или адреса t.me/username.";
    renderImport();
    return;
  }
  if (invalid) importNotice = `Пропущены записи с неподходящим форматом · ${invalid}`;
  state.source = { name, chats };
  state.results = [];
  addEvent(`Импортировано ${quantity(chats.length, "чат", "чата", "чатов")}`);
  renderImport();
  renderSettings();
  renderResults();
}

function renderSettings() {
  const available = state.accounts.filter(account => account.connected);
  if (!available.some(account => account.id === state.selectedAccount)) state.selectedAccount = available[0]?.id ?? "";
  const account = byId("run-account");
  account.innerHTML = available.map(profile => `<option value="${escapeText(profile.id)}">${escapeText(fullName(profile))} · @${escapeText(profile.username)}</option>`).join("");
  account.value = state.selectedAccount;
  const busy = importPending || checking || viewing;
  account.disabled = busy || !available.length;
  byId("run-limit").value = state.limit;
  byId("run-limit").disabled = busy;
  byId("upload-chats").disabled = busy;
  byId("example-chats").disabled = busy;
  byId("check-stories").disabled = busy || !state.source || !available.length;
  byId("check-stories").textContent = checking ? "Проверяем сторис…" : "Проверить сторис";
  byId("check-hint").textContent = state.source
    ? "Проверка покажет сторис условных участников."
    : "Добавь список чатов для начала проверки.";
}

function addEvent(text) {
  state.events.unshift({ text, at: Date.now() });
  state.events = state.events.slice(0, 25);
  saveState();
  renderEvents();
}

function renderEvents() {
  byId("event-list").innerHTML = state.events.length ? state.events.map(event => `
    <div class="event-row">
      <time class="event-time small muted" datetime="${new Date(event.at).toISOString()}">${clock.format(event.at)}</time>
      <div class="small">${escapeText(event.text)}</div>
    </div>
  `).join("") : '<p class="empty">Здесь появятся записи об импорте, проверке и просмотре.</p>';
}

function renderResults() {
  const withStory = state.results.filter(profile => profile.hasStory).length;
  const viewed = state.results.filter(profile => profile.viewed).length;
  byId("results-summary").textContent = state.results.length
    ? `${quantity(state.results.length, "участник", "участника", "участников")} · Со сторис ${withStory}`
    : "";
  const start = byId("start-views");
  start.hidden = !withStory || viewing || withStory === viewed;
  start.disabled = importPending || checking || !state.selectedAccount;
  start.textContent = viewed ? "Продолжить демо-просмотр" : "Запустить демо-просмотр";
  byId("stop-views").hidden = !viewing;
  byId("run-progress").hidden = !viewing && !viewed;
  byId("progress-label").textContent = `Демо-просмотр · ${viewed} из ${withStory}`;
  byId("views-progress").max = withStory || 1;
  byId("views-progress").value = viewed;
  byId("results-list").innerHTML = state.results.length ? state.results.map(profile => {
    const label = profile.viewed ? "Просмотрено в демо" : profile.hasStory ? "Есть сторис" : "Нет сторис";
    const tone = profile.viewed ? "viewed" : profile.hasStory ? "ready" : "off";
    return `<div class="result-row">
      <div>
        <div class="participant-name">@${escapeText(profile.username)}</div>
        <div class="participant-chat">${escapeText(profile.chat)}</div>
      </div>
      <span class="pill ${tone}">${label}</span>
    </div>`;
  }).join("") : '<p class="empty">Запусти проверку, чтобы увидеть тестовых участников и их сторис.</p>';
}

function checkStories() {
  if (checking || viewing || importPending || !state.source || !state.selectedAccount) return;
  checking = true;
  renderSettings();
  renderResults();
  const count = Math.min(state.source.chats.length * 3, state.limit);
  setTimeout(() => {
    state.results = Array.from({ length: count }, (_, index) => ({
      username: `demo_member_${index + 1}`,
      chat: state.source.chats[Math.floor(index / 3)],
      hasStory: index % 3 !== 1,
      viewed: false
    }));
    checking = false;
    addEvent(`Демо-проверка завершена · ${quantity(count, "участник", "участника", "участников")}`);
    renderSettings();
    renderResults();
    showToast("Демо-проверка завершена.");
  }, 550);
}

function stopViewing(finished = false) {
  if (!viewing) return;
  clearTimeout(viewTimer);
  viewing = false;
  addEvent(finished ? "Демо-просмотр завершён" : "Демо-просмотр остановлен");
  renderSettings();
  renderResults();
}

function markNextView() {
  if (!viewing) return;
  const next = state.results.find(profile => profile.hasStory && !profile.viewed);
  if (!next) {
    stopViewing(true);
    return;
  }
  next.viewed = true;
  saveState();
  renderResults();
  viewTimer = setTimeout(markNextView, 240);
}

function startViewing() {
  if (viewing || checking || importPending || !state.selectedAccount) return;
  if (!state.results.some(profile => profile.hasStory && !profile.viewed)) return;
  viewing = true;
  const account = state.accounts.find(profile => profile.id === state.selectedAccount);
  addEvent(`Демо-просмотр запущен · @${account.username}`);
  renderSettings();
  renderResults();
  viewTimer = setTimeout(markNextView, 240);
}

byId("add-account").addEventListener("click", () => editProfile(null));
byId("account-list").addEventListener("click", event => {
  const button = event.target.closest("[data-edit]");
  if (button) editProfile(state.accounts.find(account => account.id === button.dataset.edit));
});
byId("close-dialog").addEventListener("click", () => dialog.close());
byId("cancel-profile").addEventListener("click", () => dialog.close());
dialog.addEventListener("close", () => { if (!dialog.open) draft = null; });
byId("first-name").addEventListener("input", updateAvatarPreview);
byId("last-name").addEventListener("input", updateAvatarPreview);
profileForm.addEventListener("submit", saveProfile);
byId("remove-avatar").addEventListener("click", () => {
  draft.avatar = "";
  byId("avatar-file").value = "";
  updateAvatarPreview();
});
byId("avatar-file").addEventListener("change", async event => {
  const current = draft;
  const file = event.target.files[0];
  if (!file) return;
  byId("save-profile").disabled = true;
  byId("avatar-file").disabled = true;
  byId("remove-avatar").disabled = true;
  try {
    const avatar = await resizedAvatar(file);
    if (draft !== current || !dialog.open) return;
    draft.avatar = avatar;
    showProfileError("");
    updateAvatarPreview();
  } catch (error) {
    if (draft === current && dialog.open) {
      showProfileError(error.message.startsWith("Выбери") ? error.message : "Фото не открылось. Выбери другой файл.");
      byId("avatar-file").value = "";
    }
  } finally {
    byId("avatar-file").disabled = false;
    byId("remove-avatar").disabled = false;
    if (draft === current) byId("save-profile").disabled = false;
  }
});

byId("upload-chats").addEventListener("click", () => byId("chat-file").click());
byId("example-chats").addEventListener("click", () => {
  useSource("пример-чатов.txt", "@demo_design_chat\nhttps://t.me/demo_city_club\n@demo_photo_lab");
});
byId("chat-file").addEventListener("change", async event => {
  const file = event.target.files[0];
  event.target.value = "";
  if (!file) return;
  importPending = true;
  renderSettings();
  renderResults();
  try {
    if (!/\.(txt|csv)$/i.test(file.name) || file.size > 128 * 1024) {
      importNotice = "Выбери файл TXT или CSV до 128 КБ.";
    } else {
      useSource(file.name, await file.text());
    }
  } catch {
    importNotice = "Файл не удалось прочитать. Выбери его ещё раз.";
  } finally {
    importPending = false;
    renderImport();
    renderSettings();
    renderResults();
  }
});
byId("run-account").addEventListener("change", event => {
  state.selectedAccount = event.target.value;
  saveState();
});
byId("run-limit").addEventListener("change", event => {
  state.limit = Number(event.target.value);
  saveState();
});
byId("check-stories").addEventListener("click", checkStories);
byId("start-views").addEventListener("click", startViewing);
byId("stop-views").addEventListener("click", () => stopViewing());

document.querySelector(".skip-link").addEventListener("click", event => {
  event.preventDefault();
  byId("main").focus();
});
addEventListener("hashchange", () => {
  showPage();
  scrollTo(0, 0);
});

showPage();
renderAccounts();
renderImport();
renderSettings();
renderResults();
renderEvents();
