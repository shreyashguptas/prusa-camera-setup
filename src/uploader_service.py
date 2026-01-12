"""Camera upload service - continuously captures and uploads to Prusa Connect."""

import sys
import time

from .config import Config
from .camera import Camera
from .uploader import PrusaConnectUploader


def main():
    """Run the camera upload service."""
    config = Config()
    if not config.load():
        print("Configuration not found. Run setup.py first.")
        sys.exit(1)

    if not config.camera_token:
        print("Camera token not configured. Run setup.py first.")
        sys.exit(1)

    camera = Camera(
        width=config.camera_width,
        height=config.camera_height,
        quality=config.camera_quality,
    )

    uploader = PrusaConnectUploader(
        camera_token=config.camera_token,
        fingerprint=f"prusacam-{config.printer_uuid[:8]}",
    )

    print("=== Prusa Camera Upload Service ===")
    print(f"Upload interval: {config.upload_interval}s")
    print(f"Resolution: {config.camera_width}x{config.camera_height}")
    print()

    consecutive_failures = 0
    max_failures = 5

    while True:
        try:
            # Capture image
            snapshot = camera.capture()

            if snapshot:
                # Upload to Prusa Connect
                if uploader.upload(snapshot):
                    consecutive_failures = 0
                else:
                    consecutive_failures += 1
                    print(f"Upload failed ({consecutive_failures}/{max_failures})")
            else:
                consecutive_failures += 1
                print(f"Capture failed ({consecutive_failures}/{max_failures})")

            # Back off if too many failures
            if consecutive_failures >= max_failures:
                print("Too many failures, waiting 60s...")
                time.sleep(60)
                consecutive_failures = 0
            else:
                time.sleep(config.upload_interval)

        except KeyboardInterrupt:
            print("\nStopping upload service...")
            break
        except Exception as e:
            print(f"Error: {e}")
            time.sleep(30)


if __name__ == "__main__":
    main()
