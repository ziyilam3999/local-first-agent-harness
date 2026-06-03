"""The relay engine: a 3-agent (planner -> executor -> evaluator) coding chain.

Each role runs as a REAL `claude -p` subprocess with its production shape:

  * heterogeneous per-role models   (planner/eval = strong cloud, executor = cheap local or cloud)
  * a real Agent subprocess per role (`claude -p`, one OS process each)
  * the executor has real tools      (Read/Grep/Glob/Edit/Write/Bash) in a real repo
  * the executor edits real files     (cwd = a checkout of the instance repo)
  * the evaluator EXECUTES real tests (its Bash runs eval_patch.sh -> SWE-bench docker oracle)
  * the evaluator model != executor   (independent reviewer, never self-grade)
  * fix-until-pass, capped per-ITERATION (MAX_ITERS = 1 + N1 + N2); the SHIP/ITERATE decision
    is a deterministic rule-table in the free Python loop (`decide_action`) -- no extra LLM call

Roles are defined by a two-file contract bundled with this package:
  agents/<role>.md            -> frontmatter (model, tools, skills) + system-prompt body
  skills/<role>-specialist/   -> SKILL.md job manual, appended to the system prompt

MCP is hard-suppressed (--strict-mcp-config + empty config) so roles get ONLY their
allow-listed built-in tools.

This module is the chain engine. Ground-truth scoring is the SWE-bench docker oracle
(eval_patch.sh / oracle_eval).
"""
from __future__ import annotations
import json, os, re, shutil, signal, subprocess, sys, threading, time
from collections import Counter, deque
from pathlib import Path

HERE = Path(__file__).resolve().parent
# Bundled role files (agents/ + skills/) ship inside the package. Override with LFAH_BUNDLE_DIR.
BUNDLE_DIR = Path(os.environ.get("LFAH_BUNDLE_DIR", str(HERE / "bundle")))
AGENTS_DIR = BUNDLE_DIR / "agents"
SKILLS_DIR = BUNDLE_DIR / "skills"
# Python interpreter that has the `swebench` package installed (oracle scoring). Defaults to the
# interpreter running this process; override with LFAH_VENV_PY to point at a dedicated venv.
VENV_PY = os.environ.get("LFAH_VENV_PY", sys.executable)
# Base URL the local backend talks to (e.g. a local OpenAI-compatible proxy in front of Ollama).
CCR_BASE_URL = os.environ.get("LFAH_CCR_BASE_URL", "http://127.0.0.1:3456")


def _cfg(*names: str, default: str) -> str:
    """Read a tunable from the FIRST non-empty env var in `names`, else `default`.

    One-name-one-file convention: pass the explicit per-run OVERRIDE name first, then the canonical
    cc-models.env SSOT name. The value lives ONCE in the SSOT and every consumer reads it under the
    SAME name; an LFAH_-prefixed var is only an explicit per-run override. The `default` is a last-
    resort FLOOR for when the SSOT is not sourced -- it must NOT contradict the SSOT (the 2026-06-02
    confound was a 900s engine default that disagreed with the 1800s SSOT, masked by a typo'd env)."""
    for n in names:
        v = os.environ.get(n, "").strip()
        if v:
            return v
    return default


# Map of the load-bearing legacy names -> the modern equivalent, for a precise hint. Anything else
# RK_-prefixed falls back to the RK_X -> LFAH_X rename convention.
_DEPRECATED_RK_ENV = {
    "RK_CLAUDE_TIMEOUT_S":     "LFAH_CLAUDE_TIMEOUT_S (or the cc-models.env SSOT LOCAL_ROLE_TIMEOUT_S)",
    "RK_CLOUD_HANDOFF":        "LFAH_CLOUD_HANDOFF (or the SSOT LOCAL_FAIL_CLOUD_HANDOFF)",
    "RK_CLOUD_HANDOFF_MODEL":  "LFAH_CLOUD_HANDOFF_MODEL (or the SSOT CLOUD_HANDOFF_MODEL)",
    "RK_MOVE_BACKSTOP":        "LFAH_MOVE_BACKSTOP",
}


def _warn_deprecated_rk_env(env=None, out=None) -> list:
    """Warn LOUDLY for any RK_* env var still set -- they are SILENTLY IGNORED post-#550.

    The realkitchen->lfah rename retired every RK_* name; the engine now reads LFAH_* / cc-models.env
    SSOT names. A typo'd RK_CLAUDE_TIMEOUT_S was the 2026-06-02 timeout confound: it was ignored, so the
    run used the (then 900s) default instead of the 1800s SSOT -- a silent half-cap. A set-but-ignored
    config var must never be silent. Returns the list of stale names found (for the test)."""
    env = os.environ if env is None else env
    out = sys.stderr if out is None else out
    stale = sorted(n for n in env if n.startswith("RK_") and str(env.get(n, "")).strip())
    for n in stale:
        repl = _DEPRECATED_RK_ENV.get(n, "LFAH_" + n[3:])
        print(f"[lfah] WARNING: {n} is set but IGNORED -- the engine reads {repl} "
              f"(RK_* was retired in the #550 realkitchen->lfah rename). "
              f"Set the modern name or source the cc-models.env SSOT.", file=out)
    return stale


_warn_deprecated_rk_env()  # fire once at import, against the real environment

# Inlined code/diff extractor (kept for callers; the SHIP/ITERATE decision uses the deterministic
# `decide_action` rule-table below, so there is no coordinator JSON to parse).
def extract_python_code(text, last=False):
    blocks = re.findall(r"```(?:python|diff)?\n(.*?)```", text, re.DOTALL)
    return (blocks[-1] if last else blocks[0]).strip() if blocks else ""

# Per-role TIME cap. SSOT = cc-models.env `LOCAL_ROLE_TIMEOUT_S` (1800s/30min, cloud-aware fail-fast,
# PR#823). Precedence: explicit per-run override LFAH_CLAUDE_TIMEOUT_S > SSOT LOCAL_ROLE_TIMEOUT_S >
# 1800 floor. The floor is the SSOT value (NOT the old 900) so a missing SSOT can never silently halve
# the cap again (the 2026-06-02 confound). Source cc-models.env to set it; don't hard-code per launcher.
CLAUDE_TIMEOUT_S = int(_cfg("LFAH_CLAUDE_TIMEOUT_S", "LOCAL_ROLE_TIMEOUT_S", default="1800"))
# The 30-min TIME cap is the sole resource limit. One high backstop guards
# only against a true infinite loop and never binds on real work -- TIME always caps first. The real
# runaway-guard is STUCK-DETECTION (detect a looping agent live + stop it), below.
_MOVE_BACKSTOP = int(os.environ.get("LFAH_MOVE_BACKSTOP", "500"))
MAX_TURNS_PLAN = MAX_TURNS_EXEC = MAX_TURNS_EVAL = MAX_TURNS_PREEVAL = _MOVE_BACKSTOP
TOTAL_CAP = int(os.environ.get("LFAH_TOTAL_CAP", "16"))  # absolute role-call backstop (real cap is per-iteration; see MAX_ITERS)

# The role binary. Overridable so a deterministic fake-emitter can exercise the full Popen/streaming
# path in tests without spending cloud tokens.
CLAUDE_BIN = os.environ.get("LFAH_CLAUDE_BIN", "claude")

# STUCK-DETECTION -- the live runaway-guard that REPLACES move-counting. We stream the role's
# stream-json stdout in REAL TIME; if the agent spins on near-identical tool calls (a loop), we kill it
# and grade the partial patch (honest "unresolved") rather than letting it burn the whole TIME cap. A loop
# = within the last WINDOW tool-uses, one (tool,input) signature repeats >= THRESHOLD times AND the window
# holds <= DISTINCT_MAX distinct signatures. The distinct-floor is the false-positive guard: healthy
# read->edit->test iteration produces MANY distinct signatures, so it never trips; only low-diversity
# spinning does. All knobs are env-tunable.
STUCK_DETECT       = os.environ.get("LFAH_STUCK_DETECT", "1") != "0"
STUCK_WINDOW       = int(os.environ.get("LFAH_LOOP_WINDOW", "8"))
STUCK_THRESHOLD    = int(os.environ.get("LFAH_LOOP_THRESHOLD", "4"))
STUCK_DISTINCT_MAX = int(os.environ.get("LFAH_LOOP_DISTINCT_MAX", "3"))
# Optional cross-run incident log: when set, every stuck-kill appends a JSON line of PROOF here so a
# loop can be investigated later WITHOUT re-running (the same evidence also rides in each role result
# under `stuck_evidence`). Default off (the per-role evidence is enough).
STUCK_LOG          = os.environ.get("LFAH_STUCK_LOG", "")

# Local-arm output-token estimate. A local OpenAI-compatible bridge in front of Ollama can return BLANK
# usage to `claude -p` (both `usage` and `modelUsage` arrive 0), so a local role's output_tokens/output_tps
# would be 0 even on a clean, successful run. We estimate output tokens from the chars the model actually
# GENERATED (assistant text + tool-call JSON) using a calibrated chars/token ratio (~3.24 for code/mixed
# samples). Cloud roles keep their EXACT API usage. The `tps_source` field on every role result tags
# whether the count is "api" (exact) or an "est-chars" estimate.
LOCAL_CHARS_PER_TOKEN = float(os.environ.get("LFAH_LOCAL_CHARS_PER_TOKEN", "3.24"))

# Local-first / cloud-fallback.
# A: a LOCAL executor that TIMED OUT or got STUCK is NOT retried on the same local model -- a capped role is
#    the FAILURE tail, not a slow solve, and retries rarely rescue. Fail fast.
# C: when cloud-handoff is ON, on that local timeout|stuck we HAND THE SAME PLAN to a cloud model. The
#    honest local result is UNCHANGED (final_resolved stays local's); the cloud outcome lands in a
#    SEPARATE `handoff` field. SSOT names live in cc-models.env (LOCAL_FAIL_CLOUD_HANDOFF=0,
#    CLOUD_HANDOFF_MODEL=opus); an LFAH_-prefixed var is only an explicit per-run override. Precedence:
#    override > SSOT > floor (one-name-one-file).
CLOUD_HANDOFF       = _cfg("LFAH_CLOUD_HANDOFF", "LOCAL_FAIL_CLOUD_HANDOFF", default="0") != "0"
CLOUD_HANDOFF_MODEL = _cfg("LFAH_CLOUD_HANDOFF_MODEL", "CLOUD_HANDOFF_MODEL", default="sonnet")

# Tools a headless chain role must NEVER be able to invoke. `--allowedTools` is an ALLOW set, but it
# does NOT gate interactive/control tools that the CLI exposes by default: even when AskUserQuestion is
# absent from --allowedTools, a role still calls it and the CLI executes it (auto-dismissed in -p mode),
# burning a whole turn on a question no human can answer (the pytest-6197 no-edit failure: the local
# executor explored, asked, and never edited). The CLI only suppresses it via --disallowedTools. Keep
# this an env-overridable SSOT (comma-separated) so the denylist can grow without code edits.
DISALLOWED_TOOLS = [t.strip() for t in
                    os.environ.get("LFAH_DISALLOWED_TOOLS", "AskUserQuestion").split(",") if t.strip()]

# Optional save hooks. Both default OFF so a public install never tries to write anywhere unexpected.
SAVE_LEARNINGS = os.environ.get("RELAY_SAVE_LEARNINGS", "0") != "0"
# Optional external "prior lessons" lookup binary. Disabled unless RELAY_LESSONS_BIN points at one.
LESSONS_BIN = os.environ.get("RELAY_LESSONS_BIN", "")


def _tool_signature(block: dict) -> str:
    """Stable signature of a tool_use = name + canonical-JSON input. Identical repeated calls collide."""
    try:
        return block.get("name", "") + "|" + json.dumps(block.get("input", {}), sort_keys=True)
    except Exception:
        return block.get("name", "") + "|<unserializable>"


def _window_is_stuck(window: deque) -> bool:
    """Spin-loop test: window full + low diversity + one signature dominates (env-tuned STUCK_*)."""
    if len(window) < STUCK_WINDOW:
        return False
    counts = Counter(window)
    if len(counts) > STUCK_DISTINCT_MAX:
        return False
    return counts.most_common(1)[0][1] >= STUCK_THRESHOLD


# ---------------------------------------------------------------------------
# Two-file role contract: parse agents/<role>.md frontmatter + body + SKILL.md
# ---------------------------------------------------------------------------
def parse_role(role: str, skill_override: str | None = None) -> dict:
    agent_md = (AGENTS_DIR / f"{role}.md").read_text()
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", agent_md, re.DOTALL)
    if not m:
        raise ValueError(f"{role}.md has no frontmatter block")
    fm_raw, body = m.group(1), m.group(2).strip()
    fm = {}
    skills, in_skills = [], False
    for line in fm_raw.splitlines():
        if in_skills:
            sm = re.match(r"\s*-\s*(.+)$", line)
            if sm:
                skills.append(sm.group(1).strip()); continue
            in_skills = False
        if re.match(r"\s*skills:\s*$", line):
            in_skills = True; continue
        km = re.match(r"(\w+):\s*(.*)$", line)
        if km:
            fm[km.group(1)] = km.group(2).strip()
    # Append each declared specialist SKILL.md (the job manual) to the system prompt. When the
    # category profile supplies a recipe (skill_override) it WINS over the agent frontmatter -- that
    # is the profile seam: the SAME role file serves any category; the profile picks the manual, so a
    # new category is additive (no per-category agent files).
    skill_text = ""
    skills_to_load = [skill_override] if skill_override else skills
    for s in skills_to_load:
        sp = SKILLS_DIR / s / "SKILL.md"
        if sp.exists():
            skill_text += f"\n\n# Specialist manual: {s}\n\n" + sp.read_text()
    tools = [t.strip() for t in fm.get("tools", "").split(",") if t.strip()]
    return {
        "role": role,
        "model": fm.get("model", "sonnet"),
        "tools": tools,
        "skills": skills_to_load,
        "system_prompt": body + skill_text,
    }


# ---------------------------------------------------------------------------
# Per-role `claude -p` subprocess with REAL tools + stream-json telemetry
# ---------------------------------------------------------------------------
def _role_env(backend: str) -> dict:
    """cloud => real Anthropic endpoint (your configured auth); local => local proxy -> Ollama."""
    env = dict(os.environ)
    env.pop("ANTHROPIC_BASE_URL", None)
    if backend == "local":
        env["ANTHROPIC_BASE_URL"] = CCR_BASE_URL
        env["ANTHROPIC_AUTH_TOKEN"] = "local-dummy"
        env.pop("ANTHROPIC_API_KEY", None)
    return env


def _build_role_cmd(*, model: str, max_turns: int, tools: list, system_prompt: str) -> list:
    """Construct the `claude -p` argv for one role. Extracted from run_role so the flag contract
    (allow set, disallow set, stdin-delivery) is unit-testable without spawning a subprocess."""
    cmd = [
        CLAUDE_BIN, "-p", "--model", model,
        "--output-format", "stream-json", "--verbose",
        "--permission-mode", "acceptEdits",
        "--max-turns", str(max_turns),
        "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
        "--setting-sources", "",
        "--append-system-prompt", system_prompt,
    ]
    tools_csv = ",".join(tools)
    if tools_csv:
        cmd += ["--allowedTools", tools_csv]
    # ENFORCE the deny set. --allowedTools alone does NOT keep a role off interactive/control tools the
    # CLI exposes by default (verified: AskUserQuestion runs even when absent from --allowedTools); only
    # --disallowedTools suppresses it. Without this a role can burn a turn on a question no human can
    # answer (pytest-6197: explored, asked, never edited -> empty patch).
    if DISALLOWED_TOOLS:
        cmd += ["--disallowedTools", ",".join(DISALLOWED_TOOLS)]
    return cmd


def run_role(*, spec: dict, model: str, backend: str, user_prompt: str,
             cwd: Path, max_turns: int, dry_run: bool = False) -> dict:
    """Run one role as a real `claude -p` agent. Returns dict with response text,
    the list of tool_use events (faithfulness evidence), cost, and wall time."""
    if dry_run:
        return {"response": '{"action":"SHIP","reason":"dry-run"}', "tool_uses": [],
                "cost_usd": 0.0, "num_turns": 0, "wall_s": 0.0, "raw_lines": 0,
                "input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0, "duration_api_ms": 0, "duration_ms": 0,
                "output_tps": 0.0, "model_resolved": model, "soft_error": "", "stuck_evidence": None}
    cmd = _build_role_cmd(model=model, max_turns=max_turns,
                          tools=spec["tools"], system_prompt=spec["system_prompt"])
    # Deliver the prompt via STDIN, NOT as a trailing positional arg. In the current Claude Code CLI,
    # --allowedTools (and --mcp-config) are VARIADIC and slurp a trailing positional, so
    # `... --allowedTools <csv> <prompt>` consumes the prompt as another tool name -> claude errors
    # "Input must be provided" -> the role never runs and uses zero tools. stdin delivery is immune.
    t0 = time.time()
    # --- LIVE-STREAMED subprocess (stuck-detection) --------------------------------------------------
    # subprocess.run() blocks until the role finishes or hits the TIME cap; it cannot see a spin loop
    # mid-flight. Popen + line-buffered stdout lets us parse each stream-json event AS IT ARRIVES, detect a
    # loop, and kill EARLY (grade the partial). A watchdog thread enforces the TIME cap (Popen has no
    # built-in timeout=, and it also covers a role that hangs emitting NOTHING). The authoritative telemetry
    # parse still runs post-hoc on the full captured stdout -- this loop only feeds detection.
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, bufsize=1,
                            cwd=str(cwd), env=_role_env(backend), start_new_session=True)
    kill_reason = {"v": ""}                 # "" | "stuck" | "timeout"  (set here or by the watchdog)
    done = threading.Event()

    def _terminate():
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)   # kill the whole process group (claude + children)
        except Exception:
            try: proc.terminate()
            except Exception: pass

    def _watchdog():
        # TIME cap: the role ran its full budget without finishing (or hung with no output). Kill it; the
        # caller grades whatever patch is on disk.
        if not done.wait(CLAUDE_TIMEOUT_S):
            if not kill_reason["v"]:
                kill_reason["v"] = "timeout"
            _terminate()
    threading.Thread(target=_watchdog, daemon=True).start()

    # Feed the prompt via stdin in a thread -- a large prompt can exceed the OS pipe buffer, and writing it
    # inline before reading stdout would deadlock if the child starts emitting first.
    def _feed():
        try:
            proc.stdin.write(user_prompt); proc.stdin.close()
        except Exception:
            pass
    threading.Thread(target=_feed, daemon=True).start()

    # Drain stderr in a thread so a full stderr pipe can never block the child.
    err_chunks: list[str] = []
    def _drain_err():
        try:
            for ln in proc.stderr:
                err_chunks.append(ln)
        except Exception:
            pass
    threading.Thread(target=_drain_err, daemon=True).start()

    # Main loop: read stdout live (readline avoids the read-ahead buffering of `for line in file`, so each
    # JSON event is seen the moment it is flushed) and watch for a spin loop.
    out_lines: list[str] = []
    sig_window: deque = deque(maxlen=STUCK_WINDOW)
    tool_use_n = 0                          # running count of tool_uses (the "move ordinal" of the kill)
    stuck_evidence = None                   # PROOF of the loop, captured at the kill for later investigation
    role_name = spec.get("role", "?")
    for line in iter(proc.stdout.readline, ""):
        out_lines.append(line)
        if not STUCK_DETECT or kill_reason["v"]:
            continue
        s = line.strip()
        if not s:
            continue
        try:
            obj = json.loads(s)
        except Exception:
            continue
        if obj.get("type") != "assistant":
            continue
        looped = False
        for block in obj.get("message", {}).get("content", []):
            if block.get("type") == "tool_use":
                tool_use_n += 1
                sig_window.append(_tool_signature(block))
                if _window_is_stuck(sig_window):
                    looped = True; break
        if looped:
            kill_reason["v"] = "stuck"
            # Capture honest PROOF: which signature dominated, how often, the whole window, where it hit.
            wc = Counter(sig_window)
            top_sig, top_count = wc.most_common(1)[0]
            stuck_evidence = {
                "looping_signature": top_sig,        # the exact tool+input it spun on
                "repeat_count": top_count,           # how many times that sig is in the window
                "distinct_in_window": len(wc),       # diversity (low => a real spin, not iteration)
                "window_signatures": list(sig_window),  # full window = the loop, verbatim
                "tool_use_ordinal": tool_use_n,      # which move tripped it
                "rule": {"window": STUCK_WINDOW, "threshold": STUCK_THRESHOLD,
                         "distinct_max": STUCK_DISTINCT_MAX},
            }
            sys.stderr.write(
                f"[relay stuck-detect] role={role_name} model={model} KILLED at tool_use #{tool_use_n}: "
                f"signature {top_sig!r} repeated {top_count}x in a window of {STUCK_WINDOW} "
                f"({len(wc)} distinct). Partial patch graded as honest unresolved.\n")
            sys.stderr.flush()
            if STUCK_LOG:                            # optional cross-run incident log (investigate later)
                try:
                    with open(STUCK_LOG, "a") as _fh:
                        _fh.write(json.dumps({"ts": round(time.time(), 3), "role": role_name,
                                              "model": model, "backend": backend, **stuck_evidence}) + "\n")
                except Exception:
                    pass
            _terminate()                    # SIGTERM the group; stdout EOFs, loop drains then ends
    done.set()                              # release the watchdog (we are done reading)
    try:
        proc.wait(timeout=15)
    except Exception:                       # graceful SIGTERM ignored -> escalate to SIGKILL
        try: os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            try: proc.kill()
            except Exception: pass
        try: proc.wait(timeout=10)
        except Exception: pass
    stdout = "".join(out_lines)
    stderr = "".join(err_chunks)
    rc = proc.returncode if proc.returncode is not None else -1
    wall_s = time.time() - t0
    # A stuck-kill or a TIME-cap kill is an honest `unresolved`, NOT a harness error -- the caller grades the
    # partial patch on disk (never raises). A plain non-zero exit is reported verbatim; still graded, not
    # raised. The stream-json `result` is emitted before a cap, so the parse below works on captured `stdout`.
    if kill_reason["v"]:
        soft_error = kill_reason["v"]
    elif rc == 0:
        soft_error = ""
    else:
        soft_error = f"exit {rc}: {(stderr or '')[:200]}"
    # Parse the stream-json JSONL: collect tool_use blocks + the final result object (incl. usage).
    tool_uses, final_text, cost, num_turns = [], "", 0.0, 0
    usage, duration_api_ms, duration_ms, model_resolved = {}, 0, 0, ""
    gen_chars = 0                            # chars the model GENERATED (text + tool-call JSON) -> local-TPS estimate
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        t = obj.get("type")
        if t == "assistant":
            mdl = obj.get("message", {}).get("model")     # concrete model id the provider actually ran
            if mdl:
                model_resolved = mdl
            for block in obj.get("message", {}).get("content", []):
                bt = block.get("type")
                if bt == "tool_use":
                    tool_uses.append({"name": block.get("name"),
                                      "input": block.get("input", {})})
                    gen_chars += len(block.get("name", "") or "")   # model emitted the tool name + input as tokens
                    try:
                        gen_chars += len(json.dumps(block.get("input", {}), default=str))
                    except Exception:
                        pass
                elif bt == "text":
                    gen_chars += len(block.get("text", "") or "")
        elif t == "result":
            final_text = obj.get("result", "") or final_text
            cost = obj.get("total_cost_usd", cost) or cost
            num_turns = obj.get("num_turns", num_turns) or num_turns
            usage = obj.get("usage", usage) or usage
            duration_api_ms = obj.get("duration_api_ms", duration_api_ms) or duration_api_ms
            duration_ms = obj.get("duration_ms", duration_ms) or duration_ms
            model_resolved = obj.get("model", model_resolved) or model_resolved  # result obj may carry it too
    # Three-axis telemetry: per-role tokens (cost), durations + output_tps (performance).
    in_tok = usage.get("input_tokens", 0) or 0
    out_tok = usage.get("output_tokens", 0) or 0
    cache_read = usage.get("cache_read_input_tokens", 0) or 0
    cache_creation = usage.get("cache_creation_input_tokens", 0) or 0
    # LOCAL path: `usage.output_tokens` may arrive 0 (the local proxy forwards no token counts), so out_tok==0
    # even on a clean run. Recover a real output-token count from the chars the model GENERATED (text +
    # tool-call JSON) using the calibrated ratio, so output_tps is meaningful instead of 0.0. Cloud roles
    # report exact usage (out_tok>0) and are left untouched. `tps_source` tags exact-vs-estimate for honesty.
    tps_source = "api"
    if out_tok == 0 and gen_chars > 0:
        out_tok = round(gen_chars / LOCAL_CHARS_PER_TOKEN)
        tps_source = f"est-chars/{LOCAL_CHARS_PER_TOKEN:g}"
    # output_tps = EFFECTIVE throughput: output tokens per WALL second. SAME denominator (wall_s) for cloud
    # + local => apples-to-apples end-to-end rate, incl. prefill + tool gaps. tps_source flags whether
    # out_tok is exact ("api") or estimated.
    output_tps = round(out_tok / wall_s, 2) if wall_s > 0 else 0.0
    return {"response": final_text, "tool_uses": tool_uses, "cost_usd": cost,
            "user_prompt": user_prompt,   # persist the EXACT input the role saw
            "num_turns": num_turns, "wall_s": wall_s,
            "raw_lines": len(stdout.splitlines()),
            "input_tokens": in_tok, "output_tokens": out_tok,
            "cache_read_input_tokens": cache_read, "cache_creation_input_tokens": cache_creation,
            "duration_api_ms": duration_api_ms, "duration_ms": duration_ms,
            "output_tps": output_tps, "tps_source": tps_source, "gen_chars": gen_chars,
            "model_resolved": model_resolved, "soft_error": soft_error,
            "stuck_evidence": stuck_evidence}   # proof when soft_error=="stuck" (None otherwise)


# ---------------------------------------------------------------------------
# Ground-truth oracle: run the SWE-bench docker eval on the executor's patch
# ---------------------------------------------------------------------------
def oracle_eval(instance_id: str, diff_path: Path, run_id: str) -> dict:
    """Score a candidate patch via the canonical SWE-bench docker harness.
    Returns {'resolved': bool, 'report': <path|None>, 'rc': int}."""
    diff = diff_path.read_text()
    preds = diff_path.parent / "runs" / run_id
    preds.mkdir(parents=True, exist_ok=True)
    pf = preds / "preds.jsonl"
    pf.write_text(json.dumps({"instance_id": instance_id,
                              "model_name_or_path": run_id,
                              "model_patch": diff}) + "\n")
    env = dict(os.environ)
    env["DOCKER_HOST"] = os.environ.get("LFAH_DOCKER_HOST", "unix:///var/run/docker.sock")
    dataset = os.environ.get("LFAH_DATASET", "princeton-nlp/SWE-bench_Verified")
    split = os.environ.get("LFAH_SPLIT", "test")
    cmd = [VENV_PY, "-m", "swebench.harness.run_evaluation",
           "--dataset_name", dataset, "--split", split,
           "--instance_ids", instance_id, "--predictions_path", str(pf),
           "--run_id", run_id, "--max_workers", "1", "--namespace", "none",
           "--cache_level", "instance", "--timeout", "1800"]
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=str(preds), env=env,
                       timeout=2400)
    report = next(iter(sorted(preds.glob(f"{run_id}.*.json"), reverse=True)), None)
    resolved = False
    if report:
        try:
            resolved = json.loads(report.read_text()).get("resolved_instances", 0) == 1
        except Exception:
            pass
    (preds / "eval.log").write_text((r.stdout or "")[-8000:] + "\n--STDERR--\n" + (r.stderr or "")[-4000:])
    return {"resolved": resolved, "report": str(report) if report else None, "rc": r.returncode}


# ---------------------------------------------------------------------------
# Ground-truth oracle #2: JavaScript / jest. The first NON-Python grader -- proof that the
# profile/oracle seam generalizes across languages. Same (instance_id, diff_path, run_id) ->
# {resolved, report, rc} contract as oracle_eval, so the engine's profile indirection is unchanged.
# Where oracle_eval reconstructs ground truth from the SWE-bench HF dataset keyed by instance_id,
# this reconstructs it from a LOCAL instance dir (LFAH_DATA_DIR/instances/<id>/) -- symmetric, both
# resolve everything from the id + a data-root env. Hermetic in docker by default (parity with the
# pytest oracle); LFAH_JEST_DOCKER=0 runs jest on host node for a fast dev loop.
# ---------------------------------------------------------------------------
def jest_oracle_eval(instance_id: str, diff_path: Path, run_id: str) -> dict:
    """Score a candidate patch against a JS/jest instance. resolved == the WHOLE suite is green
    after applying the patch (covers FAIL_TO_PASS and PASS_TO_PASS in one shot)."""
    data_root = os.environ.get("LFAH_DATA_DIR")
    if not data_root:
        raise RuntimeError("jest_oracle_eval needs LFAH_DATA_DIR (root holding "
                           "instances/<instance_id>/{instance.json,repo})")
    inst_dir = Path(data_root) / "instances" / instance_id
    instance = json.loads((inst_dir / "instance.json").read_text())
    src_repo = inst_dir / "repo"

    # Work area must sit on a docker-mountable path (colima mounts $HOME). In real chain runs
    # diff_path.parent is the instance dir (under $HOME); tests override via LFAH_JEST_WORKROOT.
    work_root = Path(os.environ.get("LFAH_JEST_WORKROOT") or diff_path.parent) / "jest-runs" / run_id
    work_root.mkdir(parents=True, exist_ok=True)
    jestrepo = work_root / "jestrepo"
    if jestrepo.exists():
        shutil.rmtree(jestrepo)
    shutil.copytree(src_repo, jestrepo)

    # Guarantee the base state even if the canonical copy drifted, then apply the candidate patch.
    base = instance.get("base_commit")
    if base and (jestrepo / ".git").exists():
        for c in (["checkout", "--quiet", "--force", base], ["reset", "--hard", "--quiet"],
                  ["clean", "-fdq"]):
            subprocess.run(["git", "-C", str(jestrepo), *c], capture_output=True, text=True)
    diff_text = diff_path.read_text()
    apply_rc, apply_err = 0, ""
    if diff_text.strip():                       # empty diff -> no-op -> baseline (correctly unresolved)
        ap = subprocess.run(["git", "-C", str(jestrepo), "apply", "--whitespace=nowarn",
                             str(diff_path)], capture_output=True, text=True)
        apply_rc, apply_err = ap.returncode, ap.stderr

    # Tamper-hardening: re-impose the canonical graded test files from base_commit, discarding any
    # candidate edits to them. The task is to fix SOURCE; a candidate that weakens/deletes the test
    # to go green must not be rewarded. base_commit already carries the canonical (failing) test, so
    # `git checkout <base> -- <test_files>` restores it. No-op when the candidate left tests alone
    # (the honest case), and skipped for instances that declare no test_files (behavior-preserving).
    test_files = instance.get("test_files") or []
    reimpose_rc, reimpose_err = None, ""
    if base and test_files and (jestrepo / ".git").exists():
        ci = subprocess.run(["git", "-C", str(jestrepo), "checkout", base, "--", *test_files],
                            capture_output=True, text=True)
        reimpose_rc, reimpose_err = ci.returncode, ci.stderr
        # A non-zero rc means a test_file path is absent at base_commit (corpus/config bug): the
        # re-imposition silently no-ops and the grader reverts to gameable behavior. Surface it
        # loudly in eval.log so it is caught, rather than degrading the integrity check in silence.

    out_json = jestrepo / ".jestout.json"
    # One container invocation: install jest, run the suite, emit JSON. `; true` so a non-zero jest
    # exit (failing tests) does not mask the JSON we parse for the verdict.
    inner = ("npm install --silent --no-audit --no-fund && "
             "npx --no-install jest --json --outputFile=.jestout.json --ci; true")
    timeout_s = int(os.environ.get("LFAH_JEST_TIMEOUT_S", "600"))
    env = dict(os.environ)
    if os.environ.get("LFAH_JEST_DOCKER", "1") != "0":
        image = os.environ.get("LFAH_JEST_IMAGE", "node:20-slim")
        # Honor an explicit LFAH_DOCKER_HOST or an inherited DOCKER_HOST; otherwise leave it UNSET so the
        # docker CLI falls back to its active context (e.g. colima) instead of a wrong /var/run default.
        docker_host = os.environ.get("LFAH_DOCKER_HOST") or os.environ.get("DOCKER_HOST")
        if docker_host:
            env["DOCKER_HOST"] = docker_host
        cmd = ["docker", "run", "--rm", "-v", f"{jestrepo}:/work", "-w", "/work", image,
               "sh", "-lc", inner]
    else:
        cmd = ["sh", "-lc", inner]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=str(jestrepo), env=env,
                           timeout=timeout_s)
        rc, runout, runerr = r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired as e:
        rc, runout, runerr = 124, (e.stdout or ""), f"jest timed out after {timeout_s}s"

    resolved, report = False, None
    if out_json.exists():
        report = str(out_json)
        try:
            j = json.loads(out_json.read_text())
            resolved = (bool(j.get("success")) and j.get("numFailedTests", 1) == 0
                        and j.get("numFailedTestSuites", 1) == 0 and j.get("numTotalTests", 0) >= 1)
        except Exception:
            resolved = False
    (work_root / "eval.log").write_text(
        f"apply_rc={apply_rc}\n{apply_err}\n"
        f"reimpose_rc={reimpose_rc}\n{reimpose_err}\n--JEST STDOUT--\n{(runout or '')[-8000:]}"
        f"\n--JEST STDERR--\n{(runerr or '')[-4000:]}")
    return {"resolved": resolved, "report": report, "rc": rc, "reimpose_rc": reimpose_rc}


# ---------------------------------------------------------------------------
# The capped 3-agent chain on ONE SWE-bench instance (planner -> executor -> oracle -> evaluator)
# ---------------------------------------------------------------------------
def reset_repo(repo: Path, base_commit: str):
    if not Path(repo).exists():            # no checkout (e.g. a dry-run smoke) -> nothing to reset
        return
    for c in (f"git -c advice.detachedHead=false checkout --quiet --force {base_commit}",
              "git reset --hard --quiet", "git clean -fdq"):
        subprocess.run(c, shell=True, cwd=str(repo), capture_output=True)


def git_diff(repo: Path) -> str:
    if not Path(repo).exists():            # no checkout -> no diff
        return ""
    return subprocess.run("git diff", shell=True, cwd=str(repo),
                          capture_output=True, text=True).stdout


def lessons_find(topic: str) -> str:
    """Optional 'prior lessons' lookup. Off by default: returns '' unless RELAY_LESSONS_BIN points at an
    executable that takes a topic argument and prints relevant prior notes on stdout. Best-effort:
    returns '' on any failure (missing binary / timeout) so the chain never blocks on it."""
    if not LESSONS_BIN or not topic.strip():
        return ""
    bin_path = Path(LESSONS_BIN)
    if not bin_path.exists():
        return ""
    try:
        r = subprocess.run([str(bin_path), topic], capture_output=True, text=True, timeout=30)
        return (r.stdout or "").strip()[:2000]
    except Exception:
        return ""


def decide_action(*, oracle_resolved: bool, eval_text: str, n1_left: int, n2_left: int) -> dict:
    """The folded coordinator rule-table (the conductor is the free Python loop, no LLM).

    Ground truth is the docker oracle; the evaluator's verdict only routes the KIND of iterate:
      resolved                      -> SHIP
      unresolved + ISSUE-PLAN + N2  -> ITERATE-REPLAN   (the plan was wrong; regenerate it)
      unresolved + exec budget      -> ITERATE-EXECUTOR (the code was wrong; fix it)
      unresolved + only replan left -> ITERATE-REPLAN
      unresolved + no budget        -> SHIP (loop labels it SHIP-CAPPED)
    An unresolved oracle can NEVER silently SHIP while iteration budget remains."""
    if oracle_resolved:
        return {"action": "SHIP", "reason": "oracle_resolved"}
    et = (eval_text or "").upper()
    if "ISSUE-PLAN" in et and n2_left > 0:
        return {"action": "ITERATE-REPLAN", "reason": "oracle_unresolved+issue_plan"}
    if n1_left > 0:
        return {"action": "ITERATE-EXECUTOR", "reason": "oracle_unresolved"}
    if n2_left > 0:
        return {"action": "ITERATE-REPLAN", "reason": "oracle_unresolved+exec_budget_spent"}
    return {"action": "SHIP", "reason": "budget_exhausted"}


# ---------------------------------------------------------------------------
# Category profiles + the mechanical completeness gate.
# A category = a PROFILE = {3 recipes + oracle + faithfulness-asserts}; the fixed orchestrator loads
# it by category. code-fix is profile #1. The engine reads the oracle/profile indirection -- it never
# hard-assumes eval_patch.sh -- so other task categories become additive, not engine rewrites.
# ---------------------------------------------------------------------------
REQUIRED_ROLES = ("planner", "executor", "evaluator")
FAITHFULNESS_AXES = ("heterogeneous_models", "evaluator_ne_executor", "executor_used_tools",
                     "evaluator_executed_real_test", "capped_<=max_iters",
                     "no_premature_ship_of_broken")


def make_codefix_profile() -> dict:
    """Profile #1 -- code-fix. Oracle class = deterministic (SWE-bench docker FAIL_TO_PASS test)."""
    return {
        "category": "code-fix",
        "recipes": {"planner": "codefix-plan-specialist", "executor": "codefix-execute-specialist",
                    "evaluator": "codefix-evaluate-specialist"},
        "oracle": {"kind": "deterministic", "wrapper": "eval_patch.sh", "fn": oracle_eval},
        "faithfulness_asserts": list(FAITHFULNESS_AXES),
    }


def make_jest_profile() -> dict:
    """Profile #1 for JavaScript -- code-fix graded by jest (the first non-Python oracle). Reuses the
    SAME language-agnostic codefix specialist recipes (they drive a generic 'fix the failing test'
    loop via Bash, with no pytest assumption); only the oracle/wrapper differ. Proves a new language
    is an ADDITIVE profile, not an engine rewrite."""
    return {
        "category": "code-fix",
        "language": "javascript",
        "recipes": {"planner": "codefix-plan-specialist", "executor": "codefix-execute-specialist",
                    "evaluator": "codefix-evaluate-specialist"},
        "oracle": {"kind": "deterministic", "wrapper": "eval_patch_jest.sh", "fn": jest_oracle_eval},
        "faithfulness_asserts": list(FAITHFULNESS_AXES),
    }


def select_profile(instance: dict) -> dict:
    """The language axis: pick the code-fix profile for an instance's declared language. JavaScript
    (language in {'javascript','js'}) -> the jest-graded profile; everything else (python, an absent
    field, or any other value) -> the default pytest/swebench codefix profile. Existing Python
    instances carry no 'language' field, so this is behavior-preserving for them."""
    lang = str((instance or {}).get("language", "") or "").strip().lower()
    return make_jest_profile() if lang in ("javascript", "js") else make_codefix_profile()


def assert_profile_complete(profile: dict, role_models: dict, role_backends: dict) -> dict:
    """MECHANICAL completeness gate. REFUSES (raises RuntimeError) to run a category whose profile is
    missing any of {3 recipes, an oracle, faithfulness-asserts}, OR that declares >1 distinct local
    model across local-backed roles (the one-local-model-at-a-time rule for a single GPU). Returns a
    checks dict on ALLOW (a complete profile)."""
    problems = []
    recipes = profile.get("recipes") or {}
    missing = [r for r in REQUIRED_ROLES if not recipes.get(r)]
    if missing:
        problems.append(f"missing recipe(s) for {missing}")
    else:
        for r in REQUIRED_ROLES:
            sp = SKILLS_DIR / recipes[r] / "SKILL.md"
            if not sp.exists():
                problems.append(f"recipe SKILL.md not found for {r}: {sp}")
    oracle = profile.get("oracle") or {}
    if not oracle:
        problems.append("missing oracle")
    elif oracle.get("kind") == "deterministic":
        wrap = HERE / (oracle.get("wrapper") or "")
        if not oracle.get("wrapper") or not wrap.exists():
            problems.append(f"deterministic oracle wrapper not found: {wrap}")
        if not callable(oracle.get("fn")):
            problems.append("oracle.fn is not callable")
    if not profile.get("faithfulness_asserts"):
        problems.append("missing faithfulness-asserts")
    local_models = {role_models.get(r) for r in role_models if role_backends.get(r) == "local"}
    local_models.discard(None)
    if len(local_models) > 1:
        problems.append(f"declares >1 distinct local model {sorted(local_models)} "
                        f"(one-local-model-at-a-time rule)")
    if problems:
        raise RuntimeError("PROFILE INCOMPLETE (completeness gate) for category "
                           f"'{profile.get('category')}': " + "; ".join(problems))
    return {"category": profile.get("category"), "recipes_ok": True, "oracle_ok": True,
            "faithfulness_asserts_ok": True, "local_models_ok": True,
            "n_distinct_local_models": len(local_models)}


def run_chain(*, instance: dict, repo: Path, role_models: dict, role_backends: dict,
              mode: str, profile: dict, dry_run: bool = False) -> dict:
    """role_models/role_backends: per-role {planner,executor,evaluator}. `profile` is the loaded
    category profile (recipes + oracle + faithfulness); the engine reads its verifier from here,
    never hard-assuming eval_patch.sh. mode 'a' => N2=0 (no replan); mode 'c' => N1=1,N2=1."""
    recipes = profile["recipes"]
    specs = {r: parse_role(r, skill_override=recipes.get(r))
             for r in ("planner", "executor", "evaluator")}
    N1, N2 = (1, 0) if mode == "a" else (1, 1)
    force_iterate = int(os.environ.get("LFAH_FORCE_ITERATE", "0"))  # test hook: force the first N iterations to ITERATE
    iid = instance["instance_id"]
    problem = instance["problem_statement"]
    f2p = instance["FAIL_TO_PASS"]
    rounds, rounds_used = [], 0
    eval_wrapper = HERE / profile["oracle"]["wrapper"]   # the seam: verifier comes from the profile
    oracle_fn = profile["oracle"]["fn"]                  # harness-owned ground-truth scoring (per category)

    reset_repo(repo, instance["base_commit"])

    # Optional prior-lessons lookup (off unless RELAY_LESSONS_BIN is set), injected into BOTH evaluator
    # passes (pre-code plan review + post-code code review). Query the instance_id first (most precise),
    # then fall back to the repo name.
    lessons_topic = iid
    lessons_lessons = lessons_find(iid)
    if not lessons_lessons:
        lessons_topic = instance["repo"].split("/")[-1]
        lessons_lessons = lessons_find(lessons_topic)
    lessons_block = ("\n\nRELEVANT PRIOR LESSONS (factor these into your review):\n"
                   + (lessons_lessons if lessons_lessons else "(none on record for this topic)") + "\n")

    # ---- planner (once) ----
    plan_prompt = (
        f"TASK (repo: {instance['repo']} @ {instance['base_commit']}):\n\n{problem}\n\n"
        f"The repo is checked out in your working directory. The change must make this "
        f"failing test pass: {f2p}. Study the relevant files, then write the structured plan.")
    p = run_role(spec=specs["planner"], model=role_models["planner"],
                 backend=role_backends["planner"], user_prompt=plan_prompt,
                 cwd=repo, max_turns=MAX_TURNS_PLAN, dry_run=dry_run)
    rounds_used += 1
    plan_text = p["response"]

    # ---- evaluator PRE-CODE plan gate -- catch a bad plan BEFORE an executor round. Reuses the
    #      evaluator role/model, no patch yet; at most ONE pre-code replan (consumes N2), then proceeds
    #      regardless so a stubborn planner can't deadlock. ----
    precode_prompt = (
        "PRE-CODE PLAN REVIEW -- there is NO code yet.\n\n"
        f"TASK (repo: {instance['repo']}):\n\n{problem}\n\n"
        f"PLANNER'S PLAN (Approach + AC):\n{plan_text}\n\n"
        "EXECUTOR'S PATCH: (none -- no code yet)\n"
        + lessons_block +
        f"\nThe target failing test is: {f2p}. Judge ONLY the plan/AC per your PRE-CODE mode; do NOT "
        f"run the verifier (nothing to test yet). Emit PASS or ISSUE-PLAN: <one line>.")
    pre_ev = run_role(spec=specs["evaluator"], model=role_models["evaluator"],
                      backend=role_backends["evaluator"], user_prompt=precode_prompt,
                      cwd=repo, max_turns=MAX_TURNS_PREEVAL, dry_run=dry_run)
    rounds_used += 1
    precode = {"verdict": pre_ev["response"], "tool_uses": pre_ev["tool_uses"],
               "patch_present": False, "before_first_executor": True, "replanned": False,
               "eval": pre_ev}
    if "ISSUE-PLAN" in (pre_ev["response"] or "").upper() and N2 > 0 and not dry_run:
        N2 -= 1
        pr = run_role(spec=specs["planner"], model=role_models["planner"],
                      backend=role_backends["planner"],
                      user_prompt=plan_prompt + f"\n\nPRIOR ATTEMPT FAILED. Evaluator said:\n{pre_ev['response']}",
                      cwd=repo, max_turns=MAX_TURNS_PLAN, dry_run=dry_run)
        rounds_used += 1
        plan_text = pr["response"]
        precode["replanned"] = True

    MAX_ITERS = 1 + N1 + N2          # AFTER the pre-code gate: the loop ceiling reflects the replan
                                     # budget the gate may already have spent (keeps the cap tight)
    verdict = None
    last_diff, last_eval_text = "", ""
    iteration = 0
    while True:
        # ---- executor ----
        exec_prompt = (
            f"TASK (repo: {instance['repo']}):\n\n{problem}\n\n"
            f"PLANNER'S PLAN:\n{plan_text}\n\n"
            f"The repo is checked out in your working directory. Make the change with your "
            f"tools (Edit/Write), self-check with Bash, and ensure the intended fix is on disk. "
            f"The target failing test is: {f2p}.")
        if last_eval_text:
            exec_prompt += f"\n\nPRIOR EVALUATOR FEEDBACK (fix this):\n{last_eval_text}"
        e = run_role(spec=specs["executor"], model=role_models["executor"],
                     backend=role_backends["executor"], user_prompt=exec_prompt,
                     cwd=repo, max_turns=MAX_TURNS_EXEC, dry_run=dry_run)
        rounds_used += 1
        last_diff = git_diff(repo)
        diff_file = repo.parent / f"patch.round{iteration}.diff"
        diff_file.write_text(last_diff)

        # ---- ground-truth oracle (harness-owned, canonical scoring) ----
        oracle = ({"resolved": True, "report": None, "rc": 0} if dry_run
                  else oracle_fn(iid, diff_file, f"rk-{iid}-r{iteration}-{int(time.time())}"))

        # ---- evaluator (DIFFERENT model; runs the real test via its Bash) ----
        eval_prompt = (
            f"TASK (repo: {instance['repo']}):\n\n{problem}\n\n"
            f"PLANNER'S PLAN (Approach + AC):\n{plan_text}\n\n"
            f"EXECUTOR'S PATCH (git diff):\n```diff\n{last_diff[:6000]}\n```\n"
            + lessons_block +
            f"\nTo verify the code against the REAL test, run this in your working directory:\n"
            f"    bash {eval_wrapper} {iid}\n"
            f"It runs the canonical SWE-bench test ({f2p}) in the correct docker env and prints "
            f"RESOLVED=true|false. Base your code verdict on that actual output. Then output your "
            f"PASS / ISSUE-PLAN / ISSUE-CODE verdict per your manual.")
        ev = run_role(spec=specs["evaluator"], model=role_models["evaluator"],
                      backend=role_backends["evaluator"], user_prompt=eval_prompt,
                      cwd=repo, max_turns=MAX_TURNS_EVAL, dry_run=dry_run)
        rounds_used += 1
        last_eval_text = ev["response"]

        # ---- orchestrator decision (deterministic rule-table; no coordinator LLM) ----
        action = decide_action(oracle_resolved=oracle["resolved"], eval_text=last_eval_text,
                               n1_left=N1, n2_left=N2)
        rounds.append({"iteration": iteration, "planner": p if iteration == 0 else None,
                       "executor": e, "evaluator": ev,
                       "action": action, "oracle": oracle, "diff_bytes": len(last_diff),
                       "diff_text": last_diff})   # persist the ACTUAL patch the executor produced

        act = action.get("action", "SHIP")
        # Test hook: force the first `force_iterate` iterations to ITERATE (deterministic smoke).
        if iteration < force_iterate:
            act = "ITERATE-EXECUTOR" if N1 > 0 else ("ITERATE-REPLAN" if N2 > 0 else "SHIP")
        # IMPROVEMENT A: a LOCAL executor that TIMED OUT or got STUCK is NOT retried on the same local
        # model -- re-running re-fails and a capped role is the failure tail, not a slow solve. Fail fast
        # -> SHIP-CAPPED (unresolved). The post-loop cloud handoff (improvement C) may then rescue it.
        if (role_backends["executor"] == "local" and e.get("soft_error") in ("timeout", "stuck")
                and not oracle["resolved"]):
            rounds[-1]["action"] = {"action": "SHIP",
                                    "reason": f"local_executor_{e.get('soft_error')}_no_retry"}
            verdict = "SHIP-CAPPED"
            break
        # Cap enforcement: the UNIT is iterations (MAX_ITERS = 1 + N1 + N2), not per-role-call rounds.
        # A forced stop on an unresolved oracle is a capped ship.
        at_cap = (iteration + 1) >= MAX_ITERS or rounds_used >= TOTAL_CAP
        if act == "SHIP":
            verdict = "SHIP-CAPPED" if action.get("reason") == "budget_exhausted" else "SHIP"
            break
        if at_cap:
            verdict = "SHIP-CAPPED"; break
        if act == "ITERATE-REPLAN" and N2 > 0:
            N2 -= 1
            pr = run_role(spec=specs["planner"], model=role_models["planner"],
                          backend=role_backends["planner"],
                          user_prompt=plan_prompt + f"\n\nPRIOR ATTEMPT FAILED. Evaluator said:\n{last_eval_text}",
                          cwd=repo, max_turns=MAX_TURNS_PLAN, dry_run=dry_run)
            rounds_used += 1; plan_text = pr["response"]
        elif act == "ITERATE-EXECUTOR" and N1 > 0:
            N1 -= 1
        else:
            verdict = "SHIP-CAPPED"; break
        iteration += 1

    final_resolved = bool(rounds[-1]["oracle"]["resolved"])
    iterations = iteration + 1          # number of exec->oracle->eval passes actually run

    # IMPROVEMENT C: local-first / cloud-fallback. When the LOCAL executor failed fast (timeout|stuck) and
    # LFAH_CLOUD_HANDOFF=1, hand the SAME plan to a CLOUD model (escalate the hard bug the free local tier
    # can't do). final_resolved (local's honest result) is UNCHANGED; the cloud outcome lands in a SEPARATE
    # `handoff` field (model + resolved + wall + $).
    handoff = None
    le = rounds[-1].get("executor") or {}
    if (CLOUD_HANDOFF and not dry_run and role_backends["executor"] == "local"
            and le.get("soft_error") in ("timeout", "stuck") and not rounds[-1]["oracle"]["resolved"]):
        reset_repo(repo, instance["base_commit"])           # clean slate: measure cloud's standalone ability
        handoff_prompt = (
            f"TASK (repo: {instance['repo']}):\n\n{problem}\n\n"
            f"PLANNER'S PLAN:\n{plan_text}\n\n"
            f"The repo is checked out in your working directory. Make the change with your tools (Edit/Write), "
            f"self-check with Bash, ensure the fix is on disk. The target failing test is: {f2p}.")
        he = run_role(spec=specs["executor"], model=CLOUD_HANDOFF_MODEL, backend="cloud",
                      user_prompt=handoff_prompt, cwd=repo, max_turns=MAX_TURNS_EXEC)
        rounds_used += 1
        h_diff = git_diff(repo); (repo.parent / "patch.handoff.diff").write_text(h_diff)
        h_oracle = oracle_fn(iid, repo.parent / "patch.handoff.diff", f"rk-{iid}-handoff-{int(time.time())}")
        handoff = {"trigger": le.get("soft_error"), "model_requested": CLOUD_HANDOFF_MODEL,
                   "model_resolved": he.get("model_resolved"), "backend": "cloud",
                   "resolved": bool(h_oracle["resolved"]), "wall_s": round(he.get("wall_s", 0.0), 1),
                   "cost_usd": he.get("cost_usd"), "output_tps": he.get("output_tps"),
                   "tps_source": he.get("tps_source"), "diff_bytes": len(h_diff),
                   "input_tokens": he.get("input_tokens"), "output_tokens": he.get("output_tokens"),
                   "soft_error": he.get("soft_error")}

    return {"instance_id": iid, "mode": mode, "category": profile.get("category"),
            "oracle_wrapper": profile["oracle"]["wrapper"],   # so faithfulness checks the REAL wrapper, not a hardcoded one
            "verdict": verdict, "final_resolved": final_resolved, "rounds_used": rounds_used,
            "iterations": iterations, "max_iters": MAX_ITERS, "precode_gate": precode,
            "lessons_lookup": {"topic": lessons_topic, "n_chars": len(lessons_lessons),
                             "injected": bool(lessons_lessons)},
            "rounds": rounds, "role_models": role_models, "role_backends": role_backends,
            "handoff": handoff}


# ---------------------------------------------------------------------------
# Faithfulness assertions (the 6 axes) -- the smoke's pass/fail gate
# ---------------------------------------------------------------------------
def assert_faithful(result: dict) -> dict:
    checks, models = {}, result["role_models"]
    checks["heterogeneous_models"] = len(set(models.values())) >= 2
    checks["evaluator_ne_executor"] = models["evaluator"] != models["executor"]
    exec_tool_uses, eval_tool_uses = 0, 0
    eval_ran_test = False
    wrapper = result.get("oracle_wrapper", "eval_patch.sh")   # the profile's verifier (per-language)
    for rd in result["rounds"]:
        exec_tool_uses += len(rd["executor"]["tool_uses"])
        for tu in rd["evaluator"]["tool_uses"]:
            eval_tool_uses += 1
            if tu["name"] == "Bash" and wrapper in json.dumps(tu.get("input", {})):
                eval_ran_test = True
    checks["executor_used_tools"] = exec_tool_uses >= 1
    checks["evaluator_executed_real_test"] = eval_ran_test
    # cap is per-ITERATION (MAX_ITERS = 1 + N1 + N2). A run is faithful iff its iteration count is within budget.
    iters = result.get("iterations", result["rounds_used"])
    max_iters = result.get("max_iters", TOTAL_CAP)
    checks["capped_<=max_iters"] = iters <= max_iters
    # ship-broken: a SHIP verdict whose oracle says unresolved is allowed ONLY at the iteration cap.
    last = result["rounds"][-1]
    shipped_broken = (result["verdict"] == "SHIP" and not last["oracle"]["resolved"]
                      and iters < max_iters)
    checks["no_premature_ship_of_broken"] = not shipped_broken
    return {"checks": checks, "all_pass": all(checks.values()),
            "exec_tool_uses": exec_tool_uses, "eval_tool_uses": eval_tool_uses}


# ---------------------------------------------------------------------------
# Three-axis telemetry: performance (output TPS) + cost (tokens/$) + quality (pass@1)
# per role, so the chain records & monitors where to optimize. cost.by_role_ranked ranks roles by
# total tokens -- the highest-token role is the obvious place to swap a cheaper model.
# ---------------------------------------------------------------------------
ROLE_KEYS = ("planner", "executor", "evaluator")


def compute_telemetry(result: dict) -> dict:
    per_role = {}
    for rd in result["rounds"]:
        for rk in ROLE_KEYS:
            r = rd.get(rk)
            if not r:                       # planner present only on iter 0
                continue
            agg = per_role.setdefault(rk, {"input_tokens": 0, "output_tokens": 0,
                "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
                "cost_usd": 0.0, "wall_s": 0.0, "duration_api_ms": 0, "tool_uses": 0, "calls": 0,
                "model_resolved": "", "tps_source": "api", "gen_chars": 0})
            for k in ("input_tokens", "output_tokens", "cache_read_input_tokens",
                      "cache_creation_input_tokens", "duration_api_ms"):
                agg[k] += r.get(k, 0) or 0
            agg["cost_usd"] += r.get("cost_usd", 0.0) or 0.0
            agg["wall_s"] += r.get("wall_s", 0.0) or 0.0
            agg["tool_uses"] += len(r.get("tool_uses", []))
            agg["calls"] += 1
            if r.get("model_resolved"):              # the concrete model id the provider actually ran
                agg["model_resolved"] = r["model_resolved"]
            agg["gen_chars"] += r.get("gen_chars", 0) or 0
            if r.get("tps_source"):                  # "api" (exact) vs "est-chars/..." (local estimate)
                agg["tps_source"] = r["tps_source"]
    for rk, agg in per_role.items():
        # effective throughput: output tokens per WALL second -- same for cloud + local
        agg["output_tps"] = round(agg["output_tokens"] / agg["wall_s"], 2) if agg["wall_s"] > 0 else 0.0
        agg["total_tokens"] = agg["input_tokens"] + agg["output_tokens"]
    by_role_ranked = [{"role": rk, "total_tokens": agg["total_tokens"],
                       "output_tokens": agg["output_tokens"], "cost_usd": round(agg["cost_usd"], 4)}
                      for rk, agg in sorted(per_role.items(),
                                            key=lambda kv: kv[1]["total_tokens"], reverse=True)]
    rounds = result["rounds"]
    resolved = bool(rounds[-1]["oracle"]["resolved"])
    resolved_iter0 = bool(rounds[0]["oracle"]["resolved"])
    quality = {
        "pass_at_1": resolved,                              # oracle resolved at the end
        "pass_first_try": resolved_iter0,                   # resolved on iteration 0
        "rescue": resolved and not resolved_iter0,          # resolved only after an iterate
        "evaluator_verdict": ((rounds[-1].get("evaluator") or {}).get("response", "") or "")[:200],
        "iterations": result.get("iterations"),
    }
    rm = result.get("role_models", {}); rb = result.get("role_backends", {})
    # which model was called for each role: alias requested vs the CONCRETE id the provider ran
    # (aliases resolve at runtime -> store both).
    models = {rk: {"requested": rm.get(rk), "resolved": agg.get("model_resolved", ""),
                   "backend": rb.get(rk)} for rk, agg in per_role.items()}
    return {
        "models": models,
        "performance": {rk: {"output_tps": agg["output_tps"], "tps_source": agg.get("tps_source", "api"),
                             "wall_s": round(agg["wall_s"], 1), "gen_chars": agg.get("gen_chars", 0),
                             "model_requested": rm.get(rk), "model_resolved": agg.get("model_resolved", ""),
                             "backend": rb.get(rk)}
                        for rk, agg in per_role.items()},
        "cost": {
            "by_role": {rk: {k: agg[k] for k in ("input_tokens", "output_tokens",
                        "cache_read_input_tokens", "cache_creation_input_tokens")} |
                        {"cost_usd": round(agg["cost_usd"], 4)} for rk, agg in per_role.items()},
            "by_role_ranked": by_role_ranked,               # highest-token role first = where to optimize
            "chain_total_tokens": sum(a["total_tokens"] for a in per_role.values()),
            "chain_output_tokens": sum(a["output_tokens"] for a in per_role.values()),
            "chain_total_cost_usd": round(sum(a["cost_usd"] for a in per_role.values()), 4),
        },
        "quality": quality,
        "chain_wall_s": round(sum(a["wall_s"] for a in per_role.values()), 1),
    }


def record_run_data(result: dict, profile: dict) -> dict:
    """Optional: append a granular PER-ROLE record to skills/<role>-specialist/runs/data.json after each
    chain run -- the per-unit signal a downstream self-improvement loop can read. Bucketed per category so
    a code-fix planner is improved only from code-fix runs. Best-effort: never raises into the chain.
    Off unless RELAY_SAVE_LEARNINGS=1 (callers gate it; see CLI)."""
    import datetime
    written = {}
    tel = result.get("telemetry") or {}
    perf = tel.get("performance", {})
    cost_by_role = (tel.get("cost") or {}).get("by_role", {})
    recipes = profile.get("recipes", {})
    models = result.get("role_models", {})
    backends = result.get("role_backends", {})
    iid = result.get("instance_id")
    category = result.get("category")
    quality = tel.get("quality", {})
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    chain_outcome = {
        "resolved": bool(quality.get("pass_at_1")),
        "evaluator_verdict": quality.get("evaluator_verdict", ""),
        "verdict": result.get("verdict"),
        "iterations": result.get("iterations"),
        "pass_first_try": quality.get("pass_first_try"),
        "rescue": quality.get("rescue"),
    }
    for role in REQUIRED_ROLES:
        skill = recipes.get(role) or f"{role}-specialist"
        # most recent round (or the pre-code gate) carrying this role's own output, for the summary
        role_obj = None
        for rd in reversed(result.get("rounds", [])):
            if rd.get(role):
                role_obj = rd[role]; break
        if role_obj is None and role == "evaluator":
            role_obj = (result.get("precode_gate") or {}).get("eval")
        c = cost_by_role.get(role, {})
        pf = perf.get(role, {})
        rec = {
            "timestamp": ts, "category": category, "instance_id": iid, "role": role,
            "model": models.get(role), "model_requested": models.get(role),
            "model_resolved": pf.get("model_resolved") or (role_obj or {}).get("model_resolved", ""),
            "backend": backends.get(role),
            "output_summary": ((role_obj or {}).get("response") or "")[:200],
            "tool_uses": len((role_obj or {}).get("tool_uses", [])),
            "input_tokens": c.get("input_tokens"), "output_tokens": c.get("output_tokens"),
            "output_tps": pf.get("output_tps"), "cost_usd": c.get("cost_usd"),
            "wall_s": pf.get("wall_s"), "chain_outcome": chain_outcome,
        }
        data_path = SKILLS_DIR / skill / "runs" / "data.json"
        try:
            data_path.parent.mkdir(parents=True, exist_ok=True)
            data = (json.loads(data_path.read_text()) if data_path.exists()
                    else {"skill": skill, "lastRun": None, "totalRuns": 0, "runs": []})
            data["runs"].append(rec)
            data["runs"] = data["runs"][-50:]
            data["totalRuns"] = data.get("totalRuns", 0) + 1
            data["lastRun"] = ts
            data_path.write_text(json.dumps(data, indent=2, default=str))
            written[role] = str(data_path)
        except Exception as ex:
            written[role] = f"ERROR: {ex}"
    return written


def save_learnings(result: dict, profile: dict) -> dict:
    """Optional: write a one-line summary card per chain run to a local notes directory (topic
    `relay-runs`). DETERMINISTIC -- no extra LLM call. Off unless RELAY_SAVE_LEARNINGS=1; best-effort
    (never raises into the chain). The notes directory is RELAY_NOTES_DIR (default ./runs/notes)."""
    import datetime
    saved = {"card": None}
    iid = result.get("instance_id"); category = result.get("category")
    verdict = result.get("verdict"); resolved = result.get("final_resolved")
    iters = result.get("iterations"); max_iters = result.get("max_iters")
    faith = (result.get("faithfulness") or {}).get("all_pass")
    tel = result.get("telemetry") or {}
    ranked = (tel.get("cost") or {}).get("by_role_ranked") or []
    top_role = ranked[0]["role"] if ranked else "?"
    cost = (tel.get("cost") or {}).get("chain_total_cost_usd")
    q = tel.get("quality") or {}
    pre = result.get("precode_gate") or {}
    precode = ((pre.get("verdict") or "").splitlines() or [""])[0][:60]
    eval_v = ((q.get("evaluator_verdict") or "").splitlines() or [""])[0][:60]
    date = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    tel_models = tel.get("models", {})
    models_str = "; ".join(f"{rk}={m.get('requested')}/{m.get('backend')}->{m.get('resolved') or '?'}"
                           for rk, m in tel_models.items())
    try:
        notes_dir = Path(os.environ.get("RELAY_NOTES_DIR", str(Path.cwd() / "runs" / "notes")))
        notes_dir.mkdir(parents=True, exist_ok=True)
        card_id = f"relay-run-{iid}-{date}"
        ranked_str = ", ".join(f"{x['role']}={x['total_tokens']}tok" for x in ranked)
        card = (
            f"# relay {category} {iid}: {verdict} resolved={resolved} ({iters}/{max_iters} iters, ${cost})\n\n"
            f"- models (role = requested/backend -> resolved): {models_str}\n"
            f"- outcome: verdict={verdict}, resolved={resolved}, iterations={iters}/{max_iters}, "
            f"faithful={faith} (pass_first_try={q.get('pass_first_try')}, rescue={q.get('rescue')})\n"
            f"- cost: chain ${cost}; per-role ranked by tokens = {ranked_str}; top role = {top_role}\n"
            f"- pre-code plan gate: {precode or '(n/a)'}; final evaluator: {eval_v or '(n/a)'}\n")
        card_path = notes_dir / f"{card_id}.md"
        card_path.write_text(card)
        saved["card"] = str(card_path)
    except Exception as ex:
        saved["card"] = f"ERROR: {ex}"
    return saved
