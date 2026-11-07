/* genai-bench 单人版 SPA.
 *
 * ZERO dependencies, ZERO CDN, ZERO runtime network beyond our own relative
 * /api paths. Hash routing so a refresh on any frontend route never 404s
 * (the Flask fallback always serves index.html). All local state (connection
 * presets, param presets, last form, cross-page selection) lives in the
 * browser's localStorage ONLY -- guardrail #4.
 */
(function (global) {
  "use strict";

  // ------------------------------------------------------------------ utils
  var GB = {};
  global.GB = GB;
  var $ = function (sel, root) { return (root || document).querySelector(sel); };
  var $$ = function (sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); };
  var esc = function (s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  };
  var attr = function (s) { return esc(s).replace(/"/g, "&quot;"); };

  function api(path, opts) {
    opts = opts || {};
    var init = { method: opts.method || "GET", headers: {} };
    if (opts.body !== undefined) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(opts.body);
    }
    return fetch(path, init).then(function (r) {
      var ct = r.headers.get("content-type") || "";
      if (ct.indexOf("application/json") >= 0) {
        return r.json().then(function (j) { if (!r.ok) { j._status = r.status; } return j; });
      }
      return r.blob().then(function (b) { return { _blob: b, _status: r.status, _disp: r.headers.get("content-disposition") }; });
    });
  }

  var toastTimer = null;
  function toast(msg, kind) {
    var t = $("#toast");
    t.textContent = msg;
    t.className = "toast show" + (kind ? " " + kind : "");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { t.className = "toast"; }, 3600);
  }

  // local state (browser only; never sent to server except as explicit form values)
  var LS = {
    get: function (k, d) { try { var v = localStorage.getItem("gb." + k); return v ? JSON.parse(v) : d; } catch (e) { return d; } },
    set: function (k, v) { try { localStorage.setItem("gb." + k, JSON.stringify(v)); } catch (e) {} },
  };

  var S = {
    templates: null,
    form: null,              // { task_ids:[], values:{taskid:{...}}, secrets:{}, title:"", sweep:{field,values,on} }
    selected: new Set(LS.get("selected", [])),
    history: { page: 1, size: 20, task: "", model: "", status: "", keyword: "", group: "", starred: "", sort: "start_desc" },
    pollTimer: null,
    lastQueue: null,
    activeBatch: LS.get("activeBatch", null),   // survives refresh so 中断本批/进度 still work
  };

  function saveSelected() { LS.set("selected", Array.from(S.selected)); }

  // ------------------------------------------------------------- bootstrap
  function boot() {
    api("/api/health").then(function (h) {
      $("#health-dot").className = "dot " + (h.ok ? "ok" : "bad");
    }).catch(function () { $("#health-dot").className = "dot bad"; });
    api("/api/templates").then(function (t) {
      S.templates = t;
      S.form = defaultForm();
      // restore last form but never restore secrets from disk
      var last = LS.get("lastForm", null);
      if (last && last.task_ids) {
        S.form.task_ids = Array.isArray(last.task_ids) ? last.task_ids.slice(0, 1) : [last.task_ids];
        S.form.values = last.values || {};
        S.form.title = last.title || "";
        if (last.sweep) S.form.sweep = last.sweep;
        S.activePreset = last.preset_id || null;
        S.form.preset_id = last.preset_id || "";
        S.form.preset_name = last.preset_name || "";
      }
      route();
    }).catch(function (e) { $("#view").innerHTML = '<div class="card"><h2>无法加载模板</h2><p class="small">' + esc(e.error || e) + "</p></div>"; });
    global.addEventListener("hashchange", route);
    startPolling();
  }

  function defaultForm() {
    var values = {};
    if (S.templates) S.templates.tasks.forEach(function (t) { values[t.id] = defaultsFor(t); });
    return { task_ids: S.templates ? [S.templates.tasks[0].id] : [], values: values, secrets: {}, title: "", sweep: { on: false, field: "", values: "" } };
  }
  function defaultsFor(task) {
    var v = {};
    task.common_fields.forEach(function (f) { if (f.default !== undefined && f.default !== null) v[f.key] = f.default; });
    task.fields.forEach(function (f) { if (f.default !== undefined && f.default !== null) v[f.key] = f.default; });
    return v;
  }
  function task(id) { for (var i = 0; i < S.templates.tasks.length; i++) if (S.templates.tasks[i].id === id) return S.templates.tasks[i]; return null; }
  function persistForm() {
    LS.set("lastForm", {
      task_ids: S.form.task_ids, values: S.form.values, title: S.form.title,
      sweep: S.form.sweep, preset_id: S.form.preset_id || "", preset_name: S.form.preset_name || ""
    });
  }

  // --------------------------------------------------------------- routing
  function route() {
    var raw = location.hash.replace(/^#\/?/, "") || "new";
    var qi = raw.indexOf("?");
    var query = {};
    if (qi >= 0) {
      raw.slice(qi + 1).split("&").forEach(function (kv) {
        if (!kv) return;
        var p = kv.split("=");
        query[decodeURIComponent(p[0])] = decodeURIComponent(p.slice(1).join("=") || "");
      });
      raw = raw.slice(0, qi);
    }
    var parts = raw.split("/");
    var view = parts[0];
    if (query.group !== undefined) { S.history.group = query.group; S.history.page = 1; }
    $$("[data-nav]").forEach(function (a) { a.classList.toggle("active", a.getAttribute("data-nav") === view || (view === "dashboard" && a.getAttribute("data-nav") === "dashboard")); });
    if (view === "history") renderHistory();
    else if (view === "dashboard") renderDashboard(parts[1] || null);
    else if (view === "new") renderNew();
    else renderNew();
  }
  // NOTE: `location.hash = "/#/new"` makes the browser store "#/#/new" (it adds
  // its own "#"), and route() then reads view === "#" and silently falls back to
  // the 新建实验 page -- which is why the post-发起 jump to the live monitor used
  // to land on the wrong page. Always normalise to a single leading "#/".
  GB.go = function (h) {
    var t = String(h || "").trim();
    t = t.replace(/^#/, "");
    t = t.replace(/^\/#/, "");
    if (t.charAt(0) !== "/") t = "/" + t;
    if (location.hash === "#" + t) { route(); return; }
    location.hash = t;
  };

  // ------------------------------------------------------------ queue poll
  function startPolling() {
    if (S.pollTimer) return;
    S.pollTimer = setInterval(function () {
      api("/api/benchmark/queue").then(function (q) {
        S.lastQueue = q;
        var pill = $("#queue-pill");
        if (!pill) return;
        if (q.running) { pill.textContent = "● 运行中 " + q.running.slice(0, 8) + " · 队列 " + q.queued.length; pill.className = "pill running"; }
        else if (q.paused) { pill.textContent = "⏸ 已停止(队列暂停)"; pill.className = "pill"; }
        else { pill.textContent = "空闲(严格串行)"; pill.className = "pill"; }
        var qs = $("#queue-list"); if (qs) qs.innerHTML = queueHtml(q);
        var bs = $("#batch-slot"); if (bs) bs.innerHTML = batchProgressHtml(q);
        var ds = $("#dash-status"); if (ds) refreshDashboardStatus();
      }).catch(function () {});
    }, 2500);
  }
  function queueHtml(q) {
    if (!q.jobs || !q.jobs.length) return '<div class="small">队列为空。多选 / 扫描 / 批量重跑都进入这一条串行队列。</div>';
    return q.jobs.slice(-8).reverse().map(function (j) {
      return '<div class="queue-item"><span class="badge ' + esc(j.status) + '">' + esc(j.status) + "</span>" +
        '<span class="rid" onclick="GB.copy(this)">' + esc(j.run_id) + "</span>" +
        '<span>' + esc(j.title || "") + "</span>" +
        (j.position ? '<span class="small">位置 ' + j.position + "</span>" : "") +
        (j.status === "running" || j.status === "queued" ? '<button class="btn sm danger" onclick="GB.stopRun(\'' + attr(j.run_id) + '\')">停止</button>' : "") +
        "</div>";
    }).join("");
  }
  GB.stopRun = function (run_id) {
    api("/api/benchmark/" + encodeURIComponent(run_id) + "/stop", { method: "POST" }).then(function (r) {
      toast(r.ok ? "已终止, 队列后续不会自动开跑" : (r.error || "停止失败"), r.ok ? "good" : "bad");
      if (location.hash.indexOf("new") >= 0) renderNew();
    });
  };
  GB.copy = function (node) {
    var txt = node.textContent;
    if (navigator.clipboard) navigator.clipboard.writeText(txt).then(function () { toast("已复制 " + txt, "good"); });
    else toast(txt);
  };

  // ================================================================ NEW PAGE
  function renderNew() {
    if (!S.templates) { $("#view").innerHTML = '<div class="empty">加载中…</div>'; return; }
    var t = S.templates;
    var ids = S.form.task_ids;
    var html = "";
    html += '<h1>新建实验</h1><p class="sub">探活 → 选一个任务 → 选一个套餐（把参考参数填进表单，都能改）→ 确认无误后点「发起实验」→ 串行跑 → 实时监控 → 结果进历史（可批量下载/删除）。跑完的原始结果会原样保留。</p>';

    // deviations notice
    if (t.deviations && t.deviations.length) {
      html += '<details class="fold"><summary>规格 ↔ 引擎偏差说明(' + t.deviations.length + ' 项, 点击展开)</summary><div class="fold-body"><ul class="small">' +
        t.deviations.map(function (d) { return "<li>" + esc(d) + "</li>"; }).join("") + "</ul></div></details>";
    }

    // local config fold (connection/env presets + API key, explicitly local-only)
    html += '<details class="fold" id="local-fold"><summary>本地配置 / 环境预设（仅存本浏览器 localStorage）</summary><div class="fold-body">' +
      '<div class="notice">连接信息、环境预设、API Key 只保存在本浏览器本地，不上传、不落服务器磁盘；API Key 仅在单次发起时进入后端内存并注入子进程环境，绝不写入 params.json / 日志 / 导出包。改模板＝改表单命名，重启生效。</div>' +
      '<div class="row"><div class="col">' +
      '<label class="fld"><span>环境预设名称</span><input type="text" id="preset-name" placeholder="例如 本地 vLLM / 测试环境"></label>' +
      '<div class="btnbar"><button class="btn sm" onclick="GB.savePreset()">保存当前为预设</button>' +
      '<select id="preset-list" style="width:auto"><option value="">选择已存预设…</option></select>' +
      '<button class="btn sm" onclick="GB.loadPreset()">载入</button>' +
      '<button class="btn sm danger" onclick="GB.delPreset()">删除</button></div></div>' +
      '<div class="col"><label class="fld"><span>API Key（仅本地暂存，刷新即清空，不会落盘）</span>' +
      '<input type="password" id="api-key" autocomplete="off" placeholder="sk-…"></label>' +
      '<label class="chk"><input type="checkbox" id="remember-key"> 会话内记住(仍只在本页内存)</label></div></div></div></details>';

    // preflight card
    html += '<div class="card tight"><div class="btnbar"><strong>连接探活</strong>' +
      '<span class="small">对 /v1/models 发一次最小请求：可达 / 401 / 超时 / 模型不存在。不落盘、不记日志。</span>' +
      '<input type="text" id="pf-base" style="width:260px" placeholder="api_base (默认取所选任务连接字段)">' +
      '<input type="text" id="pf-model" style="width:200px" placeholder="模型名(可选)">' +
      '<button class="btn primary" onclick="GB.preflight()">探活</button>' +
      '<span id="pf-result" class="small"></span></div></div>';

    // refill (反显) card
    html += '<div class="card tight"><div class="btnbar"><strong>参数反显</strong>' +
      '<span class="small">与「重跑」不同：把历史/预设/CLI 的完整参数(含折叠与固定值)填回表单，改动 diff 可见，再手动发起。</span>' +
      '<button class="btn sm" onclick="GB.refill.history()">历史为模板新建</button>' +
      '<button class="btn sm" onclick="GB.refill.preset()">本地参数预设</button>' +
      '<button class="btn sm" onclick="GB.refill.cli()">粘贴 CLI</button>' +
      '<button class="btn sm" onclick="GB.saveParamPreset()">存为参数预设</button></div>' +
      '<div id="refill-diff" class="small"></div></div>';

    // task SINGLE-select (one task per experiment launch)
    html += '<div class="card"><h2>① 选择任务（单选：一次只压一个任务）</h2>' +
      '<p class="small">三个任务分别压「文本生成 / 向量生成 / 重排序」。先选一个；想压别的任务，等这一批跑完再新建一次即可（严格串行，不混跑）。</p><div class="task-sel">';
    t.tasks.forEach(function (tk) {
      var on = ids[0] === tk.id;
      html += '<div class="task-card ' + (on ? "on" : "") + '" onclick="GB.selectTask(\'' + attr(tk.id) + '\')">' +
        '<div class="tname">' + esc(tk.display_name) + '</div><div class="tcode">' + esc(tk.short_code) + " · " + esc(tk.task) + "</div>" +
        '<div class="tnote">' + esc(tk.defaults_note || "") + "</div>" +
        (on ? '<div class="tag" style="margin-top:8px;color:var(--accent)">已选</div>' : "") + "</div>";
    });
    html += "</div></div>";

    // Tokenizer upload + selection (offline local bundles; no HF_TOKEN needed)
    html += '<div class="card tight"><h2>Tokenizer · 上传本地文件 / 选择使用</h2>' +
      '<p class="small">上传 HuggingFace 风格 tokenizer 目录文件（tokenizer.json / tokenizer_config.json / vocab.json / merges.txt / tokenizer.model / spiece.model 等）后即可<b>选择</b>使用：后端把所选本地目录以绝对路径传给 <span class="kbd">--model-tokenizer</span>，<b>离线加载、无需 HF_TOKEN / 联网</b>。不选则照常在各任务「Tokenizer」输入框手填 HuggingFace ID。文件仅存服务端实验目录 <span class="kbd">_tokenizers/</span>，不进导出包。</p>' +
      '<div class="tokenizer-pick">' +
      '<select id="tok-select"><option value="">— 未选用本地 Tokenizer（手动填 HF ID）—</option></select>' +
      '<button class="btn sm primary" onclick="GB.applyTokenizer()">应用到所选任务</button></div>' +
      '<div class="row"><div class="col"><label class="fld"><span>名称（便于识别，如 Qwen2.5-7B tokenizer）</span>' +
      '<input type="text" id="tok-name" placeholder="留空则默认为 tokenizer"></label></div>' +
      '<div class="col"><label class="fld"><span>选择 tokenizer 文件（可多选，请勿选模型权重）</span>' +
      '<input type="file" id="tok-files" multiple></label></div></div>' +
      '<div class="btnbar"><button class="btn primary" onclick="GB.uploadTokenizer()">上传并选用</button>' +
      '<span id="tok-status" class="small"></span></div>' +
      '<div class="tokenizer-list" id="tok-list"></div></div>';

    // 套餐 (purpose-named presets) — primary UI for beginners
    html += presetBoardHtml();

    // per-task params: ALWAYS VISIBLE (套餐 only fills reference values; user edits freely)
    if (ids.length) {
      html += '<div class="card"><h2>③ 参数（套餐已填入参考值，<b>都能改</b>，改完再决定要不要发起）</h2>' +
        '<p class="small">下面是套餐/模板填好的参考参数，只是建议。每一项下方都有「大白话」解释。确认满意了，再到最下面点「发起实验」；<b>点套餐不会自动跑</b>。</p>' +
        '<details class="fold" open><summary>参数表单（展开可看全部字段）</summary><div class="fold-body">' +
        taskParamsCard(ids[0]) + glossaryHtml() + "</div></details></div>";
    }

    // sweep: for a whole-series preset this is the EDITABLE ladder shown before launch
    html += '<div class="card"><h2>④ 参数扫描（整串套餐会把「档位」列在这里，<b>可改</b>，确认后才发起）</h2>' +
      '<label class="chk"><input type="checkbox" id="sweep-on" ' + (S.form.sweep.on ? "checked" : "") + ' onchange="GB.toggleSweep(this.checked)"> 启用扫描</label>' +
      '<div class="row" style="margin-top:10px"><div class="col"><label class="fld"><span>扫描字段（把哪个旋钮从低到高逐档跑）</span><select id="sweep-field"></select></label></div>' +
      '<div class="col"><label class="fld"><span>档位列表（逗号分隔，例如 4,8,16,32,64；可随意删改）</span><input type="text" id="sweep-values" value="' + attr(S.form.sweep.values) + '" placeholder="4,8,16,32,64"></label></div></div>' +
      '<p class="hint">整串套餐＝把某一旋钮（如并发）从低到高每一档各跑一次，一档一档严格串行，跑完自动出对比/拐点图。这里列出的档位只是参考，改完点「发起实验」才真正入队；随时可「中断本批」。</p></div>';

    // title + actions
    html += '<div class="card"><h2>⑤ 标题与发起</h2>' +
      '<label class="fld"><span>任务显示名（可编辑；留空则按模板自动生成。run_id 永不变，改标题不影响目录/Dashboard/重跑）</span>' +
      '<input type="text" id="run-title" value="' + attr(S.form.title) + '" placeholder="留空自动生成，如 Chat · Qwen2.5-7B · c32 L1k t10m · 1107-1430"></label>' +
      '<div class="btnbar"><button class="btn primary" onclick="GB.startRuns()">发起实验（串行）</button>' +
      '<button class="btn" onclick="GB.preview()">预览最终参数</button>' +
      '<span class="small" id="start-summary"></span></div></div>';

    // queue card
    html += '<div class="card"><h2>串行队列（任何时候只跑一个；中断本批后剩余档不自动开跑）</h2>' +
      '<div id="batch-slot"></div><div id="queue-list">' + queueHtml({ jobs: [] }) + "</div></div>";

    $("#view").innerHTML = html;
    refreshPresetList();
    fillSweepFields();
    renderTokenizerWidget();
    // paint batch progress immediately (poll also keeps it fresh)
    if (S.lastQueue) { var bs = $("#batch-slot"); if (bs) bs.innerHTML = batchProgressHtml(S.lastQueue); }
    var keyInput = $("#api-key");
    if (S.form.secrets && S.form.secrets.api_key) keyInput.value = S.form.secrets.api_key;
    keyInput.addEventListener("input", function () { S.form.secrets.api_key = keyInput.value; });
  }

  // -- 套餐 presets (purpose-named, backend config driven) -------------------
  function presetList() { return (S.templates && S.templates.presets) || []; }
  function presetById(id) { var l = presetList(); for (var i = 0; i < l.length; i++) if (l[i].id === id) return l[i]; return null; }

  function presetCombo(p) {
    var g = (S.templates && S.templates.glossary) || {};
    return ((g.preset_combo || {})[p.id]) || "";
  }

  // Beginner glossary: 名词表 + 常见组合 + 怎么读曲线. All text comes from the
  // ONE backend config file (guardrail #6), so editing it needs no frontend build.
  function glossaryHtml() {
    var g = (S.templates && S.templates.glossary) || {};
    if (!g || (!g.terms && !g.combos)) return "";
    var html = '<details class="fold"><summary>📖 名词表 / 常见组合说明（小白必看，点开）</summary><div class="fold-body">' +
      (g.intro ? '<div class="notice">' + esc(g.intro) + "</div>" : "");
    if (g.terms && g.terms.length) {
      html += "<h3>名词大白话</h3><div class=" + '"glossary-grid"' + ">";
      g.terms.forEach(function (x) {
        html += '<div class="gterm"><b>' + esc(x.term) + "</b><div>" + esc(x.plain) + "</div></div>";
      });
      html += "</div>";
    }
    if (g.combos && g.combos.length) {
      html += "<h3>不同参数组合分别在测什么</h3><div class=" + '"glossary-grid"' + ">";
      g.combos.forEach(function (x) {
        html += '<div class="gterm"><b>' + esc(x.name) + "</b><div>" + esc(x.plain) + "</div></div>";
      });
      html += "</div>";
    }
    html += "</div></details>";
    return html;
  }

  function chartHelpHtml() {
    var g = (S.templates && S.templates.glossary) || {};
    var rc = g.read_chart;
    if (!rc) return "";
    var html = '<details class="fold" open><summary>📈 这图怎么看懂（小白向）</summary><div class="fold-body">' +
      (rc.intro ? '<div class="notice">' + esc(rc.intro) + "</div>" : "") +
      (rc.points && rc.points.length ? '<ul class="small">' + rc.points.map(function (p) { return "<li>" + esc(p) + "</li>"; }).join("") + "</ul>" : "") +
      (rc.hint ? '<p class="hint">' + esc(rc.hint) + "</p>" : "") +
      "</div></details>";
    return html;
  }

  function presetBoardHtml() {
    var list = presetList();
    if (!list.length) return "";
    var html = '<div class="card"><h2>② 选一个套餐（单选：一次只套用一个，参数只是参考、可改）</h2>' +
      '<p class="small">每个套餐都按<b>目的</b>命名，告诉你它在干嘛、会看到什么、什么时候选它，并解释了<b>这组参数组合到底在测什么</b>。' +
      '点一下套餐：它只把<b>参考参数填进下面的表单</b>（都会显示出来、都能改），<b>不会自动开始跑</b>；你确认无误后再点最下面的「发起实验」。' +
      '带 <b>整串</b> 的套餐会把整条压力梯子逐档列在「参数扫描」里，同样要你确认才发起。</p>' +
      '<div class="preset-grid">';
    list.forEach(function (p) {
      var on = S.activePreset === p.id;
      var combo = presetCombo(p);
      html += '<div class="preset-card' + (on ? " on" : "") + '" onclick="GB.runPreset(\'' + attr(p.id) + '\')">' +
        '<div class="picon">' + esc(p.icon || "•") + "</div>" +
        '<div class="pname">' + esc(p.name) + (p.default ? ' <span class="tag" style="color:var(--accent)">推荐</span>' : "") + (on ? ' <span class="tag" style="color:var(--good)">已选</span>' : "") + "</div>" +
        '<div class="ptag">' + esc(p.tag || "") + "</div>" +
        '<div class="pwhen"><b>什么时候选我：</b>' + esc(p.when || "") + "</div>" +
        '<div class="psee"><b>会看到：</b>' + esc(p.see || "") + "</div>" +
        (combo ? '<div class="pcombo"><b>这组参数在测什么：</b>' + esc(combo) + "</div>" : "") +
        "</div>";
    });
    html += "</div>" +
      '<p class="hint">套餐只是把推荐值填表，<b>改不改、跑不跑都由你决定</b>。单点套餐填一组固定值；整串套餐把各档位填进「④ 参数扫描」，确认后一次性串行入队并自动出拐点图。随时可「中断本批」。套餐定义在 <span class="kbd">genai_bench/web/templates/webui_templates.json</span>。</p>' +
      "</div>";
    return html;
  }

  // A preset ONLY fills reference values into the form. It never auto-starts a run.
  // The user edits anything they like, then decides to press "发起实验".
  GB.runPreset = function (id) {
    var p = presetById(id); if (!p) return;
    var tid = S.form.task_ids[0];
    if (!tid) { toast("请先选一个任务", "bad"); return; }

    // single-point preset: fill the friendly task-parameter values for this task
    if (p.kind !== "series") {
      var set = (p.set && p.set[tid]) || {};
      Object.keys(set).forEach(function (k) {
        (S.form.values[tid] || (S.form.values[tid] = {}))[k] = set[k];
      });
      // a single point means: no ladder to scan
      S.form.sweep = { on: false, field: "", values: "", x: S.form.sweep.x || "", chartMetric: S.form.sweep.chartMetric || "", presetId: id, presetName: p.name };
    } else {
      // whole-series preset: expose the ladder in an EDITABLE sweep field, do NOT run it
      var ladders = (S.templates && S.templates.ladders) || {};
      var ladder = ladders[p.ladder] || ladders.full || {};
      var spec = ladder[tid] || ladder.chat || null;
      if (!spec) { toast("该任务没有可用的压力梯子", "bad"); return; }
      S.form.sweep = {
        on: true, field: spec.field, values: spec.values.join(","),
        x: spec.x || "num_concurrency",
        chartMetric: p.chart_metric || "throughput",
        presetId: id, presetName: p.name
      };
    }
    S.activePreset = id;                 // single-select highlight
    S.form.preset_id = id;
    S.form.preset_name = p.name;
    persistForm(); renderNew();
    toast("已套用「" + p.name + "」的参考参数；下面表单都能改，确认后再点『发起实验』", "good");
  };

  function taskParamsCard(id) {
    var tk = task(id); if (!tk) return "";
    var vals = S.form.values[id] || (S.form.values[id] = defaultsFor(tk));
    var html = '<div class="card"><h2>' + esc(tk.display_name) + ' · 施压档参数</h2><div class="split">';
    html += '<div><h3>连接 / 环境</h3>';
    tk.common_fields.forEach(function (f) { html += fieldHtml(f, vals, id); });
    html += "</div><div><h3>任务参数（推荐值来自单一模板配置）</h3>";
    tk.fields.forEach(function (f) { html += fieldHtml(f, vals, id); });
    html += "</div></div></div>";
    return html;
  }

  function fieldHtml(f, vals, taskId) {
    var v = vals[f.key] === undefined ? f.default : vals[f.key];
    var readonly = f.type === "readonly";
    var input;
    if (f.allow_custom && f.custom_type === "text") {
      // Free text with suggestions: a <datalist> keeps the familiar "click to
      // pick" dropdown (now including Huawei Ascend / Alibaba Hanguang) while
      // letting the 小白 type any card the list has never heard of.
      var lid = "dl-" + taskId + "-" + f.key;
      input = '<input type="text" list="' + attr(lid) + '" data-task="' + attr(taskId) + '" data-key="' + attr(f.key) + '" value="' + attr(v === null || v === undefined ? "" : v) + '" placeholder="可手填任意型号" oninput="GB.setField(this)" ' + (readonly ? "readonly" : "") + ">" +
        '<datalist id="' + attr(lid) + '">' + f.options.map(function (o) {
          return '<option value="' + attr(o.value) + '">' + esc(o.label || o.value) + "</option>";
        }).join("") + "</datalist>" +
        '<div class="hint">↓ 点一下输入框会展开常见型号（含华为昇腾 / 阿里含光 / 海光 / 寒武纪 / 昆仑芯）；列表里没有就直接手填</div>';
    } else if (f.allow_custom) {
      // A free number box (any value, e.g. 批大小 10 或 并发 50) plus the
      // recommended ladder as one-click chips.
      input = '<input type="number" min="1" data-task="' + attr(taskId) + '" data-key="' + attr(f.key) + '" value="' + attr(v) + '" oninput="GB.setField(this)" ' + (readonly ? "readonly" : "") + ">" +
        '<div class="chips">' + f.options.map(function (o) {
          // quote the js args with single quotes: the whole handler lives inside
          // a double-quoted HTML attribute, so JSON.stringify's double quotes
          // would terminate the attribute and leave a dead button.
          var vLit = /^-?\d+(\.\d+)?$/.test(String(o.value)) ? String(o.value) : "'" + String(o.value).replace(/'/g, "\\'") + "'";
          return '<button type="button" class="chip' + (String(o.value) === String(v) ? " on" : "") + '" data-v="' + attr(o.value) + '" title="点一下填入 ' + attr(o.value) + '，也可以直接手填任意数字" onclick="GB.setFieldValue(\'' + attr(taskId) + '\',\'' + attr(f.key) + '\',' + vLit + ')">' + esc(o.short || o.label) + "</button>";
        }).join("") + "</div>" +
        '<div class="hint">↑ 档位按钮点一下就会填进上面的输入框；不想用档位也可以直接手填任意数字</div>';
    } else if (f.options && f.options.length) {
      input = '<select data-task="' + attr(taskId) + '" data-key="' + attr(f.key) + '" onchange="GB.setField(this)" ' + (readonly ? "disabled" : "") + ">" +
        f.options.map(function (o) { return '<option value="' + attr(o.value) + '" ' + (String(o.value) === String(v) ? "selected" : "") + ">" + esc(o.label) + (o.short ? " (" + esc(o.short) + ")" : "") + "</option>"; }).join("") + "</select>";
    } else if (f.type === "number") {
      input = '<input type="number" data-task="' + attr(taskId) + '" data-key="' + attr(f.key) + '" value="' + attr(v) + '" onchange="GB.setField(this)" ' + (readonly ? "readonly" : "") + ">";
    } else {
      input = '<input type="text" data-task="' + attr(taskId) + '" data-key="' + attr(f.key) + '" value="' + attr(v) + '" onchange="GB.setField(this)" ' + (readonly ? "readonly" : "") + ">";
    }
    var plain = f.plain || ((S.templates && S.templates.glossary && S.templates.glossary.field_plain) || {})[f.key] || "";
    return '<label class="fld"><span>' + esc(f.label) + (f.abbrev ? ' <b style="color:var(--accent2)">' + esc(f.abbrev) + "</b>" : "") +
      (f.required ? ' <span style="color:var(--danger)">*</span>' : "") + (readonly ? " · 固定" : "") + "</span>" + input +
      (plain ? '<div class="plain">💡 ' + esc(plain) + "</div>" : "") +
      (f.hint ? '<div class="hint">' + esc(f.hint) + "</div>" : "") + "</label>";
  }

  // quick-pick chip: write a recommended ladder value into the free number box
  GB.setFieldValue = function (tid, k, value) {
    (S.form.values[tid] || (S.form.values[tid] = {}))[k] = value;
    persistForm();
    var box = document.querySelector('input[data-task="' + tid + '"][data-key="' + k + '"]');
    if (box) box.value = value;
    var wrap = box && box.parentNode;
    if (wrap) {
      wrap.querySelectorAll(".chip").forEach(function (c) {
        c.classList.toggle("on", c.getAttribute("data-v") === String(value));
      });
    }
  };

  GB.setField = function (node) {
    var tid = node.getAttribute("data-task"), k = node.getAttribute("data-key");
    var v = node.type === "number" ? Number(node.value) : node.value;
    // a cleared number box must not turn into NaN in the request payload
    if (node.type === "number" && (!node.value || !isFinite(v))) return;
    (S.form.values[tid] || (S.form.values[tid] = {}))[k] = v;
    // manually editing the Tokenizer text box clears any local-bundle selection
    if (k === "model_tokenizer") {
      var ts = $("#tok-select");
      if (ts && String(node.value).indexOf("tokenizer:") !== 0) ts.value = "";
    }
    persistForm();
  };
  GB.selectTask = function (id) {
    // 单选：一次只压一个任务。切换任务时清掉上一个套餐的扫描档位，避免串味。
    if (S.form.task_ids[0] !== id) {
      S.form.sweep = { on: false, field: "", values: "", x: "", chartMetric: "", presetId: "", presetName: "" };
      S.activePreset = null;
      S.form.preset_id = ""; S.form.preset_name = "";
    }
    S.form.task_ids = [id];
    persistForm(); renderNew();
  };
  GB.toggleSweep = function (on) { S.form.sweep.on = on; };

  // -- tokenizer bundles (offline, upload + select + delete) ----------------
  function renderTokenizerWidget() {
    var sel = $("#tok-select"); if (!sel) return;
    var items = (S.templates && S.templates.tokenizers) || [];
    // remember the current choice, and whether any selected task already uses a ref
    var current = sel.value;
    var activeRef = "";
    S.form.task_ids.forEach(function (tid) {
      var v = (S.form.values[tid] || {}).model_tokenizer || "";
      if (v.indexOf("tokenizer:") === 0) activeRef = v;
    });
    sel.innerHTML = '<option value="">— 未选用本地 Tokenizer（手动填 HF ID）—</option>' +
      items.map(function (it) {
        var ref = "tokenizer:" + it.id;
        return '<option value="' + attr(ref) + '">' + esc(it.name) + " · " + it.file_count + " 文件" +
          (it.ready ? "" : " · 缺核心文件") + "</option>";
      }).join("");
    if (activeRef && items.some(function (it) { return "tokenizer:" + it.id === activeRef; })) sel.value = activeRef;
    else if (current && items.some(function (it) { return "tokenizer:" + it.id === current; })) sel.value = current;

    var list = $("#tok-list");
    if (list) {
      if (!items.length) { list.innerHTML = '<div class="small">尚未上传任何本地 Tokenizer。</div>'; }
      else list.innerHTML = items.map(function (it) {
        return '<div class="row tokenizer-list"><div>' +
          '<b>' + esc(it.name) + '</b> <span class="small">' + esc(it.id) + " · " + it.file_count +
          " 文件 · " + (it.size / 1024).toFixed(0) + " KiB" + (it.ready ? "" : " · 缺核心文件") + "</span>" +
          '<div class="small">' + esc((it.files || []).map(function (f) { return f.name; }).join(", ")) + "</div></div>" +
          '<button class="btn sm danger" onclick="GB.deleteTokenizer(\'' + attr(it.id) + '\')">删除</button></div>';
      }).join("");
    }
  }

  GB.applyTokenizer = function () {
    var sel = $("#tok-select"), ref = sel ? sel.value : "";
    var ids = S.form.task_ids.slice();
    ids.forEach(function (tid) {
      (S.form.values[tid] || (S.form.values[tid] = {}))[ "model_tokenizer" ] = ref || "";
    });
    // keep the visible text inputs in sync with the underlying form state
    $$('input[data-key="model_tokenizer"]').forEach(function (inp) { inp.value = ref || ""; });
    persistForm();
    toast(ref ? "已选用本地 Tokenizer（离线加载）" : "已切换为手动填写 HF ID", "good");
  };

  GB.uploadTokenizer = function () {
    var nameIn = $("#tok-name"), filesIn = $("#tok-files"), status = $("#tok-status");
    var files = filesIn && filesIn.files ? Array.prototype.slice.call(filesIn.files) : [];
    if (!files.length) { toast("请先选择 tokenizer 文件", "bad"); if (status) status.textContent = "未选择文件"; return; }
    var fd = new FormData();
    fd.append("name", (nameIn && nameIn.value || "").trim());
    files.forEach(function (f) { fd.append("files", f, f.name); });
    if (status) status.textContent = "上传中…";
    // raw fetch: api() always JSON.stringifies, but multipart needs NO Content-Type
    fetch("/api/tokenizers", { method: "POST", body: fd }).then(function (r) {
      return r.json().then(function (j) { return { ok: r.ok, j: j }; });
    }).then(function (res) {
      if (!res.ok || !res.j.ok) { var msg = (res.j && res.j.error) || "上传失败"; if (status) status.textContent = msg; toast(msg, "bad"); return; }
      var item = res.j.item, ref = "tokenizer:" + item.id;
      // refresh the live tokenizer list from the server, then select the new one
      return api("/api/tokenizers").then(function (r) {
        if (r.ok && S.templates) S.templates.tokenizers = r.items;
        renderTokenizerWidget();
        var sel = $("#tok-select"); if (sel) sel.value = ref;
        GB.applyTokenizer();
        if (status) status.textContent = "已上传并选用：" + item.name;
        toast("已上传并选用本地 Tokenizer: " + item.name, "good");
      });
    }).catch(function (e) { var msg = "上传异常: " + e; if (status) status.textContent = msg; toast(msg, "bad"); });
  };

  GB.deleteTokenizer = function (tid) {
    if (!confirm("删除本地 Tokenizer「" + tid + "」？已发起实验的快照仍保留其引用，重跑需重新上传。")) return;
    api("/api/tokenizers/" + encodeURIComponent(tid), { method: "DELETE" }).then(function (r) {
      if (!r.ok) { toast(r.error || "删除失败", "bad"); return; }
      return api("/api/tokenizers").then(function (q) {
        if (q.ok && S.templates) S.templates.tokenizers = q.items;
        renderTokenizerWidget();
        toast("已删除本地 Tokenizer", "good");
      });
    });
  };

  function fillSweepFields() {
    var sel = $("#sweep-field"); if (!sel) return;
    var tid = S.form.task_ids[0], tk = task(tid);
    var opts = [];
    if (tk) tk.fields.forEach(function (f) { if (f.sweepable) opts.push(f); });
    sel.innerHTML = opts.map(function (f) { return '<option value="' + attr(f.key) + '">' + esc(f.label) + " (" + esc(f.abbrev) + ")</option>"; }).join("");
    if (S.form.sweep.field && opts.some(function (f) { return f.key === S.form.sweep.field; })) sel.value = S.form.sweep.field;
    sel.onchange = function () { S.form.sweep.field = sel.value; };
    var vin = $("#sweep-values"); if (vin) vin.onchange = function () { S.form.sweep.values = vin.value; };
    S.form.sweep.field = sel.value;
  }

  // -- preflight -----------------------------------------------------------
  GB.preflight = function () {
    var base = $("#pf-base").value.trim() || firstVal("api_base");
    var model = $("#pf-model").value.trim() || firstVal("api_model_name");
    var out = $("#pf-result");
    out.textContent = "探活中…";
    api("/api/preflight", { method: "POST", body: { api_base: base, api_model_name: model, secrets: { api_key: ($("#api-key") || {}).value || "" } } })
      .then(function (r) {
        out.innerHTML = (r.ok ? '<span style="color:var(--ok)">✓ ' : '<span style="color:var(--danger)">✗ ') + esc(r.status + " · " + r.message) + "</span>";
        if (r.available) out.innerHTML += '<div class="small">可用模型: ' + esc(r.available.join(", ")) + "</div>";
      }).catch(function (e) { out.textContent = "探活异常: " + e; });
  };
  function firstVal(key) {
    var tid = S.form.task_ids[0];
    return (S.form.values[tid] || {})[key] || "";
  }

  // -- presets -------------------------------------------------------------
  function refreshPresetList() {
    var sel = $("#preset-list"); if (!sel) return;
    var presets = LS.get("presets", {});
    sel.innerHTML = '<option value="">选择已存预设…</option>' + Object.keys(presets).map(function (k) { return '<option value="' + attr(k) + '">' + esc(k) + "</option>"; }).join("");
  }
  GB.savePreset = function () {
    var name = ($("#preset-name") || {}).value.trim();
    if (!name) { toast("请输入预设名称", "bad"); return; }
    var presets = LS.get("presets", {});
    presets[name] = { values: JSON.parse(JSON.stringify(S.form.values)), key: "" }; // never persist API key
    LS.set("presets", presets); refreshPresetList(); toast("预设已存本地(不含 API Key)", "good");
  };
  GB.loadPreset = function () {
    var name = ($("#preset-list") || {}).value; if (!name) return;
    var p = LS.get("presets", {})[name]; if (!p) return;
    S.form.values = JSON.parse(JSON.stringify(p.values)); persistForm(); renderNew(); toast("已载入预设 " + name, "good");
  };
  GB.delPreset = function () {
    var name = ($("#preset-list") || {}).value; if (!name) return;
    var presets = LS.get("presets", {}); delete presets[name]; LS.set("presets", presets); refreshPresetList(); toast("已删除预设");
  };
  GB.saveParamPreset = function () {
    var name = prompt("参数预设名称?"); if (!name) return;
    var pp = LS.get("paramPresets", {}); pp[name] = { task_ids: S.form.task_ids, values: JSON.parse(JSON.stringify(S.form.values)) };
    LS.set("paramPresets", pp); toast("参数预设已存本地", "good");
  };

  // -- refill (反显) -------------------------------------------------------
  GB.refill = {
    history: function () {
      api("/api/experiments?page=1&size=20").then(function (r) {
        var items = r.items || [];
        if (!items.length) { toast("暂无历史实验", "bad"); return; }
        modal("以历史实验为模板新建(反显)", "<p class=small>选择一条，把它的完整参数(含折叠/固定值)填回表单；与重跑不同，反显后可改再发起。</p>" +
          items.map(function (it) { return '<div class="queue-item"><span class="rid">' + esc(it.run_id) + '</span><span>' + esc(it.title) + '</span><button class="btn sm" onclick="GB.applyRefill(\'' + attr(it.run_id) + '\')">反显</button></div>'; }).join(""), true);
      });
    },
    preset: function () {
      var pp = LS.get("paramPresets", {}), keys = Object.keys(pp);
      if (!keys.length) { toast("暂无本地参数预设", "bad"); return; }
      modal("本地参数预设(反显)", keys.map(function (k) { return '<div class="queue-item"><span>' + esc(k) + '</span><button class="btn sm" onclick="GB.applyPresetRefill(\'' + attr(k) + '\')">反显</button></div>'; }).join(""), true);
    },
    cli: function () {
      modal("粘贴 CLI(反显)", '<p class=small>粘贴 <span class="kbd">genai-bench benchmark …</span>，解析出任务与参数填回表单。</p>' +
        '<textarea id="cli-text" style="min-height:120px" placeholder="genai-bench benchmark --task text-to-text --num-concurrency 32 --traffic-scenario D(1024,512) --max-time-per-run 10 ..."></textarea>' +
        '<div class="btnbar" style="margin-top:10px"><button class="btn primary" onclick="GB.parseCli()">解析并反显</button></div>', true);
    },
  };
  GB.applyRefill = function (run_id) {
    api("/api/experiments/" + encodeURIComponent(run_id) + "/params").then(function (p) {
      applyRefillPayload(p, "历史 " + run_id);
    });
  };
  GB.applyPresetRefill = function (name) {
    var pp = LS.get("paramPresets", {})[name]; if (!pp) return;
    applyRefillPayload({ task_id: pp.task_ids[0], task_ids: pp.task_ids, values: pp.values[pp.task_ids[0]] || {}, fixed: {} }, "预设 " + name);
  };
  GB.parseCli = function () {
    var text = ($("#cli-text") || {}).value;
    api("/api/params/parse-cli", { method: "POST", body: { cli: text } }).then(function (r) {
      if (!r.ok) { toast(r.error || "解析失败", "bad"); return; }
      applyRefillPayload({ task_id: r.task_id, task_ids: [r.task_id], values: r.values, fixed: {} }, "CLI");
    });
  };
  function applyRefillPayload(p, label) {
    var tid = p.task_id;
    if (!tid || !task(tid)) { toast("无法识别任务，反显失败", "bad"); return; }
    var before = JSON.parse(JSON.stringify(S.form.values[tid] || {}));
    var after = Object.assign({}, (S.form.values[tid] || {}), p.values || {});
    S.form.values[tid] = after;
    // 单选：反显也只保留一个任务，避免把历史里的多选带回表单
    S.form.task_ids = [tid];
    S.form.sweep = { on: false, field: "", values: "", x: "", chartMetric: "", presetId: "", presetName: "" };
    S.activePreset = null; S.form.preset_id = ""; S.form.preset_name = "";
    var diff = diffHtml(before, after);
    closeModal(); renderNew();
    var box = $("#refill-diff"); if (box) box.innerHTML = '<div style="margin-top:8px"><b>' + esc(label) + " → 反显完成</b>（含折叠/固定值）。改动 diff： " + diff + "</div>";
    toast("参数已反显: " + label, "good");
  }
  function diffHtml(a, b) {
    var keys = Array.from(new Set(Object.keys(a).concat(Object.keys(b))));
    var out = [];
    keys.forEach(function (k) {
      if (String(a[k]) !== String(b[k])) out.push('<span class="diff-del">' + esc(k) + "=" + esc(a[k]) + '</span> → <span class="diff-add">' + esc(k) + "=" + esc(b[k]) + "</span>");
    });
    return out.length ? out.join("；") : '<span class="small">无改动</span>';
  }

  // -- preview -------------------------------------------------------------
  GB.preview = function () {
    var tid = S.form.task_ids[0], tk = task(tid), vals = S.form.values[tid] || {};
    modal("最终参数预览(脱敏)", "<p class=small>以下为将提交的表单值(不含 API Key)。引擎真实命令由后端单一模板生成，模板里的固定项(如迭代维度、批大小=1)会自动补齐。</p>" +
      "<pre class='code'>" + esc(JSON.stringify(vals, null, 2)) + "</pre>", true);
  };

  // -- start ---------------------------------------------------------------
  // One-shot enqueue: ALL calls go into the backend's single serial queue
  // immediately (same group), so 「中断本批」 can cancel every remaining level.
  function startCalls(calls, opts) {
    opts = opts || {};
    var summary = $("#start-summary");
    var chain = Promise.resolve(), results = [], errors = [];
    calls.forEach(function (c) {
      chain = chain.then(function () {
        return api("/api/benchmark/start", { method: "POST", body: {
          task_id: c.task_id, values: c.values, secrets: S.form.secrets || {},
          title: S.form.title || "", group: c.group || "", sweep_key: c.sweep_key, sweep_values: c.sweep_values,
        } }).then(function (r) { results.push(r); }, function (e) { errors.push(e); });
      });
    });
    chain.then(function () {
      if (errors.length) { toast("入队失败: " + (errors[0].error || errors[0]), "bad"); if (summary) summary.textContent = ""; return; }
      var bad = results.filter(function (r) { return !r.ok; });
      if (bad.length) { toast("部分发起失败: " + bad[0].error, "bad"); if (summary) summary.textContent = ""; return; }
      // remember the whole batch locally so the queue bar can show 第几档/共几档
      S.activeBatch = {
        group: opts.group || "",
        ids: results.map(function (r) { return r.run_id; }),
        total: results.length,
        title: opts.presetName ? "套餐 · " + opts.presetName : (calls.length > 1 ? "参数扫描" : "单次实验"),
        presetId: opts.presetId || "",
        chartMetric: opts.chartMetric || "p99",
        x: opts.x || "num_concurrency",
      };
      LS.set("activeBatch", S.activeBatch);
      toast("已入队 " + results.length + " 个实验(严格串行)", "good");
      if (summary) summary.textContent = "run_id: " + S.activeBatch.ids.join(", ");
      var first = results[0].run_id;
      setTimeout(function () { GB.go("/#/dashboard/" + first); }, 700);
    });
  }

  GB.startRuns = function () {
    var ids = S.form.task_ids.slice();
    var title = ($("#run-title") || {}).value.trim(); S.form.title = title;
    var key = (($("#api-key") || {}).value || "").trim();
    S.form.secrets = key ? { api_key: key } : {};
    // MERGE the sweep DOM values into the existing sweep state: a whole-series
    // preset already put chartMetric/x/presetId there; do NOT clobber them.
    var prev = S.form.sweep || {};
    var sweepOn = ($("#sweep-on") || {}).checked;
    var sweepField = ($("#sweep-field") || {}).value || prev.field;
    var sweepVals = ($("#sweep-values") || {}).value.split(",").map(function (s) { return s.trim(); }).filter(Boolean);
    S.form.sweep = {
      on: !!sweepOn, field: sweepField, values: sweepVals.join(","),
      x: prev.x || "num_concurrency",
      chartMetric: prev.chartMetric || "p99",
      presetId: prev.presetId || "", presetName: prev.presetName || ""
    };
    persistForm();

    var group = "";
    var pid = S.form.sweep.presetId;
    if (sweepOn && sweepVals.length > 1) group = (pid ? "preset-" + pid : "sweep-") + "-" + Date.now();
    var calls = [], x_field = S.form.sweep.x || "num_concurrency";
    ids.forEach(function (tid) {
      var tk = task(tid);
      var vals = Object.assign({}, S.form.values[tid] || {});
      if (sweepOn && sweepVals.length > 1) {
        // pick the axis the compare chart can actually draw for this task
        if (tk && tk.task !== "text-to-text") x_field = "batch_size";
        sweepVals.forEach(function (sv) {
          var v = Object.assign({}, vals); v[sweepField] = isNaN(Number(sv)) ? sv : Number(sv);
          calls.push({ task_id: tid, values: v, group: group, sweep_key: sweepField, sweep_values: sweepVals });
        });
      } else {
        calls.push({ task_id: tid, values: vals, group: "" });
      }
    });

    var summary = $("#start-summary");
    if (summary) summary.textContent = "准备发起 " + calls.length + " 个实验(严格串行)…";
    startCalls(calls, {
      group: group,
      chartMetric: S.form.sweep.chartMetric || "p99",
      x: x_field,
      presetId: pid,
      presetName: S.form.sweep.presetName || ""
    });
  };

  // -- 中断本批 (stop the running level, cancel every remaining level) ------
  GB.interruptBatch = function () {
    api("/api/benchmark/queue").then(function (q) {
      S.lastQueue = q;
      var jobs = (q.jobs || []).filter(function (j) {
        return j.status === "running" || j.status === "queued";
      });
      var mine = S.activeBatch ? jobs.filter(function (j) { return j.group === S.activeBatch.group; }) : [];
      // stop() accepts EITHER a running OR a queued job: it cancels every
      // queued sibling and pauses the queue. So even in the brief window where
      // nothing has been picked up yet, stopping a queued level still halts
      // the whole batch and prevents any auto-start.
      var target = mine.filter(function (j) { return j.status === "running"; })[0]
        || (q.running && jobs.filter(function (j) { return j.run_id === q.running; })[0])
        || mine[0]
        || jobs[0];
      if (!target) { toast("当前没有可中断的实验", "bad"); return; }
      return api("/api/benchmark/" + encodeURIComponent(target.run_id) + "/stop", { method: "POST" }).then(function (r) {
        toast(r.ok ? "已中断本批：正在跑的这一档已终止，剩余档全部取消，不会自动开跑" : (r.error || r.message || "中断失败"), r.ok ? "good" : "bad");
        if (r.ok) renderNew();
      });
    }).catch(function (e) { toast("中断失败: " + (e.error || e), "bad"); });
  };

  function batchProgressHtml(q) {
    var b = S.activeBatch;
    if (!b || !b.ids || !b.ids.length) return "";
    var byId = {};
    (q.jobs || []).forEach(function (j) { byId[j.run_id] = j; });
    var done = 0, running = 0, failed = 0, stopped = 0, queued = 0;
    b.ids.forEach(function (id) {
      var st = (byId[id] || {}).status;
      if (st === "done") done++;
      else if (st === "running") running++;
      else if (st === "failed") failed++;
      else if (st === "stopped") stopped++;
      else queued++;
    });
    var finished = done + failed + stopped;
    // 第几档 = how many levels are already settled, plus one while a level is in flight
    var level = Math.min(finished + (running || queued ? 1 : 0), b.total);
    var state = running ? "正在跑第 " + level + " / 共 " + b.total + " 档" :
      (queued ? "第 " + level + " / 共 " + b.total + " 档排队中" : "本批已结束");
    var tail = stopped ? " · 已中断 " + stopped + " 档" : "";
    var left = queued ? " · 还剩 " + queued + " 档" : "";
    return '<div class="batchbar">' +
      '<div><b>' + esc(b.title) + "</b> <span class=\"small\">" + esc(state + tail + left) + "</span></div>" +
      '<div class="mini-progress"><span style="width:' + Math.round(100 * finished / Math.max(1, b.total)) + '%"></span></div>' +
      '<div class="small">完成 ' + done + " · 失败 " + failed + " · 中断 " + stopped + " · 待跑 " + queued + " / 共 " + b.total + " 档</div>" +
      '<div class="btnbar" style="margin-top:8px">' +
      (running || queued ? '<button class="btn sm danger" onclick="GB.interruptBatch()">中断本批</button>' : "") +
      (finished ? '<button class="btn sm primary" onclick="GB.batchKnee()">看拐点图</button>' : "") +
      '<button class="btn sm" onclick="GB.go(\'/#/history?group=' + encodeURIComponent(b.group) + '\')">看本批历史</button>' +
      "</div></div>";
  }

  // Knee chart: x = the axis that was swept, y = the preset's metric.
  GB.batchKnee = function () {
    var b = S.activeBatch;
    if (!b || !b.ids.length) { toast("本批还没有结果", "bad"); return; }
    var metric = b.chartMetric || "p99";
    modal("本批拐点图 · " + esc(b.title) + "（只读，离线渲染）",
      '<p class="small">横轴 = 被扫描的档位（' + esc(b.x) + "），纵轴 = " + esc(metric) + "。拐点即吞吐见顶 / P99 起飞的那一档。</p>" +
      '<div id="chart-box" class="chart-wrap"><canvas id="chart-canvas" width="640" height="360"></canvas></div>' +
      '<div class="btnbar" style="margin-top:10px"><button class="btn sm" onclick="GB.downloadChart()">导出 PNG</button></div>', true);
    var ids = b.ids.filter(function (id) { var j = (S.lastQueue || {}).jobs || []; return j.some(function (x) { return x.run_id === id && x.status !== "stopped" && x.status !== "failed"; }); });
    api("/api/experiments/chart?ids=" + encodeURIComponent((ids.length ? ids : b.ids).join(",")) + "&metric=" + encodeURIComponent(metric) + "&x=" + encodeURIComponent(b.x))
      .then(function (p) { global.GBChart.line($("#chart-canvas"), p, { title: b.title + " · " + (p.label || metric) }); });
  };


  // ============================================================ HISTORY PAGE
  function renderHistory() {
    var h = S.history;
    var html = '<h1>历史结果</h1><p class="sub">跑完的<b>原始结果文件原样保留</b>（引擎产出的文件名/内容都不改）。勾选多条可<b>批量下载 ZIP</b>（含原始结果+汇总表+图表）或<b>批量删除</b>；单条也能单独下载。扫描目录 → 内存排序 → 切片，无数据库；星标跨页置顶，勾选跨页保留。</p>';
    html += '<div class="filters">' +
      '<input type="text" id="f-keyword" placeholder="搜索标题/run_id/标签/模型" value="' + attr(h.keyword) + '">' +
      '<select id="f-task"><option value="">全部任务</option>' + (S.templates ? S.templates.tasks.map(function (t) { return '<option value="' + attr(t.task) + '">' + esc(t.display_name) + "</option>"; }).join("") : "") + "</select>" +
      '<input type="text" id="f-model" placeholder="模型" value="' + attr(h.model) + '">' +
      '<select id="f-status"><option value="">全部状态</option>' + ["queued", "running", "done", "failed", "stopped"].map(function (s) { return '<option value="' + s + '">' + s + "</option>"; }).join("") + "</select>" +
      '<input type="text" id="f-group" placeholder="扫描组" value="' + attr(h.group) + '">' +
      '<label class="chk"><input type="checkbox" id="f-starred"> 仅星标</label>' +
      '<select id="f-size"><option>20</option><option>50</option><option>100</option></select>' +
      '<button class="btn primary" onclick="GB.search()">筛选</button>' +
      '<button class="btn" onclick="GB.resetFilters()">重置</button></div>';
    html += '<div class="bulkbar"><span>已选 <span class="count" id="sel-count">' + S.selected.size + "</span> 条(跨页保留)</span>" +
      '<button class="btn sm" onclick="GB.selectAll()">全选(当前筛选全部)</button>' +
      '<button class="btn sm" onclick="GB.clearSel()">清空</button>' +
      '<button class="btn sm primary" onclick="GB.exportSel(\'zip\')">批量下载 ZIP（原始结果+汇总表+图表）</button>' +
      '<button class="btn sm" onclick="GB.exportSel(\'csv\')">汇总 CSV</button>' +
      '<button class="btn sm" onclick="GB.bulkRerun()">批量重跑(串行)</button>' +
      '<button class="btn sm" onclick="GB.compareSel()">并排对比图</button>' +
      '<button class="btn sm danger" onclick="GB.bulkDelete()">批量删除选中</button></div>';
    html += '<div class="card" id="hist-table"><div class="empty">加载中…</div></div>';
    html += '<div class="pager" id="pager"></div>';
    $("#view").innerHTML = html;
    $("#f-task").value = h.task; $("#f-status").value = h.status; $("#f-size").value = String(h.size); $("#f-starred").checked = h.starred === "1";
    loadHistory();
  }

  function historyQuery(extra) {
    var h = S.history, q = Object.assign({ page: h.page, size: h.size, task: h.task, model: h.model, status: h.status, keyword: h.keyword, group: h.group, starred: h.starred, sort: h.sort }, extra || {});
    return Object.keys(q).map(function (k) { return encodeURIComponent(k) + "=" + encodeURIComponent(q[k] == null ? "" : q[k]); }).join("&");
  }

  function loadHistory() {
    api("/api/experiments?" + historyQuery()).then(function (r) {
      var box = $("#hist-table"); if (!box) return;
      if (!r.items || !r.items.length) { box.innerHTML = '<div class="empty">没有符合条件的实验</div>'; } else {
        var head = "<tr><th></th><th>任务显示名 / run_id</th><th>任务 / 模型</th><th>关键指标</th><th>状态 / 时间</th><th>操作</th></tr>";
        var rows = r.items.map(function (it) { return histRow(it); }).join("");
        box.innerHTML = "<table>" + head + rows + "</table>";
      }
      $("#sel-count").textContent = S.selected.size;
      var pages = Math.max(1, Math.ceil(r.total / r.size));
      $("#pager").innerHTML = '<span>共 ' + r.total + " 条 · 第 " + r.page + "/" + pages + " 页</span>" +
        '<button class="btn sm" ' + (r.page <= 1 ? "disabled" : "") + ' onclick="GB.page(-1)">上一页</button>' +
        '<button class="btn sm" ' + (r.page >= pages ? "disabled" : "") + ' onclick="GB.page(1)">下一页</button>';
    });
  }

  function histRow(it) {
    var sel = S.selected.has(it.run_id);
    var m = it.metrics || {};
    var status = it.status || "unknown";
    var tags = (it.tags || []).map(function (t) { return '<span class="tag">' + esc(t) + "</span>"; }).join("");
    var metrics = '<div class="metric-strip">' +
      '<span class="m">吞吐 <b>' + esc(fmtNum(m.throughput)) + "</b> req/s</span>" +
      '<span class="m">P50 <b>' + esc(fmtNum(m.p50)) + "</b></span>" +
      '<span class="m">P99 <b>' + esc(fmtNum(m.p99)) + "</b></span>" +
      '<span class="m">错误率 <b>' + esc(fmtNum(m.error_rate)) + "</b></span></div>";
    return "<tr>" +
      '<td><input type="checkbox" ' + (sel ? "checked" : "") + ' onchange="GB.toggleSel(\'' + attr(it.run_id) + '\',this.checked)"></td>' +
      '<td><div class="ttl">' + esc(it.title || it.run_id) + "</div>" +
        '<div class="rid" onclick="GB.copy(this)" title="完整 slug: ' + attr(it.slug || "") + '">' + esc(it.run_id) + "</div>" +
        '<div style="margin-top:4px">' + tags + '<span class="star ' + (it.starred ? "on" : "") + '" onclick="GB.star(\'' + attr(it.run_id) + '\',' + (it.starred ? "false" : "true") + ')">' + (it.starred ? "★" : "☆") + "</span>" +
        (it.group ? '<span class="tag">组 ' + esc(it.group) + "</span>" : "") + "</div></td>" +
      '<td class="small">' + esc(it.task || "") + "<br>" + esc(it.model || "") + "</td>" +
      "<td>" + metrics + "</td>" +
      '<td><span class="badge ' + esc(status) + '">' + esc(status) + '</span><div class="small" style="margin-top:4px">' + esc((it.start || "").replace("T", " ").slice(0, 19)) + "</div></td>" +
      '<td><div class="btnbar">' +
        '<a class="btn sm" href="' + attr(it.dashboard_url || ("/#/dashboard/" + it.run_id)) + '">监控</a>' +
        '<button class="btn sm" onclick="GB.chartOne(\'' + attr(it.run_id) + '\')">时序图</button>' +
        '<button class="btn sm" onclick="GB.editRun(\'' + attr(it.run_id) + '\')">编辑</button>' +
        '<button class="btn sm" onclick="GB.rerunRun(\'' + attr(it.run_id) + '\')">重跑</button>' +
        '<button class="btn sm" onclick="GB.newFrom(\'' + attr(it.run_id) + '\')">以此为模板</button>' +
        '<button class="btn sm" onclick="GB.exportOne(\'' + attr(it.run_id) + '\')">下载原始结果</button>' +
        "</div></td></tr>";
  }
  function fmtNum(v) { return (v === null || v === undefined || v === "") ? "-" : (Math.round(Number(v) * 100) / 100); }

  GB.search = function () {
    var h = S.history;
    h.keyword = $("#f-keyword").value.trim(); h.task = $("#f-task").value; h.model = $("#f-model").value.trim();
    h.status = $("#f-status").value; h.group = $("#f-group").value.trim();
    h.starred = $("#f-starred").checked ? "1" : ""; h.size = Number($("#f-size").value); h.page = 1;
    loadHistory();
  };
  GB.resetFilters = function () { S.history = { page: 1, size: 20, task: "", model: "", status: "", keyword: "", group: "", starred: "", sort: "start_desc" }; renderHistory(); };
  GB.page = function (d) { S.history.page = Math.max(1, S.history.page + d); loadHistory(); };
  GB.toggleSel = function (id, on) { on ? S.selected.add(id) : S.selected.delete(id); saveSelected(); $("#sel-count").textContent = S.selected.size; };
  GB.clearSel = function () { S.selected.clear(); saveSelected(); loadHistory(); };
  GB.selectAll = function () {
    api("/api/experiments?" + historyQuery({ page: 1, size: 100 })).then(function (r) {
      var p = 1;
      function add(batch) { batch.items.forEach(function (it) { S.selected.add(it.run_id); }); if (batch.items.length && p * 100 < batch.total) { p++; api("/api/experiments?" + historyQuery({ page: p, size: 100 })).then(add); } else { saveSelected(); loadHistory(); toast("已选中当前筛选全部 " + S.selected.size + " 条", "good"); } }
      add(r);
    });
  };
  GB.star = function (id, on) { api("/api/experiments/" + encodeURIComponent(id), { method: "PATCH", body: { starred: on } }).then(loadHistory); };

  // -- export --------------------------------------------------------------
  function selectedIds() { return Array.from(S.selected); }
  function downloadBlob(blob, disp, fallback) {
    var name = fallback;
    // Prefer the RFC 5987 form: the ASCII `filename=` fallback has the Chinese
    // characters stripped out ("genai-bench_3_20261107-1420.zip").
    if (disp) {
      var m = /filename\*=UTF-8''([^;]+)/i.exec(disp);
      if (m) { try { name = decodeURIComponent(m[1]); } catch (e) { name = m[1]; } }
      else {
        var m2 = /filename="?([^";]+)"?/.exec(disp);
        if (m2) name = m2[1];
      }
    }
    var url = URL.createObjectURL(blob), a = document.createElement("a");
    a.href = url; a.download = name; document.body.appendChild(a); a.click(); document.body.removeChild(a);
    setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
  }
  GB.exportSel = function (fmt) {
    var ids = selectedIds(); if (!ids.length) { toast("请先勾选实验", "bad"); return; }
    var q = "ids=" + encodeURIComponent(ids.join(",")) + "&format=" + fmt + "&include_charts=1";
    api("/api/experiments/export?" + q).then(function (r) {
      if (r._blob) downloadBlob(r._blob, r._disp, "genai-bench-export." + fmt);
      else toast(r.error || "导出失败", "bad");
    });
  };
  GB.exportOne = function (id) {
    api("/api/experiments/export?ids=" + encodeURIComponent(id) + "&format=zip&include_charts=1").then(function (r) {
      if (r._blob) downloadBlob(r._blob, r._disp, id + ".zip"); else toast(r.error || "导出失败", "bad");
    });
  };
  GB.bulkRerun = function () {
    var ids = selectedIds(); if (!ids.length) { toast("请先勾选实验", "bad"); return; }
    if (!confirm("批量重跑 " + ids.length + " 条(严格串行，同一队列)，继续?")) return;
    var chain = Promise.resolve();
    ids.forEach(function (id) { chain = chain.then(function () { return api("/api/experiments/" + encodeURIComponent(id) + "/rerun", { method: "POST", body: {} }); }); });
    chain.then(function () { toast("已入队批量重跑(串行)", "good"); });
  };
  GB.rerunRun = function (id) {
    api("/api/experiments/" + encodeURIComponent(id) + "/rerun", { method: "POST", body: {} }).then(function (r) {
      toast(r.ok ? "已重跑入队: " + r.run_id : (r.error || "重跑失败"), r.ok ? "good" : "bad");
      if (r.ok) setTimeout(function () { GB.go("/#/dashboard/" + r.run_id); }, 500);
    });
  };
  GB.bulkDelete = function () {
    var ids = selectedIds(); if (!ids.length) { toast("请先勾选实验", "bad"); return; }
    if (!confirm("确认删除 " + ids.length + " 条实验目录? 不可恢复(引擎原始结果也会删除)。")) return;
    var chain = Promise.resolve();
    ids.forEach(function (id) { chain = chain.then(function () { return api("/api/experiments/" + encodeURIComponent(id), { method: "DELETE" }); }); });
    chain.then(function () { ids.forEach(function (id) { S.selected.delete(id); }); saveSelected(); loadHistory(); toast("已删除", "good"); });
  };
  GB.newFrom = function (id) {
    api("/api/experiments/" + encodeURIComponent(id) + "/params").then(function (p) { applyRefillPayload(p, "历史 " + id); GB.go("/#/new"); });
  };
  GB.editRun = function (id) {
    api("/api/experiments/" + encodeURIComponent(id)).then(function (m) {
      api("/api/experiments/" + encodeURIComponent(id) + "/params").then(function (p) {
        modal("编辑(装饰信息；run_id/目录/Dashboard 链接不变)",
          '<label class="fld"><span>标题</span><input type="text" id="ed-title" value="' + attr(m.title || "") + '"></label>' +
          '<label class="fld"><span>标签(逗号分隔)</span><input type="text" id="ed-tags" value="' + attr((m.tags || []).join(",")) + '"></label>' +
          '<label class="fld"><span>分组</span><input type="text" id="ed-group" value="' + attr(m.group || "") + '"></label>' +
          '<label class="fld"><span>备注(note.txt)</span><textarea id="ed-note">' + esc(p.note || "") + "</textarea></label>" +
          '<div class="btnbar"><button class="btn primary" onclick="GB.saveEdit(\'' + attr(id) + '\')">保存</button></div>', true);
      });
    });
  };
  GB.saveEdit = function (id) {
    var body = {
      title: $("#ed-title").value, tags: $("#ed-tags").value.split(",").map(function (s) { return s.trim(); }).filter(Boolean),
      group: $("#ed-group").value.trim(), note: $("#ed-note").value,
    };
    api("/api/experiments/" + encodeURIComponent(id), { method: "PATCH", body: body }).then(function (r) {
      closeModal(); if (r.ok) { toast("已保存(run_id 不变)", "good"); loadHistory(); } else toast(r.error, "bad");
    });
  };

  // -- charts --------------------------------------------------------------
  GB.chartOne = function (id) {
    modal("单条时序图(只读，离线渲染)", '<div class="btnbar" id="chart-metric-bar"></div><div id="chart-box" class="chart-wrap"><canvas id="chart-canvas" width="640" height="360"></canvas></div>' +
      '<div class="btnbar" style="margin-top:10px"><button class="btn sm" onclick="GB.downloadChart()">导出 PNG</button></div>' + chartHelpHtml(), true);
    var bar = $("#chart-metric-bar");
    (S.templates.metrics || []).forEach(function (m) {
      var b = document.createElement("button"); b.className = "btn sm" + (m.key === "p99" ? " primary" : ""); b.textContent = m.label;
      b.onclick = function () { $$("#chart-metric-bar .btn").forEach(function (x) { x.classList.remove("primary"); }); b.classList.add("primary"); drawChart([id], m.key); };
      bar.appendChild(b);
    });
    drawChart([id], "p99");
  };
  GB.compareSel = function () {
    var ids = selectedIds(); if (ids.length < 1) { toast("请先勾选至少一条", "bad"); return; }
    modal("多条对比 / 拐点图(只读，离线渲染)", '<div class="btnbar" id="chart-metric-bar"></div>' + xAxisHtml() + '<div id="chart-box" class="chart-wrap"><canvas id="chart-canvas" width="640" height="360"></canvas></div>' +
      '<div class="btnbar" style="margin-top:10px"><button class="btn sm" onclick="GB.downloadChart()">导出 PNG</button></div>' + chartHelpHtml(), true);
    var bar = $("#chart-metric-bar");
    var xsel = $("#chart-x");
    if (xsel) xsel.onchange = function () { drawChart(ids, currentMetric()); };
    (S.templates.metrics || []).forEach(function (m) {
      var b = document.createElement("button"); b.className = "btn sm" + (m.key === "p99" ? " primary" : ""); b.textContent = m.label; b.setAttribute("data-metric", m.key);
      b.onclick = function () { $$("#chart-metric-bar .btn").forEach(function (x) { x.classList.remove("primary"); }); b.classList.add("primary"); drawChart(ids, m.key); };
      bar.appendChild(b);
    });
    drawChart(ids, "p99");
  };
  function currentMetric() {
    var b = $("#chart-metric-bar") && $("#chart-metric-bar").querySelector(".btn.primary");
    return (b && b.getAttribute("data-metric")) || "p99";
  }
  // x axis choices for the compare chart: the swept dimension, plus the
  // combined "documents in flight" axis that makes 批10×并发10 and 批2×并发50
  // land on the same x=100 — that is the axis those two configs are meant to
  // be read against.
  var X_AXES = [
    { key: "auto", label: "自动(扫描维度)" },
    { key: "num_concurrency", label: "并发" },
    { key: "batch_size", label: "批大小" },
    { key: "concurrency_x_batch", label: "总在飞文档数(批×并发)" },
    { key: "index", label: "按勾选顺序" }
  ];
  function xAxisHtml() {
    return '<label class="fld" style="max-width:280px"><span>横轴</span><select id="chart-x">' +
      X_AXES.map(function (a) { return '<option value="' + attr(a.key) + '">' + esc(a.label) + "</option>"; }).join("") +
      "</select></label>";
  }
  function drawChart(ids, metric) {
    var sel = $("#chart-x");
    var xa = sel ? sel.value : "auto";
    api("/api/experiments/chart?ids=" + encodeURIComponent(ids.join(",")) + "&metric=" + encodeURIComponent(metric) + "&x=" + encodeURIComponent(xa))
      .then(function (p) {
        var axisName = xa === "concurrency_x_batch" ? "总在飞文档数" : (p.x_field || xa);
        global.GBChart.line($("#chart-canvas"), p, { title: (ids.length > 1 ? "对比 " : "时序 ") + (p.label || metric) + (ids.length > 1 ? " · 横轴 " + axisName : "") });
      });
  }
  GB.downloadChart = function () { global.GBChart.downloadPNG($("#chart-canvas"), "genai-bench-chart.png"); };

  // ========================================================== DASHBOARD PAGE
  function renderDashboard(run_id) {
    var html = '<h1>实时监控（只读）</h1><p class="sub">上游 genai-bench 的 ui/dashboard.py 是 Rich 终端 Live 面板，没有 HTTP Dashboard；这里是应用内只读监控页，按 run_id 主键定位、读取引擎已持久化的结果文件，绝不重算指标。子进程以 ENABLE_UI=false 无头运行。</p>';
    html += '<div class="card tight"><div class="btnbar"><strong>当前实验</strong><span id="dash-status" class="small">加载中…</span>' +
      '<a class="btn sm" href="/#/history">返回历史</a></div></div>';
    html += '<div class="split"><div class="card"><h2>关键指标(引擎已算好)</h2><div id="dash-metrics"></div></div>' +
      '<div class="card"><h2>串行队列</h2><div id="queue-list"></div></div></div>';
    html += '<div class="card"><h2>时序 / 对比图（离线渲染）</h2><div class="btnbar" id="chart-metric-bar"></div>' +
      '<div id="chart-box" class="chart-wrap"><canvas id="chart-canvas" width="640" height="360"></canvas></div>' +
      '<div class="btnbar" style="margin-top:10px"><button class="btn sm" onclick="GB.downloadChart()">导出 PNG</button>' +
      '<button class="btn sm" onclick="GB.refreshDash()">刷新</button></div></div>';
    $("#view").innerHTML = html;
    S.dashRunId = run_id || null;
    var bar = $("#chart-metric-bar");
    (S.templates.metrics || []).forEach(function (m) {
      var b = document.createElement("button"); b.className = "btn sm" + (m.key === "p99" ? " primary" : ""); b.textContent = m.label;
      b.onclick = function () { $$("#chart-metric-bar .btn").forEach(function (x) { x.classList.remove("primary"); }); b.classList.add("primary"); GB.dashChart(m.key); };
      bar.appendChild(b);
    });
    refreshDashboardStatus(); GB.dashChart("p99");
  }
  function refreshDashboardStatus() {
    var id = S.dashRunId;
    if (!id) {
      api("/api/benchmark/queue").then(function (q) {
        $("#dash-status").innerHTML = q.running ? ('运行中 <span class="rid">' + esc(q.running) + "</span> · 队列 " + q.queued.length) : "队列空闲";
        if (q.running) S.dashRunId = q.running;
      });
    }
    if (!S.dashRunId) { $("#dash-metrics").innerHTML = '<div class="small">暂无运行中的实验。从「新建实验」发起，或到「历史结果」点监控。</div>'; return; }
    api("/api/experiments/" + encodeURIComponent(S.dashRunId)).then(function (m) {
      var mm = m.metrics || {};
      $("#dash-status").innerHTML = '<span class="rid">' + esc(S.dashRunId) + "</span> · <b>" + esc(m.title || "") + "</b> · <span class=\"badge " + esc(m.status || "") + '\">' + esc(m.status || "") + "</span>";
      $("#dash-metrics").innerHTML = '<div class="metric-strip">' + (S.templates.metrics || []).map(function (d) {
        return '<span class="m">' + esc(d.label) + " <b>" + esc(fmtNum(mm[d.key])) + "</b> " + esc(d.unit || "") + "</span>";
      }).join("") + "</div>" + (m.has_results ? "" : '<p class="small" style="margin-top:10px">结果文件尚未写出(实验可能刚启动)。</p>');
    });
  }
  GB.dashChart = function (metric) { if (S.dashRunId) drawChart([S.dashRunId], metric); else toast("暂无可画实验", "bad"); };
  GB.refreshDash = function () { refreshDashboardStatus(); GB.dashChart("p99"); };

  // ------------------------------------------------------------- modal util
  function modal(title, body, wide) {
    $("#modal-root").innerHTML = '<div class="modal-bg" onclick="if(event.target===this)GB.closeModal()"><div class="modal" style="' + (wide ? "max-width:760px" : "max-width:560px") + '">' +
      '<div class="btnbar" style="justify-content:space-between"><h2 style="margin:0">' + esc(title) + '</h2><button class="btn sm" onclick="GB.closeModal()">关闭</button></div>' +
      '<div style="margin-top:14px">' + body + "</div></div></div>";
  }
  GB.closeModal = closeModal;
  function closeModal() { $("#modal-root").innerHTML = ""; }

  // ------------------------------------------------------------------- go
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})(window);
