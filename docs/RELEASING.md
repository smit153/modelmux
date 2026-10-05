# Releasing

One tag releases everything with **one version**: the server image on GHCR
and the `modelmux-cli` package on PyPI. The CLI release pins the exact image
built in the same run, by digest.

```
tag vX.Y.Z ─▶ version check ─▶ image (amd64+arm64) ─▶ scan · SBOM · provenance
            ─▶ pin digest into CLI ─▶ TestPyPI + smoke test ─▶ approve ─▶ PyPI ─▶ GitHub release
```

The workflow is `.github/workflows/release.yml`.

## One-time setup

1. **PyPI and TestPyPI trusted publishers** (no API tokens are stored anywhere).
   On <https://pypi.org/manage/account/publishing/> and
   <https://test.pypi.org/manage/account/publishing/> add a *pending publisher*:

   | Field | PyPI | TestPyPI |
   |---|---|---|
   | PyPI project name | `modelmux-cli` | `modelmux-cli` |
   | Owner | `smit153` | `smit153` |
   | Repository name | `modelmux` | `modelmux` |
   | Workflow name | `release.yml` | `release.yml` |
   | Environment name | `pypi` | `testpypi` |

2. **GitHub environments** (Settings → Environments): create `testpypi` and
   `pypi`. On `pypi`, add yourself as a **required reviewer**, so every real
   PyPI publish waits for your approval.

3. **GHCR visibility**: after the first release pushes
   `ghcr.io/smit153/modelmux`, open the package (your profile → Packages →
   modelmux → Package settings) and set it to **Public**. New packages are
   private by default, and a private image makes `modelmux up` fail with
   "The image registry refused access" for everyone else.

## Each release

1. **Bump the version** in all three places (CI fails if they differ):
   - `server/pyproject.toml`
   - `cli/python/pyproject.toml`
   - `shared/release.json` (`"version"`; leave `"image": null`, the workflow fills it)

   Then `uv lock` in `server/` and `cli/python/`, and commit:
   `chore: release X.Y.Z`.

2. **Dry run** (optional, recommended): Actions → release → Run workflow,
   `dry_run` checked. It builds the multi-arch image and the CLI and runs the
   tests, but pushes and publishes nothing.

3. **Tag and push**:

   ```bash
   git tag vX.Y.Z
   git push origin main vX.Y.Z
   ```

4. **Watch the run.** It publishes to TestPyPI and installs from there first.
   Then the `pypi` job waits for your approval: check the TestPyPI page and
   approve.

5. **Check the result**:

   ```bash
   pipx install modelmux-cli==X.Y.Z
   modelmux --version
   modelmux up && modelmux status
   ```

## If something goes wrong

- **Before the PyPI approval**: reject the job. Nothing reached PyPI. Fix,
  delete the tag (`git push --delete origin vX.Y.Z && git tag -d vX.Y.Z`) and
  tag again. TestPyPI does not allow re-uploading the same version: bump to
  the next patch version if it was already uploaded there.
- **After PyPI**: versions cannot be replaced. Yank the bad release on PyPI
  and publish `X.Y.Z+1`. The image tag stays, but no CLI points at it.
- **Image scan fails**: a fixable HIGH/CRITICAL vulnerability was found.
  Rebuild picks up Debian security updates; add a justified entry to
  `.trivyignore` only if it truly does not apply.
