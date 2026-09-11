#!/bin/zsh
# Rewrite plugin output GeoTIFFs as tiled, overviewed COGs so QGIS can pan them.
#
#   retile_outputs.sh <dir-or-file> [...]          lossless: tiling + overviews + ZSTD only
#   RETILE_F32=1 retile_outputs.sh <dir-or-file>   also cast Float64 change rasters to Float32
#
# The lossless mode returns bit-identical pixel values; only the on-disk layout
# changes. The F32 cast is irreversible, so it is opt-in.
set -e
Q=/Applications/QGIS-LTR.app/Contents/MacOS
export GDAL_DATA=/Applications/QGIS-LTR.app/Contents/Resources/share/gdal
export PROJ_DATA=/Applications/QGIS-LTR.app/Contents/Resources/qgis/proj
export GDAL_CACHEMAX=1024

retile () {
  local f=$1
  local info=$($Q/gdalinfo "$f" 2>/dev/null)
  local tiled=0 f64=0
  [[ $info == *"Block=512x512"* ]] && tiled=1
  [[ $info == *"Type=Float64"*   ]] && f64=1
  # A file that is already tiled still needs a pass when the float32 narrowing is
  # asked for and has not happened yet -- checking the layout alone would skip
  # every raster a previous lossless run already converted.
  local ot=()
  if (( f64 )) && [[ -n ${RETILE_F32:-} ]]; then
    ot=(-ot Float32)
  elif (( tiled )); then
    echo "  skip (already done)   $f:t"; return
  fi
  local before=$(stat -f%z "$f")
  $Q/gdal_translate -q -of COG $ot \
      -co COMPRESS=ZSTD -co LEVEL=1 -co PREDICTOR=YES \
      -co BLOCKSIZE=512 -co OVERVIEW_RESAMPLING=AVERAGE \
      -co NUM_THREADS=ALL_CPUS \
      "$f" "$f.retile.tmp" 2>/dev/null
  # only replace once the new file opens cleanly -- never leave a half-written raster
  $Q/gdalinfo "$f.retile.tmp" >/dev/null 2>&1 || { rm -f "$f.retile.tmp"; echo "  FAILED  $f:t"; return; }
  mv "$f.retile.tmp" "$f"
  local after=$(stat -f%z "$f")
  printf "  %5.0f -> %5.0f MB  %s\n" $((before/1048576.0)) $((after/1048576.0)) "$f:t"
}

for arg in "$@"; do
  if [[ -d $arg ]]; then
    find "$arg" -name '*.tif' -size +20M | while read -r f; do retile "$f"; done
  else
    retile "$arg"
  fi
done
