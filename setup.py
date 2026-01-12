#!/usr/bin/env python3
"""
Prusa Camera Setup - Interactive setup script for Raspberry Pi camera
with Prusa Connect integration and NAS timelapse storage.
"""

import os
import sys
import subprocess
import shutil
import getpass
from pathlib import Path
from typing import Optional, List

try:
    from simple_term_menu import TerminalMenu
    HAS_MENU = True
except ImportError:
    HAS_MENU = False

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent))

from src.config import Config
from src.camera import Camera
from src.uploader import PrusaConnectUploader
from src.printer import PrinterStatus
from src.nas import NASMount


def list_smb_shares(nas_ip: str, username: str, password: str) -> List[str]:
    """List available SMB shares on the NAS."""
    try:
        result = subprocess.run(
            ["smbclient", "-L", nas_ip, "-U", f"{username}%{password}", "-g"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        shares = []
        for line in result.stdout.split("\n"):
            if line.startswith("Disk|"):
                parts = line.split("|")
                if len(parts) >= 2:
                    share_name = parts[1]
                    # Skip system shares
                    if not share_name.endswith("$"):
                        shares.append(share_name)
        return shares
    except Exception:
        return []


def list_smb_directory(nas_ip: str, share: str, path: str, username: str, password: str) -> List[str]:
    """List directories in an SMB path."""
    try:
        smb_path = f"//{nas_ip}/{share}"
        if path:
            smb_path += f"/{path}"

        result = subprocess.run(
            ["smbclient", smb_path, "-U", f"{username}%{password}", "-c", "ls"],
            capture_output=True,
            text=True,
            timeout=30,
        )

        dirs = []
        for line in result.stdout.split("\n"):
            line = line.strip()
            if not line:
                continue
            # Parse smbclient ls output: "dirname    D    0  Mon Jan 12 10:00:00 2026"
            if "  D  " in line or "\tD\t" in line:
                # Extract directory name (first part before multiple spaces)
                parts = line.split()
                if parts:
                    dirname = parts[0]
                    if dirname not in (".", ".."):
                        dirs.append(dirname)
        return sorted(dirs)
    except Exception:
        return []


def browse_smb_interactive(nas_ip: str, username: str, password: str) -> Optional[str]:
    """Interactively browse SMB shares and return selected path."""
    if not HAS_MENU:
        print("  Arrow-key navigation not available. Install: pip3 install simple-term-menu")
        return None

    # First, list shares
    print("  Fetching available shares...")
    shares = list_smb_shares(nas_ip, username, password)

    if not shares:
        print("  No shares found or could not connect.")
        return None

    # Select share
    print()
    print("  Use arrow keys to select, Enter to confirm:")
    menu = TerminalMenu(shares, title="  Select SMB Share:")
    share_idx = menu.show()

    if share_idx is None:
        return None

    selected_share = shares[share_idx]
    current_path = ""

    # Browse directories
    while True:
        display_path = f"{selected_share}/{current_path}" if current_path else selected_share
        print(f"\n  Current: //{nas_ip}/{display_path}")
        print("  Fetching folders...")

        dirs = list_smb_directory(nas_ip, selected_share, current_path, username, password)

        # Build menu options
        options = ["[SELECT THIS FOLDER]", "[GO BACK]"] + dirs

        menu = TerminalMenu(options, title="  Navigate folders:")
        choice_idx = menu.show()

        if choice_idx is None or choice_idx == 1:  # Cancel or Go Back
            if current_path:
                # Go up one level
                current_path = "/".join(current_path.split("/")[:-1])
            else:
                # At root, go back to share selection
                return browse_smb_interactive(nas_ip, username, password)
        elif choice_idx == 0:  # Select this folder
            if current_path:
                return f"{selected_share}/{current_path}"
            return selected_share
        else:
            # Enter selected directory
            selected_dir = options[choice_idx]
            if current_path:
                current_path = f"{current_path}/{selected_dir}"
            else:
                current_path = selected_dir


def print_header(text: str):
    """Print a section header."""
    print()
    print("=" * 50)
    print(f"  {text}")
    print("=" * 50)
    print()


def print_step(step: int, total: int, text: str):
    """Print a step indicator."""
    print(f"[{step}/{total}] {text}")


def prompt(text: str, default: str = "") -> str:
    """Prompt user for input with optional default."""
    if default:
        result = input(f"{text} [{default}]: ").strip()
        return result if result else default
    return input(f"{text}: ").strip()


def prompt_password(text: str) -> str:
    """Prompt for password (hidden input)."""
    return getpass.getpass(f"{text}: ")


def prompt_int(text: str, default: int) -> int:
    """Prompt for integer with default."""
    while True:
        result = prompt(text, str(default))
        try:
            return int(result)
        except ValueError:
            print("Please enter a valid number.")


def confirm(text: str, default: bool = True) -> bool:
    """Ask for confirmation."""
    suffix = " [Y/n]" if default else " [y/N]"
    result = input(f"{text}{suffix}: ").strip().lower()
    if not result:
        return default
    return result in ("y", "yes")


def check_camera_config() -> bool:
    """Check if camera_auto_detect=1 is in config.txt."""
    for config_path in [Path("/boot/firmware/config.txt"), Path("/boot/config.txt")]:
        if config_path.exists():
            try:
                content = config_path.read_text()
                for line in content.split("\n"):
                    line = line.strip()
                    if line.startswith("#"):
                        continue
                    if "camera_auto_detect" in line and "=1" in line:
                        return True
            except Exception:
                pass
    return False


def check_prerequisites() -> bool:
    """Check and display prerequisites."""
    print_header("Prerequisites Check")

    # Define checks: (name, check_function, apt_package, install_instruction)
    checks = [
        ("camera_auto_detect=1", check_camera_config(), None, None),
        ("rpicam-still", shutil.which("rpicam-still") is not None, "rpicam-apps", None),
        ("ffmpeg", shutil.which("ffmpeg") is not None, "ffmpeg", None),
        ("TailScale", shutil.which("tailscale") is not None, None, "curl -fsSL https://tailscale.com/install.sh | sh"),
        ("cifs-utils", Path("/sbin/mount.cifs").exists(), "cifs-utils", None),
        ("smbclient", shutil.which("smbclient") is not None, "smbclient", None),
    ]

    missing = []
    for name, ok, apt_pkg, custom_install in checks:
        status = "[OK]" if ok else "[MISSING]"
        print(f"  {status} {name}")
        if not ok:
            missing.append((name, apt_pkg, custom_install))

    print()

    if not missing:
        print("All prerequisites satisfied!")
    else:
        # Collect apt packages that can be auto-installed
        apt_packages = [pkg for name, pkg, _ in missing if pkg]
        custom_installs = [(name, cmd) for name, _, cmd in missing if cmd]
        config_missing = any(name == "camera_auto_detect=1" for name, _, _ in missing)

        # Offer to auto-install apt packages
        if apt_packages:
            print(f"Missing packages: {', '.join(apt_packages)}")
            if confirm("Install missing packages automatically?", default=True):
                print(f"  Running: sudo apt install -y {' '.join(apt_packages)}")
                result = subprocess.run(
                    ["sudo", "apt", "install", "-y"] + apt_packages,
                    capture_output=False,
                )
                if result.returncode != 0:
                    print("  Installation failed. Please install manually.")
                    if not confirm("Continue anyway?", default=False):
                        return False
                else:
                    print("  Packages installed successfully!")
            else:
                print()
                print("To install manually:")
                print(f"  sudo apt install -y {' '.join(apt_packages)}")
                if not confirm("Continue anyway?", default=False):
                    return False

        # Show custom install instructions (TailScale)
        for name, cmd in custom_installs:
            print()
            print(f"{name} requires manual installation:")
            print(f"  {cmd}")
            if not confirm("Continue anyway?", default=False):
                return False

        # Camera config check
        if config_missing:
            print()
            print("Camera not enabled in config.txt!")
            print("Add this line to /boot/firmware/config.txt:")
            print("  camera_auto_detect=1")
            print()
            print("Then reboot your Pi.")
            if not confirm("Continue anyway?", default=False):
                return False

    # Check TailScale status (only if installed)
    if shutil.which("tailscale"):
        print()
        print("Checking TailScale connection...")
        try:
            result = subprocess.run(
                ["tailscale", "status"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                print("  TailScale is not connected.")
                print("  Run: sudo tailscale up")
                if not confirm("Continue anyway?", default=False):
                    return False
            else:
                print("  TailScale is connected.")
        except Exception:
            print("  Could not check TailScale status.")

    print()
    return True


def setup_prusa_connect(config: Config) -> bool:
    """Configure Prusa Connect credentials."""
    print_header("Prusa Connect Setup")

    print("You need the following from Prusa Connect (connect.prusa3d.com):")
    print()
    print("1. Printer UUID - Found in the URL when viewing your printer")
    print("   Example: https://connect.prusa3d.com/printers/YOUR-UUID-HERE")
    print()
    print("2. Camera Token - Generate from 'Add Camera' on your printer page")
    print("   (20 character token)")
    print()
    print("3. API Key - Generate from Account > API Access")
    print("   Select 'PrusaConnect API Key' (NOT PrusaLink API Key)")
    print("   (Used for auto-detecting print start/stop)")
    print()

    # Printer UUID
    current_uuid = config.printer_uuid
    printer_uuid = prompt("Printer UUID", current_uuid)
    if not printer_uuid:
        print("Printer UUID is required.")
        return False
    config.set("prusa", "printer_uuid", printer_uuid)

    # Camera Token
    current_token = config.camera_token
    camera_token = prompt("Camera Token (20 chars)", current_token)
    if not camera_token:
        print("Camera token is required.")
        return False
    config.set("prusa", "camera_token", camera_token)

    # API Key
    current_api_key = config.api_key
    api_key = prompt("API Key (for auto-detection)", current_api_key)
    if not api_key:
        print("API key is required for auto-detection.")
        return False
    config.set("prusa", "api_key", api_key)

    # Test camera connection
    print()
    print("Testing camera upload connection...")
    uploader = PrusaConnectUploader(camera_token)
    ok, error = uploader.test_connection()
    if ok:
        print("  Camera connection: OK")
    else:
        print(f"  Camera connection: FAILED ({error})")
        if not confirm("Continue anyway?", default=False):
            return False

    # Test API connection
    print("Testing API connection...")
    printer = PrinterStatus(api_key, printer_uuid)
    ok, error = printer.test_connection()
    if ok:
        print("  API connection: OK")
    else:
        print(f"  API connection: FAILED ({error})")
        if not confirm("Continue anyway?", default=False):
            return False

    return True


def setup_nas(config: Config) -> bool:
    """Configure NAS storage."""
    print_header("NAS Storage Setup")

    print("Configure your TrueNAS/SMB share for timelapse storage.")
    print("Ensure TailScale is connected and NAS is reachable.")
    print()

    # NAS IP
    current_ip = config.nas_ip
    nas_ip = prompt("NAS IP address (TailScale IP)", current_ip)
    config.set("nas", "ip", nas_ip)

    # Test NAS connectivity
    print(f"Testing connectivity to {nas_ip}...")
    try:
        result = subprocess.run(
            ["ping", "-c", "1", "-W", "3", nas_ip],
            capture_output=True,
            timeout=10,
        )
        if result.returncode == 0:
            print("  NAS is reachable.")
        else:
            print("  NAS is not reachable. Check TailScale connection.")
            if not confirm("Continue anyway?", default=False):
                return False
    except Exception:
        print("  Could not test connectivity.")

    # SMB credentials (need these first to browse)
    print()
    print("Enter SMB credentials for the NAS:")
    current_user = config.nas_username
    username = prompt("SMB Username", current_user)
    config.set("nas", "username", username)

    password = prompt_password("SMB Password")

    # Share path - try interactive browse first
    print()
    share_path = None

    if HAS_MENU and shutil.which("smbclient"):
        if confirm("Browse NAS folders interactively?", default=True):
            share_path = browse_smb_interactive(nas_ip, username, password)
            if share_path:
                print(f"  Selected: {share_path}")

    if not share_path:
        # Manual entry fallback
        print()
        print("SMB Share Path - where to find it:")
        print("  TrueNAS: Shares > Windows Shares (SMB) > Path column")
        print("  Synology: Control Panel > Shared Folder > folder name")
        print("  Example: 'storage/videos/printer' or just 'printer-footage'")
        print()
        current_share = config.nas_share
        share_path = prompt("SMB share path", current_share)

    config.set("nas", "share", share_path)

    # Mount point
    print()
    print("Local mount point - this is a folder ON YOUR PI where NAS files will appear.")
    print("The default is fine for most users.")
    current_mount = config.nas_mount_point or "/mnt/nas/printer-footage"
    mount_point = prompt("Local mount point", current_mount)
    config.set("nas", "mount_point", mount_point)

    # Set up NAS mount
    nas = NASMount(nas_ip, share_path, mount_point, username)

    print()
    print("Setting up SMB credentials...")
    if nas.setup_credentials(password):
        print("  Credentials saved to /etc/smbcredentials")
    else:
        print("  Failed to save credentials (needs sudo)")
        return False

    print("Creating mount point...")
    if nas.create_mount_point():
        print(f"  Created {mount_point}")
    else:
        print("  Failed to create mount point")
        return False

    print("Testing NAS mount...")
    ok, error = nas.mount()
    if ok:
        print("  NAS mounted successfully!")
        # Verify we can write
        test_file = Path(mount_point) / ".write_test"
        try:
            test_file.touch()
            test_file.unlink()
            print("  Write access: OK")
        except Exception:
            print("  Write access: FAILED")
            return False
    else:
        print(f"  Mount failed: {error}")
        return False

    return True


def setup_timelapse_settings(config: Config) -> bool:
    """Configure timelapse settings."""
    print_header("Timelapse Settings")

    print("Configure how timelapses are captured and created.")
    print()

    # Capture interval
    current = config.capture_interval
    interval = prompt_int("Capture interval (seconds)", current)
    config.set("timelapse", "capture_interval", str(interval))

    # Video FPS
    current = config.video_fps
    fps = prompt_int("Video FPS", current)
    config.set("timelapse", "video_fps", str(fps))

    # Video quality
    print()
    print("Video quality (CRF value):")
    print("  18 = High quality (larger files)")
    print("  20 = Good quality (recommended)")
    print("  23 = Medium quality")
    print("  28 = Lower quality (smaller files)")
    current = config.video_quality
    quality = prompt_int("Video quality (CRF)", current)
    config.set("timelapse", "video_quality", str(quality))

    return True


def setup_camera_settings(config: Config) -> bool:
    """Configure camera settings."""
    print_header("Camera Settings")

    print("Configure camera capture settings.")
    print("Default values work well for Prusa Connect.")
    print()

    # Resolution
    current_w = config.camera_width
    current_h = config.camera_height
    width = prompt_int("Image width", current_w)
    height = prompt_int("Image height", current_h)
    config.set("camera", "width", str(width))
    config.set("camera", "height", str(height))

    # Quality
    current = config.camera_quality
    quality = prompt_int("JPEG quality (1-100)", current)
    config.set("camera", "quality", str(quality))

    # Upload interval
    current = config.upload_interval
    interval = prompt_int("Upload interval (seconds)", current)
    config.set("camera", "upload_interval", str(interval))

    return True


def install_services(config: Config) -> bool:
    """Install systemd services."""
    print_header("Installing Services")

    install_dir = Path(__file__).parent.resolve()
    user = os.environ.get("USER", "shreyash")
    mount_point = config.nas_mount_point
    mount_unit = mount_point.replace("/", "-").lstrip("-") + ".mount"

    # Read and customize service templates
    templates_dir = install_dir / "templates"

    services = [
        ("prusacam.service", "Camera upload service"),
        ("timelapse-monitor.service", "Timelapse monitor service"),
    ]

    for service_file, description in services:
        print(f"Installing {description}...")

        template_path = templates_dir / service_file
        if not template_path.exists():
            print(f"  Template not found: {template_path}")
            continue

        content = template_path.read_text()
        content = content.replace("{{INSTALL_DIR}}", str(install_dir))
        content = content.replace("{{USER}}", user)
        content = content.replace("{{MOUNT_UNIT}}", mount_unit)

        # Write to temp file and move with sudo
        temp_path = Path(f"/tmp/{service_file}")
        temp_path.write_text(content)

        result = subprocess.run(
            ["sudo", "mv", str(temp_path), f"/etc/systemd/system/{service_file}"],
            capture_output=True,
        )
        if result.returncode != 0:
            print(f"  Failed to install {service_file}")
            continue

        print(f"  Installed /etc/systemd/system/{service_file}")

    # Create NAS mount unit
    print("Creating NAS mount unit...")
    nas = NASMount(
        config.nas_ip,
        config.nas_share,
        config.nas_mount_point,
        config.nas_username,
    )

    mount_content = nas.get_systemd_mount_unit()
    temp_path = Path(f"/tmp/{mount_unit}")
    temp_path.write_text(mount_content)

    subprocess.run(
        ["sudo", "mv", str(temp_path), f"/etc/systemd/system/{mount_unit}"],
        capture_output=True,
    )

    # Reload systemd and enable services
    print("Enabling services...")
    subprocess.run(["sudo", "systemctl", "daemon-reload"], capture_output=True)

    subprocess.run(
        ["sudo", "systemctl", "enable", mount_unit],
        capture_output=True,
    )
    subprocess.run(
        ["sudo", "systemctl", "enable", "prusacam.service"],
        capture_output=True,
    )
    subprocess.run(
        ["sudo", "systemctl", "enable", "timelapse-monitor.service"],
        capture_output=True,
    )

    # Start services
    print("Starting services...")
    subprocess.run(
        ["sudo", "systemctl", "start", mount_unit],
        capture_output=True,
    )
    subprocess.run(
        ["sudo", "systemctl", "start", "prusacam.service"],
        capture_output=True,
    )
    subprocess.run(
        ["sudo", "systemctl", "start", "timelapse-monitor.service"],
        capture_output=True,
    )

    print()
    print("Services installed and started!")
    print()
    print("Check status with:")
    print("  systemctl status prusacam")
    print("  systemctl status timelapse-monitor")
    print()

    return True


def main():
    """Main setup entry point."""
    print()
    print("=" * 50)
    print("     Prusa Camera Setup")
    print("     Raspberry Pi Camera for Prusa Connect")
    print("     with NAS Timelapse Storage")
    print("=" * 50)
    print()

    # Load or create config
    config = Config()
    config.load()

    # Step 1: Prerequisites
    if not check_prerequisites():
        print("Setup cancelled.")
        sys.exit(1)

    # Step 2: Prusa Connect
    if not setup_prusa_connect(config):
        print("Setup cancelled.")
        sys.exit(1)

    # Save config after Prusa setup
    config.save()
    print("Configuration saved.")

    # Step 3: NAS Storage
    if not setup_nas(config):
        print("Setup cancelled.")
        sys.exit(1)

    # Save config after NAS setup
    config.save()

    # Step 4: Timelapse Settings
    if not setup_timelapse_settings(config):
        print("Setup cancelled.")
        sys.exit(1)

    # Step 5: Camera Settings (optional)
    if confirm("Configure advanced camera settings?", default=False):
        setup_camera_settings(config)

    # Save final config
    config.save()
    print("Configuration saved to ~/.prusa_camera_config")

    # Step 6: Install Services
    if confirm("Install and start systemd services?", default=True):
        if not install_services(config):
            print("Service installation had issues. Check manually.")

    print_header("Setup Complete!")

    print("Your Prusa camera is now configured!")
    print()
    print("What happens now:")
    print("  1. Camera uploads snapshots to Prusa Connect every",
          f"{config.upload_interval}s")
    print("  2. When you start a print, timelapse recording begins automatically")
    print("  3. When print completes, video is created and saved to NAS")
    print()
    print("Videos will be saved to:")
    print(f"  {config.nas_mount_point}/")
    print()
    print("Manual timelapse control:")
    print("  Start: echo 'my_print' > ~/.timelapse_recording")
    print("  Stop:  rm ~/.timelapse_recording")
    print()
    print("View logs:")
    print("  journalctl -u prusacam -f")
    print("  journalctl -u timelapse-monitor -f")
    print()


if __name__ == "__main__":
    main()
