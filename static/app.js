"use strict";

/* ---------- 状态 ---------- */
const S = {
  courseTypes: [],
  auth: { logged_in: false, user: "", password_held: false },
  settings: null,
  batch: { state: "unavailable", message: "", active: null, choices: [], id: "", name: "", source: "unknown", validated_ts: "", mode: "auto", manual_id: "" },
  queue: [],          // 加入目标队列的教学班（规范化对象，含 class_type/jxbid/name/kxh）
  selected: [],       // 已选课程（用于改选）
  pairs: [],          // 改选对 { old, target }
  task: null,
  active: false,
  lastSeq: 0,
  eventsRendered: 0,
  lastPillAuth: "",
  busy: false,
};
let _authRefreshing = false;   // 防止 api() 在 auth_expired 时无限递归刷新 bootstrap

const MODE_NAMES = { grab: "抢课", poll: "仅监控", swap: "安全改选" };
const STATUS_ZH = {
  pending: "排队中", running: "运行中", waiting_login: "等待重新登录",
  stopping: "正在停止", succeeded: "已完成", stopped: "已停止",
  failed: "已失败", manual_attention: "需要人工处理",
};
const SOURCE_ZH = {
  account_elective: "账号普通批次", account_experimental: "账号实验批次",
  student_info: "账号批次", elective_user: "账号上下文", landing: "页面解析",
  login_location: "登录捕获", html: "页面解析", last_success: "上次成功",
  legacy: "兼容候选", manual: "手动指定", default: "当前默认", unknown: "未设置",
};
const TERMINAL_STATUS = new Set(["succeeded", "stopped", "failed", "manual_attention"]);
const SUBJECT_ZH = {
  slot: "余量", full: "满员", try_add: "尝试选课", add_ok: "选课成功", add_fail: "选课失败",
  reconcile: "对账", drop_ok: "退课成功", drop_unknown: "退课待确认",
  swap_found: "发现余量", swap_ok: "改选成功", swap_precheck: "预检", rollback: "回退",
  rollback_ok: "回退成功", rollback_unknown: "回退待确认", manual_attention: "需人工处理",
  all_done: "全部完成", relogin: "重新登录", auth_wait: "等待登录", stop_request: "停止",
  stopped: "已停止", error: "错误", rearm: "重新武装",
};

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[c]));

/* ---------- API ---------- */
async function api(path, opts = {}) {
  const init = { method: opts.method || "GET", headers: {} };
  if (opts.body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(opts.body);
  }
  const r = await fetch(path, init);
  let j = null;
  try { j = await r.json(); } catch (e) { /* ignore */ }
  if (j && j.ok === true) return j.data;
  const info = (j && j.error) || {};
  const msg = info.message || `请求失败（HTTP ${r.status}）`;
  const er = new Error(msg);
  er.code = info.code || "http_error";
  er.retryable = !!info.retryable;
  er.httpStatus = r.status;
  // 会话被顶下线/过期时（auth_expired），顺带刷新 bootstrap，让批次条变红并
  // 弹出“会话已过期/被顶下线 → 重新登录”的入口，而不是停留在绿色 ready。
  if (er.code === "auth_expired" && path !== "/api/bootstrap" && path !== "/api/auth/login"
      && !_authRefreshing) {
    _authRefreshing = true;
    refreshBootstrap().catch(() => resetAccountState())
      .finally(() => { _authRefreshing = false; });
  }
  throw er;
}

function toast(msg, kind = "") {
  const t = $("toast");
  t.textContent = msg;
  t.className = "toast show" + (kind ? " " + kind : "");
  clearTimeout(toast._h);
  toast._h = setTimeout(() => { t.className = "toast"; }, 3200);
}

function setStateLine(id, text, kind) {
  const el = $(id);
  el.textContent = text;
  el.className = "state-line" + (kind ? " " + kind : "");
}

/* ---------- 顶层渲染 ---------- */
function renderAll() {
  renderAuth();
  renderBatchPill();
  renderQueue();
  renderModePanel();
  renderTask();
  updateReloginBar();
}

/* 重新登录条：任务“等待重新登录”时，或会话被顶下线/过期（空闲无任务）时显示。 */
let _reloginBarShown = false;   // 只在重新登录条“变为可见”那一刻聚焦，避免每次轮询抢焦点
function updateReloginBar() {
  const bar = $("reloginBar");
  if (!bar) return;
  const t = S.task;
  const waiting = !!(t && t.status === "waiting_login");
  const expired = S.auth.logged_in && !!S.batch && S.batch.state === "auth_expired";
  const show = waiting || (expired && !S.active);
  bar.classList.toggle("hidden", !show);
  if (show && !_reloginBarShown) $("inpReloginPwd").focus();
  _reloginBarShown = show;
}

function renderAuth() {
  const btn = $("btnLogout");
  if (S.auth.logged_in) {
    btn.hidden = false;
    $("btnSelected").hidden = false;
    $("pillAuth").textContent = "认证 · " + (S.auth.user || "已登录");
    $("pillAuth").dataset.state = "ok";
    $("panelLogin").querySelectorAll("input").forEach(i => { i.disabled = true; });
    $("btnLogin").disabled = true;
  } else {
    btn.hidden = true;
    $("btnSelected").hidden = true;
    $("pillAuth").textContent = "认证 · 未登录";
    $("pillAuth").dataset.state = "idle";
    $("panelLogin").querySelectorAll("input").forEach(i => { i.disabled = false; });
    $("btnLogin").disabled = false;
  }
  if ($("inpPassword").value && !S.auth.logged_in) { $("inpPassword").value = ""; }
}

function activeBatch() {
  if (S.batch.active) return S.batch.active;
  if (!S.batch.id) return null;
  return { id: S.batch.id, name: S.batch.name || "", display_name: S.batch.display_name || "", source: S.batch.source, validated_ts: S.batch.validated_ts };
}

function renderBatchPill() {
  const p = $("pillBatch");
  const b = activeBatch();
  const state = S.batch.state || (b ? "ready" : "unavailable");
  if (b && state === "ready") {
    const label = b.name || b.display_name || `批次 …${String(b.id).slice(-8)}`;
    p.textContent = `批次 · ${label}`;
    p.dataset.state = state === "ready" ? "ok" : "warn";
    const srcZh = SOURCE_ZH[b.source] || b.source || "未知来源";
    const when = b.validated_ts || S.batch.validated_ts || "";
    $("batchState").textContent = `当前：${label} · ID …${String(b.id).slice(-8)} · ${srcZh}${when ? " · " + when + " 验证" : ""}`;
    $("batchState").className = "state-line ok";
  } else {
    const labels = {
      selection_required: "发现多个有效批次，请选择后采用",
      not_open: "统一认证已成功，但课程服务尚未开放",
      auth_expired: "会话已过期，请重新登录",
      unavailable: "暂时没有可用批次",
    };
    p.textContent = state === "selection_required" ? "批次 · 待选择"
      : state === "not_open" ? "批次 · 尚未开放"
      : state === "auth_expired" ? "批次 · 会话已过期" : "批次 · 未就绪";
    p.dataset.state = state === "auth_expired" || state === "unavailable" ? "err" : "warn";
    $("batchState").textContent = S.batch.message || labels[state] || "尚未取得有效批次";
    $("batchState").className = "state-line" + (state === "unavailable" || state === "auth_expired" ? " error" : "");
  }
  renderBatchChoices();
}

function choiceLabel(c) {
  const name = c.name || c.display_name || `批次 …${String(c.id || "").slice(-8)}（名称未获取）`;
  const category = c.category === "experimental" ? "实验" : (c.category === "elective" ? "普通" : "");
  return `${name}${category ? " · " + category : ""} · …${String(c.id || "").slice(-8)}`;
}

function renderBatchChoices() {
  const sel = $("selBatchChoice");
  if (!sel) return;
  const previous = sel.value;
  const choices = S.batch.choices || [];
  sel.innerHTML = "";
  if (!choices.length) {
    const o = document.createElement("option");
    o.value = ""; o.textContent = "尚未发现可选批次";
    sel.appendChild(o);
  } else {
    choices.forEach((c) => {
      const o = document.createElement("option");
      o.value = c.id; o.textContent = choiceLabel(c);
      o.disabled = c.can_select === false || (c.need_confirm && !c.is_confirmed) || c.status === "invalid";
      sel.appendChild(o);
    });
    const active = activeBatch();
    const wanted = choices.some(c => c.id === previous) ? previous : (active && active.id);
    if (wanted && choices.some(c => c.id === wanted)) sel.value = wanted;
  }
  renderBatchChoiceDetail();
}

function renderBatchChoiceDetail() {
  const box = $("batchChoiceDetail");
  if (!box) return;
  const c = (S.batch.choices || []).find(x => x.id === $("selBatchChoice").value);
  if (!c) { box.textContent = "重新发现后会在这里显示账号可用批次及其状态。"; return; }
  const parts = [];
  if (c.begin_time || c.end_time) parts.push(`时间：${c.begin_time || "?"} — ${c.end_time || "?"}`);
  parts.push(`ID：${c.id}`);
  if (c.status_message || c.message) parts.push(c.status_message || c.message);
  if (c.no_select_reason) parts.push(`不可选：${c.no_select_reason}`);
  if ((c.need_confirm && !c.is_confirmed)) parts.push("该批次需要先在官方选课页面确认通知。本站不会代为确认。");
  box.textContent = parts.join("\n");
}

/* ---------- 登录 ---------- */
async function doLogin(e) {
  e.preventDefault();
  if (S.busy) return;
  S.busy = true;
  const btn = $("btnLogin");
  btn.disabled = true; btn.textContent = "登录中…";
  try {
    const data = await api("/api/auth/login", { method: "POST", body: {
      user_id: $("inpUserId").value.trim(), password: $("inpPassword").value,
    }});
    applyBootstrap(data);
    $("inpPassword").value = "";
    const attached = !!data.attached;
    const state = S.batch.state || (activeBatch() ? "ready" : "unavailable");
    const b = activeBatch();
    let message = attached ? "已连接该账号现有的本站会话。" : "统一认证登录成功。";
    if (state === "ready" && b) message += ` 当前批次：${b.name || b.display_name || "…" + String(b.id).slice(-8)}。`;
    else if (state === "selection_required") message += ` 发现 ${(S.batch.choices || []).length} 个批次，请选择。`;
    else if (state === "not_open") message += " 课程服务尚未开放，已保留登录状态。";
    else message += " 暂未取得可用批次。";
    setStateLine("loginState", message, state === "unavailable" ? "" : "ok");
    toast(attached ? "已连接现有账号会话" : "登录成功", "ok");
  } catch (err) {
    setStateLine("loginState", err.message, "error");
    toast(err.message, "error");
  } finally {
    S.busy = false;
    btn.disabled = !!S.auth.logged_in; btn.textContent = "登录并发现批次";
  }
}

async function doLogout() {
  try {
    await api("/api/auth/logout", { method: "POST", body: {} });
    await refreshBootstrap();
    toast("已退出登录", "ok");
  } catch (err) { toast(err.message, "error"); }
}

async function doResume() {
  const pwd = $("inpReloginPwd").value;
  if (!pwd) { toast("请输入密码", "error"); return; }
  try {
    await api("/api/auth/relogin", { method: "POST", body: { password: pwd } });
    $("inpReloginPwd").value = "";
    await refreshBootstrap();
    toast(S.active ? "已恢复任务" : "已恢复会话", "ok");
  } catch (err) { toast(err.message, "error"); }
}

/* ---------- 批次 ---------- */
async function changeBatchMode(value) {
  const prev = S.settings.batch_mode;
  try {
    const s = await api("/api/settings", { method: "PUT", body: { batch_mode: value } });
    S.settings = s;
    toast(value === "auto" ? "已切换为自动发现" : "已切换为手动指定");
    if (value === "manual") $("inpManualBatch").focus();
    await refreshBootstrap();
  } catch (err) {
    S.settings.batch_mode = prev;
    syncBatchControls();   // 恢复被拒绝切换的选项显示
    toast(err.message, "error");
  }
}

async function doDiscover() {
  $("batchState").textContent = "正在尝试自动发现…";
  try {
    const data = await api("/api/batch/discover", { method: "POST", body: {} });
    if (data && data.batch) S.batch = data.batch;
    await refreshBootstrap();
    if (S.batch.state === "ready") toast("批次发现并验证成功", "ok");
    else if (S.batch.state === "selection_required") toast("发现多个有效批次，请选择");
    else setStateLine("batchState", S.batch.message || "未发现有效批次", S.batch.state === "not_open" ? "" : "error");
  } catch (err) {
    setStateLine("batchState", err.message, "error");
  }
}

async function doActivateBatch() {
  const id = $("selBatchChoice").value;
  if (!id) { toast("请选择可用批次", "error"); return; }
  setStateLine("batchState", "正在重新验证并采用所选批次…");
  try {
    await api("/api/batch/activate", { method: "POST", body: { batch_id: id } });
    await refreshBootstrap();
    const b = activeBatch();
    toast(`已采用 ${b ? (b.name || b.display_name || "所选批次") : "所选批次"}`, "ok");
  } catch (err) {
    setStateLine("batchState", err.message, "error");
  }
}

async function doValidateManual() {
  const id = $("inpManualBatch").value.trim();
  if (!id) { toast("请先输入批次 ID", "error"); return; }
  $("batchState").textContent = "正在验证手动批次…";
  try {
    const data = await api("/api/batch/validate", { method: "POST", body: { batch_id: id } });
    setStateLine("batchState", "手动批次有效，已采用： " + id, "ok");
    await refreshBootstrap();
  } catch (err) {
    setStateLine("batchState", err.message, "error");
  }
}

/* ---------- 搜索 ---------- */
async function doSearch(e) {
  e.preventDefault();
  const name = $("inpSearchName").value.trim();
  const typeId = parseInt($("selType").value, 10);
  if (Number.isNaN(typeId)) { toast("请选择课程类型", "error"); return; }
  setStateLine("searchState", name ? "搜索中…" : "正在加载该类型全部课程…");
  try {
    const data = await api("/api/courses/search", { method: "POST", body: { name, class_type_id: typeId } });
    $("resultWrap").classList.remove("hidden");
    renderResults(data.sections || [], data.count || 0);
    const shown = (data.sections || []).length;
    let msg;
    if (!data.count) msg = "未找到匹配的课程（可留空列出该类型全部）。";
    else if (data.truncated) msg = `匹配 ${data.count} 个教学班，仅显示前 ${shown} 个。`;
    else msg = `共 ${data.count} 个教学班，可多选加入目标。`;
    setStateLine("searchState", msg, data.count ? "ok" : "");
    if (!shown) $("resultWrap").classList.add("hidden");
  } catch (err) {
    $("resultWrap").classList.add("hidden");
    setStateLine("searchState", err.message, "error");
  }
}

function renderResults(sections, total) {
  const tb = $("resultTable").querySelector("tbody");
  tb.innerHTML = "";
  if (!sections || !sections.length) { $("resultCount").textContent = "无结果"; return; }
  $("resultCount").textContent = total == null || total === sections.length
    ? `共 ${sections.length} 个教学班，可多选加入目标。`
    : `匹配 ${total} 个，当前显示 ${sections.length} 个，可多选加入目标。`;
  sections.forEach((s) => {
    const tr = document.createElement("tr");
    const mark = s.has_slot ? `<span class="good">${s.selected}/${s.capacity} 有空</span>`
      : `<span class="bad">${s.selected}/${s.capacity} 已满</span>`;
    tr.innerHTML = `
      <td class="ck"><input type="checkbox" class="rowsel" value="${esc(s.jxbid)}"></td>
      <td><div><strong>${esc(s.name)}</strong> <span class="mono">${esc(s.code)}</span></div>
          <div class="hint">课序号 ${esc(s.kxh)} · ${esc(s.type_name)}</div></td>
      <td>${esc(s.teacher || "—")}</td>
      <td>${esc(s.weeks || "—")}<br>${esc(s.schedule || "")}</td>
      <td>${esc(s.place || "—")}</td>
      <td class="num">${mark}</td>`;
    tr.querySelector(".rowsel").dataset.key = s.class_type + ":" + s.jxbid;
    tr.querySelector(".rowsel").dataset.sec = JSON.stringify({
      class_type: s.class_type, jxbid: s.jxbid, name: s.name, kxh: s.kxh,
      teacher: s.teacher, place: s.place, weeks: s.weeks, schedule: s.schedule,
    });
    tb.appendChild(tr);
  });
}

function addSelectedToQueue() {
  const boxes = [...document.querySelectorAll("#resultTable .rowsel:checked")];
  if (!boxes.length) { toast("请先勾选教学班", "error"); return; }
  let added = 0;
  boxes.forEach((b) => {
    const sec = JSON.parse(b.dataset.sec);
    const key = b.dataset.key;
    if (!S.queue.some((q) => (q.class_type + ":" + q.jxbid) === key)) {
      S.queue.push(sec); added++;
    }
  });
  renderQueue();
  if (S.mode === "swap") renderPairsBuilt();
  toast(added ? `已加入 ${added} 个教学班（重复项已忽略）。` : "没有新增（可能已存在）。", "ok");
}

function renderQueue() {
  const list = $("queueList");
  $("queueCount").textContent = S.queue.length;
  $("queueEmpty").classList.toggle("hidden", S.queue.length > 0);
  list.innerHTML = "";
  S.queue.forEach((q, i) => {
    const li = document.createElement("li");
    li.innerHTML = `<span>${esc(q.name)} <span class="mono">${esc(q.kxh || "")}</span></span>
      <button type="button" aria-label="移除" data-i="${i}">×</button>`;
    list.appendChild(li);
  });
  syncLocked();
}

function removeQueue(i) {
  S.queue.splice(i, 1);
  renderQueue();
  if (S.mode === "swap") renderPairsBuilt();
}

/* ---------- 模式与改选 ---------- */
function currentMode() {
  const r = document.querySelector('input[name="mode"]:checked');
  return r ? r.value : "grab";
}

function renderModePanel() {
  const swap = currentMode() === "swap";
  $("swapPanel").classList.toggle("hidden", !swap);
  $("btnStart").textContent = swap ? "启动安全改选" : (currentMode() === "grab" ? "启动抢课" : "启动仅监控");
  if (swap) renderPairsBuilt();
}

async function loadSelected() {
  try {
    const data = await api("/api/courses/selected");
    S.selected = data.sections || [];
    renderSelectedTable();
    setStateLine("selectedState", `已加载 ${S.selected.length} 门已选课程。`, "ok");
  } catch (err) {
    setStateLine("selectedState", err.message, "error");
  }
}

function renderSelectedTable() {
  const tb = $("selectedTable").querySelector("tbody");
  tb.innerHTML = "";
  S.selected.forEach((o, i) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td class="ck"><input type="checkbox" class="oldsel" data-i="${i}"></td>
      <td><strong>${esc(o.name)}</strong> <span class="mono">${esc(o.kxh || "")}</span>
          <div class="hint mono">${esc(o.jxbid)}</div></td>
      <td>${esc(o.class_type || "未知类型")}</td>`;
    tb.appendChild(tr);
  });
}

function buildPairs() {
  const chosenOld = [...document.querySelectorAll("#selectedTable .oldsel:checked")].map(b => parseInt(b.dataset.i, 10));
  if (chosenOld.length === 0) { toast("请先勾选要退掉的已选课程", "error"); return; }
  if (S.queue.length === 0) { toast("目标队列为空，请先加入目标课程", "error"); return; }
  if (chosenOld.length !== S.queue.length) {
    toast(`勾选的旧课程(${chosenOld.length}) 数量需与目标队列(${S.queue.length})一致，按顺序一一对应。`, "error");
    return;
  }
  S.pairs = S.queue.map((tg, i) => ({
    old: { jxbid: S.selected[chosenOld[i]].jxbid, name: S.selected[chosenOld[i]].name, kxh: S.selected[chosenOld[i]].kxh, class_type: S.selected[chosenOld[i]].class_type },
    target: tg,
  }));
  renderPairsBuilt();
  toast("已建立 " + S.pairs.length + " 组改选对。", "ok");
}

function renderPairsBuilt() {
  const wrap = $("pairWrap");
  const tb = $("pairTable").querySelector("tbody");
  tb.innerHTML = "";
  if (S.pairs.length) {
    wrap.classList.remove("hidden");
    S.pairs.forEach((p) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `<td>${esc(p.old.name)}(${esc(p.old.kxh)}) → <strong>${esc(p.target.name)}</strong>(${esc(p.target.kxh)})</td>
        <td>待启动</td>`;
      tb.appendChild(tr);
    });
  } else {
    wrap.classList.add("hidden");
  }
}

/* ---------- 任务 ---------- */
function taskPayload() {
  const mode = currentMode();
  if (mode === "swap") {
    if (!S.pairs.length) throw new Error("尚未建立改选对。");
    return { mode, pairs: S.pairs.map((p) => ({
      old: { jxbid: p.old.jxbid, name: p.old.name, kxh: p.old.kxh, class_type: p.old.class_type },
      target: { class_type: p.target.class_type, jxbid: p.target.jxbid, name: p.target.name, kxh: p.target.kxh },
    })) };
  }
  if (!S.queue.length) throw new Error("目标队列为空，请先加入目标课程。");
  return { mode, targets: S.queue.map((q) => ({
    class_type: q.class_type, jxbid: q.jxbid, name: q.name, kxh: q.kxh,
  })) };
}

async function startTask() {
  if (S.busy) return;
  let payload;
  try { payload = taskPayload(); }
  catch (err) { toast(err.message, "error"); return; }
  S.busy = true;
  try {
    const data = await api("/api/tasks", { method: "POST", body: payload });
    S.task = data;
    toast("任务已启动", "ok");
    await pollOnce();
  } catch (err) {
    toast(err.message, "error");
    setStateLine("taskState", err.message, "error");
  } finally { S.busy = false; }
}

async function stopTask() {
  try {
    const data = await api("/api/tasks/stop", { method: "POST", body: {} });
    S.task = data.task || S.task;
    toast("已请求停止，正在安全收尾…");
  } catch (err) { toast(err.message, "error"); }
}

function renderTask() {
  const t = S.task;
  const meta = $("taskMeta");
  const stop = $("btnStop");
  const reloginBar = $("reloginBar");
  if (!t) {
    S.active = false;
    meta.textContent = "尚无任务";
    $("taskStatesWrap").classList.add("hidden");
    stop.hidden = true;
    updateReloginBar();
    setStateLine("taskState", "未启动任务。先在左侧登录并获得批次，然后搜索加入目标。", "");
    $("pillTask").textContent = "任务 · 空闲";
    $("pillTask").dataset.state = "idle";
    syncLocked();
    return;
  }
  S.active = !TERMINAL_STATUS.has(t.status);
  $("pillTask").textContent = `任务 · ${MODE_NAMES[t.mode] || t.mode} · ${STATUS_ZH[t.status] || t.status}`;
  $("pillTask").dataset.state = t.status === "manual_attention" ? "err"
    : (t.status === "running" || t.status === "waiting_login") ? "warn"
    : (t.status === "succeeded") ? "ok" : "idle";
  stop.hidden = !S.active;
  stop.textContent = t.stop_requested ? "正在安全停止…" : "停止任务";
  updateReloginBar();
  const bits = [`#${t.id} · ${MODE_NAMES[t.mode] || t.mode} · ${STATUS_ZH[t.status] || t.status}`];
  if (t.started_ts) bits.push("开始 " + t.started_ts);
  if (t.stage) bits.push("当前：" + t.stage);
  if (t.summary) bits.push(t.summary);
  if (t.error) bits.push("错误：" + t.error);
  meta.textContent = bits.join("  |  ");

  const states = t.states || {};
  const keys = Object.keys(states);
  const wrap = $("taskStatesWrap");
  wrap.classList.toggle("hidden", keys.length === 0);
  if (keys.length) {
    const tb = $("taskStatesTable").querySelector("tbody");
    tb.innerHTML = "";
    keys.forEach((k) => {
      const st = states[k];
      const tr = document.createElement("tr");
      const num = (st.capacity === null || st.capacity === undefined)
        ? "—" : `${st.selected ?? "?"}/${st.capacity}`;
      const detail = esc(st.detail || st.status || "");
      const cls = st.status === "done" ? "good" : (st.status === "manual" ? "bad" : "");
      tr.innerHTML = `<td>${esc(st.label || k)}<div class="hint">${detail}</div></td>
        <td class="num">${num}</td>
        <td>${esc(st.status)}</td><td class="num">${esc(st.last_check || "—")}</td>`;
      if (cls) tr.classList.add(cls);
      tb.appendChild(tr);
    });
  }
  syncLocked();
}

/* 任务运行期间锁定“准备下一轮任务”的控件，避免误解（运行中的任务用的是启动时的快照）。 */
function syncLocked() {
  const act = !!S.active;
  document.querySelectorAll('input[name="mode"]').forEach(r => { r.disabled = act; });
  ["btnStart", "btnAddSelected", "btnClearQueue", "btnLoadSelected", "btnBuildPairs",
   "btnDiscover", "btnActivateBatch", "btnValidateBatch", "selBatchChoice", "inpManualBatch"]
    .forEach((id) => { const el = $(id); if (el) el.disabled = act; });
  document.querySelectorAll('input[name="batchMode"]').forEach(r => { r.disabled = act; });
  $("queueList").querySelectorAll("button").forEach((b) => { b.disabled = act; });
  $("resultTable").querySelectorAll(".rowsel").forEach((b) => { b.disabled = act; });
}

/* ---------- 事件拉取 ---------- */
let _pollInFlight = false;
async function pollOnce() {
  if (_pollInFlight) return;   // 上一次请求未返回时不重叠轮询，避免事件重复
  if (!S.auth.logged_in) return;   // 未登录不轮询任务/事件，避免无谓的 401
  _pollInFlight = true;
  try {
    let data;
    try { data = await api("/api/tasks/current"); }
    catch (e) {
      if (e.httpStatus === 401) {
        try { await refreshBootstrap(); } catch (_) { resetAccountState(); renderAll(); }
      }
      return;
    }
    S.task = data;
    renderTask();

    try {
      const ev = await api("/api/tasks/events?after=" + S.lastSeq);
      if (ev.events && ev.events.length) {
        const list = $("eventList");
        ev.events.forEach((it) => {
          const li = document.createElement("li");
          li.dataset.level = it.level;
          const kind = SUBJECT_ZH[it.kind] || it.kind;
          li.innerHTML = `<span class="t">${esc(it.ts)}</span><strong>[${esc(kind)}]</strong> ${esc(it.message)}`;
          list.appendChild(li);
        });
        while (list.children.length > 260) list.removeChild(list.firstChild);
        list.scrollTop = list.scrollHeight;
        S.eventsRendered += ev.events.length;
      }
      S.lastSeq = ev.last_seq;
    } catch (e) { /* 忽略事件轮询瞬时错误 */ }
  } finally {
    _pollInFlight = false;
  }
}

/* ---------- bootstrap ---------- */
async function refreshBootstrap() {
  const data = await api("/api/bootstrap");
  applyBootstrap(data);
  return data;
}

function resetAccountState() {
  S.settings = null;
  S.batch = { state: "unavailable", message: "", active: null, choices: [], id: "", name: "", source: "unknown", validated_ts: "", mode: "auto", manual_id: "" };
  S.queue = [];
  S.selected = [];
  S.pairs = [];
  S.task = null;
  S.active = false;
  S.lastSeq = 0;
  S.eventsRendered = 0;
  $("eventList").innerHTML = "";
  $("resultTable").querySelector("tbody").innerHTML = "";
  $("resultWrap").classList.add("hidden");
  setStateLine("searchState", "", "");
  renderSelectedTable();
  renderSelectedDetail([]);
  setStateLine("selectedDialogState", "", "");
}

function clearBatchScopedState() {
  S.queue = [];
  S.selected = [];
  S.pairs = [];
  $("resultTable").querySelector("tbody").innerHTML = "";
  $("resultWrap").classList.add("hidden");
  setStateLine("searchState", "批次已切换，请重新搜索课程。", "");
  renderSelectedTable();
  renderSelectedDetail([]);
}

function applyBootstrap(d) {
  const previousAuth = S.auth || {};
  const previousBatch = activeBatch();
  const nextAuth = d.auth || { logged_in: false, user: "", password_held: false };
  const accountChanged = previousAuth.logged_in && (!nextAuth.logged_in || previousAuth.user !== nextAuth.user);
  if (accountChanged) resetAccountState();
  S.courseTypes = d.course_types ?? S.courseTypes;
  S.auth = nextAuth;
  S.settings = d.settings ?? null;
  S.batch = d.batch ?? S.batch;
  S.task = d.task ?? null;
  const nextBatch = activeBatch();
  if (!accountChanged && previousBatch && previousBatch.id !== (nextBatch?.id || "")) clearBatchScopedState();
  if (S.mode === "swap") renderPairsBuilt();
  fillTypeSelect();
  syncBatchControls();
  renderAll();
}

function fillTypeSelect() {
  const sel = $("selType");
  const cur = sel.value;
  sel.innerHTML = "";
  S.courseTypes.forEach((t) => {
    const o = document.createElement("option");
    o.value = t.id; o.textContent = `${t.id} · ${t.name}`;
    sel.appendChild(o);
  });
  if (S.courseTypes.some(t => String(t.id) === cur)) sel.value = cur;
}

function syncBatchControls() {
  const mode = (S.settings && S.settings.batch_mode) || "auto";
  const radios = document.querySelectorAll('input[name="batchMode"]');
  radios.forEach(r => { r.checked = r.value === mode; });
  $("autoBatchBox").classList.toggle("hidden", mode === "manual");
  $("manualBatchBox").classList.toggle("hidden", mode !== "manual");
  $("inpManualBatch").value = (S.settings && S.settings.batch_manual_id) || "";
  renderBatchChoices();
}

/* ---------- 设置弹窗 ---------- */
function openSettings() {
  const s = S.settings;
  if (!s) {
    // 多用户版设置按账号保存：未登录时给明确提示，而不是静默无反应。
    toast(S.auth.logged_in ? "设置尚未就绪，请刷新页面后重试。" : "请先登录后再打开“设置”。", "error");
    return;
  }
  $("inpStudentClass").value = s.student_class || "";
  $("inpInterval").value = s.poll_interval_sec || 5;
  $("chkTls").checked = s.tls_verify === false; // 勾选 = 关闭证书校验
  const pr = s.proxy || {};
  $("chkProxy").checked = !!pr.enabled;
  $("inpProxyUrl").value = "";   // 与授权码一致：留空 = 不修改已保存地址
  updateProxyNote();
  $("chkEmail").checked = !!s.smtp.enabled;
  $("selSecurity").value = s.smtp.security || "ssl";
  $("inpSmtpServer").value = s.smtp.server || "";
  $("inpSmtpPort").value = s.smtp.port || 465;
  $("inpSmtpUser").value = s.smtp.username || "";
  $("inpSmtpPassword").value = "";
  $("inpReceiver").value = s.smtp.receiver || "";
  updateEmailNote();
  if (!$("settingsDialog").open) $("settingsDialog").showModal();
}

/* 收集与当前设置不同、需要下发的字段；被服务器在“任务运行中”禁止的
   student_class / poll_interval_sec 若未变化则不下发，避免整单被 409 拒绝。 */
function buildSettingsBody() {
  const cur = S.settings;
  if (!cur) return {};
  const body = {};
  const sc = $("inpStudentClass").value.trim();
  if (sc !== (cur.student_class || "")) body.student_class = sc;
  const iv = parseInt($("inpInterval").value, 10) || 5;
  if (iv !== (cur.poll_interval_sec || 5)) body.poll_interval_sec = iv;
  const tv = !$("chkTls").checked;
  if (tv !== !!cur.tls_verify) body.tls_verify = tv;

  const smtpc = cur.smtp || {};
  const sm = {};
  const en = $("chkEmail").checked;
  if (en !== !!smtpc.enabled) sm.enabled = en;
  const sec = $("selSecurity").value;
  if (sec !== (smtpc.security || "ssl")) sm.security = sec;
  const server = $("inpSmtpServer").value.trim();
  if (server !== (smtpc.server || "")) sm.server = server;
  const port = parseInt($("inpSmtpPort").value, 10) || 465;
  if (port !== (smtpc.port || 465)) sm.port = port;
  const user = $("inpSmtpUser").value.trim();
  if (user !== (smtpc.username || "")) sm.username = user;
  const recv = $("inpReceiver").value.trim();
  if (recv !== (smtpc.receiver || "")) sm.receiver = recv;
  const pw = $("inpSmtpPassword").value;
  if (pw) sm.password = pw;
  if (Object.keys(sm).length) body.smtp = sm;

  const curp = cur.proxy || {};
  const px = {};
  const pe = $("chkProxy").checked;
  if (pe !== !!curp.enabled) px.enabled = pe;
  const purl = $("inpProxyUrl").value.trim();
  if (purl) px.url = purl;   // 仅当用户填了新地址才下发；空串保留原值
  if (Object.keys(px).length) body.proxy = px;
  return body;
}

function updateEmailNote() {
  const s = S.settings;
  if (!s) return;
  const has = !!(s.smtp && s.smtp.password_configured);
  $("emailSavedNote").textContent = has
    ? "已保存授权码（输入框留空即保留原值）。它保存在服务器数据目录 data/users/<学号>/settings.json，已加入 .gitignore。"
    : "尚未保存授权码。保存后它仅保存在服务器数据目录 data/users/<学号>/settings.json（已加入 .gitignore），API 不回传。";
}

function updateProxyNote() {
  const s = S.settings;
  if (!s) return;
  const pr = s.proxy || {};
  const note = $("proxyNote");
  if (!pr.configured) {
    note.textContent = "未配置代理。启用后，本程序访问统一认证/选课系统的请求会走这里；"
      + "服务器自身环境变量里的全局代理会被忽略。地址留空即保留已保存值。";
  } else {
    note.textContent = (pr.enabled ? "已启用代理：" : "已保存但未启用：")
      + (pr.url || "(已脱敏)") + "。代理密码不回传，只保存在你的账号设置文件中。";
  }
}

async function clearProxy() {
  try {
    const s = await api("/api/settings", { method: "PUT", body: { proxy: { enabled: false, url: null } } });
    S.settings = s;
    $("chkProxy").checked = false;
    $("inpProxyUrl").value = "";
    updateProxyNote();
    toast("已清除并停用代理");
  } catch (err) { toast(err.message, "error"); }
}

async function saveSettings() {
  const body = buildSettingsBody();
  if (!Object.keys(body).length) { $("settingsDialog").close(); return; }
  try {
    const s = await api("/api/settings", { method: "PUT", body });
    S.settings = s;
    $("inpSmtpPassword").value = "";
    $("inpProxyUrl").value = "";
    $("settingsDialog").close();
    updateProxyNote();
    toast("设置已保存", "ok");
    renderAll();
  } catch (err) { toast(err.message, "error"); }
}

async function clearEmailSecret() {
  try {
    await api("/api/settings", { method: "PUT", body: { smtp: { password: null } } });
    await refreshBootstrap();
    $("inpSmtpPassword").value = "";
    updateEmailNote();
    toast("已清除保存的授权码");
  } catch (err) { toast(err.message, "error"); }
}

async function testEmail() {
  // 表单改动先落盘，再用保存后的配置发送测试邮件（不改动运行期字段则不会被 409 拦截）。
  const body = buildSettingsBody();
  try {
    if (Object.keys(body).length) {
      S.settings = await api("/api/settings", { method: "PUT", body });
    }
    const data = await api("/api/settings/email/test", { method: "POST", body: {} });
    $("inpSmtpPassword").value = "";
    $("inpProxyUrl").value = "";
    updateEmailNote();
    updateProxyNote();
    toast(data.message, data.message.startsWith("发送成功") ? "ok" : "");
  } catch (err) {
    toast(err.message, "error");
  }
}

/* ---------- 已选课程详情 ---------- */
async function openSelectedDialog() {
  const d = $("selectedDialog");
  if (!d.open) d.showModal();
  await refreshSelectedDetail();
}

async function refreshSelectedDetail() {
  setStateLine("selectedDialogState", "正在拉取已选课程与详情…");
  try {
    const data = await api("/api/courses/selected/detail");
    renderSelectedDetail(data.sections || []);
    setStateLine("selectedDialogState",
      data.count ? `共 ${data.count} 门已选课程。` : "当前账号还没有已选课程。", "ok");
  } catch (err) {
    renderSelectedDetail([]);
    setStateLine("selectedDialogState", err.message, "error");
  }
}

function capacityCell(s) {
  if (s.capacity === null || s.capacity === undefined) return '<span class="hint">—</span>';
  const main = `${s.selected ?? "?"}/${s.capacity}` + (s.has_slot ? " 有空" : " 已满");
  const m = s.has_slot ? `<span class="good">${esc(main)}</span>` : `<span class="bad">${esc(main)}</span>`;
  const parts = [];
  const i = s.internal, ex = s.external;
  if (i && i.capacity !== null && i.capacity !== undefined) parts.push(`内 ${i.selected ?? "?"}/${i.capacity}`);
  if (ex && ex.capacity !== null && ex.capacity !== undefined) parts.push(`外 ${ex.selected ?? "?"}/${ex.capacity}`);
  return m + (parts.length ? `<div class="hint">${esc(parts.join(" · "))}</div>` : "");
}

function renderSelectedDetail(sections) {
  const tb = $("selectedDetailTable").querySelector("tbody");
  tb.innerHTML = "";
  $("selectedCount").textContent = sections.length;
  sections.forEach((s) => {
    const tr = document.createElement("tr");
    const title = esc(s.name || "—");
    const code = s.code ? ` <span class="mono">${esc(s.code)}</span>` : "";
    const sub = [];
    if (s.kxh) sub.push("课序号 " + s.kxh);
    if (s.type_name) sub.push(s.type_name);
    const schedule = (s.weeks || s.schedule)
      ? esc(s.weeks || "") + (s.weeks && s.schedule ? "<br>" : "") + (s.schedule ? esc(s.schedule) : "")
      : "—";
    tr.innerHTML = `
      <td><div><strong>${title}</strong>${code}</div>
          <div class="hint">${esc(sub.join(" · "))}</div></td>
      <td>${esc(s.teacher || "—")}</td>
      <td>${schedule}</td>
      <td>${esc(s.place || "—")}</td>
      <td class="num">${capacityCell(s)}</td>`;
    tb.appendChild(tr);
  });
}

/* ---------- 访问口令 ---------- */
function showGate() {
  const d = $("gateDialog");
  $("gateState").textContent = "";
  $("inpGate").value = "";
  if (!d.open) d.showModal();
  $("inpGate").focus();
}

async function submitGate(e) {
  e.preventDefault();
  const token = $("inpGate").value;
  if (!token) { setStateLine("gateState", "请输入口令。", "error"); return; }
  const btn = $("btnGateOk");
  btn.disabled = true;
  try {
    await api("/api/gate", { method: "POST", body: { token } });
    $("gateDialog").close();
    location.reload();   // 简单可靠：口令生效后整页重载进入应用
  } catch (err) {
    setStateLine("gateState", err.message, "error");
    $("inpGate").value = "";
  } finally {
    btn.disabled = false;
  }
}

/* ---------- 绑定事件 ---------- */
function bind() {
  $("formLogin").addEventListener("submit", doLogin);
  $("btnLogout").addEventListener("click", doLogout);
  $("btnResume").addEventListener("click", doResume);
  $("inpReloginPwd").addEventListener("keydown", (e) => { if (e.key === "Enter") doResume(); });

  document.querySelectorAll('input[name="batchMode"]').forEach((r) => {
    r.addEventListener("change", () => changeBatchMode(r.value));
  });
  $("btnDiscover").addEventListener("click", doDiscover);
  $("btnActivateBatch").addEventListener("click", doActivateBatch);
  $("selBatchChoice").addEventListener("change", renderBatchChoiceDetail);
  $("btnValidateBatch").addEventListener("click", doValidateManual);

  $("formSearch").addEventListener("submit", doSearch);
  $("btnAddSelected").addEventListener("click", addSelectedToQueue);

  $("queueList").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-i]");
    if (b) removeQueue(parseInt(b.dataset.i, 10));
  });
  $("btnClearQueue").addEventListener("click", () => { S.queue = []; renderQueue(); });

  document.querySelectorAll('input[name="mode"]').forEach((r) => {
    r.addEventListener("change", () => {
      S.mode = currentMode();
      renderModePanel();
      if (S.mode === "swap") renderSelectedTable();
    });
  });
  $("btnLoadSelected").addEventListener("click", loadSelected);
  $("btnBuildPairs").addEventListener("click", buildPairs);

  $("btnStart").addEventListener("click", startTask);
  $("btnStop").addEventListener("click", stopTask);

  $("btnSettings").addEventListener("click", openSettings);
  $("btnCancelSettings").addEventListener("click", () => $("settingsDialog").close());
  $("btnSelected").addEventListener("click", openSelectedDialog);
  $("btnRefreshSelected").addEventListener("click", refreshSelectedDetail);
  $("btnCloseSelected").addEventListener("click", () => $("selectedDialog").close());
  $("btnSaveSettings").addEventListener("click", saveSettings);
  $("btnTestEmail").addEventListener("click", testEmail);
  $("btnClearEmailSecret").addEventListener("click", clearEmailSecret);
  $("btnClearProxy").addEventListener("click", clearProxy);
  $("formSettings").addEventListener("submit", (e) => e.preventDefault());

  $("formGate").addEventListener("submit", submitGate);
  $("btnGateCancel").addEventListener("click", () => $("gateDialog").close());

  document.addEventListener("visibilitychange", () => {
    clearInterval(window.__pollTimer);
    startPolling();
  });
}

function startPolling() {
  const interval = document.hidden ? 4000 : 1000;
  window.__pollTimer = setInterval(pollOnce, interval);
}

/* ---------- init ---------- */
(async function init() {
  bind();
  let booted = false;
  try {
    await refreshBootstrap();
    booted = true;
  } catch (err) {
    if (err.httpStatus === 403) {
      showGate();   // 需要访问口令：输入后整页重载
    } else {
      toast("无法连接本程序：请确认服务已启动。", "error");
    }
  }
  if (booted) {
    startPolling();
    pollOnce();
  }
  // 队列变化后若处于改选模式且已有旧课程，提示重建
})();
