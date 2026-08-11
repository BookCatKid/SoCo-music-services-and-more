"""Tests for the snapshot module."""

from unittest import mock
from unittest.mock import MagicMock, call, patch

from soco.data_structures import DidlMusicTrack, DidlResource
from soco.snapshot import GroupSnapshot, Snapshot


def make_track(uri, protocol_info="http-get:*:audio/mpeg:*"):
    """Create a DidlMusicTrack with a resource URI, as returned by get_queue."""
    resource = DidlResource(uri=uri, protocol_info=protocol_info)
    return DidlMusicTrack(
        title="Test Track",
        parent_id="Q:0",
        item_id="Q:0/1",
        resources=[resource],
    )


def test_group_snapshot_single_zone_no_grouping(moco):
    """Standalone zones are not recorded as groups."""
    standalone = MagicMock()
    group = make_group(standalone, [])

    zgs = MagicMock()
    zgs.groups = {"g1": group}
    zgs.visible_zones = {standalone}

    with patch(
        "soco.core.SoCo.zone_group_state",
        new_callable=mock.PropertyMock,
        return_value=zgs,
    ), patch("soco.snapshot.Snapshot"):
        snap = GroupSnapshot(moco)
        snap.snapshot()

    assert snap._groups == []


def test_restore_queue_calls_add_uri_to_queue(moco):
    """_restore_queue adds each queue item's URI via add_uri_to_queue."""
    track1 = make_track("x-file-cifs://nas/music/a.mp3")
    track2 = make_track("http://192.168.1.50/music/b.mp3")

    snap = Snapshot(moco, snapshot_queue=True)
    snap.queue = [[track1, track2]]

    moco.add_uri_to_queue = MagicMock()
    snap._restore_queue()

    moco.add_uri_to_queue.assert_has_calls(
        [
            call("x-file-cifs://nas/music/a.mp3"),
            call("http://192.168.1.50/music/b.mp3"),
        ]
    )


def test_restore_queue_http_uri(moco):
    """Tracks added via HTTP (e.g. WebDAV) are correctly restored (issue #983).

    DidlMusicTrack has no direct .uri attribute; the URI lives in resources[0].
    get_uri() must be used instead.
    """
    http_track = make_track("http://192.168.1.50/share/song.mp3")
    assert not hasattr(http_track, "uri"), "DidlMusicTrack should not have .uri"
    assert http_track.get_uri() == "http://192.168.1.50/share/song.mp3"

    snap = Snapshot(moco, snapshot_queue=True)
    snap.queue = [[http_track]]

    moco.add_uri_to_queue = MagicMock()
    snap._restore_queue()

    moco.add_uri_to_queue.assert_called_once_with("http://192.168.1.50/share/song.mp3")


def make_group(coordinator, members):
    """Build a fake ZoneGroup-like object with coordinator and members."""
    group = MagicMock()
    group.coordinator = coordinator
    group.members = [coordinator] + members
    return group


def test_group_snapshot_records_groups(moco):
    """GroupSnapshot.snapshot records the household groups and per-zone state."""
    coordinator = MagicMock()
    member = MagicMock()
    group = make_group(coordinator, [member])

    zgs = MagicMock()
    zgs.groups = {"g1": group}
    zgs.visible_zones = {coordinator, member}

    with patch(
        "soco.core.SoCo.zone_group_state",
        new_callable=mock.PropertyMock,
        return_value=zgs,
    ), patch("soco.snapshot.Snapshot") as snap_cls:
        snap = GroupSnapshot(moco)
        snap.snapshot()

    assert snap._groups == [(coordinator, [coordinator, member])]
    assert set(snap._snapshots) == {coordinator, member}
    # Snapshot was created for each zone
    assert snap_cls.call_count == 2
    snap_cls.assert_any_call(coordinator, snapshot_queue=False)
    snap_cls.assert_any_call(member, snapshot_queue=False)


def test_group_snapshot_restore_rejoins_groups(moco):
    """GroupSnapshot.restore re-forms the recorded groups before restoring."""
    coordinator = MagicMock()
    member = MagicMock()
    group = make_group(coordinator, [member])

    zgs = MagicMock()
    zgs.groups = {"g1": group}
    zgs.visible_zones = {coordinator, member}

    with patch(
        "soco.core.SoCo.zone_group_state",
        new_callable=mock.PropertyMock,
        return_value=zgs,
    ), patch("soco.snapshot.Snapshot") as snap_cls:
        snap = GroupSnapshot(moco)
        snap.snapshot()
        snap.restore()

    member.join.assert_called_once_with(coordinator)
    coordinator.join.assert_not_called()
    # Each zone's snapshot is restored
    assert snap_cls.return_value.restore.call_count == 2


def test_group_snapshot_context_manager(moco):
    """GroupSnapshot can be used as a context manager."""
    coordinator = MagicMock()
    member = MagicMock()
    group = make_group(coordinator, [member])

    zgs = MagicMock()
    zgs.groups = {"g1": group}
    zgs.visible_zones = {coordinator, member}

    with patch(
        "soco.core.SoCo.zone_group_state",
        new_callable=mock.PropertyMock,
        return_value=zgs,
    ), patch("soco.snapshot.Snapshot") as snap_cls:
        with GroupSnapshot(moco):
            # enter ran snapshot() for every zone
            assert snap_cls.call_count == 2
        # exit restored every zone
        assert snap_cls.return_value.restore.call_count == 2


def test_restore_queue_skipped_when_none(moco):
    """_restore_queue does nothing when queue was not snapshotted."""
    snap = Snapshot(moco, snapshot_queue=False)
    moco.add_uri_to_queue = MagicMock()
    snap._restore_queue()
    moco.add_uri_to_queue.assert_not_called()
