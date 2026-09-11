#!/usr/bin/env bash
# Run every plugin check against QGIS's own Python.
#
# These import qgis.core, so they need the interpreter INSIDE the QGIS app
# bundle — not the project venv, which has the imagery stack but no PyQGIS.
# PROJ/GDAL data paths are exported because a coordinate transform silently
# returns the input unchanged when it cannot find proj.db, which would make a
# CRS test pass for the wrong reason.
#
#   ./tests/run_all.sh            # all of them
#   ./tests/run_all.sh detection  # just the ones matching "detection"
set -uo pipefail
cd "$(dirname "$0")/.."

find_python() {
  if [ -n "${QGIS_PYTHON:-}" ] && [ -x "$QGIS_PYTHON" ]; then echo "$QGIS_PYTHON"; return; fi
  for p in \
    /Applications/QGIS-LTR.app/Contents/Frameworks/bin/python3 \
    /Applications/QGIS.app/Contents/Frameworks/bin/python3 \
    /usr/bin/python3
  do [ -x "$p" ] && "$p" -c "import qgis.core" >/dev/null 2>&1 && { echo "$p"; return; }; done
  command -v python3
}
PY="$(find_python)"
if ! "$PY" -c "import qgis.core" >/dev/null 2>&1; then
  echo "No Python with PyQGIS found. Set QGIS_PYTHON to the python inside your"
  echo "QGIS app bundle, e.g."
  echo "  QGIS_PYTHON=/Applications/QGIS-LTR.app/Contents/Frameworks/bin/python3 ./tests/run_all.sh"
  exit 2
fi

# QGIS's own resources; harmless if the directories are absent.
for base in /Applications/QGIS-LTR.app /Applications/QGIS.app; do
  [ -d "$base/Contents/Resources/qgis/proj" ] && {
    export PROJ_LIB="$base/Contents/Resources/qgis/proj"
    export PROJ_DATA="$PROJ_LIB"
    export GDAL_DATA="$base/Contents/Resources/qgis/gdal"
    break; }
done
# PYTHONHOME leaks from a parent QGIS process and stops the child interpreter
# booting at all ("init_fs_encoding").
unset PYTHONHOME PYTHONPATH
export QT_QPA_PLATFORM=offscreen

FILTER="${1:-}"
pass=0; fail=0; failed=()
for t in tests/test_*.py; do
  [ -n "$FILTER" ] && [[ "$t" != *"$FILTER"* ]] && continue
  name="$(basename "$t")"
  if out="$("$PY" "$t" 2>&1)"; then
    printf '  \033[32mPASS\033[0m  %-28s %s\n' "$name" \
      "$(echo "$out" | grep -v '^WARNING\|QStandardPaths\|proj_get\|Deprecation' | tail -1)"
    pass=$((pass+1))
  else
    printf '  \033[31mFAIL\033[0m  %s\n' "$name"
    echo "$out" | grep -v '^WARNING\|QStandardPaths\|proj_get' | tail -12 | sed 's/^/        /'
    fail=$((fail+1)); failed+=("$name")
  fi
done
echo
echo "  $pass passed, $fail failed"
[ "$fail" -gt 0 ] && { echo "  failed: ${failed[*]}"; exit 1; }
exit 0
