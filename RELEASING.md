# Releasing kidwit

kidwit is published to PyPI as `kidwit`, and as a single file (`kidwit.py`) attached to each GitHub Release. This page is the checklist.

## How versions work

1. Versions follow [Semantic Versioning](https://semver.org/). While the major version is 0, a minor release may change flags or the `--json` event protocol. Such changes are listed in `CHANGELOG.md`.
2. The version lives in one place: `VERSION` in `src/kidwit.py`. `pyproject.toml` reads it, and `kidwit --version` prints it.
3. Release candidates use PEP 440 names, for example `0.1.0rc1`. Git tags add a `v`: `v0.1.0rc1`, `v0.1.0`.

## One-time setup

1. Create a PyPI account and a TestPyPI account. Turn on two-factor authentication on both.
2. Create the public GitHub repository and push `main`.
3. Set up PyPI trusted publishing, so no API token is ever stored. On PyPI and on TestPyPI, add a "pending publisher" with the repository owner, repository name, workflow file `release.yml`, and environment name `pypi` (TestPyPI: `testpypi`).
4. In the GitHub repository settings, create the two environments `testpypi` and `pypi`. Require your approval on `pypi`.
5. Protect `main`: require the CI workflow to pass before merging.

## The workflows

1. `ci.yml` runs on every push and pull request. It runs the tests on Python 3.14, on Linux, macOS and Windows (more Python versions will be added after the first PyPI release), and checks that `src/kidwit.py` imports only the standard library and stays within the size ceiling.
2. `release.yml` runs when a tag starting with `v` is pushed. It does these steps in order:
   1. Checks that the tag matches `VERSION`.
   2. Builds the sdist and the wheel, and runs `twine check`.
   3. For a release candidate tag, publishes to TestPyPI. For a final tag, publishes to PyPI after your approval.
   Publishing to PyPI from the workflow only runs when the repository variable `TRUSTED_PUBLISHING` is `true` (Settings > Secrets and variables > Actions > Variables), because it needs the trusted publishers from "One-time setup". Until then the workflow only builds, checks and creates the GitHub Release, and the upload to PyPI is done by hand with `twine upload dist/*` after `twine check`.
   4. Creates the GitHub Release with `kidwit.py`, the wheel, the sdist and a `SHA256SUMS` file attached.

## Releasing a version

1. Run the evals and record the pass rate in the release notes: `python3 evals/run_evals.py`.
2. Move the entries under "Unreleased" in `CHANGELOG.md` to a new section with the version and date.
3. Change `VERSION` in `src/kidwit.py`.
4. Run `scripts/check.sh`. Then build locally and check the result:

   ```
   python3 -m pip install build twine      # in a throwaway virtual environment
   python3 -m build
   twine check dist/*
   ```

5. Commit as `Release 0.1.0`, then tag it: `git tag v0.1.0rc1` for a candidate first.
6. Push the commit and the tag. The release workflow publishes the candidate to TestPyPI.
7. Test the candidate on a clean machine or virtual environment, on macOS and Linux (and Windows if you can):

   ```
   pipx install --pip-args="--index-url https://test.pypi.org/simple/" kidwit==0.1.0rc1
   kidwit --version
   kidwit --read-only "what does this project do?"
   ```

8. If it works, tag the final release (`v0.1.0`), push the tag, and approve the `pypi` environment. Then check:

   ```
   pipx install kidwit && kidwit --version
   uvx kidwit --version
   ```

9. Update the install snippets on the website if the version changed, and announce.

## If a release is bad

1. Do not delete it. On PyPI, "yank" the release, so new installs skip it but pinned installs keep working.
2. Fix the problem and publish a patch release (for example `0.1.1`) the same way.

## The one-line installer

Because kidwit asks for approval on stdin, it cannot be run as `curl ... | python3 -`: the pipe would use up the standard input. These are the supported one-liners instead:

1. Install: `curl -fsSL https://kidwit.sabbiramin.dev/install | sh`. The script (kept in this repository as `scripts/install.sh`) checks for Python 3.10 or newer and installs with `pipx` or `uv` when they exist. Otherwise it downloads `kidwit.py` from the latest GitHub Release, checks the SHA-256 checksum from `SHA256SUMS`, and puts a launcher in `~/.local/bin`. If the release has no files yet, it installs the PyPI package into a private virtual environment. It never uses `sudo`, and it installs the latest release unless `KIDWIT_VERSION` is set. `scripts/install.sh` is tested offline in `tests/test_kidwit.py`; after changing it, run `scripts/check.sh`.
2. Try without installing (bash or zsh): `python3 <(curl -fsSL https://kidwit.sabbiramin.dev/kidwit.py) "fix the failing test"`.
3. After the first PyPI release: `uvx kidwit "fix the failing test"` or `pipx run kidwit`.

The website sends `/install` to `scripts/install.sh` on the `main` branch and `/kidwit.py` to the file attached to the latest GitHub Release, using plain redirects, so the repository stays the single source of truth. Anyone who runs the installer runs whatever is on `main`, so review changes to `scripts/install.sh` like release code.
