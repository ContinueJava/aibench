/* genai-bench offline chart library.
 *
 * Vendored into the frontend build output (genai_bench/web/static/chart.js).
 * Pure canvas 2D: ZERO dependencies, ZERO CDN, ZERO runtime network. This is
 * guardrail #12 -- charts must render fully offline in the final image.
 *
 * Public API: window.GBChart.line(canvas, payload, opts)
 *   payload = { mode:"single"|"compare"|"empty", label, unit, series:[{points:[{x,y}]}],
 *               points:[{x,y,title,run_id}] , x_field }
 *   opts = { title, metricLabel }
 */
(function (global) {
  "use strict";

  var CSS = {
    bg: "#ffffff", grid: "#e7ecf3", axis: "#8090a6", text: "#33445c",
    line: "#4c8dff", dot: "#2a63cc", area: "rgba(76,141,255,.10)",
    compare: "#e07a3f", compareDot: "#b85a24",
  };

  function el(id) { return typeof id === "string" ? document.getElementById(id) : id; }

  function niceNum(range, round) {
    var exp = Math.floor(Math.log10(range || 1)), f = (range || 1) / Math.pow(10, exp), nf;
    if (round) nf = f < 1.5 ? 1 : f < 3 ? 2 : f < 7 ? 5 : 10;
    else nf = f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10;
    return nf * Math.pow(10, exp);
  }

  function fmt(v) {
    if (v === null || v === undefined || isNaN(v)) return "-";
    var a = Math.abs(v);
    if (a >= 1e9) return (v / 1e9).toFixed(1) + "G";
    if (a >= 1e6) return (v / 1e6).toFixed(1) + "M";
    if (a >= 1e3) return (v / 1e3).toFixed(1) + "k";
    if (a >= 100) return v.toFixed(0);
    if (a >= 1) return v.toFixed(1);
    if (a >= 0.01) return v.toFixed(2);
    return v.toExponential(1);
  }

  function setup(canvas) {
    var dpr = global.devicePixelRatio || 1;
    var rect = canvas.getBoundingClientRect();
    var w = Math.max(320, Math.round(rect.width || canvas.width || 640));
    var h = Math.max(220, Math.round(rect.height || canvas.height || 340));
    canvas.width = w * dpr; canvas.height = h * dpr;
    canvas.style.width = w + "px"; canvas.style.height = h + "px";
    var ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return { ctx: ctx, w: w, h: h };
  }

  function line(canvas, payload, opts) {
    opts = opts || {};
    canvas = el(canvas);
    if (!canvas) return null;
    var env = setup(canvas), ctx = env.ctx, w = env.w, h = env.h;
    ctx.fillStyle = CSS.bg; ctx.fillRect(0, 0, w, h);
    var title = opts.title || payload.label || "图表";
    ctx.fillStyle = CSS.text; ctx.font = "600 13px sans-serif";
    ctx.fillText(String(title).slice(0, 70), 14, 20);

    var pad = { l: 56, r: 16, t: 34, b: 40 };
    var pw = w - pad.l - pad.r, ph = h - pad.t - pad.b;

    // collect points
    var pts = [];
    if (payload.mode === "compare" || (payload.points && payload.points.length)) {
      (payload.points || []).forEach(function (p) {
        pts.push({ x: Number(p.x), y: Number(p.y), label: p.title || p.run_id });
      });
    } else {
      (payload.series || []).forEach(function (s) {
        (s.points || []).forEach(function (p) { pts.push({ x: Number(p.x), y: Number(p.y) }); });
      });
    }
    pts = pts.filter(function (p) { return isFinite(p.x) && isFinite(p.y); });

    if (!pts.length) {
      ctx.fillStyle = CSS.axis; ctx.font = "13px sans-serif";
      ctx.fillText("暂无数据(实验可能还在运行 / 该指标无逐请求序列)", pad.l, pad.t + ph / 2);
      return { empty: true, canvas: canvas };
    }

    var xs = pts.map(function (p) { return p.x; }), ys = pts.map(function (p) { return p.y; });
    var xmin = Math.min.apply(null, xs), xmax = Math.max.apply(null, xs);
    var ymin = Math.min.apply(null, ys), ymax = Math.max.apply(null, ys);
    if (ymin === ymax) { ymin -= Math.abs(ymin || 1) * 0.1 + 1; ymax += Math.abs(ymax || 1) * 0.1 + 1; }
    var ypad = (ymax - ymin) * 0.12; ymin -= ypad; ymax += ypad;
    if (xmin === xmax) { xmin -= 1; xmax += 1; }
    var step = niceNum((ymax - ymin) / 5, true);
    var y0 = Math.floor(ymin / step) * step, y1 = Math.ceil(ymax / step) * step;

    function X(v) { return pad.l + (v - xmin) / (xmax - xmin || 1) * pw; }
    function Y(v) { return pad.t + ph - (v - y0) / (y1 - y0 || 1) * ph; }

    // grid + y ticks
    ctx.font = "11px sans-serif"; ctx.textAlign = "right"; ctx.textBaseline = "middle";
    for (var v = y0; v <= y1 + 1e-9; v += step) {
      var yy = Y(v);
      ctx.strokeStyle = CSS.grid; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(pad.l, yy); ctx.lineTo(w - pad.r, yy); ctx.stroke();
      ctx.fillStyle = CSS.axis; ctx.fillText(fmt(v), pad.l - 8, yy);
    }
    // axes
    ctx.strokeStyle = CSS.axis; ctx.beginPath();
    ctx.moveTo(pad.l, pad.t); ctx.lineTo(pad.l, pad.t + ph); ctx.lineTo(w - pad.r, pad.t + ph); ctx.stroke();
    // x ticks (max 8)
    ctx.textAlign = "center"; ctx.textBaseline = "top";
    var xticks = [];
    if (payload.mode === "compare") { pts.forEach(function (p) { xticks.push(p.x); }); }
    else {
      var n = Math.min(8, pts.length);
      for (var i = 0; i < n; i++) xticks.push(xmin + (xmax - xmin) * i / (n - 1 || 1));
    }
    var seen = {};
    xticks.forEach(function (v) {
      var key = fmt(v); if (seen[key]) return; seen[key] = 1;
      var xx = X(v);
      ctx.strokeStyle = CSS.grid; ctx.beginPath(); ctx.moveTo(xx, pad.t + ph); ctx.lineTo(xx, pad.t + ph + 4); ctx.stroke();
      ctx.fillStyle = CSS.axis; ctx.fillText(key, xx, pad.t + ph + 7);
    });

    var sorted = pts.slice().sort(function (a, b) { return a.x - b.x; });
    var isCompare = payload.mode === "compare";
    // area fill
    ctx.beginPath(); ctx.moveTo(X(sorted[0].x), pad.t + ph);
    sorted.forEach(function (p) { ctx.lineTo(X(p.x), Y(p.y)); });
    ctx.lineTo(X(sorted[sorted.length - 1].x), pad.t + ph); ctx.closePath();
    ctx.fillStyle = CSS.area; ctx.fill();
    // line
    ctx.beginPath();
    sorted.forEach(function (p, i) { i ? ctx.lineTo(X(p.x), Y(p.y)) : ctx.moveTo(X(p.x), Y(p.y)); });
    ctx.strokeStyle = isCompare ? CSS.compare : CSS.line; ctx.lineWidth = 2; ctx.stroke();
    // dots
    sorted.forEach(function (p) {
      ctx.beginPath(); ctx.arc(X(p.x), Y(p.y), isCompare ? 4 : 2.5, 0, Math.PI * 2);
      ctx.fillStyle = isCompare ? CSS.compareDot : CSS.dot; ctx.fill();
      if (isCompare && p.label) {
        ctx.fillStyle = CSS.text; ctx.font = "10px sans-serif"; ctx.textAlign = "center"; ctx.textBaseline = "bottom";
        ctx.fillText(fmt(p.y), X(p.x), Y(p.y) - 7);
      }
    });

    // x label
    ctx.fillStyle = CSS.axis; ctx.font = "11px sans-serif"; ctx.textAlign = "center";
    var xlabel = isCompare ? ("x: " + (payload.x_field || "num_concurrency")) : "逐请求序号 / 时间近似(只读)";
    ctx.fillText(xlabel, pad.l + pw / 2, h - 12);
    // unit
    if (payload.unit) {
      ctx.save(); ctx.translate(14, pad.t + ph / 2); ctx.rotate(-Math.PI / 2);
      ctx.textAlign = "center"; ctx.fillStyle = CSS.axis; ctx.fillText(payload.unit, 0, 0); ctx.restore();
    }
    return { empty: false, canvas: canvas, points: sorted };
  }

  /** Export the chart canvas as a PNG download (offline, no server). */
  function downloadPNG(canvas, filename) {
    canvas = el(canvas); if (!canvas) return;
    var a = document.createElement("a");
    a.download = filename || "chart.png";
    a.href = canvas.toDataURL("image/png");
    document.body.appendChild(a); a.click(); document.body.removeChild(a);
  }

  global.GBChart = { line: line, downloadPNG: downloadPNG, fmt: fmt };
})(window);
