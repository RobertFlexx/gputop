#!/bin/sh
set -eu

if [ -n "${ZSH_VERSION:-}" ] && [ -x /bin/sh ]; then
  exec /bin/sh "$0" "$@"
fi

SCRIPT_VERSION="1.0.0"
APP_NAME="gputop"
MIN_PYTHON="3.10"
MANAGED_PYTHON="3.12"
PYTHON_TAGS="python3.14 python3.13 python3.12 python3.11 python3.10 python3 python"
REPO="${GPUTOP_REPO:-RobertFlexx/gputop}"
REF="${GPUTOP_REF:-main}"
SOURCE="${GPUTOP_SOURCE:-}"
SOURCE_MODE="${GPUTOP_SOURCE_MODE:-auto}"
METHOD="${GPUTOP_METHOD:-}"
PYTHON="${GPUTOP_PYTHON:-}"
BIN_DIR="${GPUTOP_BIN_DIR:-}"
RC_FILE="${GPUTOP_RC_FILE:-}"
ASSUME_YES="${GPUTOP_YES:-}"
CLEAN_VENV="${GPUTOP_CLEAN_VENV:-}"
PYTHON_CANDIDATES=""
PYTHON_SEEN=""
FOREIGN_BIN=""
FOREIGN_PYTHON=""
FOREIGN_VERSION=""
CURRENT_VERSION=""
LAUNCHER_KIND="none"
STATE_RC_FILE=""
ACTION=""
PLAN_ONLY=0
EDIT_PATH=1
HOME_DIR="${HOME:-}"
C_RESET=""
C_BOLD=""
C_DIM=""
C_RED=""
C_GREEN=""
C_YELLOW=""
C_CYAN=""
TMP_DIR=""
DATA_DIR="${GPUTOP_DIR:-}"
VENV_DIR=""
SRC_DIR=""
VENV_PY=""
VENV_BIN=""
LAUNCHER=""
STATE_FILE=""
TOOLS_BIN=""

enable_color() {
  if [ -t 1 ] && [ -z "${NO_COLOR:-}" ] && [ "${TERM:-dumb}" != "dumb" ]; then
    C_RESET=$(printf '\033[0m')
    C_BOLD=$(printf '\033[1m')
    C_DIM=$(printf '\033[2m')
    C_RED=$(printf '\033[31m')
    C_GREEN=$(printf '\033[32m')
    C_YELLOW=$(printf '\033[33m')
    C_CYAN=$(printf '\033[36m')
  fi
}

say() {
  printf '%s\n' "$*"
}

step() {
  printf '%s->%s %s%s%s\n' "$C_CYAN" "$C_RESET" "$C_BOLD" "$*" "$C_RESET"
}

detail() {
  printf '   %s%s%s\n' "$C_DIM" "$*" "$C_RESET"
}

field() {
  printf '   %-12s %s\n' "$1" "$2"
}

warn() {
  printf '%swarning:%s %s\n' "$C_YELLOW" "$C_RESET" "$*" >&2
}

fail() {
  printf '%serror:%s %s\n' "$C_RED" "$C_RESET" "$*" >&2
}

die() {
  fail "$*"
  exit 1
}

ok() {
  printf '%s%s%s %s\n' "$C_GREEN" "$APP_NAME" "$C_RESET" "$*"
}

have() {
  command -v "$1" >/dev/null 2>&1
}

version_ge() {
  _vg_left=$1
  _vg_right=$2
  if [ "$_vg_left" = "$_vg_right" ]; then
    return 0
  fi
  _vg_index=1
  while [ "$_vg_index" -le 3 ]; do
    _vg_a=$(printf '%s' "$_vg_left" | cut -d. -f"$_vg_index")
    _vg_b=$(printf '%s' "$_vg_right" | cut -d. -f"$_vg_index")
    _vg_a=${_vg_a:-0}
    _vg_b=${_vg_b:-0}
    if [ "$_vg_a" -gt "$_vg_b" ] 2>/dev/null; then
      return 0
    fi
    if [ "$_vg_a" -lt "$_vg_b" ] 2>/dev/null; then
      return 1
    fi
    _vg_index=$((_vg_index + 1))
  done
  return 0
}

cleanup() {
  if [ -n "$TMP_DIR" ] && [ -d "$TMP_DIR" ]; then
    rm -rf "$TMP_DIR"
  fi
}

make_temp_dir() {
  if [ -n "$TMP_DIR" ] && [ -d "$TMP_DIR" ]; then
    return 0
  fi
  _mt_base=${TMPDIR:-/tmp}
  if [ ! -d "$_mt_base" ]; then
    _mt_base=/tmp
  fi
  _mt_try=$(mktemp -d "$_mt_base/${APP_NAME}-install.XXXXXX" 2>/dev/null) || _mt_try=""
  if [ -z "$_mt_try" ]; then
    _mt_try="$_mt_base/${APP_NAME}-install.$$"
    mkdir -p "$_mt_try" || return 1
  fi
  TMP_DIR=$_mt_try
  return 0
}

confirm() {
  _cf_prompt=$1
  _cf_default=${2:-y}
  if [ "$_cf_default" = "y" ]; then
    _cf_hint="Y/n"
  else
    _cf_hint="y/N"
  fi
  if [ ! -t 0 ]; then
    if [ "$_cf_default" = "y" ]; then
      return 0
    fi
    return 1
  fi
  while :; do
    printf '%s%s%s [%s] ' "$C_BOLD" "$_cf_prompt" "$C_RESET" "$_cf_hint" >&2
    IFS= read -r _cf_answer || _cf_answer=""
    _cf_lower=$(printf '%s' "$_cf_answer" | tr '[:upper:]' '[:lower:]')
    case "$_cf_lower" in
      "")
        if [ "$_cf_default" = "y" ]; then
          return 0
        fi
        return 1
        ;;
      y | yes)
        return 0
        ;;
      n | no)
        return 1
        ;;
      *)
        printf 'Please answer y or n.\n' >&2
        ;;
    esac
  done
}

ask() {
  _ak_prompt=$1
  _ak_default=${2:-}
  if [ -n "$_ak_default" ]; then
    printf '%s%s%s [%s]: ' "$C_BOLD" "$_ak_prompt" "$C_RESET" "$_ak_default" >&2
  else
    printf '%s%s%s: ' "$C_BOLD" "$_ak_prompt" "$C_RESET" >&2
  fi
  IFS= read -r _ak_answer || _ak_answer=""
  if [ -z "$_ak_answer" ]; then
    printf '%s' "$_ak_default"
  else
    printf '%s' "$_ak_answer"
  fi
}

ask_choice() {
  _ac_prompt=$1
  shift
  _ac_index=1
  printf '%s%s%s\n' "$C_BOLD" "$_ac_prompt" "$C_RESET" >&2
  for _ac_option in "$@"; do
    printf '   %s) %s\n' "$_ac_index" "$_ac_option" >&2
    _ac_index=$((_ac_index + 1))
  done
  ask "Choice" "1"
}

usage() {
  cat <<EOF
${APP_NAME} installer ${SCRIPT_VERSION} for macOS, Linux and the BSDs

  curl -fsSL https://raw.githubusercontent.com/${REPO}/${REF}/install.sh | sh

With no arguments the installer offers an interactive menu, including updating
an installation that is already on this machine.

Actions:
  -i, --install         Install ${APP_NAME} (default)
  -u, --update          Update an existing installation
      --uninstall       Remove an installation made by this script
  -y, --yes             Accept the defaults, never prompt
  -n, --dry-run         Print the plan and exit without changing anything

Install options:
      --dir DIR         Data directory (default: \$XDG_DATA_HOME/${APP_NAME})
      --bin-dir DIR     Directory for the ${APP_NAME} command (default: ~/.local/bin)
      --python PATH     Python interpreter to use
      --method NAME     pip or uv (default: pip, uv when uv is available)
      --ref REF         Git ref to install (default: main)
      --repo OWNER/NAME GitHub repository (default: ${REPO})
      --source PATH     Install from a local checkout instead of the repository
      --rc-file FILE    Shell startup file to update (default: detected)
      --no-path         Leave the shell startup file alone

  -h, --help            Show this help
  -V, --version         Show the installer version

Environment equivalents: GPUTOP_DIR, GPUTOP_BIN_DIR, GPUTOP_PYTHON,
GPUTOP_METHOD, GPUTOP_REF, GPUTOP_REPO, GPUTOP_SOURCE, GPUTOP_RC_FILE,
GPUTOP_YES, GPUTOP_SOURCE_MODE, GPUTOP_CLEAN_VENV, NO_COLOR.

The installer runs as your own user, never needs root, and keeps its Python
environment inside the data directory so an update can replace it cleanly.
EOF
}

parse_args() {
  while [ $# -gt 0 ]; do
    case "$1" in
      -i | --install)
        ACTION="install"
        ;;
      -u | --update)
        ACTION="update"
        ;;
      --uninstall | --remove)
        ACTION="uninstall"
        ;;
      -y | --yes | --no-prompt)
        ASSUME_YES=1
        ;;
      -n | --dry-run)
        PLAN_ONLY=1
        ;;
      -d | --dir)
        [ $# -ge 2 ] || die "--dir needs a value"
        DATA_DIR=$2
        shift
        ;;
      --bin-dir)
        [ $# -ge 2 ] || die "--bin-dir needs a value"
        BIN_DIR=$2
        shift
        ;;
      --python)
        [ $# -ge 2 ] || die "--python needs a value"
        PYTHON=$2
        shift
        ;;
      -m | --method)
        [ $# -ge 2 ] || die "--method needs a value"
        METHOD=$2
        shift
        ;;
      --ref)
        [ $# -ge 2 ] || die "--ref needs a value"
        REF=$2
        shift
        ;;
      --repo)
        [ $# -ge 2 ] || die "--repo needs a value"
        REPO=$2
        shift
        ;;
      --source)
        [ $# -ge 2 ] || die "--source needs a value"
        SOURCE=$2
        shift
        ;;
      --rc-file)
        [ $# -ge 2 ] || die "--rc-file needs a value"
        RC_FILE=$2
        shift
        ;;
      --no-path)
        EDIT_PATH=0
        ;;
      -h | --help)
        usage
        exit 0
        ;;
      -V | --version)
        say "${APP_NAME} installer ${SCRIPT_VERSION}"
        exit 0
        ;;
      *)
        fail "unknown option: $1"
        usage >&2
        exit 2
        ;;
    esac
    shift
  done
}

apply_layout() {
  if [ -z "$DATA_DIR" ]; then
    _al_data_home=${XDG_DATA_HOME:-}
    if [ -z "$_al_data_home" ]; then
      [ -n "$HOME_DIR" ] || die "HOME is not set, use --dir to choose an install directory"
      _al_data_home="$HOME_DIR/.local/share"
    fi
    DATA_DIR="$_al_data_home/${APP_NAME}"
  fi
  if [ -z "$BIN_DIR" ]; then
    [ -n "$HOME_DIR" ] || die "HOME is not set, use --bin-dir to choose a command directory"
    BIN_DIR="$HOME_DIR/.local/bin"
  fi
  case "$DATA_DIR" in
    */) DATA_DIR=${DATA_DIR%/} ;;
  esac
  case "$BIN_DIR" in
    */) BIN_DIR=${BIN_DIR%/} ;;
  esac
  VENV_DIR="$DATA_DIR/venv"
  SRC_DIR="$DATA_DIR/src"
  VENV_BIN="$VENV_DIR/bin"
  VENV_PY="$VENV_BIN/python"
  LAUNCHER="$BIN_DIR/${APP_NAME}"
  STATE_FILE="$DATA_DIR/install.state"
  TOOLS_BIN="$DATA_DIR/tools/bin"
}

detect_local_source() {
  if [ -n "$SOURCE" ]; then
    [ -f "$SOURCE/pyproject.toml" ] || die "no pyproject.toml in $SOURCE"
    SOURCE=$(cd "$SOURCE" && pwd -P)
    return 0
  fi
  if [ -f "$0" ] && [ -d "$(dirname "$0")/${APP_NAME}" ]; then
    _ls_dir=$(cd "$(dirname "$0")" && pwd -P)
    if [ -f "$_ls_dir/${APP_NAME}/__init__.py" ] && [ -f "$_ls_dir/pyproject.toml" ]; then
      SOURCE="$_ls_dir"
    fi
  fi
}

python_version() {
  "$1" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null
}

python_is_usable() {
  _pu_version=$(python_version "$1" 2>/dev/null) || return 1
  if [ -z "$_pu_version" ]; then
    return 1
  fi
  version_ge "$_pu_version" "$MIN_PYTHON"
}

python_full_version() {
  "$1" -c 'import platform; print(platform.python_version())' 2>/dev/null || printf '?'
}

find_pythons() {
  PYTHON_CANDIDATES=""
  PYTHON_SEEN=""
  if [ -n "$PYTHON" ]; then
    if ! have "$PYTHON"; then
      die "python not found: $PYTHON"
    fi
    if ! python_is_usable "$PYTHON"; then
      die "$PYTHON runs Python $(python_version "$PYTHON" || printf '?'), $MIN_PYTHON or newer is required"
    fi
    PYTHON_CANDIDATES=$PYTHON
    return 0
  fi
  for _fp_tag in $PYTHON_TAGS; do
    _fp_path=$(command -v "$_fp_tag" 2>/dev/null) || continue
    if [ -z "$_fp_path" ]; then
      continue
    fi
    if ! python_is_usable "$_fp_path"; then
      continue
    fi
    _fp_key=$("$_fp_path" -c 'import sys; print(sys.executable)' 2>/dev/null) || _fp_key=$_fp_path
    case "
$PYTHON_SEEN
" in
      *"
$_fp_key
"*) continue ;;
    esac
    PYTHON_SEEN="$PYTHON_SEEN$_fp_key
"
    PYTHON_CANDIDATES="$PYTHON_CANDIDATES$_fp_path
"
  done
  if [ -z "$PYTHON_CANDIDATES" ]; then
    return 1
  fi
  return 0
}

uv_path() {
  if have uv; then
    command -v uv
    return 0
  fi
  if [ -x "$TOOLS_BIN/uv" ]; then
    printf '%s' "$TOOLS_BIN/uv"
    return 0
  fi
  return 1
}

ensure_uv() {
  if uv_path >/dev/null 2>&1; then
    return 0
  fi
  step "Bootstrapping uv"
  mkdir -p "$TOOLS_BIN"
  make_temp_dir
  _eu_url="https://astral.sh/uv/install.sh"
  _eu_script="$TMP_DIR/uv-install.sh"
  if ! download "$_eu_url" "$_eu_script"; then
    die "could not download the uv installer from $_eu_url"
  fi
  if ! UV_INSTALL_DIR="$TOOLS_BIN" UV_NO_MODIFY_PATH=1 sh "$_eu_script" >"$TMP_DIR/uv-install.log" 2>&1; then
    die "the uv installer failed, see $TMP_DIR/uv-install.log"
  fi
  [ -x "$TOOLS_BIN/uv" ] || die "the uv installer did not place uv in $TOOLS_BIN"
  ok "uv is ready in $TOOLS_BIN"
}

install_managed_python() {
  _im_uv=$(uv_path) || die "uv is required to install a managed Python"
  step "Installing a managed Python $MANAGED_PYTHON with uv"
  if ! "$_im_uv" python install "$MANAGED_PYTHON" >/dev/null 2>&1; then
    die "uv could not install Python $MANAGED_PYTHON"
  fi
  _im_python=$("$_im_uv" python find "$MANAGED_PYTHON" 2>/dev/null) || _im_python=""
  if [ -z "$_im_python" ]; then
    die "uv installed Python $MANAGED_PYTHON but did not report its path"
  fi
  printf '%s' "$_im_python"
}

resolve_method() {
  if [ -z "$METHOD" ]; then
    if have uv; then
      METHOD="uv"
    else
      METHOD="pip"
    fi
  fi
  case "$METHOD" in
    pip) ;;
    uv) ensure_uv ;;
    *) die "unknown method: $METHOD (use pip or uv)" ;;
  esac
}

resolve_python() {
  if [ -n "$PYTHON" ]; then
    if ! have "$PYTHON"; then
      die "python not found: $PYTHON"
    fi
    if ! python_is_usable "$PYTHON"; then
      die "$PYTHON runs Python $(python_version "$PYTHON" || printf '?'), $MIN_PYTHON or newer is required"
    fi
    return 0
  fi
  if find_pythons; then
    PYTHON=$(printf '%s' "$PYTHON_CANDIDATES" | head -n 1)
    return 0
  fi
  if is_tty && confirm "No Python $MIN_PYTHON+ found here. Install one with uv?" y; then
    ensure_uv
    PYTHON=$(install_managed_python)
    return 0
  fi
  if [ "$METHOD" = "uv" ]; then
    PYTHON=$(install_managed_python)
    return 0
  fi
  return 1
}

is_tty() {
  [ -t 0 ]
}

download() {
  _dl_url=$1
  _dl_dest=$2
  if have curl; then
    curl -fsSL --retry 3 --retry-delay 1 -o "$_dl_dest" "$_dl_url" && return 0
  fi
  if have wget; then
    wget -q -O "$_dl_dest" "$_dl_url" && return 0
  fi
  if have fetch; then
    fetch -q -o "$_dl_dest" "$_dl_url" && return 0
  fi
  return 1
}

archive_url() {
  printf 'https://codeload.github.com/%s/legacy.tar.gz/%s' "$REPO" "$REF"
}

git_url() {
  printf 'https://github.com/%s.git' "$REPO"
}

is_commit() {
  case "$1" in
    "" | *[!0-9a-fA-F]*) return 1 ;;
  esac
  [ "${#1}" -ge 7 ] || return 1
  return 0
}

fetch_with_tarball() {
  _ft_dest=$1
  _ft_tarball="$TMP_DIR/source.tar.gz"
  if ! download "$(archive_url)" "$_ft_tarball"; then
    if is_commit "$REF"; then
      fail "could not download the source archive for $REPO at $REF"
      fail "a commit has to be reachable from a branch or tag, try --ref main or a release tag"
    else
      fail "could not download the source archive for $REPO at $REF"
    fi
    exit 1
  fi
  _ft_extract="$TMP_DIR/extract"
  mkdir -p "$_ft_extract"
  if ! tar -xzf "$_ft_tarball" -C "$_ft_extract"; then
    die "could not unpack the source archive"
  fi
  set -- "$_ft_extract"/*
  if [ ! -d "$1" ]; then
    die "the source archive was empty"
  fi
  rm -rf "$_ft_dest"
  mkdir -p "$(dirname "$_ft_dest")"
  mv "$1" "$_ft_dest"
}

clone_repo() {
  _cr_dest=$1
  rm -rf "$_cr_dest"
  if is_commit "$REF"; then
    git init -q "$_cr_dest" >/dev/null 2>&1 || return 1
    git -C "$_cr_dest" remote add origin "$(git_url)" >/dev/null 2>&1 || return 1
    git -C "$_cr_dest" fetch -q --depth 1 origin "$REF" >/dev/null 2>&1 || return 1
    git -C "$_cr_dest" checkout -q --detach FETCH_HEAD >/dev/null 2>&1 || return 1
    return 0
  fi
  git clone -q --depth 1 --single-branch --branch "$REF" "$(git_url)" "$_cr_dest" >/dev/null 2>&1 || return 1
  return 0
}

fetch_source() {
  _fs_dest=${1:-$SRC_DIR}
  if [ -n "$SOURCE" ]; then
    rm -rf "$_fs_dest"
    mkdir -p "$(dirname "$_fs_dest")"
    cp -R "$SOURCE" "$_fs_dest"
    detail "copied the checkout at $SOURCE"
    return 0
  fi
  case "$SOURCE_MODE" in
    git)
      clone_repo "$_fs_dest" || die "git could not clone $REPO at $REF"
      detail "cloned $REPO at $REF"
      return 0
      ;;
    tarball)
      fetch_with_tarball "$_fs_dest"
      detail "downloaded $REPO at $REF"
      return 0
      ;;
    auto)
      if have git && clone_repo "$_fs_dest"; then
        detail "cloned $REPO at $REF"
        return 0
      fi
      if have git; then
        warn "git could not reach the repository, using the source archive"
      fi
      ;;
    *)
      die "unknown source mode: $SOURCE_MODE"
      ;;
  esac
  fetch_with_tarball "$_fs_dest"
  detail "downloaded $REPO at $REF"
}

refresh_source() {
  if [ -n "$SOURCE" ]; then
    fetch_source
    return 0
  fi
  if [ -d "$SRC_DIR/.git" ]; then
    if git -C "$SRC_DIR" fetch -q --depth 1 origin "$REF" >/dev/null 2>&1 ||
      git -C "$SRC_DIR" fetch -q --depth 1 --tags --force origin >/dev/null 2>&1; then
      if git -C "$SRC_DIR" checkout -q --detach FETCH_HEAD >/dev/null 2>&1; then
        detail "moved the checkout to $(git -C "$SRC_DIR" rev-parse --short HEAD 2>/dev/null || printf '%s' "$REF")"
        return 0
      fi
    fi
    warn "git could not update the checkout, using the source archive"
  fi
  fetch_with_tarball "$SRC_DIR"
  detail "downloaded $REPO at $REF"
}

source_version() {
  _sv_file="$SRC_DIR/$APP_NAME/__init__.py"
  [ -f "$_sv_file" ] || return 1
  _sv_version=$(sed -n 's/^__version__ *= *"\([^"]*\)".*/\1/p' "$_sv_file" | head -n 1)
  [ -n "$_sv_version" ] || return 1
  printf '%s' "$_sv_version"
}

managed_version() {
  [ -x "$VENV_PY" ] || return 1
  _mv_version=$("$VENV_PY" -c 'import importlib.metadata as m; print(m.version("gputop"))' 2>/dev/null) || _mv_version=""
  [ -n "$_mv_version" ] || return 1
  printf '%s' "$_mv_version"
}

has_install() {
  [ -d "$VENV_DIR" ] && [ -x "$VENV_PY" ]
}

has_managed_install() {
  has_install && return 0
  [ -f "$STATE_FILE" ]
}

path_entry() {
  _pe_saved=$PATH
  PATH=$BIN_DIR:$PATH
  _pe_found=$(command -v "$APP_NAME" 2>/dev/null) || _pe_found=""
  PATH=$_pe_saved
  printf '%s' "$_pe_found"
}

state_value() {
  [ -f "$STATE_FILE" ] || return 1
  _st_line=$(grep "^$1=" "$STATE_FILE" 2>/dev/null | head -n 1) || _st_line=""
  [ -n "$_st_line" ] || return 1
  printf '%s' "${_st_line#*=}"
  return 0
}

state_write() {
  mkdir -p "$DATA_DIR"
  {
    printf 'version=1\n'
    printf 'data_dir=%s\n' "$DATA_DIR"
    printf 'venv_dir=%s\n' "$VENV_DIR"
    printf 'src_dir=%s\n' "$SRC_DIR"
    printf 'bin_dir=%s\n' "$BIN_DIR"
    printf 'launcher=%s\n' "$LAUNCHER"
    printf 'launcher_kind=%s\n' "$LAUNCHER_KIND"
    printf 'rc_file=%s\n' "$STATE_RC_FILE"
    printf 'method=%s\n' "$METHOD"
    printf 'ref=%s\n' "$REF"
    printf 'repo=%s\n' "$REPO"
    printf 'python=%s\n' "$PYTHON"
    printf 'installed_version=%s\n' "$(managed_version || printf 'unknown')"
  } >"$STATE_FILE.tmp"
  mv "$STATE_FILE.tmp" "$STATE_FILE"
}

foreign_python() {
  _fp_script=$1
  [ -f "$_fp_script" ] || return 1
  IFS= read -r _fp_line <"$_fp_script" || return 1
  case "$_fp_line" in
    '#!'*) ;;
    *) return 1 ;;
  esac
  _fp_shebang=${_fp_line#\#!}
  _fp_shebang=${_fp_shebang%"${_fp_shebang##*[![:space:]]}"}
  _fp_python=""
  for _fp_word in $_fp_shebang; do
    if [ "$_fp_word" = "env" ]; then
      continue
    fi
    _fp_python=$_fp_word
    break
  done
  [ -n "$_fp_python" ] || return 1
  [ -x "$_fp_python" ] || return 1
  "$_fp_python" -c 'import importlib.metadata as m; m.version("gputop")' >/dev/null 2>&1 || return 1
  printf '%s' "$_fp_python"
}

foreign_is_user_owned() {
  [ -n "$1" ] || return 1
  case "$1" in
    "$HOME_DIR"/* | */venv/* | */.venv/* | */envs/* | *pyenv* | */uv/tools/*) return 0 ;;
  esac
  return 1
}

detect_foreign() {
  FOREIGN_BIN=$(path_entry)
  FOREIGN_PYTHON=""
  FOREIGN_VERSION=""
  [ -n "$FOREIGN_BIN" ] || return 1
  [ "$(dirname "$FOREIGN_BIN")" = "$BIN_DIR" ] && return 1
  [ -x "$FOREIGN_BIN" ] || return 1
  if ! FOREIGN_PYTHON=$(foreign_python "$FOREIGN_BIN"); then
    FOREIGN_PYTHON=""
    return 1
  fi
  FOREIGN_VERSION=$("$FOREIGN_PYTHON" -c 'import importlib.metadata as m; print(m.version("gputop"))' 2>/dev/null) || FOREIGN_VERSION="unknown"
  return 0
}

path_block_start() {
  printf '# >>> %s >>>' "$APP_NAME"
}

path_block_end() {
  printf '# <<< %s <<<' "$APP_NAME"
}

detect_rc_file() {
  if [ -n "$RC_FILE" ]; then
    return 0
  fi
  _dr_base=$(basename "${SHELL:-sh}")
  case "$_dr_base" in
    zsh) RC_FILE="$HOME_DIR/.zshrc" ;;
    bash) RC_FILE="$HOME_DIR/.bashrc" ;;
    fish) RC_FILE="$HOME_DIR/.config/fish/conf.d/${APP_NAME}.fish" ;;
    csh | tcsh) RC_FILE="$HOME_DIR/.cshrc" ;;
    ksh | ksh93 | mksh | pdksh) RC_FILE="$HOME_DIR/.kshrc" ;;
    *) RC_FILE="$HOME_DIR/.profile" ;;
  esac
  if [ "$_dr_base" = "zsh" ] && [ ! -f "$RC_FILE" ] && [ -f "$HOME_DIR/.zprofile" ]; then
    RC_FILE="$HOME_DIR/.zprofile"
  fi
  return 0
}

path_in_rc() {
  detect_rc_file
  [ -f "$RC_FILE" ] || return 1
  grep -q "^$(path_block_start)$" "$RC_FILE" 2>/dev/null || return 1
  return 0
}

strip_path_block() {
  detect_rc_file
  [ -f "$RC_FILE" ] || return 0
  _sb_tmp="$RC_FILE.${APP_NAME}.tmp"
  if awk -v start="$(path_block_start)" -v end="$(path_block_end)" '
    $0 == start { skip = 1; next }
    $0 == end { skip = 0; next }
    skip != 1 { print }
  ' "$RC_FILE" >"$_sb_tmp"; then
    mv "$_sb_tmp" "$RC_FILE"
  else
    rm -f "$_sb_tmp"
    return 1
  fi
  return 0
}

write_path_block() {
  detect_rc_file
  mkdir -p "$(dirname "$RC_FILE")"
  strip_path_block || warn "could not clean the previous block in $RC_FILE"
  _wp_kind=$(basename "$RC_FILE")
  {
    printf '%s\n' "$(path_block_start)"
    case "$_wp_kind" in
      *.fish)
        printf 'if type -q fish_add_path\n'
        printf '    fish_add_path --path %s --move --global\n' "$BIN_DIR"
        printf 'else\n'
        printf "    set -gx PATH %s \$PATH\n" "$BIN_DIR"
        printf 'end\n'
        ;;
      *cshrc)
        printf "setenv PATH \"%s:\$PATH\"\n" "$BIN_DIR"
        ;;
      *)
        printf "case \":\$PATH:\" in\n"
        printf '  *":%s:"*) ;;\n' "$BIN_DIR"
        printf "  *) PATH=\"%s:\$PATH\"; export PATH ;;\n" "$BIN_DIR"
        printf 'esac\n'
        ;;
    esac
    printf '%s\n' "$(path_block_end)"
  } >>"$RC_FILE"
  STATE_RC_FILE=$RC_FILE
  detail "wrote a PATH block in $RC_FILE"
}

setup_path() {
  [ "$EDIT_PATH" = "1" ] || return 0
  detect_rc_file
  if path_in_rc; then
    write_path_block
    return 0
  fi
  if [ -n "$ASSUME_YES" ] || confirm "Add $BIN_DIR to your shell startup file?" y; then
    write_path_block
  else
    detail "skipped $RC_FILE, run the launcher by path or add it yourself"
  fi
  return 0
}

remove_path_block() {
  if [ -z "$RC_FILE" ]; then
    detect_rc_file
  fi
  if [ -f "$RC_FILE" ] && path_in_rc; then
    strip_path_block
    detail "removed the ${APP_NAME} block from $RC_FILE"
  fi
  return 0
}

bin_dir_on_path() {
  case ":${PATH:-}:" in
    *":$BIN_DIR:"*) return 0 ;;
  esac
  return 1
}

backup_existing() {
  _be_target=$1
  _be_stamp=$(date +%Y%m%d%H%M%S 2>/dev/null) || _be_stamp="bak"
  _be_new="${_be_target}.${_be_stamp}.bak"
  if ! mv "$_be_target" "$_be_new"; then
    die "could not move $_be_target aside"
  fi
  printf '%s' "$_be_new"
}

write_launcher() {
  step "Linking the ${APP_NAME} command"
  mkdir -p "$BIN_DIR"
  if [ -L "$LAUNCHER" ]; then
    _wl_target=$(readlink "$LAUNCHER" 2>/dev/null) || _wl_target=""
    case "$_wl_target" in
      */${APP_NAME} | */bin/${APP_NAME}) rm -f "$LAUNCHER" ;;
      *)
        _wl_backup=$(backup_existing "$LAUNCHER")
        detail "moved the existing $LAUNCHER to $_wl_backup"
        ;;
    esac
  elif [ -e "$LAUNCHER" ]; then
    _wl_backup=$(backup_existing "$LAUNCHER")
    detail "moved the existing $LAUNCHER to $_wl_backup"
  fi
  if ln -s "$VENV_BIN/${APP_NAME}" "$LAUNCHER" 2>/dev/null; then
    LAUNCHER_KIND="symlink"
    detail "$LAUNCHER points at $VENV_BIN/${APP_NAME}"
    return 0
  fi
  LAUNCHER_KIND="shim"
  {
    printf '#!/bin/sh\n'
    printf 'exec "%s/%s" "$@"\n' "$VENV_BIN" "$APP_NAME"
  } >"$LAUNCHER"
  chmod +x "$LAUNCHER" 2>/dev/null || true
  detail "wrote a launcher script at $LAUNCHER"
  return 0
}

create_venv() {
  step "Preparing the Python environment"
  if [ "$CLEAN_VENV" = "1" ] && [ -d "$VENV_DIR" ]; then
    rm -rf "$VENV_DIR"
  fi
  if has_install; then
    detail "reusing $VENV_DIR"
  else
    if [ -z "$PYTHON" ] && [ "$METHOD" = "uv" ]; then
      PYTHON=$(install_managed_python)
    fi
    [ -n "$PYTHON" ] || die "no Python interpreter is available"
    mkdir -p "$DATA_DIR"
    if [ "$METHOD" = "uv" ]; then
      _cv_uv=$(uv_path) || die "uv is required for method uv"
      if ! "$_cv_uv" venv --quiet --python "$PYTHON" "$VENV_DIR"; then
        die "uv could not create the environment"
      fi
    elif ! "$PYTHON" -m venv "$VENV_DIR"; then
      die "$PYTHON could not create the environment, install the venv module first"
    fi
  fi
  [ -x "$VENV_PY" ] || die "the environment has no interpreter at $VENV_PY"
  if [ "$METHOD" != "uv" ]; then
    PIP_DISABLE_PIP_VERSION_CHECK=1 "$VENV_PY" -m pip install --quiet --upgrade pip >/dev/null 2>&1 ||
      detail "could not upgrade pip inside the environment, continuing"
  fi
  return 0
}

install_package() {
  step "Installing $APP_NAME"
  if [ "$METHOD" = "uv" ]; then
    _ip_uv=$(uv_path) || die "uv is required for method uv"
    if ! PIP_ROOT_USER_ACTION=ignore "$_ip_uv" pip install --quiet --python "$VENV_PY" --upgrade "$SRC_DIR"; then
      die "uv could not install $APP_NAME"
    fi
  elif ! PIP_ROOT_USER_ACTION=ignore "$VENV_PY" -m pip install --quiet --upgrade "$SRC_DIR"; then
    die "pip could not install $APP_NAME"
  fi
  return 0
}

verify_install() {
  _vi_version=$(managed_version) || _vi_version=""
  if [ -z "$_vi_version" ]; then
    die "$APP_NAME is missing from the environment at $VENV_DIR"
  fi
  if ! "$LAUNCHER" --version >/dev/null 2>&1; then
    die "$LAUNCHER --version failed, try $APP_NAME --doctor for details"
  fi
  printf '%s' "$_vi_version"
}

report_run_hint() {
  if bin_dir_on_path; then
    say "   run ${C_BOLD}${APP_NAME}${C_RESET} to start"
  else
    say "   run ${C_BOLD}$LAUNCHER${C_RESET}, then start a new terminal to use ${C_BOLD}${APP_NAME}${C_RESET}"
  fi
  say "   ${C_DIM}${APP_NAME} --doctor lists the metrics this machine can read${C_RESET}"
  say ""
  return 0
}

report_installed() {
  say ""
  ok "installed $1 in $DATA_DIR"
  report_run_hint
  return 0
}

print_plan() {
  say ""
  step "Plan"
  for _pp_key in "$@"; do
    case "$_pp_key" in
      action=*) field "action" "${_pp_key#action=}" ;;
      method=*) field "method" "${_pp_key#method=}" ;;
      python=*) field "python" "${_pp_key#python=}" ;;
      source=*) field "source" "${_pp_key#source=}" ;;
      version=*) field "version" "${_pp_key#version=}" ;;
      data=*) field "data dir" "${_pp_key#data=}" ;;
      command=*) field "command" "${_pp_key#command=}" ;;
      startup=*) field "shell file" "${_pp_key#startup=}" ;;
    esac
  done
  say ""
  return 0
}

confirm_plan() {
  if [ "$PLAN_ONLY" = "1" ]; then
    return 0
  fi
  if [ -n "$ASSUME_YES" ] || [ ! -t 0 ]; then
    return 0
  fi
  confirm "Proceed?" y
}

choose_python_interactively() {
  printf '   %sPython interpreters found:%s\n' "$C_DIM" "$C_RESET" >&2
  _cp_index=1
  for _cp_path in $PYTHON_CANDIDATES; do
    printf '   %s) %s (%s)\n' "$_cp_index" "$_cp_path" "$(python_version "$_cp_path")" >&2
    _cp_index=$((_cp_index + 1))
  done
  _cp_answer=$(ask "Interpreter" "1")
  case "$_cp_answer" in
    "" | *[!0-9]*) ;;
    *)
      _cp_selected=$(printf '%s\n' "$PYTHON_CANDIDATES" | sed -n "${_cp_answer}p")
      if [ -n "$_cp_selected" ]; then
        printf '%s' "$_cp_selected"
        return 0
      fi
      ;;
  esac
  printf '%s' "$PYTHON_CANDIDATES" | head -n 1
}

source_label() {
  if [ -n "$SOURCE" ]; then
    printf '%s (local checkout)' "$SOURCE"
  else
    printf '%s at %s' "$REPO" "$REF"
  fi
}

do_install() {
  if [ -z "$METHOD" ]; then
    resolve_method
  fi
  if [ -z "$PYTHON" ]; then
    resolve_python || die "Python $MIN_PYTHON or newer is required, install it or rerun with --method uv"
  fi
  detect_rc_file
  print_plan \
    "action=install $APP_NAME from $(source_label)" \
    "method=$METHOD" \
    "python=$PYTHON ($(python_full_version "$PYTHON"))" \
    "data=$DATA_DIR" \
    "command=$LAUNCHER" \
    "startup=$(if [ "$EDIT_PATH" = "1" ]; then printf '%s' "$RC_FILE"; else printf 'left unchanged'; fi)"
  if [ "$PLAN_ONLY" = "1" ]; then
    return 0
  fi
  confirm_plan || die "cancelled"
  make_temp_dir
  step "Fetching the source"
  fetch_source
  create_venv
  install_package
  write_launcher
  setup_path
  state_write
  report_installed "$(verify_install)"
}

do_update() {
  if [ -z "$METHOD" ]; then
    resolve_method
  fi
  if ! has_install; then
    step "No environment in $DATA_DIR, installing instead"
    do_install
    return 0
  fi
  CURRENT_VERSION=$(managed_version) || CURRENT_VERSION="unknown"
  detect_rc_file
  print_plan \
    "action=update $APP_NAME in $DATA_DIR" \
    "source=$(source_label)" \
    "method=$METHOD" \
    "version=$CURRENT_VERSION" \
    "command=$LAUNCHER"
  if [ "$PLAN_ONLY" = "1" ]; then
    return 0
  fi
  confirm_plan || die "cancelled"
  make_temp_dir
  step "Refreshing the source"
  refresh_source
  install_package
  if [ ! -e "$LAUNCHER" ] && [ ! -L "$LAUNCHER" ]; then
    write_launcher
  fi
  setup_path
  state_write
  _du_new=$(source_version) || _du_new=""
  _du_installed=$(verify_install)
  if [ -z "$_du_new" ]; then
    _du_new=$_du_installed
  fi
  say ""
  if [ "$_du_installed" = "$CURRENT_VERSION" ]; then
    ok "$_du_installed is up to date"
  else
    ok "updated: $CURRENT_VERSION to $_du_installed"
  fi
  if [ -n "$_du_new" ] && [ "$_du_new" != "$_du_installed" ]; then
    warn "the source reports $_du_new while the environment reports $_du_installed"
  fi
  report_run_hint
  return 0
}

update_foreign() {
  _uf_python=$1
  _uf_bin=$2
  _uf_before=$("$_uf_bin" --version 2>/dev/null | awk '{print $NF}') || _uf_before="unknown"
  print_plan \
    "action=update the existing $APP_NAME" \
    "location=$_uf_bin" \
    "python=$_uf_python" \
    "source=$(source_label)" \
    "version=$_uf_before"
  if [ "$PLAN_ONLY" = "1" ]; then
    return 0
  fi
  if [ -t 0 ] && [ -z "$ASSUME_YES" ]; then
    confirm "Update this installation in place?" y || die "cancelled"
  fi
  make_temp_dir
  fetch_source "$TMP_DIR/src"
  if ! PIP_ROOT_USER_ACTION=ignore "$_uf_python" -m pip install --quiet --upgrade "$TMP_DIR/src"; then
    die "pip could not update $APP_NAME in $_uf_python"
  fi
  _uf_after=$("$_uf_bin" --version 2>/dev/null | awk '{print $NF}') || _uf_after="unknown"
  ok "updated: $_uf_before to $_uf_after"
  say "   ${C_DIM}$_uf_bin${C_RESET}"
  say ""
  return 0
}

uninstall_foreign() {
  _un_python=$1
  step "Removing $APP_NAME from $_un_python"
  if [ "$PLAN_ONLY" = "1" ]; then
    return 0
  fi
  if [ -t 0 ] && [ -z "$ASSUME_YES" ]; then
    confirm "Uninstall $APP_NAME from $_un_python?" n || die "cancelled"
  fi
  if ! "$_un_python" -m pip uninstall -y "$APP_NAME"; then
    die "pip could not uninstall $APP_NAME"
  fi
  ok "removed from $_un_python"
  return 0
}

do_uninstall() {
  detect_rc_file
  _du_existing=$(path_entry)
  if [ ! -d "$DATA_DIR" ] && [ -z "$_du_existing" ]; then
    say "no $APP_NAME installation found"
    return 0
  fi
  print_plan \
    "action=uninstall $APP_NAME" \
    "data=$DATA_DIR" \
    "command=$LAUNCHER" \
    "startup=$(if [ -f "$RC_FILE" ]; then printf '%s' "$RC_FILE"; else printf 'nothing to clean'; fi)"
  if [ "$PLAN_ONLY" = "1" ]; then
    return 0
  fi
  confirm_plan || die "cancelled"
  if [ -e "$LAUNCHER" ] || [ -L "$LAUNCHER" ]; then
    rm -f "$LAUNCHER"
    detail "removed $LAUNCHER"
  fi
  remove_path_block
  if [ -d "$DATA_DIR" ]; then
    rm -rf "$DATA_DIR"
    detail "removed $DATA_DIR"
  fi
  if detect_foreign; then
    uninstall_foreign "$FOREIGN_PYTHON"
  fi
  ok "is uninstalled"
  say ""
  return 0
}

interactive_install() {
  step "Setting up $APP_NAME"
  if ! confirm "Install into $DATA_DIR?" y; then
    _ii_dir=$(ask "Install directory" "$DATA_DIR")
    if [ -z "$_ii_dir" ]; then
      die "an install directory is required"
    fi
    DATA_DIR=$_ii_dir
    apply_layout
  fi
  if have uv; then
    _ii_method=$(ask_choice "How should ${APP_NAME} be installed?" "pip, the standard installer" "uv, fast and able to install Python")
  else
    _ii_method="1"
  fi
  case "$_ii_method" in
    2) METHOD="uv" ;;
    *) METHOD="pip" ;;
  esac
  resolve_method
  if ! find_pythons; then
    if confirm "No Python $MIN_PYTHON+ found here. Install one with uv?" y; then
      ensure_uv
      PYTHON=$(install_managed_python)
    else
      die "Python $MIN_PYTHON or newer is required"
    fi
  else
    PYTHON=$(choose_python_interactively)
  fi
  do_install
}

interactive_update() {
  step "Existing installation found"
  field "location" "$DATA_DIR"
  field "version" "$CURRENT_VERSION"
  say ""
  if ! confirm "Update this installation?" y; then
    say "left the existing installation unchanged"
    return 0
  fi
  _iu_method=$(state_value method || printf '')
  if [ -n "$_iu_method" ]; then
    METHOD=$_iu_method
  fi
  resolve_method
  do_update
}

main() {
  trap cleanup EXIT HUP INT TERM
  enable_color
  parse_args "$@"
  if [ -z "$ACTION" ]; then
    ACTION="install"
  fi
  apply_layout
  detect_local_source
  make_temp_dir

  if [ "$ACTION" = "uninstall" ]; then
    do_uninstall
    exit 0
  fi

  detect_foreign || true

  if [ "$ACTION" = "update" ] && ! has_managed_install; then
    if [ -z "$FOREIGN_PYTHON" ]; then
      if [ -n "$FOREIGN_BIN" ]; then
        die "$FOREIGN_BIN is not a pip installation of ${APP_NAME}, update it with the tool that installed it"
      fi
      say "no $APP_NAME installation found, installing instead"
      ACTION="install"
    elif ! foreign_is_user_owned "$FOREIGN_PYTHON"; then
      die "$FOREIGN_BIN belongs to an environment outside your user account, update it with the tool that installed it"
    else
      update_foreign "$FOREIGN_PYTHON" "$FOREIGN_BIN"
      exit 0
    fi
  fi

  if [ -t 0 ] && [ -z "$ASSUME_YES" ] && [ "$ACTION" = "install" ] &&
    [ -z "$PYTHON" ] && [ -z "$SOURCE" ] && [ -z "$METHOD" ] && [ "$PLAN_ONLY" = "0" ]; then
    if has_managed_install; then
      CURRENT_VERSION=$(managed_version) || CURRENT_VERSION="unknown"
      interactive_update
      exit 0
    fi
    if [ -n "$FOREIGN_BIN" ]; then
      step "Existing ${APP_NAME} found"
      field "location" "$FOREIGN_BIN"
      field "version" "${FOREIGN_VERSION:-unknown}"
      say ""
      if confirm "Update that installation?" y; then
        if foreign_is_user_owned "$FOREIGN_PYTHON"; then
          update_foreign "$FOREIGN_PYTHON" "$FOREIGN_BIN"
        else
          warn "$FOREIGN_BIN belongs to an environment outside your user account, update it with the tool that installed it"
          warn "installing a separate copy in $DATA_DIR, run $LAUNCHER to use it"
          do_install
        fi
        exit 0
      fi
    fi
    interactive_install
    exit 0
  fi

  if [ "$ACTION" = "update" ] && [ -z "$METHOD" ]; then
    _mn_method=$(state_value method || printf '')
    if [ -n "$_mn_method" ]; then
      METHOD=$_mn_method
    fi
  fi
  resolve_method
  if ! resolve_python; then
    die "Python $MIN_PYTHON or newer is required, install it or rerun with --method uv"
  fi
  if [ "$ACTION" = "update" ]; then
    do_update
  else
    do_install
  fi
}

main "$@"
