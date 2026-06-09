# Contributing to local-first-agent-harness

Thanks for your interest in contributing!

## Brand voice — name, pronunciation & video/narration intro

When producing ANY spoken content about this project (videos, voiceovers, demos,
conference talks), follow these conventions so the brand sounds the same everywhere:

- **Pronunciation: "lfah" is pronounced "alpha".** In a voiceover or text-to-speech
  script, write the SPOKEN form as `Alpha` so it is voiced "alpha" — but keep the
  DISPLAYED text (captions, on-screen titles, slides) as `lfah`. So: spoken = "Alpha",
  written = `lfah`. If a single string drives both audio and on-screen text, split them.
- **Always expand the name in the intro, then hook.** The first thing the audience
  hears is that **lfah is short for "local-first-agent-harness"** — and then,
  immediately, a HOOK on why having one is amazing: the with-vs-without contrast.
  *Without* it, every bug fix burns expensive cloud tokens on one-shot guesses;
  *with* it, the heavy work runs FREE on your own local model and only escalates to the
  cloud when it's genuinely stuck — graded by real tests, matching cloud quality at
  roughly half the cost.
- **Stay honest.** The full-cloud relay is still the raw-quality ceiling (highest
  resolve %); recommend lfah on VALUE (cost-per-fix + a free local executor), never as
  "best at everything". See the README's "honest, axis by axis" section.

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

## Releases

Releases are cut by the `/ship` pipeline (Stage 7) — the same client-side flow
every repo in this fleet uses. There is **no** tag-triggered release Action;
`/ship` owns releases end to end, so the flow is identical across repos.

Because this is a Python package (it has a `pyproject.toml`, not a
`package.json`), `/ship` Stage 7 sees no `package.json` and **skips the automatic
version bump** — versioning is **manual**. To cut a release:

1. Bump `version` in `pyproject.toml` and move the `## Unreleased` notes in
   `CHANGELOG.md` under a new `## [X.Y.Z]` heading (the changelog is the single
   source of truth for "what changed").
2. Merge that via a normal PR.
3. Tag the merge commit and create the GitHub Release from the changelog section:

   ```bash
   git tag vX.Y.Z <merge-sha>
   git push origin vX.Y.Z
   gh release create vX.Y.Z --title vX.Y.Z \
     --notes "$(awk '/^## \[X.Y.Z\]/{g=1;next} g&&/^## /{exit} g' CHANGELOG.md)"
   ```

   Keep the tag and the `pyproject.toml` version in sync (e.g. tag `v0.2.0` ⇔
   `version = "0.2.0"`).

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
