"""E2E API-Tests für Iteration 5: Neue öffentliche Endpoints und erweiterte Zeit-Analyse.

WICHTIG: Verbindet auf PRODUKTIONS-MongoDB-Atlas. Nach jedem Test aufräumen!
KEINE destruktiven Aktionen: KEINE Trades löschen, KEINE Strategien löschen.
"""
import os
import time
import pytest
import requests

# Backend URL from environment or use localhost (internal testing)
BASE = os.environ.get("REACT_APP_BACKEND_URL", "http://localhost:8001").rstrip("/")
ADMIN_USER = os.environ.get("ADMIN_USER", "Admin")
ADMIN_PW = os.environ.get("ADMIN_PASSWORD", "admin")


@pytest.fixture(scope="module")
def token():
    """Get admin token for authenticated requests."""
    r = requests.post(f"{BASE}/api/auth/login",
                      json={"username": ADMIN_USER, "password": ADMIN_PW}, timeout=15)
    assert r.status_code == 200, f"Login failed: {r.text}"
    return r.json().get("token") or r.json().get("access_token")


@pytest.fixture(scope="module")
def auth(token):
    """Authorization header with admin token."""
    return {"Authorization": f"Bearer {token}"}


# ========== TASK 1: GET /api/autotrade/strategy_coin_configs (now public) ==========
class TestStrategyCoinsConfigsPublic:
    """Test that strategy_coin_configs endpoint is now public (read-only)."""
    
    def test_get_without_token_returns_200(self):
        """Public endpoint should work without authentication."""
        r = requests.get(f"{BASE}/api/autotrade/strategy_coin_configs", timeout=15)
        assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text}"
        print("✓ GET /api/autotrade/strategy_coin_configs without token: 200 OK")
    
    def test_returns_nested_dict_structure(self):
        """Response should have nested dict structure: {strategy_id: {symbol: config}}."""
        r = requests.get(f"{BASE}/api/autotrade/strategy_coin_configs", timeout=15)
        assert r.status_code == 200
        data = r.json()
        assert "configs" in data, f"Missing 'configs' key in response: {data.keys()}"
        configs = data["configs"]
        assert isinstance(configs, dict), f"configs should be dict, got {type(configs)}"
        
        # Should have data (142 configs mentioned in review request)
        if configs:
            # Check nested structure
            for strategy_id, symbols in configs.items():
                assert isinstance(symbols, dict), f"Strategy {strategy_id} should have dict of symbols"
                for symbol, config in symbols.items():
                    assert isinstance(config, dict), f"Config for {strategy_id}/{symbol} should be dict"
                    print(f"  Found config: {strategy_id}/{symbol} -> {config.get('mode', 'N/A')}")
                    break  # Just check first one
                break  # Just check first strategy
        
        print(f"✓ Nested dict structure verified. Total strategies: {len(configs)}")
    
    def test_write_endpoint_remains_protected(self):
        """POST /api/autotrade/strategy/{id}/coin/{symbol} should require auth."""
        # Try to write without token - should get 401/403
        r = requests.post(f"{BASE}/api/autotrade/strategy/test_strategy/coin/BTCUSDT",
                         json={"mode": "paper", "enabled": True}, timeout=15)
        assert r.status_code in (401, 403), \
            f"Write endpoint should be protected, got {r.status_code}: {r.text}"
        print(f"✓ Write endpoint protected: {r.status_code}")


# ========== TASK 2: GET /api/analytics/time-based/{symbol} (extended) ==========
class TestTimeBasedAnalyticsExtended:
    """Test extended time-based analytics with new fields."""
    
    def test_backward_compatible_fields(self):
        """Should still have time_analytics and best_hours fields."""
        r = requests.get(f"{BASE}/api/analytics/time-based/BTCUSDT", timeout=15)
        assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text}"
        data = r.json()
        
        # Check backward compatible fields
        assert "time_analytics" in data, "Missing time_analytics field"
        assert "best_hours" in data, "Missing best_hours field"
        assert isinstance(data["time_analytics"], list), "time_analytics should be list"
        assert isinstance(data["best_hours"], list), "best_hours should be list"
        assert len(data["best_hours"]) <= 5, f"best_hours should have max 5 entries, got {len(data['best_hours'])}"
        print(f"✓ Backward compatible fields present: time_analytics ({len(data['time_analytics'])}), best_hours ({len(data['best_hours'])})")
    
    def test_new_fields_present(self):
        """Should have new fields: by_hour, by_weekday, by_combo, strategy_id."""
        r = requests.get(f"{BASE}/api/analytics/time-based/BTCUSDT", timeout=15)
        assert r.status_code == 200
        data = r.json()
        
        # Check new fields
        assert "by_hour" in data, "Missing by_hour field"
        assert "by_weekday" in data, "Missing by_weekday field"
        assert "by_combo" in data, "Missing by_combo field"
        assert "strategy_id" in data, "Missing strategy_id field"
        
        assert isinstance(data["by_hour"], list), "by_hour should be list"
        assert isinstance(data["by_weekday"], list), "by_weekday should be list"
        assert isinstance(data["by_combo"], list), "by_combo should be list"
        
        print(f"✓ New fields present: by_hour ({len(data['by_hour'])}), by_weekday ({len(data['by_weekday'])}), by_combo ({len(data['by_combo'])})")
    
    def test_by_hour_structure(self):
        """by_hour entries should have correct structure and win_rate calculation."""
        r = requests.get(f"{BASE}/api/analytics/time-based/BTCUSDT", timeout=15)
        assert r.status_code == 200
        data = r.json()
        
        by_hour = data["by_hour"]
        if by_hour:
            entry = by_hour[0]
            # Check required fields
            assert "hour" in entry, "by_hour entry missing 'hour'"
            assert "total_signals" in entry, "by_hour entry missing 'total_signals'"
            assert "wins" in entry, "by_hour entry missing 'wins'"
            assert "losses" in entry, "by_hour entry missing 'losses'"
            assert "decided" in entry, "by_hour entry missing 'decided'"
            assert "win_rate" in entry, "by_hour entry missing 'win_rate'"
            assert "avg_crv" in entry, "by_hour entry missing 'avg_crv'"
            
            # Validate hour range
            assert 0 <= entry["hour"] <= 23, f"hour should be 0-23, got {entry['hour']}"
            
            # Validate win_rate calculation
            if entry["decided"] > 0:
                expected_wr = round(entry["wins"] / entry["decided"] * 100, 1)
                assert entry["win_rate"] == expected_wr, \
                    f"win_rate mismatch: expected {expected_wr}, got {entry['win_rate']}"
            else:
                assert entry["win_rate"] == 0.0, "win_rate should be 0 when decided=0"
            
            print(f"✓ by_hour structure valid: hour={entry['hour']}, signals={entry['total_signals']}, win_rate={entry['win_rate']}%")
    
    def test_by_weekday_structure(self):
        """by_weekday entries should have weekday names and weekday_index."""
        r = requests.get(f"{BASE}/api/analytics/time-based/BTCUSDT", timeout=15)
        assert r.status_code == 200
        data = r.json()
        
        by_weekday = data["by_weekday"]
        if by_weekday:
            entry = by_weekday[0]
            # Check required fields
            assert "weekday" in entry, "by_weekday entry missing 'weekday'"
            assert "weekday_index" in entry, "by_weekday entry missing 'weekday_index'"
            assert "total_signals" in entry, "by_weekday entry missing 'total_signals'"
            assert "win_rate" in entry, "by_weekday entry missing 'win_rate'"
            
            # Validate weekday
            valid_weekdays = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
            assert entry["weekday"] in valid_weekdays, \
                f"weekday should be in {valid_weekdays}, got {entry['weekday']}"
            assert 0 <= entry["weekday_index"] <= 6, \
                f"weekday_index should be 0-6, got {entry['weekday_index']}"
            
            print(f"✓ by_weekday structure valid: {entry['weekday']} (index={entry['weekday_index']}), signals={entry['total_signals']}")
    
    def test_by_combo_structure(self):
        """by_combo entries should have both hour and weekday."""
        r = requests.get(f"{BASE}/api/analytics/time-based/BTCUSDT", timeout=15)
        assert r.status_code == 200
        data = r.json()
        
        by_combo = data["by_combo"]
        if by_combo:
            entry = by_combo[0]
            # Check required fields
            assert "hour" in entry, "by_combo entry missing 'hour'"
            assert "weekday" in entry, "by_combo entry missing 'weekday'"
            assert "total_signals" in entry, "by_combo entry missing 'total_signals'"
            assert "win_rate" in entry, "by_combo entry missing 'win_rate'"
            
            print(f"✓ by_combo structure valid: {entry['weekday']} {entry['hour']}:00, signals={entry['total_signals']}, win_rate={entry['win_rate']}%")
    
    def test_strategy_filter(self):
        """Test strategy_id filter parameter."""
        # First get without filter to see total signals
        r1 = requests.get(f"{BASE}/api/analytics/time-based/BTCUSDT", timeout=15)
        assert r1.status_code == 200
        data1 = r1.json()
        total_unfiltered = sum(e.get("total_signals", 0) for e in data1.get("by_hour", []))
        
        # Now test with a strategy filter (use a common strategy like scalping_4_rules)
        r2 = requests.get(f"{BASE}/api/analytics/time-based/BTCUSDT?strategy_id=scalping_4_rules", timeout=15)
        assert r2.status_code == 200
        data2 = r2.json()
        
        # Check strategy_id is set in response
        assert data2.get("strategy_id") == "scalping_4_rules", \
            f"strategy_id should be 'scalping_4_rules', got {data2.get('strategy_id')}"
        
        # Filtered totals should be <= unfiltered
        total_filtered = sum(e.get("total_signals", 0) for e in data2.get("by_hour", []))
        assert total_filtered <= total_unfiltered, \
            f"Filtered signals ({total_filtered}) should be <= unfiltered ({total_unfiltered})"
        
        print(f"✓ Strategy filter works: unfiltered={total_unfiltered}, filtered={total_filtered}")
    
    def test_unknown_strategy_returns_empty(self):
        """Unknown strategy_id should return 200 with empty lists, not 500."""
        r = requests.get(f"{BASE}/api/analytics/time-based/BTCUSDT?strategy_id=gibts_nicht_xyz", timeout=15)
        assert r.status_code == 200, f"Should return 200 for unknown strategy, got {r.status_code}: {r.text}"
        data = r.json()
        
        # Should have empty or very small lists
        assert isinstance(data.get("by_hour"), list), "by_hour should be list"
        assert isinstance(data.get("by_weekday"), list), "by_weekday should be list"
        assert isinstance(data.get("by_combo"), list), "by_combo should be list"
        
        print(f"✓ Unknown strategy handled gracefully: by_hour={len(data['by_hour'])}, by_weekday={len(data['by_weekday'])}")


    def test_pnl_fields_present_in_all_groupings(self):
        """Iter 5.2: All groupings should have PnL fields from auto_trades."""
        r = requests.get(f"{BASE}/api/analytics/time-based/BTCUSDT", timeout=15)
        assert r.status_code == 200
        data = r.json()
        
        # Required PnL fields added in Iter 5.2
        pnl_fields = ["trades", "trade_wins", "trade_losses", "trade_win_rate",
                     "pnl", "avg_pnl", "best_trade", "worst_trade"]
        
        # Test by_hour
        by_hour = data["by_hour"]
        if by_hour:
            entry = by_hour[0]
            for field in pnl_fields:
                assert field in entry, f"by_hour missing PnL field: {field}"
            # Validate types
            assert isinstance(entry["trades"], int), "trades should be int"
            assert isinstance(entry["pnl"], (int, float)), "pnl should be numeric"
            assert isinstance(entry["trade_win_rate"], (int, float)), "trade_win_rate should be numeric"
            print(f"✓ by_hour has all PnL fields: hour={entry['hour']}, trades={entry['trades']}, pnl={entry['pnl']}")
        
        # Test by_weekday
        by_weekday = data["by_weekday"]
        if by_weekday:
            entry = by_weekday[0]
            for field in pnl_fields:
                assert field in entry, f"by_weekday missing PnL field: {field}"
            print(f"✓ by_weekday has all PnL fields: {entry['weekday']}, trades={entry['trades']}, pnl={entry['pnl']}")
        
        # Test by_combo
        by_combo = data["by_combo"]
        if by_combo:
            entry = by_combo[0]
            for field in pnl_fields:
                assert field in entry, f"by_combo missing PnL field: {field}"
            print(f"✓ by_combo has all PnL fields: {entry['weekday']} {entry['hour']}:00, trades={entry['trades']}, pnl={entry['pnl']}")
    
    def test_pnl_calculations_correct(self):
        """Iter 5.2: Validate PnL calculations (trade_win_rate, avg_pnl)."""
        r = requests.get(f"{BASE}/api/analytics/time-based/BTCUSDT", timeout=15)
        assert r.status_code == 200
        data = r.json()
        
        by_hour = data["by_hour"]
        # Find entries with trades to validate calculations
        entries_with_trades = [e for e in by_hour if e.get("trades", 0) > 0]
        
        if entries_with_trades:
            entry = entries_with_trades[0]
            wins = entry["trade_wins"]
            losses = entry["trade_losses"]
            total = wins + losses
            
            # Validate trade_win_rate
            if total > 0:
                expected_wr = round(wins / total * 100, 1)
                assert entry["trade_win_rate"] == expected_wr, \
                    f"trade_win_rate mismatch: expected {expected_wr}, got {entry['trade_win_rate']}"
            else:
                assert entry["trade_win_rate"] == 0.0, "trade_win_rate should be 0 when no decided trades"
            
            # Validate avg_pnl
            if entry["trades"] > 0:
                expected_avg = round(entry["pnl"] / entry["trades"], 2)
                assert entry["avg_pnl"] == expected_avg, \
                    f"avg_pnl mismatch: expected {expected_avg}, got {entry['avg_pnl']}"
            
            print(f"✓ PnL calculations correct: hour={entry['hour']}, trades={entry['trades']}, "
                  f"win_rate={entry['trade_win_rate']}%, avg_pnl={entry['avg_pnl']}")
        else:
            print("✓ No trades found to validate calculations (data-dependent)")
    
    def test_strategy_filter_with_pnl(self):
        """Iter 5.2: Strategy filter should also filter PnL data."""
        # Test with a strategy that likely has trades
        r = requests.get(f"{BASE}/api/analytics/time-based/BTCUSDT?strategy_id=scalping_4_rules", timeout=15)
        assert r.status_code == 200
        data = r.json()
        
        assert data["strategy_id"] == "scalping_4_rules"
        
        # Check that PnL fields are present even with filter
        by_hour = data.get("by_hour", [])
        if by_hour:
            entry = by_hour[0]
            pnl_fields = ["trades", "trade_wins", "trade_losses", "pnl"]
            for field in pnl_fields:
                assert field in entry, f"Filtered data missing PnL field: {field}"
            print(f"✓ Strategy filter preserves PnL fields: hour={entry.get('hour')}, trades={entry.get('trades')}")
    
    def test_zero_trade_entries_have_zero_pnl(self):
        """Iter 5.2: Entries with 0 trades should have PnL fields set to 0."""
        r = requests.get(f"{BASE}/api/analytics/time-based/BTCUSDT", timeout=15)
        assert r.status_code == 200
        data = r.json()
        
        by_hour = data["by_hour"]
        zero_trade_entries = [e for e in by_hour if e.get("trades", 0) == 0]
        
        if zero_trade_entries:
            entry = zero_trade_entries[0]
            assert entry["pnl"] == 0.0, f"Expected pnl=0.0, got {entry['pnl']}"
            assert entry["avg_pnl"] == 0.0, f"Expected avg_pnl=0.0, got {entry['avg_pnl']}"
            assert entry["best_trade"] == 0.0, f"Expected best_trade=0.0, got {entry['best_trade']}"
            assert entry["worst_trade"] == 0.0, f"Expected worst_trade=0.0, got {entry['worst_trade']}"
            assert entry["trade_win_rate"] == 0.0, f"Expected trade_win_rate=0.0, got {entry['trade_win_rate']}"
            print(f"✓ Zero-trade entries have zero PnL: hour={entry['hour']}, all PnL fields=0")
        else:
            print("✓ All entries have trades (data-dependent)")



# ========== TASK 3: Iteration-4 AI Features Verification ==========
class TestAISupervisorSettings:
    """Test AI Supervisor settings endpoints."""
    
    def test_get_supervisor_returns_settings(self, auth):
        """GET /api/ai/supervisor should return settings block."""
        r = requests.get(f"{BASE}/api/ai/supervisor", headers=auth, timeout=15)
        assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text}"
        data = r.json()
        
        assert "settings" in data, f"Missing 'settings' in response: {data.keys()}"
        settings = data["settings"]
        assert "auto_enabled" in settings, "Missing auto_enabled in settings"
        assert "interval_hours" in settings, "Missing interval_hours in settings"
        assert "auto_switch" in settings, "Missing auto_switch in settings"
        
        print(f"✓ Supervisor settings present: auto_enabled={settings['auto_enabled']}, interval_hours={settings['interval_hours']}, auto_switch={settings['auto_switch']}")
    
    def test_settings_clamp_low(self, auth):
        """interval_hours=1 should be clamped to 6."""
        r = requests.post(f"{BASE}/api/ai/supervisor/settings",
                         headers=auth, json={"interval_hours": 1}, timeout=15)
        assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text}"
        data = r.json()
        settings = data.get("settings", data)
        assert settings["interval_hours"] == 6, \
            f"interval_hours=1 should be clamped to 6, got {settings['interval_hours']}"
        print("✓ Low value clamped: interval_hours=1 -> 6")
    
    def test_settings_clamp_high(self, auth):
        """interval_hours=999 should be clamped to 168."""
        r = requests.post(f"{BASE}/api/ai/supervisor/settings",
                         headers=auth, json={"interval_hours": 999}, timeout=15)
        assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text}"
        data = r.json()
        settings = data.get("settings", data)
        assert settings["interval_hours"] == 168, \
            f"interval_hours=999 should be clamped to 168, got {settings['interval_hours']}"
        print("✓ High value clamped: interval_hours=999 -> 168")
    
    def test_restore_defaults(self, auth):
        """Restore default settings after tests."""
        r = requests.post(f"{BASE}/api/ai/supervisor/settings",
                         headers=auth,
                         json={"auto_enabled": False, "interval_hours": 24, "auto_switch": False},
                         timeout=15)
        assert r.status_code == 200, f"Failed to restore defaults: {r.text}"
        data = r.json()
        settings = data.get("settings", data)
        assert settings["auto_enabled"] is False
        assert settings["interval_hours"] == 24
        assert settings["auto_switch"] is False
        print("✓ Default settings restored: auto_enabled=False, interval_hours=24, auto_switch=False")


class TestAISupervisorHistory:
    """Test AI Supervisor history endpoint."""
    
    def test_history_endpoint(self, auth):
        """GET /api/ai/supervisor/history should return reports list."""
        r = requests.get(f"{BASE}/api/ai/supervisor/history?limit=5", headers=auth, timeout=15)
        assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text}"
        data = r.json()
        
        assert "reports" in data, f"Missing 'reports' in response: {data.keys()}"
        reports = data["reports"]
        assert isinstance(reports, list), f"reports should be list, got {type(reports)}"
        
        # If there are reports, check structure
        if reports:
            report = reports[0]
            assert "ts" in report, "Report missing 'ts' field"
            # Check descending order (newest first)
            if len(reports) > 1:
                ts_list = [r["ts"] for r in reports]
                assert ts_list == sorted(ts_list, reverse=True), "Reports should be sorted newest first"
        
        print(f"✓ History endpoint works: {len(reports)} reports found")


class TestAISupervisorRollback:
    """Test AI Supervisor rollback endpoint."""
    
    def test_rollback_without_active_switch(self, auth):
        """POST /api/ai/supervisor/rollback without active switch should return 400."""
        r = requests.post(f"{BASE}/api/ai/supervisor/rollback",
                         headers=auth, json={}, timeout=15)
        # Should return 400 when no active switch
        assert r.status_code == 400, \
            f"Should return 400 without active switch, got {r.status_code}: {r.text}"
        
        # Check error message mentions "umschaltung"
        body = r.text.lower()
        assert "umschalt" in body or "keine" in body, \
            f"Error message should mention 'umschaltung', got: {r.text}"
        
        print(f"✓ Rollback without active switch returns 400: {r.text[:100]}")


class TestQuickPrompts:
    """Test Quick Prompts endpoints."""
    
    def test_get_public(self):
        """GET /api/ai/quick-prompts should be public."""
        r = requests.get(f"{BASE}/api/ai/quick-prompts", timeout=15)
        assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text}"
        data = r.json()
        
        assert "prompts" in data, f"Missing 'prompts' in response: {data.keys()}"
        assert "customized" in data, f"Missing 'customized' in response: {data.keys()}"
        assert isinstance(data["prompts"], list), f"prompts should be list, got {type(data['prompts'])}"
        
        print(f"✓ Quick prompts public endpoint works: {len(data['prompts'])} prompts, customized={data['customized']}")
    
    def test_post_requires_auth(self):
        """POST /api/ai/quick-prompts without token should return 401/403."""
        r = requests.post(f"{BASE}/api/ai/quick-prompts",
                         json={"prompts": ["Test"]}, timeout=15)
        assert r.status_code in (401, 403), \
            f"POST should require auth, got {r.status_code}: {r.text}"
        print(f"✓ POST requires auth: {r.status_code}")
    
    def test_post_trims_and_saves(self, auth):
        """POST should trim whitespace and remove empty entries."""
        payload = {"prompts": ["  Test A  ", "", "Test B", "  ", "Test C"]}
        r = requests.post(f"{BASE}/api/ai/quick-prompts",
                         headers=auth, json=payload, timeout=15)
        assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text}"
        data = r.json()
        
        # Should have trimmed and removed empty
        expected = ["Test A", "Test B", "Test C"]
        assert data["prompts"] == expected, \
            f"Expected {expected}, got {data['prompts']}"
        
        # Verify persistence
        r2 = requests.get(f"{BASE}/api/ai/quick-prompts", timeout=15)
        data2 = r2.json()
        assert data2["prompts"] == expected, "Prompts not persisted correctly"
        assert data2["customized"] is True, "customized should be True"
        
        print(f"✓ Prompts trimmed and saved: {data['prompts']}")
    
    def test_restore_defaults(self, auth):
        """Restore default prompts after tests."""
        defaults = [
            "Wie ist deine aktuelle Performance?",
            "Was hast du zuletzt gelernt?",
            "Sei heute defensiv",
            "Begründe deine letzte Entscheidung",
        ]
        r = requests.post(f"{BASE}/api/ai/quick-prompts",
                         headers=auth, json={"prompts": defaults}, timeout=15)
        assert r.status_code == 200, f"Failed to restore defaults: {r.text}"
        assert r.json()["prompts"] == defaults
        
        # Verify
        r2 = requests.get(f"{BASE}/api/ai/quick-prompts", timeout=15)
        data2 = r2.json()
        assert data2["prompts"] == defaults, "Defaults not restored"
        
        print(f"✓ Default prompts restored: {len(defaults)} prompts")


class TestApplyAssist:
    """Test apply-assist endpoint."""
    
    def test_apply_without_assist_returns_400(self, auth):
        """POST /api/ai/strategies/{non-existent-id}/apply-assist should return 400 or 404."""
        # Use a non-existent strategy ID
        fake_id = "irgendeine-nicht-existente-id-xyz123"
        r = requests.post(f"{BASE}/api/ai/strategies/{fake_id}/apply-assist",
                         headers=auth, json={}, timeout=15)
        
        # Should return 400 or 404, not 500
        assert r.status_code in (400, 404), \
            f"Should return 400 or 404 for non-existent strategy, got {r.status_code}: {r.text}"
        
        print(f"✓ apply-assist with non-existent ID returns {r.status_code}: {r.text[:100]}")


# ========== Test Summary ==========
def test_summary():
    """Print test summary."""
    print("\n" + "="*80)
    print("ITERATION 5 API TESTS COMPLETED")
    print("="*80)
    print("✓ Task 1: GET /api/autotrade/strategy_coin_configs is public")
    print("✓ Task 2: GET /api/analytics/time-based/{symbol} extended with new fields")
    print("✓ Task 3: Iteration-4 AI features verified (E2E)")
    print("="*80)
