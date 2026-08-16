"""Tests for DirectControl (virtual line-in) music-service playback."""

import json
from types import SimpleNamespace

import pytest

from soco.exceptions import MusicServiceException
from soco.music_services.browser import direct_control
from soco.music_services.browser.direct_control import (
    SONOS_API_KEY,
    DirectControlSession,
    direct_control_app_id,
    direct_control_client_id,
    direct_control_container_type,
    direct_control_metadata,
    direct_control_playable_item_types,
    direct_control_provider,
    direct_control_session,
    direct_control_uri,
    load_container,
)


def test_direct_control_provider():
    assert direct_control_provider(12) == "spotify"
    assert direct_control_provider("12") == "spotify"
    assert direct_control_provider(9) == "spotify"
    assert direct_control_provider(236) == "pandora"
    assert direct_control_provider(239) == "audible"
    # Non-DirectControl services have no provider label.
    assert direct_control_provider(204) is None
    assert direct_control_provider(0) is None


def test_direct_control_app_id():
    # The DirectControl application id is the value the speaker reports as
    # r:DirectControlClientID, distinct from the VLI URI provider label.
    assert direct_control_app_id(9) == "spotify.connect.adapter"
    assert direct_control_app_id(12) == "spotify.connect.adapter"
    assert direct_control_app_id(236) == "com.pandora.dc"
    assert direct_control_app_id(239) == "com.audible.mobile.sonos"
    assert direct_control_app_id(204) is None


def test_direct_control_container_type():
    assert direct_control_container_type(12) == "playlist.spotify.connect"
    assert direct_control_container_type(236) == "program.pandora.connect"
    assert direct_control_container_type(239) == "audiobook.audible.connect"
    # Only live-verified types are listed; others are unknown.
    assert direct_control_container_type(999) is None


def test_direct_control_playable_item_types():
    # Spotify's playable units are its containers (via can_browse in the
    # browser) and its tracks; Pandora stations are programs; Audible books
    # are audiobooks.
    assert direct_control_playable_item_types(12) == {"track"}
    assert direct_control_playable_item_types(236) == {"program"}
    assert direct_control_playable_item_types(239) == {"audiobook"}
    assert direct_control_playable_item_types(999) is None


def test_direct_control_uri():
    assert (
        direct_control_uri("RINCON_00012345678901234", "spotify")
        == "x-sonos-vli:RINCON_00012345678901234:2,spotify:"
    )
    assert (
        direct_control_uri("RINCON_00012345678901234", "spotify", "label-1")
        == "x-sonos-vli:RINCON_00012345678901234:2,spotify:label-1"
    )


def test_direct_control_metadata():
    uri = direct_control_uri("RINCON_00012345678901234", "spotify")
    metadata = direct_control_metadata("RINCON_00012345678901234", "spotify", uri)

    assert 'id="spotify"' in metadata
    assert "<dc:title>spotify</dc:title>" in metadata
    assert "<upnp:class>object.item.audioItem.linein</upnp:class>" in metadata
    assert '<res protocolInfo="x-sonos-vli:*:audio:*">' in metadata
    assert uri in metadata
    assert '<vli cookie="7" group=""></vli>' in metadata


def test_direct_control_metadata_custom_title():
    uri = direct_control_uri("RINCON_00012345678901234", "spotify")
    metadata = direct_control_metadata(
        "RINCON_00012345678901234", "spotify", uri, title="Spotify"
    )

    assert "<dc:title>Spotify</dc:title>" in metadata
    assert 'id="spotify"' in metadata


class FakeResponse:
    def __init__(self, status=200, body=b"{}"):
        self.status = status
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fake_http_error(code, body=b"{}"):
    import io
    import urllib.error

    return urllib.error.HTTPError(
        "https://192.168.1.51:1443/api/v1/groups/gid/playback/loadContainer",
        code,
        "Error",
        {},
        io.BytesIO(body),
    )


def _patch_urlopen(monkeypatch, response):
    # ``load_container`` imports urllib lazily inside the function; make sure
    # the module exposes it before we patch its ``urlopen``.
    import urllib.error
    import urllib.request  # noqa: F401

    direct_control.urllib = urllib
    captured = {}

    def fake_urlopen(request, context=None, timeout=None):
        captured["request"] = request
        captured["context"] = context
        captured["timeout"] = timeout
        if isinstance(response, urllib.error.HTTPError):
            raise response
        return response

    monkeypatch.setattr(direct_control.urllib.request, "urlopen", fake_urlopen)
    return captured


def test_load_container_posts_verified_schema(monkeypatch):
    captured = _patch_urlopen(monkeypatch, FakeResponse())
    device = SimpleNamespace(ip_address="192.168.1.51")

    result = load_container(
        device,
        group_uid="RINCON_00012345678901234:4215913542",
        object_id="spotify:playlist:37i9dQZF1E4sD262twcXeU",
        service_id=12,
        account_serial=63,
        name="Zach Bryan Radio",
    )

    assert result is True
    request = captured["request"]
    assert request.method == "POST"
    assert request.full_url == (
        "https://192.168.1.51:1443/api/v1/groups/"
        "RINCON_00012345678901234:4215913542/playback/loadContainer"
    )
    headers = {name.lower(): value for name, value in request.header_items()}
    assert headers["x-sonos-api-key"] == SONOS_API_KEY
    assert headers["content-type"] == "application/json"
    body = json.loads(request.data.decode("utf-8"))
    assert body == {
        "containerId": {
            "objectId": "spotify:playlist:37i9dQZF1E4sD262twcXeU",
            "serviceId": "12",
            "accountId": "sn_63",
        },
        "containerMetadata": {
            "type": "playlist.spotify.connect",
            "name": "Zach Bryan Radio",
            "imageUrl": "",
            "description": "",
            "tags": [],
        },
        "playOnCompletion": True,
    }


def test_load_container_propagates_image_and_description(monkeypatch):
    captured = _patch_urlopen(monkeypatch, FakeResponse())
    device = SimpleNamespace(ip_address="192.168.1.51")

    load_container(
        device,
        "gid",
        "object:1",
        service_id=12,
        account_serial=63,
        name="Thing",
        image_url="https://i.scdn.co/image/abc",
        description="A radio station",
        tags=["explicit"],
    )

    body = json.loads(captured["request"].data.decode("utf-8"))
    metadata = body["containerMetadata"]
    assert metadata["imageUrl"] == "https://i.scdn.co/image/abc"
    assert metadata["description"] == "A radio station"
    assert metadata["tags"] == ["explicit"]


def test_load_container_accepts_explicit_container_type(monkeypatch):
    captured = _patch_urlopen(monkeypatch, FakeResponse())
    device = SimpleNamespace(ip_address="192.168.1.51")

    load_container(
        device,
        "gid",
        "object:1",
        service_id=999,
        account_serial=1,
        name="Thing",
        container_type="thing.connect",
    )

    body = json.loads(captured["request"].data.decode("utf-8"))
    assert body["containerMetadata"]["type"] == "thing.connect"


def test_load_container_raises_without_verified_type(monkeypatch):
    _patch_urlopen(monkeypatch, FakeResponse())
    device = SimpleNamespace(ip_address="192.168.1.51")

    with pytest.raises(MusicServiceException, match="container type"):
        load_container(
            device,
            "gid",
            "object:1",
            service_id=999,
            account_serial=1,
            name="Thing",
        )


def test_load_container_raises_on_http_error(monkeypatch):
    _patch_urlopen(monkeypatch, _fake_http_error(500, b"boom"))
    device = SimpleNamespace(ip_address="192.168.1.51")

    with pytest.raises(MusicServiceException, match="HTTP 500"):
        load_container(
            device,
            "gid",
            "object:1",
            service_id=12,
            account_serial=63,
            name="Thing",
        )


def test_load_container_accepts_custom_port(monkeypatch):
    captured = _patch_urlopen(monkeypatch, FakeResponse())
    device = SimpleNamespace(ip_address="192.168.1.51")

    load_container(
        device,
        "gid",
        "object:1",
        service_id=12,
        account_serial=63,
        name="Thing",
        port=4433,
    )

    assert captured["request"].full_url.startswith("https://192.168.1.51:4433/")


def test_direct_control_session_parses_playback_session(monkeypatch):
    captured = _patch_urlopen(
        monkeypatch,
        FakeResponse(
            body=json.dumps(
                {
                    "playbackSession": {
                        "_objectType": "directControl",
                        "clientId": "spotify.connect.adapter",
                        "isSuspended": False,
                        "accountId": "",
                    }
                }
            ).encode()
        ),
    )
    device = SimpleNamespace(ip_address="192.168.1.51")

    session = direct_control_session(device, "gid")

    assert session == DirectControlSession(
        client_id="spotify.connect.adapter", account_id="", suspended=False
    )
    assert captured["request"].method == "GET"
    assert captured["request"].full_url.endswith("/api/v1/groups/gid/playbackMetadata")


def test_direct_control_session_parses_suspended_and_account_id(monkeypatch):
    _patch_urlopen(
        monkeypatch,
        FakeResponse(
            body=json.dumps(
                {
                    "playbackSession": {
                        "clientId": "com.audible.mobile.sonos",
                        "isSuspended": True,
                        "accountId": "sn_21",
                    }
                }
            ).encode()
        ),
    )
    device = SimpleNamespace(ip_address="192.168.1.51")

    session = direct_control_session(device, "gid")

    assert session == DirectControlSession(
        client_id="com.audible.mobile.sonos", account_id="sn_21", suspended=True
    )


def test_direct_control_client_id_parses_playback_session(monkeypatch):
    captured = _patch_urlopen(
        monkeypatch,
        FakeResponse(
            body=json.dumps(
                {
                    "playbackSession": {
                        "_objectType": "directControl",
                        "clientId": "spotify.connect.adapter",
                        "isSuspended": False,
                        "accountId": "",
                    }
                }
            ).encode()
        ),
    )
    device = SimpleNamespace(ip_address="192.168.1.51")

    client_id = direct_control_client_id(device, "gid")

    assert client_id == "spotify.connect.adapter"
    assert captured["request"].method == "GET"
    assert captured["request"].full_url.endswith("/api/v1/groups/gid/playbackMetadata")


def test_direct_control_client_id_none_without_session(monkeypatch):
    _patch_urlopen(
        monkeypatch,
        FakeResponse(body=json.dumps({"playbackSession": {"clientId": ""}}).encode()),
    )
    device = SimpleNamespace(ip_address="192.168.1.51")

    assert direct_control_client_id(device, "gid") is None


def test_direct_control_client_id_none_on_http_error(monkeypatch):
    _patch_urlopen(monkeypatch, _fake_http_error(404))
    device = SimpleNamespace(ip_address="192.168.1.51")

    assert direct_control_client_id(device, "gid") is None
