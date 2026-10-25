import datetime as dt
import json

import pytest

from daily_brief import alarm_clock as ac

MON = dt.datetime(2026, 10, 5, 6, 0, tzinfo=dt.UTC)   # a Monday


def s(**kw):
    base = {"enabled": True, "time": "06:00", "days": [0, 1, 2, 3, 4, 5, 6], "sound": "podcast"}
    return ac.parse(base | kw)


def test_defaults_when_file_missing_or_corrupt(tmp_path):
    assert ac.load(tmp_path) == ac.Setting()
    (tmp_path / "alarm.json").write_text("{not json")
    assert ac.load(tmp_path) == ac.Setting()
    assert ac.Setting().enabled is False


def test_save_load_roundtrip_and_atomic(tmp_path):
    ac.save(tmp_path, s(time="07:15", days=[1, 3], sound="spotify"))
    assert ac.load(tmp_path) == s(time="07:15", days=[1, 3], sound="spotify")
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("bad", [
    {"time": "25:00"}, {"time": "6:00"}, {"time": "06:0"}, {"days": []}, {"days": [7]}, {"days": [-1]},
    {"days": [1, 1]}, {"sound": "radio"}, {"enabled": "yes"}, {"days": "mon"}, {"days": [True]},
])
def test_parse_rejects(bad):
    with pytest.raises(ac.SettingError):
        s(**bad)


def test_parse_rejects_unknown_keys():
    with pytest.raises(ac.SettingError):
        ac.parse({"enabled": True, "time": "06:00", "days": [0], "sound": "podcast", "x": 1})


def test_due_rules():
    on = s()
    assert ac.is_due(on, MON, 10, False)
    assert ac.is_due(on, MON + dt.timedelta(minutes=9, seconds=59), 10, False)
    assert not ac.is_due(on, MON + dt.timedelta(minutes=10), 10, False)       # too late
    assert not ac.is_due(on, MON - dt.timedelta(seconds=1), 10, False)        # too early
    assert not ac.is_due(on, MON, 10, True)                                   # already fired today
    assert not ac.is_due(s(enabled=False), MON, 10, False)
    assert not ac.is_due(s(days=[1]), MON, 10, False)                         # Monday not selected


def test_next_fire():
    on = s()
    assert ac.next_fire(on, MON - dt.timedelta(hours=1)) == MON
    assert ac.next_fire(on, MON + dt.timedelta(minutes=1)) == MON + dt.timedelta(days=1)
    assert ac.next_fire(s(days=[6]), MON) == dt.datetime(2026, 10, 11, 6, 0, tzinfo=dt.UTC)  # Sunday
    assert ac.next_fire(s(enabled=False), MON) is None
    sun_late = dt.datetime(2026, 10, 11, 23, 59, tzinfo=dt.UTC)
    assert ac.next_fire(s(days=[0]), sun_late) == dt.datetime(2026, 10, 12, 6, 0, tzinfo=dt.UTC)


def test_as_dict_is_json_ready():
    json.dumps(ac.as_dict(s()))
    assert ac.as_dict(s())["days"] == [0, 1, 2, 3, 4, 5, 6]
