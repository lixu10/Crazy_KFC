"use strict";

/* ---------- 状态 ---------- */
const S = {
  courseTypes: [],
  auth: { logged_in: false, user: "", password_held: false },
  settings: null,
  batch: { id: "", source: "unknown", validated_ts: "", mode: "auto", manual_id: "" },
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

const MODE_NAMES = { grab: "抢课", poll: "仅监控", swap: "安全改选" };
const STATUS_ZH = {
  pending: "排队中", running: "运行中", waiting_login: "等待重新登录",
  stopping: "正在停止", succeeded: "已完成", stopped: "已停止",
  failed: "已失败", manual_attention: "需要人工处理",
};
const SOURCE_ZH = {
  login_location: "登录捕获", html: "页面解析", last_success: "上次成功",
  legacy: "兼容候选", manual: "手动指定", unknown: "未设置",
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
  const msg = (j && j.error && j.error.message) || `请求失败（HTTP ${r.status}）`;
  const er = new Error(msg);
  er.httpStatus = r.status;
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
}

function renderAuth() {
  const btn = $("btnLogout");
  if (S.auth.logged_in) {
    btn.hidden = false;
    $("pillAuth").textContent = "认证 · " + (S.auth.user || "已登录");
    $("pillAuth").dataset.state = "ok";
    $("panelLogin").querySelectorAll("input").forEach(i => { i.disabled = true; });
    $("btnLogin").disabled = true;
  } else {
    btn.hidden = true;
    $("pillAuth").textContent = "认证 · 未登录";
    $("pillAuth").dataset.state = "idle";
    $("panelLogin").querySelectorAll("input").forEach(i => { i.disabled = false; });
    $("btnLogin").disabled = false;
  }
  if ($("inpPassword").value && !S.auth.logged_in) { $("inpPassword").value = ""; }
}

function renderBatchPill() {
  const p = $("pillBatch");
  const id = S.batch.id || (S.batch.mode === "manual" ? S.batch.manual_id || "" : "");
  const tag = id ? (id.length > 8 ? id.slice(-8) : id) : "—";
  const srcZh = SOURCE_ZH[S.batch.source] || S.batch.source || "未设置";
  p.textContent = id ? `批次 · ${tag}（${srcZh}）` : "批次 · 未设置";
  p.dataset.state = id ? (S.batch.source === "unknown" ? "warn" : "ok") : "idle";
  $("batchState").textContent = id
    ? `当前：${id}（来源 ${srcZh}${S.batch.validated_ts ? " · " + S.batch.validated_ts : ""}）`
    : "尚未取得有效批次";
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
    setStateLine("loginState", "登录成功，正在使用发现的批次。", "ok");
    toast("登录成功", "ok");
  } catch (err) {
    setStateLine("loginState", err.message, "error");
    toast(err.message, "error");
  } finally {
    S.busy = false;
    btn.disabled = false; btn.textContent = "登录并发现批次";
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
    toast("已恢复任务", "ok");
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
    if (data.ok) {
      toast("批次发现成功（" + data.source + "）", "ok");
    } else {
      setStateLine("batchState", data.msg || "未发现有效批次", "error");
    }
    await refreshBootstrap();
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
  if (!name) { toast("请输入课程名称或代码", "error"); return; }
  if (Number.isNaN(typeId)) { toast("请选择课程类型", "error"); return; }
  setStateLine("searchState", "搜索中…");
  try {
    const data = await api("/api/courses/search", { method: "POST", body: { name, class_type_id: typeId } });
    $("resultWrap").classList.remove("hidden");
    renderResults(data.sections || []);
    setStateLine("searchState", data.count === 0 ? "未找到精确匹配的教学班。" : "找到 " + data.count + " 个教学班。", data.count ? "ok" : "");
    if (!data.sections || !data.sections.length) $("resultWrap").classList.add("hidden");
  } catch (err) {
    $("resultWrap").classList.add("hidden");
    setStateLine("searchState", err.message, "error");
  }
}

function renderResults(sections) {
  const tb = $("resultTable").querySelector("tbody");
  tb.innerHTML = "";
  if (!sections || !sections.length) { $("resultCount").textContent = "无结果"; return; }
  $("resultCount").textContent = `本次结果 ${sections.length} 个，可多选加入目标。`;
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
    reloginBar.classList.add("hidden");
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
  reloginBar.classList.toggle("hidden", t.status !== "waiting_login");
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
  ["btnStart", "btnAddSelected", "btnClearQueue", "btnLoadSelected", "btnBuildPairs"]
    .forEach((id) => { const el = $(id); if (el) el.disabled = act; });
  $("queueList").querySelectorAll("button").forEach((b) => { b.disabled = act; });
  $("resultTable").querySelectorAll(".rowsel").forEach((b) => { b.disabled = act; });
}

/* ---------- 事件拉取 ---------- */
let _pollInFlight = false;
async function pollOnce() {
  if (_pollInFlight) return;   // 上一次请求未返回时不重叠轮询，避免事件重复
  _pollInFlight = true;
  try {
    let data;
    try { data = await api("/api/tasks/current"); }
    catch (e) { return; }
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

function applyBootstrap(d) {
  S.courseTypes = d.course_types || S.courseTypes;
  S.auth = d.auth || S.auth;
  S.settings = d.settings || S.settings;
  S.batch = d.batch || S.batch;
  S.task = d.task || null;
  S.pairs = [];
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
  $("lblManual").classList.toggle("hidden", mode !== "manual");
  $("inpManualBatch").value = (S.settings && S.settings.batch_manual_id) || "";
}

/* ---------- 设置弹窗 ---------- */
function openSettings() {
  const s = S.settings;
  if (!s) return;
  $("inpStudentClass").value = s.student_class || "";
  $("inpInterval").value = s.poll_interval_sec || 5;
  $("chkTls").checked = s.tls_verify === false; // 勾选 = 关闭证书校验
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
  return body;
}

function updateEmailNote() {
  const s = S.settings;
  if (!s) return;
  const has = !!(s.smtp && s.smtp.password_configured);
  $("emailSavedNote").textContent = has
    ? "已保存授权码（改留空即保留原值）。它保存在本机 data/settings.json，已加入 .gitignore。"
    : "尚未保存授权码。保存后它仅保存在本机 data/settings.json（已加入 .gitignore），API 不回传。";
}

async function saveSettings() {
  const body = buildSettingsBody();
  if (!Object.keys(body).length) { $("settingsDialog").close(); return; }
  try {
    const s = await api("/api/settings", { method: "PUT", body });
    S.settings = s;
    $("inpSmtpPassword").value = "";
    $("settingsDialog").close();
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
    updateEmailNote();
    toast(data.message, data.message.startsWith("发送成功") ? "ok" : "");
  } catch (err) {
    toast(err.message, "error");
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
  $("btnSaveSettings").addEventListener("click", saveSettings);
  $("btnTestEmail").addEventListener("click", testEmail);
  $("btnClearEmailSecret").addEventListener("click", clearEmailSecret);
  $("formSettings").addEventListener("submit", (e) => e.preventDefault());

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
  try {
    await refreshBootstrap();
  } catch (err) {
    toast("无法连接本程序：请确认服务已启动。", "error");
  }
  startPolling();
  pollOnce();
  // 队列变化后若处于改选模式且已有旧课程，提示重建
})();
