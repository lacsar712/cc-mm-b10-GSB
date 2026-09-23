const tokenKey = "methane_token";
let token = localStorage.getItem(tokenKey) || "";
let role = localStorage.getItem("methane_role") || "";
let historyId = null;

const loginBox = document.querySelector("#login");
const appBox = document.querySelector("#app");
const rows = document.querySelector("#rows");
const live = document.querySelector("#live");
const form = document.querySelector("#form");
const frag = document.querySelector("#frag");

function esc(v) {
  return String(v ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function cell(v) {
  // 旧种子行没有三栏，显示 — 但不影响新行；新行三栏齐全。
  return v ? esc(v) : '<span class="muted">—</span>';
}

function paint(list) {
  rows.innerHTML = list
    .map(
      (r) =>
        `<tr>
          <td>${esc(r.site)}</td>
          <td>${esc(r.ch4_pct)}</td>
          <td>${cell(r.wind_speed)}</td>
          <td>${cell(r.tunnel_temp)}</td>
          <td>${cell(r.instrument_no)}</td>
          <td class="${r.level === "报警" ? "alarm" : "ok"}">${esc(r.level)}</td>
          <td>${esc(r.note)}</td>
          <td><button data-id="${r.id}" class="histBtn">履历</button></td>
        </tr>`,
    )
    .join("");
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(options.headers || {}),
    },
  });
  if (res.status === 204) return {};
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || "请求失败");
  return data;
}

function showApp() {
  loginBox.hidden = true;
  appBox.hidden = false;
  document.querySelector("#who").textContent = role === "writer" ? "检查员" : "查看";
  document.querySelector("#out").hidden = false;
  form.hidden = role !== "writer";
  // 旁观账号不能新建过滤方案：方案名输入与保存按钮隐藏，仍可下拉套用方案。
  document.querySelector("#schemeEdit").hidden = role !== "writer";
  document.querySelector("#correctForm").hidden = role !== "writer";
  connect();
  loadSchemes();
  load();
}

async function load() {
  const q = frag.value.trim() ? `?instrument=${encodeURIComponent(frag.value.trim())}` : "";
  paint(await api(`/api/readings${q}`));
  if (historyId !== null) loadHistory(historyId);
}

async function loadSchemes() {
  const sel = document.querySelector("#schemes");
  const schemes = await api("/api/filter-schemes");
  sel.innerHTML =
    '<option value="">— 命名方案一键套用 —</option>' +
    schemes
      .map(
        (s) =>
          `<option value="${esc(s.instrument_fragment)}">${esc(s.name)}（${esc(s.instrument_fragment)}）</option>`,
      )
      .join("");
}

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/alerts`);
  ws.onmessage = (ev) => {
    const row = JSON.parse(ev.data);
    live.textContent = `刚推送：${row.site} ${row.level}`;
    load(); // 沿用当前片段过滤，不符合片段的新行不会出现
  };
}

document.querySelector("#go").onclick = async () => {
  try {
    const data = await api("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({
        username: document.querySelector("#user").value,
        password: document.querySelector("#pass").value,
      }),
    });
    token = data.access_token;
    role = data.role;
    localStorage.setItem(tokenKey, token);
    localStorage.setItem("methane_role", role);
    showApp();
  } catch (err) {
    live.textContent = err.message;
  }
};

form.onsubmit = async (e) => {
  e.preventDefault();
  const wind = document.querySelector("#wind").value.trim();
  const temp = document.querySelector("#temp").value.trim();
  const inst = document.querySelector("#instrument").value.trim();
  // 三栏强制填写，缺一即停住，不发请求、不入库、不推送。
  const missing = [];
  if (!wind) missing.push("风速");
  if (!temp) missing.push("巷温");
  if (!inst) missing.push("仪器编号");
  if (!document.querySelector("#site").value.trim()) missing.push("测点");
  if (document.querySelector("#ch4").value === "") missing.push("甲烷");
  if (missing.length) {
    live.textContent = `上报已停住：${missing.join("、")}未填写，三栏缺一不可`;
    return;
  }
  try {
    await api("/api/readings", {
      method: "POST",
      body: JSON.stringify({
        site: document.querySelector("#site").value.trim(),
        ch4_pct: Number(document.querySelector("#ch4").value),
        wind_speed: wind,
        tunnel_temp: temp,
        instrument_no: inst,
      }),
    });
    live.textContent = "上报成功";
    form.reset();
    load();
  } catch (err) {
    live.textContent = err.message;
  }
};

document.querySelector("#filterBtn").onclick = () => load();
frag.onkeydown = (e) => {
  if (e.key === "Enter") {
    e.preventDefault();
    load();
  }
};
document.querySelector("#clearFilter").onclick = () => {
  frag.value = "";
  document.querySelector("#schemes").value = "";
  load();
};

document.querySelector("#schemes").onchange = (e) => {
  // 一键套用命名方案：把片段填进过滤框并按服务端过滤。
  frag.value = e.target.value;
  load();
};

document.querySelector("#saveScheme").onclick = async () => {
  const name = document.querySelector("#schemeName").value.trim();
  const fragment = frag.value.trim();
  if (!name || !fragment) {
    live.textContent = "先填仪器编号片段和方案名称再保存";
    return;
  }
  try {
    await api("/api/filter-schemes", {
      method: "POST",
      body: JSON.stringify({ name, instrument_fragment: fragment }),
    });
    document.querySelector("#schemeName").value = "";
    live.textContent = "方案已保存，可在下拉中一键套用";
    loadSchemes();
  } catch (err) {
    live.textContent = err.message;
  }
};

async function loadHistory(id) {
  historyId = id;
  const list = await api(`/api/readings/${id}/history`);
  const panel = document.querySelector("#historyPanel");
  panel.hidden = false;
  document.querySelector("#historyTitle").textContent = `记录 #${id}`;
  document.querySelector("#historyRows").innerHTML = list.length
    ? list
        .map(
          (h) =>
            `<div class="hrow">${esc(h.changed_at.replace("T", " ").slice(0, 19))}
              <strong>${esc(h.label)}</strong>：
              <span class="muted">${h.old == null ? "（首次录入）" : esc(h.old)}</span>
              → <strong>${esc(h.new)}</strong>
              <span class="muted">by ${esc(h.changed_by)}</span>
            </div>`,
        )
        .join("")
    : '<p class="muted">暂无履历</p>';
}

rows.onclick = (e) => {
  const btn = e.target.closest(".histBtn");
  if (btn) loadHistory(Number(btn.dataset.id));
};

document.querySelector("#correctForm").onsubmit = async (e) => {
  e.preventDefault();
  const body = {};
  const w = document.querySelector("#cWind").value.trim();
  const t = document.querySelector("#cTemp").value.trim();
  const i = document.querySelector("#cInst").value.trim();
  if (w) body.wind_speed = w;
  if (t) body.tunnel_temp = t;
  if (i) body.instrument_no = i;
  if (!Object.keys(body).length) {
    live.textContent = "请至少填写一栏要改正的内容";
    return;
  }
  try {
    await api(`/api/readings/${historyId}`, { method: "PATCH", body: JSON.stringify(body) });
    live.textContent = "已改正，旧履历行保留改正前原文";
    e.target.reset();
    loadHistory(historyId);
    load();
  } catch (err) {
    live.textContent = err.message;
  }
};

document.querySelector("#closeHistory").onclick = () => {
  historyId = null;
  document.querySelector("#historyPanel").hidden = true;
};

document.querySelector("#out").onclick = () => {
  localStorage.clear();
  location.reload();
};

if (token) showApp();
