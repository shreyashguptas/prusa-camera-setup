"""Timelapse recording and video creation with auto-detection."""

import os
import sys
import time
import shutil
import subprocess
from pathlib import Path
from datetime import datetime
from typing import Optional

from .config import Config
from .camera import Camera
from .printer import PrinterStatus


class TimelapseManager:
    """Manages timelapse recording with auto-detection via Prusa Connect API."""

    CONTROL_FILE = Path.home() / ".timelapse_recording"

    def __init__(self, config: Config):
        self.config = config
        self.camera = Camera(
            width=config.camera_width,
            height=config.camera_height,
            quality=config.camera_quality,
        )
        self.printer = PrinterStatus(
            printer_ip=config.printer_ip,
            api_key=config.api_key,
        )
        self.storage_path = Path(config.nas_mount_point)

    def _get_session_name(self) -> str:
        """Generate a session name from current timestamp."""
        return datetime.now().strftime("print_%Y%m%d_%H%M%S")

    def _get_active_session(self) -> Optional[str]:
        """Get the currently active session name from control file."""
        if self.CONTROL_FILE.exists():
            name = self.CONTROL_FILE.read_text().strip()
            return name if name else None
        return None

    def start_recording(self, name: Optional[str] = None) -> str:
        """
        Start a new timelapse recording session.

        Args:
            name: Optional session name, auto-generated if not provided.

        Returns:
            Session name.
        """
        session_name = name or self._get_session_name()
        session_dir = self.storage_path / session_name / "frames"
        session_dir.mkdir(parents=True, exist_ok=True)

        self.CONTROL_FILE.write_text(session_name)
        return session_name

    def stop_recording(self) -> Optional[str]:
        """
        Stop the current recording session.

        Returns:
            Session name that was stopped, or None if not recording.
        """
        if not self.CONTROL_FILE.exists():
            return None

        session_name = self.CONTROL_FILE.read_text().strip()
        self.CONTROL_FILE.unlink()
        return session_name

    def is_recording(self) -> bool:
        """Check if currently recording."""
        return self.CONTROL_FILE.exists()

    def capture_frame(self, session_name: str, frame_number: int) -> bool:
        """
        Capture a frame for the timelapse.

        Args:
            session_name: Current session name
            frame_number: Frame sequence number

        Returns:
            True if capture successful.
        """
        frames_dir = self.storage_path / session_name / "frames"
        if not frames_dir.exists():
            frames_dir.mkdir(parents=True, exist_ok=True)

        # Capture to temp location first
        snapshot = self.camera.capture()
        if not snapshot:
            return False

        # Copy to NAS
        frame_path = frames_dir / f"frame_{frame_number:06d}.jpg"
        try:
            shutil.copy2(snapshot, frame_path)
            return True
        except Exception:
            return False

    def create_video(self, session_name: str) -> Optional[Path]:
        """
        Create timelapse video from captured frames.

        Args:
            session_name: Session name

        Returns:
            Path to created video or None on failure.
        """
        session_dir = self.storage_path / session_name
        frames_dir = session_dir / "frames"
        output_file = session_dir / f"{session_name}.mp4"

        if not frames_dir.exists():
            return None

        frame_count = len(list(frames_dir.glob("frame_*.jpg")))
        if frame_count == 0:
            return None

        cmd = [
            "ffmpeg", "-y",
            "-framerate", str(self.config.video_fps),
            "-pattern_type", "glob",
            "-i", str(frames_dir / "frame_*.jpg"),
            "-c:v", "libx264",
            "-preset", "medium",
            "-crf", str(self.config.video_quality),
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            str(output_file),
        ]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=600,
            )

            if result.returncode == 0 and output_file.exists():
                return output_file
            return None

        except subprocess.TimeoutExpired:
            return None
        except Exception:
            return None

    def run_monitor(self, check_interval: int = 30):
        """
        Run the timelapse monitor loop with auto-detection.

        Args:
            check_interval: Seconds between printer status checks
        """
        print("=== Prusa Timelapse Monitor ===")
        print(f"Storage: {self.storage_path}")
        print(f"Capture interval: {self.config.capture_interval}s")
        print(f"Auto-detect: Enabled (checking every {check_interval}s)")
        print()
        print("Manual control:")
        print(f"  Start: echo 'name' > {self.CONTROL_FILE}")
        print(f"  Stop:  rm {self.CONTROL_FILE}")
        print()

        current_session = None
        current_job_id = None
        frame_count = 0
        last_capture = 0

        while True:
            try:
                # Check for manual override
                manual_session = self._get_active_session()

                # Check printer status
                status = self.printer.get_status()
                is_printing = status.is_printing if status else False
                job_id = status.job_id if status else None

                # Determine if we should be recording
                should_record = is_printing or manual_session is not None

                # Detect job ID change (new print started while already recording)
                if current_session and current_job_id and job_id and job_id != current_job_id:
                    print(f"\nJob changed ({current_job_id} -> {job_id}), finalizing previous recording...")
                    print(f"Recording stopped: {current_session}")
                    print("Creating video...")
                    video_path = self.create_video(current_session)
                    if video_path:
                        print(f"Video created: {video_path}")
                    else:
                        print("Video creation failed")
                    # Reset for new recording
                    current_session = None
                    current_job_id = None
                    frame_count = 0

                # Handle state transitions
                if should_record and current_session is None:
                    # Start recording
                    if manual_session:
                        current_session = manual_session
                        current_job_id = None  # Manual recordings don't track job ID
                        print(f"Recording started (manual): {current_session}")
                    else:
                        job_name = status.job_name if status else None
                        if job_name:
                            # Clean job name for filename
                            safe_name = "".join(c if c.isalnum() or c in "._-" else "_" for c in job_name)
                            current_session = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{safe_name}"
                        else:
                            current_session = self._get_session_name()
                        current_job_id = job_id  # Track job ID for this recording
                        self.start_recording(current_session)
                        print(f"Recording started (auto, job {job_id}): {current_session}")

                    frame_count = 0
                    last_capture = 0

                elif not should_record and current_session is not None:
                    # Stop recording
                    print(f"Recording stopped: {current_session}")
                    print("Creating video...")

                    video_path = self.create_video(current_session)
                    if video_path:
                        print(f"Video created: {video_path}")
                    else:
                        print("Video creation failed")

                    current_session = None
                    current_job_id = None
                    frame_count = 0

                # Capture frames if recording
                if current_session:
                    now = time.time()
                    if now - last_capture >= self.config.capture_interval:
                        if self.capture_frame(current_session, frame_count):
                            frame_count += 1
                            print(f"Frame {frame_count} captured", end="\r", flush=True)
                        last_capture = now

                time.sleep(min(check_interval, self.config.capture_interval))

            except KeyboardInterrupt:
                print("\n\nStopping monitor...")
                if current_session:
                    print(f"Creating video for {current_session}...")
                    self.stop_recording()
                    video_path = self.create_video(current_session)
                    if video_path:
                        print(f"Video created: {video_path}")
                break
            except Exception as e:
                print(f"\nError: {e}")
                time.sleep(60)


def main():
    """Entry point for timelapse monitor."""
    config = Config()
    if not config.load():
        print("Configuration not found. Run setup.py first.")
        sys.exit(1)

    if not config.is_configured():
        print("Configuration incomplete. Run setup.py first.")
        sys.exit(1)

    manager = TimelapseManager(config)
    manager.run_monitor()


if __name__ == "__main__":
    main()
