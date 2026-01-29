import io
import json
import logging
import time
import uuid
import os
import gc
from collections import deque
from datetime import datetime, timezone

import psutil
import pydeepskylog as pds
from PIL import Image, ImageDraw
from PiFinder import utils, calc_utils, config
from PiFinder.db.observations_db import (
    ObservationsDatabase,
)
from PiFinder.equipment import Telescope, Eyepiece
from PiFinder.keyboard_interface import KeyboardInterface
from PiFinder.multiproclogging import MultiprocLogging
from bottle import (
    Bottle,
    run,
    request,
    template,
    response,
    static_file,
    debug,
    redirect,
    CherootServer,
)

sys_utils = utils.get_sys_utils()

logger = logging.getLogger("Server")

# Generate a secret to validate the auth cookie
SESSION_SECRET = str(uuid.uuid4())


def auth_required(func):
    def auth_wrapper(*args, **kwargs):
        # check for and validate cookie
        auth_cookie = request.get_cookie("pf_auth", secret=SESSION_SECRET)
        if auth_cookie:
            return func(*args, **kwargs)

        return template("login", origin_url=request.url)

    return auth_wrapper


class Server:
    def __init__(
        self,
        keyboard_queue,
        ui_queue,
        gps_queue,
        log_queue,
        shared_state,
        is_debug=False,
    ):
        self.version_txt = f"{utils.pifinder_dir}/version.txt"
        self.keyboard_queue = keyboard_queue
        self.ui_queue = ui_queue
        self.gps_queue = gps_queue
        self.log_queue = log_queue
        self.shared_state = shared_state
        self.ki = KeyboardInterface()
        # gps info
        self.lat = None
        self.lon = None
        self.altitude = None
        self.gps_locked = False
        # Track PiFinder start time for uptime
        self.start_time = time.time()
        # Track recent solve timings (timestamp, extract_ms, solve_ms, total_ms)
        # Store up to 300 solves (~5 minutes at 1 solve/sec)
        self.solve_history = deque(maxlen=300)
        self.last_solve_timestamp = None

        # Get GPU memory allocation at startup
        self.gpu_mem_mb = self._get_gpu_memory()

        if is_debug:
            logger.setLevel(logging.DEBUG)

        button_dict = {
            "UP": self.ki.PLUS,
            "DN": self.ki.MINUS,
            "SQUARE": self.ki.SQUARE,
            "A": self.ki.LEFT,
            "B": self.ki.UP,
            "C": self.ki.DOWN,
            "D": self.ki.RIGHT,
            "ALT_PLUS": self.ki.ALT_PLUS,
            "ALT_MINUS": self.ki.ALT_MINUS,
            "ALT_LEFT": self.ki.ALT_LEFT,
            "ALT_UP": self.ki.ALT_UP,
            "ALT_DOWN": self.ki.ALT_DOWN,
            "ALT_RIGHT": self.ki.ALT_RIGHT,
            "ALT_0": self.ki.ALT_0,
            "LNG_LEFT": self.ki.LNG_LEFT,
            "LNG_UP": self.ki.LNG_UP,
            "LNG_DOWN": self.ki.LNG_DOWN,
            "LNG_RIGHT": self.ki.LNG_RIGHT,
            "LNG_SQUARE": self.ki.LNG_SQUARE,
        }

        self.network = sys_utils.Network()

        app = Bottle()
        debug(True)

        @app.route(r"/images/<filename:re:.*\.png>")
        def send_image(filename):
            return static_file(filename, root="views/images", mimetype="image/png")

        @app.route("/js/<filename>")
        def send_js(filename):
            return static_file(filename, root="views/js")

        @app.route("/css/<filename>")
        def send_css(filename):
            return static_file(filename, root="views/css")

        @app.route("/")
        def home():
            logger.debug("/ called")
            # Get version info
            software_version = "Unknown"
            try:
                with open(self.version_txt, "r") as ver_f:
                    software_version = ver_f.read()
            except (FileNotFoundError, IOError) as e:
                logger.warning(f"Could not read version file: {str(e)}")

            # Try to update GPS state
            try:
                self.update_gps()
            except Exception as e:
                logger.error(f"Failed to update GPS in home route: {str(e)}")

            # Use GPS data if available
            lat_text = str(self.lat) if self.gps_locked else ""
            lon_text = str(self.lon) if self.gps_locked else ""
            gps_icon = "gps_fixed" if self.gps_locked else "gps_off"
            gps_text = "Locked" if self.gps_locked else "Not Locked"

            # Default camera values
            ra_text = "0"
            dec_text = "0"
            camera_icon = "broken_image"

            # Try to get solution data
            try:
                if self.shared_state.solve_state() is True:
                    camera_icon = "camera_alt"
                    solution = self.shared_state.solution()
                    if solution:
                        hh, mm, _ = calc_utils.ra_to_hms(solution["RA"])
                        ra_text = f"{hh:02.0f}h{mm:02.0f}m"
                        dec_text = f"{solution['Dec']: .2f}"
            except Exception as e:
                logger.error(f"Failed to get solution data: {str(e)}")

            # Render the template with available data
            return template(
                "index",
                software_version=software_version,
                wifi_mode=self.network.wifi_mode(),
                ip=self.network.local_ip(),
                network_name=self.network.get_connected_ssid(),
                gps_icon=gps_icon,
                gps_text=gps_text,
                lat_text=lat_text,
                lon_text=lon_text,
                camera_icon=camera_icon,
                ra_text=ra_text,
                dec_text=dec_text,
            )

        @app.route("/login", method="post")
        def login():
            password = request.forms.get("password")
            origin_url = request.forms.get("origin_url", "/")
            if sys_utils.verify_password("pifinder", password):
                # set auth cookie, doesn't matter what's in it, just as long
                # as it's there and cryptographically valid
                response.set_cookie("pf_auth", str(uuid.uuid4()), secret=SESSION_SECRET)
                redirect(origin_url)
            else:
                return template(
                    "login", origin_url=origin_url, error_message="Invalid Password"
                )

        @app.route("/remote")
        @auth_required
        def remote():
            return template(
                "remote",
            )

        @app.route("/advanced")
        @auth_required
        def advanced():
            return template(
                "advanced",
            )

        @app.route("/network")
        @auth_required
        def network_page():
            show_new_form = request.query.add_new or 0

            return template(
                "network",
                net=self.network,
                show_new_form=show_new_form,
            )

        @app.route("/gps")
        @auth_required
        def gps_page():
            self.update_gps()
            show_new_form = request.query.add_new or 0
            logger.debug(
                "/gps: %f, %f, %f ",
                self.lat or 0.0,
                self.lon or 0.0,
                self.altitude or 0.0,
            )

            return template(
                "gps",
                show_new_form=show_new_form,
                lat=self.lat,
                lon=self.lon,
                altitude=self.altitude,
            )

        @app.route("/gps/update", method="post")
        @auth_required
        def gps_update():
            lat = request.forms.get("latitudeDecimal")
            lon = request.forms.get("longitudeDecimal")
            altitude = request.forms.get("altitude")
            date_req = request.forms.get("date")
            time_req = request.forms.get("time")
            gps_lock(float(lat), float(lon), float(altitude))
            if time_req and date_req:
                datetime_str = f"{date_req} {time_req}"
                datetime_obj = datetime.strptime(datetime_str, "%Y-%m-%d %H:%M:%S")
                datetime_utc = datetime_obj.replace(tzinfo=timezone.utc)
                time_lock(datetime_utc)
            logger.debug(
                "GPS update: %s, %s, %s, %s, %s", lat, lon, altitude, date_req, time_req
            )
            time.sleep(1)  # give the gps thread a chance to update
            return home()

        @app.route("/locations")
        @auth_required
        def locations_page():
            show_new_form = request.query.add_new or 0
            cfg = config.Config()
            cfg.load_config()  # Ensure config is loaded
            return template(
                "locations",
                locations=cfg.locations.locations,
                show_new_form=show_new_form,
            )

        @app.route("/locations/add", method="post")
        @auth_required
        def location_add():
            try:
                name = request.forms.get("name").strip()
                lat = float(request.forms.get("latitude"))
                lon = float(request.forms.get("longitude"))
                altitude = float(request.forms.get("altitude"))
                error_in_m = float(request.forms.get("error_in_m", "0"))
                source = request.forms.get("source", "Manual Entry")

                # Server-side validation
                if not name:
                    raise ValueError("Location name is required")
                if not (-90 <= lat <= 90):
                    raise ValueError("Latitude must be between -90 and 90")
                if not (-180 <= lon <= 180):
                    raise ValueError("Longitude must be between -180 and 180")
                if not (-1000 <= altitude <= 10000):
                    raise ValueError("Altitude must be between -1000 and 10000 meters")
                if not (0 <= error_in_m <= 10000):
                    raise ValueError("Error must be between 0 and 10000 meters")

                from PiFinder.locations import Location

                new_location = Location(
                    name=name,
                    latitude=lat,
                    longitude=lon,
                    height=altitude,
                    error_in_m=error_in_m,
                    source=source,
                )

                cfg = config.Config()
                cfg.load_config()
                cfg.locations.add_location(new_location)
                cfg.save_locations()

                self.ui_queue.put("reload_config")
                redirect("/locations")

            except ValueError as e:
                return template(
                    "locations",
                    locations=config.Config().locations.locations,
                    show_new_form=1,
                    error_message=str(e),
                )

        @app.route("/locations/rename/<location_id:int>", method="post")
        @auth_required
        def location_rename(location_id):
            try:
                cfg = config.Config()
                cfg.load_config()

                if not (0 <= location_id < len(cfg.locations.locations)):
                    raise ValueError("Invalid location ID")

                name = request.forms.get("name").strip()
                lat = float(request.forms.get("latitude"))
                lon = float(request.forms.get("longitude"))
                altitude = float(request.forms.get("altitude"))
                error_in_m = float(request.forms.get("error_in_m", "0"))
                source = request.forms.get("source", "Manual Entry")

                # Server-side validation
                if not name:
                    raise ValueError("Location name is required")
                if not (-90 <= lat <= 90):
                    raise ValueError("Latitude must be between -90 and 90")
                if not (-180 <= lon <= 180):
                    raise ValueError("Longitude must be between -180 and 180")
                if not (-1000 <= altitude <= 10000):
                    raise ValueError("Altitude must be between -1000 and 10000 meters")
                if not (0 <= error_in_m <= 10000):
                    raise ValueError("Error must be between 0 and 10000 meters")

                location = cfg.locations.locations[location_id]
                location.name = name
                location.latitude = lat
                location.longitude = lon
                location.height = altitude
                location.error_in_m = error_in_m
                location.source = source

                cfg.save_locations()
                self.ui_queue.put("reload_config")
                redirect("/locations")

            except ValueError as e:
                return template(
                    "locations",
                    locations=config.Config().locations.locations,
                    show_new_form=0,
                    error_message=str(e),
                )

        @app.route("/locations/delete/<location_id:int>")
        @auth_required
        def location_delete(location_id):
            cfg = config.Config()
            cfg.load_config()
            if 0 <= location_id < len(cfg.locations.locations):
                location = cfg.locations.locations[location_id]
                cfg.locations.remove_location(location)
                cfg.save_locations()
                # Notify main process to reload config
                self.ui_queue.put("reload_config")
            redirect("/locations")

        @app.route("/locations/set_default/<location_id:int>")
        @auth_required
        def location_set_default(location_id):
            cfg = config.Config()
            cfg.load_config()
            if 0 <= location_id < len(cfg.locations.locations):
                location = cfg.locations.locations[location_id]
                cfg.locations.set_default(location)
                cfg.save_locations()
                # Notify main process to reload config
                self.ui_queue.put("reload_config")
            redirect("/locations")

        @app.route("/locations/load/<location_id:int>")
        @auth_required
        def location_load(location_id):
            cfg = config.Config()
            cfg.load_config()  # Ensure config is loaded
            if 0 <= location_id < len(cfg.locations.locations):
                location = cfg.locations.locations[location_id]
                gps_lock(location.latitude, location.longitude, location.height)
            redirect("/locations")

        @app.route("/network/add", method="post")
        @auth_required
        def network_add():
            ssid = request.forms.get("ssid")
            psk = request.forms.get("psk")
            if len(psk) < 8:
                key_mgmt = "NONE"
            else:
                key_mgmt = "WPA-PSK"

            self.network.add_wifi_network(ssid, key_mgmt, psk)
            return network_page()

        @app.route("/network/delete/<network_id:int>")
        @auth_required
        def network_delete(network_id):
            self.network.delete_wifi_network(network_id)
            return network_page()

        @app.route("/network/update", method="post")
        @auth_required
        def network_update():
            wifi_mode = request.forms.get("wifi_mode")
            ap_name = request.forms.get("ap_name")
            host_name = request.forms.get("host_name")

            self.network.set_wifi_mode(wifi_mode)
            self.network.set_ap_name(ap_name)
            self.network.set_host_name(host_name)
            return template("restart")

        @app.route("/tools/pwchange", method="post")
        @auth_required
        def password_change():
            current_password = request.forms.get("current_password")
            new_passworda = request.forms.get("new_passworda")
            new_passwordb = request.forms.get("new_passwordb")

            if new_passworda == "" or current_password == "" or new_passwordb == "":
                return template(
                    "tools", error_message="You must fill in all password fields"
                )

            if new_passworda == new_passwordb:
                if sys_utils.change_password(
                    "pifinder", current_password, new_passworda
                ):
                    return template("tools", status_message="Password Changed")
                else:
                    return template("tools", error_message="Incorrect current password")
            else:
                return template("tools", error_message="New passwords do not match")

        @app.route("/system/restart")
        @auth_required
        def system_restart():
            """
            Restarts the RPI system
            """

            sys_utils.restart_system()
            return "restarting"

        @app.route("/system/restart_pifinder")
        @auth_required
        def pifinder_restart():
            """
            Restarts just the PiFinder software
            """
            sys_utils.restart_pifinder()
            return "restarting"

        @app.route("/equipment")
        @auth_required
        def equipment():
            return template("equipment", equipment=config.Config().equipment)

        @app.route("/equipment/set_active_instrument/<instrument_id:int>")
        @auth_required
        def set_active_instrument(instrument_id: int):
            cfg = config.Config()
            cfg.equipment.set_active_telescope(cfg.equipment.telescopes[instrument_id])
            cfg.save_equipment()
            self.ui_queue.put("reload_config")
            return template(
                "equipment",
                equipment=cfg.equipment,
                success_message=cfg.equipment.active_telescope.make
                + " "
                + cfg.equipment.active_telescope.name
                + " set as active instrument.",
            )

        @app.route("/equipment/set_active_eyepiece/<eyepiece_id:int>")
        @auth_required
        def set_active_eyepiece(eyepiece_id: int):
            cfg = config.Config()
            cfg.equipment.set_active_eyepiece(cfg.equipment.eyepieces[eyepiece_id])
            cfg.save_equipment()
            self.ui_queue.put("reload_config")
            return template(
                "equipment",
                equipment=cfg.equipment,
                success_message=cfg.equipment.active_eyepiece.make
                + " "
                + cfg.equipment.active_eyepiece.name
                + " set as active eyepiece.",
            )

        @app.route("/equipment/import_from_deepskylog", method="post")
        @auth_required
        def equipment_import():
            username = request.forms.get("dsl_name")
            cfg = config.Config()
            if username:
                instruments = pds.dsl_instruments(username)
                for instrument in instruments:
                    if instrument["type"] == 0:
                        # Skip the naked eye
                        continue

                    make = instrument["instrument_make"]["name"]

                    obstruction_perc = instrument["obstruction_perc"]
                    if obstruction_perc is None:
                        obstruction_perc = 0
                    else:
                        obstruction_perc = float(obstruction_perc)

                    # Convert the html special characters (ampersand, quote, ...) in instrument["name"]
                    # to the corresponding character
                    instrument["name"] = instrument["name"].replace("&amp;", "&")
                    instrument["name"] = instrument["name"].replace("&quot;", '"')
                    instrument["name"] = instrument["name"].replace("&apos;", "'")
                    instrument["name"] = instrument["name"].replace("&lt;", "<")
                    instrument["name"] = instrument["name"].replace("&gt;", ">")

                    new_instrument = Telescope(
                        make=make,
                        name=instrument["name"],
                        aperture_mm=int(instrument["diameter"]),
                        focal_length_mm=int(instrument["diameter"] * instrument["fd"]),
                        obstruction_perc=obstruction_perc,
                        mount_type=instrument["mount_type"]["name"].lower(),
                        flip_image=bool(instrument["flip_image"]),
                        flop_image=bool(instrument["flop_image"]),
                        reverse_arrow_a=False,
                        reverse_arrow_b=False,
                    )
                    try:
                        cfg.equipment.telescopes.index(new_instrument)
                    except ValueError:
                        cfg.equipment.telescopes.append(new_instrument)

                # Add the eyepieces from deepskylog
                eyepieces = pds.dsl_eyepieces(username)
                for eyepiece in eyepieces:
                    # Convert the html special characters (ampersand, quote, ...) in eyepiece["name"]
                    # to the corresponding character
                    eyepiece["name"] = eyepiece["name"].replace("&amp;", "&")
                    eyepiece["name"] = eyepiece["name"].replace("&quot;", '"')
                    eyepiece["name"] = eyepiece["name"].replace("&apos;", "'")
                    eyepiece["name"] = eyepiece["name"].replace("&lt;", "<")
                    eyepiece["name"] = eyepiece["name"].replace("&gt;", ">")

                    make = eyepiece["eyepiece_make"]["name"]

                    new_eyepiece = Eyepiece(
                        make=make,
                        name=eyepiece["name"],
                        focal_length_mm=float(eyepiece["focalLength"]),
                        afov=int(eyepiece["apparentFOV"]),
                        field_stop=float(eyepiece["field_stop_mm"]),
                    )
                    try:
                        cfg.equipment.eyepieces.index(new_eyepiece)
                    except ValueError:
                        cfg.equipment.eyepieces.append(new_eyepiece)

                cfg.save_equipment()
                self.ui_queue.put("reload_config")
            return template(
                "equipment",
                equipment=config.Config().equipment,
                success_message="Equipment Imported, restart your PiFinder to use this new data",
            )

        @app.route("/equipment/edit_eyepiece/<eyepiece_id:int>")
        @auth_required
        def edit_eyepiece(eyepiece_id: int):
            if eyepiece_id >= 0:
                eyepiece = config.Config().equipment.eyepieces[eyepiece_id]
            else:
                eyepiece = Eyepiece(
                    make="", name="", focal_length_mm=0, afov=0, field_stop=0
                )

            return template("edit_eyepiece", eyepiece=eyepiece, eyepiece_id=eyepiece_id)

        @app.route("/equipment/add_eyepiece/<eyepiece_id:int>", method="post")
        @auth_required
        def equipment_add_eyepiece(eyepiece_id: int):
            cfg = config.Config()

            try:
                eyepiece = Eyepiece(
                    make=request.forms.get("make"),
                    name=request.forms.get("name"),
                    focal_length_mm=float(request.forms.get("focal_length_mm")),
                    afov=int(request.forms.get("afov")),
                    field_stop=float(request.forms.get("field_stop")),
                )

                if eyepiece_id >= 0:
                    cfg.equipment.eyepieces[eyepiece_id] = eyepiece
                else:
                    try:
                        index = cfg.equipment.telescopes.index(eyepiece)
                        cfg.equipment.eyepieces[index] = eyepiece
                    except ValueError:
                        cfg.equipment.eyepieces.append(eyepiece)

                cfg.save_equipment()
                self.ui_queue.put("reload_config")
            except Exception as e:
                logger.error(f"Error adding eyepiece: {e}")

            return template(
                "equipment",
                equipment=config.Config().equipment,
                success_message="Eyepiece added, restart your PiFinder to use",
            )

        @app.route("/equipment/delete_eyepiece/<eyepiece_id:int>")
        @auth_required
        def equipment_delete_eyepiece(eyepiece_id: int):
            cfg = config.Config()
            cfg.equipment.eyepieces.pop(eyepiece_id)
            cfg.save_equipment()
            self.ui_queue.put("reload_config")
            return template(
                "equipment",
                equipment=config.Config().equipment,
                success_message="Eyepiece Deleted, restart your PiFinder to remove from menu",
            )

        @app.route("/equipment/edit_instrument/<instrument_id:int>")
        @auth_required
        def edit_instrument(instrument_id: int):
            if instrument_id >= 0:
                telescope = config.Config().equipment.telescopes[instrument_id]
            else:
                telescope = Telescope(
                    make="",
                    name="",
                    aperture_mm=0,
                    focal_length_mm=0,
                    obstruction_perc=0,
                    mount_type="",
                    flip_image=False,
                    flop_image=False,
                    reverse_arrow_a=False,
                    reverse_arrow_b=False,
                )

            return template(
                "edit_instrument", telescope=telescope, instrument_id=instrument_id
            )

        @app.route("/equipment/add_instrument/<instrument_id:int>", method="post")
        @auth_required
        def equipment_add_instrument(instrument_id: int):
            cfg = config.Config()

            try:
                instrument = Telescope(
                    make=request.forms.get("make"),
                    name=request.forms.get("name"),
                    aperture_mm=int(request.forms.get("aperture")),
                    focal_length_mm=int(request.forms.get("focal_length_mm")),
                    obstruction_perc=float(request.forms.get("obstruction_perc")),
                    mount_type=request.forms.get("mount_type"),
                    flip_image=bool(request.forms.get("flip")),
                    flop_image=bool(request.forms.get("flop")),
                    reverse_arrow_a=bool(request.forms.get("reverse_arrow_a")),
                    reverse_arrow_b=bool(request.forms.get("reverse_arrow_b")),
                )
                if instrument_id >= 0:
                    cfg.equipment.telescopes[instrument_id] = instrument
                else:
                    try:
                        index = cfg.equipment.telescopes.index(instrument)
                        cfg.equipment.telescopes[index] = instrument
                    except ValueError:
                        cfg.equipment.telescopes.append(instrument)

                cfg.save_equipment()
                self.ui_queue.put("reload_config")
            except Exception as e:
                logger.error(f"Error adding instrument: {e}")
            return template(
                "equipment",
                equipment=config.Config().equipment,
                success_message="Instrument Added, restart your PiFinder to use",
            )

        @app.route("/equipment/delete_instrument/<instrument_id:int>")
        @auth_required
        def equipment_delete_instrument(instrument_id: int):
            cfg = config.Config()
            cfg.equipment.telescopes.pop(instrument_id)
            cfg.save_equipment()
            self.ui_queue.put("reload_config")
            return template(
                "equipment",
                equipment=config.Config().equipment,
                success_message="Instrument Deleted, restart your PiFinder to remove from menu",
            )

        @app.route("/observations")
        @auth_required
        def obs_sessions():
            obs_db = ObservationsDatabase()
            if request.query.get("download", 0) == "1":
                # Download all as TSV
                observations = obs_db.observations_as_tsv()

                response.set_header(
                    "Content-Disposition", "attachment; filename=observations.tsv"
                )
                response.set_header("Content-Type", "text/tsv")
                return observations

            # regular html page of sessions
            sessions = obs_db.get_sessions()
            metadata = {
                "sess_count": len(sessions),
                "object_count": sum(x["observations"] for x in sessions),
                "total_duration": sum(x["duration"] for x in sessions),
            }
            return template("obs_sessions", sessions=sessions, metadata=metadata)

        @app.route("/observations/<session_id>")
        @auth_required
        def obs_session(session_id):
            obs_db = ObservationsDatabase()
            if request.query.get("download", 0) == "1":
                # Download all as TSV
                observations = obs_db.observations_as_tsv(session_id)

                response.set_header(
                    "Content-Disposition",
                    f"attachment; filename=observations_{session_id}.tsv",
                )
                response.set_header("Content-Type", "text/tsv")
                return observations

            session = obs_db.get_sessions(session_id)[0]
            objects = obs_db.get_logs_by_session(session_id)
            ret_objects = []
            for obj in objects:
                obj_ = dict(obj)
                obj_notes = json.loads(obj_["notes"])
                obj_["notes"] = "<br>".join(
                    [f"{key}: {value}" for key, value in obj_notes.items()]
                )
                ret_objects.append(obj_)
            return template("obs_session_log", session=session, objects=ret_objects)

        @app.route("/tools")
        @auth_required
        def tools():
            return template("tools")

        @app.route("/debug")
        @auth_required
        def debug_page():
            return template("debug")

        @app.route("/logs")
        @auth_required
        def logs_page():
            # Get current log level
            root_logger = logging.getLogger()
            current_level = logging.getLevelName(root_logger.getEffectiveLevel())
            return template("logs", current_level=current_level)

        @app.route("/logs/stream")
        @auth_required
        def stream_logs():
            try:
                position = int(request.query.get("position", 0))
                log_file = "/home/pifinder/PiFinder_data/pifinder.log"

                try:
                    file_size = os.path.getsize(log_file)
                    # If position is beyond file size or 0, start from beginning
                    if position >= file_size or position == 0:
                        position = 0

                    with open(log_file, "r") as f:
                        if position > 0:
                            f.seek(position)
                        new_lines = f.readlines()
                        new_position = f.tell()

                    # If we're at the start of the file, get all lines
                    # Otherwise, only return new lines if there are any
                    if position == 0 or new_lines:
                        return {"logs": new_lines, "position": new_position}
                    else:
                        return {"logs": [], "position": position}
                except FileNotFoundError:
                    logger.error(f"Log file not found: {log_file}")
                    return {"logs": [], "position": 0}

            except Exception as e:
                logger.error(f"Error streaming logs: {e}")
                return {"logs": [], "position": position}

        @app.route("/logs/current_level")
        @auth_required
        def get_current_log_level():
            root_logger = logging.getLogger()
            current_level = logging.getLevelName(root_logger.getEffectiveLevel())
            return {"level": current_level}

        @app.route("/logs/components")
        @auth_required
        def get_component_levels():
            try:
                import json5

                with open("pifinder_logconf.json", "r") as f:
                    config = json5.load(f)
                # Get all loggers from the config
                loggers = config.get("loggers", {})
                # Get current runtime levels for each logger
                current_levels = {}
                # Get all loggers from the config file
                for logger_name in loggers.keys():
                    logger = logging.getLogger(logger_name)
                    current_levels[logger_name] = {
                        "config_level": loggers.get(logger_name, {}).get(
                            "level", "INFO"
                        ),
                        "current_level": logging.getLevelName(
                            logger.getEffectiveLevel()
                        ),
                    }
                return {"components": current_levels}
            except Exception as e:
                logging.error(f"Error reading log configuration: {e}")
                return {"status": "error", "message": str(e)}

        @app.route("/logs/download")
        @auth_required
        def download_logs():
            import zipfile
            import os
            from datetime import datetime

            try:
                # Create a temporary zip file
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                zip_path = f"/home/pifinder/PiFinder_data/logs_{timestamp}.zip"

                with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
                    # Add all log files
                    log_dir = "/home/pifinder/PiFinder_data"
                    for filename in os.listdir(log_dir):
                        if filename.startswith("pifinder") and filename.endswith(
                            ".log"
                        ):
                            file_path = os.path.join(log_dir, filename)
                            zipf.write(file_path, filename)

                # Send the zip file
                response.set_header("Content-Type", "application/zip")
                response.set_header(
                    "Content-Disposition", f"attachment; filename=logs_{timestamp}.zip"
                )

                with open(zip_path, "rb") as f:
                    content = f.read()

                # Clean up the temporary zip file
                os.remove(zip_path)

                return content

            except Exception as e:
                logger.error(f"Error creating log zip: {e}")
                return template("logs", error_message="Error creating log archive")

        @app.route("/tools/backup")
        @auth_required
        def tools_backup():
            _backup_file = sys_utils.backup_userdata()

            # Assumes the standard backup location
            return static_file("PiFinder_backup.zip", "/home/pifinder/PiFinder_data")

        @app.route("/tools/restore", method="post")
        @auth_required
        def tools_restore():
            sys_utils.remove_backup()
            backup_file = request.files.get("backup_file")
            backup_file.filename = "PiFinder_backup.zip"
            backup_file.save("/home/pifinder/PiFinder_data")

            sys_utils.restore_userdata(
                "/home/pifinder/PiFinder_data/PiFinder_backup.zip"
            )

            return template("restart_pifinder")

        @app.route("/key_callback", method="POST")
        @auth_required
        def key_callback():
            button = request.json.get("button")
            if button in button_dict:
                self.key_callback(button_dict[button])
            else:
                self.key_callback(int(button))
            return {"message": "success"}

        @app.route("/image")
        def serve_pil_image():
            empty_img = Image.new(
                "RGB", (60, 30), color=(73, 109, 137)
            )  # create an image using PIL
            img = None
            try:
                img = self.shared_state.screen()
            except (BrokenPipeError, EOFError):
                pass
            response.content_type = "image/png"  # adjust for your image format

            if img is None:
                img = empty_img
            img_byte_arr = io.BytesIO()
            img.save(img_byte_arr, format="PNG")  # adjust for your image format
            img_byte_arr = img_byte_arr.getvalue()

            return img_byte_arr

        @app.route("/api/solved_frame/data")
        def api_solved_frame_data():
            """Return JSON data about the last solved frame"""
            response.content_type = "application/json"

            try:
                frame_data = self.shared_state.last_solved_frame()
                if frame_data is None:
                    return json.dumps({"error": "No solved frame available"})

                # Get current solution from integrator for updated Alt/Az
                current_solution = self.shared_state.solution()

                # Merge current Alt/Az into frame solution data
                solution_data = frame_data.get("solution", {}).copy()
                if current_solution:
                    # Update with integrator-calculated Alt/Az if available
                    camera_center = current_solution.get("camera_center", {})
                    if camera_center.get("Alt") is not None:
                        solution_data["Alt"] = camera_center.get("Alt")
                    if camera_center.get("Az") is not None:
                        solution_data["Az"] = camera_center.get("Az")

                # Convert to JSON-serializable format
                result = {
                    "solution": solution_data,
                    "camera": frame_data.get("camera"),
                    "timing": frame_data.get("timing"),
                    "num_centroids": frame_data.get("num_centroids"),
                    "centroids": frame_data.get("centroids"),
                    "metadata": frame_data.get("metadata"),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
                return json.dumps(result, indent=2)
            except Exception as e:
                logger.error(f"Error retrieving solved frame data: {e}")
                return json.dumps({"error": str(e)})

        @app.route("/api/solved_frame/image")
        def api_solved_frame_image():
            """Return the last solved frame as PNG with centroids overlaid"""
            response.content_type = "image/png"

            try:
                frame_data = self.shared_state.last_solved_frame()
                if frame_data is None:
                    # Return empty image
                    empty_img = Image.new("RGB", (512, 512), color=(0, 0, 0))
                    img_byte_arr = io.BytesIO()
                    empty_img.save(img_byte_arr, format="PNG")
                    return img_byte_arr.getvalue()

                # Get the original image
                img = frame_data.get("image")
                if img is None:
                    empty_img = Image.new("RGB", (512, 512), color=(0, 0, 0))
                    img_byte_arr = io.BytesIO()
                    empty_img.save(img_byte_arr, format="PNG")
                    return img_byte_arr.getvalue()

                # Convert to RGB if grayscale
                if img.mode != "RGB":
                    img = img.convert("RGB")

                # Draw centroids
                draw = ImageDraw.Draw(img)
                centroids = frame_data.get("centroids", [])
                for centroid in centroids:
                    x, y = int(centroid[0]), int(centroid[1])
                    # Draw a small circle around each centroid
                    r = 5
                    draw.ellipse([x-r, y-r, x+r, y+r], outline="red", width=2)

                # Save to bytes
                img_byte_arr = io.BytesIO()
                img.save(img_byte_arr, format="PNG")
                return img_byte_arr.getvalue()

            except Exception as e:
                logger.error(f"Error generating solved frame image: {e}")
                empty_img = Image.new("RGB", (512, 512), color=(64, 0, 0))
                img_byte_arr = io.BytesIO()
                empty_img.save(img_byte_arr, format="PNG")
                return img_byte_arr.getvalue()

        def _identify_process_role(proc):
            """Identify the role of a PiFinder process based on parent/child relationships."""
            try:
                # Main process is the parent of all others
                # It's the one whose parent is not another PiFinder process
                parent = proc.parent()
                if parent:
                    parent_cmdline = ' '.join(parent.cmdline() if parent.cmdline() else [])
                    if 'PiFinder' not in parent_cmdline:
                        return 'Main/UI'

                # Try to identify by open files or connections
                try:
                    connections = proc.connections()
                    for conn in connections:
                        if conn.status == 'LISTEN' and conn.laddr.port == 8080:
                            return 'Webserver'
                        if hasattr(conn, 'laddr') and hasattr(conn.laddr, 'port') and conn.laddr.port == 4030:
                            return 'SkySafari'
                except (psutil.AccessDenied, AttributeError):
                    pass

                # Default to generic worker
                return 'Worker'
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                return 'Unknown'

        @app.route("/api/metrics")
        def api_metrics():
            """Return JSON performance metrics"""
            response.content_type = "application/json"

            try:
                # Update GPS state
                self.update_gps()

                # Calculate uptime
                uptime_seconds = time.time() - self.start_time

                # Get all PiFinder processes
                pifinder_procs = []
                total_rss = 0
                for proc in psutil.process_iter(['pid', 'name', 'cmdline', 'cpu_percent', 'create_time']):
                    cmdline_str = str(proc.info.get('cmdline', []))
                    if 'PiFinder' in cmdline_str:
                        try:
                            mem = proc.memory_info()
                            total_rss += mem.rss
                            role = _identify_process_role(proc)
                            pifinder_procs.append({
                                'pid': proc.info['pid'],
                                'role': role,
                                'rss_mb': round(mem.rss / 1024 / 1024, 1),
                                'cpu_percent': proc.info['cpu_percent']
                            })
                        except (psutil.NoSuchProcess, psutil.AccessDenied):
                            pass

                # System memory
                sys_mem = psutil.virtual_memory()
                sys_swap = psutil.swap_memory()

                # Garbage collection stats
                gc_counts = gc.get_count()
                gc_stats = {
                    'generation0': gc_counts[0],
                    'generation1': gc_counts[1],
                    'generation2': gc_counts[2],
                }

                # Solve metrics from shared state
                solution = self.shared_state.solution()
                last_img = self.shared_state.last_image_metadata()
                last_solved = self.shared_state.last_solved_frame()

                solver_metrics = {
                    'last_solve_time': solution.get('solve_time') if solution else None,
                    'imu_delta': last_img.get('imu_delta') if last_img else None,
                }

                if last_solved:
                    timing = last_solved.get('timing', {})

                    # Create a unique key for this solve based on timing signature
                    # This handles cases where solve_time might not update
                    solve_signature = (
                        timing.get('t_extract_ms'),
                        timing.get('t_solve_ms'),
                        timing.get('total_ms'),
                    )

                    # Track this solve in history if it's new (different from last)
                    if solve_signature != self.last_solve_timestamp:
                        solve_time = solution.get('solve_time') if solution else time.time()
                        self.solve_history.append({
                            'timestamp': solve_time,
                            'extract_ms': timing.get('t_extract_ms'),
                            'solve_ms': timing.get('t_solve_ms'),
                            'total_ms': timing.get('total_ms'),
                        })
                        self.last_solve_timestamp = solve_signature

                    # Calculate windowed statistics
                    now = time.time()
                    windows = {
                        'last_10': {'count': 10, 'solves': []},
                        'last_1min': {'seconds': 60, 'solves': []},
                        'last_5min': {'seconds': 300, 'solves': []},
                    }

                    # Populate windows
                    for solve in reversed(self.solve_history):
                        # Last 10 solves
                        if len(windows['last_10']['solves']) < 10:
                            windows['last_10']['solves'].append(solve)

                        # Time-based windows
                        age = now - solve['timestamp']
                        if age <= 60:
                            windows['last_1min']['solves'].append(solve)
                        if age <= 300:
                            windows['last_5min']['solves'].append(solve)

                    # Calculate stats for each window
                    timing_windows = {}
                    for window_name, window_data in windows.items():
                        solves = window_data['solves']
                        if solves:
                            timing_windows[window_name] = {
                                'count': len(solves),
                                'extract_mean_ms': round(sum(s['extract_ms'] for s in solves if s['extract_ms']) / len([s for s in solves if s['extract_ms']]), 2) if any(s['extract_ms'] for s in solves) else None,
                                'extract_max_ms': round(max(s['extract_ms'] for s in solves if s['extract_ms']), 2) if any(s['extract_ms'] for s in solves) else None,
                                'solve_mean_ms': round(sum(s['solve_ms'] for s in solves if s['solve_ms']) / len([s for s in solves if s['solve_ms']]), 2) if any(s['solve_ms'] for s in solves) else None,
                                'solve_max_ms': round(max(s['solve_ms'] for s in solves if s['solve_ms']), 2) if any(s['solve_ms'] for s in solves) else None,
                                'total_mean_ms': round(sum(s['total_ms'] for s in solves if s['total_ms']) / len([s for s in solves if s['total_ms']]), 2) if any(s['total_ms'] for s in solves) else None,
                                'total_max_ms': round(max(s['total_ms'] for s in solves if s['total_ms']), 2) if any(s['total_ms'] for s in solves) else None,
                                'individual_solves': solves  # Include individual solve data for charting
                            }
                        else:
                            timing_windows[window_name] = {'count': 0}

                    solver_metrics.update({
                        'last_extract_ms': timing.get('t_extract_ms'),
                        'last_solve_ms': timing.get('t_solve_ms'),
                        'last_total_ms': timing.get('total_ms'),
                        'last_num_centroids': last_solved.get('num_centroids'),
                        'timing_windows': timing_windows,
                    })

                # GPS status
                gps_status = {
                    'locked': self.gps_locked,
                    'sats': self.shared_state.sats(),
                }

                metrics = {
                    'timestamp': datetime.now(timezone.utc).isoformat(),
                    'uptime_seconds': round(uptime_seconds, 1),
                    'uptime_formatted': f"{int(uptime_seconds // 3600)}h {int((uptime_seconds % 3600) // 60)}m {int(uptime_seconds % 60)}s",
                    'memory': {
                        'pifinder_total_mb': round(total_rss / 1024 / 1024, 1),
                        'system_total_mb': round(sys_mem.total / 1024 / 1024, 1),
                        'system_percent': sys_mem.percent,
                        'available_mb': round(sys_mem.available / 1024 / 1024, 1),
                        'gpu_mem_mb': self.gpu_mem_mb,
                        'swap_used_mb': round(sys_swap.used / 1024 / 1024, 1),
                        'swap_percent': sys_swap.percent,
                        'processes': sorted(pifinder_procs, key=lambda x: x['rss_mb'], reverse=True)
                    },
                    'gc': gc_stats,
                    'solver': solver_metrics,
                    'gps': gps_status,
                    'cpu_load': list(os.getloadavg())
                }

                return json.dumps(metrics, indent=2)

            except Exception as e:
                logger.error(f"Error generating metrics: {e}")
                return json.dumps({"error": str(e)})

        @auth_required
        def gps_lock(lat: float = 50, lon: float = 3, altitude: float = 10):
            msg = (
                "fix",
                {
                    "lat": lat,
                    "lon": lon,
                    "altitude": altitude,
                    "error_in_m": 0,
                    "source": "WEB",
                    "lock": True,
                },
            )
            self.gps_queue.put(msg)
            logger.debug("Putting location msg on gps_queue: {msg}")

        @auth_required
        def time_lock(time=datetime.now()):
            msg = ("time", time)
            self.gps_queue.put(msg)
            logger.debug("Putting time msg on gps_queue: {msg}")

        # If the PiFinder software is running as a service
        # it can grab port 80.  If not, it needs to use 8080
        try:
            run(
                app,
                host="0.0.0.0",
                port=80,
                quiet=True,
                debug=True,
                server=CherootServer,
            )
        except (PermissionError, OSError):
            logger.info("Web Interface on port 8080")
            run(
                app,
                host="0.0.0.0",
                port=8080,
                quiet=True,
                debug=True,
                server=CherootServer,
            )

    def key_callback(self, key):
        self.keyboard_queue.put(key)

    def _get_gpu_memory(self):
        """Get GPU memory allocation in MB using vcgencmd"""
        try:
            import subprocess
            result = subprocess.run(
                ["vcgencmd", "get_mem", "gpu"],
                capture_output=True,
                text=True,
                timeout=2
            )
            if result.returncode == 0:
                # Output format: "gpu=32M\n"
                output = result.stdout.strip()
                if "=" in output:
                    mem_str = output.split("=")[1].rstrip("M")
                    return int(mem_str)
        except Exception as e:
            logger.warning(f"Could not determine GPU memory: {e}")

        # Default fallback - Pi 4 default is 76MB, Pi Zero 2W should be 32MB
        # Return None to indicate unknown
        return None

    def update_gps(self):
        """Update GPS information"""
        location = self.shared_state.location()

        if location.lock is True:
            self.gps_locked = True
            self.lat = location.lat
            self.lon = location.lon
            self.altitude = location.altitude
        else:
            self.gps_locked = False
            self.lat = None
            self.lon = None
            self.altitude = None


def run_server(
    keyboard_queue, ui_queue, gps_queue, shared_state, log_queue, verbose=False
):
    MultiprocLogging.configurer(log_queue)
    Server(keyboard_queue, ui_queue, gps_queue, log_queue, shared_state, verbose)
