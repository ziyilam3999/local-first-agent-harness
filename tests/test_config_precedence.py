"""Config precedence: one-name-one-file (override > SSOT > floor).

Guards the 2026-06-02 confound where the engine's per-role timeout default (900s) disagreed with the
cc-models.env SSOT (1800s) and a launcher set a typo'd env name the engine ignored -> the run silently
ran at half the agreed cap. The fix routes each SSOT-backed knob through `_cfg(override, ssot, floor)`.
"""
import lfah.relay as relay


def test_cfg_override_wins(monkeypatch):
    monkeypatch.setenv("OVERRIDE_X", "over")
    monkeypatch.setenv("SSOT_X", "ssot")
    assert relay._cfg("OVERRIDE_X", "SSOT_X", default="floor") == "over"


def test_cfg_ssot_when_no_override(monkeypatch):
    monkeypatch.delenv("OVERRIDE_X", raising=False)
    monkeypatch.setenv("SSOT_X", "ssot")
    assert relay._cfg("OVERRIDE_X", "SSOT_X", default="floor") == "ssot"


def test_cfg_floor_when_neither(monkeypatch):
    monkeypatch.delenv("OVERRIDE_X", raising=False)
    monkeypatch.delenv("SSOT_X", raising=False)
    assert relay._cfg("OVERRIDE_X", "SSOT_X", default="floor") == "floor"


def test_cfg_blank_and_whitespace_are_skipped(monkeypatch):
    # an env var present-but-empty (or whitespace) must NOT shadow the next source
    monkeypatch.setenv("OVERRIDE_X", "   ")
    monkeypatch.setenv("SSOT_X", "ssot")
    assert relay._cfg("OVERRIDE_X", "SSOT_X", default="floor") == "ssot"


def test_timeout_floor_is_ssot_value_not_900():
    # regression: the floor must be the calibrated SSOT value, never the old 900s that caused the
    # 2026-06-02 confound. (The live constant is read at import under the test env, which sets neither
    # LFAH_CLAUDE_TIMEOUT_S nor LOCAL_ROLE_TIMEOUT_S, so it must equal the 1800 floor.)
    import os
    if not os.environ.get("LFAH_CLAUDE_TIMEOUT_S") and not os.environ.get("LOCAL_ROLE_TIMEOUT_S"):
        assert relay.CLAUDE_TIMEOUT_S == 1800


# --- deprecated RK_* env warning (#581): a set-but-ignored config var must never be silent ---
import io


def test_warn_fires_for_known_rk_env_with_modern_hint():
    env = {"RK_CLAUDE_TIMEOUT_S": "900"}
    out = io.StringIO()
    stale = relay._warn_deprecated_rk_env(env=env, out=out)
    assert stale == ["RK_CLAUDE_TIMEOUT_S"]
    msg = out.getvalue()
    assert "RK_CLAUDE_TIMEOUT_S" in msg and "IGNORED" in msg
    assert "LFAH_CLAUDE_TIMEOUT_S" in msg and "LOCAL_ROLE_TIMEOUT_S" in msg


def test_warn_falls_back_to_rename_convention_for_unknown_rk():
    env = {"RK_DATA_DIR": "/some/path"}
    out = io.StringIO()
    stale = relay._warn_deprecated_rk_env(env=env, out=out)
    assert stale == ["RK_DATA_DIR"]
    assert "LFAH_DATA_DIR" in out.getvalue()  # RK_X -> LFAH_X fallback


def test_warn_silent_when_no_rk_env():
    env = {"LFAH_CLAUDE_TIMEOUT_S": "1800", "PATH": "/usr/bin"}
    out = io.StringIO()
    stale = relay._warn_deprecated_rk_env(env=env, out=out)
    assert stale == []
    assert out.getvalue() == ""


def test_warn_skips_blank_rk_env():
    # an RK_ var present-but-empty is not a real misconfiguration -> no warning
    env = {"RK_CLOUD_HANDOFF": "   "}
    out = io.StringIO()
    assert relay._warn_deprecated_rk_env(env=env, out=out) == []
    assert out.getvalue() == ""
