#!/usr/bin/python
# -*- coding:utf-8 -*-
"""
This module handles plotting starfields
and constellations.

Uses SQLite for star data instead of pandas for reduced memory footprint.
"""

import os
import datetime
import sqlite3
import numpy as np
from pathlib import Path
from PiFinder import utils
from PIL import Image, ImageDraw, ImageChops

from skyfield.api import Star, utc, Angle
from skyfield.projections import build_stereographic_projection
from PiFinder.calc_utils import sf_utils


class StarData:
    """
    Holds star data loaded from SQLite as numpy arrays.
    Provides array-based access compatible with skyfield Star().
    """

    def __init__(self, db_path: Path, mag_limit: float = 7.5):
        """Load star data from SQLite database."""
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        # Load stars up to magnitude limit
        cursor.execute("""
            SELECT hip_id, ra_hours, dec_degrees, magnitude,
                   parallax_mas, ra_mas_per_year, dec_mas_per_year
            FROM stars
            WHERE magnitude <= ?
            ORDER BY magnitude
        """, (mag_limit,))

        rows = cursor.fetchall()
        n_stars = len(rows)

        # Store as numpy arrays for efficient vectorized operations
        self.hip_ids = np.array([r[0] for r in rows], dtype=np.int32)
        self.ra_hours = np.array([r[1] for r in rows], dtype=np.float64)
        self.dec_degrees = np.array([r[2] for r in rows], dtype=np.float64)
        self.magnitude = np.array([r[3] for r in rows], dtype=np.float32)
        self.parallax_mas = np.array([r[4] for r in rows], dtype=np.float64)
        self.ra_mas_per_year = np.array([r[5] for r in rows], dtype=np.float64)
        self.dec_mas_per_year = np.array([r[6] for r in rows], dtype=np.float64)

        # Build hip_id -> index mapping for constellation lookups
        self._hip_to_idx = {hip: idx for idx, hip in enumerate(self.hip_ids)}

        conn.close()

    def __len__(self):
        return len(self.hip_ids)

    def get_index(self, hip_id: int) -> int:
        """Get array index for a HIP ID."""
        return self._hip_to_idx.get(hip_id, -1)

    def create_skyfield_stars(self, indices=None):
        """
        Create skyfield Star object from data.
        If indices provided, create only for those stars.
        """
        if indices is None:
            return Star(
                ra_hours=self.ra_hours,
                dec_degrees=self.dec_degrees,
                ra_mas_per_year=self.ra_mas_per_year,
                dec_mas_per_year=self.dec_mas_per_year,
                parallax_mas=self.parallax_mas,
            )
        else:
            return Star(
                ra_hours=self.ra_hours[indices],
                dec_degrees=self.dec_degrees[indices],
                ra_mas_per_year=self.ra_mas_per_year[indices],
                dec_mas_per_year=self.dec_mas_per_year[indices],
                parallax_mas=self.parallax_mas[indices],
            )


class ConstellationData:
    """Holds constellation edge data from SQLite."""

    def __init__(self, db_path: Path, star_data: StarData):
        """Load constellation edges from SQLite."""
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        cursor.execute("""
            SELECT start_hip, end_hip FROM constellation_edges
        """)

        rows = cursor.fetchall()
        conn.close()

        # Convert HIP IDs to star indices
        start_indices = []
        end_indices = []

        for start_hip, end_hip in rows:
            start_idx = star_data.get_index(start_hip)
            end_idx = star_data.get_index(end_hip)
            if start_idx >= 0 and end_idx >= 0:
                start_indices.append(start_idx)
                end_indices.append(end_idx)

        self.start_indices = np.array(start_indices, dtype=np.int32)
        self.end_indices = np.array(end_indices, dtype=np.int32)

    def __len__(self):
        return len(self.start_indices)


class Starfield:
    """
    Plots a starfield at the
    specified RA/DEC + roll
    """

    def __init__(self, colors, resolution, mag_limit=7, fov=10.2):
        self.colors = colors
        self.resolution = resolution
        utctime = datetime.datetime(2023, 1, 1, 2, 0, 0).replace(tzinfo=utc)
        ts = sf_utils.ts
        self.t = ts.from_datetime(utctime)
        # An ephemeris from the JPL provides Sun and Earth positions.

        self.earth = sf_utils.earth.at(self.t)

        # Load star data from SQLite
        db_path = Path(utils.astro_data_dir, "pifinder_objects.db")
        self.star_data = StarData(db_path, mag_limit=7.5)

        # Image size stuff
        self.render_size = resolution
        self.render_center = (
            int(self.render_size[0] / 2),
            int(self.render_size[1] / 2),
        )

        self.set_mag_limit(mag_limit)

        # Create skyfield Star objects and observe
        skyfield_stars = self.star_data.create_skyfield_stars()
        self.star_positions = self.earth.observe(skyfield_stars)
        self.set_fov(fov)

        # Constellation data
        self.const_data = ConstellationData(db_path, self.star_data)

        # Pre-compute constellation star positions
        if len(self.const_data) > 0:
            start_stars = self.star_data.create_skyfield_stars(self.const_data.start_indices)
            end_stars = self.star_data.create_skyfield_stars(self.const_data.end_indices)
            self.const_start_positions = self.earth.observe(start_stars)
            self.const_end_positions = self.earth.observe(end_stars)
        else:
            self.const_start_positions = None
            self.const_end_positions = None

        # Working arrays for projection coordinates (pre-allocate)
        n_stars = len(self.star_data)
        self._star_x = np.zeros(n_stars, dtype=np.float64)
        self._star_y = np.zeros(n_stars, dtype=np.float64)

        n_edges = len(self.const_data)
        self._const_sx = np.zeros(n_edges, dtype=np.float64)
        self._const_sy = np.zeros(n_edges, dtype=np.float64)
        self._const_ex = np.zeros(n_edges, dtype=np.float64)
        self._const_ey = np.zeros(n_edges, dtype=np.float64)

        # Load markers
        marker_path = Path(utils.pifinder_dir, "markers")
        pointer_image_path = Path(marker_path, "pointer.png")
        _pointer_image = Image.open(str(pointer_image_path)).crop(
            [
                int((256 - self.render_size[0]) / 2),
                int((256 - self.render_size[1]) / 2),
                int((256 - self.render_size[0]) / 2) + self.render_size[0],
                int((256 - self.render_size[1]) / 2) + self.render_size[1],
            ]
        )
        self.pointer_image = ImageChops.multiply(
            _pointer_image,
            Image.new("RGB", self.render_size, colors.get(64)),
        )
        # load markers...
        self.markers = {}
        for filename in os.listdir(marker_path):
            if filename.startswith("mrk_"):
                marker_code = filename[4:-4]
                _image = Image.new("RGB", self.render_size)
                _image.paste(
                    Image.open(f"{marker_path}/mrk_{marker_code}.png"),
                    (self.render_center[0] - 11, self.render_center[1] - 11),
                )
                self.markers[marker_code] = ImageChops.multiply(
                    _image, Image.new("RGB", self.render_size, colors.get(256))
                )

    def set_mag_limit(self, mag_limit):
        self.mag_limit = mag_limit

    def set_fov(self, fov):
        self.fov = fov
        angle = np.pi - (self.fov) / 360.0 * np.pi
        limit = np.sin(angle) / (1.0 - np.cos(angle))

        # Used for vis culling in projection space
        self.limit = limit

        self.image_scale = int(self.render_size[0] / limit)
        self.pixel_scale = self.image_scale / 2

        # figure out magnitude limit for fov
        mag_range = (7.5, 5)
        fov_range = (5, 40)
        perc_fov = (fov - fov_range[0]) / (fov_range[1] - fov_range[0])
        if perc_fov > 1:
            perc_fov = 1
        if perc_fov < 0:
            perc_fov = 0

        mag_setting = mag_range[0] - ((mag_range[0] - mag_range[1]) * perc_fov)
        self.set_mag_limit(mag_setting)

    def radec_to_xy(self, ra: float, dec: float) -> tuple[float, float]:
        """
        Converts an RA/DEC to screen space x/y for the current projection
        """
        # Create single star and observe
        marker_star = Star(
            ra_hours=Angle(degrees=ra)._hours,
            dec_degrees=dec,
        )
        marker_pos = self.earth.observe(marker_star)

        # Project
        x, y = self.projection(marker_pos)

        # prep rotate by roll....
        roll_rad = (self.roll) * (np.pi / 180)
        roll_sin = np.sin(roll_rad)
        roll_cos = np.cos(roll_rad)

        # Rotate
        xr = x * roll_cos - y * roll_sin
        yr = y * roll_cos + x * roll_sin

        # Convert to screen space
        x_pos = float(xr) * self.pixel_scale + self.render_center[0]
        y_pos = float(yr) * -1 * self.pixel_scale + self.render_center[1]

        return x_pos, y_pos

    def plot_markers(self, marker_list):
        """
        Returns an image to add to another image
        Marker list should be a list of
        (RA_Hours, DEC_degrees, symbol) tuples
        """
        ret_image = Image.new("RGB", self.render_size)
        idraw = ImageDraw.Draw(ret_image)

        if not marker_list:
            return ret_image

        # Extract marker data as arrays
        ra_hours = np.array([m[0] for m in marker_list], dtype=np.float64)
        dec_degrees = np.array([m[1] for m in marker_list], dtype=np.float64)
        symbols = [m[2] for m in marker_list]

        # Create Star objects and observe
        marker_stars = Star(ra_hours=ra_hours, dec_degrees=dec_degrees)
        marker_positions = self.earth.observe(marker_stars)

        # Project
        x, y = self.projection(marker_positions)

        # prep rotate by roll....
        roll_rad = (self.roll) * (np.pi / 180)
        roll_sin = np.sin(roll_rad)
        roll_cos = np.cos(roll_rad)

        # Rotate
        xr = x * roll_cos - y * roll_sin
        yr = y * roll_cos + x * roll_sin

        # Convert to screen space
        x_pos = xr * self.pixel_scale + self.render_center[0]
        y_pos = yr * -1 * self.pixel_scale + self.render_center[1]

        # Filter for visibility (keep targets regardless)
        for i, symbol in enumerate(symbols):
            xp, yp = x_pos[i], y_pos[i]

            # Skip if not visible and not target
            if symbol != "target":
                if xp < 0 or xp >= self.render_size[0] or yp < 0 or yp >= self.render_size[1]:
                    continue

            if symbol == "target":
                # Draw cross
                idraw.line(
                    [xp, yp - 5, xp, yp + 5],
                    fill=self.colors.get(255),
                )
                idraw.line(
                    [xp - 5, yp, xp + 5, yp],
                    fill=self.colors.get(255),
                )

                # Draw pointer....
                # if not within screen
                if (
                    xp > 0
                    or xp < self.render_size[0]
                    or yp > 0
                    or yp < self.render_size[1]
                ):
                    # calc degrees to target....
                    deg_to_target = (
                        np.rad2deg(
                            np.arctan2(
                                yp - self.render_center[1],
                                xp - self.render_center[0],
                            )
                        )
                        + 180
                    )
                    tmp_pointer = self.pointer_image.copy()
                    tmp_pointer = tmp_pointer.rotate(-deg_to_target)
                    ret_image = ImageChops.add(ret_image, tmp_pointer)

            else:
                _image = ImageChops.offset(
                    self.markers[symbol],
                    int(xp) - (self.render_center[0] - 5),
                    int(yp) - (self.render_center[1] - 5),
                )
                ret_image = ImageChops.add(ret_image, _image)

        return ret_image

    def update_projection(self, ra, dec):
        """
        Updates the shared projection used for various plotting
        routines
        """
        sky_pos = Star(
            ra=Angle(degrees=ra),
            dec_degrees=dec,
        )
        center = self.earth.observe(sky_pos)
        self.projection = build_stereographic_projection(center)

    def plot_starfield(
        self, ra, dec, roll, constellation_brightness=32, shade_frustrum: bool = False
    ):
        """
        Returns an image of the starfield at the
        provided RA/DEC/ROLL with or without
        constellation lines
        """
        self.update_projection(ra, dec)
        self.roll = roll

        # Project all stars
        self._star_x[:], self._star_y[:] = self.projection(self.star_positions)

        # Project constellation endpoints
        if self.const_start_positions is not None:
            self._const_sx[:], self._const_sy[:] = self.projection(self.const_start_positions)
            self._const_ex[:], self._const_ey[:] = self.projection(self.const_end_positions)

        pil_image, visible_count = self.render_starfield_pil(
            constellation_brightness, shade_frustrum
        )
        return pil_image, visible_count

    def render_starfield_pil(
        self, constellation_brightness: int, shade_frustrum: bool = False
    ):
        """
        constellation_brightness: intensity of constellation lines
        shade_frustrum: Shade areas of the chart that are outside of the actual camera FOV

        returns (image, visible_star_count)
        """
        ret_image = Image.new("L", self.render_size)
        idraw = ImageDraw.Draw(ret_image)

        frustrum_perc = 9.5 / self.fov
        if shade_frustrum and frustrum_perc < 0.99:
            idraw.rectangle(
                [
                    0,
                    0,
                    self.render_size[0],
                    self.render_size[1],
                ],
                fill=32,
            )

            # Calc square for in-frustrum
            frustrum_offset = (
                self.render_size[0] - frustrum_perc * self.render_size[0]
            ) / 2
            idraw.rectangle(
                [
                    frustrum_offset,
                    frustrum_offset,
                    self.render_size[0] - frustrum_offset,
                    self.render_size[1] - frustrum_offset,
                ],
                fill=0,
            )

        # prep rotate by roll....
        roll_rad = (self.roll) * (np.pi / 180)
        roll_sin = np.sin(roll_rad)
        roll_cos = np.cos(roll_rad)

        # constellation lines first
        if constellation_brightness and len(self.const_data) > 0:
            # Rotate constellation line endpoints
            sxr = self._const_sx * roll_cos - self._const_sy * roll_sin
            syr = self._const_sy * roll_cos + self._const_sx * roll_sin
            exr = self._const_ex * roll_cos - self._const_ey * roll_sin
            eyr = self._const_ey * roll_cos + self._const_ex * roll_sin

            # Convert to screen space
            sx_pos = sxr * self.pixel_scale + self.render_center[0]
            sy_pos = syr * -1 * self.pixel_scale + self.render_center[1]
            ex_pos = exr * self.pixel_scale + self.render_center[0]
            ey_pos = eyr * -1 * self.pixel_scale + self.render_center[1]

            # Filter for visibility (either endpoint on screen)
            w, h = self.render_size
            visible = (
                ((sx_pos > 0) & (sx_pos < w) & (sy_pos > 0) & (sy_pos < h)) |
                ((ex_pos > 0) & (ex_pos < w) & (ey_pos > 0) & (ey_pos < h))
            )

            # Draw visible constellation lines
            for i in np.where(visible)[0]:
                idraw.line(
                    [sx_pos[i], sy_pos[i], ex_pos[i], ey_pos[i]],
                    fill=constellation_brightness,
                )

        # Filter stars by magnitude
        mag_mask = self.star_data.magnitude < self.mag_limit

        # Filter by visibility in projection space
        vis_mask = (
            (self._star_x > -self.limit) &
            (self._star_x < self.limit) &
            (self._star_y > -self.limit) &
            (self._star_y < self.limit)
        )

        # Combined mask
        visible_mask = mag_mask & vis_mask
        visible_indices = np.where(visible_mask)[0]

        # Get visible star data
        vis_x = self._star_x[visible_indices]
        vis_y = self._star_y[visible_indices]
        vis_mag = self.star_data.magnitude[visible_indices]

        # Rotate
        xr = vis_x * roll_cos - vis_y * roll_sin
        yr = vis_y * roll_cos + vis_x * roll_sin

        # Convert to screen space
        x_pos = xr * self.pixel_scale + self.render_center[0]
        y_pos = yr * -1 * self.pixel_scale + self.render_center[1]

        # Draw stars
        for i in range(len(visible_indices)):
            xp, yp, mag = x_pos[i], y_pos[i], vis_mag[i]

            plot_size = (self.mag_limit - mag) / 3
            fill = 255
            if mag > 4.5:
                fill = 128
            if plot_size < 0.5:
                idraw.point((xp, yp), fill=fill)
            else:
                idraw.circle(
                    (round(xp), round(yp)),
                    radius=plot_size,
                    fill=255,
                    width=0,
                )

        # Count stars in frustrum for return value
        visible_count = len(visible_indices)
        if frustrum_perc < 0.99:
            frustrum_offset = (
                self.render_size[0] - frustrum_perc * self.render_size[0]
            ) / 2
            in_frustrum = (
                (x_pos > frustrum_offset) &
                (x_pos < self.render_size[0] - frustrum_offset) &
                (y_pos > frustrum_offset) &
                (y_pos < self.render_size[1] - frustrum_offset)
            )
            visible_count = np.sum(in_frustrum)

        return ret_image, visible_count
