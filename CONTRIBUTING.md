# Contributing

Thanks for your interest in SplitWave. Bug reports, ideas and pull requests are welcome.

## Before you start

- Look through the open issues; for a larger change, open an issue first so we can agree on the approach.
- The core in this repository is licensed under the Elastic License 2.0. Because SplitWave is a source-available product with paid editions, **we ask contributors to sign a Contributor License Agreement** before the first pull request is merged (a bot will guide you). The CLA lets the maintainers include your contribution in the core (under its current and future licences) and in the paid editions. You keep the ownership of your work.
- The paid modules are not developed in this repository and do not accept outside contributions.

## Development setup

```bash
cd control-plane
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest -q -n 4          # backend tests
```

The console is plain JavaScript without a build step (`control-plane/app/ui`). Browser tests:

```bash
cd ui-tests && npm ci && npx playwright install chromium && ./run.sh
```

## Guidelines

- Keep changes focused and add or update tests. The test suites must pass.
- The interface supports light, dark and system themes and two languages (English, Russian): update both translations. Use linear icons, no emoji, solid colours without transparency.
- Never log or return secret values; see `docs/security.md` for the model.
- Commit messages: a short imperative summary, for example "Fix rollout wait on missing secret".
