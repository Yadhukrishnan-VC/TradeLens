import pytest

from conftest import fresh_engine
from fastapi.testclient import TestClient

from tradelens.api.main import create_app
from tradelens.config import Settings
from tradelens.data.synthetic import DemoProvider
from tradelens.db import make_session_factory


@pytest.fixture
def client():
    settings = Settings("sqlite://", "data", True, 100_000.0, "semi_auto", "paper")
    app = create_app(settings, provider=DemoProvider(), session_factory=make_session_factory(fresh_engine()))
    return TestClient(app)


def test_health_and_strategies(client):
    assert client.get("/health").json()["broker"] == "paper"
    names = {s["name"] for s in client.get("/strategies").json()}
    assert {"ema_cross", "donchian_breakout"} <= names


def test_backtest_persists_run_and_fit_map(client):
    r = client.post("/backtests", json={"strategy": "donchian_breakout", "symbol": "DEMO2"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["metrics"]["n_trades"] > 0 and body["verdict"] in {"candidate", "no_edge", "insufficient_data"}
    runs = client.get("/backtests").json()
    assert len(runs) == 1 and "trades" not in runs[0]
    assert len(client.get(f"/backtests/{body['run_id']}").json()["trades"]) == body["n_trades"]
    fits = client.get("/fits").json()
    assert fits[0]["strategy"] == "donchian_breakout" and fits[0]["avg_volume"] > 0 and fits[0]["n_bars"] == 1500


def test_errors_are_clean(client):
    assert client.post("/backtests", json={"strategy": "nope", "symbol": "DEMO1"}).status_code == 400
    assert client.post("/backtests", json={"strategy": "ema_cross", "symbol": "DEMO1", "params": {"bad": 1}}).status_code == 400
    assert client.post("/orders/999/approve").status_code == 404


def test_account_and_kill_switch(client):
    assert client.get("/account").json()["equity"] == 100_000
    client.post("/kill-switch", json={"active": True})
    assert client.get("/account").json()["kill_switch"] is True


def test_symbols_and_risk_config(client):
    assert client.get("/symbols").json() == ["DEMO1", "DEMO2", "DEMO3"]
    cfg = client.get("/risk-config").json()
    assert cfg["risk_pct"] == 0.01 and cfg["max_open_positions"] == 5 and cfg["daily_loss_limit_pct"] == 0.03


def test_dashboard_is_served_when_built(tmp_path):
    (tmp_path / "index.html").write_text("<html>tradelens ui</html>")
    settings = Settings("sqlite://", "data", True, 100_000.0, "semi_auto", "paper")
    app = create_app(settings, provider=DemoProvider(), frontend_dist=tmp_path,
                     session_factory=make_session_factory(fresh_engine()))
    c = TestClient(app)
    assert "tradelens ui" in c.get("/ui/").text
    assert c.get("/", follow_redirects=False).headers["location"] == "/ui/"
    assert c.get("/health").status_code == 200          # API unaffected by the static mount
