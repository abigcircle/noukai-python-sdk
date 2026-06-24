# Releasing

This document is the authoritative runbook for publishing a new version of `noukai-sdk` to PyPI.

## Prerequisites (one-time setup)

Before you can publish, the following must be configured once:

1. **GitHub environment**: In the repo's Settings → Environments, create an environment named `pypi`.
2. **PyPI trusted publisher**: At https://pypi.org/manage/account/publishing/, add a new trusted publisher:
   - PyPI project name: `noukai-sdk`
   - GitHub owner: `abigcircle`
   - Repository: `noukai-python-sdk`
   - Workflow filename: `release.yml`
   - Environment name: `pypi`

This allows the release workflow to publish to PyPI using OIDC — no long-lived API tokens are required.

---

## Release steps

### 1. Bump versions

Both locations must be updated and must match:

- `src/noukai_sdk/_version.py` — update `__version__ = "X.Y.Z"`
- `pyproject.toml` — update `version = "X.Y.Z"` under `[project]`

### 2. Update CHANGELOG.md

Move the `[Unreleased]` section content under a new dated heading:

```markdown
## [X.Y.Z] — YYYY-MM-DD

### Added
- ...

### Changed
- ...

### Fixed
- ...
```

Add the comparison links at the bottom of the file:

```markdown
[Unreleased]: https://github.com/abigcircle/noukai-python-sdk/compare/vX.Y.Z...HEAD
[X.Y.Z]: https://github.com/abigcircle/noukai-python-sdk/compare/vPREV...vX.Y.Z
```

### 3. Commit and merge to main

```bash
git add src/noukai_sdk/_version.py pyproject.toml CHANGELOG.md
git commit -m "chore: release vX.Y.Z"
# Open a PR and merge, or push directly to main if you have that permission.
```

### 4. Tag the release

```bash
git tag vX.Y.Z
git push origin vX.Y.Z
```

This push triggers `.github/workflows/release.yml`.

### 5. Watch CI

The release workflow will:
1. Verify the tag version matches `_version.py` (fails fast if mismatched).
2. Build the wheel and sdist with `uv build`.
3. Publish to PyPI via OIDC trusted publisher.
4. Create a GitHub release with auto-generated release notes.

Monitor progress at: https://github.com/abigcircle/noukai-python-sdk/actions

### 6. Verify

- PyPI listing: https://pypi.org/project/noukai-sdk/
- Quick install check: `pip install noukai-sdk==X.Y.Z`

---

## Emergency: yanking a bad release

If a broken version reaches PyPI:

```bash
# Yank the release (users with that version pinned still get it, but
# pip install without a version pin will skip it).
pip install twine
twine yank noukai-sdk --version X.Y.Z --reason "describe the problem"
```

Then fix the issue, bump to a patch version (X.Y.Z+1), and follow the full release steps above.

---

## Pre-release dry run

Before tagging, verify the build locally:

```bash
cd /path/to/noukai-python
uv build
ls dist/                                    # noukai_sdk-X.Y.Z-py3-none-any.whl + .tar.gz
uvx twine check dist/*                      # verify wheel metadata is valid

# Test install in a throwaway venv
python -m venv /tmp/sdk-install-test
/tmp/sdk-install-test/bin/pip install dist/*.whl
/tmp/sdk-install-test/bin/python -c "from noukai_sdk import Noukai; print(Noukai.__doc__)"
```

If anything looks off, fix and re-run before tagging.
