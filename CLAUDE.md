# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A local Python dashboard that parses Claude Code's JSONL usage logs from `~/.claude/projects/` into a SQLite database (`~/.claude/usage.db`) and serves an analytics web UI at `localhost:8080`. Stdlib-only — no external dependencies for runtime.

## Commands

```bash
# Run the dashboard (scans logs + opens browser)
python3 cli.py dashboard

# Run tests
python3 -m pytest test_dashboard.py -v

# Run a single test class or method
python3 -m pytest test_dashboard.py::TestPricing -v
python3 -m pytest test_dashboard.py::TestPricing::test_opus_cost -v

# CLI utilities
python3 cli.py scan              # Scan JSONL into SQLite
python3 cli.py today             # Today's usage summary
python3 cli.py stats             # All-time stats
python3 cli.py session <prefix>  # Turn-by-turn session detail
```

## Architecture

Three-module design with clear separation:

- **scanner.py** — Parses `~/.claude/projects/**/*.jsonl` files, writes to SQLite. Tracks file mtimes/line counts for incremental scanning. Aggregates turns into session-level summaries.
- **cli.py** — CLI entry point with subcommands. Contains Bedrock pricing tables and `calc_cost()`/`calc_bedrock_cost()` functions.
- **dashboard.py** — HTTP server that serves both the HTML/JS UI (inline, with Chart.js from CDN) and REST API endpoints (`/api/data`, `/api/session/<id>`). Contains API pricing tables and the Cost Insights tab logic.

**Data flow**: JSONL files → `scanner.py` → SQLite → `dashboard.py` API → Browser (client-side filtering by date range and model)

## Database Schema

```sql
sessions (session_id PK, project_name, first/last_timestamp, git_branch,
          total_input/output_tokens, cache_read/creation, model, turn_count)
turns    (id PK, session_id, timestamp, model, input/output_tokens,
          cache_read/creation_tokens, tool_name, cwd, prompt_preview)
processed_files (path PK, mtime, lines)
```

## Key Design Decisions

- **Dual pricing**: API rates live in `dashboard.py` (lines ~131-138), Bedrock rates in `cli.py` (lines ~28-36). They use different cache multipliers.
- **Incremental scan**: Only processes new/modified JSONL lines using mtime + line count tracking in `processed_files` table.
- **Client-side filtering**: Server returns full dataset; JS filters by date range and model. Selections persist in URL params (`?range=7d&models=...`).
- **No external deps**: Runtime uses only Python stdlib (sqlite3, http.server, json, pathlib). Tests require `pytest`.

## Testing

Tests are in `test_dashboard.py` using `unittest.TestCase` with an in-memory SQLite database. Test classes: `TestPricing`, `TestGetDashboardData`, `TestGetSessionDetail`, `TestAPI`. Tests import directly from `dashboard.py` and `scanner.py`.

## Troubleshooting: Data Not Loading

**"Database not found" error in dashboard:**
- `~/.claude/usage.db` doesn't exist yet. Run `python3 cli.py scan` first (or use `python3 cli.py dashboard` which scans automatically).
- If `~/.claude/` directory itself is missing, the scanner now creates it (fixed in commit `b5d1e15`), but on older checkouts you may need to `mkdir -p ~/.claude` manually.

**Dashboard shows empty/zero data after scan:**
- The scanner skips JSONL lines with zero token usage (`input + output + cache_read + cache_creation == 0`). If all records lack `usage` fields, nothing gets inserted.
- Check that `~/.claude/projects/` contains `.jsonl` files with `"type": "assistant"` records that have non-zero `message.usage` data.

**Stale data — new sessions not appearing:**
- The `processed_files` table tracks each file's mtime and line count. If a file was modified but its mtime didn't change (e.g., copied with preserved timestamps), the scanner skips it.
- Fix: delete the tracking row to force a re-scan of that file:
  ```bash
  sqlite3 ~/.claude/usage.db "DELETE FROM processed_files WHERE path LIKE '%filename%';"
  python3 cli.py scan
  ```
- Nuclear option — full re-scan from scratch:
  ```bash
  rm ~/.claude/usage.db
  python3 cli.py scan
  ```

**Duplicate token counts after re-scan:**
- The incremental update path (`scanner.py` line ~332) only inserts turns from new lines (lines beyond `old_lines` count). But if you delete `usage.db` without clearing `processed_files` (impossible since they're in the same DB), or manually truncate the turns table, session totals can double-count because `upsert_sessions` adds new token counts on top of existing ones.
- Safest recovery: delete the entire DB and re-scan.

**Cost Insights tab shows $0.00:**
- Costs are calculated client-side using the pricing tables in `dashboard.py`. If a model string doesn't match any key in the pricing map (e.g., a new model name), its cost defaults to zero.
- Check the model names in your data vs the keys in `PRICING` dict (~line 131 of `dashboard.py`).
