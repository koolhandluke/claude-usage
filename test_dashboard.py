"""
Tests for dashboard.py — cost calculations, data retrieval, and API responses.
Run: python3 -m pytest test_dashboard.py -v   (or: python3 -m unittest test_dashboard -v)
"""

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from http.server import HTTPServer
from threading import Thread
from urllib.request import urlopen

from dashboard import (
    PRICING_PY,
    _get_pricing_py,
    _calc_cost_py,
    get_dashboard_data,
    get_session_detail,
    DashboardHandler,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _create_test_db(path):
    """Create a test SQLite DB with the same schema as scanner.py."""
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY,
            project_name TEXT,
            first_timestamp TEXT,
            last_timestamp TEXT,
            git_branch TEXT,
            total_input_tokens INTEGER,
            total_output_tokens INTEGER,
            total_cache_read INTEGER,
            total_cache_creation INTEGER,
            model TEXT,
            turn_count INTEGER
        );
        CREATE TABLE IF NOT EXISTS turns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            timestamp TEXT,
            model TEXT,
            input_tokens INTEGER,
            output_tokens INTEGER,
            cache_read_tokens INTEGER,
            cache_creation_tokens INTEGER,
            tool_name TEXT,
            cwd TEXT,
            prompt_preview TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id);
        CREATE INDEX IF NOT EXISTS idx_turns_timestamp ON turns(timestamp);
        CREATE INDEX IF NOT EXISTS idx_sessions_first ON sessions(first_timestamp);
    """)
    conn.close()
    return path


def _seed_test_data(path, sessions=None, turns=None):
    """Insert test sessions and turns into DB."""
    conn = sqlite3.connect(path)
    for s in (sessions or []):
        conn.execute(
            "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                s["session_id"], s.get("project_name", "test-project"),
                s.get("first_timestamp", "2026-04-20T10:00:00Z"),
                s.get("last_timestamp", "2026-04-20T11:00:00Z"),
                s.get("git_branch", "main"),
                s.get("total_input_tokens", 1000),
                s.get("total_output_tokens", 500),
                s.get("total_cache_read", 200),
                s.get("total_cache_creation", 100),
                s.get("model", "claude-opus-4-6"),
                s.get("turn_count", 5),
            ),
        )
    for t in (turns or []):
        conn.execute(
            "INSERT INTO turns (session_id, timestamp, model, input_tokens, "
            "output_tokens, cache_read_tokens, cache_creation_tokens, "
            "tool_name, cwd, prompt_preview) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                t["session_id"], t.get("timestamp", "2026-04-20T10:05:00Z"),
                t.get("model", "claude-opus-4-6"),
                t.get("input_tokens", 200), t.get("output_tokens", 100),
                t.get("cache_read_tokens", 40), t.get("cache_creation_tokens", 20),
                t.get("tool_name", ""), t.get("cwd", "/tmp"),
                t.get("prompt_preview", "test prompt"),
            ),
        )
    conn.commit()
    conn.close()


# ── Pricing & cost calculation tests ─────────────────────────────────────────

class TestPricing(unittest.TestCase):

    def test_known_model_lookup(self):
        p = _get_pricing_py("claude-opus-4-6")
        self.assertIsNotNone(p)
        self.assertEqual(p["input"], 6.15)
        self.assertEqual(p["output"], 30.75)

    def test_sonnet_lookup(self):
        p = _get_pricing_py("claude-sonnet-4-6")
        self.assertIsNotNone(p)
        self.assertEqual(p["input"], 3.69)

    def test_haiku_lookup(self):
        p = _get_pricing_py("claude-haiku-4-5")
        self.assertIsNotNone(p)
        self.assertEqual(p["input"], 1.23)

    def test_prefix_match(self):
        """Model names starting with a known key should resolve."""
        p = _get_pricing_py("claude-opus-4-6-extended")
        self.assertIsNotNone(p)
        self.assertEqual(p["input"], 6.15)

    def test_fuzzy_match_opus(self):
        p = _get_pricing_py("some-opus-variant")
        self.assertIsNotNone(p)
        self.assertEqual(p["input"], 6.15)

    def test_fuzzy_match_sonnet(self):
        p = _get_pricing_py("my-sonnet-model")
        self.assertIsNotNone(p)
        self.assertEqual(p["input"], 3.69)

    def test_unknown_model_returns_none(self):
        self.assertIsNone(_get_pricing_py("gpt-4"))
        self.assertIsNone(_get_pricing_py(""))
        self.assertIsNone(_get_pricing_py(None))

    def test_calc_cost_basic(self):
        """Verify cost formula: (inp*input + out*output + cr*cache_read + cc*cache_write) / 1e6."""
        cost = _calc_cost_py("claude-opus-4-6", 1_000_000, 100_000, 500_000, 50_000)
        p = PRICING_PY["claude-opus-4-6"]
        expected = (
            1_000_000 * p["input"] / 1e6
            + 100_000 * p["output"] / 1e6
            + 500_000 * p["cache_read"] / 1e6
            + 50_000 * p["cache_write"] / 1e6
        )
        self.assertAlmostEqual(cost, expected, places=6)

    def test_calc_cost_zero_tokens(self):
        self.assertEqual(_calc_cost_py("claude-opus-4-6", 0, 0, 0, 0), 0)

    def test_calc_cost_unknown_model(self):
        self.assertEqual(_calc_cost_py("unknown-model", 1000, 500, 200, 100), 0)

    def test_calc_cost_none_model(self):
        self.assertEqual(_calc_cost_py(None, 1000, 500, 200, 100), 0)

    def test_opus_more_expensive_than_sonnet(self):
        tokens = (100_000, 50_000, 20_000, 10_000)
        opus_cost = _calc_cost_py("claude-opus-4-6", *tokens)
        sonnet_cost = _calc_cost_py("claude-sonnet-4-6", *tokens)
        self.assertGreater(opus_cost, sonnet_cost)

    def test_sonnet_more_expensive_than_haiku(self):
        tokens = (100_000, 50_000, 20_000, 10_000)
        sonnet_cost = _calc_cost_py("claude-sonnet-4-6", *tokens)
        haiku_cost = _calc_cost_py("claude-haiku-4-5", *tokens)
        self.assertGreater(sonnet_cost, haiku_cost)


# ── Data retrieval tests ─────────────────────────────────────────────────────

class TestGetDashboardData(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = Path(self.tmp.name)
        self.tmp.close()
        _create_test_db(self.db_path)

    def tearDown(self):
        self.db_path.unlink(missing_ok=True)

    def test_missing_db(self):
        missing = Path("/tmp/nonexistent_test.db")
        result = get_dashboard_data(missing)
        self.assertIn("error", result)

    def test_empty_db(self):
        result = get_dashboard_data(self.db_path)
        self.assertIn("all_models", result)
        self.assertIn("daily_by_model", result)
        self.assertIn("sessions_all", result)
        self.assertIn("top_turns", result)
        self.assertIn("generated_at", result)
        self.assertEqual(len(result["all_models"]), 0)
        self.assertEqual(len(result["sessions_all"]), 0)

    def test_single_session(self):
        _seed_test_data(self.db_path, sessions=[
            {"session_id": "abc12345-6789-0000-0000-000000000000"},
        ], turns=[
            {"session_id": "abc12345-6789-0000-0000-000000000000",
             "timestamp": "2026-04-20T10:05:00Z"},
        ])
        result = get_dashboard_data(self.db_path)
        self.assertEqual(len(result["sessions_all"]), 1)
        s = result["sessions_all"][0]
        self.assertEqual(s["session_id"], "abc12345")
        self.assertEqual(s["full_session_id"], "abc12345-6789-0000-0000-000000000000")
        self.assertEqual(s["model"], "claude-opus-4-6")
        self.assertEqual(s["input"], 1000)
        self.assertEqual(s["output"], 500)

    def test_multiple_models(self):
        _seed_test_data(self.db_path, sessions=[
            {"session_id": "sess-opus-0001", "model": "claude-opus-4-6"},
            {"session_id": "sess-sonn-0001", "model": "claude-sonnet-4-6"},
        ], turns=[
            {"session_id": "sess-opus-0001", "model": "claude-opus-4-6"},
            {"session_id": "sess-sonn-0001", "model": "claude-sonnet-4-6"},
        ])
        result = get_dashboard_data(self.db_path)
        self.assertEqual(len(result["all_models"]), 2)
        self.assertIn("claude-opus-4-6", result["all_models"])
        self.assertIn("claude-sonnet-4-6", result["all_models"])

    def test_daily_aggregation(self):
        _seed_test_data(self.db_path, turns=[
            {"session_id": "s1", "model": "claude-opus-4-6",
             "timestamp": "2026-04-20T10:00:00Z", "input_tokens": 100, "output_tokens": 50},
            {"session_id": "s1", "model": "claude-opus-4-6",
             "timestamp": "2026-04-20T14:00:00Z", "input_tokens": 200, "output_tokens": 75},
            {"session_id": "s1", "model": "claude-opus-4-6",
             "timestamp": "2026-04-21T09:00:00Z", "input_tokens": 300, "output_tokens": 100},
        ], sessions=[
            {"session_id": "s1"},
        ])
        result = get_dashboard_data(self.db_path)
        daily = result["daily_by_model"]
        day_20 = [d for d in daily if d["day"] == "2026-04-20"]
        day_21 = [d for d in daily if d["day"] == "2026-04-21"]
        self.assertEqual(len(day_20), 1)
        self.assertEqual(day_20[0]["input"], 300)  # 100 + 200
        self.assertEqual(day_20[0]["output"], 125)  # 50 + 75
        self.assertEqual(len(day_21), 1)
        self.assertEqual(day_21[0]["input"], 300)

    def test_top_turns_limit(self):
        """Top turns should be capped at 50."""
        sessions = [{"session_id": f"s{i}", "turn_count": 1} for i in range(60)]
        turns = [
            {"session_id": f"s{i}", "model": "claude-opus-4-6",
             "input_tokens": 1000 + i, "output_tokens": 500}
            for i in range(60)
        ]
        _seed_test_data(self.db_path, sessions=sessions, turns=turns)
        result = get_dashboard_data(self.db_path)
        self.assertEqual(len(result["top_turns"]), 50)

    def test_session_duration_calculated(self):
        _seed_test_data(self.db_path, sessions=[
            {"session_id": "dur-test-001",
             "first_timestamp": "2026-04-20T10:00:00Z",
             "last_timestamp": "2026-04-20T10:30:00Z"},
        ])
        result = get_dashboard_data(self.db_path)
        s = result["sessions_all"][0]
        self.assertEqual(s["duration_min"], 30.0)


class TestGetSessionDetail(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = Path(self.tmp.name)
        self.tmp.close()
        _create_test_db(self.db_path)

    def tearDown(self):
        self.db_path.unlink(missing_ok=True)

    def test_missing_db(self):
        result = get_session_detail("abc", Path("/tmp/nonexistent_test.db"))
        self.assertIn("error", result)

    def test_session_not_found(self):
        result = get_session_detail("nonexist", self.db_path)
        self.assertIn("error", result)
        self.assertEqual(result["error"], "Session not found")

    def test_session_detail_structure(self):
        _seed_test_data(self.db_path, sessions=[
            {"session_id": "detail-test-0001-0000-000000000000",
             "project_name": "my-project", "model": "claude-opus-4-6",
             "total_input_tokens": 500, "total_output_tokens": 250,
             "total_cache_read": 100, "total_cache_creation": 50,
             "turn_count": 2},
        ], turns=[
            {"session_id": "detail-test-0001-0000-000000000000",
             "timestamp": "2026-04-20T10:00:00Z",
             "input_tokens": 300, "output_tokens": 150,
             "cache_read_tokens": 60, "cache_creation_tokens": 30,
             "tool_name": "bash", "prompt_preview": "run tests"},
            {"session_id": "detail-test-0001-0000-000000000000",
             "timestamp": "2026-04-20T10:05:00Z",
             "input_tokens": 200, "output_tokens": 100,
             "cache_read_tokens": 40, "cache_creation_tokens": 20,
             "tool_name": "read", "prompt_preview": "show file"},
        ])
        result = get_session_detail("detail-t", self.db_path)

        self.assertNotIn("error", result)
        self.assertEqual(result["session_id"], "detail-t")
        self.assertEqual(result["project"], "my-project")
        self.assertEqual(result["model"], "claude-opus-4-6")
        self.assertEqual(result["turn_count"], 2)
        self.assertEqual(len(result["turns"]), 2)

    def test_cumulative_cost_increases(self):
        _seed_test_data(self.db_path, sessions=[
            {"session_id": "cumul-test-0001-0000-000000000000", "turn_count": 3},
        ], turns=[
            {"session_id": "cumul-test-0001-0000-000000000000",
             "timestamp": f"2026-04-20T10:0{i}:00Z",
             "input_tokens": 1000, "output_tokens": 500}
            for i in range(3)
        ])
        result = get_session_detail("cumul-te", self.db_path)
        turns = result["turns"]
        self.assertEqual(len(turns), 3)
        # Cumulative cost should monotonically increase
        for i in range(1, len(turns)):
            self.assertGreater(turns[i]["cumulative_cost"], turns[i - 1]["cumulative_cost"])

    def test_cache_rate_computed(self):
        _seed_test_data(self.db_path, sessions=[
            {"session_id": "cache-test-0001-0000-000000000000",
             "total_cache_read": 700, "total_input_tokens": 200,
             "total_cache_creation": 100, "turn_count": 1},
        ], turns=[
            {"session_id": "cache-test-0001-0000-000000000000",
             "input_tokens": 200, "cache_read_tokens": 700,
             "cache_creation_tokens": 100, "output_tokens": 50},
        ])
        result = get_session_detail("cache-te", self.db_path)
        # cache_rate = 700 / (200+700+100) * 100 = 70.0
        self.assertAlmostEqual(result["cache_rate"], 70.0, places=1)

    def test_total_cost_matches_sum_of_turns(self):
        _seed_test_data(self.db_path, sessions=[
            {"session_id": "cost-sum-0001-0000-000000000000", "turn_count": 2},
        ], turns=[
            {"session_id": "cost-sum-0001-0000-000000000000",
             "timestamp": "2026-04-20T10:00:00Z",
             "input_tokens": 5000, "output_tokens": 2000,
             "cache_read_tokens": 1000, "cache_creation_tokens": 500},
            {"session_id": "cost-sum-0001-0000-000000000000",
             "timestamp": "2026-04-20T10:05:00Z",
             "input_tokens": 3000, "output_tokens": 1500,
             "cache_read_tokens": 800, "cache_creation_tokens": 300},
        ])
        result = get_session_detail("cost-sum", self.db_path)
        turn_costs = sum(t["cost"] for t in result["turns"])
        self.assertAlmostEqual(result["total_cost"], turn_costs, places=6)


# ── API endpoint tests ───────────────────────────────────────────────────────

class TestAPIEndpoints(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        cls.db_path = Path(cls.tmp.name)
        cls.tmp.close()
        _create_test_db(cls.db_path)
        _seed_test_data(cls.db_path, sessions=[
            {"session_id": "api-test-0001-0000-000000000000",
             "project_name": "api-project", "model": "claude-opus-4-6"},
        ], turns=[
            {"session_id": "api-test-0001-0000-000000000000"},
        ])
        # Patch the functions called by the handler to use our test DB
        cls.patch_data = patch(
            "dashboard.get_dashboard_data",
            side_effect=lambda db_path=None: get_dashboard_data(cls.db_path),
        )
        cls.patch_session = patch(
            "dashboard.get_session_detail",
            side_effect=lambda prefix, db_path=None: get_session_detail(prefix, cls.db_path),
        )
        cls.patch_data.start()
        cls.patch_session.start()
        cls.server = HTTPServer(("localhost", 0), DashboardHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.patch_data.stop()
        cls.patch_session.stop()
        cls.db_path.unlink(missing_ok=True)

    def _get(self, path):
        url = f"http://localhost:{self.port}{path}"
        with urlopen(url) as resp:
            return resp.status, resp.read()

    def _get_json(self, path):
        status, body = self._get(path)
        return status, json.loads(body)

    def test_index_returns_html(self):
        status, body = self._get("/")
        self.assertEqual(status, 200)
        self.assertIn(b"Claude Code Usage Dashboard", body)

    def test_api_data_returns_json(self):
        status, data = self._get_json("/api/data")
        self.assertEqual(status, 200)
        self.assertIn("all_models", data)
        self.assertIn("daily_by_model", data)
        self.assertIn("sessions_all", data)
        self.assertIn("top_turns", data)
        self.assertIn("generated_at", data)

    def test_api_data_has_sessions(self):
        _, data = self._get_json("/api/data")
        self.assertEqual(len(data["sessions_all"]), 1)
        self.assertEqual(data["sessions_all"][0]["project"], "api-project")

    def test_api_session_detail(self):
        _, data = self._get_json("/api/session/api-test")
        self.assertNotIn("error", data)
        self.assertEqual(data["project"], "api-project")
        self.assertIn("turns", data)

    def test_api_session_not_found(self):
        _, data = self._get_json("/api/session/zzz-nope")
        self.assertIn("error", data)

    def test_html_contains_tab_bar(self):
        """Verify the Cost Insights tab is present in the HTML."""
        _, body = self._get("/")
        self.assertIn(b'id="tab-bar"', body)
        self.assertIn(b"Cost Insights", body)

    def test_html_contains_insight_panels(self):
        _, body = self._get("/")
        self.assertIn(b'id="insights-container"', body)
        self.assertIn(b'id="insight-hotspots"', body)
        self.assertIn(b'id="insight-trend"', body)
        self.assertIn(b'id="insight-projects"', body)
        self.assertIn(b'id="insight-cache"', body)
        self.assertIn(b'id="insight-model-opt"', body)
        self.assertIn(b'id="insight-high-output"', body)

    def test_html_contains_buildProjectMap(self):
        """Verify the shared helper function is in the JS."""
        _, body = self._get("/")
        self.assertIn(b"buildProjectMap", body)

    def test_html_contains_cost_trend_map_lookup(self):
        """Verify the O(n) map lookup is present in the rendered JS."""
        _, body = self._get("/")
        self.assertIn(b"costByDay", body)
        # The old O(n*m) nested pattern inside renderCostTrend should be gone
        # (it used daily.map with an inner loop over filteredDaily)
        self.assertNotIn(b"if (r.day === d.day) dayCost", body)

    def test_html_contains_isFlat_check(self):
        """Verify the zero-delta edge case fix is present."""
        _, body = self._get("/")
        self.assertIn(b"isFlat", body)


if __name__ == "__main__":
    unittest.main()
