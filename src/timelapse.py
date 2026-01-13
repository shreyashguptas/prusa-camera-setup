"""Timelapse recording and video creation with auto-detection."""

import os
import sys
import time
import shutil
import signal
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

    def start_recording(self, name: Optional[str] = None, manual: bool = False) -> str:
        """
        Start a new timelapse recording session.

        Args:
            name: Optional session name, auto-generated if not provided.
            manual: If True, write to control file (for manual override sessions).
                    Auto-detected sessions should NOT write to control file.

        Returns:
            Session name.
        """
        session_name = name or self._get_session_name()
        session_dir = self.storage_path / session_name / "frames"
        session_dir.mkdir(parents=True, exist_ok=True)

        # Only write control file for manual sessions
        # Auto sessions rely on printer status - control file would cause infinite recording
        if manual:
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

    def _copy_with_timeout(self, src: Path, dst: Path, timeout: int = 30) -> bool:
        """
        Copy file with timeout to prevent hanging on slow/dead NAS.

        Args:
            src: Source file path
            dst: Destination file path
            timeout: Timeout in seconds (default 30)

        Returns:
            True if copy succeeded within timeout.
        """
        def timeout_handler(signum, frame):
            raise TimeoutError("File copy timed out")

        old_handler = signal.signal(signal.SIGALRM, timeout_handler)
        signal.alarm(timeout)
        try:
            shutil.copy2(src, dst)
            return True
        except TimeoutError:
            print(f"\nWarning: NAS transfer timed out for {dst.name}")
            return False
        except Exception as e:
            print(f"\nWarning: NAS transfer failed: {e}")
            return False
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old_handler)

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

        # Copy to NAS with timeout protection
        frame_path = frames_dir / f"frame_{frame_number:06d}.jpg"
        return self._copy_with_timeout(snapshot, frame_path, timeout=30)

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
            "-start_number", "0",
            "-i", str(frames_dir / "frame_%06d.jpg"),
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
            # Log FFMPEG error for debugging
            if result.stderr:
                print(f"FFMPEG error: {result.stderr}")
            return None

        except subprocess.TimeoutExpired:
            print("FFMPEG error: Timed out after 10 minutes")
            return None
        except Exception as e:
            print(f"FFMPEG error: {e}")
            return None

    def create_video_async(self, session_name: str) -> Optional[subprocess.Popen]:
        """
        Start video creation in background (non-blocking).

        Args:
            session_name: Session name

        Returns:
            Popen process handle or None on setup failure.
        """
        session_dir = self.storage_path / session_name
        frames_dir = session_dir / "frames"
        output_file = session_dir / f"{session_name}.mp4"

        if not frames_dir.exists():
            return None

        frame_count = len(list(frames_dir.glob("frame_*.jpg")))
        if frame_count == 0:
            return None

        # Use nice to lower FFMPEG priority so it doesn't starve frame capture
        cmd = [
            "nice", "-n", "10",
            "ffmpeg", "-y",
            "-framerate", str(self.config.video_fps),
            "-start_number", "0",
            "-i", str(frames_dir / "frame_%06d.jpg"),
            "-c:v", "libx264",
            "-preset", "medium",
            "-crf", str(self.config.video_quality),
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            str(output_file),
        ]

        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            return process
        except Exception as e:
            print(f"Failed to start video encoding: {e}")
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

        # Resilience: track background video encoding processes
        pending_videos: list[tuple[str, subprocess.Popen]] = []

        # Resilience: debounce printer status to avoid false stops
        not_printing_count = 0
        STOP_THRESHOLD = 3  # Require 3 consecutive "not printing" to stop

        # Resilience: track capture metrics
        capture_success = 0
        capture_failed = 0

        while True:
            try:
                # Check for completed background video encodings (non-blocking)
                for session, proc in pending_videos[:]:
                    if proc.poll() is not None:
                        if proc.returncode == 0:
                            output_file = self.storage_path / session / f"{session}.mp4"
                            print(f"\nVideo created: {output_file}")
                        else:
                            stderr = proc.stderr.read().decode() if proc.stderr else ""
                            print(f"\nVideo encoding failed for {session}: {stderr[:200]}")
                        pending_videos.remove((session, proc))

                # Check for manual override
                manual_session = self._get_active_session()

                # Check printer status
                status = self.printer.get_status()

                # Resilience: handle API failures gracefully
                if status is None:
                    print("Warning: Printer API unreachable", end="\r", flush=True)
                    time.sleep(check_interval)
                    continue

                is_printing = status.is_printing
                job_id = status.job_id

                # Determine if we should be recording
                should_record = is_printing or manual_session is not None

                # Resilience: debounce stop decision
                if not should_record and current_session and not manual_session:
                    not_printing_count += 1
                    if not_printing_count < STOP_THRESHOLD:
                        # Don't stop yet, keep capturing
                        now = time.time()
                        if now - last_capture >= self.config.capture_interval:
                            if self.capture_frame(current_session, frame_count):
                                frame_count += 1
                                capture_success += 1
                                print(f"Frame {frame_count} captured (debounce {not_printing_count}/{STOP_THRESHOLD})", end="\r", flush=True)
                            else:
                                capture_failed += 1
                            last_capture = now
                        time.sleep(min(check_interval, self.config.capture_interval))
                        continue
                else:
                    not_printing_count = 0

                # Detect job ID change (new print started while already recording)
                if current_session and current_job_id and job_id and job_id != current_job_id:
                    print(f"\nJob changed ({current_job_id} -> {job_id}), finalizing previous recording...")
                    print(f"Recording stopped: {current_session}")
                    # Start video encoding in background (non-blocking)
                    proc = self.create_video_async(current_session)
                    if proc:
                        print(f"Video encoding started in background: {current_session}")
                        pending_videos.append((current_session, proc))
                    else:
                        print("Video encoding failed to start (no frames?)")
                    # Reset for new recording immediately - don't wait for encoding
                    current_session = None
                    current_job_id = None
                    frame_count = 0

                # Handle state transitions
                if should_record and current_session is None:
                    # Start recording
                    if manual_session:
                        current_session = manual_session
                        current_job_id = None  # Manual recordings don't track job ID
                        # Create session directory (control file already exists from manual creation)
                        session_dir = self.storage_path / current_session / "frames"
                        session_dir.mkdir(parents=True, exist_ok=True)
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
                    print(f"\nRecording stopped: {current_session}")
                    # Log capture metrics for this session
                    total = capture_success + capture_failed
                    if total > 0:
                        rate = capture_success / total * 100
                        print(f"Session capture rate: {rate:.1f}% ({capture_success}/{total})")
                    # Start video encoding in background (non-blocking)
                    proc = self.create_video_async(current_session)
                    if proc:
                        print(f"Video encoding started in background: {current_session}")
                        pending_videos.append((current_session, proc))
                    else:
                        print("Video encoding failed to start (no frames?)")

                    current_session = None
                    current_job_id = None
                    frame_count = 0
                    capture_success = 0
                    capture_failed = 0

                # Capture frames if recording
                if current_session:
                    now = time.time()
                    if now - last_capture >= self.config.capture_interval:
                        if self.capture_frame(current_session, frame_count):
                            frame_count += 1
                            capture_success += 1
                            print(f"Frame {frame_count} captured", end="\r", flush=True)
                        else:
                            capture_failed += 1
                        last_capture = now

                        # Resilience: log capture rate every 100 attempts
                        total = capture_success + capture_failed
                        if total > 0 and total % 100 == 0:
                            rate = capture_success / total * 100
                            print(f"\nCapture rate: {rate:.1f}% ({capture_success}/{total})")

                time.sleep(min(check_interval, self.config.capture_interval))

            except KeyboardInterrupt:
                print("\n\nStopping monitor...")
                if current_session:
                    print(f"Creating video for {current_session}...")
                    self.stop_recording()
                    # Use blocking create_video for graceful shutdown
                    video_path = self.create_video(current_session)
                    if video_path:
                        print(f"Video created: {video_path}")
                # Wait for any pending background encodings
                if pending_videos:
                    print(f"Waiting for {len(pending_videos)} background encoding(s) to finish...")
                    for session, proc in pending_videos:
                        proc.wait()
                        if proc.returncode == 0:
                            print(f"Video created: {session}")
                        else:
                            print(f"Video failed: {session}")
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
