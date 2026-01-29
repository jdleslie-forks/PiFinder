# PiFinder Development Setup Notes

This file contains local development setup notes and common issues.

## First-Time Setup

After cloning the repository and installing dependencies, you **must** build the star database before PiFinder will run:

```bash
cd python/
source .venv/bin/activate
python scripts/build_star_database.py
```

### Why is this required?

The star database build step pre-processes the Hipparcos star catalog into the `stars` and `constellation_edges` tables in `astro_data/pifinder_objects.db`. This step:

- Downloads the Hipparcos catalog (~9MB) if not cached
- Filters to stars brighter than magnitude 7.5 (~25,000 stars)
- Processes constellation line data
- Builds optimized SQLite tables with indexes
- Requires pandas (build-time dependency only)
- Takes 1-2 minutes on Pi Zero 2W, faster on desktop

**Without this step**, PiFinder will crash during menu initialization with:
```
sqlite3.OperationalError: no such table: stars
```

### When to rebuild

You only need to run this once per checkout. Re-run if:
- You delete `astro_data/pifinder_objects.db`
- The database schema changes in upstream updates
- You see "no such table: stars" errors

## Common Issues

### Missing star database
**Error:** `sqlite3.OperationalError: no such table: stars`
**Fix:** Run `python scripts/build_star_database.py`

### Multiprocessing manager errors
**Error:** `FileNotFoundError: [Errno 2] No such file or directory` from solver process
**Cause:** Main process crashed before child processes could connect
**Fix:** Check main process logs for root cause (often missing database tables)

## Pi Zero 2 W Specific Configuration

The official `pifinder_setup.sh` script is designed for Raspberry Pi 4 (BCM2711). For **Pi Zero 2 W** (BCM2837), use the dedicated setup script or manually configure as described below.

### Quick Setup

Use the Pi Zero 2 W specific setup script:

```bash
./pifinder_setup_zero2w.sh
```

Or reference these Pi Zero 2 W specific config files:
- `pi_config_files/boot_config_zero2w.txt` - Boot configuration example
- `pi_config_files/gpsd_zero2w.conf` - GPS daemon configuration

### Manual Configuration

For manual setup or understanding the differences:

### Issue: Hardware UART Incompatibility

**Root cause:**
- Pi 4 (BCM2711) has uart3 peripheral that can map to GPIOs 4-5
- Pi Zero 2 W (BCM2837) only has uart0, which cannot use GPIOs 4-5
- PiFinder v3 board GPS is physically wired to GPIOs 4-5 (designed for Pi 4)
- Result: No hardware UART available on the GPIOs where GPS is connected

### Solution: Firmware Software UART at 4800 Baud

Use the Raspberry Pi firmware's software UART implementation via `rpi-fw-uart` overlay with GPS configured to 4800 baud.

**Why 4800 baud is needed:**
- Stock `rpi-fw-uart` driver polls RX FIFO every 50ms
- FIFO buffer is only 32 bytes
- At 9600 baud, GPS sends ~48 bytes in 50ms → FIFO overflow → data corruption
- At 4800 baud, GPS sends ~24 bytes in 50ms → fits in FIFO → clean data

**Implementation:**
- GPS boots at factory default 9600 baud NMEA
- Startup script (`gps_set_4800_baud.py`) sends UBX command to switch to 4800 baud (runtime only, not saved)
- Script runs before gpsd starts (via systemd ExecStartPre)
- gpsd connects at 4800 baud
- No custom kernel drivers needed - uses stock `rpi-fw-uart`

**What this does:**
- Creates a software-based UART on GPIOs 4-5 via firmware
- Runs in the VideoCore firmware, using the second VPU core
- Appears as `/dev/ttyRFU0` to the system

### Complete Boot Config for Pi Zero 2 W

Your `/boot/firmware/config.txt` should include:

```
dtparam=spi=on
dtparam=i2c_arm=on
dtparam=i2c_arm_baudrate=10000
dtparam=audio=off
dtoverlay=pwm,pin=13,func=4
dtoverlay=rpi-fw-uart,txd0_pin=4,rxd0_pin=5
isp_use_vpu0=1
```

**Key differences from Pi 4:**
- Uses `rpi-fw-uart` (firmware software UART) instead of `uart3` hardware UART
- Requires `audio=off` and `isp_use_vpu0=1` for firmware UART to work
- GPS configured to 4800 baud via startup script to avoid FIFO overflow
- No Bluetooth overlay needed (internal Bluetooth routing doesn't conflict)

### Overlay Translation Reference

| Function | Pi 4 (BCM2711) | Pi Zero 2 W (BCM2837) | Notes |
|----------|----------------|------------------------|-------|
| SPI | `dtparam=spi=on` | Same | Universal |
| I2C | `dtparam=i2c_arm=on` | Same | Universal |
| I2C Speed | `dtparam=i2c_arm_baudrate=10000` | Same | Universal |
| Audio | `dtparam=audio=on` | `dtparam=audio=off` | **Must disable for fw UART** |
| PWM (GPIO 13) | `dtoverlay=pwm,pin=13,func=4` | Same | Pin 13 works on both |
| GPS UART (GPIO 4-5) | `dtoverlay=uart3` | `dtoverlay=rpi-fw-uart,txd0_pin=4,rxd0_pin=5` | **Firmware UART required** |
| ISP config | Not needed | `isp_use_vpu0=1` | **Required for fw UART** |

### GPS Daemon Configuration

Update `/etc/default/gpsd` to use the firmware UART device:

```bash
DEVICES="/dev/ttyRFU0"
GPSD_OPTIONS="-s 9600"
```

**Note:**
- Stock Pi 4 configuration uses `/dev/ttyAMA1` (hardware UART)
- Pi Zero 2 W with **custom driver** uses `/dev/ttyRFU0` (firmware software UART)
- The `-s 9600` option sets the baud rate explicitly for the GPS module

### Verification

# Check that ttyRFU0 exists for GPS
ls -l /dev/ttyRFU0

# Verify gpsd is configured correctly
cat /etc/default/gpsd | grep DEVICES

# Check that GPS daemon is running
systemctl status gpsd

# Test GPS data quality (should show clean NMEA sentences)
gpspipe -r | head -20

# Monitor PiFinder GPS connection
tail -f ~/PiFinder_data/pifinder.log | grep GPS
```

## Testing on Device

When deploying to a Pi Zero 2W for testing:

1. Build the star database on the device (or copy from development machine)
2. Run via systemd service: `systemctl --user start pifinder.service`
3. Monitor logs: `tail -f ~/PiFinder_data/pifinder.log`

Note: This branch uses lazy catalog loading by default for memory optimization.

## API Endpoints for Monitoring

Recent additions for performance monitoring:

- `GET /api/metrics` - JSON with memory, CPU, GC stats, solver timing
- `GET /api/solved_frame/data` - JSON metadata for last solved frame
- `GET /api/solved_frame/image` - PNG image (512x512) with centroids marked

Example:
```bash
curl http://localhost:8080/api/metrics | jq .
```
