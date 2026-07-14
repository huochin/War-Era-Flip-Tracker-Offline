"""
WarEra Flip Trading Tracker (GUI)
==================================

A local desktop tool (Tkinter) to pull WarEra trading history for a
Personal / Country / Party / MU entity and compute FIFO flip-trading
profit, with results saved into a per-entity folder that's reused across
runs.

Requirements:
    - Python 3.9+
    - tkinter (bundled with Python on Windows/macOS; on Linux you may need
      `sudo apt install python3-tk` or your distro's equivalent)
    - Optional, only if you want PNG exports of the dashboard:
      `pip install html2image` (also needs Chrome/Chromium installed and
      discoverable on your system -- html2image drives it directly)

Run with:
    python3 warera_flip_tracker.py

Output layout:
    warera_data/<Entity Name>/transactions_cache.json   (accumulating cache)
    warera_data/<Entity Name>/warera_transactions_<ID>.csv   (optional)
    warera_data/<Entity Name>/warera_trading_detail_<ID>.csv
    warera_data/<Entity Name>/warera_flip_profit_<ID>.csv
    warera_data/_registry.json   (ID -> folder name, so repeats reuse the same folder)
    warera_settings.json         (last-used form values, incl. API key; chmod 600)

Security note: warera_settings.json is permission-restricted (owner-only)
but NOT encrypted. Treat it like any other local file containing a secret.
"""

import csv
import json
import os
import queue
import re
import threading
import time
import tkinter as tk
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from datetime import datetime, timedelta, timezone
from tkinter import messagebox, scrolledtext, ttk

BASE_URL = "https://api2.warera.io/trpc"
TRANSACTION_TYPE = "trading"
PAGE_LIMIT = 100
MAX_PAGES = 2000  # safety cap per run; raise if a single entity has more history than this

BASE_DIR = "warera_data"
REGISTRY_FILE = os.path.join(BASE_DIR, "_registry.json")
SETTINGS_FILE = "warera_settings.json"

# Per-entity-type wiring: which API param to query by, which fields on a
# transaction mark "this side belongs to that entity", how to search by
# name, and how to confirm/resolve its display name.
ENTITY_CONFIG = {
    "Personal": {
        "query_param": "userId",
        "buyer_field": "buyerId",
        "seller_field": "sellerId",
        "search_key": "userIds",
        "confirm_endpoint": "user.getUserLite",
        "confirm_param": "userId",
        "name_field": "username",
    },
    "Country": {
        "query_param": "countryId",
        "buyer_field": "buyerCountryId",
        "seller_field": "sellerCountryId",
        "search_key": "countryIds",
        "confirm_endpoint": "country.getCountryById",
        "confirm_param": "countryId",
        "name_field": "name",
    },
    "Party": {
        "query_param": "partyId",
        "buyer_field": "buyerPartyId",
        "seller_field": "sellerPartyId",
        "search_key": "partyIds",
        "confirm_endpoint": "party.getById",
        "confirm_param": "partyId",
        "name_field": "name",
    },
    "MU": {
        "query_param": "muId",
        "buyer_field": "buyerMuId",
        "seller_field": "sellerMuId",
        "search_key": "muIds",
        "confirm_endpoint": "mu.getById",
        "confirm_param": "muId",
        "name_field": "name",
    },
}

# Used to detect ANY entity tag on a given side, regardless of which entity
# we're currently tracking -- lets a counterparty be labeled even when it's
# not the entity this run is focused on.
SIDE_ENTITY_TAGS = [
    ("buyerCountryId", "sellerCountryId", "Country"),
    ("buyerMuId", "sellerMuId", "MU"),
    ("buyerPartyId", "sellerPartyId", "Party"),
]

DASHBOARD_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Warera Trading Ledger</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600;700&display=swap');

:root{
  --bg: #0a0e0c;
  --panel: #111613;
  --panel-2: #161c18;
  --line: #23302a;
  --ink: #e7ece8;
  --ink-dim: #8ca396;
  --ink-faint: #526059;
  --profit: #4ade80;
  --profit-dim: #245c3d;
  --loss: #f2765c;
  --buy: #e8a33d;
  --sell: #4ade80;
  --accent: #7dd3c0;
  --surplus: #c99bf0;
}
*{box-sizing:border-box; margin:0; padding:0;}
body{
  background: var(--bg);
  color: var(--ink);
  font-family: 'Space Grotesk', sans-serif;
  padding: 28px 20px 60px;
  min-width: 320px;
}
.wrap{ max-width: 1180px; margin: 0 auto; }

/* ticker header */
.ticker{
  border: 1px solid var(--line);
  background: linear-gradient(180deg, var(--panel-2), var(--panel));
  border-radius: 10px;
  padding: 22px 26px;
  margin-bottom: 22px;
  position: relative;
  overflow: hidden;
}
.ticker::before{
  content:"";
  position:absolute; inset:0;
  background: repeating-linear-gradient(90deg, transparent, transparent 39px, rgba(125,211,192,0.035) 39px, rgba(125,211,192,0.035) 40px);
  pointer-events:none;
}
.eyebrow{
  font-family:'JetBrains Mono', monospace;
  font-size: 10.5px;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--ink-faint);
  display:flex; align-items:center; gap:8px;
}
.eyebrow .dot{ width:6px; height:6px; border-radius:50%; background: var(--profit); box-shadow:0 0 8px var(--profit); }

.entity-header{ display:flex; align-items:baseline; flex-wrap:wrap; gap: 12px; margin-top: 10px; position: relative; }
.entity-name{ font-size: clamp(26px, 4.6vw, 38px); font-weight: 700; letter-spacing: -0.01em; color: var(--ink); }
.pill{
  font-family:'JetBrains Mono', monospace; font-size: 11px; font-weight: 600; letter-spacing: 0.04em;
  text-transform: uppercase; padding: 5px 10px; border-radius: 20px;
  border: 1px solid var(--line); background: var(--panel-2); color: var(--ink-dim); white-space: nowrap;
}
.pill.type{ color: var(--accent); border-color: rgba(125,211,192,0.35); background: rgba(125,211,192,0.08); }
.pill.date .cal{ opacity:.6; margin-right:4px; }

p.tagline{ color: var(--ink-faint); margin-top: 10px; font-size: 12.5px; max-width: 640px; position: relative; }

.ticker-row{
  display:flex; flex-wrap:wrap; gap: 28px;
  margin-top: 20px;
  position: relative;
}
.stat{ min-width: 150px; }
.stat .label{ font-family:'JetBrains Mono',monospace; font-size:10.5px; color:var(--ink-faint); text-transform:uppercase; letter-spacing:.08em; }
.stat .value{ font-family:'JetBrains Mono',monospace; font-size: 22px; font-weight:700; margin-top:4px; }
.stat .value.profit{ color: var(--profit); }
.stat .value.loss{ color: var(--loss); }
.stat .value.buy{ color: var(--buy); }
.stat .sub{ font-size: 11px; color: var(--ink-dim); margin-top:2px; }
.stat .subline{ font-family:'JetBrains Mono',monospace; font-size: 11.5px; margin-top:4px; }
.stat .subline.open{ color: var(--accent); }
.stat .subline.surplus{ color: var(--surplus); }

.section-label{
  font-family:'JetBrains Mono', monospace; font-size: 11px; letter-spacing: 0.1em; text-transform: uppercase;
  color: var(--ink-faint); margin: 26px 2px 10px; display:flex; align-items:center; gap:10px;
}
.section-label::after{ content:""; flex:1; height:1px; background: var(--line); }
.section-label:first-of-type{ margin-top: 6px; }

/* grid */
.grid{ display:grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom:16px; align-items: stretch; }
.grid.full{ grid-template-columns: 1fr; }
@media (max-width: 860px){ .grid{ grid-template-columns: 1fr; } }

.card{
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 10px;
  padding: 18px 18px 10px;
  display:flex; flex-direction:column;
}
.card-head{ display:flex; align-items:flex-start; justify-content:space-between; gap:10px; }
.card h2{
  font-size: 14px;
  font-weight: 600;
  letter-spacing: 0.01em;
  margin-bottom: 2px;
}
.card p.desc{
  font-size: 12px;
  color: var(--ink-dim);
  margin-bottom: 12px;
}
.chart-box{ position:relative; width:100%; margin-top:auto; }
.chart-box.h280{ height:280px; }
.chart-box.h240{ height:240px; }
.chart-box.h220{ height:220px; }

/* toggle pills for chart granularity */
.toggle-group{ display:flex; gap:4px; background:var(--panel-2); border:1px solid var(--line); border-radius:7px; padding:3px; flex-shrink:0; }
.toggle-btn{
  font-family:'JetBrains Mono', monospace; font-size:10.5px; letter-spacing:.03em; text-transform:uppercase;
  border:none; background:transparent; color: var(--ink-faint); padding:5px 10px; border-radius:5px; cursor:pointer;
}
.toggle-btn.active{ background: var(--panel); color: var(--accent); }

/* legend badges */
.legend-row{ display:flex; gap:14px; flex-wrap:wrap; margin-bottom:10px; font-family:'JetBrains Mono',monospace; font-size:11px; color:var(--ink-dim); }
.legend-row .sw{ display:inline-block; width:9px; height:9px; border-radius:2px; margin-right:5px; vertical-align:middle; }

/* tables */
.postable{ width:100%; border-collapse:collapse; font-family:'JetBrains Mono',monospace; font-size:12.5px; }
.postable th{ text-align:left; color:var(--ink-faint); font-weight:500; font-size:10.5px; text-transform:uppercase; letter-spacing:.06em; padding:6px 8px; border-bottom:1px solid var(--line); }
.postable td{ padding:8px; border-bottom:1px solid rgba(35,48,42,0.5); }
.postable tr:last-child td{ border-bottom:none; }
.postable tr.totals-row td{ border-top: 1px solid var(--line); border-bottom:none; font-weight:700; color: var(--ink); padding-top:10px; }
.item-tag{ display:inline-block; padding:2px 7px; border-radius:4px; background:var(--panel-2); border:1px solid var(--line); font-size:11.5px; }

.footnote{
  font-family:'JetBrains Mono', monospace;
  font-size: 11px;
  color: var(--ink-faint);
  text-align:center;
  margin-top: 22px;
  line-height:1.6;
}
.footnote b{ color: var(--ink-dim); }
</style>
</head>
<body>
<div class="wrap">

  <div class="ticker">
    <div class="eyebrow"><span class="dot"></span>WARERA TRADE LEDGER</div>
    <div class="entity-header">
      <span class="entity-name">__ENTITY_NAME__</span>
      <span class="pill type">__ENTITY_TYPE__</span>
      <span class="pill date" id="dateRange"><span class="cal">&#128197;</span></span>
    </div>
    <p class="tagline">Realised profit is small next to the money moved and here's where it actually comes from.</p>
    <div class="ticker-row" id="ticker-stats"></div>
  </div>

  <div class="section-label">Trading activity</div>
  <div class="grid">
    <div class="card">
      <div class="card-head">
        <div>
          <h2>Weekly money in vs. out</h2>
          <p class="desc">Total spent buying vs. total received selling, per week. The gap between the bars is that week's profit or loss.</p>
        </div>
      </div>
      <div class="chart-box h240"><canvas id="flowChart"></canvas></div>
    </div>
    <div class="card">
      <div class="card-head">
        <div>
          <h2>Trading activity per week</h2>
          <p class="desc">Number of fills per week, shows when trading ramped up.</p>
        </div>
      </div>
      <div class="chart-box h240"><canvas id="activityChart"></canvas></div>
    </div>
  </div>

  <div class="section-label">Profit results</div>
  <div class="grid">
    <div class="card">
      <div class="card-head">
        <div>
          <h2>Cumulative realised profit</h2>
          <p class="desc" id="cumDesc">Running total of profit locked in each time a buy was matched against a later sell (FIFO).</p>
        </div>
        <div class="toggle-group" id="cumToggle">
          <button class="toggle-btn" data-mode="daily">Daily</button>
          <button class="toggle-btn" data-mode="weekly">Weekly</button>
        </div>
      </div>
      <div class="chart-box h240"><canvas id="cumChart"></canvas></div>
    </div>
    <div class="card">
      <div class="card-head">
        <div>
          <h2>Margin % by item</h2>
          <p class="desc">Profit as a share of cost, highest margin first.</p>
        </div>
      </div>
      <div class="chart-box h240"><canvas id="marginChart"></canvas></div>
    </div>
  </div>

  <div class="grid full">
    <div class="card">
      <div class="card-head">
        <div>
          <h2>Realised profit by item</h2>
          <p class="desc">Scale is logarithmic so smaller items stay visible next to the biggest movers.</p>
        </div>
      </div>
      <div class="chart-box h280"><canvas id="profitChart"></canvas></div>
    </div>
  </div>

  <div class="section-label">Holdings &amp; surplus</div>
  <div class="grid full">
    <div class="card">
      <div class="card-head">
        <div>
          <h2>Still holding (bought, not yet sold)</h2>
          <p class="desc">Open inventory at cost basis, not yet realised as profit or loss.</p>
        </div>
      </div>
      <table class="postable" id="openTable">
        <thead><tr><th>Item</th><th>Qty</th><th>Cost value</th></tr></thead>
        <tbody></tbody>
        <tfoot></tfoot>
      </table>
    </div>
  </div>
  <div class="grid full">
    <div class="card">
      <div class="card-head">
        <div>
          <h2>Produce surplus (sold without a buying record)</h2>
          <p class="desc">Goods sold that this ledger never saw a matching purchase for, most likely made or gathered rather than bought, so there's no cost basis and no flip profit to compute.</p>
        </div>
      </div>
      <table class="postable" id="surplusTable">
        <thead><tr><th>Item</th><th>Qty sold</th><th>Money received</th></tr></thead>
        <tbody></tbody>
        <tfoot></tfoot>
      </table>
    </div>
  </div>

  <div class="footnote">
    Search window: <b id="fn-search-window"></b><br>
    Data found: <b id="fn-range"></b> &middot; <b id="fn-trades"></b> fills matched__FOOTNOTE_EXTRA__ &middot; unmatched sells mean those goods were acquired outside this trade log, so their profit can't be computed here.
  </div>

</div>

<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/chartjs-plugin-datalabels/2.2.0/chartjs-plugin-datalabels.min.js"></script>
<script>
Chart.register(ChartDataLabels);

const DATA = __DATA_JSON__;

const CSS = getComputedStyle(document.documentElement);
const col = (name) => CSS.getPropertyValue(name).trim();
Chart.defaults.font.family = "'JetBrains Mono', monospace";
Chart.defaults.color = col('--ink-dim');
Chart.defaults.font.size = 11;
Chart.defaults.plugins.datalabels.display = false; // opt in per-chart below
if (navigator.webdriver) {
  Chart.defaults.animation = false;
}

// ---- currency: this game's unit is BTC, prefixed like a ticker symbol ----
function fmt(n, digits=0){
  return Number(n).toLocaleString('en-US', {minimumFractionDigits:digits, maximumFractionDigits:digits});
}
function btc(n, digits=0){
  const sign = n < 0 ? '-' : '';
  return sign + '\u20bf' + fmt(Math.abs(n), digits);
}
function shortDay(d){
  const dt = new Date(d+'T00:00:00Z');
  return dt.toLocaleDateString('en-US', {month:'short', day:'numeric', timeZone:'UTC'});
}
function shortWeek(w){
  const d = new Date(w+'T00:00:00Z');
  return d.toLocaleDateString('en-US', {month:'short', day:'numeric', timeZone:'UTC'});
}
const labelBg = { backgroundColor: 'rgba(10,14,12,0.72)', borderRadius: 4, padding: {top:2,bottom:2,left:5,right:5} };

// ---- ticker stats ----
document.getElementById('dateRange').innerHTML = '<span class="cal">&#128197;</span>' + DATA.totals.date_min + ' \u2192 ' + DATA.totals.date_max;

const t = DATA.totals;
const profitPositive = t.total_profit >= 0;
const stats = [
  {label:'Realised profit', value: btc(t.total_profit,2), cls: profitPositive?'profit':'loss', sub:`${t.n_trades.toLocaleString()} fills`},
  {label:'Total bought', value: btc(t.total_buy,0), cls:'buy', sub:'money spent acquiring goods',
   subline: `- ${btc(t.open_cost_total,2)} still held at cost`, subclass:'open'},
  {label:'Total sold', value: btc(t.total_sell,0), cls:'', sub:'money received from sales',
   subline: `- ${btc(t.surplus_revenue_total,2)} from produce surplus`, subclass:'surplus'},
  {label:'Margin on turnover', value: (t.total_buy ? (t.total_profit/t.total_buy*100).toFixed(2) : '0.00')+'%', cls:'', sub:'profit \u00f7 money bought with'},
];
document.getElementById('ticker-stats').innerHTML = stats.map(s => `
  <div class="stat">
    <div class="label">${s.label}</div>
    <div class="value ${s.cls}">${s.value}</div>
    <div class="sub">${s.sub}</div>
    ${s.subline ? `<div class="subline ${s.subclass}">${s.subline}</div>` : ''}
  </div>`).join('');

document.getElementById('fn-range').textContent = `${t.date_min} \u2192 ${t.date_max}`;
document.getElementById('fn-search-window').textContent = t.search_window;
document.getElementById('fn-trades').textContent = t.n_trades.toLocaleString();

// ---- cumulative profit chart: daily/weekly toggle ----
let cumChartInstance = null;
function renderCumChart(mode){
  const rows = mode === 'daily' ? DATA.daily_profit : DATA.weekly_profit;
  const labelFn = mode === 'daily' ? (r => shortDay(r.day)) : (r => shortWeek(r.week));
  document.getElementById('cumDesc').textContent = mode === 'daily'
    ? 'Running total of profit locked in each time a buy was matched against a later sell (FIFO), day by day.'
    : 'Running total of profit locked in each time a buy was matched against a later sell (FIFO), week by week.';
  document.querySelectorAll('#cumToggle .toggle-btn').forEach(b => b.classList.toggle('active', b.dataset.mode === mode));
  if (cumChartInstance) cumChartInstance.destroy();
  cumChartInstance = new Chart(document.getElementById('cumChart'), {
    type: 'line',
    data: {
      labels: rows.map(labelFn),
      datasets: [{
        label: 'Cumulative profit',
        data: rows.map(r => r.cumulative),
        borderColor: col('--profit'),
        backgroundColor: 'rgba(74,222,128,0.12)',
        fill: true,
        tension: 0.25,
        pointRadius: 0,
        pointHoverRadius: 4,
        borderWidth: 2,
      }]
    },
    options: {
      interaction: {
        mode: 'index',
        intersect: false
      },
      responsive:true, maintainAspectRatio:false,
      plugins:{ legend:{display:false}, datalabels:{display:false},
        tooltip:{ callbacks:{ label: (c)=>' '+btc(c.parsed.y,2) } } },
      scales:{
        x:{ grid:{ color: col('--line') }, ticks:{ maxRotation:0, autoSkip:true, maxTicksLimit: mode==='daily'?8:7 } },
        y:{ grid:{ color: col('--line') }, ticks:{ callback:(v)=>btc(v) } }
      }
    }
  });
}
document.getElementById('cumToggle').addEventListener('click', (e) => {
  const btn = e.target.closest('.toggle-btn'); if (!btn) return;
  renderCumChart(btn.dataset.mode);
});
renderCumChart(DATA.totals.default_granularity || (DATA.totals.span_days < 14 ? 'daily' : 'weekly'));

// ---- weekly flow chart ----
new Chart(document.getElementById('flowChart'), {
  type: 'bar',
  data: {
    labels: DATA.weekly_flow.map(d => shortWeek(d.week)),
    datasets: [
      { label:'Bought', data: DATA.weekly_flow.map(d=>d.Buy), backgroundColor: col('--buy') },
      { label:'Sold', data: DATA.weekly_flow.map(d=>d.Sell), backgroundColor: col('--sell') },
    ]
  },
  options:{
    responsive:true, maintainAspectRatio:false,
    plugins:{ legend:{ position:'top', align:'end', labels:{ boxWidth:9, boxHeight:9, padding:12 } },
      tooltip:{ callbacks:{ label:(c)=>` ${c.dataset.label}: ${btc(c.parsed.y,0)}` } },
      datalabels:{ display: (ctx)=> ctx.dataset.data.length <= 12, anchor:'end', align:'top', color: col('--ink'),
        font:{size:9}, ...labelBg, formatter:(v)=> v>=1000 ? btc(v/1000,1)+'k' : btc(v,0) } },
    scales:{
      x:{ grid:{display:false}, ticks:{ maxRotation:0, autoSkip:true, maxTicksLimit:7 } },
      y:{ grid:{ color: col('--line') }, ticks:{ callback:(v)=>'\u20bf'+fmt(v/1000)+'k' } }
    }
  }
});

// ---- profit by item (log scale) ----
const itemsSorted = DATA.item_summary.filter(d => Math.abs(d.profit) > 0 || d.matched_qty > 0)
  .sort((a,b) => b.profit - a.profit);
new Chart(document.getElementById('profitChart'), {
  type: 'bar',
  data: {
    labels: itemsSorted.map(d => d.itemCode),
    datasets: [{
      label: 'Realised profit',
      data: itemsSorted.map(d => Math.max(d.profit, 0.01)),
      backgroundColor: itemsSorted.map((d,i) => i < 2 ? col('--profit') : 'rgba(125,211,192,0.55)'),
      borderRadius: 3,
      barThickness: 22,
    }]
  },
  options:{
    responsive:true, maintainAspectRatio:false,
    layout: {
      padding: {
        top: 25 // 👈 Adds 25px of breathing room at the top so labels don't clip
      }
    },
    plugins:{ legend:{display:false},
      tooltip:{ callbacks:{
        label:(c)=>` ${btc(itemsSorted[c.dataIndex].profit,3)} profit on ${fmt(itemsSorted[c.dataIndex].matched_qty)} matched units`
      } },
      datalabels:{ display:true, anchor:'end', align:'top', color: col('--ink'), font:{size:9},
        ...labelBg, formatter:(v,ctx)=> btc(itemsSorted[ctx.dataIndex].profit,2) } },
    scales:{
      x:{ grid:{display:false} },
      y:{ type:'logarithmic', grid:{ color: col('--line') }, ticks:{ callback:(v)=>{
          const s = v.toString();
          if (['0.01','0.1','1','10','100','1000','10000'].includes(s)) return '\u20bf'+v;
          return '';
        } } }
    }
  }
});

// ---- margin % chart ----
const marginSorted = [...DATA.item_summary].filter(d => d.matched_qty > 0).sort((a,b)=>b.margin_pct-a.margin_pct);
new Chart(document.getElementById('marginChart'), {
  type: 'bar',
  data: {
    labels: marginSorted.map(d=>d.itemCode),
    datasets: [{
      label: 'Margin %',
      data: marginSorted.map(d=>d.margin_pct),
      backgroundColor: col('--accent'),
      borderRadius: 3,
    }]
  },
  options:{
    indexAxis:'y',
    responsive:true, maintainAspectRatio:false,
    layout: {
      padding: { left: 20, right: 35 } // 👈 Prevents names and % from clipping
    },
    plugins:{ legend:{display:false},
      tooltip:{ callbacks:{ label:(c)=>` ${c.parsed.x}% margin` } },
      datalabels:{ display:true, anchor:'end', align:'right', color: col('--ink'), font:{size:9},
        ...labelBg, formatter:(v)=> v+'%' } },
    scales:{
      x:{ grid:{ color: col('--line') }, ticks:{ callback:(v)=>v+'%' } },
      y:{ 
        grid:{display:false}, 
        ticks: { 
          autoSkip: false,
          font: {
            size: 10 // 👈 Locks the Y-axis text exactly at 10px so it won't auto-adjust
          } // 👈 Forces all items to display
        }
      }
    }
  }
});

// ---- activity chart ----
new Chart(document.getElementById('activityChart'), {
  type:'bar',
  data:{
    labels: DATA.weekly_flow.map(d=>shortWeek(d.week)),
    datasets:[{ label:'Fills', data: DATA.weekly_flow.map(d=>d.count), backgroundColor: 'rgba(125,211,192,0.6)', borderRadius:3 }]
  },
  options:{
    responsive:true, maintainAspectRatio:false,
    plugins:{ legend:{display:false}, tooltip:{ callbacks:{ label:(c)=>` ${fmt(c.parsed.y)} fills` } },
      datalabels:{ display: (ctx)=> ctx.dataset.data.length <= 12, anchor:'end', align:'top', color: col('--ink'), font:{size:9},
        ...labelBg, formatter:(v)=> fmt(v) } },
    scales:{
      x:{ grid:{display:false}, ticks:{ maxRotation:0, autoSkip:true, maxTicksLimit:7 } },
      y:{ grid:{ color: col('--line') } }
    }
  }
});

// ---- still holding table (with totals row) ----
const openTotal = DATA.open_positions.reduce((s,p)=>s+p.cost_value, 0);
document.querySelector('#openTable tbody').innerHTML = DATA.open_positions.map(p => `
  <tr>
    <td><span class="item-tag">${p.item}</span></td>
    <td>${fmt(p.qty)}</td>
    <td>${btc(p.cost_value,2)}</td>
  </tr>`).join('');
document.querySelector('#openTable tfoot').innerHTML = `<tr class="totals-row"><td>Total</td><td></td><td>${btc(openTotal,2)}</td></tr>`;

// ---- produce surplus table (with totals row) ----
const surplusTotal = DATA.produce_surplus.reduce((s,p)=>s+p.revenue, 0);
document.querySelector('#surplusTable tbody').innerHTML = DATA.produce_surplus.map(p => `
  <tr>
    <td><span class="item-tag">${p.item}</span></td>
    <td>${fmt(p.qty)}</td>
    <td>${btc(p.revenue,2)}</td>
  </tr>`).join('');
document.querySelector('#surplusTable tfoot').innerHTML = `<tr class="totals-row"><td>Total</td><td></td><td>${btc(surplusTotal,2)}</td></tr>`;
</script>
</body>
</html>
"""

KNOWN_TRADING_KEYS = {
    "_id", "money", "itemCode", "quantity", "sellerId", "buyerId",
    "transactionType", "offerCreatedAt", "createdAt", "updatedAt", "__v",
    "buyerCountryId", "buyerMuId", "buyerPartyId",
    "sellerCountryId", "sellerMuId", "sellerPartyId",
}


# ----------------------------------------------------------------------------
# Generic file / string helpers
# ----------------------------------------------------------------------------

def load_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path: str, data):
    dirname = os.path.dirname(path)
    if dirname:
        os.makedirs(dirname, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def sanitize_filename(name: str) -> str:
    name = (name or "").strip()
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = re.sub(r"\s+", " ", name)
    return name[:80] if name else "unknown"


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def parse_user_datetime(s: str):
    """Accept 'YYYY-MM-DD[ HH:MM[:SS]]' (assumed UTC) or raw ISO-8601.
    Returns a tz-aware datetime, or None if the input is blank."""
    s = (s or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        return parse_iso(s)
    except Exception:
        raise ValueError(f"Could not parse '{s}'. Use YYYY-MM-DD HH:MM:SS (UTC), or leave blank.")


# ----------------------------------------------------------------------------
# API layer
# ----------------------------------------------------------------------------

def call(endpoint: str, params: dict, api_key: str, max_retries: int = 4) -> dict:
    """Call the WarEra tRPC API with retries + backoff on transient failures."""
    url = f"{BASE_URL}/{endpoint}?input={urllib.parse.quote(json.dumps(params))}"
    last_err = None
    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(url, headers={
                "x-api-key": api_key,
                "User-Agent": "Mozilla/5.0",
            })
            with urllib.request.urlopen(req, timeout=20) as resp:
                body = json.loads(resp.read())
            if "error" in body:
                raise RuntimeError(f"API error: {body['error']}")
            return body["result"]["data"]
        except Exception as e:
            last_err = e
            if attempt < max_retries - 1:
                time.sleep(1.5 ** attempt)
    raise RuntimeError(f"Request to {endpoint} failed after {max_retries} attempts: {last_err}")


class EntityNotFoundError(RuntimeError):
    """Search/lookup succeeded but no matching entity was confirmed."""


class EntitySearchFailedError(RuntimeError):
    """The API itself couldn't be reached/authenticated -- likely a bad key or outage."""


class EntityResolutionCancelled(Exception):
    """User backed out of the candidate-confirmation loop."""


def search_entity_candidates(name: str, entity_type: str, api_key: str) -> list:
    """Return EVERY candidate ID from search.searchAnything for the given
    entity type (not just the first), so the caller can step through them
    in order until the user confirms the right one."""
    cfg = ENTITY_CONFIG[entity_type]
    try:
        data = call("search.searchAnything", {"searchText": name}, api_key)
    except Exception as e:
        raise EntitySearchFailedError(
            f"Search request failed: {e}\n\n"
            f"Your API key might not be working, or the WarEra API may be down. "
            f"Please check your key, or try again later."
        )
    return data.get(cfg["search_key"]) or []


def confirm_entity_name(entity_type: str, entity_id: str, api_key: str) -> str:
    """Resolve an entity ID to its canonical display name, using the
    appropriate confirm endpoint for that entity type."""
    cfg = ENTITY_CONFIG[entity_type]
    data = call(cfg["confirm_endpoint"], {cfg["confirm_param"]: entity_id}, api_key)
    name = data.get(cfg["name_field"])
    if not name:
        raise RuntimeError(f"Could not confirm a name for {entity_type} ID {entity_id}.")
    return name



# ----------------------------------------------------------------------------
# Per-entity folder registry (same ID -> same folder, across runs)
# ----------------------------------------------------------------------------

def get_entity_folder(entity_id: str, resolved_name: str) -> str:
    os.makedirs(BASE_DIR, exist_ok=True)
    registry = load_json(REGISTRY_FILE, {})
    if entity_id in registry:
        folder_name = registry[entity_id]
    else:
        base = sanitize_filename(resolved_name) or entity_id
        folder_name = base
        existing = set(registry.values())
        i = 2
        while folder_name in existing:
            folder_name = f"{base}_{i}"
            i += 1
        registry[entity_id] = folder_name
        save_json(REGISTRY_FILE, registry)

    path = os.path.join(BASE_DIR, folder_name)
    os.makedirs(path, exist_ok=True)
    return path


# ----------------------------------------------------------------------------
# Settings persistence (prefill the form next run)
# ----------------------------------------------------------------------------

def load_settings() -> dict:
    return load_json(SETTINGS_FILE, {})


def save_settings(settings: dict):
    save_json(SETTINGS_FILE, settings)
    try:
        os.chmod(SETTINGS_FILE, 0o600)  # best-effort; not real encryption
    except Exception:
        pass


# ----------------------------------------------------------------------------
# Fetch + pagination
# ----------------------------------------------------------------------------

def fetch_all_transactions(entity_type, entity_id, api_key, start_dt, end_dt,
                            cache, stop_at_cached, log):
    cfg = ENTITY_CONFIG[entity_type]
    all_items = []
    cursor = None
    seen_cursors = set()

    for page in range(MAX_PAGES):
        params = {
            "limit": PAGE_LIMIT,
            cfg["query_param"]: entity_id,
            "transactionType": TRANSACTION_TYPE,
        }
        if cursor is not None:
            params["cursor"] = cursor

        try:
            data = call("transaction.getPaginatedTransactions", params, api_key)
        except Exception as e:
            log(f"[warn] giving up on page {page + 1} after retries: {e}")
            log("[warn] keeping what was collected so far -- it still merges into the cache.")
            break

        items = data.get("items", [])
        if not items:
            log(f"page {page + 1}: no more items.")
            break

        page_all_cached = stop_at_cached and all(it.get("_id") in cache for it in items)

        kept_this_page = 0
        stop = False
        for it in items:
            created = parse_iso(it["createdAt"])
            if end_dt and created > end_dt:
                continue  # too new, skip until inside the window
            if start_dt and created < start_dt:
                stop = True  # walked past the start of the window
                break
            all_items.append(it)
            kept_this_page += 1

        log(f"page {page + 1}: fetched {len(items)}, kept {kept_this_page} (total {len(all_items)})")

        next_cursor = data.get("nextCursor")
        if stop or page_all_cached or not next_cursor or next_cursor in seen_cursors:
            if page_all_cached and not stop:
                log("Reached previously-cached data -- stopping early (cache mode).")
            break

        seen_cursors.add(next_cursor)
        cursor = next_cursor
        time.sleep(0.2)  # be polite to the API

    return all_items


# ----------------------------------------------------------------------------
# Entity-side detection helpers
# ----------------------------------------------------------------------------

def entity_side(tx: dict, entity_type: str, entity_id: str):
    """Which raw side ('buyer'/'seller') the tracked entity occupies, or None."""
    cfg = ENTITY_CONFIG[entity_type]
    if tx.get(cfg["buyer_field"]) == entity_id:
        return "buyer"
    if tx.get(cfg["seller_field"]) == entity_id:
        return "seller"
    return None


def side_entity_tag(tx: dict, side: str):
    """Return (label, id) if this raw side carries ANY country/MU/party tag,
    regardless of which entity this run is tracking."""
    for buyer_field, seller_field, label in SIDE_ENTITY_TAGS:
        field = buyer_field if side == "buyer" else seller_field
        val = tx.get(field)
        if val:
            return label, val
    return None


def is_self_trade(tx: dict, entity_type: str, entity_id: str) -> bool:
    cfg = ENTITY_CONFIG[entity_type]
    return tx.get(cfg["buyer_field"]) == entity_id and tx.get(cfg["seller_field"]) == entity_id


def is_attributable(tx: dict, entity_type: str, entity_id: str) -> bool:
    """True if this trade's profit/loss should count toward the tracked
    entity's flip P&L."""
    if is_self_trade(tx, entity_type, entity_id):
        return False
    side = entity_side(tx, entity_type, entity_id)
    if side is None:
        return False
    if entity_type == "Personal":
        # Exclude trades this personal account executed ON BEHALF OF an
        # entity -- that profit belongs to the entity's books, not the
        # individual's.
        return side_entity_tag(tx, side) is None
    return True


# ----------------------------------------------------------------------------
# Flip profit (FIFO)
# ----------------------------------------------------------------------------

def compute_flip_profit(items: list, entity_type: str, entity_id: str):
    trades = [it for it in items
              if it.get("transactionType") == "trading" and is_attributable(it, entity_type, entity_id)]
    trades.sort(key=lambda it: it["createdAt"])

    open_lots = {}
    realised = {}
    unmatched_sell_qty = {}
    unmatched_sell_revenue = {}
    match_events = []  # [{date, itemCode, qty, profit}] -- for weekly profit charting

    for t in trades:
        code = t.get("itemCode")
        if not code:
            continue
        qty = t.get("quantity", 0)
        money = t.get("money", 0)
        if qty <= 0 or money is None:
            continue
        unit_price = money / qty
        side = entity_side(t, entity_type, entity_id)

        if side == "buyer":
            open_lots.setdefault(code, deque()).append([qty, unit_price])
        elif side == "seller":
            lots = open_lots.setdefault(code, deque())
            r = realised.setdefault(code, {"profit": 0.0, "matched_qty": 0, "revenue": 0.0, "cost": 0.0})
            remaining = qty
            trade_profit = 0.0
            while remaining > 0 and lots:
                lot_qty, lot_price = lots[0]
                take = min(lot_qty, remaining)
                this_profit = take * (unit_price - lot_price)
                r["profit"] += this_profit
                r["revenue"] += take * unit_price
                r["cost"] += take * lot_price
                r["matched_qty"] += take
                trade_profit += this_profit
                remaining -= take
                lot_qty -= take
                if lot_qty <= 0:
                    lots.popleft()
                else:
                    lots[0][0] = lot_qty
            if remaining > 0:
                unmatched_sell_qty[code] = unmatched_sell_qty.get(code, 0) + remaining
                unmatched_sell_revenue[code] = unmatched_sell_revenue.get(code, 0.0) + remaining * unit_price
            if trade_profit != 0.0 or r["matched_qty"] > 0:
                match_events.append({
                    "date": t.get("createdAt", ""),
                    "itemCode": code,
                    "qty": qty - remaining,
                    "profit": trade_profit,
                })

    rows = []
    total_profit = 0.0
    for code, r in sorted(realised.items()):
        margin_pct = round((r["profit"] / r["cost"]) * 100, 2) if r["cost"] else 0.0
        rows.append({
            "itemCode": code,
            "matched_qty": r["matched_qty"],
            "revenue": round(r["revenue"], 4),
            "cost": round(r["cost"], 4),
            "profit": round(r["profit"], 4),
            "margin_pct": margin_pct,
            "unmatched_sell_qty": unmatched_sell_qty.get(code, 0),
        })
        total_profit += r["profit"]

    open_positions = []
    for code, lots in open_lots.items():
        qty_sum = sum(q for q, _ in lots)
        if qty_sum > 0:
            cost_value = sum(q * p for q, p in lots)
            open_positions.append({"item": code, "qty": qty_sum, "cost_value": round(cost_value, 4)})

    produce_surplus = []
    for code, qty in unmatched_sell_qty.items():
        if qty > 0:
            produce_surplus.append({
                "item": code,
                "qty": qty,
                "revenue": round(unmatched_sell_revenue.get(code, 0.0), 4),
            })

    open_cost_total = sum(p["cost_value"] for p in open_positions)
    surplus_revenue_total = sum(p["revenue"] for p in produce_surplus)

    totals = {
        "total_realised_profit": round(total_profit, 4),
        "open_positions_by_item": {p["item"]: p["qty"] for p in open_positions},
        "unmatched_sell_qty_by_item": unmatched_sell_qty,
        "open_cost_total": round(open_cost_total, 4),
        "surplus_revenue_total": round(surplus_revenue_total, 4),
    }
    return rows, totals, match_events, open_positions, produce_surplus


# ----------------------------------------------------------------------------
# Full flattened transactions CSV (optional, checkbox-gated)
# ----------------------------------------------------------------------------

def flatten(items: list) -> tuple:
    priority = [
        "_id", "createdAt", "updatedAt", "transactionType",
        "sellerId", "buyerId", "money", "itemCode", "quantity",
        "offerCreatedAt",
    ]
    item_priority = [
        "item._id", "item.code", "item.type", "item.state", "item.maxState",
        "item.quantity", "item.lastAcquisitionAt", "item.skills",
    ]

    all_keys = set()
    rows = []
    for it in items:
        row = {}
        for k, v in it.items():
            if k == "item" and isinstance(v, dict):
                for ik, iv in v.items():
                    key = f"item.{ik}"
                    row[key] = json.dumps(iv) if isinstance(iv, (dict, list)) else iv
                    all_keys.add(key)
            else:
                row[k] = json.dumps(v) if isinstance(v, (dict, list)) else v
                all_keys.add(k)
        rows.append(row)

    ordered_cols = [c for c in priority if c in all_keys]
    ordered_cols += [c for c in item_priority if c in all_keys]
    ordered_cols += sorted(all_keys - set(ordered_cols))

    table = [[row.get(col, "") for col in ordered_cols] for row in rows]
    return ordered_cols, table


# ----------------------------------------------------------------------------
# Trading detail CSV (names resolved for the tracked entity's side)
# ----------------------------------------------------------------------------

def format_side_display(tx: dict, side: str, entity_type: str, entity_id: str, tracked_name: str) -> str:
    cfg = ENTITY_CONFIG[entity_type]
    field = cfg["buyer_field"] if side == "buyer" else cfg["seller_field"]
    if tx.get(field) == entity_id:
        return tracked_name
    tag = side_entity_tag(tx, side)
    if tag:
        label, eid = tag
        return f"{label}: {eid}"
    raw_id = tx.get(f"{side}Id", "")
    # No country/MU/party tag on this side means it was that account acting
    # for itself -- i.e. a Personal counterparty, not an unlabeled "other".
    return f"{raw_id} (Personal)"


def acting_for_label(tx: dict, entity_type: str, entity_id: str) -> str:
    """Which kind of entity the TRACKED side was acting for on this trade.
    Only meaningful when tracking a Personal account -- entity-type runs
    (Country/Party/MU) are, by construction of the query, always for that
    entity, so they use the Operator ID column instead (see build_trading_detail)."""
    side = entity_side(tx, entity_type, entity_id)
    if side is None:
        return "N/A"
    tag = side_entity_tag(tx, side)
    if tag:
        label, _eid = tag
        return label
    return "Personal"


def build_trading_detail(items: list, entity_type: str, entity_id: str, tracked_name: str):
    extra_col_name = "Acting For" if entity_type == "Personal" else "Operator ID"
    rows = []
    for it in items:
        if it.get("transactionType") != "trading":
            continue
        qty = it.get("quantity", 0)
        money = it.get("money", 0)
        price = round(money / qty, 6) if qty else ""

        self_trade = is_self_trade(it, entity_type, entity_id)
        side = entity_side(it, entity_type, entity_id)
        if self_trade:
            buy_sell = "Self"
        elif side == "buyer":
            buy_sell = "Buy"
        elif side == "seller":
            buy_sell = "Sell"
        else:
            buy_sell = "N/A"

        if entity_type == "Personal":
            extra_val = acting_for_label(it, entity_type, entity_id)
        else:
            extra_val = it.get(f"{side}Id", "") if side else "N/A"

        rows.append({
            "_id": it.get("_id", ""),
            "Offer At": it.get("offerCreatedAt", ""),
            "Fulfil At": it.get("updatedAt", ""),
            "Buy/Sell": buy_sell,
            "Seller (ToE)": format_side_display(it, "seller", entity_type, entity_id, tracked_name),
            "Buyer (ToE)": format_side_display(it, "buyer", entity_type, entity_id, tracked_name),
            extra_col_name: extra_val,
            "Item": it.get("itemCode", ""),
            "Total money": money,
            "Quantity": qty,
            "Price": price,
        })

    rows.sort(key=lambda r: r["Fulfil At"], reverse=True)
    return rows, extra_col_name


def scan_for_entity_fields(items: list, log):
    unknown_keys = set()
    for it in items:
        unknown_keys.update(set(it.keys()) - KNOWN_TRADING_KEYS)
    if unknown_keys:
        log(f"[scan] New field(s) beyond the known schema: {sorted(unknown_keys)}")


# ----------------------------------------------------------------------------
# Dashboard data (weekly aggregates the flip-profit CSV alone doesn't carry)
# ----------------------------------------------------------------------------

def iso_week_start(dt: datetime) -> str:
    monday = dt - timedelta(days=dt.weekday())
    return monday.strftime("%Y-%m-%d")


def compute_weekly_profit(match_events: list) -> list:
    weekly = {}
    for ev in match_events:
        week = iso_week_start(parse_iso(ev["date"]))
        weekly[week] = weekly.get(week, 0.0) + ev["profit"]
    cumulative = 0.0
    rows = []
    for week in sorted(weekly.keys()):
        cumulative += weekly[week]
        rows.append({"week": week, "profit": round(weekly[week], 4), "cumulative": round(cumulative, 4)})
    return rows


def compute_daily_profit(match_events: list) -> list:
    daily = {}
    for ev in match_events:
        day = ev["date"][:10]
        daily[day] = daily.get(day, 0.0) + ev["profit"]
    cumulative = 0.0
    rows = []
    for day in sorted(daily.keys()):
        cumulative += daily[day]
        rows.append({"day": day, "profit": round(daily[day], 4), "cumulative": round(cumulative, 4)})
    return rows


def compute_weekly_flow(items: list, entity_type: str, entity_id: str) -> list:
    trades = [it for it in items
              if it.get("transactionType") == "trading" and is_attributable(it, entity_type, entity_id)]
    weekly = {}
    for t in trades:
        week = iso_week_start(parse_iso(t["createdAt"]))
        side = entity_side(t, entity_type, entity_id)
        money = t.get("money", 0) or 0
        w = weekly.setdefault(week, {"Buy": 0.0, "Sell": 0.0, "count": 0})
        if side == "buyer":
            w["Buy"] += money
        elif side == "seller":
            w["Sell"] += money
        w["count"] += 1
    return [{"week": k, "Buy": round(v["Buy"], 4), "Sell": round(v["Sell"], 4), "count": v["count"]}
            for k, v in sorted(weekly.items())]


def compute_price_trends(items: list, entity_type: str, entity_id: str, flip_rows: list, top_n: int = 2):
    top_items = [r["itemCode"] for r in sorted(flip_rows, key=lambda r: r["matched_qty"], reverse=True)[:top_n]]
    trades = [it for it in items
              if it.get("transactionType") == "trading" and is_attributable(it, entity_type, entity_id)]
    result = {}
    for code in top_items:
        weekly = {}
        for t in trades:
            if t.get("itemCode") != code:
                continue
            qty = t.get("quantity", 0) or 0
            if qty <= 0:
                continue
            week = iso_week_start(parse_iso(t["createdAt"]))
            agg = weekly.setdefault(week, [0.0, 0.0])
            agg[0] += t.get("money", 0) or 0
            agg[1] += qty
        result[code] = [{"week": w, "price": round(m / q, 6) if q else 0} for w, (m, q) in sorted(weekly.items())]
    return result, top_items


def format_search_window(start_dt, end_dt, use_cache: bool) -> str:
    if use_cache:
        return "Full cached history (date range ignored)"
    s = start_dt.strftime("%Y-%m-%d %H:%M UTC") if start_dt else "the beginning"
    e = end_dt.strftime("%Y-%m-%d %H:%M UTC") if end_dt else "now"
    return f"{s} \u2192 {e}"


def build_dashboard_data(items: list, entity_type: str, entity_id: str, search_window: str) -> dict:
    flip_rows, totals, match_events, open_positions, produce_surplus = compute_flip_profit(
        items, entity_type, entity_id)
    weekly_profit = compute_weekly_profit(match_events)
    daily_profit = compute_daily_profit(match_events)
    weekly_flow = compute_weekly_flow(items, entity_type, entity_id)
    price_trends, top_items = compute_price_trends(items, entity_type, entity_id, flip_rows, top_n=2)

    trading_items = [it for it in items if it.get("transactionType") == "trading"]
    attributable = [it for it in trading_items if is_attributable(it, entity_type, entity_id)]

    if entity_type == "Personal":
        involved = [it for it in trading_items
                    if entity_side(it, entity_type, entity_id) is not None
                    and not is_self_trade(it, entity_type, entity_id)]
        n_entity_trades = max(len(involved) - len(attributable), 0)
    else:
        n_entity_trades = 0

    dates = [parse_iso(it["createdAt"]) for it in attributable] or [datetime.now(timezone.utc)]
    total_buy = sum(it.get("money", 0) or 0 for it in attributable
                     if entity_side(it, entity_type, entity_id) == "buyer")
    total_sell = sum(it.get("money", 0) or 0 for it in attributable
                      if entity_side(it, entity_type, entity_id) == "seller")

    date_min = min(dates)
    date_max = max(dates)
    span_days = (date_max - date_min).days
    default_granularity = "daily" if span_days < 14 else "weekly"

    return {
        "totals": {
            "total_profit": totals["total_realised_profit"],
            "total_buy": round(total_buy, 2),
            "total_sell": round(total_sell, 2),
            "n_trades": len(attributable),
            "n_entity_trades": n_entity_trades,
            "date_min": date_min.strftime("%Y-%m-%d"),
            "date_max": date_max.strftime("%Y-%m-%d"),
            "search_window": search_window,
            "open_cost_total": totals["open_cost_total"],
            "surplus_revenue_total": totals["surplus_revenue_total"],
            "span_days": span_days,
            "default_granularity": default_granularity,
        },
        "weekly_profit": weekly_profit,
        "daily_profit": daily_profit,
        "item_summary": flip_rows,
        "weekly_flow": weekly_flow,
        "open_positions": open_positions,
        "produce_surplus": produce_surplus,
        "price_trends": price_trends,
        "top_items": top_items,
    }


def generate_dashboard_html(items: list, entity_type: str, entity_id: str, resolved_name: str,
                             output_path: str, start_dt=None, end_dt=None, use_cache: bool = False):
    search_window = format_search_window(start_dt, end_dt, use_cache)
    data = build_dashboard_data(items, entity_type, entity_id, search_window)

    if entity_type == "Personal" and data["totals"]["n_entity_trades"]:
        footnote_extra = (f' (<b>{data["totals"]["n_entity_trades"]:,}</b> '
                           f'country/party/MU-directed fills excluded)')
    else:
        footnote_extra = ""

    html = DASHBOARD_TEMPLATE
    html = html.replace("__ENTITY_NAME__", resolved_name)
    html = html.replace("__ENTITY_TYPE__", entity_type)
    html = html.replace("__FOOTNOTE_EXTRA__", footnote_extra)
    html = html.replace("__DATA_JSON__", json.dumps(data))

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    return data


# ----------------------------------------------------------------------------
# GUI
# ----------------------------------------------------------------------------

# ----------------------------------------------------------------------------
# Dropdown date/time selector (no external date-picker dependency)
# ----------------------------------------------------------------------------

class DateTimeSelector(ttk.Frame):
    """A row of dropdowns for Year/Month/Day/Hour/Minute, with an
    'open-ended' checkbox that disables them and means 'unset' -- matching
    parse_user_datetime()'s existing '' == None convention exactly."""

    def __init__(self, parent, open_ended_text: str = "Open-ended"):
        super().__init__(parent)
        now = datetime.now(timezone.utc)
        years = [str(y) for y in range(now.year - 3, now.year + 2)]

        self.set_var = tk.BooleanVar(value=False)
        self.year_var = tk.StringVar(value=str(now.year))
        self.month_var = tk.StringVar(value=f"{now.month:02d}")
        self.day_var = tk.StringVar(value=f"{now.day:02d}")
        self.hour_var = tk.StringVar(value="00")
        self.minute_var = tk.StringVar(value="00")

        self.chk = ttk.Checkbutton(self, text=open_ended_text, variable=self.set_var,
                                    command=self._on_toggle)
        self.chk.pack(side="left")

        self.combos = []
        specs = [
            (self.year_var, years, 5),
            (self.month_var, [f"{m:02d}" for m in range(1, 13)], 3),
            (self.day_var, [f"{d:02d}" for d in range(1, 32)], 3),
            (self.hour_var, [f"{h:02d}" for h in range(0, 24)], 3),
            (self.minute_var, [f"{m:02d}" for m in range(0, 60)], 3),
        ]
        for i, (var, values, width) in enumerate(specs):
            if i > 0:
                ttk.Label(self, text=":" if i >= 3 else "-").pack(side="left")
            cb = ttk.Combobox(self, textvariable=var, values=values, state="disabled",
                               width=width)
            cb.pack(side="left")
            self.combos.append(cb)
        ttk.Label(self, text=" (UTC)").pack(side="left")

    def _on_toggle(self):
        state = "readonly" if self.set_var.get() else "disabled"
        for cb in self.combos:
            cb.configure(state=state)

    def bind_return(self, callback):
        self.chk.bind("<Return>", callback)
        for cb in self.combos:
            cb.bind("<Return>", callback)

    def get_datetime_string(self) -> str:
        """Returns '' (open-ended) or 'YYYY-MM-DD HH:MM:00', matching the
        format parse_user_datetime() already accepts."""
        if not self.set_var.get():
            return ""
        return (f"{self.year_var.get()}-{self.month_var.get()}-{self.day_var.get()} "
                f"{self.hour_var.get()}:{self.minute_var.get()}:00")

    def set_from_string(self, s: str):
        s = (s or "").strip()
        if not s:
            self.set_var.set(False)
            self._on_toggle()
            return
        try:
            dt = parse_user_datetime(s)
        except ValueError:
            dt = None
        if dt is None:
            self.set_var.set(False)
            self._on_toggle()
            return
        self.set_var.set(True)
        self.year_var.set(f"{dt.year:04d}")
        self.month_var.set(f"{dt.month:02d}")
        self.day_var.set(f"{dt.day:02d}")
        self.hour_var.set(f"{dt.hour:02d}")
        self.minute_var.set(f"{dt.minute:02d}")
        self._on_toggle()


class WareraApp:
    def __init__(self, root):
        self.root = root
        root.title("WarEra Flip Trading Tracker")
        root.geometry("820x720")

        self.log_queue = queue.Queue()
        self.running = False

        settings = load_settings()
        pad = {"padx": 8, "pady": 4}

        frm = ttk.Frame(root)
        frm.pack(fill="x", **pad)
        frm.columnconfigure(1, weight=1)

        ttk.Label(frm, text="Entity Type:").grid(row=0, column=0, sticky="w")
        self.entity_type_var = tk.StringVar(value=settings.get("entity_type", "Personal"))
        ttk.Combobox(frm, textvariable=self.entity_type_var, values=list(ENTITY_CONFIG.keys()),
                     state="readonly", width=20).grid(row=0, column=1, sticky="w")

        ttk.Label(frm, text="Input By:").grid(row=1, column=0, sticky="w")
        self.input_mode_var = tk.StringVar(value=settings.get("input_mode", "Name"))
        ttk.Combobox(frm, textvariable=self.input_mode_var, values=["Name", "ID"],
                     state="readonly", width=20).grid(row=1, column=1, sticky="w")

        ttk.Label(frm, text="ID / Name:").grid(row=2, column=0, sticky="w")
        self.value_var = tk.StringVar(value=settings.get("input_value", ""))
        ttk.Entry(frm, textvariable=self.value_var, width=45).grid(row=2, column=1, sticky="w")

        ttk.Label(frm, text="Start:").grid(row=3, column=0, sticky="w")
        self.start_selector = DateTimeSelector(frm, open_ended_text="Set start date")
        self.start_selector.grid(row=3, column=1, sticky="w")
        self.start_selector.set_from_string(settings.get("start_date", ""))

        ttk.Label(frm, text="End:").grid(row=4, column=0, sticky="w")
        self.end_selector = DateTimeSelector(frm, open_ended_text="Set end date")
        self.end_selector.grid(row=4, column=1, sticky="w")
        self.end_selector.set_from_string(settings.get("end_date", ""))

        ttk.Label(frm, text="API Key:").grid(row=5, column=0, sticky="w")
        self.api_key_var = tk.StringVar(value=settings.get("api_key", ""))
        ttk.Entry(frm, textvariable=self.api_key_var, width=55, show="*").grid(row=5, column=1, sticky="w")

        self.save_csv_var = tk.BooleanVar(value=settings.get("save_transactions_csv", False))
        ttk.Checkbutton(
            frm, variable=self.save_csv_var,
            text="Save full transaction CSV (warera_transactions_<ID>.csv)",
        ).grid(row=6, column=0, columnspan=2, sticky="w")

        self.use_cache_var = tk.BooleanVar(value=settings.get("use_cache", False))
        ttk.Checkbutton(
            frm, variable=self.use_cache_var,
            text="Use full cached history for reports (ignores date range; fetch only new data)",
        ).grid(row=7, column=0, columnspan=2, sticky="w")

        self.dashboard_var = tk.BooleanVar(value=settings.get("generate_dashboard", False))
        ttk.Checkbutton(
            frm, variable=self.dashboard_var,
            text="Generate HTML summary dashboard (warera_dashboard_<ID>.html)",
        ).grid(row=8, column=0, columnspan=2, sticky="w")

        self.dashboard_png_var = tk.BooleanVar(value=settings.get("dashboard_png", False))
        ttk.Checkbutton(
            frm, variable=self.dashboard_png_var,
            text="Save a picture summary of the HTML dashboard",
        ).grid(row=9, column=0, columnspan=2, sticky="w")

        btn_frm = ttk.Frame(root)
        btn_frm.pack(fill="x", **pad)
        self.run_btn = ttk.Button(btn_frm, text="Run", command=self.on_run)
        self.run_btn.pack(side="left", padx=4)
        ttk.Button(btn_frm, text="Delete Cache", command=self.on_delete_cache).pack(side="left", padx=4)

        ttk.Label(root, text="Log:").pack(anchor="w", padx=8)
        self.log_text = scrolledtext.ScrolledText(root, height=26, state="disabled", wrap="word")
        self.log_text.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        self.root.after(150, self.poll_log_queue)

        # Enter runs the tool from anywhere in the form (messagebox dialogs
        # capture their own Enter first, so this won't fire while one is open).
        root.bind("<Return>", self._on_enter_key)

    def _on_enter_key(self, event=None):
        if not self.running:
            self.on_run()

    # -- logging -------------------------------------------------------

    def log(self, msg: str):
        self.log_queue.put(msg)

    def poll_log_queue(self):
        try:
            while True:
                msg = self.log_queue.get_nowait()
                self.log_text.configure(state="normal")
                self.log_text.insert("end", msg + "\n")
                self.log_text.see("end")
                self.log_text.configure(state="disabled")
        except queue.Empty:
            pass
        self.root.after(150, self.poll_log_queue)

    # -- input gathering / entity resolution ----------------------------

    def gather_basic_inputs(self):
        entity_type = self.entity_type_var.get()
        input_mode = self.input_mode_var.get()
        value = self.value_var.get().strip()
        api_key = self.api_key_var.get().strip()
        if not value:
            raise ValueError("Please enter an ID or a name.")
        if not api_key:
            raise ValueError("Please enter your API key.")
        return entity_type, input_mode, value, api_key

    def gather_inputs(self):
        entity_type, input_mode, value, api_key = self.gather_basic_inputs()
        start_dt = parse_user_datetime(self.start_selector.get_datetime_string())
        end_dt = parse_user_datetime(self.end_selector.get_datetime_string())
        return entity_type, input_mode, value, api_key, start_dt, end_dt

    def resolve_entity(self, entity_type, input_mode, value, api_key):
        if input_mode == "ID":
            resolved_name = confirm_entity_name(entity_type, value, api_key)
            return value, resolved_name, True  # True = still needs the final "proceed?" confirm

        candidates = search_entity_candidates(value, entity_type, api_key)
        if not candidates:
            raise EntityNotFoundError(
                f"No {entity_type} found matching '{value}'. Please try searching by ID instead."
            )

        shown = 0
        for i, candidate_id in enumerate(candidates):
            try:
                candidate_name = confirm_entity_name(entity_type, candidate_id, api_key)
            except Exception:
                continue  # this candidate couldn't be resolved -- try the next one
            shown += 1
            choice = messagebox.askyesnocancel(
                "Confirm entity",
                f"Result {i + 1} of {len(candidates)}\n\n"
                f"{entity_type}: {candidate_name}\nID: {candidate_id}\n\n"
                f"Is this the one you're looking for?\n\n"
                f"(No = show next result, Cancel = stop searching)",
            )
            if choice is None:
                raise EntityResolutionCancelled()
            if choice:
                return candidate_id, candidate_name, False  # already confirmed, no second prompt needed

        if shown == 0:
            raise EntitySearchFailedError(
                "Could not confirm any of the search results -- your API key might not be "
                "working, or the WarEra API may be down. Please check your key, or try again later."
            )
        raise EntityNotFoundError(
            f"None of the {len(candidates)} results for '{value}' matched. "
            f"Please try searching by ID instead."
        )

    # -- Run -------------------------------------------------------------

    def on_run(self):
        if self.running:
            return
        try:
            entity_type, input_mode, value, api_key, start_dt, end_dt = self.gather_inputs()
        except ValueError as e:
            messagebox.showerror("Invalid input", str(e))
            return

        try:
            entity_id, resolved_name, needs_confirm = self.resolve_entity(
                entity_type, input_mode, value, api_key)
        except EntityResolutionCancelled:
            return
        except EntityNotFoundError as e:
            messagebox.showerror("No match found", str(e))
            return
        except EntitySearchFailedError as e:
            messagebox.showerror("Search failed", str(e))
            return
        except Exception as e:
            messagebox.showerror("Could not resolve entity", str(e))
            return

        if needs_confirm and not messagebox.askyesno(
            "Confirm entity",
            f"Track {entity_type}: {resolved_name}\nID: {entity_id}\n\nProceed?",
        ):
            return

        save_settings({
            "entity_type": entity_type,
            "input_mode": input_mode,
            "input_value": value,
            "start_date": self.start_selector.get_datetime_string(),
            "end_date": self.end_selector.get_datetime_string(),
            "api_key": api_key,
            "save_transactions_csv": self.save_csv_var.get(),
            "use_cache": self.use_cache_var.get(),
            "generate_dashboard": self.dashboard_var.get(),
            "dashboard_png": self.dashboard_png_var.get(),
        })

        self.running = True
        self.run_btn.configure(state="disabled")
        threading.Thread(
            target=self.run_pipeline,
            args=(entity_type, entity_id, resolved_name, api_key, start_dt, end_dt,
                  self.save_csv_var.get(), self.use_cache_var.get(),
                  self.dashboard_var.get(), self.dashboard_png_var.get()),
            daemon=True,
        ).start()

    def run_pipeline(self, entity_type, entity_id, resolved_name, api_key,
                      start_dt, end_dt, save_transactions_csv, use_cache,
                      generate_dashboard, dashboard_png):
        try:
            self.log(f"=== Tracking {entity_type}: {resolved_name} (ID: {entity_id}) ===")
            folder = get_entity_folder(entity_id, resolved_name)
            self.log(f"Output folder: {folder}")

            cache_path = os.path.join(folder, "transactions_cache.json")
            cache = load_json(cache_path, {})
            cache_before = len(cache)

            fetched = fetch_all_transactions(
                entity_type, entity_id, api_key, start_dt, end_dt,
                cache, stop_at_cached=use_cache, log=self.log,
            )
            self.log(f"Fetched {len(fetched)} transactions this run.")

            new_count = 0
            for it in fetched:
                tid = it.get("_id")
                if not tid:
                    continue
                if tid not in cache:
                    new_count += 1
                cache[tid] = it
            save_json(cache_path, cache)
            self.log(f"Cache: {cache_before} -> {len(cache)} unique transactions ({new_count} new).")

            if len(cache) == 0:
                warning = (
                    "No transactions were found at all for this entity, across the "
                    "account's entire history. Your API key might not be working, or "
                    "the WarEra API may be down. Please check your key, or try again later."
                )
                self.log(f"[warn] {warning}")
                messagebox.showwarning("No transactions found", warning)

            if use_cache:
                items = [it for it in cache.values() if it.get("transactionType") == TRANSACTION_TYPE]
                self.log(f"Using full cached history: {len(items)} transactions (date range ignored).")
            else:
                items = fetched
                self.log(f"Using this run's fetch only: {len(items)} transactions (scoped to date range).")

            scan_for_entity_fields(items, self.log)

            if save_transactions_csv:
                headers, rows = flatten(items)
                out_path = os.path.join(folder, f"warera_transactions_{entity_id}.csv")
                with open(out_path, "w", newline="", encoding="utf-8") as f:
                    writer = csv.writer(f)
                    writer.writerow(headers)
                    writer.writerows(rows)
                self.log(f"Saved {len(rows)} rows to {out_path}")
            else:
                self.log("Skipped full transactions CSV (checkbox off).")

            flip_rows, totals, match_events, open_positions, produce_surplus = compute_flip_profit(
                items, entity_type, entity_id)
            flip_path = os.path.join(folder, f"warera_flip_profit_{entity_id}.csv")
            with open(flip_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=[
                    "itemCode", "matched_qty", "revenue", "cost", "profit", "margin_pct", "unmatched_sell_qty",
                ])
                writer.writeheader()
                writer.writerows(flip_rows)
            self.log(f"Saved flip profit breakdown to {flip_path}")

            detail_rows, extra_col_name = build_trading_detail(items, entity_type, entity_id, resolved_name)
            detail_path = os.path.join(folder, f"warera_trading_detail_{entity_id}.csv")
            with open(detail_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=[
                    "_id", "Offer At", "Fulfil At", "Buy/Sell", "Seller (ToE)", "Buyer (ToE)",
                    extra_col_name, "Item", "Total money", "Quantity", "Price",
                ])
                writer.writeheader()
                writer.writerows(detail_rows)
            self.log(f"Saved {len(detail_rows)} trades to {detail_path}")

            self.log(f"\nTotal realised flip profit: {totals['total_realised_profit']}")
            if totals["open_positions_by_item"]:
                self.log(f"Still holding (bought, not yet sold): {totals['open_positions_by_item']}")
            if totals["unmatched_sell_qty_by_item"]:
                self.log(f"Sold with no matching purchase on record: {totals['unmatched_sell_qty_by_item']}")

            if generate_dashboard or dashboard_png:
                dash_path = os.path.join(folder, f"warera_dashboard_{entity_id}.html")
                keep_html = generate_dashboard
                if not keep_html:
                    # Only needed transiently to source the screenshot from.
                    dash_path = os.path.join(folder, f".tmp_dashboard_{entity_id}.html")

                generate_dashboard_html(items, entity_type, entity_id, resolved_name, dash_path,
                                          start_dt=start_dt, end_dt=end_dt, use_cache=use_cache)
                if keep_html:
                    self.log(f"Saved summary dashboard to {dash_path} (open it in any browser).")

                if dashboard_png:
                    png_path = os.path.join(folder, f"warera_dashboard_{entity_id}.png")
                    try:
                        from html2image import Html2Image

                        # --- Dynamic height so the open-positions table never gets cut off ---
                        base_height = 1900  # fixed charts, headers, spacing
                        num_table_rows = len(open_positions + produce_surplus)
                        dynamic_height = max(1900, base_height + (num_table_rows * 40))

                        hti = Html2Image(
                            output_path=folder,
                            custom_flags=["--force-device-scale-factor=2"],
                        )
                        hti.screenshot(
                            other_file=dash_path,
                            save_as=f"warera_dashboard_{entity_id}.png",
                            size=(1220, dynamic_height),
                        )
                        self.log(f"Saved dashboard picture to {png_path} (auto-sized to 1220x{dynamic_height})")
                    except ImportError:
                        self.log("[dashboard] 'html2image' not found. Run 'pip install html2image' "
                                  "to enable picture exports.")
                    except Exception as e:
                        self.log(f"[dashboard] Picture export failed ({e}). "
                                  f"You can still open the .html file directly in a browser.")
                    finally:
                        if not keep_html and os.path.exists(dash_path):
                            os.remove(dash_path)

            self.log("=== Done ===\n")
        except Exception as e:
            self.log(f"[error] {e}")
            messagebox.showerror("Error", str(e))
        finally:
            self.running = False
            self.run_btn.configure(state="normal")

    # -- Delete cache ------------------------------------------------------

    def on_delete_cache(self):
        try:
            entity_type, input_mode, value, api_key = self.gather_basic_inputs()
        except ValueError as e:
            messagebox.showerror("Invalid input", str(e))
            return
        try:
            entity_id, resolved_name, _needs_confirm = self.resolve_entity(
                entity_type, input_mode, value, api_key)
        except EntityResolutionCancelled:
            return
        except EntityNotFoundError as e:
            messagebox.showerror("No match found", str(e))
            return
        except EntitySearchFailedError as e:
            messagebox.showerror("Search failed", str(e))
            return
        except Exception as e:
            messagebox.showerror("Could not resolve entity", str(e))
            return

        registry = load_json(REGISTRY_FILE, {})
        folder_name = registry.get(entity_id)
        if not folder_name:
            messagebox.showinfo("No cache", f"No cache found for {resolved_name}.")
            return
        cache_path = os.path.join(BASE_DIR, folder_name, "transactions_cache.json")
        if not os.path.exists(cache_path):
            messagebox.showinfo("No cache", f"No cache file found for {resolved_name}.")
            return

        if messagebox.askyesno(
            "Delete cache",
            f"Delete cached transaction history for {resolved_name}?\n"
            f"This cannot be undone. CSV reports already saved are kept.",
        ):
            os.remove(cache_path)
            self.log(f"Deleted cache for {resolved_name} ({entity_id}).")
            messagebox.showinfo("Deleted", f"Cache deleted for {resolved_name}.")


def main():
    root = tk.Tk()
    WareraApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
