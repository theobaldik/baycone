---
name: release
description: Prepare the baycone repo for a new release — fix outdated docs, run the tests, bump the version and turn every change since the last tag into the release's changelog section. Use when the user asks to prepare, cut or make a release, to bump the version for a release, or invokes /release (optionally with `patch`, `minor` or `major`). It only prepares the working tree; it never commits, tags, pushes or publishes.
---

# Prepare a release

Prepares the working tree for `chore: release X.Y.Z`. The user commits, tags and publishes (the tag push publishes to
PyPI); never run `git commit`, `git tag`, `git push`, `uv publish` or `docker push` here. AGENTS.md holds the rules
this follows (versions, changelog format, branches); read its *Branching and releases* and *Changelog and commits* sections first.

Work through the steps in order and stop at the first one that fails: a failure must leave no version bump behind.
The bump is the last thing that changes files.

## 1. Preflight

```bash
git status --short
git branch --show-current
git describe --tags --abbrev=0 --match 'v*'      # the last release reachable from HEAD
git log --oneline "$(git describe --tags --abbrev=0 --match 'v*')"..HEAD
```

- **Branch:** `master` releases normally. On `support/vX.Y.x` only a **patch** is possible, and `:latest` must not
  be pushed for it (see step 7). On any other branch, stop: releases are cut from those two only.
- **Nothing since the last tag:** stop, there is nothing to release.
- **Uncommitted changes:** they are not in the history, so step 4 cannot see them and the release would ship them
  undocumented. Ask whether to stop so the user commits them first (recommended) or to go on.
- **Remote tags:** `git ls-remote --tags origin 'v*'` (read-only). A newer tag there than locally means the local
  repo is behind: stop and say so.

## 2. Check and fix outdated docs

Compare every doc with the code as it is now, not with what the commit messages claim. Fix what is outdated, in the
docs' own style and in American English. Never edit released changelog sections. Note each fix for the summary.

| Check | How |
|---|---|
| CLI flags and exit codes | `uv run baycone-cli --help` against the flag tables and the exit-code line in `README.md` and `docs/pypi-overview.md` |
| Config keys and defaults | the fields of `EncodingSettings` (`baycone/settings.py`), `DefaultsConfig` and `Config` (`service/baycone_service/config.py`) against the `config.yml` example in `README.md` — every key present, defaults matching |
| Library API | `__all__` in `baycone/__init__.py` and the `BayconeError` subclasses against "Using as a library" in `README.md` and "Library" in `docs/pypi-overview.md` |
| Features and limitations | each user-facing change since the last tag (step 4's log) reflected in `README.md` *Features* / *Limitations* and, for the image, `docs/dockerhub-overview.md` |
| AGENTS.md | the test count (from step 3), every module of `baycone/`, `baycone_cli/` and `service/baycone_service/` described, and every backticked identifier it names still existing (`grep -rn` it in the code) |
| Pins | the `uv` version in `Dockerfile`, `.github/workflows/tests.yml`, `.github/workflows/release.yml` and AGENTS.md agree |
| PyPI page | `docs/pypi-overview.md` uses absolute links only (relative ones break on PyPI): `grep -n '](' docs/pypi-overview.md` |
| Diagrams | the symbols and flow in `docs/*.puml` match the code. After editing one, re-render it: `cd docs && plantuml -tsvg <name>.puml`, then `grep -il "syntax error" <name>.svg` must find nothing |

If a doc is wrong in a way the code can't settle (is it the doc or the code that's wrong?), ask instead of guessing.

## 3. Run the checks

```bash
uv run pytest
black --check .
uv build -o "$SCRATCH/dist" && uvx twine check "$SCRATCH/dist"/*   # $SCRATCH: the session's scratchpad, never dist/ in the repo
```

All must pass. Don't fix a failing test as part of the release; report it and stop, unless the fix is obviously part of
this release's changes and the user agrees. Use the passing test count for AGENTS.md (step 2).

## 4. Decide the version

Read every commit since the last tag, the body too, and the diff where a subject is unclear:

```bash
git log --reverse --format='--- %h %s%n%b' "$(git describe --tags --abbrev=0 --match 'v*')"..HEAD
```

From the Conventional Commits:

- A `!` after the type or a `BREAKING CHANGE:` footer → **major**. While the version is `0.x`, a breaking change
  bumps the **minor** instead (semver allows it below 1.0); say so.
- Otherwise, any `feat` → **minor**.
- Otherwise → **patch**. On a `support/` branch, always patch, and a `feat` or a breaking change there is a reason to
  stop and ask.

Show the proposed version together with the commits that decide it, and confirm it with the user (AskUserQuestion,
recommended level first) unless they already named the level. The version is their call.

## 5. Write the changelog section

`CHANGELOG.md` follows Common Changelog with the AGENTS.md rules: groups `### Changed`, `### Added`, `### Removed`,
`### Fixed` in exactly that order; one imperative line per entry that makes sense without its heading; `**Breaking:**`
entries first in their group; no commit hashes, no `feat:` prefixes, no pasted commit subjects.

1. Start from the existing `## [Unreleased]` section and check every entry against the code. It may be stale.
2. Go through the commits from step 4. A user-facing change with no entry gets one; skip what users never see
   (`test`, `ci`, `style`, internal `refactor` / `build` / `chore`, docs-only changes unless they document something
   new). A docs or build change that ships with the image or the PyPI package (a new notice, a changed base image)
   is user-facing.
3. A fix for something introduced **since the last tag** gets no `### Fixed` entry: the released version never had the
   bug. Fold it into the entry of the feature it fixes, if that changes its wording at all.
4. Merge entries that describe one change. Keep it short: a user reads it to decide whether to upgrade.
5. Rename the heading to `## [X.Y.Z] - YYYY-MM-DD` (plain version, today's date) and leave no empty `## [Unreleased]`
   behind. Add `[X.Y.Z]: https://github.com/theobaldik/baycone/releases/tag/vX.Y.Z` at the top of the link list at the
   end of the file.
6. Check that the section is what the release workflow will publish: it extracts everything from the heading up to the
   next `## ` heading or link definition.

## 6. Bump the version

Never edit a version by hand. `bump-my-version` rewrites every pinned copy listed under
`[tool.bumpversion.files]` in `pyproject.toml`:

```bash
uvx bump-my-version bump --dry-run -v <patch|minor|major>   # every file must report a change
uvx bump-my-version bump <patch|minor|major>
grep -rn --exclude=CHANGELOG.md --exclude-dir=.git --exclude-dir=.venv -F '<old version>' . # only historical mentions may remain
uv lock --check
```

The last release touched exactly `pyproject.toml`, `service/pyproject.toml`, `uv.lock`, `compose.yml`, `README.md` and
`docs/dockerhub-overview.md`; `git diff --stat` should show those, plus the changelog and step 2's doc fixes. A file
that pins the version but isn't bumped belongs in `[tool.bumpversion.files]`; add it there and say so.

## 7. Hand over

Summarize:

- the version and why it is that level
- the doc fixes from step 2, each in one line
- the check results (test count, black, twine)
- the changelog section, in full, as it will appear on GitHub

Then give the commands for the user to run, without running any of them:

1. If step 2 changed docs, commit them first, as a separate `docs: ...` commit (stage those files only); then the
   rest as `chore: release X.Y.Z`. Both messages follow Conventional Commits, scope as in AGENTS.md.
2. `git tag vX.Y.Z && git push origin <branch> vX.Y.Z`. The tag push makes `.github/workflows/release.yml` create the
   GitHub release from the changelog section, then publish to PyPI.
3. Docker Hub: `docker compose build && docker compose push`. On a `support/` branch, drop `theobaldik/baycone:latest`
   from `build.tags` in `compose.yml` before pushing: `:latest` only ever moves for a release from `master`.
