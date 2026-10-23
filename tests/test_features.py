import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import metrics  # noqa: E402
import token_status  # noqa: E402
from backend import load_config, market_session, poll_interval  # noqa: E402
from sources import ET  # noqa: E402

LOGIN = datetime(2026, 10, 19, 14, 0, tzinfo=timezone.utc)


def test_token_levels_count_down_from_login():
    def level(hours_after_login):
        return token_status.token_status(LOGIN, LOGIN + timedelta(hours=hours_after_login))["level"]

    assert level(24) == "ok"
    assert level(5 * 24 - 1) == "ok"
    assert level(5 * 24 + 1) == "warning"
    assert level(6 * 24 + 1) == "urgent"
    assert level(7 * 24) == "expired"
    status = token_status.token_status(LOGIN, LOGIN + timedelta(days=6))
    assert status["expires_at"] == (LOGIN + timedelta(days=7)).isoformat()
    assert status["seconds_left"] == 86400


def test_token_missing_and_custom_thresholds():
    assert token_status.token_status(None, LOGIN)["level"] == "missing"
    status = token_status.token_status(LOGIN, LOGIN + timedelta(days=3), warn_days=4)
    assert status["level"] == "warning"


def test_read_login_time_from_schwab_token_file(tmp_path):
    path = tmp_path / "token.json"
    path.write_text(json.dumps({"creation_timestamp": LOGIN.timestamp(), "token": {"refresh_token": "x"}}))
    assert token_status.read_login_time(path) == LOGIN
    path.write_text("{}")
    assert token_status.read_login_time(path) is None
    assert token_status.read_login_time(tmp_path / "missing.json") is None


def test_session_and_poll_cadence():
    config = load_config()
    thursday = datetime(2026, 10, 22, tzinfo=ET)
    cases = {
        (8, 0): ("pre", 300), (9, 30): ("regular", 5), (15, 59): ("regular", 5), (16, 0): ("post", 300),
    }
    for (hour, minute), (session, seconds) in cases.items():
        now = thursday.replace(hour=hour, minute=minute)
        assert market_session(now) == session
        assert poll_interval(config, now, "schwab") == seconds
    saturday = datetime(2026, 10, 24, 11, tzinfo=ET)
    assert market_session(saturday) == "closed"
    assert poll_interval(config, saturday, "schwab") == 300
    assert poll_interval(config, saturday, "demo") == 5


def test_rvol_pace_is_its_own_field():
    quote = {"lastPrice": 10, "openPrice": 10, "closePrice": 10, "totalVolume": 500_000}
    row = metrics.stock_row("X", quote, {"avg_volume_20d": 1_000_000}, session_fraction=0.25)
    assert row["rvol"] == 0.5 and row["rvol_pace"] == 2.0
    assert metrics.stock_row("X", quote, {"avg_volume_20d": 1_000_000})["rvol_pace"] is None


def test_config_keeps_index_etfs_in_breadth_and_topd_thresholds():
    config = load_config()
    assert config["exclude_groups_from_breadth"] == []
    assert config["topd_min_pct_from_open"] == 2.0 and config["topd_min_rvol"] == 1.2
