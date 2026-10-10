# Releasing

One tag releases everything with **one version**: the server image on GHCR
and the `modelmux-cli` package on PyPI and on npm. Both CLI releases pin the
exact image built in the same run, by digest.

```
tag vX.Y.Z ─▶ version check ─▶ image (amd64+arm64) ─▶ scan · SBOM · provenance
            ─▶ pin digest into both CLIs ─▶ TestPyPI + smoke test ─▶ approve ─▶ PyPI ─┐
                                         ─▶ npm pack + smoke test ─▶ approve ─▶ npm  ─┴▶ GitHub release
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

3. **npm** (done once, before the first release that includes the Node CLI).
   npm only lets you add a trusted publisher to a package that already
   exists, so the first upload is done by hand:

   1. On <https://www.npmjs.com>, sign in (two-factor authentication on).
   2. Claim the name with a placeholder that points to the real releases:

      ```bash
      mkdir /tmp/modelmux-cli-placeholder && cd /tmp/modelmux-cli-placeholder
      npm init -y >/dev/null
      npm pkg set name=modelmux-cli version=0.0.1 \
        description="Placeholder; install modelmux-cli 0.3.0 or later"
      npm login
      npm publish --access public          # asks for your 2FA code
      npm deprecate modelmux-cli@0.0.1 "Placeholder; install modelmux-cli@latest"
      ```

   3. Package settings → **Trusted publisher** → GitHub Actions:

      | Field | Value |
      |---|---|
      | Organization or user | `smit153` |
      | Repository | `modelmux` |
      | Workflow filename | `release.yml` |
      | Environment name | `npm` |

   4. Package settings → Publishing access: **Require two-factor
      authentication and disallow tokens**. From now on only the workflow
      can publish, with no token stored anywhere, and every version gets a
      provenance statement.
   5. GitHub → Settings → Environments: create `npm` and add yourself as a
      **required reviewer**, like `pypi`.

4. **GHCR visibility**: after the first release pushes
   `ghcr.io/smit153/modelmux`, open the package (your profile → Packages →
   modelmux → Package settings) and set it to **Public**. New packages are
   private by default, and a private image makes `modelmux up` fail with
   "The image registry refused access" for everyone else.

## Each release

1. **Bump the version** in all four places (CI fails if they differ):
   - `server/pyproject.toml`
   - `cli/python/pyproject.toml`
   - `cli/node/package.json` (`npm version X.Y.Z --no-git-tag-version` in
     `cli/node/`, which also updates `package-lock.json`)
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
   approve. The `npm` job waits for its own approval: npm has no test
   registry, so the `cli-node` job has already installed the packed tarball
   and run it; that exact tarball is what gets published.

5. **Check the result**:

   ```bash
   pipx install modelmux-cli==X.Y.Z
   modelmux --version
   modelmux up && modelmux status

   npx modelmux-cli@X.Y.Z --version
   ```

## If something goes wrong

- **Before the PyPI approval**: reject the job. Nothing reached PyPI. Fix,
  delete the tag (`git push --delete origin vX.Y.Z && git tag -d vX.Y.Z`) and
  tag again. TestPyPI does not allow re-uploading the same version: bump to
  the next patch version if it was already uploaded there.
- **After PyPI**: versions cannot be replaced. Yank the bad release on PyPI
  and publish `X.Y.Z+1`. The image tag stays, but no CLI points at it.
- **After npm**: versions cannot be replaced either. Run
  `npm deprecate modelmux-cli@X.Y.Z "<why>; use X.Y.Z+1"` and publish
  `X.Y.Z+1`. (`npm unpublish` works only within 72 hours, and the version
  number can never be used again.)
- **One registry published, the other not** (for example the npm approval
  was rejected): re-run the failed job. It skips a version that is already
  on npm, so re-running is always safe.
- **Image scan fails**: a fixable HIGH/CRITICAL vulnerability was found.
  Rebuild picks up Debian security updates; add a justified entry to
  `.trivyignore` only if it truly does not apply.
