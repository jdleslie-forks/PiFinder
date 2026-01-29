#!/usr/bin/env python3
"""
Build-time script to pre-process star data into SQLite.

This runs on the development machine (not on the Pi) and requires pandas.
It processes:
1. Hipparcos star catalog → stars table
2. Constellation lines → constellation_edges table

Usage:
    python scripts/build_star_database.py

Output:
    Updates astro_data/pifinder_objects.db with stars and constellation tables
"""

import os
import sys
import sqlite3
from pathlib import Path

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

# These imports are only available at build time
import pandas as pd
import requests

# Hipparcos catalog URL
HIPPARCOS_URL = 'https://cdsarc.cds.unistra.fr/ftp/cats/I/239/hip_main.dat'

# Column spec from skyfield
HIPPARCOS_COLUMNS = (
    'Catalog', 'HIP', 'Proxy', 'RAhms', 'DEdms', 'Vmag',
    'VarFlag', 'r_Vmag', 'RAdeg', 'DEdeg', 'AstroRef', 'Plx', 'pmRA',
    'pmDE', 'e_RAdeg', 'e_DEdeg', 'e_Plx', 'e_pmRA', 'e_pmDE', 'DE:RA',
    'Plx:RA', 'Plx:DE', 'pmRA:RA', 'pmRA:DE', 'pmRA:Plx', 'pmDE:RA',
    'pmDE:DE', 'pmDE:Plx', 'pmDE:pmRA', 'F1', 'F2', '---', 'BTmag',
    'e_BTmag', 'VTmag', 'e_VTmag', 'm_BTmag', 'B-V', 'e_B-V', 'r_B-V',
    'V-I', 'e_V-I', 'r_V-I', 'CombMag', 'Hpmag', 'e_Hpmag', 'Hpscat',
    'o_Hpmag', 'm_Hpmag', 'Hpmax', 'HPmin', 'Period', 'HvarType',
    'moreVar', 'morePhoto', 'CCDM', 'n_CCDM', 'Nsys', 'Ncomp',
    'MultFlag', 'Source', 'Qual', 'm_HIP', 'theta', 'rho', 'e_rho',
    'dHp', 'e_dHp', 'Survey', 'Chart', 'Notes', 'HD', 'BD', 'CoD',
    'CPD', '(V-I)red', 'SpType', 'r_SpType',
)

MAGNITUDE_LIMIT = 7.5  # Pre-filter to bright stars


def download_hipparcos(cache_path: Path) -> Path:
    """Download Hipparcos catalog if not cached."""
    if cache_path.exists():
        print(f"Using cached Hipparcos data: {cache_path}")
        return cache_path

    print(f"Downloading Hipparcos catalog from {HIPPARCOS_URL}...")
    response = requests.get(HIPPARCOS_URL, stream=True)
    response.raise_for_status()

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with open(cache_path, 'wb') as f:
        for chunk in response.iter_content(chunk_size=8192):
            f.write(chunk)

    print(f"Downloaded to {cache_path}")
    return cache_path


def parse_hipparcos(hip_path: Path) -> pd.DataFrame:
    """Parse Hipparcos catalog using pandas."""
    print("Parsing Hipparcos catalog...")

    df = pd.read_csv(
        hip_path,
        sep='|',
        names=HIPPARCOS_COLUMNS,
        usecols=['HIP', 'Vmag', 'RAdeg', 'DEdeg', 'Plx', 'pmRA', 'pmDE'],
        na_values=['     ', '       ', '        ', '            '],
    )

    df.columns = [
        'hip_id', 'magnitude', 'ra_degrees', 'dec_degrees',
        'parallax_mas', 'ra_mas_per_year', 'dec_mas_per_year',
    ]

    # Add ra_hours for skyfield Star compatibility
    df['ra_hours'] = df['ra_degrees'] / 15.0

    # Filter to bright stars
    bright = df[df['magnitude'] <= MAGNITUDE_LIMIT].copy()

    # Drop rows with missing essential data
    bright = bright.dropna(subset=['ra_degrees', 'dec_degrees', 'magnitude'])

    # Fill missing proper motion and parallax with 0
    bright['parallax_mas'] = bright['parallax_mas'].fillna(0)
    bright['ra_mas_per_year'] = bright['ra_mas_per_year'].fillna(0)
    bright['dec_mas_per_year'] = bright['dec_mas_per_year'].fillna(0)

    print(f"  Total stars: {len(df)}")
    print(f"  Bright stars (mag <= {MAGNITUDE_LIMIT}): {len(bright)}")

    return bright


def parse_constellations(fab_path: Path) -> list:
    """Parse Stellarium constellation file."""
    print(f"Parsing constellation data from {fab_path}...")

    edges = []
    with open(fab_path, 'r') as f:
        for line in f:
            parts = line.strip().split()
            if len(parts) < 3:
                continue

            constellation = parts[0]
            # Format: CONST count hip1 hip2 hip3 hip4 ...
            # Pairs are edges: (hip1, hip2), (hip3, hip4), ...
            hip_ids = [int(x) for x in parts[2:]]

            # Pairs represent edges
            for i in range(0, len(hip_ids) - 1, 2):
                edges.append({
                    'constellation': constellation,
                    'start_hip': hip_ids[i],
                    'end_hip': hip_ids[i + 1],
                })

    print(f"  Constellation edges: {len(edges)}")
    return edges


def create_tables(conn: sqlite3.Connection):
    """Create stars and constellation_edges tables."""
    cursor = conn.cursor()

    # Drop existing tables
    cursor.execute("DROP TABLE IF EXISTS stars")
    cursor.execute("DROP TABLE IF EXISTS constellation_edges")

    # Stars table
    cursor.execute("""
        CREATE TABLE stars (
            hip_id INTEGER PRIMARY KEY,
            ra_hours REAL NOT NULL,
            ra_degrees REAL NOT NULL,
            dec_degrees REAL NOT NULL,
            magnitude REAL NOT NULL,
            parallax_mas REAL DEFAULT 0,
            ra_mas_per_year REAL DEFAULT 0,
            dec_mas_per_year REAL DEFAULT 0
        )
    """)

    # Index for magnitude filtering
    cursor.execute("CREATE INDEX idx_stars_magnitude ON stars(magnitude)")

    # Constellation edges table
    cursor.execute("""
        CREATE TABLE constellation_edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            constellation TEXT NOT NULL,
            start_hip INTEGER NOT NULL,
            end_hip INTEGER NOT NULL,
            FOREIGN KEY (start_hip) REFERENCES stars(hip_id),
            FOREIGN KEY (end_hip) REFERENCES stars(hip_id)
        )
    """)

    conn.commit()
    print("Created stars and constellation_edges tables")


def insert_stars(conn: sqlite3.Connection, stars_df: pd.DataFrame):
    """Insert star data into SQLite."""
    print("Inserting star data...")

    cursor = conn.cursor()

    for _, row in stars_df.iterrows():
        cursor.execute("""
            INSERT INTO stars
            (hip_id, ra_hours, ra_degrees, dec_degrees, magnitude,
             parallax_mas, ra_mas_per_year, dec_mas_per_year)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            int(row['hip_id']),
            float(row['ra_hours']),
            float(row['ra_degrees']),
            float(row['dec_degrees']),
            float(row['magnitude']),
            float(row['parallax_mas']),
            float(row['ra_mas_per_year']),
            float(row['dec_mas_per_year']),
        ))

    conn.commit()
    print(f"  Inserted {len(stars_df)} stars")


def insert_constellation_edges(conn: sqlite3.Connection, edges: list):
    """Insert constellation edge data into SQLite."""
    print("Inserting constellation edges...")

    cursor = conn.cursor()

    # Get set of valid HIP IDs from stars table
    cursor.execute("SELECT hip_id FROM stars")
    valid_hips = set(row[0] for row in cursor.fetchall())

    inserted = 0
    skipped = 0

    for edge in edges:
        # Only insert edges where both stars are in our filtered catalog
        if edge['start_hip'] in valid_hips and edge['end_hip'] in valid_hips:
            cursor.execute("""
                INSERT INTO constellation_edges (constellation, start_hip, end_hip)
                VALUES (?, ?, ?)
            """, (edge['constellation'], edge['start_hip'], edge['end_hip']))
            inserted += 1
        else:
            skipped += 1

    conn.commit()
    print(f"  Inserted {inserted} edges, skipped {skipped} (stars not in catalog)")


def main():
    # Paths
    script_dir = Path(__file__).parent
    project_root = script_dir.parent.parent
    astro_data = project_root / "astro_data"

    hip_cache = astro_data / "hip_main.dat"
    constellation_file = astro_data / "constellationship.fab"
    db_path = astro_data / "pifinder_objects.db"

    print(f"Project root: {project_root}")
    print(f"Database: {db_path}")
    print()

    # Download and parse Hipparcos
    hip_path = download_hipparcos(hip_cache)
    stars_df = parse_hipparcos(hip_path)

    # Parse constellations
    if not constellation_file.exists():
        print(f"Warning: Constellation file not found: {constellation_file}")
        edges = []
    else:
        edges = parse_constellations(constellation_file)

    # Update database
    print()
    print(f"Updating database: {db_path}")

    conn = sqlite3.connect(db_path)
    try:
        create_tables(conn)
        insert_stars(conn, stars_df)
        insert_constellation_edges(conn, edges)

        # Verify
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM stars")
        star_count = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM constellation_edges")
        edge_count = cursor.fetchone()[0]

        print()
        print(f"Database updated successfully:")
        print(f"  Stars: {star_count}")
        print(f"  Constellation edges: {edge_count}")

    finally:
        conn.close()


if __name__ == "__main__":
    main()
