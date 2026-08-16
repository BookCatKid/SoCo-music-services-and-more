"""Tests for music-service playback URI and DIDL metadata builders."""

from types import SimpleNamespace

import pytest

from soco.exceptions import MusicServiceException
from soco.music_services.browser import MusicServiceBrowseItem, MusicServiceBrowser
from soco.music_services.browser.playback import (
    build_metadata,
    build_uri,
    escape_id,
    normalize_item_type,
    resolve_item,
)


def make_browser(
    service_id="204", serial=3, udn="SA_RINCON52231_X_#Svc204-00abcdef-Token"
):
    account = SimpleNamespace(serial_number=serial, udn=udn)
    music_service = SimpleNamespace(desc="SA_RINCON52231_")
    return SimpleNamespace(
        service_id=service_id,
        service_name="Example",
        account=account,
        music_service=music_service,
    )


class FakeDevice:
    """The small SoCo surface MusicServiceBrowser needs."""

    household_id = "Sonos_household"
    uid = "RINCON_000000000001400"

    class _Sys:
        def GetString(self, args):  # pylint: disable=invalid-name
            return {"StringValue": "player-device-id"}

    systemProperties = _Sys()


def test_normalize_item_type():
    assert normalize_item_type("track") == "track"
    assert normalize_item_type("trackList.program") == "program"
    assert normalize_item_type("station.broadcast") == "broadcast"
    assert normalize_item_type("") == ""
    assert normalize_item_type(None) == ""


def test_escape_id():
    assert escape_id("song:1844932150") == "song%3a1844932150"
    assert escape_id("a/b?c=d&e#f") == "a%2fb%3fc%3dd%26e%23f"


def test_build_uri_track_apple_music_flags_override():
    browser = make_browser(service_id="204")
    uri = build_uri(browser, "song:1844932150", "track", "audio/aac")

    assert uri == ("x-sonos-http:song%3a1844932150.mp4?sid=204&flags=8224&sn=3")


def test_build_uri_track_generic_mime_extension_and_flags():
    browser = make_browser(service_id="201")
    assert build_uri(browser, "song:1", "track", "audio/aac").endswith(
        ".mp4?sid=201&flags=32&sn=3"
    )
    assert build_uri(browser, "song:1", "track", "audio/mpeg").endswith(
        ".mp3?sid=201&flags=32&sn=3"
    )
    # Unknown MIME falls back to .mp3 with the default flags.
    assert build_uri(browser, "song:1", "track", "audio/weird").endswith(
        ".mp3?sid=201&flags=32&sn=3"
    )


def test_build_uri_track_special_mime_types():
    browser = make_browser(service_id="12")
    # Spotify tracks map to x-sonos-spotify with the player-verified flags
    # (8224), never the x-sonosapi-stream radio scheme (review regression
    # test: Spotify audio/x-spotify maps to a Spotify resource).
    assert build_uri(browser, "spotify:track:1", "track", "audio/x-spotify") == (
        "x-sonos-spotify:spotify%3atrack%3a1?sid=12&flags=8224&sn=3"
    )
    assert build_uri(browser, "track:1", "track", "audio/flac") == (
        "x-sonos-http:track%3a1.flac?sid=12&flags=0&sn=3"
    )


def test_build_uri_stream_variants():
    browser = make_browser(service_id="254")
    assert build_uri(browser, "s49815", "stream", "") == (
        "x-sonosapi-stream:s49815?sid=254&flags=32&sn=3"
    )
    assert build_uri(browser, "s49815", "stream", "audio/aac") == (
        "x-sonosapi-stream:s49815?sid=254&flags=8224&sn=3"
    )
    assert build_uri(browser, "s1", "stream", "application/x-mpegurl") == (
        "x-sonosapi-hls:s1?sid=254&flags=288&sn=3"
    )


def test_build_uri_program_show_audiobook():
    browser = make_browser(service_id="314")
    assert build_uri(browser, "Playlist:mix1", "program") == (
        "x-sonosapi-radio:Playlist%3amix1?sid=314&flags=104&sn=3"
    )
    assert build_uri(browser, "show:1", "show") == (
        "x-sonosapi-hls-static:show%3a1?sid=314&flags=24616&sn=3"
    )
    assert build_uri(browser, "book:1", "audiobook") == (
        "x-rincon-cpcontainer:101340c8book%3a1?sid=314&flags=16584&sn=3"
    )


def test_build_uri_unknown_type_raises():
    browser = make_browser()
    with pytest.raises(MusicServiceException, match="cannot be played"):
        build_uri(browser, "library", "albumList")


def test_build_uri_normalizes_compound_type():
    browser = make_browser(service_id="303")
    uri = build_uri(browser, "sonos:2997", "trackList.program")

    assert uri == "x-sonosapi-radio:sonos%3a2997?sid=303&flags=104&sn=3"


def test_build_metadata_contains_desc_class_and_item_id():
    browser = make_browser()
    metadata = build_metadata(browser, "song:1", "Title", "track")

    assert 'id="10030020song%3a1"' in metadata
    assert "<upnp:class>object.item.audioItem.musicTrack</upnp:class>" in metadata
    assert ">SA_RINCON52231_X_#Svc204-00abcdef-Token</desc>" in metadata
    assert "<res" not in metadata


def test_build_metadata_spotify_has_protocol_info_and_udn():
    # Review regression: the Spotify DIDL must carry the Spotify resource
    # protocol info and the configured account UDN (in <desc>) for the
    # player to dereference the account's credentials.
    browser = make_browser()
    uri = build_uri(browser, "spotify:track:1", "track", "audio/x-spotify")
    metadata = build_metadata(
        browser, "spotify:track:1", "So What", "track", mime="audio/x-spotify", uri=uri
    )

    assert '<res protocolInfo="sonos.com-spotify:*:audio/x-spotify:*">' in metadata
    assert ">SA_RINCON52231_X_#Svc204-00abcdef-Token</desc>" in metadata
    assert "<upnp:class>object.item.audioItem.musicTrack</upnp:class>" in metadata
    assert uri.replace("&", "&amp;") in metadata


def test_build_metadata_stream_uses_broadcast_class():
    browser = make_browser()
    metadata = build_metadata(browser, "s1", "Station", "stream")

    assert 'id="00090000s1"' in metadata
    assert "<upnp:class>object.item.audioItem.audioBroadcast</upnp:class>" in metadata


def test_build_metadata_escapes_title():
    browser = make_browser()
    metadata = build_metadata(browser, "song:1", "A & B < C", "track")

    assert "<dc:title>A &amp; B &lt; C</dc:title>" in metadata


def test_resolve_item_uses_browse_fields_without_media_call():
    calls = []
    browser = SimpleNamespace(get_media_metadata=lambda *_a: calls.append(1) or None)
    item = MusicServiceBrowseItem(
        item_id="song:1",
        title="Title",
        kind="mediaMetadata",
        item_type="track",
        raw={"mimeType": "audio/aac"},
    )

    item_id, item_type, mime, title = resolve_item(browser, item)

    assert (item_id, item_type, mime, title) == (
        "song:1",
        "track",
        "audio/aac",
        "Title",
    )
    assert calls == []


def test_resolve_item_fetches_media_for_compound_type():
    browser = SimpleNamespace(
        get_media_metadata=lambda _id: {
            "itemType": "program",
            "mimeType": "",
            "title": "Hit List",
        }
    )
    item = MusicServiceBrowseItem(
        item_id="sonos:2997",
        title="Hit List",
        kind="mediaMetadata",
        item_type="trackList.program",
        raw={},
    )

    item_id, item_type, mime, title = resolve_item(browser, item)

    assert (item_id, item_type, mime, title) == (
        "sonos:2997",
        "program",
        "",
        "Hit List",
    )


def test_resolve_item_keeps_known_type_when_media_disagrees():
    # SiriusXM's ``trackList.program`` channels report ``itemType=track`` from
    # getMediaMetadata, but the browse type (program) is authoritative and
    # must not be overridden to a track.
    browser = SimpleNamespace(
        get_media_metadata=lambda _id: {
            "itemType": "track",
            "mimeType": "application/x-mpegURL",
            "title": "Queens of Pop",
        }
    )
    item = MusicServiceBrowseItem(
        item_id="channel-xtra:61893114",
        title="Queens of Pop",
        kind="mediaMetadata",
        item_type="trackList.program",
        raw={},
    )

    item_id, item_type, mime, title = resolve_item(browser, item)

    assert (item_id, item_type, mime, title) == (
        "channel-xtra:61893114",
        "program",
        "application/x-mpegurl",
        "Queens of Pop",
    )


def test_resolve_item_string_id_fetches_media():
    browser = SimpleNamespace(
        get_media_metadata=lambda _id: {
            "itemType": "stream",
            "mimeType": "audio/aac",
            "title": "Station",
        }
    )

    assert resolve_item(browser, "s49815") == (
        "s49815",
        "stream",
        "audio/aac",
        "Station",
    )


def test_resolve_item_falls_back_when_media_fails():
    browser = SimpleNamespace(
        get_media_metadata=lambda _id: (_ for _ in ()).throw(
            MusicServiceException("provider down")
        )
    )
    item = MusicServiceBrowseItem(
        item_id="s1",
        title="Station",
        kind="mediaMetadata",
        item_type="stream",
        raw={},
    )

    item_id, item_type, mime, title = resolve_item(browser, item)

    assert (item_id, item_type, mime, title) == ("s1", "stream", "", "Station")


def test_play_sends_built_uri_and_metadata(monkeypatch):
    service = SimpleNamespace(
        service_name="Example",
        service_id="204",
        service_type="52231",
        auth_type="AppLink",
        capabilities="0",
        version="1.0",
        container_type="MusicService",
        uri="http://example.invalid/smapi",
        secure_uri="https://example.invalid/smapi",
        presentation_map_uri=None,
        manifest_uri=None,
        desc="SA_RINCON52231_",
    )
    monkeypatch.setattr(
        "soco.music_services.browser.MusicService", lambda *_a, **_k: service
    )
    from soco.music_services.browser import credentials

    monkeypatch.setattr(
        credentials.ConfiguredMusicServiceAccount,
        "get_accounts",
        lambda *_a, **_k: [],
    )

    captured = {}

    class FakeDevice:
        household_id = "Sonos_household"
        uid = "RINCON_000000000001400"

        class _Sys:
            def GetString(self, args):  # pylint: disable=invalid-name
                return {"StringValue": "player-device-id"}

        systemProperties = _Sys()

        def play_uri(self, uri, meta="", **kwargs):
            captured["uri"] = uri
            captured["meta"] = meta
            captured["kwargs"] = kwargs
            return "played"

    account = SimpleNamespace(
        service_id=204,
        serial_number=35,
        udn="SA_RINCON52231_X_#Svc52231-bf7d4d9f-Token",
        token="t",
        key="k",
        nickname="",
        tier="",
        account_uid=0xBF7D4D9F,
    )
    browser = MusicServiceBrowser(
        "Example",
        account=account,
        device=FakeDevice(),
        session=SimpleNamespace(),
    )
    item = MusicServiceBrowseItem(
        item_id="song:1844932150",
        title="Choosin' Texas",
        kind="mediaMetadata",
        item_type="track",
        raw={"mimeType": "audio/aac"},
    )

    result = browser.play(item)

    assert result == "played"
    assert captured["uri"] == (
        "x-sonos-http:song%3a1844932150.mp4?sid=204&flags=8224&sn=35"
    )
    assert ">SA_RINCON52231_X_#Svc52231-bf7d4d9f-Token</desc>" in captured["meta"]
    assert "object.item.audioItem.musicTrack" in captured["meta"]


def test_play_raises_for_not_added_anonymous_service(monkeypatch):
    service = SimpleNamespace(
        service_name="Example",
        service_id="254",
        service_type="65031",
        auth_type="Anonymous",
        capabilities="0",
        version="1.0",
        container_type="MusicService",
        uri="http://example.invalid/smapi",
        secure_uri="https://example.invalid/smapi",
        presentation_map_uri=None,
        manifest_uri=None,
        desc="SA_RINCON65031_",
    )
    monkeypatch.setattr(
        "soco.music_services.browser.MusicService", lambda *_a, **_k: service
    )

    class FakeDevice:
        household_id = "Sonos_household"
        uid = "RINCON_000000000001400"

        class _Sys:
            def GetString(self, args):  # pylint: disable=invalid-name
                return {"StringValue": "player-device-id"}

        systemProperties = _Sys()

    # An anonymous service that is not added to the household is constructed
    # with a synthetic serial-0, empty-UDN account.
    browser = MusicServiceBrowser(
        "Example",
        account=SimpleNamespace(service_id=254, serial_number=0, udn=""),
        device=FakeDevice(),
        session=SimpleNamespace(),
    )
    item = MusicServiceBrowseItem(
        item_id="s29271",
        title="Station",
        kind="mediaMetadata",
        item_type="stream",
        raw={},
    )

    with pytest.raises(MusicServiceException, match="not added to this household"):
        browser.play(item)


def _make_dc_browser(monkeypatch, service_id="12", serial=63, service_name=None):
    service = SimpleNamespace(
        service_name=service_name or "Spotify",
        service_id=service_id,
        service_type="52231",
        auth_type="AppLink",
        capabilities="0",
        version="1.0",
        container_type="MusicService",
        uri="http://example.invalid/smapi",
        secure_uri="https://example.invalid/smapi",
        presentation_map_uri=None,
        manifest_uri=None,
        desc="SA_RINCON52231_",
    )
    monkeypatch.setattr(
        "soco.music_services.browser.MusicService", lambda *_a, **_k: service
    )
    from soco.music_services.browser import credentials

    monkeypatch.setattr(
        credentials.ConfiguredMusicServiceAccount,
        "get_accounts",
        lambda *_a, **_k: [],
    )
    account = SimpleNamespace(
        service_id=int(service_id),
        serial_number=serial,
        udn="SA_RINCON52231_X_#Svc52231-bf7d4d9f-Token",
        token="t",
        key="k",
        nickname="",
        tier="",
        account_uid=0xBF7D4D9F,
    )
    browser = MusicServiceBrowser(
        service_name or "Spotify",
        account=account,
        device=FakeDevice(),
        session=SimpleNamespace(),
    )
    return browser, account


def _fake_player(monkeypatch, calls, source=None, active_app=None, suspended=False):
    """A fake player for DirectControl routing tests.

    Args:
        source: The value of ``music_source`` (None -> unknown).
        active_app: The DirectControl application id the control API reports
            (None -> no DirectControl session).
        suspended: Whether the reported session is suspended.
    """

    class FakeGroup:
        uid = "RINCON_000000000001400:4215913542"
        coordinator = None

    class FakePlayer:
        ip_address = "192.168.1.51"
        music_source = source

        def play_direct_control(self, provider, title=None):
            calls.append(("enter", provider, title))

        def end_direct_control_session(self):
            calls.append(("end",))

        @property
        def group(self):
            return FakeGroup()

    from soco.music_services import browser as browser_module
    from soco.music_services.browser.direct_control import DirectControlSession

    monkeypatch.setattr(
        browser_module,
        "load_container",
        lambda **kwargs: calls.append(("load", kwargs)) or True,
    )
    monkeypatch.setattr(
        browser_module,
        "direct_control_session",
        lambda *_args, **_kwargs: (
            DirectControlSession(
                client_id=active_app, account_id="", suspended=suspended
            )
            if active_app
            else None
        ),
    )
    monkeypatch.setattr(
        browser_module,
        "wait_for_direct_control",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        browser_module,
        "direct_control_observable",
        lambda service_id: int(service_id) in (9, 12),
    )
    return FakePlayer()


def test_play_direct_control_container_routes_through_control_api(monkeypatch):
    browser, account = _make_dc_browser(monkeypatch)
    calls = []
    item = MusicServiceBrowseItem(
        item_id="spotify:playlist:37i9dQZF1E4sD262twcXeU",
        title="Zach Bryan Radio",
        kind="mediaCollection",
        item_type="playlist",
        album_art_uri="https://i.scdn.co/image/abc",
        summary="A radio station",
        raw={},
    )

    result = browser.play(item, device=_fake_player(monkeypatch, calls))

    assert result is True
    # No DirectControl session is active: enter the session, then load
    # the container.  (Nothing to end when no session is running.)
    assert calls[0] == ("enter", "spotify", "Spotify")
    action, load = calls[1]
    assert action == "load"
    assert load["object_id"] == "spotify:playlist:37i9dQZF1E4sD262twcXeU"
    assert int(load["service_id"]) == 12
    assert load["account_serial"] == account.serial_number
    assert load["name"] == "Zach Bryan Radio"
    assert load["container_type"] == "playlist.spotify.connect"
    assert load["group_uid"] == "RINCON_000000000001400:4215913542"
    # Provider metadata is propagated into the loadContainer request.
    assert load["image_url"] == "https://i.scdn.co/image/abc"
    assert load["description"] == "A radio station"


def test_play_direct_control_skips_reentry_when_same_app_active(monkeypatch):
    # When the *same* DirectControl application is already active (the
    # control API reports spotify.connect.adapter), switching its context
    # must not re-enter the session, which would leave it paused.
    browser, _account = _make_dc_browser(monkeypatch)
    calls = []
    item = MusicServiceBrowseItem(
        item_id="spotify:playlist:37i9dQZF1E4sD262twcXeU",
        title="Zach Bryan Radio",
        kind="mediaCollection",
        item_type="playlist",
        raw={},
    )

    result = browser.play(
        item,
        device=_fake_player(
            monkeypatch,
            calls,
            active_app="spotify.connect.adapter",
        ),
    )

    assert result is True
    assert [name for name, *_ in calls] == ["load"]


def test_play_non_dc_container_still_raises(monkeypatch):
    # Apple Music (204) is not a DirectControl service: its containers have
    # no player-resolvable URI and must be browsed into, as before.
    browser, _account = _make_dc_browser(monkeypatch, service_id="204", serial=35)
    # resolve_item consults getMediaMetadata for unknown item types; make it
    # unavailable so the fallback path (which raises) is exercised.
    monkeypatch.setattr(
        browser,
        "get_media_metadata",
        lambda _id: (_ for _ in ()).throw(MusicServiceException("provider down")),
    )
    item = MusicServiceBrowseItem(
        item_id="library:albums",
        title="Albums",
        kind="mediaCollection",
        item_type="container",
        raw={},
    )

    with pytest.raises(MusicServiceException, match="cannot be played"):
        browser.play(item)


def test_play_direct_control_requires_group(monkeypatch):
    browser, _account = _make_dc_browser(monkeypatch)

    class NoGroupPlayer:
        def play_direct_control(self, provider, title=None):
            raise AssertionError("should not enter the session without a group")

        @property
        def group(self):
            return None

    item = MusicServiceBrowseItem(
        item_id="spotify:playlist:1",
        title="Radio",
        kind="mediaCollection",
        item_type="playlist",
        raw={},
    )

    with pytest.raises(MusicServiceException, match="not part of a group"):
        browser.play(item, device=NoGroupPlayer())


def test_play_pandora_station_routes_through_control_api(monkeypatch):
    # Pandora stations surface as ``program`` items, not containers.
    browser, account = _make_dc_browser(
        monkeypatch, service_id="236", serial=1, service_name="Pandora"
    )
    calls = []
    item = MusicServiceBrowseItem(
        item_id="ST:110709827948261366",
        title="Test for Echo (Remastered) Radio",
        kind="mediaMetadata",
        item_type="program",
        raw={},
    )

    result = browser.play(item, device=_fake_player(monkeypatch, calls))

    assert result is True
    assert calls[0] == ("enter", "pandora", "Pandora")
    action, load = calls[1]
    assert action == "load"
    assert load["object_id"] == "ST:110709827948261366"
    assert int(load["service_id"]) == 236
    assert load["account_serial"] == 1
    assert load["container_type"] == "program.pandora.connect"


def test_play_spotify_track_routes_through_control_api(monkeypatch):
    # Spotify tracks are playable through DirectControl: the desktop's
    # ``playlist.spotify.connect`` container type accepts a bare track
    # object id, so tracks must route through the control API, not the
    # (rejected) ``x-sonos-spotify:`` URI path.
    browser, account = _make_dc_browser(monkeypatch)
    calls = []
    item = MusicServiceBrowseItem(
        item_id="spotify:track:7azylXFRsebfrIoAtwfjaB",
        title="So What",
        kind="mediaMetadata",
        item_type="track",
        raw={"mimeType": "audio/x-spotify"},
    )

    result = browser.play(item, device=_fake_player(monkeypatch, calls))

    assert result is True
    assert calls[0] == ("enter", "spotify", "Spotify")
    action, load = calls[1]
    assert action == "load"
    assert load["object_id"] == "spotify:track:7azylXFRsebfrIoAtwfjaB"
    assert int(load["service_id"]) == 12
    assert load["account_serial"] == account.serial_number
    assert load["name"] == "So What"
    assert load["container_type"] == "playlist.spotify.connect"


def test_play_direct_control_raises_when_wait_times_out(monkeypatch):
    # Spotify's session is observable through the control API, so entering
    # the session must wait for it to become active; if it never does, the
    # play must fail instead of blindly posting a loadContainer that would
    # race (or hit) a session that was never established.
    browser, _account = _make_dc_browser(monkeypatch)
    calls = []
    player = _fake_player(monkeypatch, calls)
    from soco.music_services import browser as browser_module

    # The fake player installs a wait that succeeds; override it to time out.
    monkeypatch.setattr(
        browser_module,
        "wait_for_direct_control",
        lambda *_args, **_kwargs: False,
    )
    item = MusicServiceBrowseItem(
        item_id="spotify:playlist:37i9dQZF1E4sD262twcXeU",
        title="Zach Bryan Radio",
        kind="mediaCollection",
        item_type="playlist",
        raw={},
    )

    with pytest.raises(MusicServiceException, match="did not become active"):
        browser.play(item, device=player)
    # The session was entered, but loadContainer must never run.
    assert [name for name, *_ in calls] == ["enter"]


def test_play_direct_control_resumes_suspended_same_app(monkeypatch):
    # The same application is active but *suspended*: the session must be
    # re-entered (resumed), not skipped — posting a container into a
    # suspended session may never start playback.
    browser, _account = _make_dc_browser(monkeypatch)
    calls = []
    item = MusicServiceBrowseItem(
        item_id="spotify:playlist:37i9dQZF1E4sD262twcXeU",
        title="Zach Bryan Radio",
        kind="mediaCollection",
        item_type="playlist",
        raw={},
    )

    result = browser.play(
        item,
        device=_fake_player(
            monkeypatch,
            calls,
            active_app="spotify.connect.adapter",
            suspended=True,
        ),
    )

    assert result is True
    # Same app but suspended: re-enter without ending the session, then load.
    assert calls[0] == ("enter", "spotify", "Spotify")
    assert [name for name, *_ in calls[1:]] == ["load"]


def test_play_direct_control_does_not_wait_for_unobservable_app(monkeypatch):
    # Pandora/Audible never report a DirectControl session through the
    # control API, so after entering their session the wait must be skipped
    # (it would otherwise always time out and stall playback).
    browser, _account = _make_dc_browser(
        monkeypatch, service_id="236", serial=1, service_name="Pandora"
    )
    calls = []
    waited = []
    from soco.music_services import browser as browser_module

    monkeypatch.setattr(
        browser_module,
        "wait_for_direct_control",
        lambda *_args, **_kwargs: waited.append(True) or True,
    )
    item = MusicServiceBrowseItem(
        item_id="ST:110709827948261366",
        title="Test for Echo Radio",
        kind="mediaMetadata",
        item_type="program",
        raw={},
    )

    result = browser.play(item, device=_fake_player(monkeypatch, calls))

    assert result is True
    assert waited == []


def test_play_direct_control_reenters_for_different_app(monkeypatch):
    # An active Audible session (com.audible.mobile.sonos) is also the
    # broad DIRECT_CONTROL music-source class, but posting a Spotify
    # container into it would silently fail.  The requested service's app
    # must differ from the active one for re-entry to happen.
    browser, _account = _make_dc_browser(monkeypatch)
    calls = []
    item = MusicServiceBrowseItem(
        item_id="spotify:playlist:37i9dQZF1E4sD262twcXeU",
        title="Zach Bryan Radio",
        kind="mediaCollection",
        item_type="playlist",
        raw={},
    )

    result = browser.play(
        item,
        device=_fake_player(
            monkeypatch,
            calls,
            source="DIRECT_CONTROL",
            active_app="com.audible.mobile.sonos",
        ),
    )

    assert result is True
    assert calls[0] == ("end",)
    assert calls[1] == ("enter", "spotify", "Spotify")
    assert [name for name, *_ in calls[2:]] == ["load"]


def test_play_unobservable_dc_source_is_ended_before_entering_spotify(monkeypatch):
    # Pandora/Audible never report playbackSession.clientId, so an active
    # session surfaces as ``session is None``.  The player is still on a
    # DirectControl source though, so the old session must be ended before
    # entering Spotify, or the Spotify container would be posted into the
    # running (unobservable) session.
    browser, _account = _make_dc_browser(monkeypatch)
    calls = []
    item = MusicServiceBrowseItem(
        item_id="spotify:playlist:37i9dQZF1E4sD262twcXeU",
        title="Zach Bryan Radio",
        kind="mediaCollection",
        item_type="playlist",
        raw={},
    )

    result = browser.play(
        item,
        device=_fake_player(
            monkeypatch,
            calls,
            source="DIRECT_CONTROL",
            active_app=None,  # unobservable session
        ),
    )

    assert result is True
    assert calls[0] == ("end",)
    assert calls[1] == ("enter", "spotify", "Spotify")
    assert [name for name, *_ in calls[2:]] == ["load"]


def test_play_unobservable_spotify_connect_source_is_ended(monkeypatch):
    # Same as above but the player reports the Spotify Connect source
    # (SPOTIFY_CONNECT) with no observable session: still conservatively
    # end before entering.
    browser, _account = _make_dc_browser(monkeypatch)
    calls = []
    item = MusicServiceBrowseItem(
        item_id="spotify:playlist:37i9dQZF1E4sD262twcXeU",
        title="Zach Bryan Radio",
        kind="mediaCollection",
        item_type="playlist",
        raw={},
    )

    result = browser.play(
        item,
        device=_fake_player(
            monkeypatch,
            calls,
            source="SPOTIFY_CONNECT",
            active_app=None,
        ),
    )

    assert result is True
    assert calls[0] == ("end",)
    assert calls[1] == ("enter", "spotify", "Spotify")
    assert [name for name, *_ in calls[2:]] == ["load"]


def test_play_unobservable_dc_source_ended_before_entering_pandora(monkeypatch):
    # Audible (unobservable) → Pandora: the requested service is itself
    # unobservable, but the existing DirectControl session must still be
    # ended before entering Pandora's session.
    browser, _account = _make_dc_browser(
        monkeypatch, service_id="236", serial=1, service_name="Pandora"
    )
    calls = []
    item = MusicServiceBrowseItem(
        item_id="ST:110709827948261366",
        title="Test for Echo Radio",
        kind="mediaMetadata",
        item_type="program",
        raw={},
    )

    result = browser.play(
        item,
        device=_fake_player(
            monkeypatch,
            calls,
            source="DIRECT_CONTROL",
            active_app=None,
        ),
    )

    assert result is True
    assert calls[0] == ("end",)
    assert calls[1] == ("enter", "pandora", "Pandora")
    assert [name for name, *_ in calls[2:]] == ["load"]


def test_play_pandora_folder_falls_through_to_uri_playback(monkeypatch):
    # Pandora's browse folders (My Stations) are containers, not playable
    # units; they must not route through DirectControl.
    browser, _account = _make_dc_browser(
        monkeypatch, service_id="236", serial=1, service_name="Pandora"
    )
    calls = []
    item = MusicServiceBrowseItem(
        item_id="myStations",
        title="My Stations",
        kind="mediaCollection",
        item_type="container",
        raw={},
    )
    monkeypatch.setattr(
        browser,
        "get_media_metadata",
        lambda _id: (_ for _ in ()).throw(MusicServiceException("provider down")),
    )

    with pytest.raises(MusicServiceException, match="cannot be played"):
        browser.play(item, device=_fake_player(monkeypatch, calls))
    assert calls == []


def test_play_audible_book_routes_through_control_api(monkeypatch):
    # Audible books surface as ``audiobook`` items, not containers.
    browser, _account = _make_dc_browser(
        monkeypatch, service_id="239", serial=21, service_name="Audible"
    )
    calls = []
    item = MusicServiceBrowseItem(
        item_id="reftitle:B0FCZNK8HF_com",
        title="Dopamine Kids",
        kind="mediaMetadata",
        item_type="audiobook",
        raw={},
    )

    result = browser.play(item, device=_fake_player(monkeypatch, calls))

    assert result is True
    assert calls[0] == ("enter", "audible", "Audible")
    action, load = calls[1]
    assert action == "load"
    assert load["object_id"] == "reftitle:B0FCZNK8HF_com"
    assert int(load["service_id"]) == 239
    assert load["container_type"] == "audiobook.audible.connect"
