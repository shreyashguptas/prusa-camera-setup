"""Timelapse frame capture with auto-detection."""

import sys
import time
import shutil
import signal
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

    def _signal_session_complete(self, session_name: str):
        """Signal that session is ready for video processing."""
        session_path = self.storage_path / session_name
        ready_marker = session_path / "ready_for_video"
        try:
            ready_marker.touch()
            print(f"Session ready for video: {session_name}")
        except Exception as e:
            print(f"Warning: Could not create ready marker: {e}")

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

    def run_monitor(self, check_interval: int = 30):
        """
        Run the timelapse monitor loop with auto-detection.

        Args:
            check_interval: Seconds between printer status checks
        """
        print("=== Prusa Timelapse Monitor ===")
        print(f"Storage: {self.storage_path}")
        print(f"Capture interval: {self.config.capture_interval}s")
        print(f"Finishing mode: >= {self.config.finishing_threshold}% @ {self.config.finishing_interval}s")
        print(f"Post-print: {self.config.post_print_frames} frames @ {self.config.post_print_interval}s")
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

        # Resilience: debounce printer status to avoid false stops
        not_printing_count = 0
        STOP_THRESHOLD = 3  # Require 3 consecutive "not printing" to stop

        # Finishing mode: faster capture when print is almost done
        finishing_mode = False

        # Post-print capture: capture extra frames after print finishes
        post_print_mode = False
        post_print_frames_captured = 0
        post_print_last_capture = 0
        post_print_failed_attempts = 0
        POST_PRINT_MAX_FAILURES = 10  # Give up after this many consecutive failures

        # Resilience: track capture metrics
        capture_success = 0
        capture_failed = 0

        while True:
            try:
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
                has_active_job = status.is_job_active
                job_id = status.job_id

                # Session management:
                # - Keep session open while job is active (including PAUSED/ATTENTION states)
                # - Only capture frames when actively printing
                should_keep_session = has_active_job or manual_session is not None
                should_capture = is_printing or manual_session is not None

                # Resilience: debounce stop decision (only when job becomes inactive, not just paused)
                # Skip debounce if in finishing mode - we're confident the print is ending
                if not should_keep_session and current_session and not manual_session and not finishing_mode:
                    not_printing_count += 1
                    if not_printing_count < STOP_THRESHOLD:
                        # Don't stop yet - job might just be in transition
                        time.sleep(min(check_interval, self.config.capture_interval))
                        continue
                else:
                    not_printing_count = 0

                # Detect job ID change (new print started while already recording)
                if current_session and current_job_id and job_id and job_id != current_job_id:
                    print(f"\nJob changed ({current_job_id} -> {job_id}), finalizing previous session...")
                    print(f"Session stopped: {current_session}")
                    # Reset all state for new recording immediately
                    current_session = None
                    current_job_id = None
                    frame_count = 0
                    finishing_mode = False
                    post_print_mode = False
                    post_print_frames_captured = 0
                    post_print_failed_attempts = 0

                # Handle state transitions
                if should_keep_session and current_session is None:
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
                    # Reset finishing and post-print state when new session starts
                    finishing_mode = False
                    post_print_mode = False
                    post_print_frames_captured = 0
                    post_print_failed_attempts = 0

                elif not should_keep_session and current_session is not None and not post_print_mode:
                    # Enter post-print capture mode instead of stopping immediately
                    if self.config.post_print_frames > 0:
                        post_print_mode = True
                        post_print_frames_captured = 0
                        post_print_last_capture = 0
                        post_print_failed_attempts = 0
                        print(f"\nPrint finished. Capturing {self.config.post_print_frames} post-print frames...")
                    else:
                        # No post-print frames configured, stop immediately
                        print(f"\nRecording stopped: {current_session}")
                        total = capture_success + capture_failed
                        if total > 0:
                            rate = capture_success / total * 100
                            print(f"Session capture rate: {rate:.1f}% ({capture_success}/{total})")
                        self._signal_session_complete(current_session)
                        current_session = None
                        current_job_id = None
                        frame_count = 0
                        capture_success = 0
                        capture_failed = 0
                        finishing_mode = False

                # Handle post-print frame capture
                if post_print_mode and current_session:
                    # Check if manual session was created - cancel post-print and switch to manual
                    if manual_session and manual_session != current_session:
                        print(f"\nManual session requested, canceling post-print capture...")
                        print(f"Post-print capture stopped early: {current_session} ({post_print_frames_captured} frames)")
                        current_session = None
                        finishing_mode = False
                        post_print_mode = False
                        post_print_frames_captured = 0
                        post_print_failed_attempts = 0
                        # Will pick up manual session on next iteration
                        time.sleep(1)
                        continue

                    now = time.time()
                    if now - post_print_last_capture >= self.config.post_print_interval:
                        if self.capture_frame(current_session, frame_count):
                            frame_count += 1
                            post_print_frames_captured += 1
                            capture_success += 1
                            post_print_failed_attempts = 0  # Reset on success
                            remaining = self.config.post_print_frames - post_print_frames_captured
                            print(f"Post-print frame {post_print_frames_captured}/{self.config.post_print_frames} captured ({remaining} remaining)", end="\r", flush=True)
                        else:
                            capture_failed += 1
                            post_print_failed_attempts += 1
                            # Check if we've exceeded max failures
                            if post_print_failed_attempts >= POST_PRINT_MAX_FAILURES:
                                print(f"\nPost-print capture aborted: {POST_PRINT_MAX_FAILURES} consecutive failures")
                                print(f"Session stopped: {current_session} ({post_print_frames_captured}/{self.config.post_print_frames} post-print frames)")
                                self._signal_session_complete(current_session)
                                current_session = None
                                current_job_id = None
                                frame_count = 0
                                capture_success = 0
                                capture_failed = 0
                                finishing_mode = False
                                post_print_mode = False
                                post_print_frames_captured = 0
                                post_print_failed_attempts = 0
                                continue
                        post_print_last_capture = now

                        # Check if we've captured all post-print frames
                        if post_print_frames_captured >= self.config.post_print_frames:
                            print(f"\nPost-print capture complete. Recording stopped: {current_session}")
                            total = capture_success + capture_failed
                            if total > 0:
                                rate = capture_success / total * 100
                                print(f"Session capture rate: {rate:.1f}% ({capture_success}/{total})")
                            self._signal_session_complete(current_session)
                            current_session = None
                            current_job_id = None
                            frame_count = 0
                            capture_success = 0
                            capture_failed = 0
                            finishing_mode = False
                            post_print_mode = False
                            post_print_frames_captured = 0
                            post_print_failed_attempts = 0

                # Capture frames only when actively printing (not during pause/filament change or post-print)
                if current_session and should_capture and not post_print_mode:
                    # Check if we've entered finishing mode (progress >= threshold)
                    progress = status.progress if status.progress is not None else 0
                    was_finishing = finishing_mode
                    finishing_mode = progress >= self.config.finishing_threshold

                    # Announce entering finishing mode
                    if finishing_mode and not was_finishing:
                        print(f"\nFinishing mode: {progress:.1f}% - capturing every {self.config.finishing_interval}s")

                    # Use faster interval in finishing mode
                    current_interval = self.config.finishing_interval if finishing_mode else self.config.capture_interval

                    now = time.time()
                    if now - last_capture >= current_interval:
                        if self.capture_frame(current_session, frame_count):
                            frame_count += 1
                            capture_success += 1
                            if finishing_mode:
                                print(f"Frame {frame_count} captured ({progress:.1f}% - finishing)", end="\r", flush=True)
                            else:
                                print(f"Frame {frame_count} captured ({progress:.1f}%)", end="\r", flush=True)
                        else:
                            capture_failed += 1
                        last_capture = now

                        # Resilience: log capture rate every 100 attempts
                        total = capture_success + capture_failed
                        if total > 0 and total % 100 == 0:
                            rate = capture_success / total * 100
                            print(f"\nCapture rate: {rate:.1f}% ({capture_success}/{total})")
                elif current_session and not should_capture and not post_print_mode:
                    # Session active but paused - show status without capturing
                    print(f"Session active, paused ({status.state_text}) - {frame_count} frames", end="\r", flush=True)

                # Use shorter sleep when in finishing mode or post-print mode
                if post_print_mode:
                    sleep_interval = min(check_interval, self.config.post_print_interval)
                elif finishing_mode:
                    sleep_interval = min(check_interval, self.config.finishing_interval)
                else:
                    sleep_interval = min(check_interval, self.config.capture_interval)
                time.sleep(sleep_interval)

            except KeyboardInterrupt:
                print("\n\nStopping monitor...")
                if current_session:
                    print(f"Finalizing session: {current_session}...")
                    self.stop_recording()
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
