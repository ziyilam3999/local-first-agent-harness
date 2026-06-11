# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Releases are cut client-side by the `/ship` pipeline (Stage 7), which pulls the
section for a tag `vX.Y.Z` out of this file as the GitHub Release notes — so this
changelog is the single source of truth for "what changed". See `CONTRIBUTING.md`
for the release steps.

## Unreleased

## [0.3.0] (2026-06-11)

### Added

* **Red-test mutation gate wired into `build-run` (#653 slice 2 / #831)** — `build.run_phase`
  now runs the slice-1 mutation gate BEFORE committing a phase's agent-authored RED test. A
  phase is "agent-authored" when it carries `picks` + `reference` + `wrong_stubs` inline in the
  build manifest; such a phase's test must discriminate every (near-correct) wrong-stub from the
  reference or the phase is REFUSED (`authortest.GateRefusal`, naming the failing mutant) and the
  build halts before the bad test is ever committed. Human-supplied phases (no agent inputs) skip
  the gate entirely — existing builds are unchanged.
* The fresh-eyes reviewer is now **BLOCKING** in the build path (and, by default, in the standalone
  `lfah author-test gate` CLI — `--advisory-reviewer` restores the slice-1 advisory behavior): a
  non-PASS verdict refuses the gate. Reuses the existing `relay.run_role` + `relay.jest_oracle_eval`
  primitives only — no new chain roles, no `relay.py` changes.
* The per-phase BUILD-SUMMARY record gains `picks` / `reference` / `wrong_stubs` (plus a `gate_log`
  pointer and `gate_discriminates`) so there is a durable paper-trail of what the gate checked
  (additive — human phases carry `null`).

### Changed

* `authortest.gate(...)` gains a `block_on_reviewer` flag and writes `refused` / `refusal_reason`
  into the gate-log; a new `authortest.gate_phase_test(...)` is the build-run entrypoint (raises on
  any refusal). Fail-fast: a reviewer model equal to the author model is now rejected BEFORE the
  expensive mutation gate runs (previously only after jest ×2). `parse_author_response` now raises a
  clear `ValueError` (not a cryptic `AttributeError`) when a `wrong_stubs` entry is not an object.

## [0.2.0] (2026-06-11)

### Added

* **Safe agent-authored red tests via a mutation gate (#653 slice 1)** — a new
  `lfah author-test` command (`derive` → approve → `gate`). An independent cloud
  author writes a failing acceptance test + a reference impl + one near-correct
  wrong-stub per must-REJECT pick from the operator's multiple-choice picks **only**
  (never executor code), emits an ELI5, and stops for approval. The mutation gate then
  runs `relay.jest_oracle_eval` ×2 in a fresh throwaway scaffold — the test must go RED
  against each wrong-stub and GREEN against the reference (a test that can't tell them
  apart is rejected as a fake oracle), with a near-correct-mutant surface guard and an
  advisory fresh-eyes reviewer. Reuses existing engine primitives only (no new chain
  roles, no `relay.py` changes). Adds `redtest-author-specialist` +
  `redtest-review-specialist` recipes and a committed gate-log proof on the copy phase
  (`results/AUTHORTEST-copy.json`, `discriminates:true` from a real jest run).
* CI workflow (`ci.yml`): ruff lint + pytest on a Linux/macOS × Python 3.10/3.12
  matrix, plus a Conventional Commits check on pushes to `main`.
* `CONTRIBUTING.md`, issue templates (bug report + feature request), and a PR
  template.
* `[tool.ruff]` config and a `dev` optional-dependency group (`pip install -e .[dev]`).
* README: badges, an architecture diagram, and an expanded local-model setup section.
* `CONTRIBUTING.md` "Brand voice" section: lfah is pronounced "alpha" (spoken `Alpha`,
  written `lfah`); spoken content must expand "local-first-agent-harness" in the intro
  and lead with the with-vs-without hook. So future content is consistent.

### Changed

* README install instructions point at `pip install git+https://…` until a PyPI
  release is published.
* Standardized the release flow on the fleet's client-side `/ship` Stage 7 — dropped
  the stray tag-triggered `release.yml` GitHub Action so releases are cut the same
  way as every other repo. Versioning stays manual (this is a Python package with no
  `package.json`, so Stage 7 skips the auto version bump); see `CONTRIBUTING.md`.

## [0.1.0] (2026-06-01)

First public release.

### Added

* **relay engine** — a three-role coding chain (planner → executor → evaluator)
  driven by a free Python orchestrator that makes a deterministic Ship/Iterate
  decision, with per-iteration stuck-detection.
* **local-first execution** — run the heavy executor role on a local model (via an
  Ollama-compatible proxy) with optional cloud fallback when the local tier gets
  stuck; the honest local outcome is preserved separately from the fallback.
* **honest grading** — candidate patches are scored by the canonical SWE-bench
  Docker oracle (`FAIL_TO_PASS` real tests), never by an LLM grading itself.
* **`lfah` CLI** — `lfah run --instance <path.json>` with `--local`, `--dry-run`,
  per-role model/backend flags, and a per-role telemetry table.
* **bundled role files** — `codefix-{plan,execute,evaluate}-specialist` skills and
  agent definitions shipped inside the package.
* Early 4-arm results (n=13, SWE-bench Verified) in the README.
