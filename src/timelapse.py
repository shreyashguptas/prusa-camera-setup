"""Timelapse recording and video creation with auto-detection."""

import sys
import time
import shutil
import signal
import subprocess
from pathlib import Path
from datetime import datetime
from typing import Optional, List, Tuple

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

    def mark_ready_for_encoding(self, session_name: str) -> bool:
        """
        Mark a session as ready for encoding by creating a marker file.

        Args:
            session_name: Session name

        Returns:
            True if marker was created successfully.
        """
        session_dir = self.storage_path / session_name
        frames_dir = session_dir / "frames"
        marker_file = session_dir / ".ready_for_encoding"

        if not frames_dir.exists():
            return False

        frame_count = len(list(frames_dir.glob("frame_*.jpg")))
        if frame_count == 0:
            return False

        try:
            # Write frame count and timestamp to marker for debugging
            marker_file.write_text(f"frames={frame_count}\ntimestamp={datetime.now().isoformat()}\n")
            return True
        except Exception as e:
            print(f"Failed to create encoding marker: {e}")
            return False

    def _create_filelist(self, session_name: str) -> Optional[Path]:
        """
        Create FFmpeg concat demuxer filelist.txt for a session.

        Args:
            session_name: Session name

        Returns:
            Path to filelist.txt, or None if no frames found.
        """
        session_dir = self.storage_path / session_name
        frames_dir = session_dir / "frames"
        filelist_path = session_dir / "filelist.txt"

        if not frames_dir.exists():
            return None

        # Get sorted frame files
        frames = sorted(frames_dir.glob("frame_*.jpg"))
        if not frames:
            return None

        # Write filelist in concat demuxer format
        try:
            with open(filelist_path, "w") as f:
                for frame in frames:
                    # Use absolute path and escape single quotes
                    escaped_path = str(frame).replace("'", "'\\''")
                    f.write(f"file '{escaped_path}'\n")
            return filelist_path
        except Exception as e:
            print(f"Failed to create filelist: {e}")
            return None

    def _build_ffmpeg_command(self, session_name: str, filelist_path: Path) -> Tuple[str, Path, Path]:
        """
        Build optimized FFmpeg command for Pi Zero 2W.

        Encodes to local /tmp first to avoid NAS issues with faststart,
        then copies to NAS. Returns a shell command string for nohup execution.

        Args:
            session_name: Session name
            filelist_path: Path to filelist.txt

        Returns:
            Tuple of (shell_command, temp_output_path, final_output_path).
        """
        session_dir = self.storage_path / session_name
        temp_output = Path(f"/tmp/{session_name}.mp4")
        final_output = session_dir / f"{session_name}.mp4"
        log_path = session_dir / "ffmpeg.log"

        # Build FFmpeg command that encodes to /tmp then copies to NAS
        ffmpeg_cmd = (
            f"ffmpeg -f concat -safe 0 -r {self.config.video_fps} "
            f"-i '{filelist_path}' -c:v libx264 -crf {self.config.video_quality} "
            f"-preset {self.config.video_preset} -threads 4 -pix_fmt yuv420p "
            f"-movflags +faststart -y '{temp_output}'"
        )

        # Full command: encode, then copy to NAS, then cleanup temp
        shell_cmd = (
            f"nohup sh -c '{ffmpeg_cmd} && cp \"{temp_output}\" \"{final_output}\" && "
            f"rm -f \"{temp_output}\"' > '{log_path}' 2>&1 &"
        )

        return (shell_cmd, temp_output, final_output)

    def encode_video_async(self, session_name: str) -> Optional[str]:
        """
        Start background FFmpeg encoding process.

        Uses nohup with shell redirection to ensure the process survives
        service restarts. Encodes to /tmp first, then copies to NAS.

        Args:
            session_name: Session name

        Returns:
            Session name if encoding started, None if couldn't start.
        """
        session_dir = self.storage_path / session_name
        ready_marker = session_dir / ".ready_for_encoding"
        progress_marker = session_dir / ".encoding_in_progress"

        # Validate session is ready
        if not ready_marker.exists():
            return None

        # Create filelist
        filelist_path = self._create_filelist(session_name)
        if not filelist_path:
            print(f"No frames found for session: {session_name}")
            return None

        # Mark encoding in progress
        try:
            progress_marker.write_text(f"started={datetime.now().isoformat()}\n")
            ready_marker.unlink()
        except Exception as e:
            print(f"Failed to update markers: {e}")
            return None

        # Build shell command (nohup + redirect, runs fully detached)
        shell_cmd, temp_path, final_path = self._build_ffmpeg_command(session_name, filelist_path)

        try:
            # Execute via shell - this returns immediately, FFmpeg runs in background
            subprocess.run(shell_cmd, shell=True, check=False)
            print(f"Encoding started: {session_name}")
            return session_name
        except Exception as e:
            print(f"Failed to start encoding: {e}")
            # Restore ready marker on failure
            try:
                ready_marker.write_text(f"frames=unknown\ntimestamp={datetime.now().isoformat()}\n")
                progress_marker.unlink(missing_ok=True)
            except Exception:
                pass
            return None

    def check_encoding_complete(self, session_name: str) -> Tuple[bool, bool]:
        """
        Check if encoding has completed by checking for output file.

        Since encoding runs via nohup in background, we check file existence
        rather than process state.

        Args:
            session_name: Session name

        Returns:
            Tuple of (is_complete, is_success).
        """
        session_dir = self.storage_path / session_name
        progress_marker = session_dir / ".encoding_in_progress"
        failed_marker = session_dir / ".encoding_failed"
        output_path = session_dir / f"{session_name}.mp4"
        temp_path = Path(f"/tmp/{session_name}.mp4")
        filelist_path = session_dir / "filelist.txt"

        # If output exists on NAS, encoding is complete
        if output_path.exists() and output_path.stat().st_size > 1000:
            # Clean up markers
            try:
                progress_marker.unlink(missing_ok=True)
                filelist_path.unlink(missing_ok=True)
                temp_path.unlink(missing_ok=True)
                print(f"Encoding complete: {session_name}")
            except Exception as e:
                print(f"Failed to clean up: {e}")
            return (True, True)

        # If temp file exists but not output, still encoding
        if temp_path.exists():
            return (False, False)

        # If progress marker exists but no temp/output, check for FFmpeg process
        if progress_marker.exists():
            # Check if FFmpeg is still running for this session
            try:
                result = subprocess.run(
                    ["pgrep", "-f", f"ffmpeg.*{session_name}"],
                    capture_output=True,
                    text=True,
                )
                if result.returncode == 0:
                    return (False, False)  # Still running
            except Exception:
                pass

            # FFmpeg not running but no output - failed
            try:
                progress_marker.unlink(missing_ok=True)
                failed_marker.write_text(f"reason=no_output\ntimestamp={datetime.now().isoformat()}\n")
                print(f"Encoding failed: {session_name} (no output file)")
            except Exception:
                pass
            return (True, False)

        # No markers, no temp, no output - not encoding
        return (True, False)

    def find_sessions_to_encode(self) -> List[str]:
        """
        Find sessions with .ready_for_encoding marker.

        Returns:
            List of session names ready for encoding.
        """
        if not self.storage_path.exists():
            return []

        sessions = []
        try:
            for session_dir in self.storage_path.iterdir():
                if session_dir.is_dir():
                    ready_marker = session_dir / ".ready_for_encoding"
                    progress_marker = session_dir / ".encoding_in_progress"
                    output_file = session_dir / f"{session_dir.name}.mp4"

                    # Only include if ready and not already encoding or encoded
                    if ready_marker.exists() and not progress_marker.exists() and not output_file.exists():
                        sessions.append(session_dir.name)
        except Exception as e:
            print(f"Error scanning for sessions: {e}")

        return sorted(sessions)

    def run_monitor(self, check_interval: int = 30):
        """
        Run the timelapse monitor loop with auto-detection.

        Args:
            check_interval: Seconds between printer status checks
        """
        print("=== Prusa Timelapse Monitor ===")
        print(f"Storage: {self.storage_path}")
        print(f"Capture interval: {self.config.capture_interval}s")
        print(f"Video encoding: CRF {self.config.video_quality}, preset {self.config.video_preset}")
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

        # Resilience: track capture metrics
        capture_success = 0
        capture_failed = 0

        # Encoding state (just track session name, FFmpeg runs via nohup)
        encoding_session: Optional[str] = None

        while True:
            try:
                # Check encoding progress
                if encoding_session:
                    is_complete, is_success = self.check_encoding_complete(encoding_session)
                    if is_complete:
                        encoding_session = None

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
                if not should_keep_session and current_session and not manual_session:
                    not_printing_count += 1
                    if not_printing_count < STOP_THRESHOLD:
                        # Don't stop yet - job might just be in transition
                        time.sleep(min(check_interval, self.config.capture_interval))
                        continue
                else:
                    not_printing_count = 0

                # Detect job ID change (new print started while already recording)
                if current_session and current_job_id and job_id and job_id != current_job_id:
                    print(f"\nJob changed ({current_job_id} -> {job_id}), finalizing previous recording...")
                    print(f"Recording stopped: {current_session}")
                    # Mark session ready for encoding
                    if self.mark_ready_for_encoding(current_session):
                        print(f"Marked ready for encoding: {current_session}")
                    else:
                        print("Failed to mark for encoding (no frames?)")
                    # Reset for new recording immediately
                    current_session = None
                    current_job_id = None
                    frame_count = 0

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

                elif not should_keep_session and current_session is not None:
                    # Stop recording
                    print(f"\nRecording stopped: {current_session}")
                    # Log capture metrics for this session
                    total = capture_success + capture_failed
                    if total > 0:
                        rate = capture_success / total * 100
                        print(f"Session capture rate: {rate:.1f}% ({capture_success}/{total})")
                    # Mark session ready for encoding
                    if self.mark_ready_for_encoding(current_session):
                        print(f"Marked ready for encoding: {current_session}")
                    else:
                        print("Failed to mark for encoding (no frames?)")

                    current_session = None
                    current_job_id = None
                    frame_count = 0
                    capture_success = 0
                    capture_failed = 0

                # Capture frames only when actively printing (not during pause/filament change)
                if current_session and should_capture:
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
                elif current_session and not should_capture:
                    # Session active but paused - show status without capturing
                    print(f"Session active, paused ({status.state_text}) - {frame_count} frames", end="\r", flush=True)

                # Start encoding if not recording and not already encoding
                if not current_session and not encoding_session:
                    sessions_to_encode = self.find_sessions_to_encode()
                    if sessions_to_encode:
                        session_to_encode = sessions_to_encode[0]
                        result = self.encode_video_async(session_to_encode)
                        if result:
                            encoding_session = session_to_encode

                time.sleep(min(check_interval, self.config.capture_interval))

            except KeyboardInterrupt:
                print("\n\nStopping monitor...")
                if current_session:
                    print(f"Finalizing session: {current_session}...")
                    self.stop_recording()
                    if self.mark_ready_for_encoding(current_session):
                        print(f"Marked ready for encoding: {current_session}")
                    else:
                        print("No frames to encode")
                if encoding_session:
                    print(f"Encoding in progress: {encoding_session} (will continue in background)")
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
