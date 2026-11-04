"""Last step of the plugin's "Install imagery tools": prove the environment works.

Imports every third-party package the pipeline uses and every pipeline module,
then loads the OmniCloudMask ensemble once — which downloads its ~58 MB of
weights now, during setup, instead of in the middle of the user's first search.
Exits non-zero (with the reason on the last line) if anything is missing, so the
setup never reports an environment as ready that a search would then fail in.

  python setup_check.py
"""
import importlib
import os
import sys
import time

PACKAGES = ("numpy", "xarray", "rioxarray", "rasterio", "pyproj", "shapely",
            "geopandas", "pyogrio", "pandas", "pystac_client", "planetary_computer",
            "stackstac", "dask", "urllib3", "requests", "planet", "httpx",
            "torch", "omnicloudmask")
PIPELINE = ("imagery", "run_groundtruth", "review_package", "planet_cache",
            "planet_imagery", "sar_imagery", "nisar_imagery", "dem_imagery",
            "run_single")


def main():
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    print(f"Python {sys.version.split()[0]} at {sys.executable}")
    for name in PACKAGES + PIPELINE:
        mod = importlib.import_module(name)
        version = getattr(mod, "__version__", "")
        print(f"  ok  {name} {version}".rstrip())
    import imagery
    t = time.time()
    device, models = imagery._ocm_models()
    print(f"  ok  cloud mask: {len(models)} models on {device} "
          f"({time.time() - t:.0f} s)")
    print("The imagery tools are ready.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"Setup check failed: {type(e).__name__}: {e}")
        sys.exit(1)
