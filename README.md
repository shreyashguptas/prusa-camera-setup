# Prusa Camera Setup

A complete camera system for Prusa 3D printers that:
- Streams camera feed to Prusa Connect
- Automatically records timelapses when printing
- Saves videos to NAS storage

Designed for Raspberry Pi Zero 2 W with camera module.

## Features

- **Prusa Connect Integration**: Camera feed visible in Prusa app
- **Auto-Detection**: Automatically starts/stops timelapse when print begins/ends
- **NAS Storage**: Saves timelapses directly to your NAS (via TailScale)
- **YouTube-Ready**: Creates MP4 videos optimized for upload
- **One-Command Setup**: Interactive `setup.py` handles all configuration

## Prerequisites

### Hardware
- Raspberry Pi Zero 2 W (or any Pi with camera support)
- Raspberry Pi Camera Module (v2 or v3)
- Network connection (WiFi or Ethernet)

### Software (on Pi)
```bash
# Update system
sudo apt update && sudo apt upgrade -y

# Install dependencies
sudo apt install -y rpicam-apps ffmpeg cifs-utils smbclient python3 python3-pip

# Install TailScale (for NAS access)
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up

# Install Python requirements
pip3 install requests simple-term-menu
```

### Camera Configuration

Ensure your `/boot/firmware/config.txt` contains:
```
camera_auto_detect=1
```

Reboot after making changes.

### NAS Setup
- TrueNAS or any SMB-compatible NAS
- TailScale installed on NAS for secure remote access
- SMB share created for timelapse storage

## Installation

1. **Clone the repository on your Pi:**
   ```bash
   cd ~
   git clone https://github.com/YOUR_USERNAME/prusa-camera-setup.git
   cd prusa-camera-setup
   ```

2. **Run setup:**
   ```bash
   python3 setup.py
   ```

3. **Follow the prompts to configure:**
   - Prusa Connect credentials (UUID, Camera Token, API Key)
   - NAS connection (IP, share path, credentials)
   - Timelapse settings (capture interval, video quality)

## Getting Prusa Connect Credentials

### Printer UUID
1. Go to [connect.prusa3d.com](https://connect.prusa3d.com)
2. Click on your printer
3. Copy the UUID from the URL:
   `https://connect.prusa3d.com/printers/YOUR-UUID-HERE`

### Camera Token
1. On your printer page, click "Add Camera"
2. Generate a new camera token (20 characters)

### API Key (PrusaLink)
1. On your printer, go to Settings > Network > PrusaLink
2. Note the API key shown there (or find it in Prusa Connect under your printer's Settings tab)
3. This is used for auto-detecting print start/stop via the local PrusaLink API

### Printer IP Address
1. On your printer, go to Settings > Network > PrusaLink
2. Note the IP address shown
3. Or check your router for the printer's IP address

## Configuration

All configuration is stored in `~/.prusa_camera_config` (not in git).

Example configuration:
```ini
[prusa]
printer_uuid = abc123-def456-...
camera_token = XXXXXXXXXXXXXXXXXXXX
api_key = your_prusalink_api_key
printer_ip = your_printer_ip_here

[nas]
ip = your_nas_ip_here
share = storage/youtube-videos/printer-footage
mount_point = /mnt/nas/printer-footage
username = your_smb_user

[timelapse]
capture_interval = 30
video_fps = 30
video_quality = 20

[camera]
width = 1704
height = 1278
quality = 85
upload_interval = 12
```

## Usage

### Automatic Mode (Default)
Once setup is complete, the system runs automatically:
- Camera uploads to Prusa Connect every 12 seconds
- When a print starts, timelapse recording begins
- When print ends, video is created and saved to NAS

### Manual Timelapse Control
```bash
# Start recording
echo "my_print_name" > ~/.timelapse_recording

# Check status
cat ~/.timelapse_recording

# Stop recording (triggers video creation)
rm ~/.timelapse_recording
```

### Service Management
```bash
# Check service status
systemctl status prusacam
systemctl status timelapse-monitor

# View logs
journalctl -u prusacam -f
journalctl -u timelapse-monitor -f

# Restart services
sudo systemctl restart prusacam
sudo systemctl restart timelapse-monitor
```

## Troubleshooting

### Camera not working
```bash
# Test camera
rpicam-still -o test.jpg

# Check if camera is detected
vcgencmd get_camera
```

### NAS not mounting
```bash
# Test connectivity
ping YOUR_NAS_IP

# Check TailScale
tailscale status

# Try manual mount
sudo mount -t cifs //NAS_IP/share /mnt/nas -o credentials=/etc/smbcredentials
```

### API connection failing
- Verify your API key is from "PrusaConnect API Key" (not PrusaLink)
- Check the printer UUID matches your printer URL
- Ensure the API key has not expired

## File Structure
```
prusa-camera-setup/
├── setup.py                 # Interactive setup script
├── src/
│   ├── config.py           # Configuration management
│   ├── camera.py           # Camera capture (rpicam-still)
│   ├── uploader.py         # Prusa Connect upload
│   ├── printer.py          # Printer status API
│   ├── timelapse.py        # Timelapse recording
│   ├── nas.py              # NAS mount handling
│   └── uploader_service.py # Camera upload daemon
├── templates/
│   ├── prusacam.service    # Camera upload systemd unit
│   └── timelapse-monitor.service
├── requirements.txt
└── README.md
```

## License

MIT License - Feel free to use and modify.

## Contributing

Pull requests welcome! Please test on a Raspberry Pi before submitting.
