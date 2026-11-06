#!/bin/sh
# Install kidwit.  Usage:
#
#   curl -fsSL https://kidwit.sabbiramin.dev/install | sh
#
# It needs Python 3.9 or newer. It never uses sudo and never writes outside your home directory.
#   1. If pipx or uv is installed, it installs the kidwit package from PyPI with it.
#   2. Otherwise it downloads the single file kidwit.py from the GitHub Release, checks its
#      SHA-256 checksum, and puts a small launcher at ~/.local/bin/kidwit.
#   3. If there is no release file yet, it installs the package from PyPI into its own virtual
#      environment (~/.local/share/kidwit/venv) and puts the launcher there.
#
# Settings (environment variables):
#   KIDWIT_VERSION   install this version, for example 0.1.0 (default: the latest)
#   KIDWIT_METHOD    auto (default), pipx, uv, file or venv
#   KIDWIT_BIN       where the launcher goes for the file method (default: ~/.local/bin)
#   KIDWIT_BASE_URL  the repository URL to download releases from (default: the GitHub repository)
#   KIDWIT_DRY_RUN   set to 1 to print what would happen and change nothing
#
# Everything is inside main(), which runs on the last line, so a download that is cut short
# cannot run half a script.

main() {
    set -eu

    repo="https://github.com/sabbiramin113008/kidwit"
    base="${KIDWIT_BASE_URL:-$repo}"
    version="${KIDWIT_VERSION:-}"
    method="${KIDWIT_METHOD:-auto}"
    bin_dir="${KIDWIT_BIN:-$HOME/.local/bin}"
    dry="${KIDWIT_DRY_RUN:-0}"

    say() { printf '%s\n' "$*"; }
    die() { printf 'kidwit install: %s\n' "$*" >&2; exit 1; }
    have() { command -v "$1" >/dev/null 2>&1; }
    run() {
        if [ "$dry" = 1 ]; then say "  would run: $*"; else "$@"; fi
    }

    [ "$(id -u)" != 0 ] || die "do not run this as root. It installs into your home directory."

    # Python 3.9 or newer.
    python=""
    for candidate in python3 python; do
        if have "$candidate" && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 9))' 2>/dev/null; then
            python="$candidate"
            break
        fi
    done
    [ -n "$python" ] || die "Python 3.9 or newer is required, and was not found. Install Python, then run this again."

    if [ "$method" = auto ]; then
        if have pipx; then method=pipx
        elif have uv; then method=uv
        else method=auto-file
        fi
    fi

    spec="kidwit"
    [ -z "$version" ] || spec="kidwit==$version"

    share="${XDG_DATA_HOME:-$HOME/.local/share}/kidwit"

    # Write the launcher: a two-line script that runs kidwit with the right Python.
    launcher() {
        mkdir -p "$bin_dir"
        printf '#!/bin/sh\nexec %s "$@"\n' "$1" > "$bin_dir/kidwit"
        chmod 755 "$bin_dir/kidwit"
        say "Installed $bin_dir/kidwit"
    }

    fetch() {
        if have curl; then curl -fsSL "$1" -o "$2"; else wget -qO "$2" "$1"; fi
    }

    # Download kidwit.py from a release and check it. Returns 1 if the release is not there.
    install_file() {
        have curl || have wget || die "curl or wget is required to download kidwit."
        if [ -n "$version" ]; then
            release="$base/releases/download/v$version"
        else
            release="$base/releases/latest/download"
        fi
        say "Downloading kidwit.py from $release."
        if [ "$dry" = 1 ]; then
            say "  would verify the SHA-256 checksum and install $bin_dir/kidwit"
            return 0
        fi
        tmp="$(mktemp -d)"
        trap 'rm -rf "$tmp"' EXIT INT TERM
        if ! fetch "$release/kidwit.py" "$tmp/kidwit.py" || ! fetch "$release/SHA256SUMS" "$tmp/SHA256SUMS"; then
            say "No release files found at $release."
            return 1
        fi
        want="$(awk '$2 == "kidwit.py" || $2 == "*kidwit.py" { print $1 }' "$tmp/SHA256SUMS")"
        [ -n "$want" ] || die "SHA256SUMS has no entry for kidwit.py"
        if have sha256sum; then got="$(sha256sum "$tmp/kidwit.py" | awk '{ print $1 }')"
        elif have shasum; then got="$(shasum -a 256 "$tmp/kidwit.py" | awk '{ print $1 }')"
        else die "sha256sum or shasum is required to check the download"
        fi
        [ "$got" = "$want" ] || die "checksum mismatch for kidwit.py (expected $want, got $got). Nothing was installed."
        say "Checksum OK."
        mkdir -p "$share"
        cp "$tmp/kidwit.py" "$share/kidwit.py"
        launcher "\"$(command -v "$python")\" \"$share/kidwit.py\""
    }

    # Install the PyPI package into a private virtual environment.
    install_venv() {
        say "Installing $spec from PyPI into $share/venv."
        if [ "$dry" = 1 ]; then
            say "  would run: $python -m venv $share/venv && $share/venv/bin/pip install $spec"
            return 0
        fi
        mkdir -p "$share"
        "$python" -m venv "$share/venv" || die "could not create a virtual environment (on Debian or Ubuntu, install python3-venv)."
        "$share/venv/bin/pip" install --quiet --disable-pip-version-check --upgrade "$spec" || die "pip could not install $spec"
        launcher "\"$share/venv/bin/kidwit\""
    }

    case "$method" in
    pipx)
        say "Installing $spec with pipx."
        run pipx install --force "$spec"
        ;;
    uv)
        say "Installing $spec with uv."
        run uv tool install --force "$spec"
        ;;
    file)
        install_file || die "no release to install. Try KIDWIT_METHOD=venv to install from PyPI instead."
        ;;
    venv)
        install_venv
        ;;
    auto-file)
        if ! install_file; then
            say "Falling back to the PyPI package."
            method=venv
            install_venv
        fi
        method=file
        ;;
    *)
        die "unknown KIDWIT_METHOD '$method' (use auto, pipx, uv, file or venv)"
        ;;
    esac

    if [ "$dry" != 1 ]; then
        say ""
        case ":$PATH:" in
        *":$bin_dir:"*) ;;
        *) case "$method" in file | venv) say "Add $bin_dir to your PATH, for example: export PATH=\"$bin_dir:\$PATH\"" ;; esac ;;
        esac
        say "Done. Set a key and try it:"
        say "  export ANTHROPIC_API_KEY=sk-ant-...      (or see: kidwit --help)"
        say "  kidwit \"what does this project do?\""
        case "$method" in
        pipx) say "To remove it: pipx uninstall kidwit" ;;
        uv) say "To remove it: uv tool uninstall kidwit" ;;
        file | venv) say "To remove it: rm $bin_dir/kidwit && rm -r $share" ;;
        esac
    fi
}

main "$@"
