"""Timelapse frame capture with auto-detection."""

import os
import sys
import time
import shutil
import signal
from pathlib import Path
from datetime import datetime
from typing import Optional

from .config import Config
from .camera import Camera
from .nas import NASMount
from .printer import PrinterStatus


class TimelapseManager:
    """Manages timelapse recording with auto-detection via Prusa Connect API."""

    CONTROL_FILE = Path.home() / ".timelapse_recording"
    LOCAL_FALLBACK_DIR = Path.home() / "timelapse_local"
    MIN_FREE_DISK_MB = 2048  # Stop local captures if less than 2GB free

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
        self.nas = NASMount(
            nas_ip=config.nas_ip,
            share_path=config.nas_share,
            mount_point=config.nas_mount_point,
            username=config.nas_username,
        )
        self._nas_available = True
        self._disk_full_warned = False

    def _check_local_disk_space(self) -> bool:
        """Check if there's enough free space on the SD card for local frames.

        Returns True if there's enough space (>= MIN_FREE_DISK_MB).
        """
        try:
            stat = os.statvfs(str(Path.home()))
            free_mb = (stat.f_bavail * stat.f_frsize) / (1024 * 1024)
            return free_mb >= self.MIN_FREE_DISK_MB
        except Exception:
            return False

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

        # Always create local session directory (primary storage)
        local_dir = self.LOCAL_FALLBACK_DIR / session_name / "frames"
        local_dir.mkdir(parents=True, exist_ok=True)

        # Also create NAS directory if available (sync target)
        if self._nas_available:
            try:
                nas_dir = self.storage_path / session_name / "frames"
                nas_dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass  # NAS unavailable, frames safe locally

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
        # Always mark locally first (primary storage)
        try:
            local_session = self.LOCAL_FALLBACK_DIR / session_name
            if local_session.exists():
                (local_session / "ready_for_video").touch()
        except Exception as e:
            print(f"Warning: Could not create local ready marker: {e}")

        # Also mark on NAS if available (sync target)
        if self._nas_available:
            try:
                nas_session = self.storage_path / session_name
                if nas_session.exists():
                    (nas_session / "ready_for_video").touch()
            except OSError:
                pass  # NAS unavailable, marker safe locally

        print(f"Session ready for video: {session_name}")

    def _sync_frames_to_nas(self):
        """Sync locally stored frames to NAS without deleting local copies.

        Local copies are retained until the video processor confirms
        the session is fully processed and synced to NAS. This ensures
        zero data loss even if NAS goes down during or after sync.
        """
        if not self.LOCAL_FALLBACK_DIR.exists():
            return

        try:
            sessions = [d for d in self.LOCAL_FALLBACK_DIR.iterdir() if d.is_dir()]
        except Exception:
            return

        if not sessions:
            return

        total_synced = 0
        for session_dir in sessions:
            frames_dir = session_dir / "frames"
            if not frames_dir.exists():
                continue
            frames = sorted(frames_dir.glob("frame_*.jpg"))
            if not frames:
                continue

            session_name = session_dir.name
            nas_frames_dir = self.storage_path / session_name / "frames"

            try:
                nas_frames_dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                print(f"NAS unavailable during sync — will retry later")
                self._nas_available = False
                return

            synced = 0
            for frame in frames:
                nas_frame = nas_frames_dir / frame.name
                # Skip frames already on NAS
                try:
                    if nas_frame.exists():
                        continue
                except OSError:
                    self._nas_available = False
                    return

                if not self._copy_with_timeout(frame, nas_frame, timeout=30):
                    print(f"NAS sync stalled at {synced} frames — will retry later")
                    return

                synced += 1
                total_synced += 1
                time.sleep(0.05)

            if synced > 0:
                print(f"Synced {synced} frames to NAS: {session_name}")

            # Sync ready_for_video marker if it exists locally
            ready_marker = session_dir / "ready_for_video"
            if ready_marker.exists():
                try:
                    nas_session = self.storage_path / session_name
                    (nas_session / "ready_for_video").touch()
                except OSError:
                    pass

        if total_synced > 0:
            print(f"NAS sync complete: {total_synced} frames synced")

    def capture_frame(self, session_name: str, frame_number: int) -> bool:
        """
        Capture a frame for the timelapse.

        Always saves locally first (guaranteed storage), then syncs to NAS
        as best-effort. Frames are never lost even if NAS is unavailable.

        Args:
            session_name: Current session name
            frame_number: Frame sequence number

        Returns:
            True if capture successful.
        """
        # Capture to temp location first
        snapshot = self.camera.capture()
        if not snapshot:
            return False

        # Always save locally first (primary storage)
        if not self._check_local_disk_space():
            if not self._disk_full_warned:
                print(f"\nSD card low on space (<{self.MIN_FREE_DISK_MB}MB free) — skipping frame capture")
                self._disk_full_warned = True
            return False

        if self._disk_full_warned:
            self._disk_full_warned = False

        local_frames_dir = self.LOCAL_FALLBACK_DIR / session_name / "frames"
        try:
            local_frames_dir.mkdir(parents=True, exist_ok=True)
            frame_path = local_frames_dir / f"frame_{frame_number:06d}.jpg"
            shutil.copy2(snapshot, frame_path)
        except Exception as e:
            print(f"\nLocal save failed: {e}")
            return False

        # Best-effort NAS sync (frame is already safe locally)
        if self._nas_available:
            try:
                nas_frames_dir = self.storage_path / session_name / "frames"
                if not nas_frames_dir.exists():
                    nas_frames_dir.mkdir(parents=True, exist_ok=True)
                nas_frame = nas_frames_dir / f"frame_{frame_number:06d}.jpg"
                if not self._copy_with_timeout(frame_path, nas_frame, timeout=10):
                    if self._nas_available:
                        self._nas_available = False
            except OSError:
                if self._nas_available:
                    print(f"\nNAS sync failed — frames safe locally")
                    self._nas_available = False

        return True

    def run_monitor(self, check_interval: int = 30):
        """
        Run the timelapse monitor loop with auto-detection.

        Args:
            check_interval: Seconds between printer status checks
        """
        print("=== Prusa Timelapse Monitor ===")
        print(f"Local storage: {self.LOCAL_FALLBACK_DIR}")
        print(f"NAS sync target: {self.storage_path}")
        print(f"Capture interval: {self.config.capture_interval}s")
        print(f"Finishing mode: >= {self.config.finishing_threshold}% @ {self.config.finishing_interval}s")
        print(f"Post-print: {self.config.post_print_frames} frames @ {self.config.post_print_interval}s")
        print(f"Auto-detect: Enabled (checking every {check_interval}s)")
        print()
        print("Manual control:")
        print(f"  Start: echo 'name' > {self.CONTROL_FILE}")
        print(f"  Stop:  rm {self.CONTROL_FILE}")
        print()

        # Check initial NAS state
        self._nas_available = self.nas.is_healthy()
        if self._nas_available:
            print("NAS: connected — frames will be synced")
            self._sync_frames_to_nas()
        else:
            print("NAS: unavailable — frames safe locally, will sync when NAS returns")
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

        # NAS health check interval (check every 5 minutes, not every loop)
        last_nas_check = 0
        NAS_CHECK_INTERVAL = 300

        while True:
            try:
                # Periodic NAS health check and recovery
                now_check = time.time()
                if now_check - last_nas_check >= NAS_CHECK_INTERVAL:
                    last_nas_check = now_check
                    was_unavailable = not self._nas_available
                    self._nas_available = self.nas.is_healthy()

                    if not self._nas_available and was_unavailable:
                        # Still unavailable — attempt remount in case NAS came back
                        self._nas_available = self.nas.try_remount()

                    if not self._nas_available and not was_unavailable:
                        print(f"\nNAS became unavailable — switching to local storage")
                    elif self._nas_available and was_unavailable:
                        print(f"\nNAS is back online — transferring local frames...")
                        self._sync_frames_to_nas()

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
                        # Always create local session directory (primary storage)
                        local_dir = self.LOCAL_FALLBACK_DIR / current_session / "frames"
                        local_dir.mkdir(parents=True, exist_ok=True)
                        # Also create NAS directory if available
                        if self._nas_available:
                            try:
                                nas_dir = self.storage_path / current_session / "frames"
                                nas_dir.mkdir(parents=True, exist_ok=True)
                            except OSError:
                                pass
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
