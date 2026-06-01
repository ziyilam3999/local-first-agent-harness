# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

### Added

* CI workflow (`ci.yml`): ruff lint + pytest on a Linux/macOS × Python 3.10/3.12
  matrix, plus a Conventional Commits check on pushes to `main`.
* Release workflow (`release.yml`): pushing a `v*` tag cuts a GitHub Release whose
  body is extracted from this changelog, guarded so the tag must match the
  `pyproject.toml` version.
* `CONTRIBUTING.md`, issue templates (bug report + feature request), and a PR
  template.
* `[tool.ruff]` config and a `dev` optional-dependency group (`pip install -e .[dev]`).
* README: badges, an architecture diagram, and an expanded local-model setup section.

### Changed

* README install instructions point at `pip install git+https://…` until a PyPI
  release is published.

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
