# Prusa Connect App and Timelapse Recording Setup

**Live camera feed in the Prusa app + automatic timelapse videos saved to your NAS.**

A plug-and-play Raspberry Pi camera system for Prusa 3D printers that does two things:

1. **📱 Remote Monitoring** — Stream your print to the Prusa Connect app so you can check on it from anywhere
2. **🎬 Automatic Timelapses** — Recording starts when you print, stops when done, and saves the video to your NAS

---

## How It Works

<!-- TODO: Add system diagram image -->
![System Diagram](./docs/system-diagram.png)


---

## Hardware You'll Need

| Component | Recommended | Notes |
|-----------|-------------|-------|
| **Raspberry Pi** | Raspberry Pi Zero 2 W | Any Pi with camera port works |
| **Camera** | Raspberry Pi Camera Module 3 | Or Camera Module 2. Both work great |
| **Power Supply** | Official Pi power supply | 5V 2.5A minimum |
| **MicroSD Card** | 16GB+ | For Raspberry Pi OS |
| **NAS** | TrueNAS, Synology, etc. | Any SMB-compatible storage |

### About the Camera

This project uses **`rpicam-still`** from the `rpicam-apps` package - the official Raspberry Pi camera software. It works with:

- **Raspberry Pi Camera Module 3** (recommended) - 12MP, autofocus
- **Raspberry Pi Camera Module 2** - 8MP, fixed focus
- **Raspberry Pi Camera Module 3 Wide** - Wider field of view
- **Raspberry Pi HQ Camera** - For advanced users

**Note:** USB webcams are NOT supported. You need a Pi camera that connects via the ribbon cable.

---

## Quick Start

### Step 1: Set Up Your Raspberry Pi

Flash Raspberry Pi OS Lite (64-bit) to your SD card using [Raspberry Pi Imager](https://www.raspberrypi.com/software/).

In the imager settings:
- Enable SSH
- Set your WiFi credentials
- Set a hostname (e.g., `prusacam`)

### Step 2: Enable the Camera

SSH into your Pi:

```bash
ssh <your-username>@<your-hostname>.local
# or use the IP address directly:
ssh <your-username>@<your-pi-ip-address>
```

> **Note:** Replace `<your-username>` with the username you set during Pi setup, and `<your-hostname>` with your Pi's hostname or IP address.

Edit the boot config:

```bash
sudo nano /boot/firmware/config.txt
```

Make sure this line exists (add it if missing):

```
camera_auto_detect=1
```

Reboot:

```bash
sudo reboot
```

### Step 3: Install Dependencies

SSH back in and run:

```bash
# Update system
sudo apt update && sudo apt upgrade -y

# Install required packages
sudo apt install -y git rpicam-apps ffmpeg cifs-utils smbclient python3 python3-pip

# Install TailScale (for secure NAS access)
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

### Step 4: Clone and Run Setup

```bash
cd ~
git clone https://github.com/shreyashguptas/prusa-camera-setup.git
cd prusa-camera-setup
python3 setup.py
```

The setup wizard will guide you through:
1. Entering your Prusa Connect credentials
2. Connecting to your NAS
3. Configuring timelapse settings
4. Installing the services

**That's it!** Your camera will now stream to Prusa Connect and automatically record timelapses.

---

## Getting Your Credentials

You'll need these during setup:

### From Prusa Connect (connect.prusa3d.com)

1. **Printer UUID**
   - Go to your printer's page on Prusa Connect
   - Copy the UUID from the URL: `https://connect.prusa3d.com/printers/YOUR-UUID-HERE`

2. **Camera Token**
   - On your printer's page, click "Add Camera"
   - Click "Generate" to create a new camera token (20 characters)

### From Your Printer

3. **PrusaLink API Key**
   - On your printer: Settings > Network > PrusaLink
   - Note the API key shown

4. **Printer IP Address**
   - On your printer: Settings > Network > PrusaLink
   - Note the IP address (e.g., `192.168.1.81`)

---

## Configuration

All settings are stored in `~/.prusa_camera_config`:

```ini
[prusa]
printer_uuid = your-printer-uuid
camera_token = XXXXXXXXXXXXXXXXXXXX
api_key = your_prusalink_api_key
printer_ip = your-printer-ip-address

[nas]
ip = 100.x.x.x
share = storage/printer-footage
mount_point = /mnt/nas/printer-footage
username = your_smb_user

[timelapse]
capture_interval = 30    # Seconds between frames
video_fps = 30           # Output video framerate
video_quality = 20       # FFmpeg CRF (lower = better quality)

[camera]
width = 1704
height = 1278
quality = 85             # JPEG quality
upload_interval = 12     # Seconds between Prusa Connect uploads
```

To change settings, either edit this file or run `python3 setup.py` again.

---

## License

MIT License - Feel free to use and modify.

## Contributing

Pull requests welcome! Please test on a Raspberry Pi before submitting.
