"""Camera capture functionality using rpicam-still."""

import subprocess
import shutil
from pathlib import Path
from typing import Optional


class Camera:
    """Handles camera capture using rpicam-still."""

    SNAPSHOT_PATH = Path("/tmp/snapshot.jpg")

    def __init__(self, width: int = 1704, height: int = 1278, quality: int = 85):
        self.width = width
        self.height = height
        self.quality = quality

    def is_available(self) -> bool:
        """Check if rpicam-still is available."""
        return shutil.which("rpicam-still") is not None

    def capture(self, output_path: Optional[Path] = None) -> Optional[Path]:
        """
        Capture an image using rpicam-still.

        Args:
            output_path: Where to save the image. Defaults to /tmp/snapshot.jpg

        Returns:
            Path to captured image, or None on failure.
        """
        output = output_path or self.SNAPSHOT_PATH

        cmd = [
            "rpicam-still",
            "-v", "0",
            "--immediate",
            "--nopreview",
            "--width", str(self.width),
            "--height", str(self.height),
            "-q", str(self.quality),
            "-o", str(output),
        ]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30
            )
            if result.returncode == 0 and output.exists():
                return output
            return None
        except subprocess.TimeoutExpired:
            return None
        except Exception:
            return None

    def get_snapshot_path(self) -> Path:
        """Get the default snapshot path."""
        return self.SNAPSHOT_PATH
