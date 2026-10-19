import pytest

from jarvis.config import VIENNA_TZ, ConfigError, load_settings


def test_env_overrides_file(tmp_path):
    f = tmp_path / ".env"
    f.write_text("# комментарий\nJARVIS_DEVICE_TOKEN=\"from-file\"\nJARVIS_PORT=9000\n", encoding="utf-8")
    s = load_settings(f, environ={"JARVIS_PORT": "9100"})
    assert s.device_token == "from-file"
    assert s.port == 9100
    assert s.tz == VIENNA_TZ


def test_missing_token_is_an_error(tmp_path):
    with pytest.raises(ConfigError, match="JARVIS_DEVICE_TOKEN"):
        load_settings(tmp_path / "nope.env", environ={})


def test_bad_port_is_an_error(tmp_path):
    with pytest.raises(ConfigError, match="JARVIS_PORT"):
        load_settings(tmp_path / "nope.env", environ={"JARVIS_DEVICE_TOKEN": "t", "JARVIS_PORT": "abc"})


def test_ha_settings_from_supervisor_or_env():
    base = {"JARVIS_DEVICE_TOKEN": "t"}
    assert load_settings(".nope", {**base}).ha_url == ""
    s = load_settings(".nope", {**base, "SUPERVISOR_TOKEN": "sv"})
    assert (s.ha_url, s.ha_token) == ("ws://supervisor/core/websocket", "sv")
    s = load_settings(".nope", {**base, "JARVIS_HA_URL": "ws://h/api/websocket", "JARVIS_HA_TOKEN": "ll"})
    assert (s.ha_url, s.ha_token) == ("ws://h/api/websocket", "ll")
