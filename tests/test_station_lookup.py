"""Station lookup must search the whole account, not just the first page."""


def _page(offset: int, count: int):
    return {"collection": {"items": [
        {"__typename": "Station", "id": f"ST:{offset + i}", "name": f"Station {offset + i}"}
        for i in range(count)
    ]}}


class PagedClient:
    """50 stations on page one, the wanted one on page two."""

    def __init__(self):
        self.calls = []

    def collection(self, types=None, limit=50, offset=0):
        self.calls.append(offset)
        if offset == 0:
            return _page(0, 50)
        return {"collection": {"items": [
            {"__typename": "Station", "id": "ST:99", "name": "Radiohead Radio"},
            {"__typename": "Station", "id": "ST:100", "name": "Adele Radio"},
        ]}}


def test_find_station_searches_past_the_first_page():
    from pandora.pandora_tools import _find_station

    client = PagedClient()
    found = _find_station(client, "radiohead")

    assert found.get("id") == "ST:99"
    assert client.calls == [0, 50]


def test_find_station_matches_exact_name_before_substring():
    from pandora.pandora_tools import _find_station

    class ExactClient:
        def collection(self, types=None, limit=50, offset=0):
            return {"collection": {"items": [
                {"id": "ST:1", "name": "Adele Radio"},
                {"id": "ST:2", "name": "Adele"},
            ]}}

    assert _find_station(ExactClient(), "adele").get("id") == "ST:2"


def test_find_station_returns_empty_when_absent():
    from pandora.pandora_tools import _find_station

    class EmptyClient:
        def collection(self, types=None, limit=50, offset=0):
            return {"collection": {"items": []}}

    assert _find_station(EmptyClient(), "nothing here") == {}
