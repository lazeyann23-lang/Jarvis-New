"""Optional JARVIS Chef metrics must never fabricate local measurements."""
import sys
from types import SimpleNamespace

from jarvis.ui.web.hud_metrics import read_hud_metrics


def test_read_hud_metrics_real_shape():
    data = read_hud_metrics()
    assert set(data) == {"cpu_percent", "ram_percent", "platform"}
    assert data["platform"]
    for field in ("cpu_percent", "ram_percent"):
        assert data[field] is None or 0 <= data[field] <= 100


def test_read_hud_metrics_unavailable(monkeypatch):
    # psutil can be missing on minimal installs. No made-up percentages.
    monkeypatch.setitem(sys.modules, "psutil", None)
    data = read_hud_metrics()
    assert data["cpu_percent"] is None
    assert data["ram_percent"] is None


def test_read_hud_metrics_values(monkeypatch):
    fake = SimpleNamespace(
        cpu_percent=lambda interval=None: 17.24,
        virtual_memory=lambda: SimpleNamespace(percent=33.36),
    )
    monkeypatch.setitem(sys.modules, "psutil", fake)
    data = read_hud_metrics()
    assert data["cpu_percent"] == 17.2
    assert data["ram_percent"] == 33.4
