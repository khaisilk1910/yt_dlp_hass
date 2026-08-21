#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 vX.Y.Z" >&2
  exit 2
fi

TAG="$1"
VERSION="${TAG#v}"
if [[ "$TAG" == "$VERSION" || ! "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "Invalid tag '$TAG'. Use vX.Y.Z, for example v0.3.0" >&2
  exit 2
fi

if [[ -n "$(git status --porcelain)" ]]; then
  echo "Working tree is not clean. Commit or stash changes first." >&2
  exit 1
fi

MANIFEST="custom_components/yt_dlp/manifest.json"
python3 - "$MANIFEST" "$VERSION" <<'PY'
import json
from pathlib import Path
import sys

path = Path(sys.argv[1])
version = sys.argv[2]
manifest = json.loads(path.read_text(encoding="utf-8"))
manifest["version"] = version
ordered = {
    "domain": manifest.pop("domain"),
    "name": manifest.pop("name"),
    **{key: manifest[key] for key in sorted(manifest)},
}
path.write_text(
    json.dumps(ordered, ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)
print(f"Updated {path} to version {version}")
PY

python3 -m compileall -q custom_components/yt_dlp
find custom_components/yt_dlp -type d -name __pycache__ -prune -exec rm -rf {} +
find custom_components/yt_dlp -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete

git add "$MANIFEST"
git commit -m "Release $TAG"
git tag -a "$TAG" -m "Release $TAG"

echo
echo "Created release commit and tag $TAG."
echo "Push them with:"
echo "  git push origin main"
echo "  git push origin $TAG"
