
#!/usr/bin/env bash
# Sweep Verdaccio using yarn.lock: for each npm-resolved entry, curl its tarball via Verdaccio.
# Requirements: bash, curl, python3 (for URL-encoding), coreutils
# Env:
#   VERDACCIO_URL   (default: http://localhost:4873)
#   YARN_LOCK       (default: ./yarn.lock)
#   OUT_DIR         (default: .sweep-tarballs) where tarballs are optionally saved
#   CONCURRENCY     (default: 8)

set -euo pipefail

VERDACCIO_URL="${VERDACCIO_URL:-http://localhost:4873}"
YARN_LOCK="${YARN_LOCK:-yarn.lock}"
OUT_DIR="${OUT_DIR:-.sweep-tarballs}"
CONCURRENCY="${CONCURRENCY:-8}"

if [ ! -f "$YARN_LOCK" ]; then
  echo "ERROR: yarn.lock not found at $YARN_LOCK" >&2
  exit 1
fi

mkdir -p "$OUT_DIR"

# URL-encode package name for Verdaccio path (handles @scopes)
encode_pkg() {
  python3 - <<'PY' "$1"
import urllib.parse,sys
print(urllib.parse.quote(sys.argv[1]))
PY
}

# Parse Yarn Berry lock: read 'resolution: "<name>@npm:<version>"' lines
# and emit "name\tversion" pairs. We skip non-npm protocols (git, patch, link, workspace).
extract_pairs_from_yarn_lock() {
  # The lock format contains lines like:
  #   resolution: "lodash@npm:4.17.21"
  #   resolution: "@scope/name@npm:1.2.5"
  grep -E '^\s*resolution:\s*".+@npm:.+"' "$YARN_LOCK" \
  | sed -E 's/^\s*resolution:\s*"(.+)@npm:([^"]+)".*/\1\t\2/' \
  | sort -u
}

echo "Enumerating npm-resolved packages from yarn.lock ..."
mapfile -t pairs < <(extract_pairs_from_yarn_lock)
echo "Found ${#pairs[@]} package@version pairs to sweep"

# Simple concurrency control
pids=()
semaphore() {
  while [ "$(jobs -r | wc -l)" -ge "$CONCURRENCY" ]; do sleep 0.2; done
}

sweep_one() {
  local name="$1" version="$2"

  local enc; enc="$(encode_pkg "$name")"

  local meta
  meta="$(curl -fsSL "$VERDACCIO_URL/$enc")" || {
    echo "WARN: metadata fetch failed for $name@$version" >&2
    return
  }

  local tarball
  tarball="$(jq -r --arg v "$version" '.versions[$v].dist.tarball // ""' <<<"$meta" 2>/dev/null)"

  local unscoped="${name##*/}"
  local base="${unscoped}-${version}.tgz"
  local local_url="$VERDACCIO_URL/$enc/-/$base"

  # SAFE filename (no slashes)
  local safe_name="${name//@/at}"
  safe_name="${safe_name//\//-}"
  local dest="$OUT_DIR/${safe_name}-${version}.tgz"
  local tmp="${dest}.part"

  echo "GET $local_url  ->  $dest"

  if ! curl -fSL --retry 3 --retry-delay 1 -o "$tmp" "$local_url"; then
    if [ -n "$tarball" ]; then
      echo "FALLBACK GET $tarball  ->  $dest"
      curl -fSL --retry 3 --retry-delay 1 -o "$tmp" "$tarball" || {
        echo "ERROR: failed to download tarball for $name@$version" >&2
        rm -f "$tmp"
        return
      }
    else
      echo "WARN: no tarball URL for $name@$version" >&2
      return
    fi
  fi

  mv "$tmp" "$dest"
}

for line in "${pairs[@]}"; do
  name="$(cut -f1 <<<"$line")"
  version="$(cut -f2 <<<"$line")"
  [ -n "$name" ] && [ -n "$version" ] || continue

  semaphore
  sweep_one "$name" "$version" &
   pids+=($!)
done

for pid in "${pids[@]}"; do wait "$pid"; done
