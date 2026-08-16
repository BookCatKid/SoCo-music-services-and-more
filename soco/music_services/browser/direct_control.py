"""DirectControl (virtual line-in) playback for music services.

Modern app-link services such as Spotify are played by the controller
through DirectControl: the speaker enters a "virtual line-in" session
(top-level transport URI ``x-sonos-vli:...``) and the service's own cloud
session drives the playback. The controller then picks *what* plays by
posting the desired container to the speaker's local control API
(``loadContainer``).

This module implements both halves:

* :func:`direct_control_uri` / :func:`direct_control_metadata` build the
  ``x-sonos-vli:`` transport URI and its DIDL, which the speaker uses to
  (re)enter the account's live DirectControl session (see
  :meth:`SoCo.play_direct_control`).
* :func:`load_container` posts a container (radio, playlist, ...) to the
  speaker's control API so the session starts playing that context.
* :func:`direct_control_session` / :func:`wait_for_direct_control` inspect
  the resulting session state so callers can tell which application is
  active and whether the session is suspended.

The control API lives on the speaker's HTTPS port 1443 (the desktop
controller's value; see :data:`_CONTROL_API_PORT`) and requires a
per-request ``X-Sonos-Api-Key`` header. The key is a fixed constant embedded
(and XOR-obfuscated) in the official desktop controller's
``sclib-csharp.dll``; it is the same for every household and device.
"""

from __future__ import unicode_literals

import json
import ssl
from collections import namedtuple

from ...exceptions import MusicServiceException

# The desktop controller's fixed control-API key. Extracted from the official
# desktop controller binary (sclib-csharp.dll, FUN_111fdc10 XOR table entry
# id=0). It is a shared constant, not per-household or per-device.
SONOS_API_KEY = "4e4561be-d88d-4297-b0d9-ffef5591b730"

# The desktop controller's control-API defaults.  These are fixed values
# from the official controller, not auto-discovered from the device: the
# port mirrors ``SSLPort`` in ZoneGroupState but is *not* read from it at
# runtime (the zone-group parser does not retain the raw XML).  Callers may
# override the port with ``port=`` when their speakers advertise a different
# value; the API version is the desktop controller's current default.
_CONTROL_API_PORT = 1443
_CONTROL_API_VERSION = "v1"

# DirectControl application ids hard-coded in the desktop controller binary
# (``sclib-csharp.dll``) per Sonos service id.  These are the values the
# speaker reports as ``r:DirectControlClientID`` during a DirectControl
# session, and they are distinct from the ``x-sonos-vli:`` URI label (a
# separate runtime representation).  SID 9 and 12 are both Spotify; 9 is
# included because the binary maps it to the same application.
_DIRECT_CONTROL_APPS = {
    9: "spotify.connect.adapter",  # Spotify
    12: "spotify.connect.adapter",  # Spotify
    236: "com.pandora.dc",  # Pandora
    239: "com.audible.mobile.sonos",  # Audible
}

# Mapping of service id to its DirectControl provider label (the part after
# ``x-sonos-vli:<uid>:2,``). Only services known to use DirectControl are
# listed; others fall back to regular URI playback.
_DIRECT_CONTROL_PROVIDERS = {
    9: "spotify",  # Spotify
    12: "spotify",  # Spotify
    236: "pandora",  # Pandora
    239: "audible",  # Audible
}

# Services whose DirectControl sessions are observable through the control
# API's playbackMetadata endpoint (``playbackSession.clientId``).  Spotify
# reports ``spotify.connect.adapter`` shortly after entering the session, so
# a wait for it is meaningful.  Pandora and Audible do not report a session
# there (their loaded containers surface as ordinary radio/audiobook
# transports instead), so waiting for a client id would always time out.
_OBSERVABLE_DC_APPS = {9, 12}

# Verified ``containerMetadata.type`` values for loadContainer per service id.
# The type is critical: the speaker silently keeps the old container when the
# type does not match the service's DirectControl session (plain
# ``"playlist"`` no-ops for Spotify). Only values verified live on hardware
# are listed; unknown services require the caller to pass ``container_type``
# explicitly.
_DIRECT_CONTROL_CONTAINER_TYPES = {
    # SID 9 and 12 share the Spotify DirectControl application and therefore
    # the verified Spotify container type (radio/playlist/album, plus a bare
    # track object id).
    9: "playlist.spotify.connect",
    12: "playlist.spotify.connect",  # verified: radio/playlist/album + track
    236: "program.pandora.connect",  # Pandora (verified: station ST: ids)
    239: "audiobook.audible.connect",  # Audible (verified: book reftitle: ids)
}

# Normalized browse item types which are the playable unit of each
# DirectControl service.  ``None`` means the service's playable units are
# its containers (``mediaCollection``).  Pandora stations surface as
# ``program`` items and Audible books as ``audiobook`` items, so those (not
# the service's browse folders) are what loadContainer consumes.  Spotify
# tracks (``mediaMetadata`` ``track`` items) are playable in addition to its
# containers: the desktop's ``playlist.spotify.connect`` container type
# accepts a bare track object id and the session plays that track.
_DIRECT_CONTROL_PLAYABLE_ITEM_TYPES = {
    # Spotify (SID 9 and 12): containers (see _is_direct_control_playable)
    # + tracks; both share the same DirectControl application.
    9: {"track"},
    12: {"track"},
    236: {"program"},  # Pandora: stations
    239: {"audiobook"},  # Audible: books
}


def _secure_context():
    """Return a TLS context that trusts the speaker's self-signed cert."""
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def direct_control_observable(service_id):
    """Whether a service's DirectControl session is observable via the API.

    Spotify reports ``playbackSession.clientId`` after entering its session,
    so callers can wait for it.  Pandora and Audible never report a session
    there, so a wait would always time out and should be skipped.
    """
    return int(service_id) in _OBSERVABLE_DC_APPS


def direct_control_app_id(service_id):
    """Return the DirectControl application id for a service id, or ``None``.

    This is the value the speaker reports as ``r:DirectControlClientID``
    during a DirectControl session (e.g. ``spotify.connect.adapter``), not
    the ``x-sonos-vli:`` URI label (see :func:`direct_control_provider`).

    Args:
        service_id (int): The Sonos service id.

    Returns:
        str or None: The DirectControl application id, or ``None`` if the
        service is not a DirectControl service.
    """
    return _DIRECT_CONTROL_APPS.get(int(service_id))


def direct_control_provider(service_id):
    """Return the DirectControl provider label for a service id, or ``None``.

    Args:
        service_id (int): The Sonos service id.

    Returns:
        str or None: The provider label used in the ``x-sonos-vli:`` URI
        (e.g. ``spotify``), or ``None`` if the service is not a
        DirectControl service.
    """
    return _DIRECT_CONTROL_PROVIDERS.get(int(service_id))


def direct_control_uri(player_uid, provider, label=""):
    """Build the ``x-sonos-vli:`` transport URI for a DirectControl session.

    Args:
        player_uid (str): The player's UID (``RINCON_...``).
        provider (str): The DirectControl provider label (e.g. ``spotify``).
        label (str): An opaque session label. The speaker ignores it; the
            real context is set separately via :func:`load_container`.

    Returns:
        str: The ``x-sonos-vli:`` URI.
    """
    return "x-sonos-vli:{}:2,{}:{}".format(player_uid, provider, label)


def direct_control_metadata(player_uid, provider, uri, title=None):
    """Build the DIDL metadata for a DirectControl transport URI.

    Mirrors the desktop controller's ``<item>`` for a virtual line-in:
    ``upnp:class`` ``object.item.audioItem.linein`` and a ``<vli>`` element.

    Args:
        player_uid (str): The player's UID (``RINCON_...``).
        provider (str): The DirectControl provider label (e.g. ``spotify``);
            used as the item id, matching the desktop controller.
        uri (str): The ``x-sonos-vli:`` URI to embed in ``<res>``.
        title (str, optional): A title shown in the controller (e.g. the
            service name). Defaults to ``provider``.

    Returns:
        str: DIDL-Lite XML metadata.
    """
    from xml.sax.saxutils import escape

    title = title or provider
    return (
        '<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
        'xmlns:r="urn:schemas-rinconnetworks-com:metadata-1-0/" '
        'xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/">'
        '<item id="%s" parentID="0" restricted="false">'
        "<dc:title>%s</dc:title>"
        "<upnp:class>object.item.audioItem.linein</upnp:class>"
        '<res protocolInfo="x-sonos-vli:*:audio:*">%s</res>'
        '<vli cookie="7" group=""></vli>'
        "</item></DIDL-Lite>"
    ) % (escape(provider), escape(title), escape(uri))


def direct_control_container_type(service_id):
    """Return the verified loadContainer container type for a service id.

    Args:
        service_id (int): The Sonos service id.

    Returns:
        str or None: The ``containerMetadata.type`` value verified for the
        service's DirectControl sessions, or ``None`` when the service has
        no verified type (callers must then pass ``container_type``
        explicitly to :func:`load_container`).
    """
    return _DIRECT_CONTROL_CONTAINER_TYPES.get(int(service_id))


def direct_control_playable_item_types(service_id):
    """Return the normalized item types loadContainer consumes for a service.

    Args:
        service_id (int): The Sonos service id.

    Returns:
        set or None: The normalized browse item types (e.g. ``program`` for
        Pandora stations, ``audiobook`` for Audible books) which are the
        service's DirectControl playable unit, or ``None`` when the playable
        units are the service's containers (Spotify).
    """
    return _DIRECT_CONTROL_PLAYABLE_ITEM_TYPES.get(int(service_id))


def _control_api_request(
    device, path, method="GET", body=None, api_key=SONOS_API_KEY, timeout=10, port=None
):
    """Send a request to the speaker's local control API.

    The API lives on the speaker's HTTPS port ``1443`` by default (the
    desktop controller's value; ``SSLPort`` in ZoneGroupState).  This is a
    documented default, not auto-discovered: ``port`` overrides it when the
    caller has read the device's zone-group state itself.
    """
    import urllib.request
    import urllib.error

    effective_port = int(port or _CONTROL_API_PORT)
    url = "https://{}:{}/api/{}/groups/{}".format(
        device.ip_address, effective_port, _CONTROL_API_VERSION, path.lstrip("/")
    )
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "X-Sonos-Api-Key": api_key,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(
            request, context=_secure_context(), timeout=timeout
        ) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", errors="replace")
    except Exception as error:  # pragma: no cover - network errors
        raise MusicServiceException(
            "DirectControl control API request failed: {}".format(error)
        ) from error


DirectControlSession = namedtuple(
    "DirectControlSession", ["client_id", "account_id", "suspended"]
)
"""The DirectControl session a group reports via the control API.

Attributes:
    client_id (str): The DirectControl application id, e.g.
        ``spotify.connect.adapter`` (``r:DirectControlClientID``).
    account_id (str): The account id from ``playbackSession.accountId``.
    suspended (bool): Whether the session is suspended
        (``r:DirectControlIsSuspended``). A suspended session is still the
        expected application but is not actively driving playback.
"""


def direct_control_session(
    device, group_uid, api_key=SONOS_API_KEY, timeout=10, port=None
):
    """Return the active DirectControl session on a group, or ``None``.

    Queries the control API's ``playbackMetadata`` endpoint and reads
    ``playbackSession``: ``clientId`` (the value the speaker reports as
    ``r:DirectControlClientID``, e.g. ``spotify.connect.adapter``),
    ``accountId`` and ``isSuspended``.  This is how a controller
    distinguishes an active Spotify session from an active Audible or
    Pandora session (which all share the broad ``DIRECT_CONTROL``
    music-source class) and detects suspended sessions.

    Returns:
        :class:`DirectControlSession` or None: The active session, or
        ``None`` when the group is not in a DirectControl session (or the
        endpoint is unavailable).
    """
    status, raw = _control_api_request(
        device,
        "{}/playbackMetadata".format(group_uid),
        api_key=api_key,
        timeout=timeout,
        port=port,
    )
    if status != 200:
        return None
    try:
        data = json.loads(raw)
        session = data.get("playbackSession") or {}
        client_id = session.get("clientId") or ""
        if not client_id:
            return None
        return DirectControlSession(
            client_id=client_id,
            account_id=session.get("accountId") or "",
            suspended=bool(session.get("isSuspended")),
        )
    except ValueError:
        return None


def direct_control_client_id(
    device, group_uid, api_key=SONOS_API_KEY, timeout=10, port=None
):
    """Return the active DirectControl application id on a group, or ``None``.

    Convenience wrapper over :func:`direct_control_session` returning only
    the application id.  Use the session form when the account id or the
    suspended flag matters too.

    Returns:
        str or None: The DirectControl application id, or ``None`` when the
        group is not in a DirectControl session (or the endpoint is
        unavailable).
    """
    session = direct_control_session(
        device, group_uid, api_key=api_key, timeout=timeout, port=port
    )
    return session.client_id if session else None


def wait_for_direct_control(
    device,
    group_uid,
    expected_app_id,
    api_key=SONOS_API_KEY,
    timeout=10,
    poll_interval=0.5,
    port=None,
):
    """Wait until the group reports the expected DirectControl application.

    Entering a DirectControl session is asynchronous: after
    ``SetAVTransportURI`` + ``Play`` the speaker negotiates with the
    service's cloud session before ``r:DirectControlClientID`` reflects the
    new application. Callers should wait for this before posting a
    ``loadContainer`` request, otherwise the context switch races the
    session start.

    ``timeout`` bounds the *total* wait: each poll uses at most the time
    remaining (capped at two seconds per request), so a stalled control-API
    request cannot stretch the wait beyond the deadline.

    Returns:
        bool: ``True`` if the expected application appeared before
        ``timeout`` seconds, ``False`` otherwise.
    """
    import time

    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        client_id = direct_control_client_id(
            device,
            group_uid,
            api_key=api_key,
            timeout=min(remaining, 2.0),
            port=port,
        )
        if client_id == expected_app_id:
            return True
        time.sleep(min(poll_interval, remaining))


def load_container(
    device,
    group_uid,
    object_id,
    service_id,
    account_serial,
    name,
    container_type=None,
    image_url="",
    description="",
    tags=None,
    api_key=SONOS_API_KEY,
    timeout=10,
    port=None,
):
    """Select a container as the current DirectControl session context.

    Posts the container to the speaker's control API
    (``/api/v1/groups/<group>/playback/loadContainer``). The speaker must
    already be in a DirectControl session (see
    :meth:`SoCo.play_direct_control`); the call switches the session to the
    given container and starts playing it.

    Args:
        device (SoCo): The player hosting the group.
        group_uid (str): The full group UID (``RINCON_...:NNN``) as returned
            by :attr:`SoCo.group`.
        object_id (str): The provider container id (e.g.
            ``spotify:playlist:37i9dQZF1E4sD262twcXeU``).
        service_id (int): The Sonos service id (e.g. ``12`` for Spotify).
        account_serial (int): The account's serial number in the household
            (e.g. ``63``).
        name (str): The container name.
        container_type (str, optional): The container type (e.g.
            ``playlist.spotify.connect`` for Spotify). When ``None``, the
            verified type for the service is used (see
            :func:`direct_control_container_type`).
        image_url (str, optional): The container artwork URI, mirrored
            from the browse item's ``album_art_uri``. Defaults to empty.
        description (str, optional): The container description/summary,
            mirrored from the browse item's ``summary``. Defaults to empty.
        tags (list, optional): Explicit-content or other container tags.
            Defaults to an empty list; the normalized browse item does not
            currently surface provider tags, so this is a forward-compatible
            pass-through rather than a populated field.
        api_key (str): The control-API key. Defaults to the fixed
        :data:`SONOS_API_KEY`.
        timeout (int): HTTPS timeout in seconds.
        port (int, optional): The speaker's control-API port. Defaults to
            ``1443`` (the desktop controller's value; ``SSLPort`` in
            ZoneGroupState). This is a documented default, not
            auto-discovered — pass the zone-group value when available.

    Raises:
        MusicServiceException: If the control API rejects the request, or
            no container type is known for the service and none was given.
    """
    if container_type is None:
        container_type = direct_control_container_type(service_id)
    if container_type is None:
        raise MusicServiceException(
            "No verified DirectControl container type for service id "
            "{}; pass container_type explicitly".format(service_id)
        )

    body = {
        "containerId": {
            "objectId": object_id,
            "serviceId": str(service_id),
            "accountId": "sn_{}".format(account_serial),
        },
        "containerMetadata": {
            "type": container_type,
            "name": name,
            "imageUrl": image_url or "",
            "description": description or "",
            "tags": tags or [],
        },
        "playOnCompletion": True,
    }
    status, raw = _control_api_request(
        device,
        "{}/playback/loadContainer".format(group_uid),
        method="POST",
        body=body,
        api_key=api_key,
        timeout=timeout,
        port=port,
    )
    if status != 200:
        raise MusicServiceException(
            "DirectControl loadContainer failed with HTTP {}: {}".format(
                status, raw[:200]
            )
        )
    return True
