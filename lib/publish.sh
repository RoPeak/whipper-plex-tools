#!/usr/bin/env bash
# Atomic no-clobber file publication for same-filesystem music stages.
confirm_publish_plan() {
  local stage="$1" dest="$2" label="$3" source rel approval
  shift 3
  local files=("$@")
  echo
  echo "$label publication plan (${#files[@]} file(s); source/staging preserved):"
  for source in "${files[@]}"; do
    rel="${source#"$stage"/}"
    printf '  %s -> %s/%s\n' "$source" "$dest" "$rel"
  done
  read -rp "Type APPLY to publish this staged output (anything else cancels): " approval || approval=""
  if [[ "$approval" != "APPLY" ]]; then
    echo "Publication cancelled; staged output is retained at: $stage"
    return 1
  fi
}

publish_file_atomic() {
  local src="$1" target="$2" temp source_size temp_size
  mkdir -p "$(dirname "$target")"
  temp="$(mktemp "$(dirname "$target")/.music-ingest-publish-XXXXXX")" || return 1
  if ! cp -p -- "$src" "$temp"; then
    echo "✘ Fail: could not copy staged file to a temporary destination for $target"
    rm -f -- "$temp"
    return 1
  fi
  source_size="$(stat -c '%s' -- "$src")" || { rm -f -- "$temp"; return 1; }
  temp_size="$(stat -c '%s' -- "$temp")" || { rm -f -- "$temp"; return 1; }
  if [[ "$source_size" != "$temp_size" ]]; then
    echo "✘ Fail: staged copy size mismatch for $target"
    rm -f -- "$temp"
    return 1
  fi
  if ! ln -T -- "$temp" "$target" 2>/dev/null; then
    echo "✘ Fail: destination appeared during publication or cannot be linked: $target"
    rm -f -- "$temp"
    return 1
  fi
  rm -f -- "$temp"
}
