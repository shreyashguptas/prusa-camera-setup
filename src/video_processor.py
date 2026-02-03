"""Video processor for creating timelapse videos from captured frames."""

import os
import sys
import time
import signal
import subprocess
from pathlib import Path
from datetime import datetime
from typing import Optional, List

from .config import Config


class VideoProcessor:
    """Creates timelapse videos from captured frames using FFmpeg."""

    READY_MARKER = "ready_for_video"
    PROCESSING_MARKER = ".processing_video"
    COMPLETE_MARKER = "video_complete"
    LOG_FILE = "video_creation.log"

    def __init__(self, config: Config):
        self.config = config
        self.storage_path = Path(config.nas_mount_point)
        self._should_stop = False

    def _check_nas_health(self, timeout: int = 5) -> bool:
        """Quick write test to verify NAS is responsive."""
        test_file = self.storage_path / ".health_check"

        def timeout_handler(signum, frame):
            raise TimeoutError("NAS health check timed out")

        old_handler = signal.signal(signal.SIGALRM, timeout_handler)
        signal.alarm(timeout)

        try:
            test_file.write_text(f"health_check_{time.time()}")
            test_file.unlink()
            return True
        except TimeoutError:
            print(f"NAS health check timed out ({timeout}s) - skipping processing")
            return False
        except Exception as e:
            print(f"NAS health check failed: {e}")
            return False
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old_handler)

    def _log(self, session_path: Path, message: str):
        """Write message to session log file."""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_line = f"[{timestamp}] {message}\n"
        try:
            log_file = session_path / self.LOG_FILE
            with open(log_file, "a") as f:
                f.write(log_line)
        except Exception as e:
            print(f"Warning: Could not write to log: {e}")
        print(message)

    def _log_memory(self, session_path: Path):
        """Log current system memory status."""
        try:
            with open("/proc/meminfo", "r") as f:
                meminfo = f.read()

            mem_total = 0
            mem_available = 0
            for line in meminfo.split("\n"):
                if line.startswith("MemTotal:"):
                    mem_total = int(line.split()[1]) // 1024
                elif line.startswith("MemAvailable:"):
                    mem_available = int(line.split()[1]) // 1024

            if mem_total > 0:
                self._log(session_path, f"System memory: {mem_available}MB free of {mem_total}MB")
        except Exception:
            pass  # Not critical if this fails

    def _get_rotation_filter(self) -> str:
        """Get FFmpeg video filter for rotation."""
        rotation = self.config.video_rotation
        if rotation == 90:
            return "transpose=1"
        elif rotation == 180:
            return "transpose=1,transpose=1"
        elif rotation == 270:
            return "transpose=2"
        return ""

    def _run_ffmpeg(self, session_path: Path, output_path: Path) -> bool:
        """Run FFmpeg using image sequence input. Returns True on success."""
        frames_dir = session_path / "frames"
        frame_pattern = str(frames_dir / "frame_%06d.jpg")

        # Build video filter
        vf_parts = []
        rotation_filter = self._get_rotation_filter()
        if rotation_filter:
            vf_parts.append(rotation_filter)

        cmd = [
            "ffmpeg",
            "-y",
            "-framerate", str(self.config.video_frame_rate),
            "-i", frame_pattern,
        ]

        if vf_parts:
            cmd.extend(["-vf", ",".join(vf_parts)])

        cmd.extend([
            "-c:v", "libx264",
            "-crf", str(self.config.video_crf),
            "-preset", self.config.video_preset,
            "-pix_fmt", "yuv420p",
            "-threads", "2",
            "-movflags", "+faststart",
            str(output_path),
        ])

        self._log(session_path, f"Running: {' '.join(cmd)}")

        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )

            timeout = 3600
            start_time = time.time()

            while process.poll() is None:
                if time.time() - start_time > timeout:
                    self._log(session_path, "ERROR: FFmpeg timeout (1 hour)")
                    process.kill()
                    return False
                time.sleep(1)

            if process.returncode == 0:
                return True
            elif process.returncode == -9:
                self._log(session_path, "ERROR: FFmpeg killed by signal 9 (likely out of memory)")
                return False
            else:
                output = process.stdout.read() if process.stdout else ""
                self._log(session_path, f"ERROR: FFmpeg failed (code {process.returncode})")
                if output:
                    self._log(session_path, f"FFmpeg output: {output[:1000]}")
                return False

        except Exception as e:
            self._log(session_path, f"ERROR: FFmpeg exception: {e}")
            return False

    def process_session(self, session_path: Path) -> bool:
        """Process a single session to create video. Returns True on success."""
        session_name = session_path.name

        ready_marker = session_path / self.READY_MARKER
        processing_marker = session_path / self.PROCESSING_MARKER
        complete_marker = session_path / self.COMPLETE_MARKER

        if not ready_marker.exists():
            return False

        if processing_marker.exists():
            self._log(session_path, f"Session {session_name} already being processed")
            return False

        if complete_marker.exists():
            self._log(session_path, f"Session {session_name} already completed")
            ready_marker.unlink(missing_ok=True)
            return True

        self._log_memory(session_path)
        self._log(session_path, f"Starting video processing for: {session_name}")

        try:
            processing_marker.touch()
        except Exception as e:
            self._log(session_path, f"ERROR: Could not create processing marker: {e}")
            return False

        try:
            frames_dir = session_path / "frames"
            if not frames_dir.exists():
                self._log(session_path, "ERROR: No frames directory found")
                processing_marker.unlink(missing_ok=True)
                return False

            frame_count = len(list(frames_dir.glob("frame_*.jpg")))
            if frame_count == 0:
                self._log(session_path, "ERROR: No frames found in session")
                processing_marker.unlink(missing_ok=True)
                return False

            self._log(session_path, f"Found {frame_count} frames")

            output_path = session_path / f"{session_name}.mp4"

            success = self._run_ffmpeg(session_path, output_path)

            if success:
                complete_marker.touch()
                ready_marker.unlink(missing_ok=True)
                processing_marker.unlink(missing_ok=True)

                if output_path.exists():
                    size_mb = output_path.stat().st_size / (1024 * 1024)
                    self._log(session_path, f"SUCCESS: Video created: {output_path.name} ({size_mb:.1f} MB)")
                return True
            else:
                processing_marker.unlink(missing_ok=True)
                return False

        except Exception as e:
            self._log(session_path, f"ERROR: Processing failed: {e}")
            processing_marker.unlink(missing_ok=True)
            return False

    def find_pending_sessions(self) -> List[Path]:
        """Find all sessions with ready_for_video marker."""
        if not self.storage_path.exists():
            return []

        pending = []
        try:
            for session_dir in self.storage_path.iterdir():
                if not session_dir.is_dir():
                    continue
                ready_marker = session_dir / self.READY_MARKER
                complete_marker = session_dir / self.COMPLETE_MARKER
                processing_marker = session_dir / self.PROCESSING_MARKER

                if ready_marker.exists() and not complete_marker.exists() and not processing_marker.exists():
                    pending.append(session_dir)
        except Exception as e:
            print(f"Error scanning for sessions: {e}")

        return sorted(pending, key=lambda p: p.name)

    def _recover_stale_sessions(self, max_age_hours: int = 2):
        """Recover sessions stuck in processing state."""
        if not self.storage_path.exists():
            return

        now = time.time()
        max_age_seconds = max_age_hours * 3600

        try:
            for session_dir in self.storage_path.iterdir():
                if not session_dir.is_dir():
                    continue

                processing_marker = session_dir / self.PROCESSING_MARKER
                if not processing_marker.exists():
                    continue

                # Check if marker is older than max_age
                marker_age = now - processing_marker.stat().st_mtime
                if marker_age < max_age_seconds:
                    continue

                session_name = session_dir.name
                print(f"Recovering stale session: {session_name} (stuck for {marker_age/3600:.1f} hours)")

                # Delete incomplete video if exists
                video_file = session_dir / f"{session_name}.mp4"
                if video_file.exists():
                    try:
                        video_file.unlink()
                        print(f"  Deleted incomplete video: {video_file.name}")
                    except Exception as e:
                        print(f"  Warning: Could not delete video: {e}")

                # Delete stale processing marker
                try:
                    processing_marker.unlink()
                    print(f"  Deleted stale processing marker")
                except Exception as e:
                    print(f"  Warning: Could not delete marker: {e}")

                # Ensure ready_for_video exists so it will be retried
                ready_marker = session_dir / self.READY_MARKER
                if not ready_marker.exists():
                    try:
                        ready_marker.touch()
                        print(f"  Created ready marker for retry")
                    except Exception as e:
                        print(f"  Warning: Could not create ready marker: {e}")

        except Exception as e:
            print(f"Error during recovery scan: {e}")

    def run_monitor(self, check_interval: int = 60):
        """Run the video processor monitor loop."""
        print("=== Prusa Video Processor ===")
        print(f"Storage: {self.storage_path}")
        print(f"Video settings:")
        print(f"  Enabled: {self.config.video_enabled}")
        print(f"  Frame rate: {self.config.video_frame_rate} FPS")
        print(f"  Rotation: {self.config.video_rotation} degrees")
        print(f"  Quality (CRF): {self.config.video_crf}")
        print(f"  Preset: {self.config.video_preset}")
        print()
        print(f"Checking for completed sessions every {check_interval}s")
        print()

        if not self.config.video_enabled:
            print("Video processing is disabled in configuration.")
            print("Enable it by setting video.enabled = true in ~/.prusa_camera_config")
            while not self._should_stop:
                time.sleep(check_interval)
            return

        def handle_signal(signum, frame):
            print("\nReceived shutdown signal...")
            self._should_stop = True

        signal.signal(signal.SIGTERM, handle_signal)
        signal.signal(signal.SIGINT, handle_signal)

        # Recover any stale sessions before starting
        self._recover_stale_sessions()
        print()

        while not self._should_stop:
            try:
                pending = self.find_pending_sessions()

                if pending:
                    # Check NAS health before processing
                    if not self._check_nas_health():
                        print("Skipping processing cycle - NAS not healthy")
                    else:
                        print(f"Found {len(pending)} session(s) ready for processing")
                        for session_path in pending:
                            if self._should_stop:
                                break
                            self.process_session(session_path)
                else:
                    print("Waiting for sessions...", end="\r", flush=True)

                for _ in range(check_interval):
                    if self._should_stop:
                        break
                    time.sleep(1)

            except KeyboardInterrupt:
                print("\n\nStopping video processor...")
                break
            except Exception as e:
                print(f"\nError in monitor loop: {e}")
                time.sleep(60)

        print("Video processor stopped.")


def main():
    """Entry point for video processor."""
    config = Config()
    if not config.load():
        print("Configuration not found. Run setup.py first.")
        sys.exit(1)

    if not config.is_configured():
        print("Configuration incomplete. Run setup.py first.")
        sys.exit(1)

    processor = VideoProcessor(config)
    processor.run_monitor()


if __name__ == "__main__":
    main()
