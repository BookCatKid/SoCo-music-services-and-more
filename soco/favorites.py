"""Access to the Sonos favorites list.

The favorites list is stored by the speakers in the ``FV:2`` container of
the ContentDirectory service. It has a fixed capacity: once the list is
full, adding another favorite fails with UPnP error 805 and
`FavoritesFullError` is raised.
"""

import re

from xml.sax.saxutils import escape

from . import discovery
from .data_structures import DidlObject, DidlFavorite, to_didl_string
from .exceptions import (
    FavoritesAlreadyAddedError,
    FavoritesFullError,
    SoCoUPnPException,
)


class Favorites:
    """The Sonos favorites list.

    Provides read access to the favorites (e.g. :meth:`get_sonos_favorites`)
    and write access (:meth:`add_to_favorites`,
    :meth:`remove_from_favorites`, :meth:`update_favorite`).
    """

    # pylint: disable=invalid-name, protected-access
    def __init__(self, soco=None):
        """
        Args:
            soco (`SoCo`, optional): A `SoCo` instance to query for
                favorites information. If `None`, or not supplied, a
                random `SoCo` instance will be used.
        """
        self.soco = soco if soco is not None else discovery.any_soco()
        self.contentDirectory = self.soco.contentDirectory

    def get_sonos_favorites(self, *args, **kwargs):
        """Get the Sonos favorites list.

        For details of the arguments, see `MusicLibrary.get_music_library_information
        <#soco.music_library.MusicLibrary.get_music_library_information>`_.
        """
        return self.soco.music_library.get_music_library_information(
            "sonos_favorites", *args, **kwargs
        )

    def get_favorite_radio_stations(self, *args, **kwargs):
        """Get the favorite radio stations from Sonos' Radio app.

        For details of the arguments, see `MusicLibrary.get_music_library_information
        <#soco.music_library.MusicLibrary.get_music_library_information>`_.
        """
        return self.soco.music_library.get_music_library_information(
            "radio_stations", *args, **kwargs
        )

    def get_favorite_radio_shows(self, *args, **kwargs):
        """Get the favorite radio shows from Sonos' Radio app.

        For details of the arguments, see `MusicLibrary.get_music_library_information
        <#soco.music_library.MusicLibrary.get_music_library_information>`_.
        """
        return self.soco.music_library.get_music_library_information(
            "radio_shows", *args, **kwargs
        )

    @staticmethod
    def _favorite_object_id(favorite):
        """Return the item id of a favorite from a `DidlFavorite` or id."""
        if isinstance(favorite, DidlFavorite):
            return favorite.item_id
        return str(favorite)

    def add_to_favorites(self, item, title=None, description=None):
        """Add an item to the Sonos favorites list.

        Args:
            item (DidlObject): The item to add, e.g. a track, album or
                radio station from the music library or a music service.
            title (str, optional): The title to show for the favorite.
                Defaults to the item's own title.
            description (str, optional): An optional description shown
                alongside the favorite, e.g. the artist name.

        Returns:
            DidlFavorite: The newly created favorite.

        Raises:
            FavoritesFullError: if the favorites list is at capacity.
            FavoritesAlreadyAddedError: if the item is already a favorite.
            SoCoUPnPException: if the item cannot be added for another
                reason.

        Example:
            Add the first track of an album to favorites::

                album = next(device.music_library.get_albums())
                track = next(album.get_tracks())
                device.favorites.add_to_favorites(track)
        """
        if not isinstance(item, DidlObject):
            raise TypeError("item must be a DidlObject, got %r" % type(item))
        uri = item.get_uri()
        protocol_info = (
            item.resources[0].protocol_info if item.resources else None
        )
        inner = to_didl_string(item)
        new_title = escape(
            title if title is not None else item.title, {'"': "&quot;"}
        )
        elements = (
            '<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
            'xmlns:r="urn:schemas-rinconnetworks-com:metadata-1-0/" '
            'xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/">'
            '<item id="" parentID="FV:2" restricted="false">'
            f"<dc:title>{new_title}</dc:title>"
            "<upnp:class>object.itemobject.item.sonos-favorite</upnp:class>"
            "<r:ordinal>0</r:ordinal>"
            '<res protocolInfo="'
            + escape(protocol_info, {'"': "&quot;"})
            + '">'
            + escape(uri)
            + "</res>"
            "<r:type>instantPlay</r:type>"
        )
        if description:
            elements += (
                f"<r:description>{escape(description, {'\"': '&quot;'})}"
                "</r:description>"
            )
        elements += f"<r:resMD>{escape(inner)}</r:resMD></item></DIDL-Lite>"

        try:
            result = self.contentDirectory.CreateObject(
                [("ContainerID", "FV:2"), ("Elements", elements)]
            )
        except SoCoUPnPException as exc:
            if exc.error_code == "805":
                raise FavoritesFullError(
                    "The Sonos favorites list is full "
                    f"(UPnP error {exc.error_code})"
                ) from exc
            if exc.error_code == "803":
                raise FavoritesAlreadyAddedError(
                    "This item is already in the Sonos favorites list "
                    f"(UPnP error {exc.error_code})"
                ) from exc
            raise

        favorite = DidlFavorite(
            title if title is not None else item.title,
            "FV:2",
            result["ObjectID"],
            restricted=False,
        )
        favorite.reference = item
        return favorite

    def remove_from_favorites(self, favorite):
        """Remove a favorite from the Sonos favorites list.

        Args:
            favorite: The favorite to remove. Either a `DidlFavorite`
                object (as returned by :meth:`get_sonos_favorites`) or
                its item id as a string, e.g. ``"FV:2/28"``.

        Raises:
            SoCoUPnPException: if the favorite does not exist (error 701).

        Example:
            Remove the first favorite::

                favorite = device.favorites.get_sonos_favorites()[0]
                device.favorites.remove_from_favorites(favorite)
        """
        object_id = self._favorite_object_id(favorite)
        self.contentDirectory.DestroyObject([("ObjectID", object_id)])

    @staticmethod
    def _update_element(didl, tag, value):
        """Return ``didl`` with the text of the first ``tag`` element replaced.

        If the element is not present, it is inserted before the closing
        ``</item>`` tag. ``value`` is XML-escaped before insertion.
        """
        escaped = escape(str(value), {'"': "&quot;"})
        pattern = r"(<%s>).*?(</%s>)" % (tag, tag)
        if re.search(pattern, didl, flags=re.DOTALL):
            return re.sub(
                pattern,
                lambda m, e=escaped: m.group(1) + e + m.group(2),
                didl,
                flags=re.DOTALL,
            )
        return didl.replace(
            "</item>",
            f"<{tag}>{escaped}</{tag}></item>",
            1,
        )

    def update_favorite(self, favorite, title=None, description=None):
        """Update the metadata of a favorite.

        Currently supports updating the ``title`` and ``description``
        fields. Only the fields supplied are changed; everything else
        (URI, artwork, metadata) is preserved.

        Args:
            favorite: The favorite to update. Either a `DidlFavorite`
                object or its item id as a string, e.g. ``"FV:2/28"``.
            title (str, optional): The new title for the favorite.
            description (str, optional): The new description shown
                alongside the favorite.

        Raises:
            SoCoUPnPException: if the favorite does not exist (error 701)
                or the metadata cannot be updated.

        Example:
            Rename the first favorite::

                favorite = device.favorites.get_sonos_favorites()[0]
                device.favorites.update_favorite(
                    favorite, title="My Fave", description="By Artist"
                )
        """
        object_id = self._favorite_object_id(favorite)
        result = self.contentDirectory.Browse(
            [
                ("ObjectID", object_id),
                ("BrowseFlag", "BrowseMetadata"),
                ("Filter", "*"),
                ("StartingIndex", 0),
                ("RequestedCount", 0),
                ("SortCriteria", ""),
            ]
        )["Result"]
        new_result = result
        if title is not None:
            new_result = self._update_element(new_result, "dc:title", title)
        if description is not None:
            new_result = self._update_element(
                new_result, "r:description", description
            )
        if new_result == result:
            return
        self.contentDirectory.UpdateObject(
            [
                ("ObjectID", object_id),
                ("CurrentTagValue", result),
                ("NewTagValue", new_result),
            ]
        )

    def rename_favorite(self, favorite, new_title):
        """Rename a favorite in the Sonos favorites list.

        Convenience wrapper around :meth:`update_favorite`.

        Args:
            favorite: The favorite to rename. Either a `DidlFavorite`
                object or its item id as a string, e.g. ``"FV:2/28"``.
            new_title (str): The new title for the favorite.

        Raises:
            SoCoUPnPException: if the favorite does not exist (error 701)
                or the title cannot be updated.
        """
        self.update_favorite(favorite, title=new_title)
