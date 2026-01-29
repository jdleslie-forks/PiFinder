#!/usr/bin/bash
# This script installs the PiFinder software on a prepared Raspberry Pi OS.
# See https://pifinder.readthedocs.io/en/release/software.html for more info.

set -e

cd ~pifinder/

sudo apt-get install -y git python3-pip samba samba-common-bin dnsmasq hostapd dhcpd gpsd \
    libjpeg-dev zlib1g-dev python3-dev libinput10 python3-picamera2

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

# Setup GPSD
sudo dpkg-reconfigure -plow gpsd
sudo cp ~/PiFinder/pi_config_files/gpsd.conf /etc/default/gpsd

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

# Enable interfaces
grep -q "^dtparam=spi=on" /boot/config.txt || \
   echo "dtparam=spi=on" | sudo tee -a /boot/config.txt
grep -q "^dtparam=i2c_arm=on" /boot/config.txt || \
   echo "dtparam=i2c_arm=on" | sudo tee -a /boot/config.txt
grep -q "^dtparam=i2c_arm_baudrate=10000" /boot/config.txt || \
   echo "dtparam=i2c_arm_baudrate=10000" | sudo tee -a /boot/config.txt
grep -q "^dtoverlay=pwm,pin=13,func=4" /boot/config.txt || \
   echo "dtoverlay=pwm,pin=13,func=4" | sudo tee -a /boot/config.txt
grep -q "^dtoverlay=uart3" /boot/config.txt || \
   echo "dtoverlay=uart3" | sudo tee -a /boot/config.txt
# Note: camera types are added lateron by python/PiFinder/switch_camera.py

# Enable I2C user-space access
grep -q "^i2c-dev" /etc/modules || echo "i2c-dev" | sudo tee -a /etc/modules
sudo usermod -a -G i2c pifinder

# Set BFQ I/O scheduler for SD card (better responsiveness on flash storage)
sudo cp ~/PiFinder/pi_config_files/60-ioschedulers.rules /etc/udev/rules.d/60-ioschedulers.rules

# Disable unwanted services
sudo systemctl disable ModemManager

# Enable service
sudo cp /home/pifinder/PiFinder/pi_config_files/pifinder.service /lib/systemd/system/pifinder.service
sudo cp /home/pifinder/PiFinder/pi_config_files/pifinder_splash.service /lib/systemd/system/pifinder_splash.service
sudo cp /home/pifinder/PiFinder/pi_config_files/cedar_detect.service /lib/systemd/system/cedar_detect.service
sudo systemctl daemon-reload
sudo systemctl enable cedar_detect
sudo systemctl enable pifinder
sudo systemctl enable pifinder_splash

echo "PiFinder setup complete, please restart the Pi"

