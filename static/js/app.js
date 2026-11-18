(() => {
  const view = document.getElementById("view");
  const msgEl = document.getElementById("msg");
  let tab = "batches";
  let meta = null;
  let batches = [];
  let selected = null;
  let detail = null;
  let reports = [];
  let refeedRuns = [];
  let refeedTarget = "";
  let form = { object_key: "", content_hash: "", byte_size: "100" };

  async function api(path, opts = {}) {
    const r = await fetch(path, {
      headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
      ...opts,
    });
    if (r.status === 401) {
      location.href = "/login";
      throw new Error("未登录");
    }
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.error || j.detail || ("HTTP " + r.status));
    return j;
  }

  function setMsg(t) {
    msgEl.textContent = t || "";
  }

  async function refresh() {
    meta = await api("/api/meta");
    document.getElementById("lab-brand").textContent = meta.lab_name || "湖区清单台";
    batches = (await api("/api/batches")).items || [];
    reports = (await api("/api/visibility-reports")).items || [];
    refeedRuns = (await api("/api/refeed-runs")).items || [];
  }

  async function openBatch(id) {
    selected = id;
    detail = await api("/api/batches/" + id);
    tab = "detail";
    syncNav();
    render();
  }

  async function doRegister() {
    if (!selected) return;
    setMsg("");
    try {
      await api("/api/batches/" + selected + "/objects", {
        method: "POST",
        body: JSON.stringify({
          object_key: form.object_key,
          content_hash: form.content_hash,
          byte_size: Number(form.byte_size || 0),
        }),
      });
      form = { object_key: "", content_hash: "", byte_size: "100" };
      await openBatch(selected);
      await refresh();
      setMsg("已登记对象");
      render();
    } catch (e) {
      setMsg(e.message);
    }
  }

  async function doClose() {
    if (!selected) return;
    setMsg("");
    try {
      await api("/api/batches/" + selected + "/close", { method: "POST", body: "{}" });
      await openBatch(selected);
      await refresh();
      setMsg("清单已闭合");
      render();
    } catch (e) {
      setMsg(e.message);
    }
  }

  async function doRefeed(objectId) {
    if (!selected) return;
    const targetId = Number(refeedTarget);
    if (!targetId) {
      setMsg("请先选择接收回灌的收集中批次");
      return;
    }
    setMsg("");
    try {
      const r = await api("/api/refeed", {
        method: "POST",
        body: JSON.stringify({
          source_batch_id: selected,
          target_batch_id: targetId,
          object_id: objectId,
        }),
      });
      await refresh();
      await openBatch(selected);
      setMsg(`回灌完成（${r.mode}）：副本已写入目标批 #${targetId}，旧闭合保留只读`);
      render();
    } catch (e) {
      setMsg(e.message);
    }
  }

  async function doReport() {
    setMsg("");
    try {
      const r = await api("/api/visibility-reports", {
        method: "POST",
        body: JSON.stringify({ as_of_date: "2026-11-16" }),
      });
      await refresh();
      tab = "reports";
      syncNav();
      setMsg(r.report.summary);
      render();
    } catch (e) {
      setMsg(e.message);
    }
  }

  function syncNav() {
    document.querySelectorAll("nav [data-tab]").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.tab === tab);
    });
  }

  function renderBatches() {
    return `
      <h2>落地批次</h2>
      <p class="muted">主链：选批 → 登记对象 → 闭合清单 → 可见性对账</p>
      <table>
        <thead><tr><th>编号</th><th>湖区</th><th>业务日</th><th>状态</th><th></th></tr></thead>
        <tbody>
          ${batches
            .map(
              (b) => `<tr>
            <td>${b.id}</td>
            <td>${b.zone_name}<div class="muted">${b.zone_code}</div></td>
            <td>${b.biz_date}</td>
            <td><span class="pill">${b.status}</span></td>
            <td><button type="button" class="secondary" data-open="${b.id}">打开</button></td>
          </tr>`
            )
            .join("")}
        </tbody>
      </table>`;
  }

  function lockedObjectIds() {
    // 已被回灌排他锁占用的源对象 id（同一源对象只许回灌一次）
    return new Set(
      refeedRuns
        .filter((r) => r.outcome === "已复制")
        .map((r) => r.source_object_id)
    );
  }

  function renderRefeedCard(b, objs) {
    const openBatches = batches.filter((x) => x.status === "收集中" && x.id !== b.id);
    const closedObjs = objs.filter((o) => o.status === "已闭合");
    const locked = lockedObjectIds();
    if (!closedObjs.length) return "";
    return `
      <div class="panel refeed-card">
        <h3>清单回灌 <span class="pill mode-pill">复制保留 · 旧闭合只读</span></h3>
        <p class="muted">已闭合对象复制到收集中批继续补采：旧批标记不动，副本带溯源入新批；同一对象排他，只许回灌一次。</p>
        <label>接收目标批（仅收集中）
          <select id="refeed-target">
            <option value="">— 请选择目标批 —</option>
            ${openBatches
              .map(
                (x) =>
                  `<option value="${x.id}" ${String(x.id) === String(refeedTarget) ? "selected" : ""}>#${x.id} ${x.zone_name} · ${x.biz_date}</option>`
              )
              .join("")}
          </select>
        </label>
        ${!openBatches.length ? `<p class="err">没有可接收的收集中批次，先去落地批次建/找开放批。</p>` : ""}
        <table>
          <thead><tr><th>源对象</th><th>指纹</th><th>回灌结局</th><th></th></tr></thead>
          <tbody>
            ${closedObjs
              .map((o) => {
                const done = locked.has(o.id);
                const run = refeedRuns.find(
                  (r) => r.source_object_id === o.id && r.outcome === "已复制"
                );
                return `<tr>
                  <td>${o.object_key}<div class="muted">源对象 #${o.id}</div></td>
                  <td>${o.content_hash}</td>
                  <td>${
                    done
                      ? `<span class="pill done-pill">已复制 → 批 #${run.target_batch_id} 副本 #${run.new_object_id}</span>`
                      : `<span class="muted">未回灌</span>`
                  }</td>
                  <td>${
                    done
                      ? `<button type="button" class="secondary" disabled>已回灌</button>`
                      : `<button type="button" class="secondary" data-refeed="${o.id}">回灌</button>`
                  }</td>
                </tr>`;
              })
              .join("")}
          </tbody>
        </table>
      </div>`;
  }

  function renderDetail() {
    if (!detail) {
      return `<h2>批次详情</h2><p class="muted">请先从落地批次打开一条。</p>`;
    }
    const b = detail.batch;
    const objs = detail.objects || [];
    const locked = lockedObjectIds();
    return `
      <h2>批次详情 #${b.id}</h2>
      <p>${b.zone_name} · ${b.biz_date} · <span class="pill">${b.status}</span></p>
      <div class="row-actions">
        <button type="button" id="btn-close">闭合清单</button>
        <button type="button" class="secondary" id="btn-report-from-detail">跑可见性对账</button>
      </div>
      <div class="panel">
        <h3>登记对象</h3>
        <label>对象键<input id="f-key" value="${form.object_key}" /></label>
        <label>内容指纹<input id="f-hash" value="${form.content_hash}" /></label>
        <label>字节数<input id="f-size" value="${form.byte_size}" /></label>
        <button type="button" id="btn-register">写入登记</button>
      </div>
      ${b.status === "已闭合" ? renderRefeedCard(b, objs) : ""}
      <h3>对象列表 <span class="muted small">归属以批次 #${b.id} 为准</span></h3>
      <table>
        <thead><tr><th>键</th><th>指纹</th><th>字节</th><th>状态</th><th>归属标记</th></tr></thead>
        <tbody>
          ${objs
            .map((o) => {
              const isCopy = o.source_object_id != null;
              const origin = isCopy
                ? `<span class="pill copy-pill">回灌副本 ← 批 #${o.source_batch_id} ${o.source_batch_name || ""} · 源 #${o.source_object_id}</span>`
                : `<span class="muted">本批原生</span>`;
              const wasRefed = locked.has(o.id);
              return `<tr>
            <td>${o.object_key}</td>
            <td>${o.content_hash}</td>
            <td>${o.byte_size}</td>
            <td>${o.status}${wasRefed ? `<div class="muted small">已回灌，旧对象只读</div>` : ""}</td>
            <td>${origin}</td>
          </tr>`;
            })
            .join("")}
        </tbody>
      </table>`;
  }

  function renderRefeedLog() {
    return `
      <h2>回灌留痕</h2>
      <p class="muted">本机库 refeed_runs 全量记录：复制保留路径下旧批闭合可追、新批副本可核，补采链路双向核对。</p>
      <table>
        <thead><tr><th>编号</th><th>路径</th><th>源批 → 目标批</th><th>源对象 → 副本</th><th>结局</th><th>说明</th><th>时间</th></tr></thead>
        <tbody>
          ${
            refeedRuns.length
              ? refeedRuns
                  .map(
                    (r) => `<tr>
            <td>${r.id}</td>
            <td><span class="pill mode-pill">${r.mode}</span></td>
            <td>#${r.source_batch_id} → #${r.target_batch_id}</td>
            <td>#${r.source_object_id} → ${r.new_object_id != null ? "#" + r.new_object_id : "—"}</td>
            <td><span class="pill ${r.outcome === "已复制" ? "done-pill" : "fail-pill"}">${r.outcome}</span></td>
            <td>${r.detail || ""}</td>
            <td>${r.created_at}</td>
          </tr>`
                  )
                  .join("")
              : `<tr><td colspan="7" class="muted">暂无回灌记录</td></tr>`
          }
        </tbody>
      </table>`;
  }

  function renderReports() {
    return `
      <h2>可见性对账</h2>
      <button type="button" id="btn-report">生成对账</button>
      <table>
        <thead><tr><th>编号</th><th>截至</th><th>错位</th><th>摘要</th></tr></thead>
        <tbody>
          ${reports
            .map(
              (r) => `<tr>
            <td>${r.id}</td><td>${r.as_of_date}</td><td>${r.mismatch_count}</td><td>${r.summary}</td>
          </tr>`
            )
            .join("")}
        </tbody>
      </table>`;
  }

  function render() {
    if (tab === "batches") view.innerHTML = renderBatches();
    else if (tab === "detail") view.innerHTML = renderDetail();
    else if (tab === "refeed") view.innerHTML = renderRefeedLog();
    else view.innerHTML = renderReports();

    view.querySelectorAll("[data-open]").forEach((btn) => {
      btn.onclick = () => openBatch(Number(btn.dataset.open));
    });
    const targetSel = document.getElementById("refeed-target");
    if (targetSel) {
      targetSel.onchange = () => (refeedTarget = targetSel.value);
    }
    view.querySelectorAll("[data-refeed]").forEach((btn) => {
      btn.onclick = () => doRefeed(Number(btn.dataset.refeed));
    });
    const reg = document.getElementById("btn-register");
    if (reg) {
      const key = document.getElementById("f-key");
      const hash = document.getElementById("f-hash");
      const size = document.getElementById("f-size");
      key.oninput = () => (form.object_key = key.value);
      hash.oninput = () => (form.content_hash = hash.value);
      size.oninput = () => (form.byte_size = size.value);
      reg.onclick = doRegister;
    }
    const closeBtn = document.getElementById("btn-close");
    if (closeBtn) closeBtn.onclick = doClose;
    const r1 = document.getElementById("btn-report");
    const r2 = document.getElementById("btn-report-from-detail");
    if (r1) r1.onclick = doReport;
    if (r2) r2.onclick = doReport;
  }

  document.querySelectorAll("nav [data-tab]").forEach((btn) => {
    btn.onclick = () => {
      tab = btn.dataset.tab;
      syncNav();
      render();
    };
  });
  document.getElementById("btn-logout").onclick = () => {
    fetch("/api/logout", { method: "POST" }).then(() => (location.href = "/login"));
  };

  refresh()
    .then(render)
    .catch((e) => setMsg(e.message));
})();
