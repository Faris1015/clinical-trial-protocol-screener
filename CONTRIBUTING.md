# Contributing

Thanks for working on the Clinical Trial Protocol Screener. This document is the
single source of truth for how a change goes from idea → branch → PR → `main`.
Every contributor (including future you) follows the same rails.

## TL;DR

```bash
# 1. Branch from an up-to-date main
git switch main && git pull
git switch -c feat/<issue-number>-<slug>      # or fix/, chore/, docs/, test/

# 2. Make the change, then run exactly what CI runs
make check                                    # lint + typecheck + test

# 3. Push and open a PR that references the issue
git push -u origin HEAD
gh pr create --fill                           # body must contain "Closes #<n>"

# 4. CI green + 1 review → squash-merge with a conventional-commit title
```

## Local setup

You need Python 3.11+ and Node 22+. The backend and frontend are set up
independently.

### Backend

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"                       # note the [dev] extra for tooling
python -m app.data.generate_ehr              # generate synthetic patients
uvicorn app.main:app --reload --port 8000
```

Requires [Ollama](https://ollama.com) running locally with
`ollama pull qwen2.5:7b`, or set `ANTHROPIC_API_KEY` and
`LLM_PROVIDER=anthropic`. Copy `backend/.env.example` to `backend/.env` for the
authoritative list of configuration variables.

### Changing backend dependencies

Add or bump deps in `backend/pyproject.toml`, then regenerate the pinned lock
the Docker image builds from:

```bash
cd backend
pip-compile --output-file=requirements.lock --strip-extras pyproject.toml
```

Run this on **Python 3.11** — the same version as the runtime image (see the
`Dockerfile`). CI regenerates the lock on 3.11 and fails if it differs from the
committed one, so a lock produced on another Python version will be rejected. If
you don't have 3.11 handy, generate it in a container:

```bash
docker run --rm -v "$PWD":/w -w /w python:3.11-slim \
  sh -c "pip install pip-tools==7.5.3 && \
         pip-compile --output-file=requirements.lock --strip-extras pyproject.toml"
```

### Frontend

```bash
cd frontend
npm install
npm run dev                                   # http://localhost:5173
```

### Git hooks (recommended)

Install once — every commit then runs the same checks CI runs, on staged files:

```bash
pip install pre-commit && pre-commit install
```

## Branching

Branch from an up-to-date `main`. Never commit directly to `main`.

Branch names are `<type>/<issue-number>-<slug>`:

| Type     | Use for                                    | Example                          |
|----------|--------------------------------------------|----------------------------------|
| `feat/`  | new capability                             | `feat/14-version-control-workflow` |
| `fix/`   | bug fix                                    | `fix/23-sse-reconnect`           |
| `chore/` | tooling, deps, config, no product change   | `chore/upgrade-ruff`             |
| `docs/`  | documentation only                         | `docs/readme-quickstart`         |
| `test/`  | tests only                                 | `test/parser-golden-set`         |

## Making changes

Before pushing, run the same checks CI runs — this is a single entry point so
humans and CI run identical commands:

```bash
make lint        # ruff + eslint
make format      # ruff format + prettier
make typecheck   # mypy + tsc --noEmit
make test        # pytest
make check       # all of the above — exactly what CI gates on
```

Keep the change scoped to one issue. If you discover unrelated work, open a new
issue rather than expanding the PR.

## Pull requests

1. Open a PR against `main`. The [PR template](.github/pull_request_template.md)
   pre-fills automatically — fill in every section.
2. **Link the issue** in the body with a closing keyword: `Closes #14`. This
   auto-closes the issue on merge.
3. CI (`backend` + `frontend` jobs) must be green. See
   [`ci.yml`](.github/workflows/ci.yml).
4. One approving review is required. [CODEOWNERS](.github/CODEOWNERS) is
   requested automatically.
5. **Squash-merge** with a conventional-commit title (see below). Head branches
   are auto-deleted on merge.

### PR title = conventional commit

Because we squash-merge, the **PR title becomes the commit on `main`**. It must
follow [Conventional Commits](https://www.conventionalcommits.org/) so we can
generate a changelog later:

```
<type>[optional scope]: <description>
```

Allowed types: `feat`, `fix`, `chore`, `docs`, `test`, `refactor`, `perf`,
`build`, `ci`, `style`, `revert`.

| Good                                          | Bad          |
|-----------------------------------------------|--------------|
| `feat: add PR title check action`             | `stuff`      |
| `fix: reconnect SSE stream after idle timeout`| `Fixed bug`  |
| `docs: document branch protection config`     | `update`     |

A [PR-title check](.github/workflows/pr-title.yml) enforces this — a PR titled
`stuff` fails the check and cannot merge.

## Repository settings

These live in GitHub settings, not the repo contents, but are documented here so
the configuration is reproducible. They pair with the CI from
[#12](../../issues/12).

Enable squash-only merges and auto-delete of head branches:

```sh
gh repo edit Faris1015/clinical-trial-protocol-screener \
  --enable-squash-merge \
  --enable-merge-commit=false \
  --enable-rebase-merge=false \
  --delete-branch-on-merge
```

Branch protection on `main`:

```sh
gh api -X PUT repos/Faris1015/clinical-trial-protocol-screener/branches/main/protection \
  --input - <<'JSON'
{
  "required_status_checks": {"strict": false, "contexts": ["backend", "frontend"]},
  "required_pull_request_reviews": {"required_approving_review_count": 1},
  "enforce_admins": false,
  "restrictions": null
}
JSON
```

This requires the `backend` and `frontend` status checks plus one approving
review before any merge to `main`.
