"""NAS/SMB mount handling for timelapse storage."""

import os
import subprocess
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

    def get_systemd_mount_unit(self) -> str:
        """Generate systemd mount unit content."""
        # Convert mount point to systemd unit name
        unit_name = str(self.mount_point).replace("/", "-").lstrip("-")

        return f"""[Unit]
Description=NAS Mount for Prusa Timelapse Storage
After=network-online.target tailscaled.service
Wants=network-online.target

[Mount]
What={self.smb_path}
Where={self.mount_point}
Type=cifs
Options=credentials={self.CREDENTIALS_PATH},uid=1000,gid=1000,file_mode=0664,dir_mode=0775,_netdev

[Install]
WantedBy=multi-user.target
"""

    def get_systemd_automount_unit(self) -> str:
        """Generate systemd automount unit content."""
        return f"""[Unit]
Description=Automount for NAS Prusa Timelapse Storage
After=network-online.target

[Automount]
Where={self.mount_point}
TimeoutIdleSec=0

[Install]
WantedBy=multi-user.target
"""
