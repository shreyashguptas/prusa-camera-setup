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

    def upload(self, image_path: Path, timeout: int = 30) -> tuple[bool, Optional[str]]:
        """
        Upload an image to Prusa Connect.

        Args:
            image_path: Path to the JPEG image
            timeout: Request timeout in seconds

        Returns:
            Tuple of (success, error_message). error_message is None on success.
        """
        if not image_path.exists():
            return False, "Image file not found"

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
            if response.status_code in (200, 204):
                return True, None
            error_detail = ""
            try:
                error_detail = response.json().get("detail", response.text[:100])
            except Exception:
                error_detail = response.text[:100] if response.text else ""
            return False, f"HTTP {response.status_code}: {error_detail}".strip()
        except requests.Timeout:
            return False, "Request timed out"
        except requests.ConnectionError:
            return False, "Connection failed"
        except requests.RequestException as e:
            return False, str(e)

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
