"""Tests for fetching available music services with logo catalog URLs."""

from unittest import mock

import pytest

from soco.music_services import music_service
from soco.music_services.music_service import (
    AvailableMusicService,
    LOGO_CATALOG_URL,
    MusicService,
    _parse_logo_catalog,
)

SERVICE_UUID = "12345678-abcd-1234-abcd-1234567890ab"

# A typical service descriptor list, including a <Manifest> element for
# Spotify so that the logo-catalog bridge can be exercised.
SERVICES_DESCRIPTOR_LIST = """<?xml version="1.0"?>
<Services SchemaVersion="1">
    <Service Id="9" Name="Spotify" Version="1.1"
        Uri="https://spotify.ws.sonos.com/smapi"
        SecureUri="https://spotify.ws.sonos.com/smapi"
        ContainerType="MService" Capabilities="2563" MaxMessagingChars="0">
        <Policy Auth="DeviceLink" PollInterval="30" />
        <Manifest Uri="https://cf.ws.sonos.com/p/m/%s" />
    </Service>
    <Service Id="254" Name="TuneIn" Version="1.1"
        Uri="http://legato.radiotime.com/Radio.asmx"
        SecureUri="http://legato.radiotime.com/Radio.asmx"
        ContainerType="MService" Capabilities="0" MaxMessagingChars="0">
        <Policy Auth="Anonymous" PollInterval="0"/>
    </Service>
</Services>
""" % SERVICE_UUID


def _image(placement, uuid_, filename):
    """Build one catalog <image> element with the URL shape of mslogo.xml."""
    return (
        f'<image placement="{placement}">'
        f"https://x.test/spotify/{uuid_}/{filename}</image>"
    )


LOGO_CATALOG_PAYLOAD = (
    '<?xml version="1.0" encoding="UTF-8"?><images><sized>'
    '<service id="Spotify">'
    + _image("square:small", SERVICE_UUID, "icon_48.png")
    + _image("square:medium", SERVICE_UUID, "icon_80.png")
    + _image("BrandLogo-v2", SERVICE_UUID, "logo.svg")
    + '</service></sized><presentationmap><service id="Spotify">'
    + _image("AttributionFullLogo", SERVICE_UUID, "attr.png")
    + "</service></presentationmap></images>"
).encode("utf-8")


@pytest.fixture(autouse=True)
def isolated_music_services(monkeypatch):
    """Use a fixed descriptor list and clear the caches for every test."""
    monkeypatch.setattr(
        MusicService,
        "_get_music_services_data_xml",
        mock.Mock(return_value=SERVICES_DESCRIPTOR_LIST),
    )
    MusicService._music_services_data = None
    music_service._logo_catalog_cache.clear()
    yield
    MusicService._music_services_data = None
    music_service._logo_catalog_cache.clear()


def test_parse_logo_catalog_merges_sections_and_keys_by_uuid():
    catalog = _parse_logo_catalog(LOGO_CATALOG_PAYLOAD)
    assert set(catalog) == {SERVICE_UUID}
    logos = catalog[SERVICE_UUID]
    assert logos["square:medium"].endswith("icon_80.png")
    # The presentationmap section is merged into the same entry.
    assert logos["AttributionFullLogo"].endswith("attr.png")


def test_get_all_music_services_without_logos(requests_mock):
    services = MusicService.get_all_music_services()
    assert len(services) == 2
    spotify = services[0]
    assert isinstance(spotify, AvailableMusicService)
    assert spotify.service_id == 9
    assert spotify.name == "Spotify"
    assert spotify.container_type == "MService"
    assert spotify.capabilities == 2563
    assert spotify.auth_type == "DeviceLink"
    assert spotify.service_type == "2311"
    assert spotify.manifest_uri.endswith(SERVICE_UUID)
    assert spotify.logos == {}
    # No catalog request should be made when logos are not requested.
    assert requests_mock.last_request is None


def test_get_all_music_services_with_logos(requests_mock):
    requests_mock.get(LOGO_CATALOG_URL, text=LOGO_CATALOG_PAYLOAD.decode("utf-8"))
    services = MusicService.get_all_music_services(include_logos=True)
    spotify = services[0]
    assert spotify.logos["square:medium"].endswith("icon_80.png")
    assert spotify.get_logo_url() == spotify.logos["square:medium"]
    # TuneIn has no manifest URI, so it gets no logos.
    assert services[1].logos == {}
    assert services[1].get_logo_url() == ""


def test_logo_catalog_is_cached(requests_mock):
    requests_mock.get(LOGO_CATALOG_URL, text=LOGO_CATALOG_PAYLOAD.decode("utf-8"))
    MusicService.get_all_music_services(include_logos=True)
    MusicService.get_all_music_services(include_logos=True)
    assert requests_mock.call_count == 1


def test_get_logo_url_falls_back_through_ladder():
    service = AvailableMusicService(
        service_id=9,
        name="Spotify",
        container_type="MService",
        capabilities=2563,
        auth_type="DeviceLink",
        service_type="2311",
        manifest_uri="",
        logos={"square:large": "large.png"},
    )
    # An exact match wins.
    assert service.get_logo_url("square:large") == "large.png"
    # A missing placement falls back to the closest available one.
    assert service.get_logo_url("square:medium") == "large.png"


def test_get_logo_url_empty_without_logos():
    service = AvailableMusicService(
        9, "Spotify", "MService", 2563, "DeviceLink", "2311", ""
    )
    assert service.get_logo_url() == ""
