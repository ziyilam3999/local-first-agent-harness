# Contributing to local-first-agent-harness

Thanks for your interest in contributing!

## Getting started

```bash
git clone https://github.com/ziyilam3999/local-first-agent-harness
cd local-first-agent-harness
python -m venv .venv && source .venv/bin/activate
pip install -e .[dev]        # package (no swebench needed for unit tests) + ruff + pytest
```

## Development

```bash
ruff check src/ tests/       # lint (pyflakes correctness; compact-style E rules are ignored)
pytest tests/ -q             # unit tests (import, role bundle, orchestrator rule-table)
```

The unit tests deliberately do **not** require the `swebench` package — it is only
invoked as a subprocess at oracle-grading time. Install it (`pip install -e .`, no
`--no-deps`) plus a running Docker daemon if you want to exercise real grading.

## Running the harness end to end

See the README for the cloud-only quickstart and the local-first mode. `lfah run
--dry-run` exercises the full chain wiring without calling any model or the oracle —
useful as a fast smoke before a real run.

## Submitting changes

1. Branch off `main`: `git checkout -b fix/your-change`
2. Make the change and add or update a test.
3. Make sure `ruff check` and `pytest` both pass.
4. Use a [Conventional Commits](https://www.conventionalcommits.org/) message
   (`feat(relay): …`, `fix(cli): …`, `docs: …`) — CI checks this on `main`.
5. Add a bullet under `## Unreleased` in `CHANGELOG.md`.
6. Open a pull request.

## Guidelines

- Keep PRs focused on a single change.
- The relay engine (`src/lfah/relay.py`) is the load-bearing core — prefer small,
  well-tested changes there and explain the why in the PR.
- Anything that couples to a machine (paths, ports, model ids, timeouts) should be an
  environment variable with a safe default, not a hard-coded value. See the
  Configuration table in the README.

## Bug reports

Open an issue with: steps to reproduce, expected vs actual behaviour, your OS, and
your Python version.
