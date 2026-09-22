#!/usr/bin/env bash
# Run the merge-rule benchmark with QGIS's own Python (it needs osgeo/GDAL).
#
#   ./benchmarks/merge_rules/run.sh              # generate SAR, then score all six
#   ./benchmarks/merge_rules/run.sh Valdez Knik  # just these events
#   SKIP_SAR=1 ./benchmarks/merge_rules/run.sh   # re-score cached SAR maps only
#   ./benchmarks/merge_rules/run.sh --no-cloud   # score without the cloud mask
#
# Not part of tests/run_all.sh: it needs the network and takes several minutes
# the first time (~80 scene downloads, cached afterwards).
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"

PY="${QGIS_PYTHON:-}"
if [ -z "$PY" ]; then
  for p in /Applications/QGIS-LTR.app/Contents/Frameworks/bin/python3 \
           /Applications/QGIS.app/Contents/Frameworks/bin/python3; do
    [ -x "$p" ] && { PY="$p"; break; }
  done
fi
[ -x "${PY:-}" ] || { echo "Set QGIS_PYTHON to the python inside your QGIS app bundle."; exit 2; }

for base in /Applications/QGIS-LTR.app /Applications/QGIS.app; do
  [ -d "$base/Contents/Resources/qgis/proj" ] && {
    export PROJ_LIB="$base/Contents/Resources/qgis/proj"
    export PROJ_DATA="$PROJ_LIB"
    export GDAL_DATA="$base/Contents/Resources/qgis/gdal"
    break; }
done
unset PYTHONHOME PYTHONPATH

events=(); flags=()
for a in "$@"; do [[ "$a" == --* ]] && flags+=("$a") || events+=("$a"); done
if [ -z "${SKIP_SAR:-}" ]; then
  "$PY" "$HERE/make_sar.py" ${events[@]+"${events[@]}"} || exit 1
fi
"$PY" "$HERE/score.py" ${events[@]+"${events[@]}"} ${flags[@]+"${flags[@]}"}
