#!/usr/bin/env bash
set -euo pipefail

# Create a review bundle from the deployed V10.7.6 tree without changing it.
# The bundle is deliberately restricted to source, tests and build metadata.

readonly SOURCE_ROOT="${1:-/home/pi/FT8Commander}"
readonly OUTPUT_FILE="${2:-/home/pi/ft8commander-v10.7.6-review.tar.gz}"
readonly STAGE_PARENT="/tmp"

declare -Ar EXPECTED_SHA256=(
  [ft8ctrl.py]="d2ecc5b4aba7b3671863e527f4bc03717366054de0607ff7302d2ec7dd65314a"
  [v60_runtime.py]="af75a365edd32688793ccdf0557e5c57ffe0fea4c5b4296b0c14338397f06b0a"
  [v107_policy.py]="3ea711332e5b0dfbd8b66b31829eae51fb45be8087257e02786d75f65a34e11c"
  [v1076_terminal_revisit.py]="73fa3cacb4cac10ce56d36dc86f915af3e63701080aff29e1af403399c694b21"
)

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

[[ -d "$SOURCE_ROOT" ]] || fail "source directory not found: $SOURCE_ROOT"
[[ "$OUTPUT_FILE" = /* ]] || fail "output path must be absolute"

for filename in "${!EXPECTED_SHA256[@]}"; do
  filepath="$SOURCE_ROOT/$filename"
  [[ -f "$filepath" && ! -L "$filepath" ]] || fail "required regular file missing: $filepath"
  actual_sha256="$(sha256sum "$filepath" | awk '{print $1}')"
  [[ "$actual_sha256" == "${EXPECTED_SHA256[$filename]}" ]] ||
    fail "SHA-256 mismatch for $filename: expected ${EXPECTED_SHA256[$filename]}, got $actual_sha256"
done

stage_dir="$(mktemp -d "$STAGE_PARENT/ft8cmd-export.XXXXXXXX")"
cleanup() {
  case "$stage_dir" in
    /tmp/ft8cmd-export.*) rm -rf -- "$stage_dir" ;;
    *) printf 'WARNING: refusing to remove unexpected temporary path: %s\n' "$stage_dir" >&2 ;;
  esac
}
trap cleanup EXIT

mkdir -p "$stage_dir/source" "$stage_dir/tests" "$stage_dir/plugins" "$stage_dir/support"

# Root Python modules are code. Configuration and runtime data use other names
# or extensions and are intentionally not selected.
while IFS= read -r -d '' filepath; do
  install -m 0644 "$filepath" "$stage_dir/source/$(basename "$filepath")"
done < <(find "$SOURCE_ROOT" -maxdepth 1 -type f -name '*.py' -print0 | sort -z)

# Only test source and small declarative fixtures are collected. Logs, ADIF,
# databases, keys, environment files and backups are excluded by construction.
if [[ -d "$SOURCE_ROOT/tests" ]]; then
  while IFS= read -r -d '' filepath; do
    relative_path="${filepath#"$SOURCE_ROOT/tests/"}"
    destination="$stage_dir/tests/$relative_path"
    mkdir -p "$(dirname "$destination")"
    install -m 0644 "$filepath" "$destination"
  done < <(
    find "$SOURCE_ROOT/tests" -type f \
      \( -name '*.py' -o -name '*.json' -o -name '*.txt' -o \
         -name '*.yaml' -o -name '*.yml' -o -name '*.ini' -o \
         -name '*.toml' -o -name '*.cfg' \) -print0 | sort -z
  )
fi

if [[ -d "$SOURCE_ROOT/plugins" ]]; then
  while IFS= read -r -d '' filepath; do
    install -m 0644 "$filepath" "$stage_dir/plugins/$(basename "$filepath")"
  done < <(find "$SOURCE_ROOT/plugins" -maxdepth 1 -type f -name '*.py' -print0 | sort -z)
fi

while IFS= read -r -d '' filepath; do
  install -m 0644 "$filepath" "$stage_dir/support/$(basename "$filepath")"
done < <(find "$SOURCE_ROOT" -maxdepth 1 -type f -name '*.sh' -print0 | sort -z)

for metadata in \
  requirements.txt requirements-dev.txt pyproject.toml setup.cfg pytest.ini tox.ini \
  .flake8 .pre-commit-config.yaml pylintrc LICENSE VERSION README.md README-V6.0.md \
  ft8ctrl.yaml.sample; do
  if [[ -f "$SOURCE_ROOT/$metadata" && ! -L "$SOURCE_ROOT/$metadata" ]]; then
    install -m 0644 "$SOURCE_ROOT/$metadata" "$stage_dir/$metadata"
  fi
done

(
  cd "$stage_dir"
  find source tests plugins support -type f -print0 | sort -z | xargs -0 sha256sum > SHA256SUMS
)

cat > "$stage_dir/REVIEW_REQUIRED.txt" <<'EOF'
This is a review bundle, not a publishable release.

Before copying any file into the public repository:
- inspect every file for embedded credentials and private network details;
- confirm the four documented V10.7.6 fingerprints;
- inspect test fixtures for personal logs, calls or location data;
- run the complete test suite with the service Python;
- do not import runtime configuration, SQLite, ADIF, logs or backups.
EOF

mkdir -p "$(dirname "$OUTPUT_FILE")"
tar -C "$stage_dir" -czf "$OUTPUT_FILE" .

printf 'V10.7.6 fingerprints: OK\n'
printf 'Review bundle created: %s\n' "$OUTPUT_FILE"
printf 'Bundle SHA-256: '
sha256sum "$OUTPUT_FILE" | awk '{print $1}'
printf 'No service was stopped or modified.\n'
