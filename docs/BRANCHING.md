# Branching and releases

ModelMux is live (0.1.0 on PyPI and GHCR), so `main` must always be
releasable. Work happens on short-lived branches; releases are tags.

## Model: GitHub Flow

| Branch | Lives | Purpose |
|---|---|---|
| `main` | forever | Always green and releasable. Release tags `vX.Y.Z` point at it. |
| `<type>/<short-name>` | days | One change. Cut from `main`, merged back by pull request, then deleted. |
| `release/0.1.x` | while 0.1 is supported | Created from `v0.1.0` **only when a 0.1 hotfix is needed**. Receives fixes only; `v0.1.N` tags point at it. |

There is no `develop` branch: with one maintainer and tag-triggered releases
it would only add merges.

## Branch names

`<type>/<short-kebab-name>`, where `type` is the conventional-commit type the
change will be merged as:

| Example | For |
|---|---|
| `feat/certified-startup` | Build-time certification and a faster startup (0.2.0) |
| `fix/cli-port-check` | A bug fix |
| `docs/branching-workflow` | Documentation only |
| `ci/pr-checks` | Workflows and repository automation |
| `chore/release-0.2.0` | Version bumps for a release |

## Merging

- Open a pull request early; CI must pass.
- **Squash merge only.** The pull request title becomes the single commit on
  `main`, so it must be a header-only conventional commit:
  `feat(server): add model filter`.
- Breaking changes get a `!`: `feat(server)!: certify cli facts at build time`.
- Delete the branch after merging.

## Releasing

1. On a `chore/release-X.Y.Z` branch, bump the version in
   `server/pyproject.toml`, `cli/python/pyproject.toml` and
   `shared/release.json`, run `uv lock` in both projects, and merge.
2. Dry-run the release workflow from `main`.
3. Tag `main` and push the tag: `git tag vX.Y.Z && git push origin vX.Y.Z`.
4. Approve the `pypi` deployment after checking TestPyPI.

See [RELEASING.md](RELEASING.md) for the details.

## Hotfix to 0.1 while 0.2 is in progress

1. Once: `git switch -c release/0.1.x v0.1.0 && git push -u origin release/0.1.x`.
2. Branch `fix/<name>` from `release/0.1.x`, open a pull request into it,
   squash-merge.
3. Bump to `0.1.N` on the release branch, dry-run the release workflow there,
   then tag `v0.1.N` on it and push the tag.
4. Cherry-pick the fix onto `main` (`git cherry-pick -x`) in its own pull
   request, so 0.2 has it too.

The release workflow runs from the tagged commit, so workflow fixes made on
`main` must be cherry-picked to `release/0.1.x` as well.

## Rules that are never broken

- Never push directly to `main`.
- **Never re-tag a version** that reached TestPyPI or PyPI; they refuse
  re-uploads. Bump the patch version instead.
- Never move or delete a release tag.

## Still to set up (`ci/pr-checks`)

- Run CI on `pull_request` to `main` and `release/**`, with `concurrency`
  cancelling outdated runs.
- In `release.yml`, refuse a tag whose commit is not on `main` or
  `release/*`.
- Repository rulesets: on `main` and `release/*` require a pull request,
  the CI checks and linear history, and block force pushes and deletion; on
  tags `v*` block updates and deletion.
- Repository settings: squash merging only, the pull request title as the
  default message, and automatic deletion of merged branches.
