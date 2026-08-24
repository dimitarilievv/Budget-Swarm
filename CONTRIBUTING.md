# Contributing

Code-quality tooling for BudgetSwarm (BS-017). CI runs the same checks on every
push and pull request — run them locally first to keep the build green.

## Python (`backend/`, `ai_engine/`)

- **Ruff** — linting (`ruff check .`)
- **Black** — formatting (`black .`)

```bash
cd backend
pip install -r requirements-dev.txt
ruff check ..          # lint the whole repo
black --check ..       # verify formatting
pytest -q              # run tests
```

Config lives in the root `pyproject.toml`.

## Frontend (`frontend/`)

- **ESLint** — linting (`npm run lint`)
- **Prettier** — formatting (`npm run format` / `npm run format:check`)

```bash
cd frontend
npm ci
npm run lint
npm run format:check
npm test
npm run build
```

## Pre-commit hooks

Hooks run Ruff, Black, Prettier, and basic file checks before each commit.

```bash
pip install pre-commit
pre-commit install                 # enable the git hook
pre-commit run --all-files         # run against the whole tree once
```

Config: `.pre-commit-config.yaml`.

## Continuous integration

`.github/workflows/ci.yml` defines two jobs — **backend** (ruff + black + pytest)
and **frontend** (eslint + prettier + vitest + build) — triggered on push and PR.
