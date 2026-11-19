const bridge = window.AstrBotPluginView || window.AstrBotPluginPage;

const state = {
  settings: {},
  instances: [],
  account: {},
  sessions: [],
  bindings: {},
  bindingPermissions: {},
  permissionDefinitions: [],
  defaultPermissions: {},
  apiError: '',
};

let selectedSessionUmo = '';
let sessionQuery = '';
let permissionTargetKey = '';
let permissionTargetSessionUmo = '';
let toastTimer = null;
let firstLoad = true;
let dragState = null;
let dropInsertIndex = null;

const $ = (id) => document.getElementById(id);

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function errorMessage(error) {
  if (!error) return "未知错误";
  if (typeof error === "string") return error;
  if (error.message) return error.message;
  if (error.detail) return typeof error.detail === "string" ? error.detail : JSON.stringify(error.detail);
  try {
    return JSON.stringify(error);
  } catch {
    return String(error);
  }
}

async function apiGet(endpoint, params = {}) {
  const result = await bridge.apiGet(endpoint, params);
  unwrapError(result);
  return result;
}

async function apiPost(endpoint, payload = {}) {
  const result = await bridge.apiPost(endpoint, payload);
  unwrapError(result);
  return result;
}

function unwrapError(result) {
  if (result && result.status === "error") {
    throw new Error(result.message || "请求失败");
  }
  if (result && result.ok === false && result.error) {
    throw new Error(result.error);
  }
}

function showToast(message, type = "ok") {
  const node = $("toast");
  node.textContent = message;
  node.className = `toast ${type === "error" ? "error" : ""}`;
  node.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => node.classList.add("hidden"), 3200);
}

function showNotice(message, error = false) {
  const node = $("notice");
  if (!message) {
    node.classList.add("hidden");
    node.textContent = "";
    return;
  }
  node.textContent = message;
  node.className = `notice ${error ? "error" : ""}`;
}

function setConnection(ok, text) {
  const pill = $("connectionPill");
  pill.className = `pill ${ok ? "ok" : "error"}`;
  pill.innerHTML = `<span class="pill-dot"></span>${escapeHtml(text)}`;
}

function setTheme(context) {
  const dark = Boolean(context?.isDark);
  document.documentElement.dataset.theme = dark ? "dark" : "light";
}

function sessionKeys(session) {
  return [...new Set([session?.umo, session?.session_id].filter(Boolean).map(String))];
}

function boundKeysForSession(session) {
  const keys = [];
  for (const sessionKey of sessionKeys(session)) {
    for (const instanceKey of state.bindings[sessionKey] || []) {
      if (!keys.includes(instanceKey)) keys.push(instanceKey);
    }
  }
  return keys;
}

function selectedSession() {
  return state.sessions.find((item) => item.umo === selectedSessionUmo) || null;
}

function instanceByKey(key) {
  return state.instances.find((item) => item.key === key) || null;
}

function statusClass(status) {
  return {
    "-1": "busy",
    0: "stopped",
    1: "stopping",
    2: "starting",
    3: "running",
  }[String(status)] || "stopped";
}

function permissionsForSession(session, instanceKey) {
  const defaults = { ...(state.defaultPermissions || {}) };
  if (!session) return defaults;
  for (const sessionKey of sessionKeys(session)) {
    const stored = state.bindingPermissions?.[sessionKey]?.[instanceKey];
    if (stored) return { ...defaults, ...stored };
  }
  return defaults;
}

function enabledPermissionLabels(permissions) {
  return state.permissionDefinitions
    .filter((item) => permissions?.[item.key])
    .map((item) => item.label);
}

function sortSessions() {
  state.sessions.sort((a, b) => {
    const aBound = boundKeysForSession(a).length > 0 ? 0 : 1;
    const bBound = boundKeysForSession(b).length > 0 ? 0 : 1;
    if (aBound !== bBound) return aBound - bBound;
    const aWhite = a.whitelisted ? 0 : 1;
    const bWhite = b.whitelisted ? 0 : 1;
    if (aWhite !== bWhite) return aWhite - bWhite;
    return String(a.display_name || a.umo).localeCompare(String(b.display_name || b.umo), "zh-CN");
  });
}

function renderAll() {
  sortSessions();
  renderInstances();
  renderSessionSelector();
  renderSessionDetails();
}

function clearDragVisuals() {
  for (const card of document.querySelectorAll(".instance-card")) {
    card.classList.remove("dragging", "drop-before", "drop-after");
  }
  $("boundDropZone")?.classList.remove("drag-over");
  $("instancePanel")?.classList.remove("unbind-target");
  dropInsertIndex = null;
}

function instanceCardMarkup(instance, location) {
  const st = statusClass(instance.status);
  const permissions = permissionsForSession(selectedSession(), instance.key);
  const labels = enabledPermissionLabels(permissions);
  const badges = labels.slice(0, 4).map((label) => `<span class="mini-badge">${escapeHtml(label)}</span>`).join("");
  return `
    <article class="instance-card" draggable="true" data-instance-key="${escapeHtml(instance.key)}" data-card-location="${location}">
      <div class="instance-avatar" aria-hidden="true">
        <svg viewBox="0 0 24 24" fill="none">
          <rect x="4" y="4" width="16" height="6" rx="2" stroke="currentColor" stroke-width="1.6"/>
          <rect x="4" y="14" width="16" height="6" rx="2" stroke="currentColor" stroke-width="1.6"/>
          <path d="M7.5 7h.01M7.5 17h.01" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"/>
        </svg>
        <span class="status-dot ${st}"></span>
      </div>
      <div class="instance-copy">
        <div class="instance-title-row">
          <span class="instance-title">${escapeHtml(instance.nickname)}</span>
          <span class="status-label ${st}">${escapeHtml(instance.status_text)}</span>
        </div>
        <div class="instance-sub">${escapeHtml(instance.instanceUuid)}</div>
        <div class="permission-badges">${badges || '<span class="mini-badge">无权限</span>'}</div>
      </div>
      <div class="instance-actions">
        <button class="card-settings-button" type="button" draggable="false" title="设置权限" aria-label="设置 ${escapeHtml(instance.nickname)} 的权限" data-permission-key="${escapeHtml(instance.key)}">
          <svg viewBox="0 0 24 24" fill="none">
            <path d="M12 8.2a3.8 3.8 0 1 0 0 7.6 3.8 3.8 0 0 0 0-7.6Z" stroke="currentColor" stroke-width="1.6"/>
            <path d="M19.2 13.6a7.9 7.9 0 0 0 .05-1.6 7.9 7.9 0 0 0-.05-1.6l2-1.6-2-3.4-2.4 1a8 8 0 0 0-2.8-1.7L13.7 2h-4l-.3 2.7A8 8 0 0 0 6.6 6.4l-2.4-1-2 3.4 2 1.6A7.9 7.9 0 0 0 4.1 12a7.9 7.9 0 0 0 .05 1.6l-2 1.6 2 3.4 2.4-1a8 8 0 0 0 2.8 1.7l.3 2.7h4l.3-2.7a8 8 0 0 0 2.8-1.7l2.4 1 2-3.4-2-1.6Z" stroke="currentColor" stroke-width="1.45" stroke-linejoin="round"/>
          </svg>
        </button>
      </div>
    </article>`;
}

function staleCardMarkup(key, location) {
  return `
    <article class="instance-card" draggable="true" data-instance-key="${escapeHtml(key)}" data-card-location="${location}">
      <div class="instance-avatar" aria-hidden="true">
        <svg viewBox="0 0 24 24" fill="none">
          <path d="M12 8v4m0 4h.01M4 20h16L12 4 4 20Z" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
        <span class="status-dot stopped"></span>
      </div>
      <div class="instance-copy">
        <div class="instance-title-row">
          <span class="instance-title">实例不可用</span>
          <span class="status-label stopped">已失效</span>
        </div>
        <div class="instance-sub">${escapeHtml(key)}</div>
        <div class="permission-badges"><span class="mini-badge">当前 API Key 无权访问</span></div>
      </div>
      <div class="instance-actions"></div>
    </article>`;
}

function bindInstanceCardEvents(container) {
  for (const card of container.querySelectorAll(".instance-card")) {
    card.addEventListener("dragstart", (event) => {
      if (event.target.closest("button")) {
        event.preventDefault();
        return;
      }
      dragState = {
        key: card.dataset.instanceKey,
        location: card.dataset.cardLocation,
        session: selectedSessionUmo,
      };
      card.classList.add("dragging");
      event.dataTransfer.effectAllowed = "move";
      event.dataTransfer.setData("text/plain", card.dataset.instanceKey);
      event.dataTransfer.setData("application/x-mcsm-instance", card.dataset.instanceKey);
    });
    card.addEventListener("dragend", () => {
      clearDragVisuals();
      dragState = null;
    });
  }
  for (const button of container.querySelectorAll("[data-permission-key]")) {
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      openPermissionModal(button.dataset.permissionKey);
    });
  }
}

function renderInstances() {
  const list = $("instanceList");
  if (!state.settings.configured) {
    $("instanceCount").textContent = "0";
    list.innerHTML = `
      <div class="empty-state">
        <div><strong>尚未连接 MCSManager</strong><p>请先到「设置」页填写 Base URL 与 API Key。</p></div>
      </div>`;
    return;
  }
  if (state.apiError) {
    $("instanceCount").textContent = "0";
    list.innerHTML = `
      <div class="empty-state">
        <div><strong>实例读取失败</strong><p>${escapeHtml(state.apiError)}</p></div>
      </div>`;
    return;
  }
  if (!state.instances.length) {
    $("instanceCount").textContent = "0";
    list.innerHTML = `
      <div class="empty-state">
        <div><strong>没有可操作实例</strong><p>当前 API Key 对应的 MCSManager 账号没有分配实例。</p></div>
      </div>`;
    return;
  }

  const session = selectedSession();
  const boundSet = new Set(session ? boundKeysForSession(session) : []);
  const unbound = state.instances.filter((instance) => !boundSet.has(instance.key));
  $("instanceCount").textContent = String(unbound.length);
  if (!unbound.length) {
    list.innerHTML = `
      <div class="empty-state">
        <div><strong>没有未授权实例</strong><p>当前 SID 已授权全部实例，或右侧实例需要重新授权。</p></div>
      </div>`;
    return;
  }
  list.innerHTML = unbound.map((instance) => instanceCardMarkup(instance, "left")).join("");
  bindInstanceCardEvents(list);
}

function renderSessionSelector() {
  const current = selectedSession();
  const triggerText = $("sessionTriggerText");
  if (current) {
    triggerText.classList.remove("placeholder");
    triggerText.textContent = current.display_name || current.session_id || current.umo;
  } else {
    triggerText.classList.add("placeholder");
    triggerText.textContent = state.sessions.length ? "请选择会话" : "未发现群会话";
  }

  const query = sessionQuery.trim().toLowerCase();
  const filtered = state.sessions.filter((item) => {
    const haystack = [item.display_name, item.umo, item.session_id, item.platform].join(" ").toLowerCase();
    return !query || haystack.includes(query);
  });
  const options = $("sessionOptions");
  if (!filtered.length) {
    options.innerHTML = `<div class="empty-state" style="min-height:100px;border:0"><div><strong>没有匹配会话</strong><p>给机器人发送 /sid 获取群 ID。</p></div></div>`;
    return;
  }
  options.innerHTML = filtered.map((session) => {
    const bindings = boundKeysForSession(session);
    const selected = session.umo === selectedSessionUmo;
    return `
      <div class="session-option ${selected ? "selected" : ""}" role="option" aria-selected="${selected}" data-session-umo="${encodeURIComponent(session.umo)}">
        <span class="session-lamp ${bindings.length ? "bound" : ""}" title="${bindings.length ? "已绑定实例" : "未绑定实例"}"></span>
        <div class="session-option-copy">
          <div class="session-option-name">${escapeHtml(session.display_name || session.umo)}</div>
          <div class="session-option-id">${escapeHtml(session.umo)}</div>
        </div>
        <div class="option-meta">
          ${session.whitelisted ? '<span class="whitelist-chip">白名单</span>' : ""}
          ${bindings.length ? `<span class="bound-num">${bindings.length} 个</span>` : ""}
        </div>
      </div>`;
  }).join("");
  for (const option of options.querySelectorAll("[data-session-umo]")) {
    option.addEventListener("click", () => {
      selectedSessionUmo = decodeURIComponent(option.dataset.sessionUmo);
      closeSessionDropdown();
      renderAll();
    });
  }
}

function renderSessionDetails() {
  const session = selectedSession();
  const meta = $("sessionMeta");
  const boundDropZone = $("boundDropZone");
  const boundList = $("boundList");
  if (!session) {
    meta.className = "session-meta empty";
    meta.textContent = state.sessions.length ? "尚未选择会话" : "没有可用于绑定的群会话，请先给机器人发送 /sid。";
    boundDropZone.classList.add("disabled");
    $("boundCount").textContent = "0";
    boundList.innerHTML = '<div class="empty-state"><div><strong>选择会话</strong><p>选择 SID 后可查看并排序已授权实例。</p></div></div>';
    return;
  }
  meta.className = "session-meta";
  meta.innerHTML = `<span><strong>${escapeHtml(session.display_name || session.session_id)}</strong> · <code>${escapeHtml(session.umo)}</code>${session.whitelisted ? " · 白名单会话" : ""}</span>`;
  boundDropZone.classList.remove("disabled");

  const bound = boundKeysForSession(session);
  $("boundCount").textContent = String(bound.length);
  if (!bound.length) {
    boundList.innerHTML = '<div class="empty-state"><div><strong>拖入实例</strong><p>从左侧拖动卡片到此处完成授权。</p></div></div>';
    return;
  }
  boundList.innerHTML = bound.map((key) => {
    const instance = instanceByKey(key);
    return instance ? instanceCardMarkup(instance, "right") : staleCardMarkup(key, "right");
  }).join("");
  bindInstanceCardEvents(boundList);
}

function insertionIndexFromEvent(event) {
  const cards = [...$("boundList").querySelectorAll(".instance-card")];
  for (const card of cards) {
    card.classList.remove("drop-before", "drop-after");
  }
  let index = cards.length;
  const targetCard = event.target.closest(".instance-card");
  if (targetCard && $("boundList").contains(targetCard)) {
    const targetIndex = cards.indexOf(targetCard);
    const rect = targetCard.getBoundingClientRect();
    const after = event.clientY > rect.top + rect.height / 2;
    index = targetIndex + (after ? 1 : 0);
    targetCard.classList.add(after ? "drop-after" : "drop-before");
  }
  return index;
}

function orderedWithoutKey(keys, key, insertionIndex) {
  const currentIndex = keys.indexOf(key);
  const without = keys.filter((item) => item !== key);
  let index = Number.isFinite(insertionIndex) ? insertionIndex : without.length;
  if (currentIndex >= 0 && currentIndex < insertionIndex) index -= 1;
  index = Math.max(0, Math.min(index, without.length));
  without.splice(index, 0, key);
  return without;
}

async function persistBindingOrder(session, keys) {
  const result = await apiPost("bindings/reorder", {
    session: session.umo,
    ordered_keys: keys,
  });
  state.bindings = result.bindings || state.bindings;
}

async function bindInstance(instanceKey, insertIndex = null) {
  const session = selectedSession();
  if (!session) {
    showToast("请先选择会话", "error");
    return;
  }
  try {
    const result = await apiPost("bindings/add", {
      session: session.umo,
      instance_key: instanceKey,
    });
    state.bindings = result.bindings || state.bindings;
    if (result.added && insertIndex !== null) {
      const ordered = orderedWithoutKey(boundKeysForSession(session), instanceKey, insertIndex);
      await persistBindingOrder(session, ordered);
    }
    const instance = instanceByKey(instanceKey);
    showToast(result.added ? `已授权「${instance?.nickname || instanceKey}」` : "该实例已授权");
    renderAll();
  } catch (error) {
    showToast(errorMessage(error), "error");
  }
}

async function reorderBinding(instanceKey, insertIndex) {
  const session = selectedSession();
  if (!session) return;
  try {
    const current = boundKeysForSession(session);
    const ordered = orderedWithoutKey(current, instanceKey, insertIndex);
    if (ordered.every((key, index) => key === current[index])) return;
    await persistBindingOrder(session, ordered);
    showToast("实例顺序已保存");
    renderAll();
  } catch (error) {
    showToast(errorMessage(error), "error");
  }
}

async function removeBinding(instanceKey) {
  const session = selectedSession();
  if (!session) return;
  try {
    const result = await apiPost("bindings/remove", {
      session: session.umo,
      instance_key: instanceKey,
    });
    state.bindings = result.bindings || state.bindings;
    showToast("已拖回左侧并解除授权");
    renderAll();
  } catch (error) {
    showToast(errorMessage(error), "error");
  }
}

function openPermissionModal(instanceKey) {
  const instance = instanceByKey(instanceKey);
  const session = selectedSession();
  if (!instance) return;
  if (!session) {
    showToast("请先选择 SID，再设置该 SID 的实例权限", "error");
    return;
  }
  permissionTargetKey = instanceKey;
  permissionTargetSessionUmo = session.umo;
  const permissions = permissionsForSession(session, instanceKey);
  $("permissionSubtitle").textContent = `${session.display_name || session.umo} · ${instance.nickname}`;
  $("permissionList").innerHTML = state.permissionDefinitions.map((item) => {
    const checked = Boolean(permissions[item.key]);
    return `
      <label class="permission-row ${item.key === "admin" ? "danger" : ""}">
        <span><strong>${escapeHtml(item.label)}</strong><p>${escapeHtml(item.description)}</p></span>
        <input type="checkbox" data-permission="${escapeHtml(item.key)}" ${checked ? "checked" : ""} />
      </label>`;
  }).join("");
  $("permissionModal").classList.remove("hidden");
}

function closePermissionModal() {
  $("permissionModal").classList.add("hidden");
  permissionTargetKey = "";
  permissionTargetSessionUmo = "";
}

async function savePermissions() {
  if (!permissionTargetKey || !permissionTargetSessionUmo) return;
  const session = state.sessions.find((item) => item.umo === permissionTargetSessionUmo);
  if (!session) {
    showToast("当前 SID 已失效，请重新选择", "error");
    return;
  }
  const permissions = {};
  for (const input of $("permissionList").querySelectorAll("[data-permission]")) {
    permissions[input.dataset.permission] = input.checked;
  }
  const button = $("savePermissionsBtn");
  button.disabled = true;
  try {
    const result = await apiPost("permissions/save", {
      session: session.umo,
      instance_key: permissionTargetKey,
      permissions,
    });
    if (!state.bindingPermissions[session.umo]) state.bindingPermissions[session.umo] = {};
    state.bindingPermissions[session.umo][permissionTargetKey] = result.permissions;
    showToast("当前 SID 的实例权限已保存");
    closePermissionModal();
    renderAll();
  } catch (error) {
    showToast(errorMessage(error), "error");
  } finally {
    button.disabled = false;
  }
}

function openSessionDropdown() {
  $("sessionDropdown").classList.remove("hidden");
  $("sessionTrigger").setAttribute("aria-expanded", "true");
  $("sessionSearch").value = sessionQuery;
  renderSessionSelector();
  setTimeout(() => $("sessionSearch").focus(), 0);
}

function closeSessionDropdown() {
  $("sessionDropdown").classList.add("hidden");
  $("sessionTrigger").setAttribute("aria-expanded", "false");
}

function populateSettings() {
  $("baseUrlInput").value = state.settings.base_url || "";
  $("apiKeyInput").value = "";
  $("apiKeyInput").placeholder = state.settings.has_api_key
    ? `已保存 ${state.settings.api_key_masked}，留空则保持不变`
    : "输入 MCSManager API Key";
  $("timeoutInput").value = String(state.settings.request_timeout || 20);
  $("verifySslInput").checked = state.settings.verify_ssl !== false;
}

async function testConnection() {
  const button = $("testBtn");
  const resultNode = $("testResult");
  button.classList.add("loading");
  button.disabled = true;
  resultNode.className = "field-hint test-result";
  resultNode.textContent = "正在测试…";
  try {
    const result = await apiPost("settings/test", {
      base_url: $("baseUrlInput").value.trim(),
      api_key: $("apiKeyInput").value.trim(),
      verify_ssl: $("verifySslInput").checked,
      request_timeout: Number($("timeoutInput").value || 20),
    });
    resultNode.className = "field-hint test-result ok";
    resultNode.textContent = `连接成功：${result.user_name || "MCSManager 用户"} · ${result.instance_count} 个可操作实例`;
  } catch (error) {
    resultNode.className = "field-hint test-result error";
    resultNode.textContent = `连接失败：${errorMessage(error)}`;
  } finally {
    button.classList.remove("loading");
    button.disabled = false;
  }
}

async function saveSettings() {
  const button = $("saveSettingsBtn");
  const status = $("settingsStatus");
  button.disabled = true;
  status.className = "settings-status";
  status.textContent = "正在保存…";
  try {
    await apiPost("settings/save", {
      base_url: $("baseUrlInput").value.trim(),
      api_key: $("apiKeyInput").value.trim(),
      verify_ssl: $("verifySslInput").checked,
      request_timeout: Number($("timeoutInput").value || 20),
    });
    status.className = "settings-status ok";
    status.textContent = "设置已保存";
    showToast("MCSManager 连接设置已保存");
    await loadData(false);
  } catch (error) {
    status.className = "settings-status error";
    status.textContent = errorMessage(error);
    showToast(errorMessage(error), "error");
  } finally {
    button.disabled = false;
  }
}

async function loadData(showSkeleton = true) {
  const refresh = $("refreshBtn");
  refresh.classList.add("spinning");
  if (showSkeleton) {
    $("instanceList").innerHTML = '<div class="skeleton"></div><div class="skeleton"></div><div class="skeleton"></div>';
  }
  try {
    const data = await apiGet("bootstrap");
    state.settings = data.settings || {};
    state.instances = data.instances || [];
    state.account = data.account || {};
    state.sessions = data.sessions || [];
    state.bindings = data.bindings || {};
    state.bindingPermissions = data.binding_permissions || {};
    state.permissionDefinitions = data.permission_definitions || [];
    state.defaultPermissions = data.default_permissions || {};
    state.apiError = data.api_error || "";

    if (selectedSessionUmo && !state.sessions.some((item) => item.umo === selectedSessionUmo)) {
      selectedSessionUmo = "";
    }
    if (!selectedSessionUmo && state.sessions.length) {
      const bound = state.sessions.find((item) => boundKeysForSession(item).length > 0);
      selectedSessionUmo = (bound || state.sessions[0]).umo;
    }

    populateSettings();
    renderAll();

    if (!state.settings.configured) {
      setConnection(false, "未配置");
      $("accountLine").textContent = "请先在设置页连接 MCSManager";
      showNotice("尚未配置 MCSManager Base URL 与 API Key。", false);
    } else if (state.apiError) {
      setConnection(false, "连接异常");
      $("accountLine").textContent = "MCSManager 连接失败";
      showNotice(`MCSManager 连接失败：${state.apiError}`, true);
    } else {
      setConnection(true, "已连接");
      const permission = state.account.permission;
      $("accountLine").textContent = `${state.account.userName || "MCSManager 用户"} · 权限 ${permission ?? "-"} · ${state.instances.length} 个实例`;
      showNotice("", false);
    }
    if (firstLoad && !state.settings.configured) {
      activateTab("settings");
    }
    firstLoad = false;
  } catch (error) {
    setConnection(false, "加载失败");
    $("accountLine").textContent = "插件初始化失败";
    showNotice(errorMessage(error), true);
    if (state.sessions.length) renderAll();
  } finally {
    refresh.classList.remove("spinning");
  }
}

function activateTab(tabName) {
  for (const tab of document.querySelectorAll(".tab")) {
    tab.classList.toggle("active", tab.dataset.tab === tabName);
  }
  $("blueprintPanel").classList.toggle("active", tabName === "blueprint");
  $("settingsPanel").classList.toggle("active", tabName === "settings");
}

function wireEvents() {
  for (const tab of document.querySelectorAll(".tab")) {
    tab.addEventListener("click", () => activateTab(tab.dataset.tab));
  }
  $("refreshBtn").addEventListener("click", () => loadData(true));
  $("testBtn").addEventListener("click", testConnection);
  $("saveSettingsBtn").addEventListener("click", saveSettings);
  $("toggleApiKey").addEventListener("click", () => {
    const input = $("apiKeyInput");
    input.type = input.type === "password" ? "text" : "password";
  });
  $("savePermissionsBtn").addEventListener("click", savePermissions);
  for (const closer of document.querySelectorAll("[data-close-modal]")) {
    closer.addEventListener("click", closePermissionModal);
  }
  $("sessionTrigger").addEventListener("click", (event) => {
    event.stopPropagation();
    if ($("sessionDropdown").classList.contains("hidden")) openSessionDropdown();
    else closeSessionDropdown();
  });
  $("sessionDropdown").addEventListener("click", (event) => event.stopPropagation());
  $("sessionSearch").addEventListener("input", (event) => {
    sessionQuery = event.target.value;
    renderSessionSelector();
  });
  document.addEventListener("click", closeSessionDropdown);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      closeSessionDropdown();
      closePermissionModal();
    }
  });
  const boundDropZone = $("boundDropZone");
  const instancePanel = $("instancePanel");

  boundDropZone.addEventListener("dragover", (event) => {
    if (!dragState || !selectedSession()) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "move";
    boundDropZone.classList.add("drag-over");
    dropInsertIndex = insertionIndexFromEvent(event);
  });
  boundDropZone.addEventListener("dragleave", (event) => {
    if (boundDropZone.contains(event.relatedTarget)) return;
    boundDropZone.classList.remove("drag-over");
    for (const card of $("boundList").querySelectorAll(".instance-card")) {
      card.classList.remove("drop-before", "drop-after");
    }
    dropInsertIndex = null;
  });
  boundDropZone.addEventListener("drop", async (event) => {
    event.preventDefault();
    const operation = dragState;
    const insertIndex = dropInsertIndex;
    boundDropZone.classList.remove("drag-over");
    if (!operation || !selectedSession()) return;
    clearDragVisuals();
    dragState = null;
    if (operation.location === "left") {
      await bindInstance(operation.key, insertIndex);
    } else if (operation.location === "right") {
      await reorderBinding(operation.key, insertIndex);
    }
  });

  instancePanel.addEventListener("dragover", (event) => {
    if (!dragState || dragState.location !== "right") return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "move";
    instancePanel.classList.add("unbind-target");
  });
  instancePanel.addEventListener("dragleave", (event) => {
    if (instancePanel.contains(event.relatedTarget)) return;
    instancePanel.classList.remove("unbind-target");
  });
  instancePanel.addEventListener("drop", async (event) => {
    if (!dragState || dragState.location !== "right") return;
    event.preventDefault();
    const key = dragState.key;
    clearDragVisuals();
    dragState = null;
    await removeBinding(key);
  });

}

async function main() {
  if (!bridge) {
    document.body.innerHTML = '<div style="padding:24px">AstrBot Plugin View bridge 未加载。</div>';
    return;
  }
  const context = await bridge.ready();
  setTheme(context);
  bridge.onContext(setTheme);
  wireEvents();
  await loadData(true);
}

main().catch((error) => {
  showNotice(`插件页面初始化失败：${errorMessage(error)}`, true);
});