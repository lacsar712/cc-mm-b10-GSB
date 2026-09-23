const tokenKey = "methane_token";
let token = localStorage.getItem(tokenKey) || "";
let role = localStorage.getItem("methane_role") || "";
let currentFilter = "";
const histCache = {};
const expanded = new Set();
let editingId = null;
let list = [];

const loginBox = document.querySelector("#login");
const appBox = document.querySelector("#app");
const rowsEl = document.querySelector("#rows");
const live = document.querySelector("#live");
const form = document.querySelector("#form");
const filterIn = document.querySelector("#filterIn");
const schemeSelect = document.querySelector("#schemeSelect");
const schemeBox = document.querySelector("#schemeBox");

function esc(v) {
  return String(v ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]),
  );
}

function tripletCell(v) {
  return v === null || v === undefined || v === ""
    ? `<span class="dim">—</span>`
    : esc(v);
}

function rowHtml(r) {
  const histBtn = `<button data-action="hist" data-id="${r.id}">履历</button>`;
  // 只有带三栏的新行才能改正；旧种子行没有原文，不给改正入口
  const editBtn =
    role === "writer" && r.wind_speed
      ? `<button data-action="edit" data-id="${r.id}">改正</button>`
      : "";
  return `<tr data-id="${r.id}">
    <td>${esc(r.site)}</td>
    <td>${esc(r.ch4_pct)}</td>
    <td>${tripletCell(r.wind_speed)}</td>
    <td>${tripletCell(r.roadway_temp)}</td>
    <td>${tripletCell(r.instrument_no)}</td>
    <td class="${r.level === "报警" ? "alarm" : "ok"}">${esc(r.level)}</td>
    <td>${esc(r.note)}</td>
    <td>${histBtn}${editBtn}</td>
  </tr>`;
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
  // 旁观账号不能新建过滤方案，但可以过滤、可以套用已有方案
  schemeBox.hidden = role !== "writer";
  connect();
  load();
  loadSchemes();
}

async function load() {
  const qs = currentFilter ? `?instrument=${encodeURIComponent(currentFilter)}` : "";
  list = await api(`/api/readings${qs}`);
  rowsEl.innerHTML = list.map(rowHtml).join("");
  for (const id of expanded) await attachDetail(id);
  if (editingId !== null) await attachDetail(editingId);
}

async function loadSchemes() {
  const schemes = await api("/api/filter-schemes");
  schemeSelect.innerHTML =
    `<option value="">命名方案（一键套用）</option>` +
    schemes
      .map(
        (s) =>
          `<option value="${esc(s.instrument)}" ${
            s.instrument === currentFilter ? "selected" : ""
          }>${esc(s.name)}（${esc(s.instrument)}）</option>`,
      )
      .join("");
}

async function attachDetail(id) {
  const anchor = rowsEl.querySelector(`tr[data-id="${id}"]`);
  if (!anchor || anchor.nextElementSibling?.classList.contains("detail")) return;
  const r = list.find((x) => x.id === id);
  let histHtml = "";
  try {
    if (!histCache[id]) histCache[id] = await api(`/api/readings/${id}/histories`);
    const hist = histCache[id];
    histHtml =
      hist.length === 0
        ? `<p class="dim">无三栏履历</p>`
        : `<ul class="hist">${hist
            .map(
              (h) =>
                `<li>[${esc(h.action)}] 风速 ${esc(h.wind_speed)} ／ 巷温 ${esc(
                  h.roadway_temp,
                )} ／ 仪器编号 ${esc(h.instrument_no)} —— ${esc(h.changed_by)} ${esc(
                  h.changed_at.replace("T", " ").slice(0, 19),
                )}</li>`,
            )
            .join("")}</ul>`;
  } catch (err) {
    histHtml = `<p class="dim">${esc(err.message)}</p>`;
  }
  const editHtml =
    role === "writer" && editingId === id && r && r.wind_speed
      ? `<div class="editbox">
          改正三栏（缺一不可）：
          <input data-k="wind_speed" value="${esc(r.wind_speed)}" placeholder="风速" />
          <input data-k="roadway_temp" value="${esc(r.roadway_temp)}" placeholder="巷温" />
          <input data-k="instrument_no" value="${esc(r.instrument_no)}" placeholder="仪器编号" />
          <button data-action="saveEdit" data-id="${id}">保存改正</button>
        </div>`
      : "";
  const tr = document.createElement("tr");
  tr.className = "detail";
  tr.innerHTML = `<td colspan="8"><strong>三栏履历</strong>${histHtml}${editHtml}</td>`;
  anchor.after(tr);
}

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/alerts`);
  ws.onmessage = () => {
    live.textContent = "收到新推送";
    load();
  };
}

document.querySelector("#go").onclick = async () => {
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
};

// 三栏强制：前端缺栏直接停住，不发请求；后端仍会再拦一道
form.onsubmit = async (e) => {
  e.preventDefault();
  const payload = {
    site: document.querySelector("#site").value,
    ch4_pct: Number(document.querySelector("#ch4").value),
    wind_speed: document.querySelector("#wind").value.trim(),
    roadway_temp: document.querySelector("#temp").value.trim(),
    instrument_no: document.querySelector("#instrument").value.trim(),
  };
  const missingLabels = [];
  if (!payload.wind_speed) missingLabels.push("风速");
  if (!payload.roadway_temp) missingLabels.push("巷温");
  if (!payload.instrument_no) missingLabels.push("仪器编号");
  if (missingLabels.length) {
    live.textContent = `停住：必填栏缺失（${missingLabels.join("、")}），未上报`;
    return;
  }
  try {
    await api("/api/readings", { method: "POST", body: JSON.stringify(payload) });
    live.textContent = "上报成功";
    document.querySelector("#ch4").value = "";
    document.querySelector("#wind").value = "";
    document.querySelector("#temp").value = "";
    document.querySelector("#instrument").value = "";
    load();
  } catch (err) {
    live.textContent = err.message;
  }
};

document.querySelector("#filterBtn").onclick = applyFilter;
filterIn.onkeydown = (e) => {
  if (e.key === "Enter") applyFilter();
};

async function applyFilter() {
  currentFilter = filterIn.value.trim();
  editingId = null;
  await load();
  await loadSchemes();
}

schemeSelect.onchange = async () => {
  currentFilter = schemeSelect.value;
  filterIn.value = currentFilter;
  editingId = null;
  await load();
};

document.querySelector("#schemeSave").onclick = async () => {
  const name = document.querySelector("#schemeName").value.trim();
  if (!name) {
    live.textContent = "先填方案名称";
    return;
  }
  if (!currentFilter) {
    live.textContent = "先输入并应用仪器编号片段，再存方案";
    return;
  }
  try {
    await api("/api/filter-schemes", {
      method: "POST",
      body: JSON.stringify({ name, instrument: currentFilter }),
    });
    document.querySelector("#schemeName").value = "";
    live.textContent = "方案已保存";
    await loadSchemes();
  } catch (err) {
    live.textContent = err.message;
  }
};

rowsEl.addEventListener("click", async (e) => {
  const btn = e.target.closest("button[data-action]");
  if (!btn) return;
  const id = Number(btn.dataset.id);
  if (btn.dataset.action === "hist") {
    if (expanded.has(id)) expanded.delete(id);
    else expanded.add(id);
    await load();
  } else if (btn.dataset.action === "edit") {
    editingId = editingId === id ? null : id;
    expanded.add(id);
    await load();
  } else if (btn.dataset.action === "saveEdit") {
    const detail = rowsEl.querySelector(`tr[data-id="${id}"] + tr.detail`);
    const vals = {};
    detail.querySelectorAll("input[data-k]").forEach((inp) => {
      vals[inp.dataset.k] = inp.value.trim();
    });
    const labels = { wind_speed: "风速", roadway_temp: "巷温", instrument_no: "仪器编号" };
    const missing = Object.keys(labels).filter((k) => !vals[k]);
    if (missing.length) {
      live.textContent = `停住：${missing.map((m) => labels[m]).join("、")}不能为空，未改正`;
      return;
    }
    try {
      await api(`/api/readings/${id}`, { method: "PATCH", body: JSON.stringify(vals) });
      // 旧履历行不变；作废缓存后重新拉取，可看到新增的改正快照
      delete histCache[id];
      editingId = null;
      expanded.add(id);
      live.textContent = "已改正，旧履历原文保留";
      await load();
    } catch (err) {
      live.textContent = err.message;
    }
  }
});

document.querySelector("#out").onclick = () => {
  localStorage.clear();
  location.reload();
};

if (token) showApp();
