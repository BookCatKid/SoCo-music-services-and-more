'''Configured household music-service accounts and their decryption.'''

from __future__ import unicode_literals

import base64
import hashlib
import html
import logging
import os
import queue
import re
import shutil
import subprocess

from ... import discovery
from ...exceptions import MusicServiceAuthException, MusicServiceException
from ...xml import XML

_ACCOUNT_SALT = bytes.fromhex('1a01a731c96e9ebde8475182b274b70e')
_LOG = logging.getLogger(__name__)


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
