// 随机导览试点工作台：所有计算在服务端，前端只做展示与表单提交。
const $ = (sel) => document.querySelector(sel);
const api = async (path, options = {}) => {
  const resp = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  const data = await resp.json();
  if (!resp.ok) throw new Error(data.detail || `HTTP ${resp.status}`);
  return data;
};

const flash = (msg, ok = true) => {
  const el = $("#flash");
  el.textContent = msg;
  el.className = `flash ${ok ? "ok" : "err"}`;
  setTimeout(() => el.classList.add("hidden"), 4000);
};
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const badge = (v) => `<span class="badge ${esc(v)}">${esc(v)}</span>`;

// --------------------------------------------------------------- 标签页
document.querySelectorAll(".tabs button").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tabs button").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    document.querySelectorAll(".tab").forEach((t) => t.classList.add("hidden"));
    $(`#tab-${btn.dataset.tab}`).classList.remove("hidden");
    if (btn.dataset.tab === "batches") loadBatches();
    if (btn.dataset.tab === "disputes") loadDisputes();
    if (btn.dataset.tab === "audit") loadAudit();
  });
});

// --------------------------------------------------------------- 批次
async function loadBatches() {
  const { batches } = await api("/api/batches");
  const tbody = $("#table-batches tbody");
  tbody.innerHTML = batches.map((b) => {
    const p = b.pool || {};
    const s = b.segments || {};
    return `<tr>
      <td>${esc(b.id)}</td><td>${esc(b.zone)}</td><td>${badge(b.status)}</td>
      <td>未用 ${p.unused || 0} / 已派 ${p.issued || 0} / 完成 ${p.completed || 0} / 回收 ${p.reclaimed || 0}</td>
      <td>在用 ${s.assigned || 0} / 交回 ${s.returned || 0}</td>
      <td>${b.open_disputes ? `<span class="badge unresolved">${b.open_disputes} 未决</span>` : "0"}</td>
      <td>${b.has_analysis ? "已生成" : "—"}</td>
      <td>
        <button class="small" onclick="showDetail('${b.id}')">详情</button>
        <button class="small ghost" onclick="showAssign('${b.id}')">分号段</button>
      </td>
    </tr>`;
  }).join("");
}

$("#form-batch").addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  try {
    const body = { zone: fd.get("zone"), qty_per_arm: Number(fd.get("qty_per_arm")) };
    if (fd.get("seed")) body.seed = Number(fd.get("seed"));
    const batch = await api("/api/batches", { method: "POST", body });
    flash(`批次 ${batch.id} 已生成，A/B 各 ${batch.qty_per_arm} 个随机号`);
    e.target.reset();
    loadBatches();
  } catch (err) { flash(err.message, false); }
});

window.showDetail = async (batchId) => {
  const d = await api(`/api/batches/${batchId}`);
  $("#card-detail").classList.remove("hidden");
  $("#detail-title").textContent = `${d.id} · ${d.zone}`;
  const segRows = d.segments.map((s) =>
    `<tr><td>${esc(s.id)}</td><td>${esc(s.device_id)}</td><td>${esc(s.status)}</td>
     <td>${esc(JSON.stringify(s.counts))}</td><td>${esc(s.returned_at || "")}</td></tr>`).join("");
  $("#detail-body").innerHTML = `
    <div class="kv">
      <div><b>状态</b>${badge(d.status)}</div>
      <div><b>每方案号量</b>${d.qty_per_arm}</div>
      <div><b>种子</b>${esc(d.seed)}</div>
      <div><b>创建时间</b>${esc(d.created_at)}</div>
      <div><b>关场时间</b>${esc(d.closed_at || "—")}</div>
      <div><b>回传次数</b>${d.uploads.length}</div>
    </div>
    <h3>号码池</h3>
    <table><thead><tr><th>方案</th><th>状态</th><th>数量</th></tr></thead>
      <tbody>${d.pool_by_arm_state.map((r) =>
        `<tr><td>${esc(r.arm)}</td><td>${esc(r.state)}</td><td>${r.n}</td></tr>`).join("")}
      </tbody></table>
    <h3>号段</h3>
    <table><thead><tr><th>号段</th><th>设备</th><th>状态</th><th>号码计数</th><th>交回时间</th></tr></thead>
      <tbody>${segRows || '<tr><td colspan="5">暂无号段</td></tr>'}</tbody></table>
    <h3>争议（${d.disputes.length}）</h3>
    <pre class="package">${esc(JSON.stringify(d.disputes, null, 2))}</pre>
    ${d.analysis ? `<h3>分析</h3><pre class="package">${esc(JSON.stringify(d.analysis, null, 2))}</pre>` : ""}`;
};

let currentBatch = null;
window.showAssign = (batchId) => {
  currentBatch = batchId;
  $("#card-assign").classList.remove("hidden");
  $("#package-out").textContent = "";
};

$("#form-assign").addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  try {
    const body = { device_id: fd.get("device_id") };
    if (fd.get("size")) body.size = Number(fd.get("size"));
    const pkg = await api(`/api/batches/${currentBatch}/segments`,
      { method: "POST", body });
    $("#package-out").textContent = JSON.stringify(pkg, null, 2);
    flash(`号段 ${pkg.segment_id} 已分配给 ${pkg.device_id}（含 HMAC 签名，可交设备离线使用）`);
    loadBatches();
  } catch (err) { flash(err.message, false); }
});

// --------------------------------------------------------------- 回传与交回
$("#form-return").addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  try {
    const r = await api(`/api/segments/${fd.get("segment_id")}/return`, {
      method: "POST",
      body: { batch_id: fd.get("batch_id") },
    });
    flash(`号段 ${r.id} 已交回，计数 ${JSON.stringify(r.counts)}`);
  } catch (err) { flash(err.message, false); }
});

$("#form-upload").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    const payload = JSON.parse(e.target.elements.payload.value);
    const r = await api("/api/uploads", { method: "POST", body: payload });
    $("#upload-out").textContent = JSON.stringify(r, null, 2);
    flash(`回传完成：受理 ${r.accepted}，重放 ${r.replayed}，隔离 ${r.quarantined}`);
  } catch (err) { flash(err.message, false); }
});

// --------------------------------------------------------------- 争议
async function loadDisputes() {
  const { disputes } = await api("/api/disputes");
  $("#table-disputes tbody").innerHTML = disputes.map((d) => `
    <tr>
      <td>${d.id}</td><td>${esc(d.batch_id)}</td><td>${esc(d.code)}</td>
      <td>${esc(d.kind)}</td>
      <td><span class="badge ${d.status === "open" ? "unresolved" : "resolved"}">${esc(d.status)}</span></td>
      <td><pre class="package">${esc(d.detail)}</pre></td>
      <td>${d.status === "open"
        ? `<button class="small" onclick="resolveOne(${d.id})">标记解决</button>`
        : esc(d.resolution || "")}</td>
    </tr>`).join("");
}

window.resolveOne = async (id) => {
  const resolution = prompt("请输入处理结论：");
  if (!resolution) return;
  try {
    await api(`/api/disputes/${id}/resolve`, {
      method: "POST", body: { resolution },
    });
    flash("争议已标记解决");
    loadDisputes();
  } catch (err) { flash(err.message, false); }
};

// --------------------------------------------------------------- 关场与分析
$("#form-close").addEventListener("submit", async (e) => {
  e.preventDefault();
  const batchId = new FormData(e.target).get("batch_id");
  try {
    const r = await api(`/api/batches/${batchId}/close`, { method: "POST", body: {} });
    flash(`批次 ${r.id} 已关场`);
  } catch (err) { flash(err.message, false); }
});

$("#form-analysis").addEventListener("submit", async (e) => {
  e.preventDefault();
  const batchId = new FormData(e.target).get("batch_id");
  try {
    const r = await api(`/api/batches/${batchId}/analysis`, { method: "POST", body: {} });
    $("#analysis-out").textContent = JSON.stringify(r, null, 2);
    flash("分析已生成（幂等：重复调用返回同一快照）");
  } catch (err) { flash(err.message, false); }
});

$("#btn-approve").addEventListener("click", async () => {
  const batchId = $("#form-analysis").elements.batch_id.value;
  try {
    const r = await api(`/api/batches/${batchId}/approve`,
      { method: "POST", body: { note: "工作台批准" } });
    $("#analysis-out").textContent = JSON.stringify(r, null, 2);
    flash("分析已批准，可分发");
  } catch (err) { flash(err.message, false); }
});

$("#btn-distribute").addEventListener("click", async () => {
  const batchId = $("#form-analysis").elements.batch_id.value;
  try {
    const r = await api(`/api/batches/${batchId}/distribute`,
      { method: "POST", body: {} });
    $("#analysis-out").textContent = JSON.stringify(r, null, 2);
    flash("已标记分发");
  } catch (err) { flash(err.message, false); }
});

// --------------------------------------------------------------- 审计
async function loadAudit() {
  const { audit } = await api("/api/audit");
  $("#table-audit tbody").innerHTML = audit.map((a) =>
    `<tr><td>${esc(a.at)}</td><td>${esc(a.actor)}</td><td>${esc(a.action)}</td>
     <td>${esc(a.entity)}</td><td><pre class="package">${esc(a.detail)}</pre></td></tr>`).join("");
}

loadBatches().catch((err) => flash(err.message, false));
