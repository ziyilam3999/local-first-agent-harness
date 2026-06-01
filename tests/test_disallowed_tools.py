"""The `claude -p` role argv must DENY interactive/control tools, not just allow a set.

Regression guard for the pytest-6197 failure: a role with an allowlist that excluded
AskUserQuestion still invoked it (the CLI exposes it by default; --allowedTools does not
gate it), burning a turn on a question no human can answer. The relay must pass
--disallowedTools so the CLI suppresses it.
"""
import importlib

relay = importlib.import_module("lfah.relay")


def _flag_value(cmd, flag):
    """Return the argument immediately following `flag` in the argv, or None."""
    return cmd[cmd.index(flag) + 1] if flag in cmd else None


def test_disallowed_tools_default_includes_askuserquestion():
    assert "AskUserQuestion" in relay.DISALLOWED_TOOLS


def test_role_cmd_passes_disallowed_tools():
    cmd = relay._build_role_cmd(
        model="sonnet", max_turns=8,
        tools=["Read", "Grep", "Glob", "Bash", "Edit", "Write"],
        system_prompt="be a good executor",
    )
    disallowed = _flag_value(cmd, "--disallowedTools")
    assert disallowed is not None, "relay must pass --disallowedTools"
    assert "AskUserQuestion" in disallowed.split(",")


def test_role_cmd_still_passes_allowed_tools():
    cmd = relay._build_role_cmd(
        model="sonnet", max_turns=8,
        tools=["Read", "Bash"], system_prompt="x",
    )
    allowed = _flag_value(cmd, "--allowedTools")
    assert allowed == "Read,Bash"
    # allow and deny are distinct sets, both present
    assert _flag_value(cmd, "--disallowedTools") is not None


def test_prompt_not_a_trailing_positional():
    """The prompt is delivered via stdin; it must never appear as a trailing argv token
    (the variadic --allowedTools/--mcp-config slurp bug)."""
    cmd = relay._build_role_cmd(
        model="sonnet", max_turns=8, tools=["Bash"], system_prompt="SENTINEL_PROMPT_BODY",
    )
    # system_prompt rides --append-system-prompt; the user prompt is never in argv at all.
    assert cmd[-1] != "SENTINEL_PROMPT_BODY"
    assert _flag_value(cmd, "--append-system-prompt") == "SENTINEL_PROMPT_BODY"
