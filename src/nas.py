"""NAS/SMB mount handling for timelapse storage."""

import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Optional


class NASMount:
    """Handles NAS/SMB mounting for timelapse storage."""

    CREDENTIALS_PATH = Path("/etc/smbcredentials")

    def __init__(
        self,
        nas_ip: str,
        share_path: str,
        mount_point: str,
        username: str,
    ):
        """
        Initialize NAS mount handler.

        Args:
            nas_ip: NAS IP address (e.g., TailScale IP)
            share_path: SMB share path (e.g., storage/youtube-videos/printer-footage)
            mount_point: Local mount point (e.g., /mnt/nas/printer-footage)
            username: SMB username
        """
        self.nas_ip = nas_ip
        self.share_path = share_path.lstrip("/")
        self.mount_point = Path(mount_point)
        self.username = username

    @property
    def smb_path(self) -> str:
        """Get the full SMB path."""
        return f"//{self.nas_ip}/{self.share_path}"

    def setup_credentials(self, password: str) -> bool:
        """
        Create SMB credentials file (requires sudo).

        Args:
            password: SMB password

        Returns:
            True if successful.
        """
        credentials_content = f"username={self.username}\npassword={password}\n"

        try:
            # Write to temp file first, then move with sudo
            temp_path = Path("/tmp/smbcredentials_temp")
            temp_path.write_text(credentials_content)

            # Move to /etc with sudo and set permissions
            result = subprocess.run(
                ["sudo", "mv", str(temp_path), str(self.CREDENTIALS_PATH)],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                return False

            result = subprocess.run(
                ["sudo", "chmod", "600", str(self.CREDENTIALS_PATH)],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                return False

            result = subprocess.run(
                ["sudo", "chown", "root:root", str(self.CREDENTIALS_PATH)],
                capture_output=True,
                text=True,
            )
            return result.returncode == 0

        except Exception:
            return False

    def create_mount_point(self) -> bool:
        """Create the mount point directory (requires sudo)."""
        try:
            result = subprocess.run(
                ["sudo", "mkdir", "-p", str(self.mount_point)],
                capture_output=True,
                text=True,
            )
            return result.returncode == 0
        except Exception:
            return False

    def mount(self) -> tuple[bool, Optional[str]]:
        """
        Mount the NAS share.

        Returns:
            Tuple of (success, error_message)
        """
        # Check if already mounted - return success to avoid stacked mounts
        if self.is_mounted():
            return True, None

        if not self.CREDENTIALS_PATH.exists():
            return False, "Credentials file not found"

        if not self.mount_point.exists():
            self.create_mount_point()

        cmd = [
            "sudo", "mount", "-t", "cifs",
            self.smb_path,
            str(self.mount_point),
            "-o", f"credentials={self.CREDENTIALS_PATH},uid=1000,gid=1000,file_mode=0664,dir_mode=0775"
        ]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
            )

            if result.returncode == 0:
                return True, None
            return False, result.stderr.strip() or "Mount failed"

        except subprocess.TimeoutExpired:
            return False, "Mount timed out"
        except Exception as e:
            return False, str(e)

    def unmount(self) -> bool:
        """Unmount the NAS share."""
        try:
            result = subprocess.run(
                ["sudo", "umount", str(self.mount_point)],
                capture_output=True,
                text=True,
            )
            return result.returncode == 0
        except Exception:
            return False

    def is_mounted(self) -> bool:
        """Check if the NAS is currently mounted."""
        try:
            result = subprocess.run(
                ["mountpoint", "-q", str(self.mount_point)],
                capture_output=True,
            )
            return result.returncode == 0
        except Exception:
            return False

    def is_healthy(self, timeout: int = 5) -> bool:
        """Check if the NAS mount is actually accessible (not stale).

        A CIFS mount can appear mounted but be stale (returning OSError
        errno 19 'No such device' on access). This does a real stat check
        with a timeout to prevent blocking on hung CIFS mounts.
        """
        def _timeout_handler(signum, frame):
            raise OSError("NAS health check timed out")

        old_handler = signal.signal(signal.SIGALRM, _timeout_handler)
        signal.alarm(timeout)
        try:
            os.stat(str(self.mount_point))
            return True
        except OSError:
            return False
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old_handler)

    def try_remount(self) -> bool:
        """Attempt to recover a stale NAS mount.

        Lazy-unmounts the stale mount, then remounts. Returns True if
        the mount is healthy after the attempt.
        """
        if self.is_healthy():
            return True

        print(f"NAS mount stale at {self.mount_point}, attempting remount...")

        # Lazy unmount to clear the stale mount
        try:
            subprocess.run(
                ["sudo", "umount", "-l", str(self.mount_point)],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except Exception:
            pass  # May fail if not mounted, that's OK

        # Brief pause for mount cleanup
        time.sleep(2)

        # Try to mount via fstab entry
        try:
            result = subprocess.run(
                ["sudo", "mount", str(self.mount_point)],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0 and self.is_healthy():
                print("NAS remount successful")
                return True
            else:
                error = result.stderr.strip() if result.stderr else "unknown error"
                print(f"NAS remount failed: {error}")
                return False
        except subprocess.TimeoutExpired:
            print("NAS remount timed out")
            return False
        except Exception as e:
            print(f"NAS remount error: {e}")
            return False

    def ensure_mounted(self) -> bool:
        """Ensure the NAS is mounted and accessible. Attempts remount if stale.

        Returns True if the NAS is accessible after this call.
        """
        if self.is_healthy():
            return True
        return self.try_remount()

    def add_to_fstab(self) -> tuple[bool, Optional[str]]:
        """
        Add NAS mount to /etc/fstab for automatic mounting on boot.

        Returns:
            Tuple of (success, error_message)
        """
        fstab_path = Path("/etc/fstab")
        fstab_entry = (
            f"{self.smb_path} {self.mount_point} cifs "
            f"credentials={self.CREDENTIALS_PATH},uid=1000,gid=1000,_netdev,x-systemd.automount 0 0"
        )

        try:
            # Check if already in fstab
            if fstab_path.exists():
                current_fstab = fstab_path.read_text()
                if self.smb_path in current_fstab:
                    return True, None  # Already configured

            # Append to fstab using sudo
            result = subprocess.run(
                ["sudo", "bash", "-c", f"echo '{fstab_entry}' >> /etc/fstab"],
                capture_output=True,
                text=True,
            )

            if result.returncode != 0:
                return False, result.stderr.strip() or "Failed to update fstab"

            # Reload systemd to pick up the automount
            subprocess.run(
                ["sudo", "systemctl", "daemon-reload"],
                capture_output=True,
            )

            return True, None

        except Exception as e:
            return False, str(e)

    def test_connection(self) -> tuple[bool, Optional[str]]:
        """
        Test connectivity to NAS (ping).

        Returns:
            Tuple of (success, error_message)
        """
        try:
            result = subprocess.run(
                ["ping", "-c", "1", "-W", "3", self.nas_ip],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                return True, None
            return False, "NAS not reachable"
        except Exception as e:
            return False, str(e)

