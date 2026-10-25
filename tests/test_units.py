from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_no_daily_autoplay_timer():
    assert not (ROOT / "systemd" / "brief-show.timer").exists()
    install = (ROOT / "bin" / "install-units").read_text()
    enable = [line for line in install.splitlines() if "enable --now" in line]
    assert enable and all("brief-show.timer" not in line for line in enable)


def test_units_use_the_app_dir_placeholder_not_a_fixed_path():
    for unit in (ROOT / "systemd").iterdir():
        text = unit.read_text()
        assert "/home/" not in text
        if unit.suffix == ".service":
            assert "@APP_DIR@" in text


def test_alarm_timer_ticks_every_minute_and_service_runs_the_tick():
    timer = (ROOT / "systemd" / "brief-alarm.timer").read_text()
    assert "OnCalendar=*-*-* *:*:00" in timer and "Persistent=false" in timer
    service = (ROOT / "systemd" / "brief-alarm.service").read_text()
    assert "daily_brief.alarm_tick" in service and "Type=oneshot" in service


def test_show_service_runs_in_alarm_mode_only():
    service = (ROOT / "systemd" / "brief-show.service").read_text()
    assert "daily_brief.show" in service and "--alarm" in service


def test_build_timer_hourly():
    timer = (ROOT / "systemd" / "brief-build.timer").read_text()
    cal = [ln for ln in timer.splitlines() if ln.startswith("OnCalendar=")]
    assert cal == ["OnCalendar=hourly"]  # any hour will do; the ready queue is the backup
    assert "RandomizedDelaySec=300" in timer and "Persistent=true" in timer
    assert "WantedBy=timers.target" in timer


def test_build_service_runs_the_builder_once_without_a_network_wait():
    service = (ROOT / "systemd" / "brief-build.service").read_text()
    assert "daily_brief.build" in service and "Type=oneshot" in service
    assert not [ln for ln in service.splitlines() if ln.startswith(("Wants=", "After=")) and "network-online" in ln]
    assert "HF_HUB_OFFLINE=1" in service


def test_install_enables_the_timers_and_api():
    install = (ROOT / "bin" / "install-units").read_text()
    enable = [ln for ln in install.splitlines() if "enable --now" in ln]
    assert any("brief-build.timer" in ln and "brief-alarm.timer" in ln and "brief-api.service" in ln for ln in enable)


def test_night_and_prefetch_units_are_gone_from_the_repo():
    for name in ("brief-night", "brief-prefetch"):
        assert not list((ROOT / "systemd").glob(f"{name}.*"))
