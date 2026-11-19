const accountApi = "/api/telegram";
const stateApi = "/api/internal/telegram-state";
const ownerId = /^[A-Za-z0-9_-]{1,160}$/;
const itemId = /^[A-Za-z0-9_-]{1,64}$/;
const routes = [
  ["GET", /^\/api\/telegram$/],
  ["POST", /^\/api\/telegram\/(?:proxies|login)$/],
  ["POST", /^\/api\/telegram\/login\/[a-z0-9_-]{1,64}\/(?:code|password)$/i],
  ["DELETE", /^\/api\/telegram\/(?:accounts|proxies|login)\/[a-z0-9_-]{1,64}$/i]
];
const maxBody = 16 * 1024;
const headersForPage = {
  "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; form-action 'self'",
  "Referrer-Policy": "no-referrer",
  "X-Content-Type-Options": "nosniff"
};

function backend(env) {
  const token = env.PANEL_BACKEND_TOKEN ?? "";
  if (!/^[A-Za-z0-9_-]{32,128}$/.test(token)) return null;
  try {
    const url = new URL(env.PANEL_BACKEND_ORIGIN);
    if (url.protocol !== "https:" || url.username || url.password || url.pathname !== "/" || url.search || url.hash) return null;
    return { origin: url.origin, token };
  } catch {
    return null;
  }
}

function problem(status, detail) {
  return Response.json({ detail }, { status, headers: { "Cache-Control": "no-store" } });
}

async function limitBody(body) {
  if (!body) return null;
  const reader = body.getReader();
  const buffer = new Uint8Array(maxBody);
  let size = 0;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) return buffer.slice(0, size);
      if (size + value.byteLength > maxBody) {
        await reader.cancel();
        return null;
      }
      buffer.set(value, size);
      size += value.byteLength;
    }
  } finally {
    reader.releaseLock();
  }
}

function validBearer(provided, token) {
  if (!/^[A-Za-z0-9_-]{32,128}$/.test(token ?? "")) return false;
  const expected = "Bearer " + token;
  if (!provided || provided.length !== expected.length) return false;
  let difference = 0;
  for (let index = 0; index < expected.length; index += 1) {
    difference |= expected.charCodeAt(index) ^ provided.charCodeAt(index);
  }
  return difference === 0;
}

function storageResponse(data) {
  return Response.json(data, { headers: { "Cache-Control": "no-store" } });
}

async function state(request, env) {
  if (request.method !== "POST" || !validBearer(request.headers.get("Authorization"), env.PANEL_STATE_TOKEN)) {
    return problem(401, "Требуется доступ к хранилищу.");
  }
  if (!env.BUCKET) return problem(503, "Хранилище временно недоступно.");
  if (request.headers.get("Content-Type")?.split(";")[0].trim() !== "application/json") {
    return problem(415, "Требуется JSON.");
  }
  let payload;
  try {
    const body = await limitBody(request.body);
    if (body === null) return problem(413, "Запись превышает допустимый размер.");
    payload = JSON.parse(new TextDecoder().decode(body));
  } catch {
    return problem(400, "Неверный формат записи.");
  }
  if (!payload || typeof payload.owner !== "string" || !ownerId.test(payload.owner) || !["account", "proxy"].includes(payload.kind)) {
    return problem(400, "Неверный адрес записи.");
  }
  const prefix = "telegram/v1/" + payload.owner + "/" + payload.kind + "/";
  try {
    if (payload.operation === "list") {
      const cursor = payload.cursor;
      if (cursor != null && (typeof cursor !== "string" || cursor.length > 2048)) {
        return problem(400, "Неверный указатель списка.");
      }
      const page = await env.BUCKET.list({ prefix, cursor: cursor || undefined, limit: 20 });
      const items = await Promise.all(page.objects.map(async entry => {
        const item = await env.BUCKET.get(entry.key);
        return item ? { id: entry.key.slice(prefix.length), value: await item.text() } : null;
      }));
      return storageResponse({ items: items.filter(Boolean), cursor: page.truncated ? page.cursor : null });
    }
    if (typeof payload.id !== "string" || !itemId.test(payload.id)) return problem(400, "Неверный адрес записи.");
    const key = prefix + payload.id;
    switch (payload.operation) {
      case "get": {
        const item = await env.BUCKET.get(key);
        return storageResponse({ value: item ? await item.text() : null });
      }
      case "save":
        if (typeof payload.value !== "string" || !/^[A-Za-z0-9_=-]{100,14000}$/.test(payload.value)) {
          return problem(400, "Неверный формат зашифрованной записи.");
        }
        await env.BUCKET.put(key, payload.value);
        return storageResponse({});
      case "remove":
        await env.BUCKET.delete(key);
        return storageResponse({});
      default:
        return problem(400, "Неверная операция хранилища.");
    }
  } catch {
    return problem(503, "Хранилище временно недоступно.");
  }
}

async function forward(request, url, connection) {
  const user = request.headers.get("oai-authenticated-user-id") ?? "";
  if (!ownerId.test(user)) {
    return problem(401, "Войди в панель через ChatGPT.");
  }
  if (!routes.some(([method, path]) => method === request.method && path.test(url.pathname)) || url.search) {
    return problem(404, "Этот адрес недоступен.");
  }
  if (request.method !== "GET" && request.headers.get("Origin") !== url.origin) {
    return problem(403, "Открой форму на странице панели.");
  }
  if (!connection) return problem(503, "Сервер Telegram пока недоступен.");

  const headers = {
    "Accept": "application/json",
    "Authorization": "Bearer " + connection.token,
    "X-Panel-User": user
  };
  let body;
  if (request.method === "POST") {
    if (request.headers.get("Content-Type")?.split(";")[0].trim() !== "application/json") {
      return problem(415, "Открой форму на странице панели.");
    }
    body = await limitBody(request.body);
    if (body === null) return problem(413, "Проверь длину полей формы.");
    headers["Content-Type"] = "application/json";
  }

  try {
    const upstream = await fetch(new URL(url.pathname, connection.origin), {
      method: request.method,
      headers,
      body,
      redirect: "error",
      signal: AbortSignal.timeout(34000)
    });
    const outgoing = { "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff" };
    const retryAfter = upstream.headers.get("Retry-After");
    if (retryAfter && /^\d+$/.test(retryAfter)) outgoing["Retry-After"] = retryAfter;
    if (upstream.status === 204) return new Response(null, { status: 204, headers: outgoing });
    if (upstream.headers.get("Content-Type")?.split(";")[0] !== "application/json") {
      return problem(502, "Сервер Telegram пока недоступен.");
    }
    outgoing["Content-Type"] = "application/json; charset=utf-8";
    return new Response(upstream.body, { status: upstream.status, headers: outgoing });
  } catch {
    return problem(502, "Связь с сервером Telegram прервалась.");
  }
}

export function createWorker(assets) {
  return {
    async fetch(request, env) {
      const url = new URL(request.url);
      if (url.pathname === stateApi) return state(request, env);
      const connection = backend(env);
      if (url.pathname === accountApi || url.pathname.startsWith(accountApi + "/")) {
        return forward(request, url, connection);
      }
      if (request.method !== "GET" && request.method !== "HEAD") return problem(404, "Страница не найдена.");
      const path = url.pathname === "/"
        ? (connection ? "/telegram.html" : "/index.html")
        : (url.pathname === "/demo" ? "/index.html" : url.pathname);
      if (!Object.hasOwn(assets, path)) return problem(404, "Страница не найдена.");
      const [type, body] = assets[path];
      return new Response(request.method === "HEAD" ? null : body, {
        headers: { ...headersForPage, "Content-Type": type }
      });
    }
  };
}
