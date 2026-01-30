#!/usr/bin/bash
# This script installs the PiFinder software on a Raspberry Pi Zero 2 W.
# For Pi 4 installation, use pifinder_setup.sh instead.
#
# Pi Zero 2 W requires different UART configuration due to having only one PL011 UART.
# See DEVELOPMENT.md for details.

set -e

cd ~pifinder/

sudo apt-get install -y git python3-pip samba samba-common-bin dnsmasq hostapd dhcpd gpsd zram-tools \
    libjpeg-dev zlib1g-dev python3-dev libinput10 python3-picamera2 llvm-15-runtime i2c-tools

if [[ -d PiFinder/ ]]; then
    cd PiFinder/ && git config pull.rebase false && git pull
else
    git clone --recursive --branch release https://github.com/brickbots/PiFinder.git
fi

# Ensure submodules are initialized (handles updates and interrupted clones)
cd ~/PiFinder/ && git submodule update --init --recursive

# Fix tetra3 import path issue in generated gRPC code
sed -i 's/^import cedar_detect_pb2 as/from tetra3 import cedar_detect_pb2 as/' \
  ~/PiFinder/python/PiFinder/tetra3/tetra3/cedar_detect_pb2_grpc.py

cd ~/PiFinder/ && sudo pip install --break-system-packages -r python/requirements.txt

# Setup GPSD - Pi Zero 2 W uses ttyRFU0 (firmware software UART)
# GPS baud rate is configured via PiFinder UI (Settings > Advanced > GPS Settings > GPS Baud Rate)
# Select "4800 (Pi Zero 2W)" for proper operation with firmware UART
sudo dpkg-reconfigure -plow gpsd
sudo sed -i 's|^DEVICES=""$|DEVICES="/dev/ttyRFU0"|' /etc/default/gpsd

# data dirs
[[ -d ~/PiFinder_data ]] || \
mkdir ~/PiFinder_data
[[ -d ~/PiFinder_data/captures ]] || \
mkdir ~/PiFinder_data/captures
[[ -d ~/PiFinder_data/obslists ]] || \
mkdir ~/PiFinder_data/obslists
[[ -d ~/PiFinder_data/screenshots ]] || \
mkdir ~/PiFinder_data/screenshots
[[ -d ~/PiFinder_data/solver_debug_dumps ]] || \
mkdir ~/PiFinder_data/solver_debug_dumps
[[ -d ~/PiFinder_data/logs ]] || \
mkdir ~/PiFinder_data/logs
find ~/PiFinder_data -type d -exec chmod 755 {} \;

# Wifi config
sudo cp ~/PiFinder/pi_config_files/dhcpcd.* /etc
sudo cp ~/PiFinder/pi_config_files/dhcpcd.conf.sta /etc/dhcpcd.conf
sudo cp ~/PiFinder/pi_config_files/dnsmasq.conf /etc/dnsmasq.conf
sudo cp ~/PiFinder/pi_config_files/hostapd.conf /etc/hostapd/hostapd.conf
echo -n "Client" > ~/PiFinder/wifi_status.txt
sudo systemctl unmask hostapd

# open permissisons on wpa_supplicant file so we can adjust network config
[[ -f /etc/wpa_supplicant/wpa_supplicant.conf ]] && \
sudo chmod 666 /etc/wpa_supplicant/wpa_supplicant.conf || true

# Samba config
sudo cp ~/PiFinder/pi_config_files/smb.conf /etc/samba/smb.conf

# Hipparcos catalog
HIP_MAIN_DAT="/home/pifinder/PiFinder/astro_data/hip_main.dat"
if [[ ! -e $HIP_MAIN_DAT ]]; then
    wget -O $HIP_MAIN_DAT https://cdsarc.cds.unistra.fr/ftp/cats/I/239/hip_main.dat
fi

# Build astronomical databases
echo "Building star database from Hipparcos catalog..."
cd ~/PiFinder/python && python3 scripts/build_star_database.py

echo "Building astronomical object catalogs (this may take several minutes)..."
cd ~/PiFinder/python && python3 -m PiFinder.catalog_imports.main

# Determine boot config location
BOOT_CONFIG="/boot/firmware/config.txt"
CMDLINE="/boot/firmware/cmdline.txt"
if [[ ! -f $BOOT_CONFIG ]]; then
    BOOT_CONFIG="/boot/config.txt"
    CMDLINE="/boot/cmdline.txt"
fi

# Disable serial console to free UART for GPS
#sudo sed -i 's/console=serial0,[0-9]* //' $CMDLINE

# Reduce GPU memory to 32MB for more system RAM (critical on Pi Zero 2W)
grep -q "^gpu_mem=32" $BOOT_CONFIG || \
   echo "gpu_mem=32" | sudo tee -a $BOOT_CONFIG

# Enable interfaces
grep -q "^dtparam=spi=on" $BOOT_CONFIG || \
   echo "dtparam=spi=on" | sudo tee -a $BOOT_CONFIG
grep -q "^dtparam=i2c_arm=on" $BOOT_CONFIG || \
   echo "dtparam=i2c_arm=on" | sudo tee -a $BOOT_CONFIG
grep -q "^dtparam=i2c_arm_baudrate=10000" $BOOT_CONFIG || \
   echo "dtparam=i2c_arm_baudrate=10000" | sudo tee -a $BOOT_CONFIG
grep -q "^dtoverlay=pwm,pin=13,func=4" $BOOT_CONFIG || \
   echo "dtoverlay=pwm,pin=13,func=4" | sudo tee -a $BOOT_CONFIG

# Pi Zero 2 W specific: Use firmware software UART for GPS on GPIOs 4-5
# Disable audio (required for firmware UART)
sudo sed -i 's/dtparam=audio=on/dtparam=audio=off/' $BOOT_CONFIG
grep -q "^dtoverlay=rpi-fw-uart" $BOOT_CONFIG || \
   echo "dtoverlay=rpi-fw-uart,txd0_pin=4,rxd0_pin=5" | sudo tee -a $BOOT_CONFIG
grep -q "^isp_use_vpu0=1" $BOOT_CONFIG || \
   echo "isp_use_vpu0=1" | sudo tee -a $BOOT_CONFIG

# Note: camera types are added lateron by python/PiFinder/switch_camera.py

# Enable I2C user-space access
grep -q "^i2c-dev" /etc/modules || echo "i2c-dev" | sudo tee -a /etc/modules
sudo usermod -a -G i2c pifinder

# Set BFQ I/O scheduler for SD card (better responsiveness on flash storage)
sudo cp ~/PiFinder/pi_config_files/60-ioschedulers.rules /etc/udev/rules.d/60-ioschedulers.rules

# Disable unwanted services
sudo systemctl disable ModemManager

# Disable traditional swap file in favor of zram compressed swap
if [[ -f /var/swap ]]; then
    sudo dphys-swapfile swapoff || true
    sudo systemctl disable dphys-swapfile || true
fi

# Enable zram compressed swap (critical for Pi Zero 2W with 512MB RAM)
sudo systemctl enable zramswap

# Enable services
sudo cp /home/pifinder/PiFinder/pi_config_files/pifinder.service /lib/systemd/system/pifinder.service
sudo cp /home/pifinder/PiFinder/pi_config_files/pifinder_splash.service /lib/systemd/system/pifinder_splash.service
sudo cp /home/pifinder/PiFinder/pi_config_files/cedar_detect.service /lib/systemd/system/cedar_detect.service
sudo systemctl daemon-reload
sudo systemctl enable cedar_detect
sudo systemctl enable pifinder
sudo systemctl enable pifinder_splash

echo "PiFinder setup complete for Pi Zero 2 W, please restart the Pi"
