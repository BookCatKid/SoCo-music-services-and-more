"""Music-service explorer: a read-only showcase of every soco music-service feature.

This is a deliberately thin Flask app. Every route is a one-to-one demo of a
soco music-service API call - the equivalent soco snippet is shown in the UI
next to each feature so the app doubles as runnable documentation.

No playback happens anywhere in this app: browsing, searching and metadata
inspection are all read-only operations.

Run it from the repository root with the checked-out soco on the path::

    pip install -r requirements.txt
    python examples/music_service_explorer/app.py
    # open http://127.0.0.1:5050
"""

from __future__ import annotations

import os
import sys
import time

# Prefer this repository's SoCo checkout over any installed copy: the
# music-service features this app demonstrates (MusicServiceBrowser, search
# variants, configured-account browsing) only exist on the music-services
# branch. This makes `python examples/music_service_explorer/app.py` work
# from anywhere without PYTHONPATH gymnastics.
_REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from flask import Flask, jsonify, render_template, request  # noqa: E402

from soco.discovery import any_soco  # noqa: E402
from soco.exceptions import MusicServiceAuthException  # noqa: E402
from soco.music_services import (  # noqa: E402
    ConfiguredMusicServiceAccount,
    MusicService,
    MusicServiceBrowser,
    MusicServiceBrowseItem,
)

app = Flask(__name__)

# The music services data is fetched once from the speaker and cached for a
# few minutes - calling ListAvailableServices on every page view is wasteful.
SERVICES_TTL = 300  # seconds


def _device():
    """The speaker that drives the app (a SoCo instance), cached briefly.

    soco caches the first discovery result per process, so a fresh SSDP scan
    is forced only when the cached speaker has gone stale (or was missing
    because the scan raced the first request).
    """
    key = "_device"
    now = time.time()
    cached = getattr(app, key, None)
    if cached and now - cached[1] < SERVICES_TTL:
        return cached[0]
    device = any_soco(allow_network_scan=True)
    if device is None:
        raise RuntimeError("No Sonos speaker found on the network")
    setattr(app, key, (device, now))
    return device


def _service_data():
    """The raw ListAvailableServices descriptor data, cached briefly."""
    key = "_services_data"
    now = time.time()
    cached = getattr(app, key, None)
    if cached and now - cached[1] < SERVICES_TTL:
        return cached[0]
    data = MusicService._get_music_services_data()  # pylint: disable=protected-access
    setattr(app, key, (data, now))
    return data


def _get_service(name):
    """A legacy MusicService instance for a named service."""
    return MusicService(name, device=_device())


def _get_browser(name):
    """A MusicServiceBrowser (account-aware) for a named service.

    This uses the account credentials Sonos already stores for the household,
    so search/browse work for authenticated services without any token dance.
    The browser is cached because constructing it performs an account event
    capture and a manifest fetch.
    """
    cache = getattr(app, "_browsers", {})
    if name in cache:
        return cache[name]
    browser = MusicServiceBrowser(name, device=_device(), allow_credential_refresh=True)
    cache[name] = browser
    # A demo app doesn't need to hold every service's browser forever.
    if len(cache) > 8:
        cache.pop(next(iter(cache)))
    app._browsers = cache
    return browser


# ---------------------------------------------------------------------------
# Feature: legacy MusicService search categories/variants
# ---------------------------------------------------------------------------

def _search_categories(name):
    service = _get_service(name)
    return {
        "categories": service.available_search_categories,
        "variants": service.available_search_variants,
    }


def _legacy_search(name, category, term, variant, index, count):
    service = _get_service(name)
    result = service.search(category, term, index=index, count=count, variant=variant)
    return {
        "items": [
            {
                "id": item.item_id,
                "title": item.title,
                "type": item.metadata.get("item_type", ""),
                "artist": item.metadata.get("artist", ""),
            }
            for item in result
        ],
        "number_returned": result.number_returned,
        "total_matches": result.total_matches,
    }


def _browser_search(name, category, term, variant, index, count):
    browser = _get_browser(name)
    result = browser.search(category, term, index=index, count=count, variant=variant)
    return {
        "items": [
            {
                "id": item.item_id,
                "title": item.title,
                "type": item.item_type,
                "artist": item.artist,
                "variant": item.variant,
                "can_browse": item.can_browse,
            }
            for item in result.items
        ],
        "number_returned": len(result.items),
        "total_matches": result.total,
    }


# ---------------------------------------------------------------------------
# Feature: browse (get_metadata)
# ---------------------------------------------------------------------------

def _legacy_browse(name, item_id, index, count, recursive):
    service = _get_service(name)
    result = service.get_metadata(
        item_id, index=index, count=count, recursive=recursive
    )
    return {
        "items": [
            {
                "id": item.item_id,
                "title": item.title,
                "type": item.metadata.get("item_type", ""),
                "artist": item.metadata.get("artist", ""),
                "can_browse": item.metadata.get("can_enumerate") == "true"
                or "collection" in item.metadata.get("item_type", "").lower(),
            }
            for item in result
        ],
        "number_returned": result.number_returned,
        "total_matches": result.total_matches,
    }


def _browser_browse(name, item_id, index, count, recursive):
    browser = _get_browser(name)
    # Content-session children must be handed back to SMAPI with the account's
    # OAuth device identity. The browser only does that when it receives a
    # MusicServiceBrowseItem with a content source_transport, so rebuild the
    # wrapper from the transport the frontend carried over.
    transport = request.args.get("transport", "")
    if transport in ("content", "content-section"):
        item = MusicServiceBrowseItem(
            item_id,
            "",
            "mediaCollection",
            source_transport=transport,
        )
        result = browser.get_metadata(
            item, index=index, count=count, recursive=recursive
        )
    else:
        result = browser.get_metadata(
            item_id, index=index, count=count, recursive=recursive
        )
    return {
        "items": [
            {
                "id": item.item_id,
                "title": item.title,
                "type": item.item_type,
                "artist": item.artist,
                "variant": item.variant,
                "can_browse": item.can_browse,
                "transport": item.source_transport,
            }
            for item in result.items
        ],
        "number_returned": len(result.items),
        "total_matches": result.total,
    }


# ---------------------------------------------------------------------------
# Feature: per-item metadata
# ---------------------------------------------------------------------------

def _legacy_media_metadata(name, item_id):
    service = _get_service(name)
    return {"metadata": service.get_media_metadata(item_id)}


def _browser_media_metadata(name, item_id):
    browser = _get_browser(name)
    return {"metadata": browser.get_media_metadata(item_id)}


# ---------------------------------------------------------------------------
# Feature: misc read-only service calls
# ---------------------------------------------------------------------------

def _misc_service_info(name):
    service = _get_service(name)
    return {
        "service_name": service.service_name,
        "service_id": service.service_id,
        "service_type": service.service_type,
        "auth_type": service.auth_type,
        "capabilities": service.capabilities,
        "version": service.version,
        "container_type": service.container_type,
        "uri": service.uri,
        "secure_uri": service.secure_uri,
        "presentation_map_uri": service.presentation_map_uri,
        "manifest_uri": service.manifest_uri,
        "desc": service.desc,
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    try:
        device = _device()
        services = [
            {
                "name": name,
                "id": data["Id"],
                "auth": data["Auth"],
                "capabilities": data["Capabilities"],
                "service_type": data["ServiceType"],
                "container_type": data["ContainerType"],
            }
            for name, data in sorted(
                ((d["Name"], d) for d in _service_data().values()), key=lambda x: x[0]
            )
        ]
        # Account capture subscribes to a ZoneGroupTopology event and can fail
        # or time out in some households. Isolate it so the service catalog
        # still renders when accounts are unavailable.
        try:
            raw_accounts = ConfiguredMusicServiceAccount.get_accounts(
                device, timeout=10
            )
        except Exception:  # pylint: disable=broad-except
            raw_accounts = []
        # Some auto-added accounts (eg TuneIn) have no per-account token UDN,
        # so account_uid can't be derived for them. Show what we can and skip
        # only those entries rather than failing the whole page.
        accounts = []
        for account in raw_accounts:
            try:
                account_uid = hex(account.account_uid)
            except Exception:  # pylint: disable=broad-except
                account_uid = "n/a"
            accounts.append(
                {
                    "service_id": account.service_id,
                    "nickname": account.nickname,
                    "tier": account.tier,
                    "username": account.username,
                    "account_uid": account_uid,
                }
            )
        # Services that have an account actually added to this household, so
        # the sidebar can filter to "accounts added to our system" only.
        # Service ids are strings in the descriptor and ints on the account,
        # so normalize before comparing.
        configured_service_ids = {str(a["service_id"]) for a in accounts}
        return render_template(
            "index.html",
            device={
                "name": device.player_name,
                "ip": device.ip_address,
                "uid": device.uid,
                "household_id": device.household_id,
            },
            services=services,
            accounts=accounts,
            configured_service_ids=configured_service_ids,
        )
    except Exception as error:  # pylint: disable=broad-except
        return render_template("index.html", error=str(error), services=[], accounts=[])


@app.route("/api/categories")
def api_categories():
    """Search categories + variants for one service (legacy API)."""
    try:
        return jsonify(_search_categories(request.args.get("service", "")))
    except Exception as error:  # pylint: disable=broad-except
        return jsonify({"error": str(error)}), 500


@app.route("/api/search")
def api_search():
    """Search one service; demonstrates legacy and browser APIs side by side."""
    name = request.args.get("service", "")
    category = request.args.get("category", "")
    term = request.args.get("term", "")
    variant = request.args.get("variant", "all")
    api = request.args.get("api", "legacy")
    index = int(request.args.get("index", 0))
    count = int(request.args.get("count", 20))
    try:
        if api == "browser":
            data = _browser_search(name, category, term, variant, index, count)
        else:
            data = _legacy_search(name, category, term, variant, index, count)
        return jsonify(data)
    except Exception as error:  # pylint: disable=broad-except
        return jsonify({"error": str(error)}), 500


@app.route("/api/browse")
def api_browse():
    """Browse a container; both APIs, with recursive + paging options."""
    name = request.args.get("service", "")
    item_id = request.args.get("item", "root")
    api = request.args.get("api", "legacy")
    index = int(request.args.get("index", 0))
    count = int(request.args.get("count", 20))
    recursive = request.args.get("recursive", "0") == "1"
    try:
        if api == "browser":
            data = _browser_browse(name, item_id, index, count, recursive)
        else:
            data = _legacy_browse(name, item_id, index, count, recursive)
        return jsonify(data)
    except Exception as error:  # pylint: disable=broad-except
        return jsonify({"error": str(error)}), 500


@app.route("/api/metadata")
def api_metadata():
    """get_media_metadata for one item, on both APIs."""
    name = request.args.get("service", "")
    item_id = request.args.get("item", "")
    api = request.args.get("api", "legacy")
    try:
        if api == "browser":
            data = _browser_media_metadata(name, item_id)
        else:
            data = _legacy_media_metadata(name, item_id)
        return jsonify(data)
    except MusicServiceAuthException as error:
        # Token-based providers (eg Apple) refuse some metadata calls under
        # the shared client; the legacy path is the reliable fallback here.
        payload = {"error": str(error), "hint": "try the legacy API"}
        return jsonify(payload), 401
    except Exception as error:  # pylint: disable=broad-except
        return jsonify({"error": str(error)}), 500


@app.route("/api/info")
def api_info():
    """Everything soco exposes about a service descriptor."""
    try:
        return jsonify(_misc_service_info(request.args.get("service", "")))
    except Exception as error:  # pylint: disable=broad-except
        return jsonify({"error": str(error)}), 500


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5050, debug=False)
