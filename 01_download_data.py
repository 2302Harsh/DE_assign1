"""Download the raw EV-charger and ABS SA4 boundary datasets.

Run with: python 01_download_data.py [--force]

Files that already exist are skipped; pass --force to download them again.
"""

import argparse
import shutil
import zipfile
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.error import URLError
from urllib.request import Request, urlopen

from common import EV_CSV_PATH, RAW_DATA_DIR, SA4_SHAPEFILE_PATH


# Official Transport for NSW and ABS download endpoints.
EV_CHARGER_URL = (
    "https://opendata.transport.nsw.gov.au/data/dataset/"
    "be1c4de4-4517-4bd0-8a09-2965ddfc7179/resource/"
    "7bbb6461-e52d-4fe7-ace4-a15c30198de0/download/ev_20251216.csv"
)
ABS_SA4_URL = (
    "https://www.abs.gov.au/statistics/standards/"
    "australian-statistical-geography-standard-asgs/"
    "edition-4-july-2026-june-2031/access-and-downloads/"
    "digital-boundary-files/SA4_2026_AUST_SHP_GDA2020.zip"
)


def download_file(url: str, output_path: Path) -> None:
    """Download *url* to *output_path* without leaving partial files behind.

    Strategy: stream the download to a temporary ".part" file in the same
    folder, validate it, and only then rename it to the real output path.
    Path.replace() is an atomic move on both Windows and Linux, so a crash or
    Ctrl-C part-way through a download can never leave a corrupt/half-written
    file sitting at output_path - the real file is either the old one or the
    fully-downloaded new one, never something in between.
    """
    print(f"Downloading {output_path.name}...")
    request = Request(url, headers={"User-Agent": "DE-assign1-data-download/1.0"})

    try:
        with urlopen(request, timeout=60) as response:
            content_type = response.headers.get_content_type()
            # The Transport for NSW / ABS servers sometimes return an HTML
            # error page (e.g. "resource moved") with a 200 status instead of
            # the file, so a content-type check is needed on top of the HTTP
            # status to catch that case.
            if output_path.suffix == ".csv" and content_type == "text/html":
                raise ValueError(f"Expected CSV, but {url} returned HTML.")

            # Write to a uniquely-named temp file next to the target, then
            # rename it below once the whole file has downloaded successfully.
            with NamedTemporaryFile("wb", delete=False, dir=output_path.parent,
                                    suffix=".part") as temporary_file:
                temporary_path = Path(temporary_file.name)
                shutil.copyfileobj(response, temporary_file, length=1024 * 1024)
    except (URLError, TimeoutError) as error:  # HTTPError is a URLError
        raise RuntimeError(f"Could not download {url}: {error}") from error

    try:
        if output_path.suffix == ".zip":
            # ZipFile raises BadZipFile for a non-ZIP file; testzip checks every member.
            # Together these catch both "not actually a zip" and "zip is truncated
            # or corrupted" before the file is treated as good.
            with zipfile.ZipFile(temporary_path) as archive:
                bad_member = archive.testzip()
            if bad_member:
                raise zipfile.BadZipFile(f"Corrupt ZIP member: {bad_member}")
        # Validation passed: atomically promote the temp file to the real path.
        temporary_path.replace(output_path)
    except Exception:
        # Validation failed: clean up the temp file so it doesn't linger, then
        # re-raise so the caller (main) sees the original error.
        temporary_path.unlink(missing_ok=True)
        raise

    print(f"Saved to {output_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--force", action="store_true", help="download files that already exist")
    force = parser.parse_args().force
    RAW_DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Skip the EV charger CSV if it's already on disk - downloads only cost
    # time and bandwidth, they don't change the analysis, so a rerun of the
    # whole pipeline shouldn't have to re-fetch data it already has.
    if force or not EV_CSV_PATH.exists():
        download_file(EV_CHARGER_URL, EV_CSV_PATH)
    else:
        print(f"{EV_CSV_PATH.name} already exists; skipping (use --force to re-download).")

    # Same idea for the ABS shapefile, which additionally needs unzipping.
    if force or not SA4_SHAPEFILE_PATH.exists():
        sa4_zip_path = RAW_DATA_DIR / "SA4_2026.zip"
        download_file(ABS_SA4_URL, sa4_zip_path)
        with zipfile.ZipFile(sa4_zip_path) as archive:
            archive.extractall(SA4_SHAPEFILE_PATH.parent)
        sa4_zip_path.unlink()  # the zip itself isn't needed once extracted
        print("Unzipped ABS shapefiles successfully.")
    else:
        print(f"{SA4_SHAPEFILE_PATH.name} already exists; skipping (use --force to re-download).")


if __name__ == "__main__":
    main()
