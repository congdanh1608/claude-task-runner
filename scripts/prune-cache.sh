#!/usr/bin/env bash
# Remove old numeric plugin cache versions only after an explicit confirmation.
set -u

usage() {
  echo "Usage: prune-cache.sh [--yes] [--keep N] [--cache-root DIR]" >&2
  exit 2
}

YES=0
KEEP=2
CACHE_ROOT="${HOME:-}/.claude/plugins/cache"
while [ "$#" -gt 0 ]; do
  case "$1" in
    --yes) YES=1; shift ;;
    --keep)
      [ "$#" -ge 2 ] || usage
      KEEP=$2
      shift 2
      ;;
    --cache-root)
      [ "$#" -ge 2 ] || usage
      CACHE_ROOT=$2
      shift 2
      ;;
    *) usage ;;
  esac
done
case "$KEEP" in ''|*[!0-9]*) usage ;; esac
if [ "${#KEEP}" -gt 4 ] || { [ "${#KEEP}" -eq 4 ] && [ "$KEEP" -gt 1000 ]; }; then
  echo "prune-cache: --keep must be between 0 and 1000" >&2
  exit 2
fi

if [ ! -d "$CACHE_ROOT" ]; then
  echo "prune-cache: cache root does not exist: $CACHE_ROOT" >&2
  exit 1
fi
CACHE_ROOT=$(cd -P "$CACHE_ROOT" 2>/dev/null && pwd -P) || exit 1
SCRIPTS_DIR="${HOME:-}/.claude/scripts"

tmp=$(mktemp -d "${TMPDIR:-/tmp}/prune-cache.XXXXXX") || exit 1
trap 'rm -rf "$tmp"' EXIT HUP INT TERM
removed=0
total_kb=0
link_targets="$tmp/link-targets"
: > "$link_targets" || {
  echo "prune-cache: cannot initialize link list; refusing to list cache paths" >&2
  exit 1
}

resolve_link() {
  local path target parent physical_parent hops
  path=$1
  hops=0
  while [ -L "$path" ]; do
    hops=$((hops + 1))
    [ "$hops" -le 40 ] || return 1
    target=$(readlink "$path") || return 1
    case "$target" in
      /*) path=$target ;;
      *) parent=${path%/*}; [ "$parent" != "$path" ] || parent=.; path="$parent/$target" ;;
    esac
  done
  if [ -d "$path" ]; then
    cd -P "$path" 2>/dev/null && pwd -P
  else
    parent=${path%/*}
    [ "$parent" != "$path" ] || parent=.
    physical_parent=$(cd -P "$parent" 2>/dev/null && pwd -P) || return 1
    printf '%s/%s\n' "$physical_parent" "${path##*/}"
  fi
}

is_linked() {
  local version_path target
  version_path=$1
  while IFS= read -r target; do
    case "$target" in "$version_path"|"$version_path"/*) return 0 ;; esac
  done < "$link_targets"
  return 1
}

if [ -d "$SCRIPTS_DIR" ]; then
  shopt -s dotglob
  for link in "$SCRIPTS_DIR"/*; do
    [ -L "$link" ] || continue
    target=$(resolve_link "$link") || {
      echo "prune-cache: cannot resolve script link; refusing to list cache paths" >&2
      exit 1
    }
    printf '%s\n' "$target" >> "$link_targets" || {
      echo "prune-cache: cannot record script link; refusing to list cache paths" >&2
      exit 1
    }
  done
  shopt -u dotglob
fi

for marketplace in "$CACHE_ROOT"/*; do
  [ -d "$marketplace" ] && [ ! -L "$marketplace" ] || continue
  for plugin in "$marketplace"/*; do
    [ -d "$plugin" ] && [ ! -L "$plugin" ] || continue
    versions="$tmp/versions"
    : > "$versions" || {
      echo "prune-cache: cannot initialize version list; refusing to list cache paths" >&2
      exit 1
    }
    for version in "$plugin"/*; do
      [ ! -L "$version" ] && [ -d "$version" ] || continue
      name=${version##*/}
      [[ "$name" =~ ^[0-9]+(\.[0-9]+)*$ ]] || continue
      printf '%s\n' "$version" >> "$versions" || {
        echo "prune-cache: cannot record version; refusing to list cache paths" >&2
        exit 1
      }
    done
    [ -s "$versions" ] || continue
    # Sort numeric version components and keep the newest requested count.
    sorted="$tmp/sorted"
    if ! sort -V "$versions" > "$sorted" 2>/dev/null; then
      if ! python3 - "$versions" "$sorted" <<'PY'
import sys
source, destination = sys.argv[1:]
paths = open(source).read().splitlines()
paths.sort(key=lambda path: tuple(int(part) for part in path.rsplit("/", 1)[-1].split(".")))
with open(destination, "w") as output:
    output.write("\n".join(paths) + ("\n" if paths else ""))
PY
      then
        echo "prune-cache: version sorting failed; refusing to list cache paths" >&2
        exit 1
      fi
    fi
    keep_file="$tmp/keep"
    if [ "$KEEP" -gt 0 ]; then
      tail -n "$KEEP" "$sorted" > "$keep_file" || {
        echo "prune-cache: retention calculation failed; refusing to list cache paths" >&2
        exit 1
      }
    else
      : > "$keep_file" || exit 1
    fi
    while IFS= read -r version; do
      if grep -Fqx "$version" "$keep_file" || is_linked "$version"; then continue; fi
      name=${version##*/}
      parent=${version%/*}
      physical_parent=$(cd -P "$parent" 2>/dev/null && pwd -P) || physical_parent=""
      if [ ! -d "$version" ] || [ -L "$version" ] || [ "$physical_parent" != "$plugin" ] || [ "$version" != "$plugin/$name" ]; then
        echo "warning: skipping unsafe cache path: $version" >&2
        continue
      fi
      size=$(du -sk "$version" 2>/dev/null | awk '{print $1}')
      [ -n "$size" ] || size=0
      if [ "$YES" -eq 1 ]; then
        if rm -rf "$version"; then
          echo "removed: $version (${size} KB)"
        else
          echo "warning: could not remove: $version" >&2
          continue
        fi
      else
        echo "$version (${size} KB)"
      fi
      removed=$((removed + 1))
      total_kb=$((total_kb + size))
    done < "$versions"
  done
done

echo "count: $removed"
echo "total size: ${total_kb} KB"
if [ "$YES" -eq 0 ]; then echo "dry-run: nothing deleted"; fi
