"""
cli.py - Command-line interface for the Claude Code usage dashboard.

Commands:
  scan      - Scan JSONL files and update the database
  today     - Print today's usage summary
  stats     - Print all-time usage statistics
  dashboard - Scan + open browser + start dashboard server
"""

import sys
import sqlite3
from pathlib import Path
from datetime import datetime, date

DB_PATH = Path.home() / ".claude" / "usage.db"

PRICING = {
    "claude-opus-4-6":   {"input": 15.00, "output": 75.00},
    "claude-opus-4-5":   {"input": 15.00, "output": 75.00},
    "claude-sonnet-4-6": {"input":  3.00, "output": 15.00},
    "claude-sonnet-4-5": {"input":  3.00, "output": 15.00},
    "claude-haiku-4-5":  {"input":  0.80, "output":  4.00},
    "claude-haiku-4-6":  {"input":  0.80, "output":  4.00},
    "default":           {"input":  3.00, "output": 15.00},
}

BEDROCK_PRICING = {
    "claude-opus-4-6":   {"input": 5.00, "output": 25.00, "cache_write": 6.25, "cache_read": 0.50},
    "claude-opus-4-5":   {"input": 5.00, "output": 25.00, "cache_write": 6.25, "cache_read": 0.50},
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00, "cache_write": 3.75, "cache_read": 0.30},
    "claude-sonnet-4-5": {"input": 3.00, "output": 15.00, "cache_write": 3.75, "cache_read": 0.30},
    "claude-haiku-4-5":  {"input": 1.00, "output":  5.00, "cache_write": 1.25, "cache_read": 0.10},
    "claude-haiku-4-6":  {"input": 1.00, "output":  5.00, "cache_write": 1.25, "cache_read": 0.10},
    "default":           {"input": 3.00, "output": 15.00, "cache_write": 3.75, "cache_read": 0.30},
}

def get_pricing(model):
    if not model:
        return PRICING["default"]
    if model in PRICING:
        return PRICING[model]
    for key in PRICING:
        if key != "default" and model.startswith(key):
            return PRICING[key]
    return PRICING["default"]

def calc_cost(model, inp, out, cache_read, cache_creation):
    p = get_pricing(model)
    return (
        inp          * p["input"]  / 1_000_000 +
        out          * p["output"] / 1_000_000 +
        cache_read   * p["input"]  * 0.10 / 1_000_000 +
        cache_creation * p["input"] * 1.25 / 1_000_000
    )

def get_bedrock_pricing(model):
    if not model:
        return BEDROCK_PRICING["default"]
    if model in BEDROCK_PRICING:
        return BEDROCK_PRICING[model]
    for key in BEDROCK_PRICING:
        if key != "default" and model.startswith(key):
            return BEDROCK_PRICING[key]
    return BEDROCK_PRICING["default"]

def calc_bedrock_cost(model, inp, out, cache_read, cache_creation):
    p = get_bedrock_pricing(model)
    return (
        inp            * p["input"]       / 1_000_000 +
        out            * p["output"]      / 1_000_000 +
        cache_read     * p["cache_read"]  / 1_000_000 +
        cache_creation * p["cache_write"] / 1_000_000
    )

def fmt(n):
    if n >= 1_000_000:
        return f"{n/1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)

def fmt_cost(c):
    return f"${c:.4f}"

def hr(char="-", width=60):
    print(char * width)


def calc_hours(session_rows):
    """Calculate total session hours (sum of durations) and wall-clock hours (merged intervals).

    Returns (session_hours, wall_clock_hours).
    """
    intervals = []
    total_session_secs = 0

    for r in session_rows:
        first_ts = r["first_timestamp"] or ""
        last_ts = r["last_timestamp"] or ""
        if not first_ts or not last_ts:
            continue
        try:
            t1 = datetime.fromisoformat(first_ts.replace("Z", "+00:00"))
            t2 = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue
        duration = (t2 - t1).total_seconds()
        if duration <= 0:
            continue
        total_session_secs += duration
        intervals.append((t1, t2))

    # Merge overlapping intervals for wall-clock time
    wall_clock_secs = 0
    if intervals:
        intervals.sort(key=lambda x: x[0])
        merged_start, merged_end = intervals[0]
        for start, end in intervals[1:]:
            if start <= merged_end:
                merged_end = max(merged_end, end)
            else:
                wall_clock_secs += (merged_end - merged_start).total_seconds()
                merged_start, merged_end = start, end
        wall_clock_secs += (merged_end - merged_start).total_seconds()

    return total_session_secs / 3600, wall_clock_secs / 3600

def require_db():
    if not DB_PATH.exists():
        print("Database not found. Run: python cli.py scan")
        sys.exit(1)
    return sqlite3.connect(DB_PATH)


# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_scan():
    from scanner import scan, PROJECTS_DIR
    print(f"Scanning {PROJECTS_DIR} ...")
    scan()


def cmd_today():
    conn = require_db()
    conn.row_factory = sqlite3.Row
    today = date.today().isoformat()

    rows = conn.execute("""
        SELECT
            COALESCE(model, 'unknown') as model,
            SUM(input_tokens)          as inp,
            SUM(output_tokens)         as out,
            SUM(cache_read_tokens)     as cr,
            SUM(cache_creation_tokens) as cc,
            COUNT(*)                   as turns
        FROM turns
        WHERE substr(timestamp, 1, 10) = ?
        GROUP BY model
        ORDER BY inp + out DESC
    """, (today,)).fetchall()

    sessions = conn.execute("""
        SELECT COUNT(DISTINCT session_id) as cnt
        FROM turns
        WHERE substr(timestamp, 1, 10) = ?
    """, (today,)).fetchone()

    print()
    hr()
    print(f"  Today's Usage  ({today})")
    hr()

    if not rows:
        print("  No usage recorded today.")
        print()
        return

    total_inp = total_out = total_cr = total_cc = total_turns = 0
    total_cost = 0.0

    for r in rows:
        cost = calc_cost(r["model"], r["inp"] or 0, r["out"] or 0, r["cr"] or 0, r["cc"] or 0)
        total_cost += cost
        total_inp += r["inp"] or 0
        total_out += r["out"] or 0
        total_cr  += r["cr"]  or 0
        total_cc  += r["cc"]  or 0
        total_turns += r["turns"]
        print(f"  {r['model']:<30}  turns={r['turns']:<4}  in={fmt(r['inp'] or 0):<8}  out={fmt(r['out'] or 0):<8}  cost={fmt_cost(cost)}")

    hr()
    print(f"  {'TOTAL':<30}  turns={total_turns:<4}  in={fmt(total_inp):<8}  out={fmt(total_out):<8}  cost={fmt_cost(total_cost)}")
    print()
    print(f"  Sessions today:   {sessions['cnt']}")
    print(f"  Cache read:       {fmt(total_cr)}")
    print(f"  Cache creation:   {fmt(total_cc)}")
    hr()
    print()
    conn.close()


def cmd_stats():
    conn = require_db()
    conn.row_factory = sqlite3.Row

    # All-time totals
    totals = conn.execute("""
        SELECT
            SUM(total_input_tokens)   as inp,
            SUM(total_output_tokens)  as out,
            SUM(total_cache_read)     as cr,
            SUM(total_cache_creation) as cc,
            SUM(turn_count)           as turns,
            COUNT(*)                  as sessions,
            MIN(first_timestamp)      as first,
            MAX(last_timestamp)       as last
        FROM sessions
    """).fetchone()

    # By model
    by_model = conn.execute("""
        SELECT
            COALESCE(model, 'unknown') as model,
            SUM(total_input_tokens)    as inp,
            SUM(total_output_tokens)   as out,
            SUM(total_cache_read)      as cr,
            SUM(total_cache_creation)  as cc,
            SUM(turn_count)            as turns,
            COUNT(*)                   as sessions
        FROM sessions
        GROUP BY model
        ORDER BY inp + out DESC
    """).fetchall()

    # Top 5 projects
    top_projects = conn.execute("""
        SELECT
            project_name,
            SUM(total_input_tokens)  as inp,
            SUM(total_output_tokens) as out,
            SUM(turn_count)          as turns,
            COUNT(*)                 as sessions
        FROM sessions
        GROUP BY project_name
        ORDER BY inp + out DESC
        LIMIT 5
    """).fetchall()

    # Daily average (last 30 days)
    daily_avg = conn.execute("""
        SELECT
            AVG(daily_inp) as avg_inp,
            AVG(daily_out) as avg_out,
            AVG(daily_cost) as avg_cost
        FROM (
            SELECT
                substr(timestamp, 1, 10) as day,
                SUM(input_tokens) as daily_inp,
                SUM(output_tokens) as daily_out,
                0.0 as daily_cost
            FROM turns
            WHERE timestamp >= datetime('now', '-30 days')
            GROUP BY day
        )
    """).fetchone()

    # Hours calculation
    all_sessions = conn.execute(
        "SELECT first_timestamp, last_timestamp FROM sessions"
    ).fetchall()
    session_hours, wall_clock_hours = calc_hours(all_sessions)

    # Build total cost across all models
    total_cost = sum(
        calc_cost(r["model"], r["inp"] or 0, r["out"] or 0, r["cr"] or 0, r["cc"] or 0)
        for r in by_model
    )

    print()
    hr("=")
    print("  Claude Code Usage - All-Time Statistics")
    hr("=")

    first_date = (totals["first"] or "")[:10]
    last_date = (totals["last"] or "")[:10]
    print(f"  Period:           {first_date} to {last_date}")
    print(f"  Total sessions:   {totals['sessions'] or 0:,}")
    print(f"  Total turns:      {fmt(totals['turns'] or 0)}")
    print(f"  Session hours:    {session_hours:.1f}h  (sum of all session durations)")
    print(f"  Wall-clock hours: {wall_clock_hours:.1f}h  (actual calendar time)")
    print()
    print(f"  Input tokens:     {fmt(totals['inp'] or 0):<12}  (raw prompt tokens)")
    print(f"  Output tokens:    {fmt(totals['out'] or 0):<12}  (generated tokens)")
    print(f"  Cache read:       {fmt(totals['cr'] or 0):<12}  (90% cheaper than input)")
    print(f"  Cache creation:   {fmt(totals['cc'] or 0):<12}  (25% premium on input)")
    print()
    print(f"  Est. total cost:  ${total_cost:.4f}")
    hr()

    total_bedrock = sum(
        calc_bedrock_cost(r["model"], r["inp"] or 0, r["out"] or 0, r["cr"] or 0, r["cc"] or 0)
        for r in by_model
    )
    print(f"  Bedrock est.:     ${total_bedrock:.4f}")
    diff = total_bedrock - total_cost
    print(f"  Bedrock diff:     {'+' if diff >= 0 else ''}{fmt_cost(diff)}")
    hr()

    print("  By Model:")
    for r in by_model:
        cost = calc_cost(r["model"], r["inp"] or 0, r["out"] or 0, r["cr"] or 0, r["cc"] or 0)
        bedrock = calc_bedrock_cost(r["model"], r["inp"] or 0, r["out"] or 0, r["cr"] or 0, r["cc"] or 0)
        print(f"    {r['model']:<30}  sessions={r['sessions']:<4}  turns={fmt(r['turns'] or 0):<6}  "
              f"in={fmt(r['inp'] or 0):<8}  out={fmt(r['out'] or 0):<8}  cost={fmt_cost(cost)}  bedrock={fmt_cost(bedrock)}")

    hr()
    print("  Top Projects:")
    for r in top_projects:
        print(f"    {(r['project_name'] or 'unknown'):<40}  sessions={r['sessions']:<3}  "
              f"turns={fmt(r['turns'] or 0):<6}  tokens={fmt((r['inp'] or 0)+(r['out'] or 0))}")

    if daily_avg["avg_inp"]:
        hr()
        print("  Daily Average (last 30 days):")
        print(f"    Input:   {fmt(int(daily_avg['avg_inp'] or 0))}")
        print(f"    Output:  {fmt(int(daily_avg['avg_out'] or 0))}")

    hr("=")
    print()
    conn.close()


def cmd_session():
    if len(sys.argv) < 3:
        print("Usage: python cli.py session <session-id-prefix>")
        sys.exit(1)

    prefix = sys.argv[2]
    conn = require_db()
    conn.row_factory = sqlite3.Row

    session = conn.execute(
        "SELECT * FROM sessions WHERE session_id LIKE ?",
        (prefix + "%",)
    ).fetchone()

    if not session:
        print(f"No session found matching '{prefix}'")
        sys.exit(1)

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

    # Session summary
    total_inp = session["total_input_tokens"] or 0
    total_out = session["total_output_tokens"] or 0
    total_cr = session["total_cache_read"] or 0
    total_cc = session["total_cache_creation"] or 0
    total_input_eq = total_inp + total_cr + total_cc
    cache_rate = (total_cr / total_input_eq * 100) if total_input_eq > 0 else 0

    try:
        t1 = datetime.fromisoformat(session["first_timestamp"].replace("Z", "+00:00"))
        t2 = datetime.fromisoformat(session["last_timestamp"].replace("Z", "+00:00"))
        duration_min = round((t2 - t1).total_seconds() / 60, 1)
    except Exception:
        duration_min = 0

    total_cost = 0
    print()
    hr("=")
    print(f"  Session: {sid[:8]}...")
    hr("=")
    print(f"  Project:      {session['project_name'] or 'unknown'}")
    print(f"  Model:        {session['model'] or 'unknown'}")
    print(f"  Duration:     {duration_min}m")
    print(f"  Turns:        {session['turn_count'] or 0}")
    print(f"  Cache rate:   {cache_rate:.1f}%")

    # Turn-by-turn table
    hr()
    print(f"  {'#':<4} {'Time':<10} {'Model':<25} {'Tool':<18} {'Prompt':<42} {'Input':<8} {'Output':<8} {'Cost':<10} {'Cumul.':<10} {'Cache%':<7}")
    hr()

    cumulative = 0
    for i, t in enumerate(turns):
        inp = t["input_tokens"] or 0
        out = t["output_tokens"] or 0
        cr = t["cache_read_tokens"] or 0
        cc = t["cache_creation_tokens"] or 0
        cost = calc_cost(t["model"], inp, out, cr, cc)
        cumulative += cost
        total_cost += cost

        t_input_eq = inp + cr + cc
        t_cache = (cr / t_input_eq * 100) if t_input_eq > 0 else 0

        ts = (t["timestamp"] or "")
        time_part = ts[11:19] if len(ts) >= 19 else ts[:8]
        tool = (t["tool_name"] or "-")[:17]
        model = (t["model"] or "unknown")[:24]
        prompt = (t["prompt_preview"] or "-")[:40]

        print(f"  {i+1:<4} {time_part:<10} {model:<25} {tool:<18} {prompt:<42} {fmt(inp):<8} {fmt(out):<8} {fmt_cost(cost):<10} {fmt_cost(cumulative):<10} {t_cache:.0f}%")

    hr()
    bedrock_total = sum(
        calc_bedrock_cost(t["model"], t["input_tokens"] or 0, t["output_tokens"] or 0,
                          t["cache_read_tokens"] or 0, t["cache_creation_tokens"] or 0)
        for t in turns
    )
    print(f"  Total cost:    {fmt_cost(total_cost)}")
    print(f"  Bedrock est.:  {fmt_cost(bedrock_total)}")
    diff = bedrock_total - total_cost
    print(f"  Bedrock diff:  {'+' if diff >= 0 else ''}{fmt_cost(diff)}")
    hr("=")
    print()


def cmd_dashboard():
    import webbrowser
    import threading
    import time

    print("Running scan first...")
    cmd_scan()

    print("\nStarting dashboard server...")
    from dashboard import serve

    def open_browser():
        time.sleep(1.0)
        webbrowser.open("http://localhost:8080")

    t = threading.Thread(target=open_browser, daemon=True)
    t.start()
    serve(port=8080)


# ── Entry point ───────────────────────────────────────────────────────────────

USAGE = """
Claude Code Usage Dashboard

Usage:
  python cli.py scan              Scan JSONL files and update database
  python cli.py today             Show today's usage summary
  python cli.py stats             Show all-time statistics
  python cli.py session <prefix>  Show turn-by-turn detail for a session
  python cli.py dashboard         Scan + start dashboard at http://localhost:8080
"""

COMMANDS = {
    "scan": cmd_scan,
    "today": cmd_today,
    "stats": cmd_stats,
    "session": cmd_session,
    "dashboard": cmd_dashboard,
}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(USAGE)
        sys.exit(0)
    COMMANDS[sys.argv[1]]()
