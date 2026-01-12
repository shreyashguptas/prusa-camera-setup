"""Prusa Connect camera upload functionality."""

import requests
from pathlib import Path
from typing import Optional


class PrusaConnectUploader:
    """Uploads camera snapshots to Prusa Connect."""

    UPLOAD_URL = "https://webcam.connect.prusa3d.com/c/snapshot"

    def __init__(self, camera_token: str, fingerprint: str = "prusa-camera-pi"):
        """
        Initialize the uploader.

        Args:
            camera_token: Camera token from Prusa Connect (20 characters)
            fingerprint: Unique identifier for this camera (>=16 chars)
        """
        self.camera_token = camera_token
        self.fingerprint = fingerprint if len(fingerprint) >= 16 else fingerprint + "0" * (16 - len(fingerprint))

    def upload(self, image_path: Path, timeout: int = 30) -> bool:
        """
        Upload an image to Prusa Connect.

        Args:
            image_path: Path to the JPEG image
            timeout: Request timeout in seconds

        Returns:
            True if upload successful, False otherwise.
        """
        if not image_path.exists():
            return False

        headers = {
            "Content-Type": "image/jpg",
            "Token": self.camera_token,
            "Fingerprint": self.fingerprint,
        }

        try:
            with open(image_path, "rb") as f:
                response = requests.put(
                    self.UPLOAD_URL,
                    headers=headers,
                    data=f.read(),
                    timeout=timeout,
                )
            return response.status_code in (200, 204)
        except requests.RequestException:
            return False

    def test_connection(self) -> tuple[bool, Optional[str]]:
        """
        Test the connection to Prusa Connect.

        Returns:
            Tuple of (success, error_message)
        """
        try:
            headers = {
                "Content-Type": "image/jpg",
                "Token": self.camera_token,
                "Fingerprint": self.fingerprint,
            }
            response = requests.put(
                self.UPLOAD_URL,
                headers=headers,
                data=b"",  # Empty test
                timeout=10,
            )
            if response.status_code in (200, 204, 400):
                return True, None
            return False, f"HTTP {response.status_code}"
        except requests.RequestException as e:
            return False, str(e)
