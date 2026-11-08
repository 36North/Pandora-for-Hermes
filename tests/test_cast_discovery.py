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


# ── live browser (#3): one listener, started on demand, closed when idle ─────


class _FakeCastInfo:
    friendly_name = "Kitchen display"
    model_name = "Google Nest Hub"
    cast_type = "cast"
    host = "192.168.0.50"
    port = 8009
    uuid = "uuid-1"


def _install_browser_fakes(monkeypatch):
    from pychromecast import discovery

    import zeroconf

    closes = []

    class FakeZeroconf:
        def close(self):
            closes.append("zeroconf.close")

    class FakeBrowser:
        instances = []

        def __init__(self, listener, zc):
            self.listener = listener
            self.zc = zc
            self.devices = {}
            self.started = False
            self.stopped = False
            FakeBrowser.instances.append(self)

        def start_discovery(self):
            self.started = True
            self.devices = {"uuid-1": _FakeCastInfo()}

        def stop_discovery(self):
            self.stopped = True

    monkeypatch.setattr(zeroconf, "Zeroconf", lambda: FakeZeroconf())
    monkeypatch.setattr(discovery, "CastBrowser", FakeBrowser)
    monkeypatch.setattr(discovery, "SimpleCastListener", lambda: object())
    return closes, FakeBrowser


def test_list_devices_starts_one_browser_and_reuses_it(monkeypatch):
    import pandora.pandora_cast as pc

    closes, FakeBrowser = _install_browser_fakes(monkeypatch)
    browser = pc._DeviceBrowser()
    monkeypatch.setattr(pc, "_device_browser", browser)

    first = pc.list_devices(settle=0.0)
    second = pc.list_devices(settle=0.0)

    assert first == second == [{
        "name": "Kitchen display", "model": "Google Nest Hub", "type": "cast",
        "host": "192.168.0.50", "port": 8009, "uuid": "uuid-1",
    }]
    assert len(FakeBrowser.instances) == 1      # one listener, not one per call
    assert FakeBrowser.instances[0].stopped is False
    browser.close()
    assert closes == ["zeroconf.close"]


def test_idle_browser_stops_and_the_next_listing_restarts_it(monkeypatch):
    import pandora.pandora_cast as pc

    closes, FakeBrowser = _install_browser_fakes(monkeypatch)
    browser = pc._DeviceBrowser(idle_seconds=30.0)
    monkeypatch.setattr(pc, "_device_browser", browser)

    pc.list_devices(settle=0.0)
    assert browser._maybe_stop_idle(now=pc.time.monotonic() + 29) is False
    assert browser._maybe_stop_idle(now=pc.time.monotonic() + 31) is True
    assert FakeBrowser.instances[0].stopped is True
    assert closes == ["zeroconf.close"]

    pc.list_devices(settle=0.0)             # next listing starts a fresh one
    assert len(FakeBrowser.instances) == 2
    browser.close()
