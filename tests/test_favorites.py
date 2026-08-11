"""Tests for the Favorites class."""

from unittest import mock

import pytest

from soco.data_structures import DidlFavorite, DidlMusicTrack
from soco.exceptions import (
    FavoritesAlreadyAddedError,
    FavoritesFullError,
    SoCoUPnPException,
)


class TestFavorites:
    def test_add_to_favorites(self, moco):
        moco.contentDirectory.CreateObject.return_value = {"ObjectID": "FV:2/130"}
        track = DidlMusicTrack("Song", "A:TRACKS", "t1")
        track.set_uri(
            "http://example.com/song.mp3", protocol_info="http-get:*:audio/mpeg:*"
        )
        fav = moco.favorites.add_to_favorites(track)
        args = moco.contentDirectory.CreateObject.call_args[0][0]
        assert args[0] == ("ContainerID", "FV:2")
        assert "<dc:title>Song</dc:title>" in args[1][1]
        assert "object.itemobject.item.sonos-favorite" in args[1][1]
        assert "<r:type>instantPlay</r:type>" in args[1][1]
        assert "http://example.com/song.mp3" in args[1][1]
        assert fav.item_id == "FV:2/130"
        assert fav.title == "Song"

    def test_add_to_favorites_with_title_and_description(self, moco):
        moco.contentDirectory.CreateObject.return_value = {"ObjectID": "FV:2/131"}
        track = DidlMusicTrack("Song", "A:TRACKS", "t1")
        track.set_uri(
            "http://example.com/song.mp3", protocol_info="http-get:*:audio/mpeg:*"
        )
        fav = moco.favorites.add_to_favorites(
            track, title="My Fave", description="By Artist"
        )
        args = moco.contentDirectory.CreateObject.call_args[0][0]
        assert "<dc:title>My Fave</dc:title>" in args[1][1]
        assert "<r:description>By Artist</r:description>" in args[1][1]
        assert fav.title == "My Fave"

    def test_add_to_favorites_full_raises_favorites_full_error(self, moco):
        moco.contentDirectory.CreateObject.side_effect = SoCoUPnPException(
            "UPnP Error 805 received", "805", "error xml"
        )
        track = DidlMusicTrack("Song", "A:TRACKS", "t1")
        track.set_uri(
            "http://example.com/song.mp3", protocol_info="http-get:*:audio/mpeg:*"
        )
        with pytest.raises(FavoritesFullError):
            moco.favorites.add_to_favorites(track)

    def test_add_to_favorites_duplicate_raises_already_added_error(self, moco):
        moco.contentDirectory.CreateObject.side_effect = SoCoUPnPException(
            "UPnP Error 803 received", "803", "error xml"
        )
        track = DidlMusicTrack("Song", "A:TRACKS", "t1")
        track.set_uri(
            "http://example.com/song.mp3", protocol_info="http-get:*:audio/mpeg:*"
        )
        with pytest.raises(FavoritesAlreadyAddedError):
            moco.favorites.add_to_favorites(track)

    def test_add_to_favorites_rejects_non_didl(self, moco):
        with pytest.raises(TypeError):
            moco.favorites.add_to_favorites("not a didl object")

    def test_remove_from_favorites_with_didl_object(self, moco):
        fav = DidlFavorite("Breathe", "FV:2", "FV:2/28")
        moco.favorites.remove_from_favorites(fav)
        moco.contentDirectory.DestroyObject.assert_called_once_with(
            [("ObjectID", "FV:2/28")]
        )

    def test_remove_from_favorites_with_item_id(self, moco):
        moco.favorites.remove_from_favorites("FV:2/28")
        moco.contentDirectory.DestroyObject.assert_called_once_with(
            [("ObjectID", "FV:2/28")]
        )

    def test_update_favorite_title(self, moco):
        moco.contentDirectory.Browse.return_value = {
            "Result": (
                '<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" '
                'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
                'xmlns:r="urn:schemas-rinconnetworks-com:metadata-1-0/" '
                'xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/">'
                '<item id="FV:2/28" parentID="FV:2" restricted="false">'
                "<dc:title>Breathe</dc:title>"
                "<upnp:class>object.itemobject.item.sonos-favorite</upnp:class>"
                "</item></DIDL-Lite>"
            )
        }
        moco.favorites.update_favorite("FV:2/28", title="Deep Breath")
        moco.contentDirectory.Browse.assert_called_once_with(
            [
                ("ObjectID", "FV:2/28"),
                ("BrowseFlag", "BrowseMetadata"),
                ("Filter", "*"),
                ("StartingIndex", 0),
                ("RequestedCount", 0),
                ("SortCriteria", ""),
            ]
        )
        args = moco.contentDirectory.UpdateObject.call_args[0][0]
        assert args[0] == ("ObjectID", "FV:2/28")
        assert args[1][0] == "CurrentTagValue"
        assert "<dc:title>Breathe</dc:title>" in args[1][1]
        assert args[2][0] == "NewTagValue"
        assert "<dc:title>Deep Breath</dc:title>" in args[2][1]
        assert "<dc:title>Breathe</dc:title>" not in args[2][1]

    def test_update_favorite_title_and_description(self, moco):
        moco.contentDirectory.Browse.return_value = {
            "Result": (
                '<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" '
                'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
                'xmlns:r="urn:schemas-rinconnetworks-com:metadata-1-0/" '
                'xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/">'
                '<item id="FV:2/28" parentID="FV:2" restricted="false">'
                "<dc:title>Breathe</dc:title>"
                "<r:description>By Fleurie</r:description>"
                "</item></DIDL-Lite>"
            )
        }
        moco.favorites.update_favorite(
            "FV:2/28", title="Deep Breath", description="By Someone"
        )
        args = moco.contentDirectory.UpdateObject.call_args[0][0]
        assert "<dc:title>Deep Breath</dc:title>" in args[2][1]
        assert "<r:description>By Someone</r:description>" in args[2][1]
        assert "<r:description>By Fleurie</r:description>" not in args[2][1]

    def test_update_favorite_escapes_title(self, moco):
        moco.contentDirectory.Browse.return_value = {
            "Result": (
                '<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" '
                'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
                'xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/">'
                '<item id="FV:2/28" parentID="FV:2" restricted="false">'
                "<dc:title>Breathe</dc:title>"
                "</item></DIDL-Lite>"
            )
        }
        moco.favorites.update_favorite("FV:2/28", title="Rock & Roll <Live>")
        args = moco.contentDirectory.UpdateObject.call_args[0][0]
        assert (
            "<dc:title>Rock &amp; Roll &lt;Live&gt;</dc:title>" in args[2][1]
        )

    def test_update_favorite_with_backslash_in_title(self, moco):
        moco.contentDirectory.Browse.return_value = {
            "Result": (
                '<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" '
                'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
                'xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/">'
                '<item id="FV:2/28" parentID="FV:2" restricted="false">'
                "<dc:title>Breathe</dc:title>"
                "</item></DIDL-Lite>"
            )
        }
        moco.favorites.update_favorite("FV:2/28", title=r"Rock \\ Band")
        args = moco.contentDirectory.UpdateObject.call_args[0][0]
        assert "<dc:title>Rock \\\\ Band</dc:title>" in args[2][1]

    def test_update_favorite_preserves_rest_of_didl(self, moco):
        moco.contentDirectory.Browse.return_value = {
            "Result": (
                '<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" '
                'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
                'xmlns:r="urn:schemas-rinconnetworks-com:metadata-1-0/" '
                'xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/">'
                '<item id="FV:2/28" parentID="FV:2" restricted="false">'
                "<dc:title>Breathe</dc:title>"
                "<upnp:class>object.itemobject.item.sonos-favorite</upnp:class>"
                "<r:resMD>inner-metadata-here</r:resMD>"
                "</item></DIDL-Lite>"
            )
        }
        moco.favorites.update_favorite("FV:2/28", title="Deep Breath")
        args = moco.contentDirectory.UpdateObject.call_args[0][0]
        new_value = args[2][1]
        sonos_fav_class = "object.itemobject.item.sonos-favorite"
        assert f"<upnp:class>{sonos_fav_class}</upnp:class>" in new_value
        assert "<r:resMD>inner-metadata-here</r:resMD>" in new_value

    def test_update_favorite_no_change_is_noop(self, moco):
        moco.contentDirectory.Browse.return_value = {
            "Result": (
                '<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" '
                'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
                'xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/">'
                '<item id="FV:2/28" parentID="FV:2" restricted="false">'
                "<dc:title>Breathe</dc:title>"
                "</item></DIDL-Lite>"
            )
        }
        moco.favorites.update_favorite("FV:2/28")
        moco.contentDirectory.UpdateObject.assert_not_called()

    def test_rename_favorite_delegates_to_update(self, moco):
        moco.contentDirectory.Browse.return_value = {
            "Result": (
                '<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" '
                'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
                'xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/">'
                '<item id="FV:2/28" parentID="FV:2" restricted="false">'
                "<dc:title>Breathe</dc:title>"
                "</item></DIDL-Lite>"
            )
        }
        moco.favorites.rename_favorite("FV:2/28", "Deep Breath")
        args = moco.contentDirectory.UpdateObject.call_args[0][0]
        assert "<dc:title>Deep Breath</dc:title>" in args[2][1]

    def test_get_sonos_favorites_delegates_to_music_library(self, moco):
        with mock.patch.object(
            moco.music_library, "get_music_library_information", return_value="favs"
        ) as mocked:
            result = moco.favorites.get_sonos_favorites()
        assert result == "favs"
        mocked.assert_called_once_with("sonos_favorites")
