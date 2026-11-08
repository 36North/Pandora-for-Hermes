"""Tests for pandora_client: query building, error mapping, field fallback, auth retry."""

from __future__ import annotations

import json
import re

import httpx
import pytest

from pandora import pandora_auth, pandora_client


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(pandora_auth, "resolve_credentials",
                        lambda **kw: {"auth_token": "tok", "csrf_token": "csrf",
                                      "device_uuid": "dev-1", "base_url": "https://www.pandora.com"})
    return pandora_client.PandoraClient()


def _response(status=200, payload=None):
    return httpx.Response(status, json=payload or {},
                          request=httpx.Request("POST", "https://www.pandora.com/api/v1/graphql/graphql"))


def test_literals_and_enums():
    assert pandora_client._lit("a\"b") == '"a\\"b"'
    assert pandora_client._lit(True) == "true"
    assert pandora_client._lit(["ST", "SF"]) == '["ST", "SF"]'
    assert pandora_client._enum("up") == "UP"
    with pytest.raises(pandora_client.PandoraError):
        pandora_client._enum("UP; DROP")


def test_source_id_normalization():
    assert pandora_client.normalize_source_id("ST:0:123") == "ST:0:123"
    with pytest.raises(pandora_client.PandoraError):
        pandora_client.normalize_source_id("123")
    with pytest.raises(pandora_client.PandoraError):
        pandora_client.normalize_source_id("XX:1")


def test_graphql_error_code_parsing(client, monkeypatch):
    payload = {"errors": [{"message": "Ensure that source is set (SOURCE_NOT_SET)",
                           "extensions": {"code": "GRAPHQL_VALIDATION_FAILED"}}]}
    monkeypatch.setattr(client._client, "post", lambda *a, **kw: _response(200, payload))
    with pytest.raises(pandora_client.PandoraAPIError) as exc:
        client.playback_current("ST:0:1")
    assert exc.value.code == "SOURCE_NOT_SET"


def test_optional_field_fallback_strips_rejected_field(client, monkeypatch):
    calls = []

    def fake_post(url, headers=None, content=None):
        query = json.loads(content)["query"]
        calls.append(query)
        if "feedbackValue" in query:
            return _response(200, {"errors": [
                {"message": 'Cannot query field "feedbackValue" on type "TrackItem".'}]})
        return _response(200, {"data": {"playback": {"current": {"trackToken": "t", "index": 2}}}})

    monkeypatch.setattr(client._client, "post", fake_post)
    data = client.playback_current("ST:0:1")
    assert data["playback"]["current"]["trackToken"] == "t"
    assert len(calls) == 2
    assert "feedbackValue" not in calls[-1]


def test_strip_selection_prefers_the_shallowest_match():
    """The optional-field stripper must take the top-level entry, not a nested one."""
    query = "query {\n  id\n  nested {\n    id\n  }\n}\n"
    assert pandora_client._strip_selection(query, "id") == "query {\n  nested {\n    id\n  }\n}\n"


def test_strip_selection_returns_query_unchanged_when_absent():
    query = "query { ok }\n"
    assert pandora_client._strip_selection(query, "ghostField") is query


def test_optional_field_fallback_keeps_longer_identifiers_intact(client, monkeypatch):
    """Regression: stripping field 'id' must not rewrite `deviceUuid` to `deviceUu`."""
    calls = []

    def fake_post(url, headers=None, content=None):
        query = json.loads(content)["query"]
        calls.append(query)
        # Two standalone `id` leaves exist: setSource's own and TrackItem.track's.
        if len(re.findall(r"(?m)^[ \t]*id[ \t]*$", query)) > 1:
            return _response(200, {"errors": [
                {"message": 'Cannot query field "id" on type "PlaybackSource".'}]})
        return _response(200, {"data": {"playback": {"setSource": {"type": "ST", "current": {}}}}})

    monkeypatch.setattr(client._client, "post", fake_post)
    client.playback_set_source("ST:0:1")

    assert len(calls) == 2
    retried = calls[-1]
    assert "deviceUuid:" in retried
    assert "deviceUu:" not in retried
    assert len(re.findall(r"(?m)^[ \t]*id[ \t]*$", retried)) == 1


def test_optional_field_fallback_removes_a_whole_block_selection(client, monkeypatch):
    """A rejected block (`current { ... }`) must be dropped with its body, not just its header."""
    calls = []

    def fake_post(url, headers=None, content=None):
        query = json.loads(content)["query"]
        calls.append(query)
        if re.search(r"(?m)^[ \t]*current[ \t]*\{", query):
            return _response(200, {"errors": [
                {"message": 'Cannot query field "current" on type "PlaybackSource".'}]})
        return _response(200, {"data": {"playback": {"setSource": {"id": "ST:0:1", "type": "ST"}}}})

    monkeypatch.setattr(client._client, "post", fake_post)
    client.playback_set_source("ST:0:1")

    retried = calls[-1]
    assert "current {" not in retried
    # The whole body must go too — the old substring strip left `{` and the
    # TrackItem/AudioAdItem fragments behind, producing an invalid document.
    assert "__typename" not in retried
    assert not re.search(r"(?m)^[ \t]*\{\s*$", retried)
    assert retried.count("{") == retried.count("}")
    assert "deviceUuid:" in retried


def test_optional_field_not_in_the_document_fails_fast(client, monkeypatch):
    """An unlocatable field must raise immediately, not re-send the same query."""
    calls = []

    def fake_post(url, headers=None, content=None):
        calls.append(json.loads(content)["query"])
        return _response(200, {"errors": [
            {"message": 'Cannot query field "ghostField" on type "X".'}]})

    monkeypatch.setattr(client._client, "post", fake_post)
    with pytest.raises(pandora_client.PandoraAPIError):
        client._run("query { ok }", optional=["ghostField"])
    assert len(calls) == 1


def test_401_triggers_relogin_and_retry(client, monkeypatch):
    state = {"auth_token": "new", "csrf_token": "c2", "device_uuid": "dev-1",
             "base_url": "https://www.pandora.com"}
    monkeypatch.setattr(pandora_auth, "relogin", lambda: state)
    monkeypatch.setattr(client, "_refresh_auth", lambda: setattr(client, "_state", state))

    statuses = []

    def fake_post(url, headers=None, content=None):
        statuses.append(headers.get("X-AuthToken"))
        if len(statuses) == 1:
            return _response(401, {"message": "expired"})
        return _response(200, {"data": {"ok": True}})

    monkeypatch.setattr(client._client, "post", fake_post)
    assert client.graphql("{ ok }") == {"ok": True}
    assert statuses == ["tok", "new"]


def test_search_query_contains_types_and_query(client, monkeypatch):
    seen = {}

    def fake_post(url, headers=None, content=None):
        seen.update(json.loads(content))
        return _response(200, {"data": {"search": {"items": []}}})

    monkeypatch.setattr(client._client, "post", fake_post)
    client.search('jazz "live"', types=["ST", "sf"], limit=5)
    query = seen["query"]
    assert 'query: "jazz \\"live\\""' in query
    assert "types: [ST, SF]" in query
    assert "limit: 5" in query


def test_playback_mutations_carry_device_and_index(client, monkeypatch):
    seen = {}

    def fake_post(url, headers=None, content=None):
        seen.update(json.loads(content))
        return _response(200, {"data": {"playback": {"skip": {"status": "OK"}}}})

    monkeypatch.setattr(client._client, "post", fake_post)
    client.playback_skip("ST:0:9", 3, 42)
    assert 'deviceUuid: "dev-1"' in seen["query"]
    assert "index: 3" in seen["query"]
    assert "elapsedTime: 42" in seen["query"]
