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
from src.uploader import PrusaConnectUploader
from src.printer import PrinterStatus
from src.nas import NASMount


def list_smb_shares(nas_ip: str, username: str, password: str) -> tuple[List[str], str]:
    """List available SMB shares on the NAS. Returns (shares, error_msg)."""
    try:
        result = subprocess.run(
            ["smbclient", "-L", nas_ip, "-U", f"{username}%{password}", "-g"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            error = result.stderr.strip() if result.stderr else "Unknown error"
            return [], f"smbclient failed: {error}"

        shares = []
        for line in result.stdout.split("\n"):
            if line.startswith("Disk|"):
                parts = line.split("|")
                if len(parts) >= 2:
                    share_name = parts[1]
                    # Skip system shares
                    if not share_name.endswith("$"):
                        shares.append(share_name)
        if not shares:
            return [], "No shares found in smbclient output"
        return shares, ""
    except subprocess.TimeoutExpired:
        return [], "Connection timed out"
    except Exception as e:
        return [], str(e)


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
        print("  Arrow-key navigation not available.")
        print("  Install with: pip3 install simple-term-menu")
        return None

    if not shutil.which("smbclient"):
        print("  smbclient not installed. Cannot browse NAS.")
        return None

    # First, list shares
    print("  Connecting to NAS and fetching shares...")
    shares, error = list_smb_shares(nas_ip, username, password)

    if error:
        print(f"  Failed to list shares: {error}")
        return None

    if not shares:
        print("  No shares found on NAS.")
        return None

    print(f"  Found {len(shares)} share(s): {', '.join(shares)}")

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


def find_config_txt() -> Optional[Path]:
    """Find the boot config.txt file."""
    for path in [Path("/boot/firmware/config.txt"), Path("/boot/config.txt")]:
        if path.exists():
            return path
    return None


def optimize_memory() -> bool:
    """Optimize memory for Pi Zero 2W: reduce CMA, GPU mem, disable unused hardware/services."""
    print_header("Memory Optimization (Pi Zero 2W)")

    print("The Pi Zero 2W has only 512MB RAM (416MB usable).")
    print("Default settings waste ~250MB on unused features.")
    print()
    print("This step will:")
    print("  - Reduce CMA reservation from 256MB to 64MB")
    print("  - Reduce GPU memory from 64MB to 32MB")
    print("  - Disable audio, Bluetooth, and framebuffers")
    print("  - Disable ModemManager and polkit services")
    print()

    if not confirm("Apply memory optimizations?", default=True):
        print("Skipping memory optimization.")
        return True

    config_path = find_config_txt()
    if not config_path:
        print("Could not find /boot/firmware/config.txt")
        return confirm("Continue anyway?", default=True)

    try:
        content = config_path.read_text()
    except PermissionError:
        result = subprocess.run(
            ["sudo", "cat", str(config_path)],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(f"  Failed to read {config_path}")
            return confirm("Continue anyway?", default=True)
        content = result.stdout

    changes = []

    # 1. Reduce CMA to 64MB
    if "dtoverlay=vc4-kms-v3d,cma-" in content:
        print("  [OK] CMA already configured")
    elif "dtoverlay=vc4-kms-v3d" in content:
        content = content.replace(
            "dtoverlay=vc4-kms-v3d",
            "dtoverlay=vc4-kms-v3d,cma-64",
        )
        changes.append("CMA reduced to 64MB")
    else:
        print("  [SKIP] vc4-kms-v3d overlay not found")

    # 2. Set gpu_mem=32
    if "gpu_mem=" in content:
        print("  [OK] gpu_mem already configured")
    else:
        # Add after [all] section if it exists, otherwise append
        if "[all]" in content:
            content = content.replace("[all]", "[all]\ngpu_mem=32", 1)
        else:
            content += "\ngpu_mem=32\n"
        changes.append("GPU memory set to 32MB")

    # 3. Disable audio
    if "dtparam=audio=off" in content:
        print("  [OK] Audio already disabled")
    elif "dtparam=audio=on" in content:
        content = content.replace("dtparam=audio=on", "dtparam=audio=off")
        changes.append("Audio disabled")

    # 4. Set max_framebuffers=0
    if "max_framebuffers=0" in content:
        print("  [OK] Framebuffers already disabled")
    elif "max_framebuffers=" in content:
        # Replace any existing value
        lines = content.split("\n")
        for i, line in enumerate(lines):
            if line.strip().startswith("max_framebuffers="):
                lines[i] = "max_framebuffers=0"
                break
        content = "\n".join(lines)
        changes.append("Framebuffers disabled")

    # 5. Disable Bluetooth
    if "dtoverlay=disable-bt" in content:
        print("  [OK] Bluetooth already disabled")
    else:
        content += "\ndtoverlay=disable-bt\n"
        changes.append("Bluetooth disabled")

    # Write config if changed
    if changes:
        print()
        for change in changes:
            print(f"  [APPLY] {change}")

        # Backup and write
        backup_path = str(config_path) + ".bak"
        result = subprocess.run(
            ["sudo", "cp", str(config_path), backup_path],
            capture_output=True,
        )
        if result.returncode == 0:
            print(f"  Backup saved to {backup_path}")

        # Write via temp file + sudo mv
        temp_path = Path("/tmp/config.txt.setup")
        temp_path.write_text(content)
        result = subprocess.run(
            ["sudo", "cp", str(temp_path), str(config_path)],
            capture_output=True,
        )
        temp_path.unlink(missing_ok=True)

        if result.returncode != 0:
            print("  Failed to write config.txt (needs sudo)")
            return confirm("Continue anyway?", default=True)

        print("  Config saved!")
    else:
        print()
        print("  Config already optimized, no changes needed.")

    # 6. Disable unnecessary services
    print()
    print("Disabling unnecessary services...")

    for service in ["ModemManager", "polkit"]:
        # Check if service exists
        check = subprocess.run(
            ["systemctl", "list-unit-files", f"{service}.service"],
            capture_output=True, text=True,
        )
        if service not in check.stdout:
            print(f"  [SKIP] {service} not installed")
            continue

        # Check if already masked
        status = subprocess.run(
            ["systemctl", "is-enabled", f"{service}.service"],
            capture_output=True, text=True,
        )
        if "masked" in status.stdout:
            print(f"  [OK] {service} already disabled")
            continue

        subprocess.run(
            ["sudo", "systemctl", "disable", "--now", f"{service}.service"],
            capture_output=True,
        )
        subprocess.run(
            ["sudo", "systemctl", "mask", f"{service}.service"],
            capture_output=True,
        )
        print(f"  [APPLY] {service} disabled and masked")

    print()
    if changes:
        print("NOTE: Boot config changes take effect after reboot.")
        print("      A reboot will be needed after setup completes.")

    print()
    return True


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

    # Define checks: (name, check_function, apt_package, pip_package, install_instruction)
    checks = [
        ("camera_auto_detect=1", check_camera_config(), None, None, None),
        ("rpicam-still", shutil.which("rpicam-still") is not None, "rpicam-apps", None, None),
        ("ffmpeg", shutil.which("ffmpeg") is not None, "ffmpeg", None, None),
        ("TailScale", shutil.which("tailscale") is not None, None, None, "curl -fsSL https://tailscale.com/install.sh | sh"),
        ("cifs-utils", Path("/sbin/mount.cifs").exists(), "cifs-utils", None, None),
        ("smbclient", shutil.which("smbclient") is not None, "smbclient", None, None),
        ("simple-term-menu", HAS_MENU, None, "simple-term-menu", None),
    ]

    missing = []
    for name, ok, apt_pkg, pip_pkg, custom_install in checks:
        status = "[OK]" if ok else "[MISSING]"
        print(f"  {status} {name}")
        if not ok:
            missing.append((name, apt_pkg, pip_pkg, custom_install))

    print()

    if not missing:
        print("All prerequisites satisfied!")
    else:
        # Collect apt packages that can be auto-installed
        apt_packages = [pkg for name, pkg, _, _ in missing if pkg]
        pip_packages = [pkg for name, _, pkg, _ in missing if pkg]
        custom_installs = [(name, cmd) for name, _, _, cmd in missing if cmd]
        config_missing = any(name == "camera_auto_detect=1" for name, _, _, _ in missing)

        # Offer to auto-install apt packages
        if apt_packages:
            print(f"Missing apt packages: {', '.join(apt_packages)}")
            if confirm("Install missing apt packages automatically?", default=True):
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
                    print("  Apt packages installed successfully!")
            else:
                print()
                print("To install manually:")
                print(f"  sudo apt install -y {' '.join(apt_packages)}")
                if not confirm("Continue anyway?", default=False):
                    return False

        # Offer to auto-install pip packages
        if pip_packages:
            print()
            print(f"Missing Python packages: {', '.join(pip_packages)}")
            if confirm("Install missing Python packages automatically?", default=True):
                for pkg in pip_packages:
                    print(f"  Installing {pkg}...")
                    result = subprocess.run(
                        ["pip3", "install", "--break-system-packages", pkg],
                        capture_output=True,
                    )
                    if result.returncode != 0:
                        print(f"  Failed to install {pkg}. Install manually:")
                        print(f"    pip3 install --break-system-packages {pkg}")
                        if not confirm("Continue anyway?", default=False):
                            return False
                    else:
                        print(f"  {pkg} installed successfully!")
            else:
                print()
                print("To install manually:")
                for pkg in pip_packages:
                    print(f"  pip3 install --break-system-packages {pkg}")
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
    """Configure Prusa Connect and PrusaLink credentials."""
    print_header("Prusa Connect & PrusaLink Setup")

    print("You need credentials from two sources:")
    print()
    print("FROM PRUSA CONNECT (connect.prusa3d.com):")
    print("  1. Printer UUID - Found in the URL when viewing your printer")
    print("     Example: https://connect.prusa3d.com/printers/YOUR-UUID-HERE")
    print()
    print("  2. Camera Token - Generate from 'Add Camera' on your printer page")
    print("     (20 character token)")
    print()
    print("FROM YOUR PRINTER (Settings > Network > PrusaLink):")
    print("  3. API Key - The PrusaLink API key shown on your printer")
    print("     (Used for auto-detecting print start/stop)")
    print()
    print("  4. Printer IP - Your printer's local IP address")
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

    # PrusaLink API Key
    current_api_key = config.api_key
    api_key = prompt("PrusaLink API Key", current_api_key)
    if not api_key:
        print("PrusaLink API key is required for auto-detection.")
        return False
    config.set("prusa", "api_key", api_key)

    # Printer Local IP (for PrusaLink API)
    print()
    print("4. Printer Local IP Address")
    print("   Find this on your printer: Settings > Network > PrusaLink")
    print("   Or check your router for the printer's IP address")
    current_ip = config.printer_ip
    printer_ip = prompt("Printer IP address", current_ip)
    if not printer_ip:
        print("Printer IP is required for auto-detection.")
        return False
    config.set("prusa", "printer_ip", printer_ip)

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

    # Test PrusaLink API connection
    print("Testing PrusaLink API connection...")
    printer = PrinterStatus(printer_ip, api_key)
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

    # Share path - try interactive browse first (automatically)
    print()
    print("=" * 50)
    print("  NAS FOLDER SELECTION")
    print("=" * 50)
    print()

    share_path = None

    # Always try interactive browse first
    print("Attempting to browse NAS folders...")
    share_path = browse_smb_interactive(nas_ip, username, password)

    if share_path:
        print()
        print(f"  Selected path: {share_path}")
    else:
        # Interactive browse failed, fall back to manual entry
        print()
        print("Falling back to manual entry...")
        print()
        print("-" * 50)
        print("MANUAL SMB PATH ENTRY")
        print("-" * 50)
        print()
        print("The path you enter is the SHARE NAME on your NAS, NOT a local path.")
        print()
        print("WRONG: /mnt/storage/youtube-videos  (this is a local TrueNAS path)")
        print("RIGHT: storage/youtube-videos       (share name + subfolder)")
        print()
        print("In TrueNAS:")
        print("  - Go to Shares > Windows Shares (SMB)")
        print("  - Look at the 'Name' column - that's your share name")
        print("  - Then add any subfolder path after it")
        print()
        current_share = config.nas_share
        while True:
            share_path = prompt("SMB share path", current_share)
            # Warn if it looks like a local path
            if share_path.startswith("/mnt") or share_path.startswith("/home"):
                print()
                print("  *** ERROR: This looks like a local path! ***")
                print("  You entered something starting with /mnt or /home")
                print("  The SMB path should be: share_name/subfolder")
                print("  NOT: /mnt/pool/share_name/subfolder")
                print()
                if not confirm("  Are you SURE this is correct?", default=False):
                    continue
            break

    # Remove leading slash if present
    share_path = share_path.lstrip("/")
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


def setup_video_settings(config: Config) -> bool:
    """Configure automatic video creation settings."""
    print_header("Video Creation Settings")

    # Check FFmpeg is installed
    if not shutil.which("ffmpeg"):
        print("FFmpeg is not installed. Video creation will not work.")
        print("Install with: sudo apt install -y ffmpeg")
        if not confirm("Continue anyway?", default=False):
            return False

    print("Configure automatic timelapse video creation.")
    print("Videos are created after each print completes.")
    print()

    # Enable/disable toggle
    current_enabled = config.video_enabled
    default_str = "yes" if current_enabled else "no"
    enabled = confirm("Enable automatic video creation?", default=current_enabled)
    config.set("video", "enabled", "true" if enabled else "false")

    if not enabled:
        print("Video creation disabled.")
        return True

    # Frame rate
    current = config.video_frame_rate
    print()
    print("Frame rate determines video playback speed.")
    print("Higher = faster playthrough, lower = slower.")
    frame_rate = prompt_int("Video frame rate (1-60 FPS)", current)
    frame_rate = max(1, min(frame_rate, 60))
    config.set("video", "frame_rate", str(frame_rate))

    # Rotation
    print()
    print("Camera rotation to apply to video:")
    print("  0   - No rotation")
    print("  90  - Rotate 90 degrees clockwise")
    print("  180 - Rotate 180 degrees (upside down correction)")
    print("  270 - Rotate 270 degrees clockwise")
    current = config.video_rotation
    while True:
        rotation = prompt_int("Rotation degrees", current)
        if rotation in (0, 90, 180, 270):
            break
        print("Please enter 0, 90, 180, or 270")
    config.set("video", "rotation", str(rotation))

    # Advanced settings
    if confirm("\nConfigure advanced video settings?", default=False):
        # CRF (quality)
        print()
        print("CRF (Constant Rate Factor) controls quality:")
        print("  0 = Lossless, 18 = High quality, 23 = Default, 28 = Low quality")
        print("  Lower = better quality but larger files")
        current = config.video_crf
        crf = prompt_int("CRF value (0-51)", current)
        crf = max(0, min(crf, 51))
        config.set("video", "crf", str(crf))

    return True


def install_services(config: Config) -> bool:
    """Install systemd services."""
    print_header("Installing Services")

    install_dir = Path(__file__).parent.resolve()
    user = os.environ.get("USER", "pi")

    # Read and customize service templates
    templates_dir = install_dir / "templates"

    services = [
        ("prusacam.service", "Camera upload service"),
        ("timelapse-monitor.service", "Timelapse monitor service"),
        ("video-processor.service", "Video processor service"),
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

    # Configure NAS mount in /etc/fstab (more reliable than systemd mount units)
    print("Configuring NAS auto-mount in /etc/fstab...")
    nas = NASMount(
        config.nas_ip,
        config.nas_share,
        config.nas_mount_point,
        config.nas_username,
    )

    ok, error = nas.add_to_fstab()
    if ok:
        print("  NAS mount configured in /etc/fstab")
    else:
        print(f"  Warning: Failed to configure fstab: {error}")
        print("  NAS may need to be mounted manually")

    # Reload systemd and enable services
    print("Enabling services...")
    subprocess.run(["sudo", "systemctl", "daemon-reload"], capture_output=True)

    subprocess.run(
        ["sudo", "systemctl", "enable", "prusacam.service"],
        capture_output=True,
    )
    subprocess.run(
        ["sudo", "systemctl", "enable", "timelapse-monitor.service"],
        capture_output=True,
    )
    subprocess.run(
        ["sudo", "systemctl", "enable", "video-processor.service"],
        capture_output=True,
    )

    # Mount NAS if not already mounted
    if not nas.is_mounted():
        print("Mounting NAS...")
        ok, error = nas.mount()
        if ok:
            print("  NAS mounted successfully")
        else:
            print(f"  Warning: NAS mount failed: {error}")

    # Restart services (restart ensures new code is loaded even if already running)
    print("Starting services...")
    subprocess.run(
        ["sudo", "systemctl", "restart", "prusacam.service"],
        capture_output=True,
    )
    subprocess.run(
        ["sudo", "systemctl", "restart", "timelapse-monitor.service"],
        capture_output=True,
    )
    subprocess.run(
        ["sudo", "systemctl", "restart", "video-processor.service"],
        capture_output=True,
    )

    print()
    print("Services installed and started!")
    print()
    print("Check status with:")
    print("  systemctl status prusacam")
    print("  systemctl status timelapse-monitor")
    print("  systemctl status video-processor")
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

    # Step 2: Memory Optimization
    if not optimize_memory():
        print("Setup cancelled.")
        sys.exit(1)

    # Step 3: Prusa Connect
    if not setup_prusa_connect(config):
        print("Setup cancelled.")
        sys.exit(1)

    # Save config after Prusa setup
    config.save()
    print("Configuration saved.")

    # Step 4: NAS Storage
    if not setup_nas(config):
        print("Setup cancelled.")
        sys.exit(1)

    # Save config after NAS setup
    config.save()

    # Step 5: Camera Settings (optional)
    if confirm("Configure advanced camera settings?", default=False):
        setup_camera_settings(config)

    # Step 6: Video Settings
    if confirm("Configure video creation settings?", default=True):
        setup_video_settings(config)

    # Save final config
    config.save()
    print("Configuration saved to ~/.prusa_camera_config")

    # Step 7: Install Services
    if confirm("Install and start systemd services?", default=True):
        if not install_services(config):
            print("Service installation had issues. Check manually.")

    print_header("Setup Complete!")

    print("Your Prusa camera is now configured!")
    print()
    print("What happens now:")
    print("  1. Camera uploads snapshots to Prusa Connect every",
          f"{config.upload_interval}s")
    print("  2. When you start a print, timelapse frames are captured automatically")
    print("  3. Frames are saved to NAS as JPEGs")
    if config.video_enabled:
        print("  4. After print completes, an MP4 video is created automatically")
    print()
    print("Files will be saved to:")
    print(f"  {config.nas_mount_point}/<session>/frames/  (JPEG frames)")
    if config.video_enabled:
        print(f"  {config.nas_mount_point}/<session>/<session>.mp4  (video)")
    print()
    print("Manual timelapse control:")
    print("  Start: echo 'my_print' > ~/.timelapse_recording")
    print("  Stop:  rm ~/.timelapse_recording")
    print()
    print("View logs:")
    print("  journalctl -u prusacam -f")
    print("  journalctl -u timelapse-monitor -f")
    print("  journalctl -u video-processor -f")
    print()


if __name__ == "__main__":
    main()
