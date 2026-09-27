#!/usr/bin/env bash
# Cross-compile the `cx` thin client for every supported target and emit a
# SHA256SUMS manifest. Run from the repo root:  bash build_client.sh
#
# The output filenames are a hard contract with another component — do not
# rename them.
set -euo pipefail

# Resolve the repo root (this script's directory) so it works from anywhere.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

# Absolute output dir; the module lives in client/cx and we build from there so
# `go build` has a module context (there is no go.mod at the repo root).
OUT="$ROOT/dist/cx"
MODDIR="$ROOT/client/cx"

# target triples -> output filename
targets=(
  "linux amd64 cx-linux-amd64"
  "linux arm64 cx-linux-arm64"
  "darwin amd64 cx-darwin-amd64"
  "darwin arm64 cx-darwin-arm64"
  "windows amd64 cx-windows-amd64.exe"
  "windows arm64 cx-windows-arm64.exe"
)

echo "==> Building cx into $OUT/"
mkdir -p "$OUT"

for t in "${targets[@]}"; do
  # shellcheck disable=SC2086
  set -- $t
  goos="$1"; goarch="$2"; name="$3"
  echo "  - $name  (GOOS=$goos GOARCH=$goarch)"
  ( cd "$MODDIR" && GOOS="$goos" GOARCH="$goarch" CGO_ENABLED=0 go build -o "$OUT/$name" . )
done

echo "==> Writing $OUT/SHA256SUMS"
(
  cd "$OUT"
  # List only the binaries, not the manifest itself; "<sha>  <filename>".
  sha256sum cx-* > SHA256SUMS
)

echo "==> Done. Artifacts:"
ls -l "$OUT"
