"""Configuration management for Prusa Camera Setup."""

import os
import configparser
from pathlib import Path
from typing import Optional


class Config:
    """Manages configuration stored in ~/.prusa_camera_config."""

    DEFAULT_CONFIG_PATH = Path.home() / ".prusa_camera_config"

    DEFAULTS = {
        "prusa": {
            "printer_uuid": "",
            "camera_token": "",
            "api_key": "",
            "printer_ip": "",
        },
        "nas": {
            "ip": "",
            "share": "",
            "mount_point": "/mnt/nas/printer-footage",
            "username": "",
        },
        "timelapse": {
            "capture_interval": "30",
            "video_fps": "30",
            "video_quality": "20",
        },
        "camera": {
            "width": "1704",
            "height": "1278",
            "quality": "85",
            "upload_interval": "12",
        },
    }

    def __init__(self, config_path: Optional[Path] = None):
        self.config_path = config_path or self.DEFAULT_CONFIG_PATH
        self.config = configparser.ConfigParser()
        self._load_defaults()

    def _load_defaults(self):
        """Load default configuration values."""
        for section, values in self.DEFAULTS.items():
            self.config[section] = values

    def load(self) -> bool:
        """Load configuration from file. Returns True if file exists."""
        if self.config_path.exists():
            self.config.read(self.config_path)
            return True
        return False

    def save(self):
        """Save configuration to file with secure permissions."""
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.config_path, "w") as f:
            self.config.write(f)
        os.chmod(self.config_path, 0o600)

    def get(self, section: str, key: str, fallback: str = "") -> str:
        """Get a configuration value."""
        return self.config.get(section, key, fallback=fallback)

    def set(self, section: str, key: str, value: str):
        """Set a configuration value."""
        if section not in self.config:
            self.config[section] = {}
        self.config[section][key] = value

    def get_int(self, section: str, key: str, fallback: int = 0) -> int:
        """Get a configuration value as integer."""
        try:
            return int(self.get(section, key, str(fallback)))
        except ValueError:
            return fallback

    @property
    def printer_uuid(self) -> str:
        return self.get("prusa", "printer_uuid")

    @property
    def camera_token(self) -> str:
        return self.get("prusa", "camera_token")

    @property
    def api_key(self) -> str:
        return self.get("prusa", "api_key")

    @property
    def printer_ip(self) -> str:
        return self.get("prusa", "printer_ip")

    @property
    def nas_ip(self) -> str:
        return self.get("nas", "ip")

    @property
    def nas_share(self) -> str:
        return self.get("nas", "share")

    @property
    def nas_mount_point(self) -> str:
        return self.get("nas", "mount_point")

    @property
    def nas_username(self) -> str:
        return self.get("nas", "username")

    @property
    def capture_interval(self) -> int:
        return self.get_int("timelapse", "capture_interval", 30)

    @property
    def video_fps(self) -> int:
        return self.get_int("timelapse", "video_fps", 30)

    @property
    def video_quality(self) -> int:
        return self.get_int("timelapse", "video_quality", 20)

    @property
    def camera_width(self) -> int:
        return self.get_int("camera", "width", 1704)

    @property
    def camera_height(self) -> int:
        return self.get_int("camera", "height", 1278)

    @property
    def camera_quality(self) -> int:
        return self.get_int("camera", "quality", 85)

    @property
    def upload_interval(self) -> int:
        return self.get_int("camera", "upload_interval", 12)

    def is_configured(self) -> bool:
        """Check if essential configuration is present."""
        return bool(
            self.printer_uuid
            and self.camera_token
            and self.api_key
            and self.printer_ip
            and self.nas_ip
            and self.nas_share
        )
