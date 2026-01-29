"""
Comet data handling with lightweight MPC parser.

Parses MPC comet orbital elements without pandas dependency.
Uses direct Kepler orbit computation to avoid skyfield.data.mpc (which imports pandas).
"""

from typing import Callable, Dict, Any, Tuple, Optional, List
from datetime import datetime, timezone
from skyfield.constants import GM_SUN_Pitjeva_2005_km3_s2 as GM_SUN
from skyfield.keplerlib import _KeplerOrbit
from skyfield.data.spice import inertial_frames
from PiFinder.utils import Timer, comet_file
from PiFinder.calc_utils import sf_utils
import requests
import os
import logging
import math

logger = logging.getLogger("Comets")

# MPC comet data URL (same as skyfield.data.mpc.COMET_URL but without importing mpc)
COMET_URL = 'https://www.minorplanetcenter.net/iau/MPCORB/CometEls.txt'


def comet_orbit(row: Dict, ts, gm_km3_s2: float):
    """
    Create a Kepler orbit for a comet from orbital elements.

    This replaces skyfield.data.mpc.comet_orbit() to avoid pandas import.

    Args:
        row: Dict with orbital elements (perihelion_year, perihelion_month,
             perihelion_day, perihelion_distance_au, eccentricity,
             inclination_degrees, longitude_of_ascending_node_degrees,
             argument_of_perihelion_degrees, designation)
        ts: Skyfield Timescale
        gm_km3_s2: Gravitational parameter (GM) in km³/s²

    Returns:
        Skyfield KeplerOrbit object
    """
    e = row['eccentricity']
    if e == 1.0:
        # Parabolic orbit
        p = row['perihelion_distance_au'] * 2.0
    else:
        # Elliptical or hyperbolic orbit
        a = row['perihelion_distance_au'] / (1.0 - e)
        p = a * (1.0 - e * e)

    t_perihelion = ts.tt(
        row['perihelion_year'],
        row['perihelion_month'],
        row['perihelion_day']
    )

    comet = _KeplerOrbit._from_periapsis(
        p,
        e,
        row['inclination_degrees'],
        row['longitude_of_ascending_node_degrees'],
        row['argument_of_perihelion_degrees'],
        t_perihelion,
        gm_km3_s2,
        10,  # center (Sun)
        row['designation'],
    )
    comet._rotation = inertial_frames['ECLIPJ2000'].T
    return comet


def parse_mpc_comet_line(line: str) -> Optional[Dict[str, Any]]:
    """
    Parse a single line from MPC CometEls.txt format.

    Returns dict with fields needed by comet_orbit(),
    or None if line is invalid/header.

    MPC Ephemerides and Orbital Elements Format:
    Cols 1-4:    Periodic comet number (i4)
    Col 5:       Orbit type (a1)
    Cols 6-12:   Provisional designation (a7)
    Cols 15-18:  Year of perihelion (i4)
    Cols 20-21:  Month of perihelion (i2)
    Cols 23-29:  Day of perihelion (f7.4)
    Cols 31-39:  Perihelion distance AU (f9.6)
    Cols 42-49:  Eccentricity (f8.6)
    Cols 52-59:  Argument of perihelion (f8.4)
    Cols 62-69:  Longitude of ascending node (f8.4)
    Cols 72-79:  Inclination (f8.4)
    Cols 82-85:  Epoch year (i4)
    Cols 86-87:  Epoch month (i2)
    Cols 88-89:  Epoch day (i2)
    Cols 92-95:  Absolute magnitude g (f4.1)
    Cols 97-100: Slope parameter k (f4.0)
    Cols 103-158: Designation and name (a56)
    Cols 160-168: Reference (a9)
    """
    if len(line) < 100:
        return None

    try:
        # Parse perihelion time
        perihelion_year = int(line[14:18].strip())
        perihelion_month = int(line[19:21].strip())
        perihelion_day = float(line[22:29].strip())

        # Parse orbital elements
        perihelion_distance = float(line[30:39].strip())
        eccentricity = float(line[41:49].strip())
        arg_perihelion = float(line[51:59].strip())
        long_asc_node = float(line[61:69].strip())
        inclination = float(line[71:79].strip())

        # Parse magnitude parameters (may be blank)
        mag_g_str = line[91:95].strip()
        mag_k_str = line[96:100].strip()
        magnitude_g = float(mag_g_str) if mag_g_str else 10.0  # default
        magnitude_k = float(mag_k_str) if mag_k_str else 5.0   # default

        # Parse designation/name
        designation = line[102:158].strip()

        # Parse reference for deduplication
        reference = line[159:168].strip() if len(line) > 159 else ""

        return {
            'designation': designation,
            'reference': reference,
            'perihelion_year': perihelion_year,
            'perihelion_month': perihelion_month,
            'perihelion_day': perihelion_day,
            'perihelion_distance_au': perihelion_distance,
            'eccentricity': eccentricity,
            'argument_of_perihelion_degrees': arg_perihelion,
            'longitude_of_ascending_node_degrees': long_asc_node,
            'inclination_degrees': inclination,
            'magnitude_g': magnitude_g,
            'magnitude_k': magnitude_k,
        }
    except (ValueError, IndexError):
        return None


def load_comets_lightweight(filepath: str) -> Dict[str, Dict[str, Any]]:
    """
    Load comet orbital elements from MPC file without pandas.

    Returns dict mapping designation -> orbital elements dict.
    Deduplicates by keeping latest reference for each designation.
    """
    comets = {}

    with open(filepath, 'rb') as f:
        for line_bytes in f:
            try:
                line = line_bytes.decode('utf-8', errors='ignore')
            except UnicodeDecodeError:
                continue

            parsed = parse_mpc_comet_line(line)
            if parsed is None:
                continue

            designation = parsed['designation']
            reference = parsed['reference']

            # Keep entry with latest reference (lexicographic comparison works for MPC refs)
            if designation not in comets or reference > comets[designation]['reference']:
                comets[designation] = parsed

    return comets


def process_comet(comet_data: Tuple[str, Dict], dt: datetime) -> Dict[str, Any]:
    """Process a single comet and compute its current position."""
    name, row = comet_data
    t = sf_utils.ts.from_datetime(dt)
    sun = sf_utils.eph["sun"]

    # Use our local comet_orbit() instead of mpc.comet_orbit()
    comet = sun + comet_orbit(row, sf_utils.ts, GM_SUN)

    topocentric = (comet - sf_utils.observer_loc).at(t)
    heliocentric = (comet - sun).at(t)

    ra, dec, earth_distance = topocentric.radec(sf_utils.ts.J2000)
    sun_distance = heliocentric.radec(sf_utils.ts.J2000)[2]

    mag_g = float(row["magnitude_g"])
    mag_k = float(row["magnitude_k"])
    mag = (
        mag_g
        + 2.5 * mag_k * math.log10(sun_distance.au)
        + 5.0 * math.log10(earth_distance.au)
    )
    if mag > 15:
        return {}

    ra_dec = (ra._degrees, dec.degrees)

    return {
        "name": name,
        "radec": ra_dec,
        "mag": mag,
        "earth_distance": earth_distance.au,
        "sun_distance": sun_distance.au,
        "orbital_elements": None,
        "row": row,
    }


def comet_data_download(
    local_filename: str, url: str = COMET_URL
) -> Tuple[bool, Optional[float]]:
    """
    Download the latest comet data from the Minor Planet Center.
    Return values are success and the age of the file in days, if available.
    """
    try:
        now = datetime.now(timezone.utc)

        # Send a HEAD request to get headers without downloading the entire file
        response = requests.head(url)
        response.raise_for_status()

        # Try to get the Last-Modified header
        last_modified = response.headers.get("Last-Modified")

        if last_modified:
            remote_date = datetime.strptime(
                last_modified, "%a, %d %b %Y %H:%M:%S GMT"
            ).replace(tzinfo=timezone.utc)
            logger.debug(f"Remote Last-Modified: {remote_date}")

            # Check if local file exists and its modification time
            if os.path.exists(local_filename):
                local_date = datetime.fromtimestamp(
                    os.path.getmtime(local_filename)
                ).replace(tzinfo=timezone.utc)
                logger.debug(f"Local Last-Modified: {local_date}")

                if remote_date <= local_date:
                    logger.debug("Local file is up to date. No download needed.")
                    return True, round((now - local_date).days)

            # Download the file if it's new or doesn't exist locally
            logger.debug("Downloading new file...")
            response = requests.get(url)
            response.raise_for_status()

            with open(local_filename, "wb") as f:
                f.write(response.content)

            # Set the file's modification time to match the server's last-modified time
            os.utime(local_filename, (remote_date.timestamp(), remote_date.timestamp()))

            logger.debug("File downloaded successfully.")
            return True, round((now - remote_date).days)
        else:
            logger.debug("Last-Modified header not available. Downloading file...")
            response = requests.get(url)
            response.raise_for_status()

            with open(local_filename, "wb") as f:
                f.write(response.content)

            logger.debug("File downloaded successfully.")
            return True, None

    except requests.RequestException as e:
        logger.error(f"Error downloading comet data: {e}")
        return False, None


def check_if_comet_download_needed(comet_file: str) -> Tuple[bool, str]:
    """
    Check if comet data file needs to be downloaded.

    Returns:
        Tuple of (need_download, reason)
    """
    if not os.path.exists(comet_file):
        return True, "Comet file does not exist"

    # Check file age - MPC data should be refreshed every 30 days
    file_age_days = (datetime.now().timestamp() - os.path.getmtime(comet_file)) / 86400
    if file_age_days > 30:
        return True, f"Comet file is {file_age_days:.1f} days old (>30 days)"

    return False, f"Comet file is fresh ({file_age_days:.1f} days old)"


def calc_comets(
    dt: datetime,
    comet_names: Optional[List[str]] = None,
    progress_callback: Optional[Callable[[int], None]] = None
) -> Dict[str, Any]:
    """
    Calculate comet positions for the given datetime.

    Args:
        dt: Datetime for position calculation
        comet_names: Optional list of comet names to calculate (for updates)
        progress_callback: Optional callback function that receives progress percentage (0-100)

    Returns:
        Dict mapping comet name -> position/magnitude data
    """
    with Timer("calc_comets()"):
        comet_dict: Dict[str, Any] = {}
        if sf_utils.observer_loc is None or dt is None:
            logger.debug(
                f"calc_comets can't run: observer loc is None: {sf_utils.observer_loc is None}, dt is None: {dt is None}"
            )
            return comet_dict

        # Report 0% at start (before slow file loading/processing)
        if progress_callback:
            progress_callback(0)

        # Load comets using lightweight parser (no pandas!)
        comets_data = load_comets_lightweight(comet_file)

        # Report progress after file loading (roughly 33% of setup time)
        if progress_callback:
            progress_callback(1)

        # Lightweight loading doesn't have separate pandas processing step,
        # but report 2% to match upstream behavior/UI expectations
        if progress_callback:
            progress_callback(2)

        total_comets = len(comets_data)
        processed = 0

        for name, row in comets_data.items():
            if comet_names is None or name in comet_names:
                result = process_comet((name, row), dt)
                if result:
                    comet_dict[result["name"]] = result

            # Report progress during iteration
            processed += 1
            if progress_callback and total_comets > 0:
                progress = int((processed / total_comets) * 100)
                progress_callback(progress)

        return comet_dict
