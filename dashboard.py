"""
dashboard.py - Local web dashboard served on localhost:8080.
"""

import json
import sqlite3
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from datetime import datetime

DB_PATH = Path.home() / ".claude" / "usage.db"


def get_dashboard_data(db_path=DB_PATH):
    if not db_path.exists():
        return {"error": "Database not found. Run: python cli.py scan"}

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # ── All models (for filter UI) ────────────────────────────────────────────
    model_rows = conn.execute("""
        SELECT COALESCE(model, 'unknown') as model
        FROM turns
        GROUP BY model
        ORDER BY SUM(input_tokens + output_tokens) DESC
    """).fetchall()
    all_models = [r["model"] for r in model_rows]

    # ── Daily per-model, ALL history (client filters by range) ────────────────
    daily_rows = conn.execute("""
        SELECT
            substr(timestamp, 1, 10)   as day,
            COALESCE(model, 'unknown') as model,
            SUM(input_tokens)          as input,
            SUM(output_tokens)         as output,
            SUM(cache_read_tokens)     as cache_read,
            SUM(cache_creation_tokens) as cache_creation,
            COUNT(*)                   as turns
        FROM turns
        GROUP BY day, model
        ORDER BY day, model
    """).fetchall()

    daily_by_model = [{
        "day":            r["day"],
        "model":          r["model"],
        "input":          r["input"] or 0,
        "output":         r["output"] or 0,
        "cache_read":     r["cache_read"] or 0,
        "cache_creation": r["cache_creation"] or 0,
        "turns":          r["turns"] or 0,
    } for r in daily_rows]

    # ── All sessions (client filters by range and model) ──────────────────────
    session_rows = conn.execute("""
        SELECT
            session_id, project_name, first_timestamp, last_timestamp,
            total_input_tokens, total_output_tokens,
            total_cache_read, total_cache_creation, model, turn_count
        FROM sessions
        ORDER BY last_timestamp DESC
    """).fetchall()


    sessions_all = []
    for r in session_rows:
        try:
            t1 = datetime.fromisoformat(r["first_timestamp"].replace("Z", "+00:00"))
            t2 = datetime.fromisoformat(r["last_timestamp"].replace("Z", "+00:00"))
            duration_min = round((t2 - t1).total_seconds() / 60, 1)
        except Exception:
            duration_min = 0
        sessions_all.append({
            "session_id":    r["session_id"][:8],
            "full_session_id": r["session_id"],
            "project":       r["project_name"] or "unknown",
            "last":          (r["last_timestamp"] or "")[:16].replace("T", " "),
            "last_date":     (r["last_timestamp"] or "")[:10],
            "first_ts":      r["first_timestamp"] or "",
            "last_ts":       r["last_timestamp"] or "",
            "duration_min":  duration_min,
            "model":         r["model"] or "unknown",
            "turns":         r["turn_count"] or 0,
            "input":         r["total_input_tokens"] or 0,
            "output":        r["total_output_tokens"] or 0,
            "cache_read":    r["total_cache_read"] or 0,
            "cache_creation": r["total_cache_creation"] or 0,
        })

    # ── Top turns by token usage (for expensive query detection) ─────────────
    top_turn_rows = conn.execute("""
        SELECT
            t.session_id, t.timestamp, t.model, t.tool_name,
            t.input_tokens, t.output_tokens,
            t.cache_read_tokens, t.cache_creation_tokens,
            t.prompt_preview,
            s.project_name
        FROM turns t
        LEFT JOIN sessions s ON t.session_id = s.session_id
        ORDER BY (t.input_tokens + t.output_tokens) DESC
        LIMIT 50
    """).fetchall()

    top_turns = [{
        "session_id":     r["session_id"][:8],
        "full_session_id": r["session_id"],
        "timestamp":      (r["timestamp"] or "")[:16].replace("T", " "),
        "timestamp_date": (r["timestamp"] or "")[:10],
        "model":          r["model"] or "unknown",
        "tool_name":      r["tool_name"] or "",
        "input":          r["input_tokens"] or 0,
        "output":         r["output_tokens"] or 0,
        "cache_read":     r["cache_read_tokens"] or 0,
        "cache_creation": r["cache_creation_tokens"] or 0,
        "project":        r["project_name"] or "unknown",
        "prompt_preview": r["prompt_preview"] or "",
    } for r in top_turn_rows]

    conn.close()

    return {
        "all_models":     all_models,
        "daily_by_model": daily_by_model,
        "sessions_all":   sessions_all,
        "top_turns":      top_turns,
        "generated_at":   datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


PRICING_PY = {
    "claude-opus-4-6":   {"input": 6.15, "output": 30.75, "cache_write": 7.69, "cache_read": 0.61},
    "claude-opus-4-5":   {"input": 6.15, "output": 30.75, "cache_write": 7.69, "cache_read": 0.61},
    "claude-sonnet-4-6": {"input": 3.69, "output": 18.45, "cache_write": 4.61, "cache_read": 0.37},
    "claude-sonnet-4-5": {"input": 3.69, "output": 18.45, "cache_write": 4.61, "cache_read": 0.37},
    "claude-haiku-4-5":  {"input": 1.23, "output": 6.15,  "cache_write": 1.54, "cache_read": 0.12},
    "claude-haiku-4-6":  {"input": 1.23, "output": 6.15,  "cache_write": 1.54, "cache_read": 0.12},
}


def _get_pricing_py(model):
    if not model:
        return None
    if model in PRICING_PY:
        return PRICING_PY[model]
    for key in PRICING_PY:
        if model.startswith(key):
            return PRICING_PY[key]
    m = model.lower()
    if "opus" in m:
        return PRICING_PY["claude-opus-4-6"]
    if "sonnet" in m:
        return PRICING_PY["claude-sonnet-4-6"]
    if "haiku" in m:
        return PRICING_PY["claude-haiku-4-5"]
    return None


def _calc_cost_py(model, inp, out, cache_read, cache_creation):
    p = _get_pricing_py(model)
    if not p:
        return 0
    return (
        inp * p["input"] / 1e6
        + out * p["output"] / 1e6
        + cache_read * p["cache_read"] / 1e6
        + cache_creation * p["cache_write"] / 1e6
    )


def get_session_detail(session_id_prefix, db_path=DB_PATH):
    if not db_path.exists():
        return {"error": "Database not found. Run: python cli.py scan"}

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    session = conn.execute(
        "SELECT * FROM sessions WHERE session_id LIKE ?",
        (session_id_prefix + "%",)
    ).fetchone()

    if not session:
        conn.close()
        return {"error": "Session not found"}

    sid = session["session_id"]

    turns = conn.execute("""
        SELECT timestamp, model, input_tokens, output_tokens,
               cache_read_tokens, cache_creation_tokens, tool_name,
               prompt_preview
        FROM turns
        WHERE session_id = ?
        ORDER BY timestamp
    """, (sid,)).fetchall()

    conn.close()

    turn_list = []
    cumulative_cost = 0
    for i, t in enumerate(turns):
        inp = t["input_tokens"] or 0
        out = t["output_tokens"] or 0
        cr = t["cache_read_tokens"] or 0
        cc = t["cache_creation_tokens"] or 0
        cost = _calc_cost_py(t["model"], inp, out, cr, cc)
        cumulative_cost += cost

        total_input_equivalent = inp + cr + cc
        cache_rate = (cr / total_input_equivalent * 100) if total_input_equivalent > 0 else 0

        turn_list.append({
            "num": i + 1,
            "timestamp": (t["timestamp"] or "")[:19].replace("T", " "),
            "model": t["model"] or "unknown",
            "tool_name": t["tool_name"] or "",
            "prompt_preview": t["prompt_preview"] or "",
            "input": inp,
            "output": out,
            "cache_read": cr,
            "cache_creation": cc,
            "cost": round(cost, 6),
            "cumulative_cost": round(cumulative_cost, 6),
            "cache_rate": round(cache_rate, 1),
        })

    try:
        t1 = datetime.fromisoformat(session["first_timestamp"].replace("Z", "+00:00"))
        t2 = datetime.fromisoformat(session["last_timestamp"].replace("Z", "+00:00"))
        duration_min = round((t2 - t1).total_seconds() / 60, 1)
    except Exception:
        duration_min = 0

    total_cr = session["total_cache_read"] or 0
    total_cc = session["total_cache_creation"] or 0
    total_inp = session["total_input_tokens"] or 0
    total_input_eq = total_inp + total_cr + total_cc
    session_cache_rate = (total_cr / total_input_eq * 100) if total_input_eq > 0 else 0

    return {
        "session_id": sid[:8],
        "full_session_id": sid,
        "project": session["project_name"] or "unknown",
        "model": session["model"] or "unknown",
        "duration_min": duration_min,
        "first": (session["first_timestamp"] or "")[:16].replace("T", " "),
        "last": (session["last_timestamp"] or "")[:16].replace("T", " "),
        "total_cost": round(cumulative_cost, 6),
        "turn_count": session["turn_count"] or 0,
        "cache_rate": round(session_cache_rate, 1),
        "total_input": total_inp,
        "total_output": session["total_output_tokens"] or 0,
        "total_cache_read": total_cr,
        "total_cache_creation": total_cc,
        "turns": turn_list,
    }


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Claude Code Usage Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  :root {
    --bg: #0f1117;
    --card: #1a1d27;
    --border: #2a2d3a;
    --text: #e2e8f0;
    --muted: #8892a4;
    --accent: #d97757;
    --blue: #4f8ef7;
    --green: #4ade80;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; font-size: 14px; }

  header { background: var(--card); border-bottom: 1px solid var(--border); padding: 16px 24px; display: flex; align-items: center; justify-content: space-between; }
  header h1 { font-size: 18px; font-weight: 600; color: var(--accent); }
  header .meta { color: var(--muted); font-size: 12px; }

  #filter-bar { background: var(--card); border-bottom: 1px solid var(--border); padding: 10px 24px; display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
  .filter-label { font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); white-space: nowrap; }
  .filter-sep { width: 1px; height: 22px; background: var(--border); flex-shrink: 0; }
  #model-checkboxes { display: flex; flex-wrap: wrap; gap: 6px; }
  .model-cb-label { display: flex; align-items: center; gap: 5px; padding: 3px 10px; border-radius: 20px; border: 1px solid var(--border); cursor: pointer; font-size: 12px; color: var(--muted); transition: border-color 0.15s, color 0.15s, background 0.15s; user-select: none; }
  .model-cb-label:hover { border-color: var(--accent); color: var(--text); }
  .model-cb-label.checked { background: rgba(217,119,87,0.12); border-color: var(--accent); color: var(--text); }
  .model-cb-label input { display: none; }
  .filter-btn { padding: 3px 10px; border-radius: 4px; border: 1px solid var(--border); background: transparent; color: var(--muted); font-size: 11px; cursor: pointer; white-space: nowrap; }
  .filter-btn:hover { border-color: var(--accent); color: var(--text); }
  .range-group { display: flex; border: 1px solid var(--border); border-radius: 6px; overflow: hidden; flex-shrink: 0; }
  .range-btn { padding: 4px 13px; background: transparent; border: none; border-right: 1px solid var(--border); color: var(--muted); font-size: 12px; cursor: pointer; transition: background 0.15s, color 0.15s; }
  .range-btn:last-child { border-right: none; }
  .range-btn:hover { background: rgba(255,255,255,0.04); color: var(--text); }
  .range-btn.active { background: rgba(217,119,87,0.15); color: var(--accent); font-weight: 600; }

  .container { max-width: 1400px; margin: 0 auto; padding: 24px; }
  .stats-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 16px; margin-bottom: 24px; }
  .stat-card { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 16px; }
  .stat-card .label { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 6px; }
  .stat-card .value { font-size: 22px; font-weight: 700; }
  .stat-card .sub { color: var(--muted); font-size: 11px; margin-top: 4px; }

  .charts-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 24px; }
  .chart-card { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 20px; }
  .chart-card.wide { grid-column: 1 / -1; }
  .chart-card h2 { font-size: 13px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 16px; }
  .chart-wrap { position: relative; height: 240px; }
  .chart-wrap.tall { height: 300px; }

  table { width: 100%; border-collapse: collapse; }
  th { text-align: left; padding: 8px 12px; font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); border-bottom: 1px solid var(--border); }
  td { padding: 10px 12px; border-bottom: 1px solid var(--border); font-size: 13px; }
  tr:last-child td { border-bottom: none; }
  tr:hover td { background: rgba(255,255,255,0.02); }
  .model-tag { display: inline-block; padding: 2px 7px; border-radius: 4px; font-size: 11px; background: rgba(79,142,247,0.15); color: var(--blue); }
  .cost { color: var(--green); font-family: monospace; }
  .cost-na { color: var(--muted); font-family: monospace; font-size: 11px; }
  .num { font-family: monospace; }
  .muted { color: var(--muted); }
  .section-title { font-size: 13px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 12px; }
  .table-card { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 20px; margin-bottom: 24px; overflow-x: auto; }

  footer { border-top: 1px solid var(--border); padding: 20px 24px; margin-top: 8px; }
  .footer-content { max-width: 1400px; margin: 0 auto; }
  .footer-content p { color: var(--muted); font-size: 12px; line-height: 1.7; margin-bottom: 4px; }
  .footer-content p:last-child { margin-bottom: 0; }
  .footer-content a { color: var(--blue); text-decoration: none; }
  .footer-content a:hover { text-decoration: underline; }

  .cache-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 4px; vertical-align: middle; }
  .cache-green  { background: #4ade80; }
  .cache-yellow { background: #fbbf24; }
  .cache-red    { background: #f87171; }
  .cost-alert   { background: rgba(248,113,113,0.08); }
  .clickable    { cursor: pointer; }
  .clickable:hover td { background: rgba(217,119,87,0.06); }

  #tab-bar { background: var(--card); border-bottom: 1px solid var(--border); padding: 0 24px; display: flex; gap: 0; }
  .tab-btn { padding: 12px 20px; background: none; border: none; border-bottom: 2px solid transparent; color: var(--muted); font-size: 13px; font-weight: 600; cursor: pointer; transition: color 0.15s, border-color 0.15s; text-transform: uppercase; letter-spacing: 0.04em; }
  .tab-btn:hover { color: var(--text); }
  .tab-btn.active { color: var(--accent); border-bottom-color: var(--accent); }

  .insight-panel { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 20px; margin-bottom: 20px; }
  .insight-panel h2 { font-size: 14px; font-weight: 600; color: var(--text); margin-bottom: 14px; }
  .insight-badges { display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 14px; }
  .insight-badge { padding: 6px 14px; border-radius: 6px; font-size: 12px; font-weight: 600; background: rgba(79,142,247,0.1); color: var(--blue); }
  .insight-badge.green { background: rgba(74,222,128,0.1); color: var(--green); }
  .insight-badge.red { background: rgba(248,113,113,0.1); color: #f87171; }
  .insight-badge.accent { background: rgba(217,119,87,0.1); color: var(--accent); }
  .insight-recommendation { background: rgba(79,142,247,0.06); border: 1px solid rgba(79,142,247,0.15); border-radius: 6px; padding: 12px 16px; margin-top: 14px; font-size: 12px; line-height: 1.6; color: var(--muted); }
  .insight-recommendation strong { color: var(--text); }
  .insight-empty { color: var(--muted); font-size: 13px; padding: 20px; text-align: center; }
  .insight-delta { display: inline-flex; align-items: center; gap: 4px; font-weight: 700; }
  .insight-delta.up { color: #f87171; }
  .insight-delta.down { color: #4ade80; }
  .insight-chart-wrap { position: relative; height: 240px; margin-bottom: 14px; }

  .modal-overlay { display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.7); z-index: 1000; overflow-y: auto; padding: 40px 20px; }
  .modal-overlay.active { display: block; }
  .modal-content { background: var(--bg); border: 1px solid var(--border); border-radius: 12px; max-width: 1200px; margin: 0 auto; padding: 28px; position: relative; }
  .modal-close { position: absolute; top: 16px; right: 20px; background: none; border: none; color: var(--muted); font-size: 24px; cursor: pointer; line-height: 1; }
  .modal-close:hover { color: var(--text); }
  .modal-header { margin-bottom: 20px; }
  .modal-header h2 { font-size: 16px; font-weight: 600; color: var(--accent); margin-bottom: 4px; }
  .modal-header .modal-sub { color: var(--muted); font-size: 12px; }
  .modal-stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 12px; margin-bottom: 20px; }
  .modal-stat { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 12px; }
  .modal-stat .label { color: var(--muted); font-size: 10px; text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 4px; }
  .modal-stat .value { font-size: 18px; font-weight: 700; }
  .modal-stat .sub { color: var(--muted); font-size: 10px; margin-top: 2px; }
  .modal-chart { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 16px; margin-bottom: 20px; }
  .modal-chart h3 { font-size: 12px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 12px; }
  .modal-chart-wrap { height: 200px; position: relative; }

  @media (max-width: 768px) { .charts-grid { grid-template-columns: 1fr; } .chart-card.wide { grid-column: 1; } }
</style>
</head>
<body>
<header>
  <h1>Claude Code Usage Dashboard</h1>
  <div class="meta" id="meta">Loading...</div>
</header>

<div id="filter-bar">
  <div class="filter-label">Models</div>
  <div id="model-checkboxes"></div>
  <button class="filter-btn" onclick="selectAllModels()">All</button>
  <button class="filter-btn" onclick="clearAllModels()">None</button>
  <div class="filter-sep"></div>
  <div class="filter-label">Range</div>
  <div class="range-group">
    <button class="range-btn" data-range="today" onclick="setRange('today')">Today</button>
    <button class="range-btn" data-range="yday"  onclick="setRange('yday')">Yesterday</button>
    <button class="range-btn" data-range="24h"   onclick="setRange('24h')">24h</button>
    <button class="range-btn" data-range="7d"    onclick="setRange('7d')">7d</button>
    <button class="range-btn" data-range="30d"   onclick="setRange('30d')">30d</button>
    <button class="range-btn" data-range="90d"   onclick="setRange('90d')">90d</button>
    <button class="range-btn" data-range="all"   onclick="setRange('all')">All</button>
  </div>
</div>

<nav id="tab-bar">
  <button class="tab-btn active" data-tab="dashboard" onclick="switchTab('dashboard')">Dashboard</button>
  <button class="tab-btn" data-tab="insights" onclick="switchTab('insights')">Cost Insights</button>
</nav>

<div class="container" id="dashboard-container">
  <div class="stats-row" id="stats-row"></div>
  <div class="charts-grid">
    <div class="chart-card wide">
      <h2 id="daily-chart-title">Daily Token Usage</h2>
      <div class="chart-wrap tall"><canvas id="chart-daily"></canvas></div>
    </div>
    <div class="chart-card">
      <h2>By Model</h2>
      <div class="chart-wrap"><canvas id="chart-model"></canvas></div>
    </div>
    <div class="chart-card">
      <h2>Top Projects by Tokens</h2>
      <div class="chart-wrap"><canvas id="chart-project"></canvas></div>
    </div>
  </div>
  <div class="table-card">
    <div class="section-title">Most Expensive Turns</div>
    <table>
      <thead><tr>
        <th>Session</th><th>Project</th><th>Prompt</th><th>Time</th><th>Model</th>
        <th>Tool</th><th>Input</th><th>Output</th><th>Cache %</th><th>Est. Cost</th>
      </tr></thead>
      <tbody id="expensive-turns-body"></tbody>
    </table>
  </div>
  <div class="table-card">
    <div class="section-title">Recent Sessions</div>
    <table>
      <thead><tr>
        <th>Session</th><th>Project</th><th>Last Active</th><th>Duration</th>
        <th>Model</th><th>Turns</th><th>Input</th><th>Output</th><th>Cache %</th><th>Est. Cost</th>
      </tr></thead>
      <tbody id="sessions-body"></tbody>
    </table>
  </div>
  <div class="table-card">
    <div class="section-title">Cost by Model</div>
    <table>
      <thead><tr>
        <th>Model</th><th>Turns</th><th>Input</th><th>Output</th>
        <th>Cache Read</th><th>Cache Creation</th><th>Est. Cost</th><th>Bedrock Est.</th><th>Savings</th>
      </tr></thead>
      <tbody id="model-cost-body"></tbody>
    </table>
  </div>
</div>

<div id="insights-container" class="container" style="display:none">
  <div id="insight-hotspots" class="insight-panel"></div>
  <div id="insight-trend" class="insight-panel"></div>
  <div id="insight-projects" class="insight-panel"></div>
  <div id="insight-cache" class="insight-panel"></div>
  <div id="insight-model-opt" class="insight-panel"></div>
  <div id="insight-high-output" class="insight-panel"></div>
</div>

<div class="modal-overlay" id="session-modal">
  <div class="modal-content">
    <button class="modal-close" onclick="closeModal()">&times;</button>
    <div id="modal-body">Loading...</div>
  </div>
</div>

<footer>
  <div class="footer-content">
    <p>Cost estimates based on Anthropic API pricing (<a href="https://claude.com/pricing#api" target="_blank">claude.com/pricing#api</a>) as of April 2026. Only models containing <em>opus</em>, <em>sonnet</em>, or <em>haiku</em> in the name are included in cost calculations. Actual costs for Max/Pro subscribers differ from API pricing.</p>
    <p>
      GitHub: <a href="https://github.com/phuryn/claude-usage" target="_blank">https://github.com/phuryn/claude-usage</a>
      &nbsp;&middot;&nbsp;
      Created by: <a href="https://www.productcompass.pm" target="_blank">The Product Compass Newsletter</a>
      &nbsp;&middot;&nbsp;
      License: MIT
    </p>
  </div>
</footer>

<script>
// ── State ──────────────────────────────────────────────────────────────────
let rawData = null;
let selectedModels = new Set();
let selectedRange = '30d';
let charts = {};
let activeTab = 'dashboard';
let insightData = null;
let insightCharts = {};

// ── Pricing (Anthropic API, April 2026) ────────────────────────────────────
const PRICING = {
  'claude-opus-4-6':   { input: 6.15,  output: 30.75, cache_write: 7.69, cache_read: 0.61 },
  'claude-opus-4-5':   { input: 6.15,  output: 30.75, cache_write: 7.69, cache_read: 0.61 },
  'claude-sonnet-4-6': { input: 3.69,  output: 18.45, cache_write: 4.61, cache_read: 0.37 },
  'claude-sonnet-4-5': { input: 3.69,  output: 18.45, cache_write: 4.61, cache_read: 0.37 },
  'claude-haiku-4-5':  { input: 1.23,  output:  6.15, cache_write: 1.54, cache_read: 0.12 },
  'claude-haiku-4-6':  { input: 1.23,  output:  6.15, cache_write: 1.54, cache_read: 0.12 },
};

function isBillable(model) {
  if (!model) return false;
  const m = model.toLowerCase();
  return m.includes('opus') || m.includes('sonnet') || m.includes('haiku');
}

function getPricing(model) {
  if (!model) return null;
  if (PRICING[model]) return PRICING[model];
  for (const key of Object.keys(PRICING)) {
    if (model.startsWith(key)) return PRICING[key];
  }
  const m = model.toLowerCase();
  if (m.includes('opus'))   return PRICING['claude-opus-4-6'];
  if (m.includes('sonnet')) return PRICING['claude-sonnet-4-6'];
  if (m.includes('haiku'))  return PRICING['claude-haiku-4-5'];
  return null;
}

function calcCost(model, inp, out, cacheRead, cacheCreation) {
  if (!isBillable(model)) return 0;
  const p = getPricing(model);
  if (!p) return 0;
  return (
    inp           * p.input       / 1e6 +
    out           * p.output      / 1e6 +
    cacheRead     * p.cache_read  / 1e6 +
    cacheCreation * p.cache_write / 1e6
  );
}

// ── Bedrock Pricing (April 2026) ────────────────────────────────────────────
const BEDROCK_PRICING = {
  'claude-opus-4-6':   { input: 5.00,  output: 25.00, cache_write: 6.25, cache_read: 0.50 },
  'claude-opus-4-5':   { input: 5.00,  output: 25.00, cache_write: 6.25, cache_read: 0.50 },
  'claude-sonnet-4-6': { input: 3.00,  output: 15.00, cache_write: 3.75, cache_read: 0.30 },
  'claude-sonnet-4-5': { input: 3.00,  output: 15.00, cache_write: 3.75, cache_read: 0.30 },
  'claude-haiku-4-5':  { input: 1.00,  output:  5.00, cache_write: 1.25, cache_read: 0.10 },
  'claude-haiku-4-6':  { input: 1.00,  output:  5.00, cache_write: 1.25, cache_read: 0.10 },
};

function getBedrockPricing(model) {
  if (!model) return null;
  if (BEDROCK_PRICING[model]) return BEDROCK_PRICING[model];
  for (const key of Object.keys(BEDROCK_PRICING)) {
    if (model.startsWith(key)) return BEDROCK_PRICING[key];
  }
  const m = model.toLowerCase();
  if (m.includes('opus'))   return BEDROCK_PRICING['claude-opus-4-6'];
  if (m.includes('sonnet')) return BEDROCK_PRICING['claude-sonnet-4-6'];
  if (m.includes('haiku'))  return BEDROCK_PRICING['claude-haiku-4-5'];
  return null;
}

function calcBedrockCost(model, inp, out, cacheRead, cacheCreation) {
  if (!isBillable(model)) return 0;
  const p = getBedrockPricing(model);
  if (!p) return 0;
  return (
    inp           * p.input       / 1e6 +
    out           * p.output      / 1e6 +
    cacheRead     * p.cache_read  / 1e6 +
    cacheCreation * p.cache_write / 1e6
  );
}

// ── Cache helpers ──────────────────────────────────────────────────────────
// ── Hours calculation ────────────────────────────────────────────────────
function calcHours(sessions, rangeStart, rangeEnd) {
  // Clamp session intervals to the selected date range so that a session
  // spanning multiple days only counts the portion inside the range.
  const clampStart = rangeStart ? new Date(rangeStart + 'T00:00:00Z') : null;
  const clampEnd   = rangeEnd   ? new Date(rangeEnd + 'T23:59:59.999Z') : null;
  let totalSessionSecs = 0;
  const intervals = [];
  for (const s of sessions) {
    if (!s.first_ts || !s.last_ts) continue;
    let t1 = new Date(s.first_ts), t2 = new Date(s.last_ts);
    if (isNaN(t1) || isNaN(t2)) continue;
    // Clamp to range
    if (clampStart && t1 < clampStart) t1 = clampStart;
    if (clampEnd && t2 > clampEnd) t2 = clampEnd;
    const dur = (t2 - t1) / 1000;
    if (dur <= 0) continue;
    totalSessionSecs += dur;
    intervals.push([t1, t2]);
  }
  // Merge overlapping intervals for wall-clock time
  intervals.sort((a, b) => a[0] - b[0]);
  let wallSecs = 0;
  if (intervals.length > 0) {
    let [ms, me] = intervals[0];
    for (let i = 1; i < intervals.length; i++) {
      const [s, e] = intervals[i];
      if (s <= me) { me = e > me ? e : me; }
      else { wallSecs += (me - ms) / 1000; ms = s; me = e; }
    }
    wallSecs += (me - ms) / 1000;
  }
  return { sessionHours: totalSessionSecs / 3600, wallClockHours: wallSecs / 3600 };
}

function cacheEfficiency(cacheRead, input, cacheCreation) {
  const total = input + cacheRead + cacheCreation;
  return total > 0 ? (cacheRead / total * 100) : 0;
}

function cacheColor(rate) {
  if (rate >= 70) return 'cache-green';
  if (rate >= 30) return 'cache-yellow';
  return 'cache-red';
}

function calcCacheSavings(model, cacheRead) {
  const p = getPricing(model);
  if (!p) return 0;
  return cacheRead * (p.input - p.cache_read) / 1e6;
}

// ── Formatting ─────────────────────────────────────────────────────────────
function fmt(n) {
  if (n >= 1e9) return (n/1e9).toFixed(2)+'B';
  if (n >= 1e6) return (n/1e6).toFixed(2)+'M';
  if (n >= 1e3) return (n/1e3).toFixed(1)+'K';
  return n.toLocaleString();
}
function fmtCost(c)    { return '$' + c.toFixed(4); }
function fmtCostBig(c) { return '$' + c.toFixed(2); }

// ── Chart colors ───────────────────────────────────────────────────────────
const TOKEN_COLORS = {
  input:          'rgba(79,142,247,0.8)',
  output:         'rgba(167,139,250,0.8)',
  cache_read:     'rgba(74,222,128,0.6)',
  cache_creation: 'rgba(251,191,36,0.6)',
};
const MODEL_COLORS = ['#d97757','#4f8ef7','#4ade80','#a78bfa','#fbbf24','#f472b6','#34d399','#60a5fa'];

// ── Time range ─────────────────────────────────────────────────────────────
const RANGE_LABELS = { 'today': 'Today', 'yday': 'Yesterday', '24h': 'Last 24 Hours', '7d': 'Last 7 Days', '30d': 'Last 30 Days', '90d': 'Last 90 Days', 'all': 'All Time' };
const RANGE_TICKS  = { 'today': 1, 'yday': 1, '24h': 2, '7d': 7, '30d': 15, '90d': 13, 'all': 12 };

function getRangeBounds(range) {
  // Returns {start, end} where end is null for open-ended ranges
  if (range === 'all') return { start: null, end: null };
  const now = new Date();
  const today = now.toISOString().slice(0, 10);
  if (range === 'today') return { start: today, end: null };
  if (range === 'yday') {
    const y = new Date(now); y.setDate(y.getDate() - 1);
    const yday = y.toISOString().slice(0, 10);
    return { start: yday, end: yday };
  }
  if (range === '24h') {
    const d = new Date(now.getTime() - 24*60*60*1000);
    return { start: d.toISOString().slice(0, 10), end: null };
  }
  const days = range === '7d' ? 7 : range === '30d' ? 30 : 90;
  const d = new Date(); d.setDate(d.getDate() - days);
  return { start: d.toISOString().slice(0, 10), end: null };
}

function readURLRange() {
  const p = new URLSearchParams(window.location.search).get('range');
  return ['today', 'yday', '24h', '7d', '30d', '90d', 'all'].includes(p) ? p : '30d';
}

function setRange(range) {
  selectedRange = range;
  document.querySelectorAll('.range-btn').forEach(btn =>
    btn.classList.toggle('active', btn.dataset.range === range)
  );
  updateURL();
  applyFilter();
}

// ── Model filter ───────────────────────────────────────────────────────────
function modelPriority(m) {
  const ml = m.toLowerCase();
  if (ml.includes('opus'))   return 0;
  if (ml.includes('sonnet')) return 1;
  if (ml.includes('haiku'))  return 2;
  return 3;
}

function readURLModels(allModels) {
  const param = new URLSearchParams(window.location.search).get('models');
  if (!param) return new Set(allModels.filter(m => isBillable(m)));
  const fromURL = new Set(param.split(',').map(s => s.trim()).filter(Boolean));
  return new Set(allModels.filter(m => fromURL.has(m)));
}

function isDefaultModelSelection(allModels) {
  const billable = allModels.filter(m => isBillable(m));
  if (selectedModels.size !== billable.length) return false;
  return billable.every(m => selectedModels.has(m));
}

function buildFilterUI(allModels) {
  const sorted = [...allModels].sort((a, b) => {
    const pa = modelPriority(a), pb = modelPriority(b);
    return pa !== pb ? pa - pb : a.localeCompare(b);
  });
  selectedModels = readURLModels(allModels);
  const container = document.getElementById('model-checkboxes');
  container.innerHTML = sorted.map(m => {
    const checked = selectedModels.has(m);
    return `<label class="model-cb-label ${checked ? 'checked' : ''}" data-model="${m}">
      <input type="checkbox" value="${m}" ${checked ? 'checked' : ''} onchange="onModelToggle(this)">
      ${m}
    </label>`;
  }).join('');
}

function onModelToggle(cb) {
  const label = cb.closest('label');
  if (cb.checked) { selectedModels.add(cb.value);    label.classList.add('checked'); }
  else            { selectedModels.delete(cb.value); label.classList.remove('checked'); }
  updateURL();
  applyFilter();
}

function selectAllModels() {
  document.querySelectorAll('#model-checkboxes input').forEach(cb => {
    cb.checked = true; selectedModels.add(cb.value); cb.closest('label').classList.add('checked');
  });
  updateURL(); applyFilter();
}

function clearAllModels() {
  document.querySelectorAll('#model-checkboxes input').forEach(cb => {
    cb.checked = false; selectedModels.delete(cb.value); cb.closest('label').classList.remove('checked');
  });
  updateURL(); applyFilter();
}

// ── URL persistence ────────────────────────────────────────────────────────
function updateURL() {
  const allModels = Array.from(document.querySelectorAll('#model-checkboxes input')).map(cb => cb.value);
  const params = new URLSearchParams();
  if (selectedRange !== '30d') params.set('range', selectedRange);
  if (!isDefaultModelSelection(allModels)) params.set('models', Array.from(selectedModels).join(','));
  const search = params.toString() ? '?' + params.toString() : '';
  history.replaceState(null, '', window.location.pathname + search);
}

// ── Aggregation & filtering ────────────────────────────────────────────────
function applyFilter() {
  if (!rawData) return;

  const bounds = getRangeBounds(selectedRange);
  const cutoff = bounds.start;
  const endDate = bounds.end;

  // Filter daily rows by model + date range
  const filteredDaily = rawData.daily_by_model.filter(r =>
    selectedModels.has(r.model) && (!cutoff || r.day >= cutoff) && (!endDate || r.day <= endDate)
  );

  // Daily chart: aggregate by day
  const dailyMap = {};
  for (const r of filteredDaily) {
    if (!dailyMap[r.day]) dailyMap[r.day] = { day: r.day, input: 0, output: 0, cache_read: 0, cache_creation: 0 };
    const d = dailyMap[r.day];
    d.input          += r.input;
    d.output         += r.output;
    d.cache_read     += r.cache_read;
    d.cache_creation += r.cache_creation;
  }
  const daily = Object.values(dailyMap).sort((a, b) => a.day.localeCompare(b.day));

  // By model: aggregate tokens + turns from daily data
  const modelMap = {};
  for (const r of filteredDaily) {
    if (!modelMap[r.model]) modelMap[r.model] = { model: r.model, input: 0, output: 0, cache_read: 0, cache_creation: 0, turns: 0, sessions: 0 };
    const m = modelMap[r.model];
    m.input          += r.input;
    m.output         += r.output;
    m.cache_read     += r.cache_read;
    m.cache_creation += r.cache_creation;
    m.turns          += r.turns;
  }

  // Filter sessions by model + date range
  const filteredSessions = rawData.sessions_all.filter(s =>
    selectedModels.has(s.model) && (!cutoff || s.last_date >= cutoff) && (!endDate || s.last_date <= endDate)
  );

  // Add session counts into modelMap
  for (const s of filteredSessions) {
    if (modelMap[s.model]) modelMap[s.model].sessions++;
  }

  const byModel = Object.values(modelMap).sort((a, b) => (b.input + b.output) - (a.input + a.output));

  // By project: aggregate from filtered sessions
  const projMap = {};
  for (const s of filteredSessions) {
    if (!projMap[s.project]) projMap[s.project] = { project: s.project, input: 0, output: 0, turns: 0 };
    projMap[s.project].input  += s.input;
    projMap[s.project].output += s.output;
    projMap[s.project].turns  += s.turns;
  }
  const byProject = Object.values(projMap).sort((a, b) => (b.input + b.output) - (a.input + a.output));

  // Totals
  const totalCost = byModel.reduce((s, m) => s + calcCost(m.model, m.input, m.output, m.cache_read, m.cache_creation), 0);
  const totalBedrockCost = byModel.reduce((s, m) => s + calcBedrockCost(m.model, m.input, m.output, m.cache_read, m.cache_creation), 0);
  const totalCacheRead = byModel.reduce((s, m) => s + m.cache_read, 0);
  const totalCacheCreation = byModel.reduce((s, m) => s + m.cache_creation, 0);
  const totalInput = byModel.reduce((s, m) => s + m.input, 0);
  const totalCacheSavings = byModel.reduce((s, m) => s + calcCacheSavings(m.model, m.cache_read), 0);
  const totalCacheRate = cacheEfficiency(totalCacheRead, totalInput, totalCacheCreation);
  const avgSessionCost = filteredSessions.length > 0 ? totalCost / filteredSessions.length : 0;

  const hours = calcHours(filteredSessions, cutoff, endDate);

  const totals = {
    sessions:       filteredSessions.length,
    turns:          byModel.reduce((s, m) => s + m.turns, 0),
    input:          totalInput,
    output:         byModel.reduce((s, m) => s + m.output, 0),
    cache_read:     totalCacheRead,
    cache_creation: totalCacheCreation,
    cost:           totalCost,
    bedrock_cost:   totalBedrockCost,
    cache_rate:     totalCacheRate,
    cache_savings:  totalCacheSavings,
    avg_session_cost: avgSessionCost,
    session_hours:    hours.sessionHours,
    wall_clock_hours: hours.wallClockHours,
  };

  // Filter top_turns by model + date range
  const filteredTopTurns = (rawData.top_turns || []).filter(t =>
    selectedModels.has(t.model) && (!cutoff || t.timestamp_date >= cutoff) && (!endDate || t.timestamp_date <= endDate)
  );

  // Update daily chart title
  document.getElementById('daily-chart-title').textContent = 'Daily Token Usage \u2014 ' + RANGE_LABELS[selectedRange];

  renderStats(totals);
  renderDailyChart(daily);
  renderModelChart(byModel);
  renderProjectChart(byProject);
  renderExpensiveTurns(filteredTopTurns);
  renderSessionsTable(filteredSessions.slice(0, 20));
  renderModelCostTable(byModel);

  insightData = { filteredSessions, filteredTopTurns, filteredDaily, daily, byModel, byProject, totals };
  if (activeTab === 'insights') renderInsights();
}

// ── Renderers ──────────────────────────────────────────────────────────────
function renderStats(t) {
  const rangeLabel = RANGE_LABELS[selectedRange].toLowerCase();
  const bedrockDiff = t.bedrock_cost - t.cost;
  const bedrockSub = bedrockDiff >= 0
    ? `${fmtCostBig(bedrockDiff)} more than API`
    : `${fmtCostBig(Math.abs(bedrockDiff))} less than API`;
  const stats = [
    { label: 'Sessions',         value: t.sessions.toLocaleString(), sub: rangeLabel },
    { label: 'Turns',            value: fmt(t.turns),                sub: rangeLabel },
    { label: 'Input Tokens',     value: fmt(t.input),                sub: rangeLabel },
    { label: 'Output Tokens',    value: fmt(t.output),               sub: rangeLabel },
    { label: 'Cache Hit Rate',   value: t.cache_rate.toFixed(1) + '%', sub: 'of input from cache', color: t.cache_rate >= 70 ? '#4ade80' : t.cache_rate >= 30 ? '#fbbf24' : '#f87171' },
    { label: 'Cache Savings',    value: fmtCostBig(t.cache_savings), sub: 'vs full-price input', color: '#4ade80' },
    { label: 'Est. Cost',        value: fmtCostBig(t.cost),          sub: 'API pricing, Apr 2026', color: '#4ade80' },
    { label: 'Session Hours',    value: t.session_hours.toFixed(1) + 'h', sub: 'sum of all durations' },
    { label: 'Wall-Clock Hours', value: t.wall_clock_hours.toFixed(1) + 'h', sub: 'actual calendar time' },
    { label: 'Avg Session Cost', value: fmtCostBig(t.avg_session_cost), sub: rangeLabel },
    { label: 'Bedrock Est.',     value: fmtCostBig(t.bedrock_cost),  sub: bedrockSub, color: '#4f8ef7' },
  ];
  document.getElementById('stats-row').innerHTML = stats.map(s => `
    <div class="stat-card">
      <div class="label">${s.label}</div>
      <div class="value" style="${s.color ? 'color:' + s.color : ''}">${s.value}</div>
      ${s.sub ? `<div class="sub">${s.sub}</div>` : ''}
    </div>
  `).join('');
}

function renderDailyChart(daily) {
  const ctx = document.getElementById('chart-daily').getContext('2d');
  if (charts.daily) charts.daily.destroy();
  charts.daily = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: daily.map(d => d.day),
      datasets: [
        { label: 'Input',          data: daily.map(d => d.input),          backgroundColor: TOKEN_COLORS.input,          stack: 'tokens' },
        { label: 'Output',         data: daily.map(d => d.output),         backgroundColor: TOKEN_COLORS.output,         stack: 'tokens' },
        { label: 'Cache Read',     data: daily.map(d => d.cache_read),     backgroundColor: TOKEN_COLORS.cache_read,     stack: 'tokens' },
        { label: 'Cache Creation', data: daily.map(d => d.cache_creation), backgroundColor: TOKEN_COLORS.cache_creation, stack: 'tokens' },
      ]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { labels: { color: '#8892a4', boxWidth: 12 } } },
      scales: {
        x: { ticks: { color: '#8892a4', maxTicksLimit: RANGE_TICKS[selectedRange] }, grid: { color: '#2a2d3a' } },
        y: { ticks: { color: '#8892a4', callback: v => fmt(v) }, grid: { color: '#2a2d3a' } },
      }
    }
  });
}

function renderModelChart(byModel) {
  const ctx = document.getElementById('chart-model').getContext('2d');
  if (charts.model) charts.model.destroy();
  if (!byModel.length) { charts.model = null; return; }
  charts.model = new Chart(ctx, {
    type: 'doughnut',
    data: {
      labels: byModel.map(m => m.model),
      datasets: [{ data: byModel.map(m => m.input + m.output), backgroundColor: MODEL_COLORS, borderWidth: 2, borderColor: '#1a1d27' }]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { position: 'bottom', labels: { color: '#8892a4', boxWidth: 12, font: { size: 11 } } },
        tooltip: { callbacks: { label: ctx => ` ${ctx.label}: ${fmt(ctx.raw)} tokens` } }
      }
    }
  });
}

function renderProjectChart(byProject) {
  const top = byProject.slice(0, 10);
  const ctx = document.getElementById('chart-project').getContext('2d');
  if (charts.project) charts.project.destroy();
  if (!top.length) { charts.project = null; return; }
  charts.project = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: top.map(p => p.project.length > 22 ? '\u2026' + p.project.slice(-20) : p.project),
      datasets: [
        { label: 'Input',  data: top.map(p => p.input),  backgroundColor: TOKEN_COLORS.input },
        { label: 'Output', data: top.map(p => p.output), backgroundColor: TOKEN_COLORS.output },
      ]
    },
    options: {
      indexAxis: 'y', responsive: true, maintainAspectRatio: false,
      plugins: { legend: { labels: { color: '#8892a4', boxWidth: 12 } } },
      scales: {
        x: { ticks: { color: '#8892a4', callback: v => fmt(v) }, grid: { color: '#2a2d3a' } },
        y: { ticks: { color: '#8892a4', font: { size: 11 } }, grid: { color: '#2a2d3a' } },
      }
    }
  });
}

function renderExpensiveTurns(topTurns) {
  const withCost = topTurns.map(t => ({
    ...t,
    cost: calcCost(t.model, t.input, t.output, t.cache_read, t.cache_creation)
  })).sort((a, b) => b.cost - a.cost).slice(0, 10);

  document.getElementById('expensive-turns-body').innerHTML = withCost.map(t => {
    const cr = cacheEfficiency(t.cache_read, t.input, t.cache_creation);
    const alertClass = t.cost >= 0.50 ? ' cost-alert' : '';
    const costCell = isBillable(t.model)
      ? `<td class="cost">${fmtCost(t.cost)}</td>`
      : `<td class="cost-na">n/a</td>`;
    const promptShort = (t.prompt_preview || '').length > 60 ? t.prompt_preview.slice(0, 60) + '\u2026' : (t.prompt_preview || '-');
    const promptFull = (t.prompt_preview || '').replace(/"/g, '&quot;');
    return `<tr class="${alertClass} clickable" onclick="openSession('${t.full_session_id}')">
      <td class="muted" style="font-family:monospace">${t.session_id}&hellip;</td>
      <td>${t.project}</td>
      <td class="muted" style="max-width:240px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${promptFull}">${promptShort}</td>
      <td class="muted">${t.timestamp}</td>
      <td><span class="model-tag">${t.model}</span></td>
      <td class="muted">${t.tool_name || '-'}</td>
      <td class="num">${fmt(t.input)}</td>
      <td class="num">${fmt(t.output)}</td>
      <td class="num"><span class="cache-dot ${cacheColor(cr)}"></span>${cr.toFixed(0)}%</td>
      ${costCell}
    </tr>`;
  }).join('');
}

function renderSessionsTable(sessions) {
  document.getElementById('sessions-body').innerHTML = sessions.map(s => {
    const cost = calcCost(s.model, s.input, s.output, s.cache_read, s.cache_creation);
    const costCell = isBillable(s.model)
      ? `<td class="cost">${fmtCost(cost)}</td>`
      : `<td class="cost-na">n/a</td>`;
    const cr = cacheEfficiency(s.cache_read, s.input, s.cache_creation);
    const cc = cacheColor(cr);
    const alertClass = cost >= 2.00 ? ' cost-alert' : '';
    return `<tr class="clickable${alertClass}" onclick="openSession('${s.full_session_id}')">
      <td class="muted" style="font-family:monospace">${s.session_id}&hellip;</td>
      <td>${s.project}</td>
      <td class="muted">${s.last}</td>
      <td class="muted">${s.duration_min}m</td>
      <td><span class="model-tag">${s.model}</span></td>
      <td class="num">${s.turns}</td>
      <td class="num">${fmt(s.input)}</td>
      <td class="num">${fmt(s.output)}</td>
      <td class="num"><span class="cache-dot ${cc}"></span>${cr.toFixed(0)}%</td>
      ${costCell}
    </tr>`;
  }).join('');
}

function renderModelCostTable(byModel) {
  document.getElementById('model-cost-body').innerHTML = byModel.map(m => {
    const cost = calcCost(m.model, m.input, m.output, m.cache_read, m.cache_creation);
    const bedrock = calcBedrockCost(m.model, m.input, m.output, m.cache_read, m.cache_creation);
    const billable = isBillable(m.model);
    const costCell = billable ? `<td class="cost">${fmtCost(cost)}</td>` : `<td class="cost-na">n/a</td>`;
    const bedrockCell = billable ? `<td class="num" style="color:#4f8ef7">${fmtCost(bedrock)}</td>` : `<td class="cost-na">n/a</td>`;
    const diff = bedrock - cost;
    const savingsCell = billable
      ? `<td class="num" style="color:${diff >= 0 ? '#f87171' : '#4ade80'}">${diff >= 0 ? '+' : ''}${fmtCost(diff)}</td>`
      : `<td class="cost-na">n/a</td>`;
    return `<tr>
      <td><span class="model-tag">${m.model}</span></td>
      <td class="num">${fmt(m.turns)}</td>
      <td class="num">${fmt(m.input)}</td>
      <td class="num">${fmt(m.output)}</td>
      <td class="num">${fmt(m.cache_read)}</td>
      <td class="num">${fmt(m.cache_creation)}</td>
      ${costCell}
      ${bedrockCell}
      ${savingsCell}
    </tr>`;
  }).join('');
}

// ── Tab switching ───────────────────────────────────────────────────────────
function switchTab(tab) {
  activeTab = tab;
  document.querySelectorAll('.tab-btn').forEach(btn =>
    btn.classList.toggle('active', btn.dataset.tab === tab)
  );
  document.getElementById('dashboard-container').style.display = tab === 'dashboard' ? '' : 'none';
  document.getElementById('insights-container').style.display = tab === 'insights' ? '' : 'none';
  if (tab === 'insights' && insightData) renderInsights();
}

// ── Cost Insights ───────────────────────────────────────────────────────────
function buildProjectMap(sessions) {
  const projMap = {};
  for (const s of sessions) {
    if (!projMap[s.project]) projMap[s.project] = { project: s.project, sessions: 0, turns: 0, input: 0, output: 0, cache_read: 0, cache_creation: 0, cost: 0, opusCost: 0, models: new Set() };
    const p = projMap[s.project];
    p.sessions++; p.turns += s.turns;
    p.input += s.input; p.output += s.output;
    p.cache_read += s.cache_read; p.cache_creation += s.cache_creation;
    const cost = calcCost(s.model, s.input, s.output, s.cache_read, s.cache_creation);
    p.cost += cost;
    if (s.model.toLowerCase().includes('opus')) p.opusCost += cost;
    p.models.add(s.model);
  }
  return Object.values(projMap);
}

function renderInsights() {
  if (!insightData) return;
  insightData.projectMap = buildProjectMap(insightData.filteredSessions);
  renderCostHotspots();
  renderCostTrend();
  renderProjectCostBreakdown();
  renderCacheEfficiency();
  renderModelOptimization();
  renderHighOutputTurns();
}

function renderCostHotspots() {
  const panel = document.getElementById('insight-hotspots');
  const { filteredSessions, filteredTopTurns, totals } = insightData;

  const sessionsWithCost = filteredSessions.map(s => ({
    ...s, cost: calcCost(s.model, s.input, s.output, s.cache_read, s.cache_creation)
  })).sort((a, b) => b.cost - a.cost);

  const top5Sessions = sessionsWithCost.slice(0, 5);
  const top5Cost = top5Sessions.reduce((s, x) => s + x.cost, 0);
  const top5Pct = totals.cost > 0 ? (top5Cost / totals.cost * 100) : 0;

  const turnsWithCost = filteredTopTurns.map(t => ({
    ...t, cost: calcCost(t.model, t.input, t.output, t.cache_read, t.cache_creation)
  })).sort((a, b) => b.cost - a.cost);
  const top5Turns = turnsWithCost.slice(0, 5);

  const costliest = top5Sessions[0];

  let html = '<h2>Cost Hotspots</h2>';
  html += '<div class="insight-badges">';
  html += '<div class="insight-badge green">Total Spend: ' + fmtCostBig(totals.cost) + '</div>';
  html += '<div class="insight-badge accent">Top 5 Sessions: ' + fmtCostBig(top5Cost) + ' (' + top5Pct.toFixed(1) + '%)</div>';
  if (costliest) html += '<div class="insight-badge red">Costliest: ' + costliest.session_id + '&hellip; &mdash; ' + fmtCostBig(costliest.cost) + '</div>';
  html += '</div>';

  html += '<div class="section-title" style="margin-top:12px">Top 5 Most Expensive Sessions</div>';
  html += '<table><thead><tr><th>Session</th><th>Date</th><th>Project</th><th>Model</th><th>Turns</th><th>Duration</th><th>Est. Cost</th></tr></thead><tbody>';
  for (const s of top5Sessions) {
    html += '<tr class="clickable" onclick="openSession(\'' + s.full_session_id + '\')">';
    html += '<td class="muted" style="font-family:monospace">' + s.session_id + '&hellip;</td>';
    html += '<td class="muted">' + s.last_date + '</td>';
    html += '<td>' + s.project + '</td>';
    html += '<td><span class="model-tag">' + s.model + '</span></td>';
    html += '<td class="num">' + s.turns + '</td>';
    html += '<td class="muted">' + s.duration_min + 'm</td>';
    html += '<td class="cost">' + fmtCost(s.cost) + '</td>';
    html += '</tr>';
  }
  html += '</tbody></table>';

  html += '<div class="section-title" style="margin-top:18px">Top 5 Most Expensive Turns</div>';
  html += '<table><thead><tr><th>Session</th><th>Date</th><th>Prompt</th><th>Project</th><th>Model</th><th>Tool</th><th>Input</th><th>Output</th><th>Est. Cost</th></tr></thead><tbody>';
  for (const t of top5Turns) {
    const preview = t.prompt_preview ? t.prompt_preview.slice(0, 60) + (t.prompt_preview.length > 60 ? '&hellip;' : '') : '-';
    html += '<tr class="clickable" onclick="openSession(\'' + t.full_session_id + '\')">';
    html += '<td class="muted" style="font-family:monospace">' + t.session_id + '&hellip;</td>';
    html += '<td class="muted">' + t.timestamp_date + '</td>';
    html += '<td class="muted" title="' + (t.prompt_preview || '').replace(/"/g, '&quot;') + '">' + preview + '</td>';
    html += '<td>' + t.project + '</td>';
    html += '<td><span class="model-tag">' + t.model + '</span></td>';
    html += '<td class="muted">' + (t.tool_name || '-') + '</td>';
    html += '<td class="num">' + fmt(t.input) + '</td>';
    html += '<td class="num">' + fmt(t.output) + '</td>';
    html += '<td class="cost">' + fmtCost(t.cost) + '</td>';
    html += '</tr>';
  }
  html += '</tbody></table>';

  if (top5Sessions.length > 0) {
    html += '<div class="insight-recommendation">';
    html += '<strong>Recommendation:</strong> Your top 5 sessions account for ' + fmtCostBig(top5Cost) + ' (' + top5Pct.toFixed(1) + '% of spend). Break large tasks into smaller sessions to improve cache reuse and reduce per-session cost.';
    html += '</div>';
  }

  panel.innerHTML = html;
}

function renderCostTrend() {
  const panel = document.getElementById('insight-trend');
  const { daily, filteredDaily } = insightData;

  if (daily.length < 2) {
    panel.innerHTML = '<h2>Cost Trend &mdash; Period Comparison</h2><div class="insight-empty">Need at least 2 days of data for trend comparison.</div>';
    return;
  }

  const costByDay = {};
  for (const r of filteredDaily) {
    costByDay[r.day] = (costByDay[r.day] || 0) + calcCost(r.model, r.input, r.output, r.cache_read, r.cache_creation);
  }
  const dailyWithCost = daily.map(d => ({ day: d.day, cost: costByDay[d.day] || 0 }));

  const mid = Math.floor(dailyWithCost.length / 2);
  const prevPeriod = dailyWithCost.slice(0, mid);
  const currPeriod = dailyWithCost.slice(mid);
  const prevTotal = prevPeriod.reduce((s, d) => s + d.cost, 0);
  const currTotal = currPeriod.reduce((s, d) => s + d.cost, 0);
  const prevAvg = prevPeriod.length > 0 ? prevTotal / prevPeriod.length : 0;
  const currAvg = currPeriod.length > 0 ? currTotal / currPeriod.length : 0;
  const deltaPct = prevTotal > 0 ? ((currTotal - prevTotal) / prevTotal * 100) : 0;
  const isUp = deltaPct > 0;
  const isFlat = Math.abs(deltaPct) < 0.05;

  let html = '<h2>Cost Trend &mdash; Period Comparison</h2>';
  html += '<div class="insight-badges">';
  html += '<div class="insight-badge">Previous: ' + fmtCostBig(prevTotal) + ' (' + prevPeriod.length + 'd avg: ' + fmtCostBig(prevAvg) + '/day)</div>';
  html += '<div class="insight-badge">Current: ' + fmtCostBig(currTotal) + ' (' + currPeriod.length + 'd avg: ' + fmtCostBig(currAvg) + '/day)</div>';
  if (isFlat) {
    html += '<div class="insight-badge">\u2192 0.0%</div>';
  } else {
    html += '<div class="insight-badge ' + (isUp ? 'red' : 'green') + '">';
    html += '<span class="insight-delta ' + (isUp ? 'up' : 'down') + '">' + (isUp ? '\u2191' : '\u2193') + ' ' + Math.abs(deltaPct).toFixed(1) + '%</span>';
    html += '</div>';
  }
  html += '</div>';
  html += '<div class="insight-chart-wrap"><canvas id="insight-trend-chart"></canvas></div>';

  panel.innerHTML = html;

  const ctx = document.getElementById('insight-trend-chart').getContext('2d');
  if (insightCharts.trend) insightCharts.trend.destroy();
  const bgColors = dailyWithCost.map((d, i) => i < mid ? 'rgba(79,142,247,0.6)' : 'rgba(217,119,87,0.6)');
  insightCharts.trend = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: dailyWithCost.map(d => d.day),
      datasets: [
        { label: 'Daily Cost', data: dailyWithCost.map(d => d.cost), backgroundColor: bgColors },
        { label: 'Prev Avg', data: dailyWithCost.map((d, i) => i < mid ? prevAvg : null), type: 'line', borderColor: 'rgba(79,142,247,0.8)', borderDash: [5,3], pointRadius: 0, borderWidth: 2, fill: false },
        { label: 'Curr Avg', data: dailyWithCost.map((d, i) => i >= mid ? currAvg : null), type: 'line', borderColor: 'rgba(217,119,87,0.8)', borderDash: [5,3], pointRadius: 0, borderWidth: 2, fill: false },
      ]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { labels: { color: '#8892a4', boxWidth: 12 } } },
      scales: {
        x: { ticks: { color: '#8892a4', maxTicksLimit: 12 }, grid: { color: '#2a2d3a' } },
        y: { ticks: { color: '#8892a4', callback: v => '$' + v.toFixed(2) }, grid: { color: '#2a2d3a' } },
      }
    }
  });
}

function renderProjectCostBreakdown() {
  const panel = document.getElementById('insight-projects');
  const projects = [...insightData.projectMap].sort((a, b) => b.cost - a.cost);
  const top10 = projects.slice(0, 10);

  if (top10.length === 0) {
    panel.innerHTML = '<h2>Per-Project Cost Breakdown</h2><div class="insight-empty">No project data available.</div>';
    return;
  }

  let html = '<h2>Per-Project Cost Breakdown</h2>';
  html += '<div class="insight-chart-wrap"><canvas id="insight-project-chart"></canvas></div>';
  html += '<table><thead><tr><th>Project</th><th>Sessions</th><th>Turns</th><th>Est. Cost</th><th>Cache %</th><th>Models</th></tr></thead><tbody>';
  for (const p of top10) {
    const cr = cacheEfficiency(p.cache_read, p.input, p.cache_creation);
    html += '<tr>';
    html += '<td>' + p.project + '</td>';
    html += '<td class="num">' + p.sessions + '</td>';
    html += '<td class="num">' + p.turns + '</td>';
    html += '<td class="cost">' + fmtCost(p.cost) + '</td>';
    html += '<td class="num"><span class="cache-dot ' + cacheColor(cr) + '"></span>' + cr.toFixed(0) + '%</td>';
    html += '<td>' + [...p.models].map(m => '<span class="model-tag" style="margin-right:4px">' + m + '</span>').join('') + '</td>';
    html += '</tr>';
  }
  html += '</tbody></table>';

  const lowCacheProj = top10.find(p => {
    const cr = cacheEfficiency(p.cache_read, p.input, p.cache_creation);
    return cr < 70 && p.cost > 0.50;
  });
  if (lowCacheProj) {
    const cr = cacheEfficiency(lowCacheProj.cache_read, lowCacheProj.input, lowCacheProj.cache_creation);
    html += '<div class="insight-recommendation">';
    html += '<strong>Recommendation:</strong> Project "' + lowCacheProj.project + '" has a cache hit rate of only ' + cr.toFixed(0) + '% with ' + fmtCostBig(lowCacheProj.cost) + ' in spend. Improving cache usage could significantly reduce costs for this project.';
    html += '</div>';
  }

  panel.innerHTML = html;

  const ctx = document.getElementById('insight-project-chart').getContext('2d');
  if (insightCharts.projects) insightCharts.projects.destroy();
  insightCharts.projects = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: top10.map(p => p.project.length > 25 ? '\u2026' + p.project.slice(-23) : p.project),
      datasets: [{ label: 'Cost', data: top10.map(p => p.cost), backgroundColor: 'rgba(217,119,87,0.7)' }]
    },
    options: {
      indexAxis: 'y', responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: {
        x: { ticks: { color: '#8892a4', callback: v => '$' + v.toFixed(2) }, grid: { color: '#2a2d3a' } },
        y: { ticks: { color: '#8892a4', font: { size: 11 } }, grid: { color: '#2a2d3a' } },
      }
    }
  });
}

function renderCacheEfficiency() {
  const panel = document.getElementById('insight-cache');

  const opportunities = insightData.projectMap.filter(p => {
    const cr = cacheEfficiency(p.cache_read, p.input, p.cache_creation);
    return cr < 70 && p.cost > 0.50;
  }).sort((a, b) => b.cost - a.cost);

  let html = '<h2>Cache Efficiency Opportunities</h2>';

  if (opportunities.length === 0) {
    html += '<div class="insight-empty">All projects with significant spend have cache hit rates above 70%.</div>';
    panel.innerHTML = html;
    return;
  }

  html += '<table><thead><tr><th>Project</th><th>Current Cache %</th><th>Spend</th><th>Potential Savings (to 70%)</th></tr></thead><tbody>';
  for (const p of opportunities) {
    const totalEq = p.input + p.cache_read + p.cache_creation;
    const currentRate = cacheEfficiency(p.cache_read, p.input, p.cache_creation);
    const targetCacheTokens = 0.70 * totalEq;
    const additionalCache = Math.max(0, targetCacheTokens - p.cache_read);
    // Estimate savings using first model's pricing
    let savingsPerToken = 0;
    const firstModel = [...p.models][0];
    if (firstModel) {
      const pr = getPricing(firstModel);
      if (pr) savingsPerToken = (pr.input - pr.cache_read) / 1e6;
    }
    const potentialSavings = additionalCache * savingsPerToken;
    html += '<tr>';
    html += '<td>' + p.project + '</td>';
    html += '<td class="num"><span class="cache-dot ' + cacheColor(currentRate) + '"></span>' + currentRate.toFixed(0) + '%</td>';
    html += '<td class="cost">' + fmtCost(p.cost) + '</td>';
    html += '<td class="cost">' + fmtCost(potentialSavings) + '</td>';
    html += '</tr>';
  }
  html += '</tbody></table>';

  html += '<div class="insight-recommendation">';
  html += '<strong>Tips to improve cache efficiency:</strong><br>';
  html += '&bull; Use longer sessions instead of starting fresh &mdash; cached context carries over between turns<br>';
  html += '&bull; Keep consistent system prompts across sessions in the same project<br>';
  html += '&bull; Avoid clearing context unnecessarily mid-session';
  html += '</div>';

  panel.innerHTML = html;
}

function renderModelOptimization() {
  const panel = document.getElementById('insight-model-opt');

  const sonnetModel = 'claude-sonnet-4-6';
  const opportunities = insightData.projectMap.filter(p => p.opusCost > 0.50).map(p => {
    const sonnetCost = calcCost(sonnetModel, p.input, p.output, p.cache_read, p.cache_creation);
    return { ...p, sonnetCost, savings: p.opusCost - sonnetCost };
  }).sort((a, b) => b.savings - a.savings);

  let html = '<h2>Model Optimization Opportunities</h2>';

  if (opportunities.length === 0) {
    html += '<div class="insight-empty">No significant Opus spend found that could be compared with Sonnet.</div>';
    panel.innerHTML = html;
    return;
  }

  const totalOpus = opportunities.reduce((s, p) => s + p.opusCost, 0);
  const totalSonnet = opportunities.reduce((s, p) => s + p.sonnetCost, 0);
  const totalSavings = totalOpus - totalSonnet;

  html += '<div class="insight-badges">';
  html += '<div class="insight-badge accent">Opus Spend: ' + fmtCostBig(totalOpus) + '</div>';
  html += '<div class="insight-badge">Sonnet Equivalent: ' + fmtCostBig(totalSonnet) + '</div>';
  html += '<div class="insight-badge green">Potential Savings: ' + fmtCostBig(totalSavings) + '</div>';
  html += '</div>';

  html += '<table><thead><tr><th>Project</th><th>Opus Cost</th><th>Sonnet Cost (est.)</th><th>Savings</th></tr></thead><tbody>';
  for (const p of opportunities) {
    html += '<tr>';
    html += '<td>' + p.project + '</td>';
    html += '<td class="cost">' + fmtCost(p.opusCost) + '</td>';
    html += '<td class="num" style="color:var(--blue)">' + fmtCost(p.sonnetCost) + '</td>';
    html += '<td class="cost">' + fmtCost(p.savings) + '</td>';
    html += '</tr>';
  }
  html += '</tbody></table>';

  html += '<div class="insight-recommendation">';
  html += '<strong>Note:</strong> These estimates show hypothetical costs if the same token volume were processed by Sonnet instead of Opus. Evaluate whether Sonnet\'s capabilities meet your project requirements before switching.';
  html += '</div>';

  panel.innerHTML = html;
}

function renderHighOutputTurns() {
  const panel = document.getElementById('insight-high-output');
  const { filteredTopTurns } = insightData;

  const highOutput = filteredTopTurns.map(t => {
    const cost = calcCost(t.model, t.input, t.output, t.cache_read, t.cache_creation);
    const outputRatio = t.input > 0 ? t.output / t.input : 0;
    const p = getPricing(t.model);
    const outputCost = p ? t.output * p.output / 1e6 : 0;
    return { ...t, cost, outputRatio, outputCost };
  }).filter(t => t.output > 10000 && t.outputRatio > 0.5 && t.cost > 0.10)
    .sort((a, b) => b.outputCost - a.outputCost)
    .slice(0, 10);

  let html = '<h2>High-Output Turns</h2>';

  if (highOutput.length === 0) {
    html += '<div class="insight-empty">No turns found with &gt;10K output tokens, output/input ratio &gt;0.5, and cost &gt;$0.10.</div>';
    panel.innerHTML = html;
    return;
  }

  html += '<table><thead><tr><th>Session</th><th>Project</th><th>Model</th><th>Tool</th><th>Input</th><th>Output</th><th>Out/In</th><th>Output Cost</th><th>Total Cost</th></tr></thead><tbody>';
  for (const t of highOutput) {
    html += '<tr class="clickable" onclick="openSession(\'' + t.full_session_id + '\')">';
    html += '<td class="muted" style="font-family:monospace">' + t.session_id + '&hellip;</td>';
    html += '<td>' + t.project + '</td>';
    html += '<td><span class="model-tag">' + t.model + '</span></td>';
    html += '<td class="muted">' + (t.tool_name || '-') + '</td>';
    html += '<td class="num">' + fmt(t.input) + '</td>';
    html += '<td class="num">' + fmt(t.output) + '</td>';
    html += '<td class="num">' + t.outputRatio.toFixed(2) + '</td>';
    html += '<td class="cost">' + fmtCost(t.outputCost) + '</td>';
    html += '<td class="cost">' + fmtCost(t.cost) + '</td>';
    html += '</tr>';
  }
  html += '</tbody></table>';

  html += '<div class="insight-recommendation">';
  html += '<strong>Tips to reduce output costs:</strong><br>';
  html += '&bull; Output tokens are 5x pricier than input for Opus &mdash; request concise responses<br>';
  html += '&bull; Ask for diffs or patches instead of full file rewrites<br>';
  html += '&bull; Use "be concise" or "brief response" in prompts for routine tasks';
  html += '</div>';

  panel.innerHTML = html;
}

// ── Session detail modal ────────────────────────────────────────────────────
let modalChart = null;

function openSession(fullId) {
  const modal = document.getElementById('session-modal');
  const body = document.getElementById('modal-body');
  body.innerHTML = '<div style="padding:40px;color:var(--muted)">Loading session details...</div>';
  modal.classList.add('active');

  fetch('/api/session/' + fullId.slice(0, 8))
    .then(r => r.json())
    .then(data => {
      if (data.error) { body.innerHTML = '<div style="padding:20px;color:#f87171">' + data.error + '</div>'; return; }
      renderModal(data);
    })
    .catch(e => { body.innerHTML = '<div style="padding:20px;color:#f87171">Error loading session</div>'; });
}

function closeModal() {
  document.getElementById('session-modal').classList.remove('active');
  if (modalChart) { modalChart.destroy(); modalChart = null; }
}

document.getElementById('session-modal').addEventListener('click', function(e) {
  if (e.target === this) closeModal();
});

function renderModal(d) {
  const body = document.getElementById('modal-body');
  const bedrockTotal = d.turns.reduce((s, t) => s + calcBedrockCost(t.model, t.input, t.output, t.cache_read, t.cache_creation), 0);
  const cacheSavings = d.turns.reduce((s, t) => s + calcCacheSavings(t.model, t.cache_read), 0);
  const avgCost = d.turn_count > 0 ? d.total_cost / d.turn_count : 0;

  body.innerHTML = `
    <div class="modal-header">
      <h2>Session ${d.session_id}&hellip;</h2>
      <div class="modal-sub">${d.project} &middot; ${d.model} &middot; ${d.first} to ${d.last} &middot; ${d.duration_min}m</div>
    </div>
    <div class="modal-stats">
      <div class="modal-stat"><div class="label">Total Cost</div><div class="value" style="color:#4ade80">${fmtCost(d.total_cost)}</div></div>
      <div class="modal-stat"><div class="label">Avg Cost/Turn</div><div class="value">${fmtCost(avgCost)}</div></div>
      <div class="modal-stat"><div class="label">Cache Hit Rate</div><div class="value" style="color:${d.cache_rate >= 70 ? '#4ade80' : d.cache_rate >= 30 ? '#fbbf24' : '#f87171'}">${d.cache_rate.toFixed(1)}%</div></div>
      <div class="modal-stat"><div class="label">Cache Savings</div><div class="value" style="color:#4ade80">${fmtCost(cacheSavings)}</div></div>
      <div class="modal-stat"><div class="label">Bedrock Est.</div><div class="value" style="color:#4f8ef7">${fmtCost(bedrockTotal)}</div></div>
      <div class="modal-stat"><div class="label">Turns</div><div class="value">${d.turn_count}</div></div>
    </div>
    <div class="modal-chart">
      <h3>Cumulative Cost</h3>
      <div class="modal-chart-wrap"><canvas id="modal-cost-chart"></canvas></div>
    </div>
    <div class="table-card" style="margin-bottom:0">
      <div class="section-title">Turn-by-Turn Breakdown</div>
      <table>
        <thead><tr>
          <th>#</th><th>Time</th><th>Prompt</th><th>Model</th><th>Tool</th>
          <th>Input</th><th>Output</th><th>Cache Read</th><th>Cache Create</th>
          <th>Cost</th><th>Cumulative</th><th>Cache %</th>
        </tr></thead>
        <tbody>${d.turns.map(t => {
          const cc = cacheColor(t.cache_rate);
          const alertClass = t.cost >= 0.50 ? ' cost-alert' : '';
          const mPrompt = (t.prompt_preview || '').length > 50 ? t.prompt_preview.slice(0, 50) + '\u2026' : (t.prompt_preview || '-');
          const mPromptFull = (t.prompt_preview || '').replace(/"/g, '&quot;');
          return `<tr class="${alertClass}">
            <td class="num">${t.num}</td>
            <td class="muted" style="font-size:11px">${t.timestamp.slice(11)}</td>
            <td class="muted" style="max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:11px" title="${mPromptFull}">${mPrompt}</td>
            <td><span class="model-tag">${t.model}</span></td>
            <td class="muted">${t.tool_name || '-'}</td>
            <td class="num">${fmt(t.input)}</td>
            <td class="num">${fmt(t.output)}</td>
            <td class="num">${fmt(t.cache_read)}</td>
            <td class="num">${fmt(t.cache_creation)}</td>
            <td class="cost">${fmtCost(t.cost)}</td>
            <td class="cost">${fmtCost(t.cumulative_cost)}</td>
            <td class="num"><span class="cache-dot ${cc}"></span>${t.cache_rate.toFixed(0)}%</td>
          </tr>`;
        }).join('')}</tbody>
      </table>
    </div>
  `;

  // Render cumulative cost chart
  const ctx = document.getElementById('modal-cost-chart').getContext('2d');
  if (modalChart) modalChart.destroy();
  modalChart = new Chart(ctx, {
    type: 'line',
    data: {
      labels: d.turns.map(t => '#' + t.num),
      datasets: [{
        label: 'Cumulative Cost',
        data: d.turns.map(t => t.cumulative_cost),
        borderColor: '#4ade80',
        backgroundColor: 'rgba(74,222,128,0.1)',
        fill: true,
        tension: 0.3,
        pointRadius: d.turns.length > 30 ? 0 : 3,
      }]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: { label: ctx => ' $' + ctx.raw.toFixed(4) } }
      },
      scales: {
        x: { ticks: { color: '#8892a4', maxTicksLimit: 15 }, grid: { color: '#2a2d3a' } },
        y: { ticks: { color: '#8892a4', callback: v => '$' + v.toFixed(2) }, grid: { color: '#2a2d3a' } },
      }
    }
  });
}

// ── Data loading ───────────────────────────────────────────────────────────
async function loadData() {
  try {
    const resp = await fetch('/api/data');
    const d = await resp.json();
    if (d.error) {
      document.body.innerHTML = '<div style="padding:40px;color:#f87171">' + d.error + '</div>';
      return;
    }
    document.getElementById('meta').textContent = 'Updated: ' + d.generated_at + ' \u00b7 Auto-refresh in 30s';

    const isFirstLoad = rawData === null;
    rawData = d;

    if (isFirstLoad) {
      // Restore range from URL, mark active button
      selectedRange = readURLRange();
      document.querySelectorAll('.range-btn').forEach(btn =>
        btn.classList.toggle('active', btn.dataset.range === selectedRange)
      );
      // Build model filter (reads URL for model selection too)
      buildFilterUI(d.all_models);
    }

    applyFilter();
  } catch(e) {
    console.error(e);
  }
}

loadData();
setInterval(loadData, 30000);
</script>
</body>
</html>
"""


class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML_TEMPLATE.encode("utf-8"))

        elif self.path == "/api/data":
            data = get_dashboard_data()
            body = json.dumps(data).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif self.path.startswith("/api/session/"):
            prefix = self.path.split("/api/session/")[1]
            data = get_session_detail(prefix)
            body = json.dumps(data).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        else:
            self.send_response(404)
            self.end_headers()


def serve(port=8080):
    server = HTTPServer(("localhost", port), DashboardHandler)
    print(f"Dashboard running at http://localhost:{port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    serve()
