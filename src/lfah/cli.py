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
    build.add_argument("--fresh", action="store_true",
                       help="Force a clean wipe + re-scaffold of --project. Default REUSES an existing project "
                            "(builds the manifest's phases on top of its current HEAD, so successive builds "
                            "accumulate into the SAME folder).")

    # `author-test` (#653): safe agent-authored red tests via a mutation gate. Two modes mirror the
    # derive-then-approve-then-gate flow (the author writes the test BEFORE any code, the mutation gate
    # proves it discriminates a wrong stub from the reference, an advisory reviewer double-checks intent).
    at = sub.add_parser("author-test", help="Safe agent-authored red tests: derive a test+reference+wrong-"
                                            "stub(s) from operator picks, then prove the test discriminates "
                                            "via a mutation gate (jest_oracle_eval x2).")
    at_sub = at.add_subparsers(dest="at_mode")

    atd = at_sub.add_parser("derive", help="Author writes the red test + reference + wrong-stub(s) from the "
                                          "operator's picks ONLY (never executor code); emits an ELI5; STOPS "
                                          "for approval.")
    atd.add_argument("--picks", required=True, help="Path to picks.json (operator MCQ accept/reject + "
                                                    "example_table + spec/module/test_path/phase).")
    atd.add_argument("--out", required=True, help="Directory for the derive bundle (test, reference, "
                                                  "wrong-stubs, ELI5.md, derive.json).")
    atd.add_argument("--author-model", default="opus", help="Cloud author model (default: opus).")
    atd.add_argument("--executor-model", default="sonnet", help="The build's configured executor model, "
                                                               "recorded so author.model != executor_model is "
                                                               "meaningful (default: sonnet).")
    atd.add_argument("--reviewer-model", default="sonnet", help="Advisory reviewer model, != author (default: "
                                                              "sonnet).")
    atd.add_argument("--source", choices=["live-agent", "recorded-fallback"], default="live-agent",
                     help="live-agent = invoke the cloud author role; recorded-fallback = assemble from "
                          "on-disk reference/wrong-stub/test (when the live author is unreachable).")
    atd.add_argument("--reference", default=None, help="(recorded-fallback) reference module file.")
    atd.add_argument("--wrong-stub", action="append", default=None,
                     help="(recorded-fallback) a wrong-stub module file; repeat for >1.")
    atd.add_argument("--agent-test", default=None, help="(recorded-fallback) the red acceptance test file.")
    atd.add_argument("--dry-run", action="store_true", help="Exercise wiring without calling the model.")

    atg = at_sub.add_parser("gate", help="Run the mutation gate (jest_oracle_eval x2) + advisory reviewer "
                                        "on an approved derive bundle; write results/AUTHORTEST-<phase>.json.")
    atg.add_argument("--bundle", required=True, help="The derive bundle dir (must contain derive.json).")
    atg.add_argument("--results", required=True, help="Directory to write the committed gate-log into.")
    atg.add_argument("--work", required=True, help="Scratch dir for the throwaway scaffold + jest runs.")
    atg.add_argument("--approved", action="store_true", help="Recorded operator approval (required to run).")
    atg.add_argument("--approval", default=None, help="Path to an approval.json carrying approved:true.")
    atg.add_argument("--node-modules", default=None, help="Optional node_modules dir to seed (copied) into "
                                                         "the scaffold so npm install is a near-noop/offline.")
    atg.add_argument("--dry-run", action="store_true", help="Stub the reviewer role.")
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
        npm_install=not args.no_npm_install, fresh=args.fresh)
    print(f"=== lfah build: project={summary['project']} loop_signal={summary['loop_signal']} ===")
    for r in summary["phases"]:
        print(f"  phase {r['id']}: resolved={r['resolved']} by={r['solved_by']} "
              f"iters={r['iterations']} cost=${r['cost_usd']} -> {(r['committed'] or '—')[:10]}")
    print(f"pipeline_complete={summary['pipeline_complete']} "
          f"({summary['phases_shipped']}/{summary['phases_total']} shipped)")
    return 0 if summary["pipeline_complete"] else 1


def _author_test(args) -> int:
    from . import authortest
    if args.at_mode == "derive":
        m = authortest.derive(
            picks_path=Path(args.picks).expanduser(), out_dir=Path(args.out).expanduser(),
            author_model=args.author_model, executor_model=args.executor_model,
            reviewer_model=args.reviewer_model, source=args.source,
            reference_path=Path(args.reference).expanduser() if args.reference else None,
            wrong_stub_paths=[Path(w).expanduser() for w in (args.wrong_stub or [])] or None,
            agent_test_path=Path(args.agent_test).expanduser() if args.agent_test else None,
            dry_run=args.dry_run)
        print(f"=== author-test derive: phase={m['phase']} source={m['source']} ===")
        print(f"author={m['author']['role']} model={m['author']['model']} (executor_model={m['executor_model']})")
        print(f"wrote bundle -> {Path(args.out).expanduser()} (derive.json, {m['files']['agent_test']}, "
              f"{m['files']['reference']}, {len(m['files']['wrong_stubs'])} wrong-stub(s), ELI5.md)")
        for ws in m["files"]["wrong_stubs"]:
            flag = "OK" if ws["surface_match"] else "SURFACE-MISMATCH"
            print(f"  wrong-stub {ws['label']}: surface={flag} ({ws['stub_exports']})")
        print("\nNEXT: surface ELI5.md to the operator for approval, then run `author-test gate --approved`.")
        return 0
    if args.at_mode == "gate":
        try:
            log = authortest.gate(
                bundle_dir=Path(args.bundle).expanduser(), results_dir=Path(args.results).expanduser(),
                work_dir=Path(args.work).expanduser(), approved=args.approved,
                approval_path=Path(args.approval).expanduser() if args.approval else None,
                node_modules_src=Path(args.node_modules).expanduser() if args.node_modules else None,
                dry_run=args.dry_run)
        except PermissionError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        print(f"=== author-test gate: phase={log['phase']} ===")
        print(f"discriminates={log['discriminates']}  "
              f"reference.resolved={log['mutants']['reference']['resolved']}  "
              f"wrong.resolved={log['mutants']['wrong']['resolved']}")
        print(f"author.model={log['author']['model']} != executor_model={log['executor_model']}  "
              f"reviewer={log['reviewer']['model']} verdict={log['reviewer']['verdict']}")
        print(f"\nwritten: {log['_gate_log_path']}")
        return 0 if log["discriminates"] else 1
    print("error: author-test needs a mode: derive | gate", file=sys.stderr)
    return 2


def main(argv=None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "run":
        return _run(args)
    if args.command == "build":
        return _build(args)
    if args.command == "author-test":
        return _author_test(args)
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
