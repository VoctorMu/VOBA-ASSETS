"use strict";

const COLORS = { accent: "#4da3ff", bull: "#22c38e", bear: "#f05d5e", amd: "#f5b942", div: "#b48cff", text: "#e6edf3",
  muted: "#8b98a8", grid: "#1b2430", bg: "#121821" };

let token = "";
try { token = localStorage.getItem("ds_token") || ""; } catch (_) {}
let selected = null;
let lastStatus = null;
let lastAnalysisKey = "";
let activeTab = "open";

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (n, d = 2) => (n == null || isNaN(n)) ? "–" : Number(n).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d });
const px = (n) => n == null ? "–" : (Math.abs(n) >= 1000 ? fmt(n, 2) : fmt(n, 4));
const when = (t) => t ? new Date(t * 1000).toISOString().slice(0, 16).replace("T", " ") : "–";
const dirTxt = (d) => d > 0 ? "BUY" : "SELL";

async function api(path, opts = {}) {
  const res = await fetch(path, { ...opts, headers: { "Content-Type": "application/json", ...(token ? { Authorization: "Bearer " + token } : {}) } });
  if (res.status === 401) {
    token = prompt("Dashboard token") || "";
    try { localStorage.setItem("ds_token", token); } catch (_) {}
    throw new Error("unauthorised");
  }
  if (!res.ok) throw new Error(path + " " + res.status);
  return res.json();
}

/* ---------- charts ---------- */
const chartOpts = (h) => ({
  height: h,
  layout: { background: { color: COLORS.bg }, textColor: COLORS.muted, fontSize: 11 },
  grid: { vertLines: { color: COLORS.grid }, horzLines: { color: COLORS.grid } },
  rightPriceScale: { borderColor: COLORS.grid, minimumWidth: 78 },
  timeScale: { borderColor: COLORS.grid, timeVisible: true, secondsVisible: false },
  crosshair: { mode: 0 },
});
const chart = LightweightCharts.createChart($("chart"), chartOpts($("chart").clientHeight));
const candles = chart.addCandlestickSeries({ upColor: COLORS.bull, downColor: COLORS.bear, borderVisible: false,
  wickUpColor: COLORS.bull, wickDownColor: COLORS.bear });
const rsiChart = LightweightCharts.createChart($("rsiChart"), { ...chartOpts($("rsiChart").clientHeight),
  rightPriceScale: { borderColor: COLORS.grid, minimumWidth: 78, scaleMargins: { top: 0.1, bottom: 0.1 } } });
const rsiLine = rsiChart.addLineSeries({ color: COLORS.accent, lineWidth: 1.5, priceLineVisible: false, lastValueVisible: true });
[70, 30].forEach((v) => rsiLine.createPriceLine({ price: v, color: COLORS.muted, lineWidth: 1, lineStyle: 2, axisLabelVisible: false }));

let syncing = false;
function sync(from, to) {
  from.timeScale().subscribeVisibleLogicalRangeChange((r) => {
    if (syncing || !r) return;
    syncing = true; to.timeScale().setVisibleLogicalRange(r); syncing = false;
  });
}
sync(chart, rsiChart); sync(rsiChart, chart);
new ResizeObserver(() => {
  chart.applyOptions({ width: $("chart").clientWidth, height: $("chart").clientHeight });
  rsiChart.applyOptions({ width: $("rsiChart").clientWidth });
}).observe($("chart"));

let overlays = [], rsiOverlays = [], priceLines = [];
function clearOverlays() {
  overlays.forEach((s) => chart.removeSeries(s)); overlays = [];
  rsiOverlays.forEach((s) => rsiChart.removeSeries(s)); rsiOverlays = [];
  priceLines.forEach((l) => candles.removePriceLine(l)); priceLines = [];
}
function line(target, store, pts, color, style = 0, width = 2) {
  const data = [];
  [...pts].sort((a, b) => a.time - b.time).forEach((p) => {
    if (data.length && data[data.length - 1].time === p.time) data[data.length - 1].value = p.value;
    else data.push(p);
  });
  if (data.length < 2) return;
  const s = target.addLineSeries({ color, lineWidth: width, lineStyle: style, priceLineVisible: false,
    lastValueVisible: false, crosshairMarkerVisible: false });
  s.setData(data);
  store.push(s);
}

function drawAnalysis(an, rr) {
  clearOverlays();
  candles.setData(an.candles);
  rsiLine.setData(an.rsi);
  const markers = [];

  an.patterns.forEach((p) => {
    const c = p.direction > 0 ? COLORS.bull : COLORS.bear;
    line(chart, overlays, p.points.map((q) => ({ time: q.time, value: q.price })), c, 0, 2);
    line(chart, overlays, p.neckline.map((q) => ({ time: q.time, value: q.price })), COLORS.text, 2, 1);
    markers.push({ time: p.break_time, position: p.direction > 0 ? "belowBar" : "aboveBar", color: c,
      shape: p.direction > 0 ? "arrowUp" : "arrowDown", text: p.name === "double_top" ? "M" : p.name === "double_bottom" ? "W" : (p.direction > 0 ? "iH&S" : "H&S") });
  });

  an.amds.forEach((a) => {
    line(chart, overlays, [{ time: a.start, value: a.high }, { time: a.dist_time, value: a.high }], COLORS.amd, 0, 1);
    line(chart, overlays, [{ time: a.start, value: a.low }, { time: a.dist_time, value: a.low }], COLORS.amd, 0, 1);
    markers.push({ time: a.manip_time, position: a.direction > 0 ? "belowBar" : "aboveBar", color: COLORS.amd,
      shape: "circle", text: "Sweep" });
    markers.push({ time: a.dist_time, position: a.direction > 0 ? "belowBar" : "aboveBar", color: COLORS.amd,
      shape: a.direction > 0 ? "arrowUp" : "arrowDown", text: "AMD" });
  });

  an.divergences.forEach((d) => {
    line(chart, overlays, [{ time: d.p1.time, value: d.p1.price }, { time: d.p2.time, value: d.p2.price }], COLORS.div, 0, 2);
    line(rsiChart, rsiOverlays, [{ time: d.p1.time, value: d.p1.rsi }, { time: d.p2.time, value: d.p2.rsi }], COLORS.div, 0, 2);
  });

  if (an.signal) {
    const s = an.signal, risk = Math.abs(s.close - s.stop), tp = s.close + s.direction * rr * risk;
    priceLines.push(candles.createPriceLine({ price: s.close, color: COLORS.text, lineWidth: 1, lineStyle: 0, title: "Entry" }));
    priceLines.push(candles.createPriceLine({ price: s.stop, color: COLORS.bear, lineWidth: 1, lineStyle: 2, title: "SL" }));
    priceLines.push(candles.createPriceLine({ price: tp, color: COLORS.bull, lineWidth: 1, lineStyle: 2, title: `TP ${rr}R` }));
  }
  candles.setMarkers(markers.sort((a, b) => a.time - b.time));
}

/* ---------- side panel ---------- */
function biasWord(b) { return b > 0 ? "Bullish" : b < 0 ? "Bearish" : "Unclear"; }

function renderChecklist(an, rr) {
  const dir = an.bias_d1 !== 0 && an.bias_d1 === an.bias_w1 ? an.bias_d1 : 0;
  const pats = an.patterns.filter((p) => p.direction === dir);
  const divs = an.divergences.filter((d) => d.direction === dir);
  const amds = an.amds.filter((a) => a.direction === dir);
  const items = [
    [an.bias_w1 !== 0, "Weekly trend", biasWord(an.bias_w1)],
    [an.bias_d1 !== 0, "Daily trend", biasWord(an.bias_d1)],
    [dir !== 0, "Timeframes aligned", dir ? `Only ${dir > 0 ? "buys" : "sells"} allowed` : "Standing aside"],
    [dir !== 0 && (pats.length + divs.length) > 0, "4H pattern or RSI divergence",
      dir ? ([...pats.map((p) => p.label), ...divs.map((d) => d.label)].join(", ") || "None in trend direction yet") : "–"],
    [dir !== 0 && amds.length > 0, "AMD complete", amds.length ? "Range → sweep → close beyond" : (dir ? "Waiting" : "–")],
    [!!an.signal, "4H close confirmation", an.signal ? "Triggered on the last closed candle" : "Not triggered"],
  ];
  $("checklist").innerHTML = items.map(([ok, t, d], i) =>
    `<li class="${ok ? "ok" : "no"}"><span class="mark">${ok ? "✓" : i + 1}</span><div><div class="t">${esc(t)}</div><div class="d">${esc(d)}</div></div></li>`).join("");

  const card = $("signalCard");
  if (!an.signal) { card.classList.add("hidden"); return; }
  const s = an.signal, risk = Math.abs(s.close - s.stop), tp = s.close + s.direction * rr * risk;
  card.classList.remove("hidden");
  card.innerHTML = `<div class="dir ${s.direction > 0 ? "buy" : "sell"}">${dirTxt(s.direction)} ${esc(an.symbol)}</div>
    <dl><dt>Entry (4H close)</dt><dd>${px(s.close)}</dd><dt>Stop loss</dt><dd>${px(s.stop)}</dd>
    <dt>Take profit</dt><dd>${px(tp)}</dd><dt>Reward : risk</dt><dd>1 : ${rr}</dd></dl>
    <div class="d" style="margin-top:8px;color:var(--muted);font-size:12px">${s.reasons.map(esc).join(" · ")}</div>`;
}

function renderRules(st) {
  const r = st.risk;
  $("rules").innerHTML = [
    `<b>${r.risk_per_trade_pct}%</b> of equity risked per trade`,
    `Take profit at <b>${r.reward_risk}×</b> the stop distance`,
    r.breakeven?.enabled ? `Stop moves to <b>break-even at ${r.breakeven.trigger_r}R</b>` : `Break-even <b>off</b>`,
    `No new trades if today could close worse than <b>-${r.daily_cap_pct}%</b>`,
    `Entries only on a <b>4H close</b> after AMD completes`,
    `4H setups must match the <b>Daily + Weekly</b> trend`,
  ].map((x) => `<li>${x}</li>`).join("");
}

/* ---------- top bar + watchlist ---------- */
function renderStatus(st) {
  const cur = st.account.currency;
  $("balance").textContent = `${fmt(st.account.balance)} ${cur}`;
  $("equity").textContent = `${fmt(st.account.equity)} ${cur}`;
  const used = Math.min(100, st.risk.day_loss_pct / st.risk.daily_cap_pct * 100);
  $("capLabel").textContent = `-${st.risk.daily_cap_pct}%`;
  $("dayLoss").textContent = st.risk.day_loss_pct > 0 ? `-${fmt(st.risk.day_loss_pct)}%` : "0.00%";
  $("capFill").style.width = used + "%";
  $("capFill").style.background = used >= 100 ? COLORS.bear : used > 50 ? COLORS.amd : COLORS.bull;

  const badges = [
    st.execution === "mt5" ? `<span class="badge live">MT5 LIVE ORDERS</span>` : `<span class="badge paper">PAPER</span>`,
    `<span class="badge">${st.data_source === "mt5" ? "MT5 data" : "Synthetic data"}</span>`,
    `<span class="badge">${esc(st.account.name)}</span>`,
  ];
  if (st.whatsapp) badges.push(`<span class="badge">WhatsApp ✓</span>`);
  if (st.paused) badges.push(`<span class="badge paused">PAUSED</span>`);
  $("badges").innerHTML = badges.join("");
  $("pauseBtn").textContent = st.paused ? "Resume" : "Pause";

  if (!selected) selected = st.symbols[0]?.symbol;
  const arrow = (b, tf) => `<span class="bias ${b > 0 ? "up" : b < 0 ? "down" : ""}" title="${tf} ${biasWord(b)}">${b > 0 ? "▲" : b < 0 ? "▼" : "–"}</span>`;
  $("watchlist").innerHTML = st.symbols.map((s) => `
    <li data-symbol="${esc(s.symbol)}" class="${s.symbol === selected ? "active" : ""}">
      <div class="wl-top"><span class="wl-name">${esc(s.symbol.replace(" Index", ""))}</span>
        <span class="wl-bias">${s.signal ? '<span class="dot" title="Signal"></span>' : ""}${arrow(s.bias_d1, "Daily")}${arrow(s.bias_w1, "Weekly")}</span></div>
      <div class="wl-status ${s.error ? "err" : ""}">${esc(s.status)}</div>
    </li>`).join("");
  renderRules(st);
}

$("watchlist").addEventListener("click", (e) => {
  const li = e.target.closest("li[data-symbol]");
  if (!li) return;
  selected = li.dataset.symbol; lastAnalysisKey = "";
  if (lastStatus) renderStatus(lastStatus);
  refreshAnalysis();
});

/* ---------- tables ---------- */
function renderTable(data) {
  const t = $("table");
  const cell = (v, cls = "") => `<td class="${cls}">${v}</td>`;
  const pnl = (v) => cell(fmt(v), v >= 0 ? "pos" : "neg");
  let head, rows;
  if (activeTab === "open") {
    head = ["Opened", "Symbol", "Side", "Lots", "Entry", "SL", "TP", "P/L"];
    rows = data.open.map((p) => [cell(when(p.open_time)), cell(esc(p.symbol)), cell(dirTxt(p.direction), p.direction > 0 ? "pos" : "neg"),
      cell(p.volume), cell(px(p.entry)), cell(px(p.sl)), cell(px(p.tp)), pnl(p.profit)]);
  } else if (activeTab === "closed") {
    head = ["Closed", "Symbol", "Side", "Lots", "Entry", "Exit", "Result", "P/L"];
    rows = data.closed.map((c) => [cell(when(c.close_time)), cell(esc(c.symbol)), cell(dirTxt(c.direction), c.direction > 0 ? "pos" : "neg"),
      cell(c.volume), cell(px(c.entry)), cell(px(c.exit)), cell(c.reason.toUpperCase()), pnl(c.profit)]);
  } else {
    head = ["Time", "Symbol", "Side", "Entry", "SL", "TP", "Lots", "Status", "Why"];
    rows = data.map((s) => [cell(when(s.created)), cell(esc(s.symbol)), cell(dirTxt(s.direction), s.direction > 0 ? "pos" : "neg"),
      cell(px(s.entry)), cell(px(s.stop)), cell(px(s.target)), cell(s.volume ?? "–"),
      cell(esc(s.status), s.status === "opened" ? "pos" : "neg"),
      cell(esc(s.detail || s.reasons.slice(1).join(" · ")), "wrap")]);
  }
  t.innerHTML = `<thead><tr>${head.map((h) => `<th>${h}</th>`).join("")}</tr></thead><tbody>${
    rows.length ? rows.map((r) => `<tr>${r.join("")}</tr>`).join("") : `<tr><td class="empty" colspan="${head.length}">Nothing yet</td></tr>`}</tbody>`;
}
document.querySelectorAll(".tab").forEach((b) => b.addEventListener("click", () => {
  document.querySelectorAll(".tab").forEach((x) => x.classList.toggle("active", x === b));
  activeTab = b.dataset.tab; refreshTables();
}));

/* ---------- controls ---------- */
$("pauseBtn").addEventListener("click", async () => {
  await api("/api/control", { method: "POST", body: JSON.stringify({ action: lastStatus?.paused ? "resume" : "pause" }) });
  refreshStatus();
});
$("killBtn").addEventListener("click", async () => {
  if (!confirm("Close ALL bot positions at market and pause the engine?")) return;
  const r = await api("/api/control", { method: "POST", body: JSON.stringify({ action: "close_all" }) });
  alert(`Closed ${r.closed} position(s). Engine paused.`);
  refreshStatus(); refreshTables();
});

/* ---------- polling ---------- */
async function refreshStatus() {
  try { lastStatus = await api("/api/status"); renderStatus(lastStatus); } catch (e) { console.warn(e); }
}
async function refreshAnalysis() {
  if (!selected || !lastStatus) return;
  const sym = lastStatus.symbols.find((s) => s.symbol === selected);
  const key = selected + "|" + (sym?.bar_time ?? "");
  if (key === lastAnalysisKey) return;
  $("chartTitle").textContent = selected;
  try {
    const an = await api("/api/analysis/" + encodeURIComponent(selected));
    const symbolChanged = !lastAnalysisKey.startsWith(selected + "|");
    lastAnalysisKey = key;
    const rr = lastStatus.risk.reward_risk;
    drawAnalysis(an, rr);
    renderChecklist(an, rr);
    if (symbolChanged) {
      const n = an.candles.length;
      chart.timeScale().setVisibleLogicalRange({ from: Math.max(0, n - 120), to: n + 5 });
    }
    $("chartStatus").textContent = an.status;
    $("chartStatus").classList.toggle("signal", !!an.signal);
  } catch (e) {
    $("chartStatus").textContent = "waiting for first analysis…";
  }
}
let posLines = [];
function drawPositions(open) {
  posLines.forEach((l) => candles.removePriceLine(l)); posLines = [];
  open.filter((p) => p.symbol === selected).forEach((p) => {
    const tag = `#${p.ticket} ${dirTxt(p.direction)}`;
    posLines.push(candles.createPriceLine({ price: p.entry, color: COLORS.accent, lineWidth: 1, lineStyle: 0, title: tag }));
    posLines.push(candles.createPriceLine({ price: p.sl, color: COLORS.bear, lineWidth: 1, lineStyle: 2, title: `${tag} SL` }));
    posLines.push(candles.createPriceLine({ price: p.tp, color: COLORS.bull, lineWidth: 1, lineStyle: 2, title: `${tag} TP` }));
  });
}
async function refreshTables() {
  try {
    const trades = await api("/api/trades");
    drawPositions(trades.open);
    renderTable(activeTab === "signals" ? await api("/api/signals") : trades);
  } catch (e) { console.warn(e); }
}
async function tick() {
  await refreshStatus();
  await refreshAnalysis();
  await refreshTables();
}
tick();
setInterval(tick, 3000);
