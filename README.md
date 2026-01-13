# Prusa Camera Setup

A plug-and-play camera system for Prusa 3D printers that streams to Prusa Connect and automatically records timelapses to your NAS.

## What This Does

1. **Live Camera Feed** - See your printer in the Prusa app from anywhere
2. **Automatic Timelapses** - Recording starts when you print, stops when done
3. **NAS Storage** - Videos saved directly to your network storage

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

## Quick Start (5 minutes)

### Step 1: Set Up Your Raspberry Pi

Flash Raspberry Pi OS Lite (64-bit) to your SD card using [Raspberry Pi Imager](https://www.raspberrypi.com/software/).

In the imager settings:
- Enable SSH
- Set your WiFi credentials
- Set hostname to `prusacam`

### Step 2: Enable the Camera

SSH into your Pi and enable the camera:

```bash
ssh pi@prusacam.local
```

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

## How It Works

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│  Pi Camera      │────▶│  Raspberry Pi    │────▶│  Prusa Connect  │
│  (captures)     │     │  (processes)     │     │  (live view)    │
└─────────────────┘     └────────┬─────────┘     └─────────────────┘
                                 │
                                 ▼
                        ┌────────────────┐
                        │  Your Printer  │
                        │  (PrusaLink)   │
                        └────────┬───────┘
                                 │ "Is it printing?"
                                 ▼
                        ┌────────────────┐
                        │  Your NAS      │
                        │  (timelapses)  │
                        └────────────────┘
```

**Services running on your Pi:**
- `prusacam.service` - Uploads camera snapshots to Prusa Connect every 12 seconds
- `timelapse-monitor.service` - Watches for prints, captures frames, creates videos

---

## Configuration

All settings are stored in `~/.prusa_camera_config`:

```ini
[prusa]
printer_uuid = your-printer-uuid
camera_token = XXXXXXXXXXXXXXXXXXXX
api_key = your_prusalink_api_key
printer_ip = 192.168.1.81

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

## Usage

### Automatic Mode (default)

Once set up, everything is automatic:
- Camera uploads to Prusa Connect continuously
- When a print starts, timelapse recording begins
- When the print ends, an MP4 video is created and saved to your NAS

### Manual Timelapse Control

```bash
# Start a manual recording
echo "my_project_name" > ~/.timelapse_recording

# Stop recording (video will be created)
rm ~/.timelapse_recording
```

### Managing Services

```bash
# Check status
systemctl status prusacam
systemctl status timelapse-monitor

# View live logs
journalctl -u timelapse-monitor -f

# Restart after config changes
sudo systemctl restart prusacam timelapse-monitor
```

---

## Troubleshooting

### Camera not working

```bash
# Test the camera
rpicam-still -o test.jpg

# Check if camera is detected
rpicam-hello --list-cameras
```

If no camera is found:
1. Check the ribbon cable connection
2. Make sure `camera_auto_detect=1` is in `/boot/firmware/config.txt`
3. Reboot

### NAS not mounting

```bash
# Check TailScale connection
tailscale status

# Test NAS connectivity
ping YOUR_NAS_IP

# Try manual mount
sudo mount -t cifs //NAS_IP/share /mnt/nas/printer-footage -o credentials=/etc/smbcredentials
```

### Timelapse not starting automatically

```bash
# Check service logs
journalctl -u timelapse-monitor -f

# Test printer API connection
curl -H "X-Api-Key: YOUR_API_KEY" http://YOUR_PRINTER_IP/api/v1/status
```

---

## File Structure

```
prusa-camera-setup/
├── setup.py                 # Interactive setup wizard
├── requirements.txt         # Python dependencies
├── README.md
├── src/
│   ├── config.py           # Configuration management
│   ├── camera.py           # Camera capture (rpicam-still)
│   ├── uploader.py         # Prusa Connect upload
│   ├── printer.py          # PrusaLink API client
│   ├── timelapse.py        # Timelapse recording & video creation
│   ├── nas.py              # NAS/SMB mount handling
│   └── uploader_service.py # Camera upload daemon
└── templates/
    ├── prusacam.service
    └── timelapse-monitor.service
```

---

## License

MIT License - Feel free to use and modify.

## Contributing

Pull requests welcome! Please test on a Raspberry Pi before submitting.
