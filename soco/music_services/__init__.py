"""This package provides the MusicService class and related functionality,
which allows access to the various third party music services which can be used
with Sonos."""

from .music_service import MusicService
from .accounts import Account
from .browser import (
    ConfiguredMusicServiceAccount,
    MusicServiceBrowseItem,
    MusicServiceBrowseResult,
    MusicServiceBrowser,
)

__all__ = [
    "MusicService",
    "Account",
    "ConfiguredMusicServiceAccount",
    "MusicServiceBrowseItem",
    "MusicServiceBrowseResult",
    "MusicServiceBrowser",
]
