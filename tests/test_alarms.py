"""Tests for the alarms module."""

from datetime import datetime, time, timedelta
from unittest.mock import MagicMock, patch, PropertyMock

import pytest
from soco.alarms import (
    Alarm,
    Alarms,
    get_time_format,
    get_time_now,
    get_time_server,
    get_time_zone,
    get_time_zone_rule,
    is_valid_recurrence,
    set_time_format,
    set_time_server,
    set_time_zone,
)
from soco.core import _ArgsSingleton
from soco.exceptions import SoCoException, SoCoUPnPException


@pytest.fixture(autouse=True)
def reset_alarms_singleton():
    """Reset the Alarms singleton between tests to prevent state leakage."""
    _ArgsSingleton._instances.pop("Alarms", None)
    yield
    _ArgsSingleton._instances.pop("Alarms", None)


def test_recurrence():
    for recur in ("DAILY", "WEEKDAYS", "WEEKENDS", "ONCE"):
        assert is_valid_recurrence(recur)

    assert is_valid_recurrence("ON_1")
    assert is_valid_recurrence("ON_123412")
    assert not is_valid_recurrence("on_1")
    assert not is_valid_recurrence("ON_123456789")
    assert not is_valid_recurrence("ON_")
    assert not is_valid_recurrence(" ON_1")


def test_alarms(moco):
    """Test loading and processing of alarms for an existing zone."""
    alarm_list_response = {
        "CurrentAlarmListVersion": "RINCON_test:14",
        "CurrentAlarmList": "<Alarms>"
        '<Alarm ID="14" StartTime="07:00:00" Duration="02:00:00" Recurrence="DAILY" '
        'Enabled="1" RoomUUID="RINCON_test" ProgramURI="x-rincon-buzzer:0" '
        'ProgramMetaData="" PlayMode="SHUFFLE_NOREPEAT" Volume="25" '
        'IncludeLinkedZones="0"/>'
        "</Alarms>",
    }
    moco.alarmClock.ListAlarms = MagicMock(return_value=alarm_list_response)
    # Create a mock zone with the correct uid
    mock_zone = MagicMock()
    mock_zone.uid = "RINCON_test"
    with patch.object(
        type(moco), "all_zones", new_callable=PropertyMock
    ) as mock_all_zones:
        mock_all_zones.return_value = [mock_zone]
        alarms = Alarms()
        alarms.update(moco)

    assert len(alarms.alarms) == 1
    assert len(alarms.alarms_skipped) == 0
    alarm = alarms.alarms["14"]
    assert alarm.zone == mock_zone
    assert alarm.start_time == time(7, 0, 0)
    assert alarm.duration == time(2, 0, 0)
    assert alarm.recurrence == "DAILY"
    assert alarm.enabled is True
    assert alarm.program_uri is None  # x-rincon-buzzer:0 is mapped to None in the code
    assert alarm.program_metadata == ""
    assert alarm.play_mode == "SHUFFLE_NOREPEAT"
    assert int(alarm.volume) == 25
    assert alarm.include_linked_zones is False
    assert alarm.room_uuid == "RINCON_test"


def test_alarms_skipped(moco):
    """Test loading and processing of alarms for a missing zone."""
    alarm_list_response = {
        "CurrentAlarmListVersion": "RINCON_test:14",
        "CurrentAlarmList": "<Alarms>"
        '<Alarm ID="14" StartTime="07:00:00" Duration="02:00:00" Recurrence="DAILY" '
        'Enabled="1" RoomUUID="RINCON_test_missing" ProgramURI="x-rincon-buzzer:0" '
        'ProgramMetaData="" PlayMode="SHUFFLE_NOREPEAT" Volume="25" '
        'IncludeLinkedZones="0"/>'
        "</Alarms>",
    }
    moco.alarmClock.ListAlarms = MagicMock(return_value=alarm_list_response)
    # Create a mock zone that does not match the RoomUUID in the alarm
    mock_zone = MagicMock()
    mock_zone.uid = "RINCON_test"
    with patch.object(
        type(moco), "all_zones", new_callable=PropertyMock
    ) as mock_all_zones:
        mock_all_zones.return_value = [mock_zone]
        alarms = Alarms()
        alarms.update(moco)

    # Verify that the alarm is skipped due to missing zone and stored in alarms_skipped
    assert len(alarms.alarms) == 0
    assert len(alarms.alarms_skipped) == 1
    alarm = alarms.alarms_skipped["14"]
    assert alarm.zone is None
    assert alarm.start_time == time(7, 0, 0)
    assert alarm.duration == time(2, 0, 0)
    assert alarm.recurrence == "DAILY"
    assert alarm.enabled is True
    assert alarm.program_uri is None  # x-rincon-buzzer:0 is mapped to None in the code
    assert alarm.program_metadata == ""
    assert alarm.play_mode == "SHUFFLE_NOREPEAT"
    assert int(alarm.volume) == 25
    assert alarm.include_linked_zones is False
    assert alarm.room_uuid == "RINCON_test_missing"

    # Add the missing zone and update skipped alarms
    mock_missing_zone = MagicMock()
    mock_missing_zone.uid = "RINCON_test_missing"
    alarms.update_skipped(mock_missing_zone)
    assert len(alarms.alarms) == 1
    assert len(alarms.alarms_skipped) == 0
    alarm = alarms.alarms["14"]
    assert alarm.zone == mock_missing_zone


def test_alarms_skipped_reuse_object_on_update(moco):
    """Verify that a skipped alarm's existing object is reused when update() is
    called again and the zone is now available, preserving object identity."""
    missing_uuid = "RINCON_test_missing"
    alarm_list_response = {
        "CurrentAlarmListVersion": "RINCON_test:14",
        "CurrentAlarmList": "<Alarms>"
        '<Alarm ID="14" StartTime="07:00:00" Duration="02:00:00" Recurrence="DAILY" '
        'Enabled="1" RoomUUID="{}" ProgramURI="x-rincon-buzzer:0" '
        'ProgramMetaData="" PlayMode="SHUFFLE_NOREPEAT" Volume="25" '
        'IncludeLinkedZones="0"/>'.format(missing_uuid) + "</Alarms>",
    }
    mock_present_zone = MagicMock()
    mock_present_zone.uid = "RINCON_test"
    mock_missing_zone = MagicMock()
    mock_missing_zone.uid = missing_uuid

    moco.alarmClock.ListAlarms = MagicMock(return_value=alarm_list_response)

    # First update: zone is missing, alarm goes to alarms_skipped
    with patch.object(
        type(moco), "all_zones", new_callable=PropertyMock
    ) as mock_all_zones:
        mock_all_zones.return_value = [mock_present_zone]
        alarms = Alarms()
        alarms.update(moco)

    assert len(alarms.alarms_skipped) == 1
    skipped_alarm_obj = alarms.alarms_skipped["14"]

    # Second update: version is higher, zone is now present
    alarm_list_response_v2 = dict(alarm_list_response)
    alarm_list_response_v2["CurrentAlarmListVersion"] = "RINCON_test:15"
    moco.alarmClock.ListAlarms = MagicMock(return_value=alarm_list_response_v2)

    with patch.object(
        type(moco), "all_zones", new_callable=PropertyMock
    ) as mock_all_zones:
        mock_all_zones.return_value = [mock_present_zone, mock_missing_zone]
        alarms.update(moco)

    assert len(alarms.alarms) == 1
    assert len(alarms.alarms_skipped) == 0
    resolved_alarm = alarms.alarms["14"]
    # The same object should have been updated in place, not replaced
    assert resolved_alarm is skipped_alarm_obj
    assert resolved_alarm.zone == mock_missing_zone


def test_save_raises_when_zone_is_none(moco):
    """Verify that save() raises SoCoException when zone is None."""
    alarm = Alarm(zone=None, room_uuid="RINCON_test_missing")
    alarm._alarm_id = None  # pylint: disable=protected-access
    with pytest.raises(SoCoException, match="zone is not set"):
        alarm.save()


# --- Alarm runtime actions (S2 AVTransport: SnoozeAlarm / RunAlarm / Stop) ---


def test_alarm_snooze_default(moco):
    """Snoozing a ringing alarm uses the default 9 minute duration."""
    alarm = Alarm(moco)
    alarm.snooze()
    moco.avTransport.SnoozeAlarm.assert_called_once_with(
        [("InstanceID", 0), ("Duration", "0:09:00")]
    )


def test_alarm_snooze_minutes_int(moco):
    """An int snooze duration is interpreted as minutes."""
    alarm = Alarm(moco)
    alarm.snooze(15)
    moco.avTransport.SnoozeAlarm.assert_called_once_with(
        [("InstanceID", 0), ("Duration", "0:15:00")]
    )


def test_alarm_snooze_timedelta(moco):
    """A timedelta snooze duration is formatted as H:MM:SS."""
    alarm = Alarm(moco)
    alarm.snooze(timedelta(hours=1, minutes=30))
    moco.avTransport.SnoozeAlarm.assert_called_once_with(
        [("InstanceID", 0), ("Duration", "1:30:00")]
    )


def test_alarm_snooze_raises_without_zone():
    """Snoozing a skipped alarm raises instead of crashing."""
    alarm = Alarm(zone=None, room_uuid="RINCON_test_missing")
    with pytest.raises(SoCoException, match="zone is not set"):
        alarm.snooze()


def test_alarm_play(moco):
    """play() previews the alarm's program via normal playback."""
    alarm = Alarm(
        moco,
        program_uri="x-rincon-radio:RINCON_abc",
        program_metadata="<DIDL-Lite/>",
    )
    moco.play_uri = MagicMock()
    alarm.play()
    moco.play_uri.assert_called_once_with(
        "x-rincon-radio:RINCON_abc", meta="<DIDL-Lite/>", title="Alarm preview"
    )


def test_alarm_play_buzzer_defaults(moco):
    """play() falls back to the built-in chime for alarms without a program."""
    alarm = Alarm(moco)
    moco.play_uri = MagicMock()
    alarm.play()
    moco.play_uri.assert_called_once_with(
        "x-rincon-buzzer:0", meta="", title="Alarm preview"
    )


def test_alarm_play_raises_without_zone():
    """play() on a skipped alarm raises instead of crashing."""
    alarm = Alarm(zone=None, room_uuid="RINCON_test_missing")
    with pytest.raises(SoCoException, match="zone is not set"):
        alarm.play()


def test_alarm_dismiss(moco):
    """dismiss() stops the ringing alarm's playback."""
    alarm = Alarm(moco)
    alarm._alarm_id = "471"  # pylint: disable=protected-access
    moco.avTransport.GetRunningAlarmProperties.return_value = {"AlarmID": "471"}
    alarm.dismiss()
    moco.avTransport.Stop.assert_called_once_with([("InstanceID", 0)])


def test_alarm_dismiss_raises_when_not_ringing(moco):
    """dismiss() refuses when the alarm is not the one ringing."""
    alarm = Alarm(moco)
    alarm._alarm_id = "471"  # pylint: disable=protected-access
    moco.avTransport.GetRunningAlarmProperties.side_effect = SoCoUPnPException(
        "no alarm", "800", "<xml/>"
    )
    with pytest.raises(SoCoException, match="not currently ringing"):
        alarm.dismiss()
    moco.avTransport.Stop.assert_not_called()


def test_alarm_dismiss_raises_when_other_alarm_ringing(moco):
    """dismiss() refuses when a different alarm is ringing."""
    alarm = Alarm(moco)
    alarm._alarm_id = "471"  # pylint: disable=protected-access
    moco.avTransport.GetRunningAlarmProperties.return_value = {"AlarmID": "999"}
    with pytest.raises(SoCoException, match="not currently ringing"):
        alarm.dismiss()


# --- Alarms: running-alarm queries ---


def test_is_alarm_running_true(moco):
    """is_alarm_running() is True while an alarm is ringing."""
    moco.avTransport.GetRunningAlarmProperties.return_value = {"AlarmID": "471"}
    assert Alarms().is_alarm_running(moco) is True


def test_is_alarm_running_false_when_idle(moco):
    """is_alarm_running() is False when the speaker is idle (UPnP 800)."""
    moco.avTransport.GetRunningAlarmProperties.side_effect = SoCoUPnPException(
        "no alarm", "800", "<xml/>"
    )
    assert Alarms().is_alarm_running(moco) is False


def test_get_running_alarm_returns_cached_alarm(moco):
    """get_running_alarm() resolves the ringing alarm from the alarm list."""
    alarm_list_response = {
        "CurrentAlarmListVersion": "RINCON_test:14",
        "CurrentAlarmList": "<Alarms>"
        '<Alarm ID="14" StartTime="07:00:00" Duration="02:00:00" Recurrence="DAILY" '
        'Enabled="1" RoomUUID="RINCON_test" ProgramURI="x-rincon-buzzer:0" '
        'ProgramMetaData="" PlayMode="SHUFFLE_NOREPEAT" Volume="25" '
        'IncludeLinkedZones="0"/>'
        "</Alarms>",
    }
    moco.alarmClock.ListAlarms = MagicMock(return_value=alarm_list_response)
    mock_zone = MagicMock()
    mock_zone.uid = "RINCON_test"
    with patch.object(
        type(moco), "all_zones", new_callable=PropertyMock
    ) as mock_all_zones:
        mock_all_zones.return_value = [mock_zone]
        moco.avTransport.GetRunningAlarmProperties.return_value = {"AlarmID": "14"}
        alarms = Alarms()
        running = alarms.get_running_alarm(moco)

    assert running is alarms.alarms["14"]
    assert running.alarm_id == "14"


def test_get_running_alarm_lightweight_when_not_in_list(moco):
    """An unknown ringing alarm still yields a usable Alarm object."""
    moco.alarmClock.ListAlarms = MagicMock(
        return_value={
            "CurrentAlarmListVersion": "RINCON_test:14",
            "CurrentAlarmList": "<Alarms></Alarms>",
        }
    )
    moco.avTransport.GetRunningAlarmProperties.return_value = {"AlarmID": "471"}
    running = Alarms().get_running_alarm(moco)
    assert running.alarm_id == "471"
    assert running.zone is moco


def test_get_running_alarm_none_when_idle(moco):
    """get_running_alarm() returns None when no alarm is ringing."""
    moco.avTransport.GetRunningAlarmProperties.side_effect = SoCoUPnPException(
        "no alarm", "800", "<xml/>"
    )
    assert Alarms().get_running_alarm(moco) is None


# --- Clock and time settings (AlarmClock service) ---


def test_get_time_format(moco):
    moco.alarmClock.GetFormat = MagicMock(
        return_value={"CurrentTimeFormat": "24H", "CurrentDateFormat": "NO_DF"}
    )
    assert get_time_format(moco) == {"time_format": "24H", "date_format": "NO_DF"}


def test_set_time_format(moco):
    moco.alarmClock.SetFormat = MagicMock()
    set_time_format(moco, "12H", "DD/MM/YYYY")
    moco.alarmClock.SetFormat.assert_called_once_with(
        [("DesiredTimeFormat", "12H"), ("DesiredDateFormat", "DD/MM/YYYY")]
    )


def test_get_time_zone(moco):
    moco.alarmClock.GetTimeZone = MagicMock(
        return_value={"Index": "4", "AutoAdjustDst": "1"}
    )
    assert get_time_zone(moco) == {"index": 4, "auto_adjust_dst": True}


def test_set_time_zone(moco):
    moco.alarmClock.SetTimeZone = MagicMock()
    set_time_zone(moco, index=4, auto_adjust_dst=False)
    moco.alarmClock.SetTimeZone.assert_called_once_with(
        [("Index", 4), ("AutoAdjustDst", "0")]
    )


def test_set_time_zone_keeps_current_index(moco):
    moco.alarmClock.GetTimeZone = MagicMock(
        return_value={"Index": "7", "AutoAdjustDst": "0"}
    )
    moco.alarmClock.SetTimeZone = MagicMock()
    set_time_zone(moco)
    moco.alarmClock.SetTimeZone.assert_called_once_with(
        [("Index", 7), ("AutoAdjustDst", "1")]
    )


def test_get_time_now(moco):
    moco.alarmClock.GetTimeNow = MagicMock(
        return_value={
            "CurrentUTCTime": "2026-08-11 22:25:15",
            "CurrentLocalTime": "2026-08-11 15:25:15",
            "CurrentTimeZone": "01e00b00",
            "CurrentTimeGeneration": "20000001",
        }
    )
    result = get_time_now(moco)
    assert result["utc_time"] == datetime(2026, 8, 11, 22, 25, 15)
    assert result["local_time"] == datetime(2026, 8, 11, 15, 25, 15)
    assert result["time_zone"] == "01e00b00"
    assert result["generation"] == "20000001"


def test_get_time_server(moco):
    moco.alarmClock.GetTimeServer = MagicMock(
        return_value={"CurrentTimeServer": "0.sonostime.pool.ntp.org"}
    )
    assert get_time_server(moco) == "0.sonostime.pool.ntp.org"


def test_set_time_server(moco):
    moco.alarmClock.SetTimeServer = MagicMock()
    set_time_server(moco, "time.apple.com")
    moco.alarmClock.SetTimeServer.assert_called_once_with(
        [("DesiredTimeServer", "time.apple.com")]
    )


def test_get_time_zone_rule(moco):
    moco.alarmClock.GetTimeZone = MagicMock(
        return_value={"Index": "4", "AutoAdjustDst": "1"}
    )
    moco.alarmClock.GetTimeZoneRule = MagicMock(
        return_value={"TimeZone": "02d000000000000000000000ffc4"}
    )
    assert get_time_zone_rule(moco) == "02d000000000000000000000ffc4"
    moco.alarmClock.GetTimeZoneRule.assert_called_once_with([("Index", 4)])
