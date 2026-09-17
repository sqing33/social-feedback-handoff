/* Mavis XHS Safe Bridge: fixed, XHS-only commands. */
const BRIDGE_URL = "ws://127.0.0.1:9333";
const SAFE_METHODS = new Set([
  "get_runtime_status",
  "get_xhs_page_state",
  "click_xhs_note_card",
]);
let socket = null;
let reconnectTimer = null;
let heartbeatTimer = null;

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type !== "GET_STATUS") return;
  (async () => {
    const result = {
      ok: true,
      bridge_connected: !!(socket && socket.readyState === WebSocket.OPEN),
      managed_tab_present: false,
      url_path: "/",
      page_state: null,
    };
    try {
      const tab = await managedXhsTab();
      result.managed_tab_present = true;
      result.url_path = new URL(tab.url || "https://xiaohongshu.com/").pathname || "/";
      const state = await chrome.scripting.executeScript({
        target: {tabId: tab.id},
        world: "MAIN",
        func: pageState,
        args: [""],
      });
      const value = state[0]?.result || {};
      result.page_state = {
        detail_ready: !!value.detail_ready,
        verification_wall: !!value.verification_wall,
        login_wall: !!value.login_wall,
        inaccessible: !!value.inaccessible,
      };
    } catch (_) {
      // The popup must remain useful when no scraper-owned XHS tab exists.
    }
    sendResponse(result);
  })();
  return true;
});

function token() {
  // Replaced with a local pairing token before Chrome loads this extension.
  // It is never exposed to page JavaScript or returned by a Bridge command.
  return "__XHS_BRIDGE_TOKEN__";
}

function isXhsUrl(url) {
  try {
    const parsed = new URL(url || "");
    return parsed.protocol === "https:" &&
      (parsed.hostname === "xiaohongshu.com" || parsed.hostname === "www.xiaohongshu.com");
  } catch (_) {
    return false;
  }
}

async function managedXhsTab() {
  const tabs = await chrome.tabs.query({
    url: ["https://xiaohongshu.com/*", "https://www.xiaohongshu.com/*"],
  });
  for (const tab of tabs) {
    if (!tab.id || !isXhsUrl(tab.url)) continue;
    try {
      const result = await chrome.scripting.executeScript({
        target: {tabId: tab.id},
        world: "MAIN",
        func: () => window.name || "",
      });
      if (result[0]?.result === "mavis-scraper-xhs") return tab;
    } catch (_) {}
  }
  throw new Error("managed XHS tab not found");
}

function pageState(noteId = "") {
  const text = String(document.body?.innerText || "").slice(0, 20000);
  const path = location.pathname || "/";
  const target = String(noteId || "");
  const rawMap = window.__INITIAL_STATE__?.note?.noteDetailMap;
  const map = rawMap?.value !== undefined
    ? rawMap.value
    : rawMap?._value !== undefined
      ? rawMap._value
      : rawMap;
  const stateReady = !!(map && typeof map === "object" && Object.entries(map).some(([key, value]) => {
    const candidate = value?.note || value?.value?.note || {};
    return !target || key.includes(target) || String(candidate.noteId || candidate.id || "") === target;
  }));
  const domReady = !!document.querySelector(
    "#detail-title, #detail-desc, .note-content .title, .note-content .desc, .note-slider-img img, video",
  );
  return {
    path,
    target_match: !target || path.includes(`/explore/${target}`) || path.includes(`/discovery/item/${target}`),
    detail_ready: stateReady || domReady,
    login_wall: /\/login(?:\/|$)/.test(path) ||
      (!!document.querySelector('.login-container, [class*="login-modal"]') && /登录|扫码/.test(text)),
    verification_wall: /扫码查看|打开小红书App扫码|请使用小红书App扫码|安全验证|访问频繁/.test(text),
    inaccessible: /当前笔记暂时无法浏览|内容不存在|笔记不存在|该笔记已被删除|私密笔记|仅作者可见|因用户设置，你无法查看|因违规无法查看/.test(text),
  };
}

function clickNoteCard(noteId) {
  const target = String(noteId || "");
  const anchors = Array.from(document.querySelectorAll(
    'a[href*="/explore/"], a[href*="/discovery/item/"]',
  ));
  const anchor = anchors.find(item => {
    try {
      const path = new URL(item.href || item.getAttribute("href") || "", location.href).pathname;
      return path.includes(`/explore/${target}`) || path.includes(`/discovery/item/${target}`);
    } catch (_) {
      return false;
    }
  });
  if (!anchor) return false;
  anchor.removeAttribute("target");
  anchor.scrollIntoView({block: "center", inline: "center"});
  anchor.click();
  return true;
}

async function run(method, params) {
  const tab = await managedXhsTab();
  if (method === "get_runtime_status") {
    return {tab_present: true, url_path: new URL(tab.url).pathname};
  }
  if (method === "get_xhs_page_state") {
    const result = await chrome.scripting.executeScript({
      target: {tabId: tab.id},
      world: "MAIN",
      func: pageState,
      args: [String(params.note_id || "")],
    });
    return result[0]?.result || {};
  }
  if (method === "click_xhs_note_card") {
    const result = await chrome.scripting.executeScript({
      target: {tabId: tab.id},
      world: "MAIN",
      func: clickNoteCard,
      args: [String(params.note_id || "")],
    });
    return !!result[0]?.result;
  }
  throw new Error("method not allowed");
}

function stopHeartbeat() {
  if (heartbeatTimer) clearInterval(heartbeatTimer);
  heartbeatTimer = null;
}

function startHeartbeat(current) {
  stopHeartbeat();
  heartbeatTimer = setInterval(() => {
    if (socket === current && current.readyState === WebSocket.OPEN) {
      current.send(JSON.stringify({type: "heartbeat"}));
    }
  }, 15000);
}

function scheduleReconnect() {
  if (reconnectTimer) return;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connect();
  }, 3000);
}

function connect() {
  if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) return;
  const current = new WebSocket(BRIDGE_URL);
  socket = current;
  current.onopen = () => {
    current.send(JSON.stringify({
      role: "extension",
      auth: token(),
      capabilities: ["public_page_state", "fixed_note_card_click"],
    }));
    startHeartbeat(current);
  };
  current.onmessage = async event => {
    let message;
    try {
      message = JSON.parse(event.data);
    } catch (_) {
      return;
    }
    if (message.type === "ready" || message.type === "heartbeat_ack") return;
    if (!SAFE_METHODS.has(message.method)) {
      current.send(JSON.stringify({
        id: message.id,
        error: {code: "BRIDGE_METHOD_NOT_ALLOWED", message: "Bridge method is not allowed"},
      }));
      return;
    }
    try {
      const result = await run(message.method, message.params || {});
      current.send(JSON.stringify({id: message.id, result}));
    } catch (_) {
      current.send(JSON.stringify({
        id: message.id,
        error: {code: "XHS_PAGE_OPERATION_FAILED", message: "controlled XHS page operation failed"},
      }));
    }
  };
  current.onclose = () => {
    if (socket === current) {
      socket = null;
      stopHeartbeat();
      scheduleReconnect();
    }
  };
  current.onerror = () => {};
}

chrome.runtime.onStartup.addListener(connect);
chrome.runtime.onInstalled.addListener(connect);
connect();
