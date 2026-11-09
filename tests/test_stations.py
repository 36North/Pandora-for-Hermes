"""Hybrid station list: recents first, then the collection, deduped by id."""
from __future__ import annotations

def test_merge_prefers_recents_and_dedupes_by_id():
    from pandora.dashboard.plugin_api import _merge_stations

    recents = [{"id": "ST:1", "name": "Alpha"}, {"id": "ST:2", "name": "Beta"}]
    collection = [{"id": "st:2", "name": "Beta"},   # same station, other casing
                  {"id": "ST:3", "name": "Gamma"},
                  {"id": "ST:1", "name": "Alpha"}]

    merged = _merge_stations(recents, collection, 10)

    assert [s["name"] for s in merged] == ["Alpha", "Beta", "Gamma"]
    assert [s["id"] for s in merged] == ["ST:1", "ST:2", "ST:3"]


def test_merge_caps_recents_but_keeps_the_rest():
    from pandora.dashboard.plugin_api import _merge_stations

    recents = [{"id": f"ST:{i}", "name": f"R{i}"} for i in range(5)]
    collection = [{"id": "ST:9", "name": "Last"}]

    merged = _merge_stations(recents, collection, 3)

    assert [s["id"] for s in merged] == ["ST:0", "ST:1", "ST:2", "ST:9"]


def test_merge_falls_back_to_name_when_an_id_is_missing():
    from pandora.dashboard.plugin_api import _merge_stations

    merged = _merge_stations([{"name": "Alpha"}], [{"name": "alpha"}, {"name": "Beta"}], 5)

    assert [s["name"] for s in merged] == ["Alpha", "Beta"]


def test_all_stations_follows_the_offset_until_a_short_page():
    from pandora.dashboard.plugin_api import _all_stations

    class FakeClient:
        def __init__(self):
            self.calls = []

        def collection(self, types=None, limit=50, offset=0):
            self.calls.append((limit, offset))
            count = 50 if offset == 0 else 7
            return {"collection": {"items": [
                {"__typename": "Station", "id": f"ST:{offset + i}", "name": f"S{offset + i}"}
                for i in range(count)
            ]}}

    client = FakeClient()
    stations = _all_stations(client)

    assert len(stations) == 57
    assert client.calls == [(50, 0), (50, 50)]


def test_all_stations_stops_when_the_account_has_none():
    from pandora.dashboard.plugin_api import _all_stations

    class EmptyClient:
        def collection(self, **kwargs):
            return {"collection": {"items": []}}

    assert _all_stations(EmptyClient()) == []
