"""Discovery must stop the browser it creates.

``pychromecast.discovery.discover_chromecasts`` returns a live browser. Dropping
it leaves a multicast listener thread and a socket bound to 5353 alive, so every
listing scan (``/devices`` after its TTL) added another one.
"""
import pytest


def _fake_browser(calls):
    class FakeZeroconf:
        def close(self):
            calls.append("zeroconf.close")

    class FakeBrowser:
        zc = FakeZeroconf()

        def stop_discovery(self):
            calls.append("stop_discovery")

    return FakeBrowser()


def _patch(monkeypatch, devices, calls):
    from pychromecast import discovery

    import pandora.pandora_cast as pc

    monkeypatch.setattr(pc, "_require_cast", lambda: None)
    monkeypatch.setattr(discovery, "discover_chromecasts",
                        lambda timeout: (devices, _fake_browser(calls)))
    return pc


def test_discover_devices_stops_the_browser(monkeypatch):
    calls = []

    class FakeDevice:
        friendly_name = "Kitchen display"
        model_name = "Google Nest Hub"
        cast_type = "cast"
        host = "192.168.0.50"
        port = 8009
        uuid = "uuid-1"

    pc = _patch(monkeypatch, [FakeDevice()], calls)
    devices = pc.discover_devices(timeout=0.1)

    assert devices == [{
        "name": "Kitchen display", "model": "Google Nest Hub", "type": "cast",
        "host": "192.168.0.50", "port": 8009, "uuid": "uuid-1",
    }]
    assert calls == ["stop_discovery", "zeroconf.close"]


def test_discover_devices_stops_the_browser_when_a_device_breaks(monkeypatch):
    calls = []

    class ExplodingDevice:
        @property
        def friendly_name(self):
            raise RuntimeError("boom")

    pc = _patch(monkeypatch, [ExplodingDevice()], calls)
    with pytest.raises(RuntimeError):
        pc.discover_devices(timeout=0.1)

    assert calls == ["stop_discovery", "zeroconf.close"]
