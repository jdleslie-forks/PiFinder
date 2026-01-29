#!/bin/bash
# Configure GT-U7 GPS module to 4800 baud for Pi Zero 2W firmware UART
# The firmware UART has a 32-byte buffer with 50ms polling, requiring 4800 baud to avoid overruns

set -e

DEVICE="/dev/ttyRFU0"
TARGET_BAUD=4800

echo "Configuring GT-U7 GPS module to ${TARGET_BAUD} baud..."

# Stop gpsd to release the serial port
echo "Stopping gpsd..."
sudo systemctl stop gpsd

# Give it a moment to release the port
sleep 1

# Configure serial port to 9600 baud (GPS default) and send PMTK command
echo "Sending PMTK251 command to set ${TARGET_BAUD} baud..."
stty -F ${DEVICE} 9600 cs8 -cstopb -parenb raw -echo

# Send command to set 4800 baud
# PMTK251,4800*14 (checksum is XOR of all chars between $ and *)
echo -ne '$PMTK251,4800*14\r\n' > ${DEVICE}

# Wait for GPS to process and switch
sleep 2

# Verify by reading at 4800 baud
echo "Verifying communication at ${TARGET_BAUD} baud..."
stty -F ${DEVICE} 4800 cs8 -cstopb -parenb raw -echo

# Read a few NMEA sentences to verify
timeout 3 cat ${DEVICE} | head -5 || {
    echo "Warning: Could not read data at ${TARGET_BAUD} baud"
    echo "GPS may need more time to acquire satellites"
}

# Restart gpsd
echo "Restarting gpsd..."
sudo systemctl start gpsd

echo "Done! GPS should now be configured for ${TARGET_BAUD} baud"
echo "Note: GPS may take several minutes to acquire satellites (cold start)"
