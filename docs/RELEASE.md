# Release SOP

This is the runbook for maintainers with publish access. End users should look at [`CHANGELOG.md`](../CHANGELOG.md) for what shipped in each release.

The release pipeline is **GitHub Actions + PyPI Trusted Publishing** — a `v*` tag push runs `.github/workflows/release.yml`, which gates publish behind verify + CI + build + manual approval. No API token in repo secrets.

---

## Standard release

For any normal patch / minor release.

1. **Bump version** in `pyproject.toml` (`version = "X.Y.Z"`). Follow [semver](https://semver.org); pre-1.0 means breaking changes are allowed at any minor bump.
2. **Add a CHANGELOG entry** under `## X.Y.Z — <one-line summary>`, above the previous release block. Wire-format changes also go in `docs/SPEC.md §13`.
3. **Commit + push to `main`:**
   ```sh
   git add pyproject.toml CHANGELOG.md
   git commit -m "release: X.Y.Z"
   git push
   ```
4. **Tag the release commit + push the tag:**
   ```sh
   git tag vX.Y.Z
   git push --tags
   ```
5. **Approve the publish in the Actions tab.** The workflow pauses at the `publish` job with a "Review pending deployment" banner. Click **Review deployments** → check `pypi` → **Approve and deploy**.

The package is live on PyPI within ~30 seconds of approval.

---

## What the workflow does

`.github/workflows/release.yml` runs four jobs in sequence; failure at any stage stops the chain:

| Job | What it checks | Failure mode |
|---|---|---|
| `verify` | Tag (`vX.Y.Z`) matches `pyproject.toml` version | Forgot to bump pyproject before tagging |
| `ci` | `ruff check`, `ruff format --check`, `mypy`, `pytest` against the tagged commit | Regression slipped past PR review |
| `build` | `python -m build` → `sdist` + `wheel` in `dist/` | Packaging metadata broken |
| `publish` | OIDC handshake to PyPI, uploads `dist/*` | Approval rejected, or PyPI rejects upload (e.g., version already exists) |

The build/publish split is the security-best-practice shape — only `publish` has the OIDC `id-token: write` permission, so arbitrary build code can't tamper with the upload identity.

---

## Pre-release checklist

Run before tagging:

- [ ] `make ci` is green locally
- [ ] CHANGELOG entry drafted, factual, scoped to user-visible changes
- [ ] Wire-format changes (if any) also entered in `docs/SPEC.md §13`
- [ ] Version bump committed and pushed (the tag must point at the bumped commit, not an earlier one)
- [ ] No uncommitted changes in the working tree

---

## Yanking a release

PyPI doesn't allow deleting versions, but it does allow **yanking** — hiding a version from `pip install` resolution while keeping it downloadable for anyone who explicitly pins it.

When to yank: a release ships with a real bug, the fixed version is published, and you want to nudge users off the broken one.

1. https://pypi.org/manage/project/baton-sdk/releases/
2. Click the version row → scroll to **"Yank release"**
3. Type a short reason (shown publicly in pip's warning)
4. **Yank**

Unyank from the same page if you change your mind.

---

## Emergency manual publish

For the rare case where CI is broken but a critical release must ship. Requires the PyPI API token stored in your macOS Keychain (one-time setup below).

```sh
cd /path/to/baton
rm -rf dist/ build/ *.egg-info
pyproject-build
unzip -l dist/baton_sdk-X.Y.Z-py3-none-any.whl   # sanity-check contents
twine upload dist/*                               # twine reads token from Keychain
```

Then commit + push the version bump so `main` and PyPI agree on what's released. File an issue noting why CI was bypassed; fix the CI path before the next release.

---

## One-time setup (already done, documented for reference)

### PyPI Trusted Publisher

`https://pypi.org/manage/project/baton-sdk/settings/publishing/` — Pending publisher with:

| Field | Value |
|---|---|
| PyPI Project Name | `baton-sdk` |
| Owner | `good-timing` |
| Repository name | `baton` |
| Workflow filename | `release.yml` |
| Environment name | `pypi` |

### GitHub `pypi` environment

Repo Settings → Environments → `pypi`:

- **Deployment branches and tags** → "Selected branches and tags" → `Tag` pattern `v*`
- **Required reviewers** → repo maintainers with publish authority

### Local Keychain token (for emergency manual publish only)

```sh
pipx install keyring
keyring set https://upload.pypi.org/legacy/ __token__
# Paste an entire-account-scoped pypi-... token at the prompt
```

`twine` reads from Keychain automatically; no `~/.pypirc` file or `PYPI_TOKEN` env var needed.

---

## Versioning policy

Pre-1.0 (`0.x.y`): no API stability promise. Breaking changes can land at any minor bump. CHANGELOG entries should call out breaking changes prominently; wire-format breaks also go in `docs/SPEC.md §13`.

Once we cut `1.0.0`: standard semver — breaking changes require a major bump, new features minor, fixes patch.
