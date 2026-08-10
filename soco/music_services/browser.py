# pylint: disable=too-many-lines

"""Read-only browsing of configured Sonos music-service accounts.

This module is deliberately additive to :mod:`soco.music_services.music_service`.
The older :class:`~soco.music_services.music_service.MusicService` API remains
unchanged; this module implements the desktop controller's newer account-aware
browse flow alongside it.

Sonos currently exposes two related browse transports. Legacy services use the
SMAPI SOAP methods directly. Some newer services advertise a manifest with an
authenticated JSON browse endpoint for their home page, then hand child object
IDs back to ordinary SMAPI. The controller also supplies credentials for
accounts which are already configured in the household through the encrypted
``ThirdPartyMediaServersX`` topology event.

Only read operations are implemented here. In particular, this module does not
add, remove, rename, authorize, or otherwise mutate music-service accounts.
"""

from __future__ import unicode_literals

import base64
import hashlib
import html
import json
import logging
import os
import queue
import re
import shutil
import subprocess
import uuid
from collections.abc import Mapping

import requests

from .. import discovery
from ..exceptions import MusicServiceAuthException, MusicServiceException
from ..xml import XML
from .music_service import MusicService

_LOG = logging.getLogger(__name__)

SOAP_ENV = "http://schemas.xmlsoap.org/soap/envelope/"
SMAPI_NS = "http://www.sonos.com/Services/1.1"
_ACCOUNT_SALT = bytes.fromhex("1a01a731c96e9ebde8475182b274b70e")

# This is the user agent used by the desktop-controller flow on which the
# implementation below is based. Some music services are surprisingly strict
# about Sonos controller identity strings, so this should not be replaced with
# requests' default user agent merely for tidiness.
DESKTOP_USER_AGENT = (
    "Linux UPnP/1.0 Sonos/90.0-77070 "
    "(WDCR:Microsoft Windows NT 10.0.19045 64-bit)"
)


def _as_mapping(value):
    """Return a provider mapping, or an empty mapping for a malformed value."""
    return value if isinstance(value, Mapping) else {}


def _as_list(value):
    """Return a provider list, or an empty list for a malformed value."""
    return value if isinstance(value, list) else []


def _as_string(value):
    """Return a provider string, or an empty string for a malformed value."""
    return value if isinstance(value, str) else ""


def _local_name(tag):
    """Return an XML tag name without its namespace."""
    return tag.rsplit("}", 1)[-1]


def _children(node, name):
    """Return descendants whose local XML name matches ``name``."""
    return [child for child in node.iter() if _local_name(child.tag) == name]


def _child_text(node, name, default=""):
    """Return the text of a direct child identified by local XML name."""
    for child in node:
        if _local_name(child.tag) == name:
            return child.text or default
    return default


def _element_value(node):
    """Convert a SMAPI result element without discarding nested metadata.

    Third-party services are not consistent enough for a fixed schema at this
    boundary. The conversion therefore preserves unknown fields and repeated
    elements, and the public browse models normalize only the small set of
    fields needed to navigate the result.
    """
    if not list(node):
        return node.text or ""

    result = {}
    for child in node:
        name = _local_name(child.tag)
        value = _element_value(child)
        if name not in result:
            result[name] = value
            continue
        if not isinstance(result[name], list):
            result[name] = [result[name]]
        result[name].append(value)
    return result


def _explicit_bool(value):
    """Return a provider boolean only when it is explicitly represented."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.lower()
        if normalized == "true":
            return True
        if normalized == "false":
            return False
    return None


def _artwork_uri(record):
    """Resolve the artwork shapes used by legacy SMAPI and content JSON."""
    record = _as_mapping(record)
    if not record:
        return ""

    for key in ("album_art_uri", "albumArtURI", "albumArtUri", "imageUrl", "logo"):
        value = _as_string(record.get(key))
        if value:
            return (
                value.replace("${width}", "400")
                .replace("${height}", "400")
                .replace("${ratio}", "1x1")
            )

    for key in (
        "streamMetadata",
        "trackMetadata",
        "metadata",
        "container",
        "track",
        "album",
    ):
        value = _artwork_uri(record.get(key))
        if value:
            return value
    return ""


def _legacy_item_kind(provider_kind, record):
    """Mirror the desktop controller's canPush distinction for SMAPI items."""
    if provider_kind != "mediaCollection":
        return provider_kind

    object_id = str(record.get("id", "")).lower()
    # These provider records are controller actions, not browse containers.
    if object_id.startswith(("upsell-banner/", "refmarketplace:")):
        return "mediaMetadata"

    can_enumerate = _explicit_bool(record.get("canEnumerate"))
    if can_enumerate is True:
        return provider_kind
    if can_enumerate is False:
        return "mediaMetadata"

    item_type = str(record.get("itemType", "")).lower()
    if item_type in {"program", "stream", "track"}:
        return "mediaMetadata"
    if _explicit_bool(record.get("canPlay")) is True and item_type not in {
        "album",
        "albumlist",
        "collection",
        "container",
        "playlist",
    }:
        return "mediaMetadata"
    return provider_kind


class ConfiguredMusicServiceAccount:
    """Credentials for one music-service account already stored by Sonos.

    Instances are read from ``ThirdPartyMediaServersX``. They are intentionally
    separate from :class:`soco.music_services.accounts.Account`: the existing
    class models the legacy ``/status/accounts`` response, while this class
    represents the newer encrypted account record used by the desktop browse
    path. Keeping the models separate avoids changing existing SoCo behavior.
    """

    def __init__(
        self,
        service_id,
        serial_number,
        udn,
        username="",
        password="",
        token="",
        key="",
        nickname="",
        tier="",
        schema_revision=7,
    ):
        self.service_id = int(service_id)
        self.serial_number = int(serial_number)
        self.udn = udn
        self.username = username
        self.password = password
        self.token = token
        self.key = key
        self.nickname = nickname
        self.tier = tier
        self.schema_revision = int(schema_revision)

    def __repr__(self):
        return "<{} service_id={} serial_number={} nickname={!r} at {}>".format(
            self.__class__.__name__,
            self.service_id,
            self.serial_number,
            self.nickname,
            hex(id(self)),
        )

    @property
    def account_uid(self):
        """int: The account UID encoded in the Sonos account UDN.

        ``SerialNum0`` is the controller-facing account selector. The native
        content transport instead keys its per-account device identity from the
        hexadecimal account UID at the end of the token UDN.
        """
        match = re.search(r"X_#Svc\d+-([0-9a-fA-F]+)-Token$", self.udn)
        if not match:
            raise MusicServiceAuthException(
                "Account UDN does not contain a numeric AccountUID: {}".format(
                    self.udn
                )
            )
        return int(match.group(1), 16)

    @classmethod
    def from_element(cls, element):
        """Build an account from a decrypted ``ThirdPartyMediaServersX`` node."""
        attrs = element.attrib
        match = re.match(r"^SA_RINCON(\d+)", attrs.get("UDN", ""))
        if not match:
            return None

        encoded_type = int(match.group(1))
        return cls(
            service_id=encoded_type // 256,
            serial_number=int(attrs.get("SerialNum0", "0") or 0),
            udn=attrs.get("UDN", ""),
            username=attrs.get("Username0", ""),
            password=attrs.get("Password0", ""),
            token=attrs.get("Token0", ""),
            key=attrs.get("Key0", ""),
            nickname=attrs.get("Nickname0", ""),
            tier=attrs.get("Tier0", ""),
            schema_revision=encoded_type % 256,
        )

    @classmethod
    def from_payload(cls, payload):
        """Parse all configured accounts from decrypted account XML bytes."""
        try:
            root = XML.fromstring(payload)
        except XML.ParseError as error:
            raise MusicServiceException(
                "ThirdPartyMediaServersX did not contain valid account XML"
            ) from error

        accounts = []
        for element in root:
            account = cls.from_element(element)
            if account is not None:
                accounts.append(account)
        return accounts

    @classmethod
    def get_accounts(cls, soco=None, timeout=8):
        """Return accounts currently configured in the Sonos household.

        The account payload arrives as the initial ZoneGroupTopology event, so
        this method reuses SoCo's normal subscription implementation rather than
        opening a second callback server. No account or player state is changed.

        Args:
            soco (SoCo, optional): Device used for the topology subscription.
                A device returned by :func:`soco.discovery.any_soco` is used when
                omitted.
            timeout (float): Number of seconds to wait for the initial event.

        Returns:
            list: :class:`ConfiguredMusicServiceAccount` instances.
        """
        device = soco or discovery.any_soco()
        encoded = _capture_account_event(device, timeout)
        payload = _decrypt_account_payload(encoded, device.household_id)
        return cls.from_payload(payload)


def _capture_account_event(device, timeout):
    """Return the encrypted account value from a ZoneGroupTopology event."""
    subscription = device.zoneGroupTopology.subscribe(
        requested_timeout=max(int(timeout) + 10, 15)
    )
    # The synchronous browser relies on the normal queue-based event backend.
    # This check provides an actionable failure if a caller configured one of
    # SoCo's asynchronous event modules instead of silently opening a second,
    # unrelated callback listener just for music-service browsing.
    if not hasattr(subscription, "events"):
        raise MusicServiceException(
            "Configured account browsing requires SoCo's synchronous event backend"
        )

    try:
        event = subscription.events.get(timeout=timeout)
    except queue.Empty as error:
        raise MusicServiceException(
            "No ThirdPartyMediaServersX event arrived within {} seconds".format(
                timeout
            )
        ) from error
    finally:
        try:
            subscription.unsubscribe()
        except Exception:  # pylint: disable=broad-except
            _LOG.debug("Could not unsubscribe account capture", exc_info=True)

    value = event.variables.get("third_party_media_servers_x")
    if not value:
        raise MusicServiceException(
            "ZoneGroupTopology event did not contain ThirdPartyMediaServersX"
        )
    return value


def _decrypt_account_payload(encoded, household_id):
    """Decrypt and verify the protocol-defined account envelope.

    The format is the same ``2:`` envelope used by the desktop controller. Sonos
    derives a per-household AES-128-CBC key with MD5 and appends the first four
    bytes of an MD5 digest to the plaintext as an integrity field. MD5 is used
    here because it is part of the on-wire Sonos protocol, not as a general
    cryptographic choice.
    """
    encoded = html.unescape(encoded).strip()
    if not encoded.startswith("2:"):
        raise MusicServiceException("Unsupported ThirdPartyMediaServersX version")

    try:
        raw = base64.b64decode(encoded[2:].encode("ascii"), validate=True)
    except (TypeError, ValueError) as error:
        raise MusicServiceException(
            "ThirdPartyMediaServersX contained invalid base64"
        ) from error

    if len(raw) < 32 or len(raw[16:]) % 16:
        raise MusicServiceException("Invalid encrypted account payload dimensions")

    iv = raw[:16]
    ciphertext = raw[16:]
    global_key = hashlib.md5(household_id.encode("utf-8") + _ACCOUNT_SALT).digest()
    blob_key = hashlib.md5(iv + global_key).digest()
    checked = _aes_128_cbc_decrypt(ciphertext, blob_key, iv)
    if len(checked) < 4:
        raise MusicServiceException("Decrypted account payload is too short")

    payload, checksum = checked[:-4], checked[-4:]
    if hashlib.md5(payload).digest()[:4] != checksum:
        raise MusicServiceException("Account payload integrity check failed")
    return payload


def _aes_128_cbc_decrypt(ciphertext, key, iv):
    """Decrypt the Sonos account envelope without adding a hard dependency.

    ``cryptography`` is preferred when the caller already has it installed. It
    cannot be made an unconditional SoCo dependency without dropping some of
    SoCo's currently supported Python versions, so OpenSSL remains a fallback.
    The fallback mirrors the working desktop-browser research implementation.
    """
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError:
        openssl = shutil.which("openssl")
        if not openssl:
            raise MusicServiceException(
                "Browsing configured music-service accounts requires either "
                "the 'cryptography' package or an OpenSSL executable"
            )
        result = subprocess.run(
            [
                openssl,
                "enc",
                "-d",
                "-aes-128-cbc",
                "-K",
                key.hex(),
                "-iv",
                iv.hex(),
            ],
            input=ciphertext,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if result.returncode != 0:
            raise MusicServiceException(
                "AES-CBC decryption or PKCS#7 validation failed"
            )
        return result.stdout

    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    if not padded:
        raise MusicServiceException("AES-CBC decryption returned no data")
    padding_length = padded[-1]
    if not 1 <= padding_length <= 16:
        raise MusicServiceException("Invalid PKCS#7 padding in account payload")
    padding = bytes([padding_length]) * padding_length
    if padded[-padding_length:] != padding:
        raise MusicServiceException("Invalid PKCS#7 padding in account payload")
    return padded[:-padding_length]


def _account_content_device_id(household_id, account):
    """Build the per-account identity used by native content sessions."""
    return "{}_{:08x}".format(household_id, account.account_uid)


def _local_time_zone():
    """Return the controller time-zone name used in SMAPI context headers."""
    configured = os.environ.get("TZ", "").strip()
    if configured:
        return configured
    resolved = os.path.realpath("/etc/localtime")
    marker = "/zoneinfo/"
    if marker in resolved:
        return resolved.split(marker, 1)[1]
    return "UTC"


class MusicServiceBrowseItem:
    """One normalized read-only item returned by a configured service."""

    def __init__(
        self,
        item_id,
        title,
        kind,
        item_type="",
        artist="",
        summary="",
        album_art_uri="",
        source_transport="smapi",
        section="",
        display_type="",
        variant="",
        raw=None,
    ):
        self.item_id = item_id
        self.title = title
        self.kind = kind
        self.item_type = item_type
        self.artist = artist
        self.summary = summary
        self.album_art_uri = album_art_uri
        self.source_transport = source_transport
        self.section = section
        self.display_type = display_type
        self.variant = variant
        self.raw = raw or {}

    def __repr__(self):
        return "<{} {!r} ({}) at {}>".format(
            self.__class__.__name__, self.title, self.item_id, hex(id(self))
        )

    @property
    def can_browse(self):
        """bool: Whether selecting this item should request child metadata."""
        return self.kind == "mediaCollection"


class MusicServiceBrowseResult:
    """A page of items returned by :class:`MusicServiceBrowser`."""

    def __init__(
        self,
        items,
        index=0,
        total=None,
        transport="smapi",
        requested_id="root",
        endpoint="",
        raw=None,
    ):
        self.items = list(items)
        self.index = int(index)
        self.count = len(self.items)
        self.total = self.count if total is None else int(total)
        self.transport = transport
        self.requested_id = requested_id
        self.endpoint = endpoint
        self.raw = raw

    def __repr__(self):
        return "<{} count={} total={} transport={!r} at {}>".format(
            self.__class__.__name__,
            self.count,
            self.total,
            self.transport,
            hex(id(self)),
        )


class _ConfiguredSmapiClient:
    """SMAPI client using credentials already stored in the Sonos household."""

    def __init__(
        self,
        music_service,
        account,
        device,
        household_id,
        device_id,
        controller_id,
        time_zone,
        explicit_content=False,
        allow_credential_refresh=False,
        session=None,
    ):
        self.music_service = music_service
        self.account = account
        self.device = device
        self.household_id = household_id
        self.device_id = device_id
        self.controller_id = controller_id
        self.time_zone = time_zone
        self.explicit_content = explicit_content
        self.allow_credential_refresh = allow_credential_refresh
        self.session_id = ""
        self.session = session or requests.Session()

    @property
    def capabilities(self):
        return int(self.music_service.capabilities)

    def _credentials(self, parent, mode="normal"):
        credentials = XML.SubElement(parent, "{%s}credentials" % SMAPI_NS)
        if self.capabilities & (1 << 18) and self.device.uid:
            XML.SubElement(credentials, "{%s}zonePlayerId" % SMAPI_NS).text = (
                self.device.uid
            )
        XML.SubElement(credentials, "{%s}deviceId" % SMAPI_NS).text = self.device_id
        XML.SubElement(credentials, "{%s}deviceProvider" % SMAPI_NS).text = "Sonos"

        if mode == "base" or self.music_service.auth_type == "Anonymous":
            return

        if mode == "normal" and self.music_service.auth_type in (
            "UserId",
            "UserIdPassword",
        ):
            login = XML.SubElement(credentials, "{%s}login" % SMAPI_NS)
            XML.SubElement(login, "{%s}username" % SMAPI_NS).text = (
                self.account.username
            )
            XML.SubElement(login, "{%s}password" % SMAPI_NS).text = (
                self.account.password
            )
            return

        # Auth=DeviceLink describes how an account is provisioned. Once Sonos
        # has stored Token0/Key0, the desktop controller browses with that pair
        # like an AppLink account. getSessionId is only the no-token fallback.
        if mode == "normal" and self.account.token:
            login_token = XML.SubElement(credentials, "{%s}loginToken" % SMAPI_NS)
            if not (self.capabilities & 8):
                XML.SubElement(login_token, "{%s}token" % SMAPI_NS).text = (
                    self.account.token
                )
                if self.account.key:
                    XML.SubElement(login_token, "{%s}key" % SMAPI_NS).text = (
                        self.account.key
                    )
            XML.SubElement(login_token, "{%s}householdId" % SMAPI_NS).text = (
                self.household_id
            )
            return

        if mode == "normal" and self.music_service.auth_type == "DeviceLink":
            if self.session_id:
                XML.SubElement(credentials, "{%s}sessionId" % SMAPI_NS).text = (
                    self.session_id
                )
            return

        if self.account.token or self.household_id:
            login_token = XML.SubElement(credentials, "{%s}loginToken" % SMAPI_NS)
            # Capability bit 3 moves the normal token into an HTTP Bearer
            # header. refreshAuthToken is the exception: the old token/key go
            # back into SOAP even when that capability is present.
            if not (self.capabilities & 8) or mode == "refresh":
                if self.account.token:
                    XML.SubElement(login_token, "{%s}token" % SMAPI_NS).text = (
                        self.account.token
                    )
                if self.account.key:
                    XML.SubElement(login_token, "{%s}key" % SMAPI_NS).text = (
                        self.account.key
                    )
            XML.SubElement(login_token, "{%s}householdId" % SMAPI_NS).text = (
                self.household_id
            )

    def _envelope(self, action, fields, credential_mode="normal"):
        envelope = XML.Element("{%s}Envelope" % SOAP_ENV)
        header = XML.SubElement(envelope, "{%s}Header" % SOAP_ENV)
        self._credentials(header, mode=credential_mode)

        # The desktop app keys SMAPI context inclusion from capability bit 16.
        if self.capabilities & (1 << 16):
            context = XML.SubElement(header, "{%s}context" % SMAPI_NS)
            XML.SubElement(context, "{%s}timeZone" % SMAPI_NS).text = self.time_zone
            if self.capabilities & (1 << 21) and self.explicit_content:
                filtering = XML.SubElement(
                    context, "{%s}contentFiltering" % SMAPI_NS
                )
                XML.SubElement(filtering, "{%s}explicit" % SMAPI_NS).text = "true"

        body = XML.SubElement(envelope, "{%s}Body" % SOAP_ENV)
        operation = XML.SubElement(body, "{%s}%s" % (SMAPI_NS, action))
        for name, value in fields.items():
            XML.SubElement(operation, "{%s}%s" % (SMAPI_NS, name)).text = str(value)
        return XML.tostring(envelope, encoding="utf-8")

    def _request(self, action, fields, credential_mode="normal", bearer_token=None):
        endpoint = self.music_service.secure_uri
        if not endpoint.lower().startswith("https://"):
            raise MusicServiceException(
                "SMAPI endpoint must use HTTPS: {}".format(endpoint)
            )

        current_bearer = self.account.token if bearer_token is None else bearer_token
        headers = {
            "Content-Type": 'text/xml; charset="utf-8"',
            "Soapaction": '"{}#{}"'.format(SMAPI_NS, action),
            "Accept-Language": "en-US",
            "X-Sonos-Controller-ID": self.controller_id,
            "User-Agent": DESKTOP_USER_AGENT,
        }
        if (
            credential_mode != "refresh"
            and self.capabilities & 8
            and current_bearer
        ):
            headers["Authorization"] = "Bearer {}".format(current_bearer)

        try:
            response = self.session.post(
                endpoint,
                data=self._envelope(action, fields, credential_mode),
                headers=headers,
                timeout=20,
            )
        except requests.RequestException as error:
            raise MusicServiceException(
                "{} request failed: {}".format(self.music_service.service_name, error)
            ) from error

        payload = response.content
        try:
            root = XML.fromstring(payload)
        except XML.ParseError as error:
            # Sonos Radio has returned xsi:nil without declaring the xsi prefix.
            # The desktop parser tolerates that specific provider defect, so
            # repair only that case before treating the response as corrupt.
            repaired = payload
            if b"xsi:" in payload and b"xmlns:xsi" not in payload:
                repaired = re.sub(
                    br"(<(?:[A-Za-z_][\w.-]*:)?Envelope)(\s)",
                    br'\1 xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"\2',
                    payload,
                    count=1,
                )
            if repaired == payload:
                raise self._non_xml_response_error(
                    payload, response.status_code
                ) from error
            try:
                root = XML.fromstring(repaired)
            except XML.ParseError as repaired_error:
                raise self._non_xml_response_error(
                    payload, response.status_code
                ) from repaired_error

        faults = _children(root, "Fault")
        if faults:
            fault = faults[0]
            detail_nodes = _children(fault, "detail")
            detail = _element_value(detail_nodes[0]) if detail_nodes else None
            raise _BrowseSoapFault(
                _child_text(fault, "faultcode", "SMAPI.Fault"),
                _child_text(fault, "faultstring", "Unknown SMAPI fault"),
                response.status_code,
                detail,
            )
        if response.status_code != 200:
            raise _BrowseSoapFault(
                "HTTP",
                "Unexpected status {}".format(response.status_code),
                response.status_code,
            )
        return root

    @staticmethod
    def _is_expired_fault(fault):
        combined = "{} {}".format(fault.code, fault.message).lower()
        return (
            "authtokenexpired" in combined
            or "invalidtoken" in combined
            or "tokenrefreshrequired" in combined
            or "token expired" in combined
            or "unauthorized" in combined
            or fault.http_status == 401
        )

    def _non_xml_response_error(self, payload, status_code):
        """Return the right exception for a response that is not XML.

        A few providers answer with plain text instead of a SOAP envelope;
        Sonos Radio, for example, responds ``Unauthorized`` when the token it
        was given is not usable.  Surfacing that as an auth-flavored fault
        gives a truthful error and lets the credential-refresh flow run.
        """
        stripped = payload.strip()
        if stripped and not stripped.startswith(b"<"):
            text = stripped.decode("utf-8", "replace")[:200]
            return _BrowseSoapFault("HTTP", text, status_code)
        return MusicServiceException(
            "{} returned malformed SMAPI XML".format(self.music_service.service_name)
        )

    @staticmethod
    def _is_invalid_session_fault(fault):
        combined = "{} {}".format(fault.code, fault.message).lower()
        return "invalidsession" in combined or "invalid session" in combined

    @staticmethod
    def _is_transient_fault(fault):
        combined = "{} {}".format(fault.code, fault.message).lower()
        provider_detail = (
            json.dumps(fault.detail, sort_keys=True).lower()
            if fault.detail is not None
            else ""
        )
        # Apple intermittently returns generic SonosError 999 for a valid
        # collection and succeeds immediately on the identical request.
        provider_retry = '"sonoserror": "999"' in provider_detail
        return provider_retry or fault.http_status in {408, 429, 502, 503, 504} or any(
            marker in combined
            for marker in (
                "read timed out",
                "timed out reading",
                "temporarily unavailable",
                "try again",
            )
        )

    @staticmethod
    def _replacement_credentials(detail):
        if isinstance(detail, Mapping):
            token = detail.get("authToken")
            key = detail.get("privateKey")
            if isinstance(token, str) and isinstance(key, str) and token and key:
                return token, key
            for child in detail.values():
                replacement = _ConfiguredSmapiClient._replacement_credentials(child)
                if replacement:
                    return replacement
        elif isinstance(detail, list):
            for child in detail:
                replacement = _ConfiguredSmapiClient._replacement_credentials(child)
                if replacement:
                    return replacement
        return None

    def _replace_credentials(self, token, key):
        self.account.token = token
        self.account.key = key
        self.session_id = ""

    def refresh_auth_token(self):
        """Refresh browse credentials in memory without writing the player.

        The desktop controller updates its active account model here; it does
        not call ``RefreshAccountCredentialsX`` on the Sonos player. Credential
        refresh is opt-in on :class:`MusicServiceBrowser` because it is not a
        pure metadata read from the provider.
        """
        if self.account.token == "needs_reauth":
            raise MusicServiceAuthException(
                "The Sonos household stores needs_reauth instead of a usable token"
            )
        root = self._request(
            "refreshAuthToken", {}, credential_mode="refresh", bearer_token=""
        )
        result_nodes = _children(root, "refreshAuthTokenResult")
        result = result_nodes[0] if result_nodes else root
        token = _child_text(result, "authToken")
        key = _child_text(result, "privateKey")
        if not token or not key:
            raise MusicServiceAuthException(
                "refreshAuthToken returned no authToken/privateKey pair"
            )
        self._replace_credentials(token, key)

    def _refresh_from_fault(self, fault):
        if self.capabilities & 8:
            self.refresh_auth_token()
            return
        replacement = self._replacement_credentials(fault.detail)
        if replacement:
            self._replace_credentials(*replacement)
            return
        self.refresh_auth_token()

    def get_session_id(self):
        """Obtain the legacy DeviceLink session used when no token is stored."""
        if self.music_service.auth_type != "DeviceLink":
            raise MusicServiceAuthException(
                "getSessionId is only valid for DeviceLink services"
            )
        root = self._request(
            "getSessionId",
            {
                "username": self.account.username,
                "password": self.account.password,
            },
            credential_mode="base",
        )
        results = _children(root, "getSessionIdResult")
        session_id = (results[0].text or "").strip() if results else ""
        if not session_id:
            raise MusicServiceAuthException(
                "getSessionId response did not contain a session ID"
            )
        self.session_id = session_id

    def _ensure_session(self):
        if (
            self.music_service.auth_type == "DeviceLink"
            and not self.account.token
            and not self.session_id
        ):
            self.get_session_id()

    def _request_with_refresh(self, action, fields):
        if self.account.token == "needs_reauth":
            raise MusicServiceAuthException(
                "The Sonos household stores needs_reauth instead of a usable token"
            )
        self._ensure_session()
        try:
            return self._request(action, fields)
        except _BrowseSoapFault as fault:
            if (
                self.music_service.auth_type == "DeviceLink"
                and self._is_invalid_session_fault(fault)
            ):
                self.session_id = ""
                self._ensure_session()
                return self._request(action, fields)
            if (
                self.music_service.auth_type == "Anonymous"
                or not self._is_expired_fault(fault)
            ):
                raise
            if not self.allow_credential_refresh:
                raise
            self._refresh_from_fault(fault)
            self._ensure_session()
            return self._request(action, fields)

    def get_metadata(self, object_id="root", index=0, count=100, recursive=False):
        fields = {"id": object_id, "index": str(index), "count": str(count)}
        if recursive:
            fields["recursive"] = "true"

        for attempt in range(3):
            try:
                root = self._request_with_refresh("getMetadata", fields)
                break
            except _BrowseSoapFault as fault:
                if attempt == 2 or not self._is_transient_fault(fault):
                    if self._is_expired_fault(fault):
                        raise MusicServiceAuthException(str(fault)) from fault
                    raise fault.as_music_service_exception() from fault
        else:  # pragma: no cover - both retry exits are explicit
            raise MusicServiceException("getMetadata retry exhausted")

        results = _children(root, "getMetadataResult")
        result = results[0] if results else root
        records = []
        for node in result:
            provider_kind = _local_name(node.tag)
            if provider_kind not in ("mediaCollection", "mediaMetadata"):
                continue
            record = _as_mapping(_element_value(node))
            if not record:
                continue
            record = dict(record)
            record["provider_kind"] = provider_kind
            record["kind"] = _legacy_item_kind(provider_kind, record)
            record["album_art_uri"] = _artwork_uri(record)
            records.append(record)

        return {
            "index": int(_child_text(result, "index", str(index))),
            "count": int(_child_text(result, "count", str(len(records)))),
            "total": int(_child_text(result, "total", str(len(records)))),
            "items": records,
        }

    def search(self, category_id, term, index=0, count=100):
        count = min(count, max(0, 1000 - index))
        try:
            root = self._request_with_refresh(
                "search",
                {
                    "id": category_id,
                    "term": term,
                    "index": str(index),
                    "count": str(count),
                },
            )
        except _BrowseSoapFault as fault:
            if self._is_expired_fault(fault):
                raise MusicServiceAuthException(str(fault)) from fault
            raise fault.as_music_service_exception() from fault
        results = _children(root, "searchResult")
        result = results[0] if results else root
        records = []
        for node in result:
            provider_kind = _local_name(node.tag)
            if provider_kind not in ("mediaCollection", "mediaMetadata"):
                continue
            record = _as_mapping(_element_value(node))
            if not record:
                continue
            record = dict(record)
            record["provider_kind"] = provider_kind
            record["kind"] = _legacy_item_kind(provider_kind, record)
            record["album_art_uri"] = _artwork_uri(record)
            records.append(record)
        return {
            "index": index,
            "count": int(_child_text(result, "count", str(len(records)))),
            "total": min(1000, int(_child_text(result, "total", str(len(records))))),
            "items": records,
        }

    def get_media_metadata(self, object_id):
        try:
            root = self._request_with_refresh("getMediaMetadata", {"id": object_id})
        except _BrowseSoapFault as fault:
            if self._is_expired_fault(fault):
                raise MusicServiceAuthException(str(fault)) from fault
            raise fault.as_music_service_exception() from fault
        results = _children(root, "getMediaMetadataResult")
        if not results:
            raise MusicServiceException(
                "getMediaMetadata response did not contain a result"
            )
        value = _element_value(results[0])
        mapping = _as_mapping(value)
        if mapping:
            result = dict(mapping)
            result["album_art_uri"] = _artwork_uri(result)
            return result
        return {"value": value}


class _BrowseSoapFault(Exception):
    """Internal representation of a provider SOAP fault."""

    def __init__(self, code, message, http_status, detail=None):
        self.code = code
        self.message = message
        self.http_status = http_status
        self.detail = detail
        super().__init__(code, message, http_status)

    def __str__(self):
        return "{}: {} (HTTP {})".format(self.code, self.message, self.http_status)

    def as_music_service_exception(self):
        """Return the appropriate existing public SoCo exception."""
        combined = "{} {}".format(self.code, self.message).lower()
        if (
            "token" in combined
            or "authorization" in combined
            or "unauthorized" in combined
            or self.http_status == 401
        ):
            return MusicServiceAuthException(str(self))
        return MusicServiceException(str(self))


def _service_manifest(music_service, session):
    """Fetch and decode the manifest advertised by ListAvailableServices."""
    if not music_service.manifest_uri:
        return {}
    try:
        response = session.get(
            music_service.manifest_uri,
            headers={"Accept": "application/json", "Accept-Language": "en-US"},
            timeout=20,
        )
        response.raise_for_status()
    except requests.RequestException as error:
        raise MusicServiceException(
            "{} manifest request failed: {}".format(
                music_service.service_name, error
            )
        ) from error
    try:
        manifest = response.json()
    except ValueError as error:
        raise MusicServiceException(
            "{} manifest was not valid JSON".format(music_service.service_name)
        ) from error
    if not isinstance(manifest, Mapping):
        raise MusicServiceException(
            "{} manifest root was not an object".format(music_service.service_name)
        )
    return manifest


def _content_endpoint(music_service, session, endpoint_type="browse"):
    """Return a manifest content endpoint of the requested type."""
    manifest = _service_manifest(music_service, session)
    for endpoint_value in _as_list(manifest.get("endpoints")):
        endpoint = _as_mapping(endpoint_value)
        if endpoint.get("type") != endpoint_type:
            continue
        uri = _as_string(endpoint.get("uri"))
        if uri:
            return uri
    raise MusicServiceException(
        "{} manifest has no {} endpoint".format(
            music_service.service_name, endpoint_type
        )
    )


def _content_headers(
    music_service,
    account,
    device_id,
    controller_id,
    time_zone,
    explicit_content,
):
    headers = {
        "Accept-Language": "en-US",
        "X-Sonos-Device-Id": device_id,
        "X-Sonos-Corr-Id": str(uuid.uuid4()),
        "X-Sonos-Controller-ID": controller_id,
        "User-Agent": DESKTOP_USER_AGENT,
        "Connection": "keep-alive",
    }
    if account.token:
        headers["Authorization"] = "Bearer {}".format(account.token)
    capabilities = int(music_service.capabilities)
    if capabilities & (1 << 16) and time_zone:
        headers["X-Sonos-Context-TimeZone"] = time_zone
    if capabilities & (1 << 21) and explicit_content:
        headers["X-Sonos-Context-ContentFiltering"] = "explicit"
    return headers


def _content_item(item, section=""):
    """Normalize a modern content JSON item for the public browse model."""
    item = _as_mapping(item)
    identity = _as_mapping(item.get("id"))
    content = _as_mapping(item.get("content"))
    object_id = _as_string(identity.get("objectId"))
    if not object_id:
        return None

    record = _as_mapping(content.get("container"))
    content_kind = "container"
    if not record:
        record = _as_mapping(content.get("track"))
        content_kind = "track"
    if not record:
        return None

    item_type = str(record.get("type", content_kind))
    can_enumerate = record.get("canEnumerate")
    collection_types = {"album", "artist", "container", "playlist", "show"}
    kind = (
        "mediaCollection"
        if content_kind == "container"
        and (can_enumerate is True or item_type in collection_types)
        else "mediaMetadata"
    )
    artist_name = _as_string(_as_mapping(record.get("artist")).get("name"))
    return MusicServiceBrowseItem(
        item_id=object_id,
        title=record.get("name", object_id),
        kind=kind,
        item_type=item_type,
        artist=artist_name,
        summary=record.get("summary", ""),
        album_art_uri=_artwork_uri(record),
        source_transport="content",
        section=section,
        display_type=item.get("displayType", ""),
        raw=dict(item),
    )


def _legacy_item(record, source_transport="smapi"):
    """Normalize one legacy SMAPI record."""
    metadata = _as_mapping(
        record.get("trackMetadata") or record.get("streamMetadata")
    )
    artist = _as_string(metadata.get("artist", record.get("artist", "")))
    title = record.get("title") or record.get("name") or record.get("id", "")
    return MusicServiceBrowseItem(
        item_id=str(record.get("id", "")),
        title=title,
        kind=record.get("kind", record.get("provider_kind", "mediaMetadata")),
        item_type=str(record.get("itemType", "")),
        artist=artist,
        summary=str(record.get("summary", "")),
        album_art_uri=record.get("album_art_uri", ""),
        source_transport=source_transport,
        raw=dict(record),
    )


class MusicServiceBrowser:
    """Browse one configured music-service account without changing it.

    The existing :class:`MusicService` API remains the primary legacy SMAPI
    implementation. This class is an opt-in companion for applications which
    need to browse the accounts already configured by the Sonos controller,
    including services which use the newer manifest/content home page.

    Typical use::

        browser = MusicServiceBrowser("Apple Music", device=speaker)
        root = browser.get_metadata()
        child = browser.get_metadata(root.items[0])

    If more than one account for the service is configured, pass the desired
    :class:`ConfiguredMusicServiceAccount` explicitly.
    """

    def __init__(
        self,
        service_name,
        account=None,
        device=None,
        allow_credential_refresh=False,
        explicit_content=False,
        time_zone=None,
        session=None,
    ):
        self.device = device or discovery.any_soco()
        self.music_service = MusicService(service_name, device=self.device)
        self.session = session or requests.Session()
        self.account = account or self._single_configured_account()
        if self.account.service_id != int(self.music_service.service_id):
            raise MusicServiceException(
                "Account belongs to service {}, not {}".format(
                    self.account.service_id, self.music_service.service_id
                )
            )

        self.allow_credential_refresh = allow_credential_refresh
        self.explicit_content = explicit_content
        self.time_zone = time_zone or _local_time_zone()
        self.device_id = self.device.systemProperties.GetString(
            [("VariableName", "R_TrialZPSerial")]
        )["StringValue"]
        self.controller_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                "soco-music-service-browser:{}:{}".format(
                    self.device.household_id, self.device_id
                ),
            )
        )
        self._client = self._make_client(self.device.household_id)
        self._content_endpoint = self._find_content_endpoint()
        self._content_views = {}

    @classmethod
    def get_accounts(cls, device=None, timeout=8):
        """Return configured accounts without constructing a browser."""
        return ConfiguredMusicServiceAccount.get_accounts(device, timeout)

    def _single_configured_account(self):
        # Anonymous services do not need household credentials. Avoid the
        # encrypted account event entirely so this companion API remains as
        # lightweight as the existing MusicService path for those providers.
        if self.music_service.auth_type == "Anonymous":
            return ConfiguredMusicServiceAccount(
                self.music_service.service_id, 0, ""
            )

        accounts = [
            account
            for account in ConfiguredMusicServiceAccount.get_accounts(self.device)
            if account.service_id == int(self.music_service.service_id)
        ]
        if not accounts:
            raise MusicServiceAuthException(
                "No configured {} account was found in this household".format(
                    self.music_service.service_name
                )
            )
        if len(accounts) > 1:
            raise MusicServiceAuthException(
                (
                    "Multiple {} accounts are configured; "
                    "pass an account explicitly"
                ).format(self.music_service.service_name)
            )
        return accounts[0]

    def _make_client(self, household_id):
        return _ConfiguredSmapiClient(
            self.music_service,
            self.account,
            self.device,
            household_id,
            self.device_id,
            self.controller_id,
            self.time_zone,
            self.explicit_content,
            self.allow_credential_refresh,
            self.session,
        )

    def _find_content_endpoint(self):
        if not self.music_service.manifest_uri:
            return ""
        try:
            return _content_endpoint(self.music_service, self.session)
        except MusicServiceException:
            # A manifest is optional browse metadata. If it does not advertise
            # a usable browse endpoint, preserve legacy SMAPI as the fallback.
            _LOG.debug(
                "%s has no usable content browse endpoint",
                self.music_service.service_name,
                exc_info=True,
            )
            return ""

    @property
    def root_transport(self):
        """str: ``'content'`` for a manifest home page, otherwise ``'smapi'``."""
        return "content" if self._content_endpoint else "smapi"

    @property
    def available_search_categories(self):
        """Delegate search-category discovery to the existing MusicService API."""
        return self.music_service.available_search_categories

    def _content_root(self):
        content_device_id = (
            _account_content_device_id(self.device.household_id, self.account)
            if self.account.udn
            else self.device_id
        )
        headers = _content_headers(
            self.music_service,
            self.account,
            content_device_id,
            self.controller_id,
            self.time_zone,
            self.explicit_content,
        )

        for attempt in range(2):
            try:
                response = self.session.get(
                    self._content_endpoint, headers=headers, timeout=20
                )
            except requests.RequestException as error:
                raise MusicServiceException(
                    "{} content browse failed: {}".format(
                        self.music_service.service_name, error
                    )
                ) from error
            if response.status_code != 401 or attempt == 1:
                break
            if not self.allow_credential_refresh:
                raise MusicServiceAuthException(
                    "{} content browse returned HTTP 401".format(
                        self.music_service.service_name
                    )
                )
            self._client.refresh_auth_token()
            headers = _content_headers(
                self.music_service,
                self.account,
                content_device_id,
                self.controller_id,
                self.time_zone,
                self.explicit_content,
            )

        if response.status_code != 200:
            raise MusicServiceException(
                "{} content browse returned HTTP {}".format(
                    self.music_service.service_name, response.status_code
                )
            )
        try:
            page = response.json()
        except ValueError as error:
            raise MusicServiceException(
                "{} content browse returned invalid JSON".format(
                    self.music_service.service_name
                )
            ) from error
        if not isinstance(page, Mapping):
            raise MusicServiceException("Content browse root was not an object")

        sections = []
        self._content_views = {}
        for view_value in _as_list(page.get("views")):
            view = _as_mapping(view_value)
            identity = _as_mapping(view.get("id"))
            content = _as_mapping(view.get("content"))
            container = _as_mapping(content.get("container"))
            object_id = _as_string(identity.get("objectId"))
            if not container or not object_id:
                continue
            embedded = [
                normalized
                for raw_item in _as_list(view.get("items"))
                for normalized in [_content_item(raw_item)]
                if normalized is not None
            ]
            title = str(container.get("name", object_id))
            section = MusicServiceBrowseItem(
                item_id=object_id,
                title=title,
                kind="mediaCollection",
                item_type="section",
                album_art_uri=embedded[0].album_art_uri if embedded else "",
                source_transport="content-section",
                display_type=view.get("displayType", ""),
                raw=dict(view),
            )
            sections.append(section)
            self._content_views[object_id] = MusicServiceBrowseResult(
                embedded,
                index=0,
                total=int(view.get("total", len(embedded)) or len(embedded)),
                transport="content",
                requested_id=object_id,
                endpoint=self._content_endpoint,
                raw=view,
            )

        return MusicServiceBrowseResult(
            sections,
            index=0,
            total=len(sections),
            transport="content",
            requested_id="root",
            endpoint=self._content_endpoint,
            raw=page,
        )

    def get_metadata(self, item="root", index=0, count=100, recursive=False):
        """Browse a root/container using the desktop controller's transport flow.

        Passing a :class:`MusicServiceBrowseItem` from a previous result keeps
        the transport provenance required by newer providers. A plain string ID
        is treated as an ordinary legacy SMAPI ID for backwards-predictable
        behavior.
        """
        if isinstance(item, MusicServiceBrowseItem):
            object_id = item.item_id
            from_content_page = item.source_transport in (
                "content",
                "content-section",
            )
        else:
            object_id = item
            from_content_page = False

        if object_id in self._content_views:
            return self._content_views[object_id]
        if object_id in ("", "root") and self._content_endpoint:
            return self._content_root()

        client = self._scoped_client(from_content_page)
        try:
            page = client.get_metadata(object_id, index, count, recursive)
        finally:
            if client is not self._client:
                self._client.session_id = client.session_id

        source_transport = "content" if from_content_page else "smapi"
        items = [_legacy_item(record, source_transport) for record in page["items"]]
        return MusicServiceBrowseResult(
            items,
            index=page["index"],
            total=page["total"],
            transport="smapi",
            requested_id=object_id,
            raw=page,
        )

    def _scoped_client(self, force_scoped=False):
        """Return the SMAPI client for the account's OAuth device identity.

        SMAPI requests identify the account through ``loginToken.householdId``.
        The desktop controller sends the account-scoped content device identity
        here, not the bare household ID: providers such as Apple reject SMAPI
        calls made under the plain household identity with
        ``InvalidTokenException`` even though their content home page accepts
        it.  Content-session objects are always handed back to SMAPI scoped;
        search is likewise scoped whenever the account carries a UDN.  The
        shared client is returned for anonymous services, which send no token.
        """
        if force_scoped or self.account.udn:
            return self._make_client(
                _account_content_device_id(self.device.household_id, self.account)
            )
        return self._client

    def search(self, category, term="", index=0, count=100, variant="all"):
        """Search the service with the existing MusicService category mapping.

        Args:
            category (str): The search category (eg ``'artists'``). See
                :attr:`MusicService.available_search_categories`.
            term (str): The term to search for.
            index (int): The starting index. Default 0.
            count (int): The maximum number of items to return. Default 100.
            variant (str): Which search variant to use, or ``'all'`` (the
                default) to search every variant and merge the results.
                Available variants are listed per category by
                :attr:`MusicService.available_search_variants`.
        """
        # Reuse the existing MusicService presentation-map parser rather than
        # maintaining a second search-category implementation here.
        # pylint: disable=protected-access
        variants = self.music_service._get_search_variants().get(category)
        # pylint: enable=protected-access
        if variants is None:
            categories = ", ".join(
                sorted(self.music_service.available_search_categories)
            )
            raise MusicServiceException(
                "Unknown search category {!r}; available categories: {}".format(
                    category, categories
                )
            )
        if variant != "all":
            variants = [entry for entry in variants if entry[0] == variant]
            if not variants:
                available = self.music_service.available_search_variants.get(
                    category, []
                )
                raise MusicServiceException(
                    "Unknown search variant {!r} for {!r}; "
                    "available variants: {}".format(
                        variant, category, ", ".join(sorted(available))
                    )
                )

        client = self._scoped_client()
        merged_items = []
        total = 0
        raw = []
        for _variant, mapped_id in variants:
            page = client.search(mapped_id, term, index, count)
            for record in page["items"]:
                item = _legacy_item(record)
                item.variant = _variant
                merged_items.append(item)
            total += page["total"]
            raw.append(page)
        return MusicServiceBrowseResult(
            merged_items,
            index=index,
            total=total,
            transport="smapi",
            requested_id=category,
            raw=raw,
        )

    def get_media_metadata(self, item):
        """Return provider metadata for one item without changing playback."""
        object_id = item.item_id if isinstance(item, MusicServiceBrowseItem) else item
        return self._client.get_media_metadata(object_id)
