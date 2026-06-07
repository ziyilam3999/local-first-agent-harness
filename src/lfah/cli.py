"""`lfah` console entrypoint — run the 3-agent local-first coding chain on one instance.

Default = cloud-only easy mode (all three roles run on the cloud). Pass --local to run the
heavy executor on a local model and escalate to the cloud only when it gets stuck.
"""
from __future__ import annotations
import argparse
import json
import os
import sys
from pathlib import Path

from . import __version__
from . import relay


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="lfah",
        description="A local-first coding agent: plan -> execute -> evaluate, graded by real tests.",
    )
    ap.add_argument("--version", action="version", version=f"lfah {__version__}")
    sub = ap.add_subparsers(dest="command")

    run = sub.add_parser("run", help="Run the chain on one SWE-bench-style instance.")
    run.add_argument("--instance", required=True,
                     help="Path to an instance JSON (instance_id, repo, base_commit, "
                          "problem_statement, FAIL_TO_PASS).")
    run.add_argument("--planner", default="opus", help="Planner model (default: opus).")
    run.add_argument("--executor", default="sonnet", help="Executor model (default: sonnet).")
    run.add_argument("--evaluator", default="opus", help="Evaluator model (default: opus).")
    run.add_argument("--executor-backend", choices=["cloud", "local"], default="cloud",
                     help="Where the executor runs (default: cloud).")
    run.add_argument("--local-model", default=None,
                     help="Local model name to use as the executor when running local-first.")
    run.add_argument("--cloud-fallback", default=None,
                     help="Cloud model to escalate to when a local executor times out or gets stuck.")
    run.add_argument("--mode", choices=["a", "c"], default="c",
                     help="'a' = no replan (1 executor round); 'c' = 1 replan + 1 exec retry (default).")
    run.add_argument("--out", default=None, help="Directory to write the result JSON into.")
    run.add_argument("--local", action="store_true",
                     help="Convenience: executor-backend=local, executor=<local-model>, "
                          "enable cloud fallback.")
    run.add_argument("--dry-run", action="store_true",
                     help="Exercise the chain wiring without calling models or the oracle.")

    build = sub.add_parser("build", help="Greenfield BUILD: drive a from-scratch app build as test-first "
                                         "phases through the chain (scaffold -> per-phase red test -> chain "
                                         "-> commit-on-SHIP), with the `both` ship-gate by default.")
    build.add_argument("--manifest", required=True,
                       help="Path to a build manifest JSON (project_name, language, phases[]). See lfah.build.")
    build.add_argument("--project", required=True, help="Directory for the evolving project (created/reset).")
    build.add_argument("--data", required=True, help="LFAH_DATA_DIR root for per-phase instance dirs.")
    build.add_argument("--out", required=True, help="Directory for per-phase results + BUILD-SUMMARY.json.")
    build.add_argument("--loop-signal", default="both", choices=["oracle", "evaluator", "both"],
                       help="Ship gate (default: both — the phase test is OUR AC, not ground truth).")
    build.add_argument("--no-npm-install", action="store_true",
                       help="Skip `npm install` during scaffold (deps already present / non-JS).")
    return ap


def _resolve_models(args) -> tuple[dict, dict]:
    """Build the per-role model + backend maps from the parsed CLI args, applying --local."""
    planner = args.planner
    executor = args.executor
    evaluator = args.evaluator
    executor_backend = args.executor_backend

    if args.local:
        # Convenience: run the heavy executor on a local model, evaluator stays cloud (so evaluator !=
        # executor AND only one local model is ever resident), and enable cloud fallback.
        executor_backend = "local"
        local_model = args.local_model or "qwen3-coder"
        executor = local_model
        if not args.cloud_fallback:
            args.cloud_fallback = "sonnet"
    elif executor_backend == "local":
        # Explicit --executor-backend local without --local: honor --local-model if given.
        if args.local_model:
            executor = args.local_model

    role_models = {"planner": planner, "executor": executor, "evaluator": evaluator}
    role_backends = {
        "planner": "cloud",
        "executor": executor_backend,
        "evaluator": "cloud",
    }
    return role_models, role_backends


def _print_telemetry(result: dict) -> None:
    tel = result.get("telemetry") or {}
    cost = tel.get("cost", {})
    perf = tel.get("performance", {})
    print("\nthree-axis telemetry (roles ranked by total tokens):")
    print(f"  {'role':<11}{'out_tps':>9}{'in_tok':>9}{'out_tok':>9}{'cost_usd':>10}{'wall_s':>8}"
          f"  {'model (requested -> resolved)'}")
    for entry in cost.get("by_role_ranked", []):
        rk = entry["role"]
        c = cost.get("by_role", {}).get(rk, {})
        p = perf.get(rk, {})
        mdl = f"{p.get('model_requested')}/{p.get('backend')} -> {p.get('model_resolved') or '(n/a)'}"
        print(f"  {rk:<11}{p.get('output_tps', 0):>9}{c.get('input_tokens', 0):>9}"
              f"{c.get('output_tokens', 0):>9}{c.get('cost_usd', 0):>10}{p.get('wall_s', 0):>8}  {mdl}")
    q = tel.get("quality", {})
    print(f"  chain totals: tokens={cost.get('chain_total_tokens')} "
          f"out_tokens={cost.get('chain_output_tokens')} "
          f"cost_usd={cost.get('chain_total_cost_usd')} wall_s={tel.get('chain_wall_s')}")
    print(f"  quality: pass@1={q.get('pass_at_1')} pass_first_try={q.get('pass_first_try')} "
          f"rescue={q.get('rescue')} iterations={q.get('iterations')}")
    if cost.get("by_role_ranked"):
        print(f"  -> highest-token role (optimize here): {cost['by_role_ranked'][0]['role']}")
    handoff = result.get("handoff")
    if handoff:
        print(f"\ncloud fallback fired ({handoff.get('trigger')}): "
              f"model={handoff.get('model_requested')} resolved={handoff.get('resolved')} "
              f"wall_s={handoff.get('wall_s')} cost_usd={handoff.get('cost_usd')}")


def _run(args) -> int:
    instance_path = Path(args.instance).expanduser()
    if not instance_path.exists():
        print(f"error: instance file not found: {instance_path}", file=sys.stderr)
        return 2
    instance = json.loads(instance_path.read_text())

    role_models, role_backends = _resolve_models(args)

    # The executor's repo checkout lives next to the instance file by default: a directory named
    # <instance_id>/repo, OR a sibling `repo/`. The chain edits real files there.
    repo = instance_path.parent / "repo"
    if not repo.exists():
        alt = instance_path.parent / instance["instance_id"] / "repo"
        if alt.exists():
            repo = alt

    # Wire up the optional cloud fallback through the engine's env-var switches.
    if args.cloud_fallback:
        os.environ["LFAH_CLOUD_HANDOFF"] = "1"
        os.environ["LFAH_CLOUD_HANDOFF_MODEL"] = args.cloud_fallback

    profile = relay.select_profile(instance)   # language axis: javascript -> jest, else pytest codefix
    gate = relay.assert_profile_complete(profile, role_models, role_backends)

    print(f"=== lfah run: category={profile['category']} mode={args.mode} "
          f"instance={instance['instance_id']} ===")
    print(f"profile completeness gate: ALLOW {gate}")
    print(f"role_models={role_models}")
    print(f"role_backends={role_backends}")
    if not args.dry_run and not repo.exists():
        print(f"warning: no repo checkout found at {repo} — the executor needs a checked-out repo "
              f"to edit. Run with --dry-run to exercise the wiring, or provide a repo/ dir next to "
              f"the instance.", file=sys.stderr)

    result = relay.run_chain(instance=instance, repo=repo, role_models=role_models,
                             role_backends=role_backends, mode=args.mode, profile=profile,
                             dry_run=args.dry_run)
    result["faithfulness"] = relay.assert_faithful(result)
    result["telemetry"] = relay.compute_telemetry(result)

    if not args.dry_run and os.environ.get("RELAY_SAVE_LEARNINGS", "0") != "0":
        result["run_data_written"] = relay.record_run_data(result, profile)
        result["learnings_saved"] = relay.save_learnings(result, profile)

    out_dir = Path(args.out).expanduser() if args.out else (Path.cwd() / "runs")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"lfah-{instance['instance_id']}-{args.mode}.json"
    out_file.write_text(json.dumps(result, indent=2, default=str))

    faith = result["faithfulness"]
    print(f"\nverdict={result['verdict']} final_resolved={result['final_resolved']} "
          f"rounds_used={result['rounds_used']} iterations={result['iterations']}/{result['max_iters']}")
    print("faithfulness checks:")
    for k, v in faith["checks"].items():
        print(f"  [{'PASS' if v else 'FAIL'}] {k}")
    print(f"ALL_FAITHFUL={'YES' if faith['all_pass'] else 'NO'}  "
          f"(exec_tool_uses={faith['exec_tool_uses']}, eval_tool_uses={faith['eval_tool_uses']})")
    _print_telemetry(result)
    print(f"\nwritten: {out_file}")
    return 0 if faith["all_pass"] else 1


def _build(args) -> int:
    from . import build as buildmod
    manifest_path = Path(args.manifest).expanduser().resolve()
    if not manifest_path.exists():
        print(f"error: manifest not found: {manifest_path}", file=sys.stderr)
        return 2
    manifest = json.loads(manifest_path.read_text())
    summary = buildmod.run_build(
        manifest=manifest, project=Path(args.project).expanduser(),
        data=Path(args.data).expanduser(), out=Path(args.out).expanduser(),
        manifest_dir=manifest_path.parent, loop_signal=args.loop_signal,
        npm_install=not args.no_npm_install)
    print(f"=== lfah build: project={summary['project']} loop_signal={summary['loop_signal']} ===")
    for r in summary["phases"]:
        print(f"  phase {r['id']}: resolved={r['resolved']} by={r['solved_by']} "
              f"iters={r['iterations']} cost=${r['cost_usd']} -> {(r['committed'] or '—')[:10]}")
    print(f"pipeline_complete={summary['pipeline_complete']} "
          f"({summary['phases_shipped']}/{summary['phases_total']} shipped)")
    return 0 if summary["pipeline_complete"] else 1


def main(argv=None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "run":
        return _run(args)
    if args.command == "build":
        return _build(args)
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
