"""Video processor for creating timelapse videos from captured frames."""

import os
import sys
import time
import shutil
import signal
import subprocess
from pathlib import Path
from datetime import datetime
from typing import Optional, List

from .config import Config
from .nas import NASMount


class VideoProcessor:
    """Creates timelapse videos from captured frames using FFmpeg."""

    READY_MARKER = "ready_for_video"
    PROCESSING_MARKER = ".processing_video"
    COMPLETE_MARKER = "video_complete"
    LOG_FILE = "video_creation.log"
    LOCAL_STORAGE_DIR = Path.home() / "timelapse_local"

    def __init__(self, config: Config):
        self.config = config
        self.storage_path = Path(config.nas_mount_point)
        self._should_stop = False
        self.nas = NASMount(
            nas_ip=config.nas_ip,
            share_path=config.nas_share,
            mount_point=config.nas_mount_point,
            username=config.nas_username,
        )

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
        """Run FFmpeg to local temp file, then copy to NAS. Returns True on success."""
        frames_dir = session_path / "frames"
        frame_pattern = str(frames_dir / "frame_%06d.jpg")

        # Use SD card for temp storage (NOT /tmp which is RAM-backed tmpfs)
        local_tmp_dir = Path.home() / ".video_processing_tmp"
        local_tmp_dir.mkdir(parents=True, exist_ok=True)
        local_tmp_file = local_tmp_dir / output_path.name

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
            "-threads", "4",
            "-movflags", "+faststart",
            str(local_tmp_file),
        ])

        self._log(session_path, f"Encoding to local temp: {local_tmp_file}")
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

            if process.returncode == -9:
                self._log(session_path, "ERROR: FFmpeg killed by signal 9 (likely out of memory)")
                return False
            elif process.returncode != 0:
                output = process.stdout.read() if process.stdout else ""
                self._log(session_path, f"ERROR: FFmpeg failed (code {process.returncode})")
                if output:
                    self._log(session_path, f"FFmpeg output: {output[:1000]}")
                return False

            # FFmpeg succeeded — copy finished file to output location
            local_size_mb = local_tmp_file.stat().st_size / (1024 * 1024)
            self._log(session_path, f"Encoding complete ({local_size_mb:.1f} MB), copying to output...")
            shutil.copy2(str(local_tmp_file), str(output_path))
            self._log(session_path, f"Video saved: {output_path}")
            return True

        except Exception as e:
            self._log(session_path, f"ERROR: FFmpeg/copy exception: {e}")
            return False
        finally:
            # Clean up local temp file
            if local_tmp_file.exists():
                try:
                    local_tmp_file.unlink()
                except Exception:
                    pass

    def _is_local_session(self, session_path: Path) -> bool:
        """Check if session is in local storage (vs NAS)."""
        try:
            return self.LOCAL_STORAGE_DIR in session_path.parents or session_path.parent == self.LOCAL_STORAGE_DIR
        except Exception:
            return False

    def _sync_session_to_nas(self, session_path: Path) -> bool:
        """Sync a completed local session (video + log) to NAS.

        Copies the video file and log to NAS, then creates the complete
        marker on NAS. Frames are also synced if not already present.
        Local session is cleaned up only after NAS sync is confirmed.
        """
        session_name = session_path.name
        nas_session = self.storage_path / session_name

        if not self.nas.is_healthy():
            self._log(session_path, "NAS unavailable — video saved locally, will sync later")
            return False

        try:
            nas_session.mkdir(parents=True, exist_ok=True)

            # Copy video to NAS
            local_video = session_path / f"{session_name}.mp4"
            if local_video.exists():
                nas_video = nas_session / local_video.name
                self._log(session_path, f"Syncing video to NAS...")
                shutil.copy2(str(local_video), str(nas_video))
                self._log(session_path, f"Video synced to NAS: {nas_video}")

            # Sync frames that aren't on NAS yet
            local_frames = session_path / "frames"
            if local_frames.exists():
                nas_frames = nas_session / "frames"
                nas_frames.mkdir(parents=True, exist_ok=True)
                for frame in sorted(local_frames.glob("frame_*.jpg")):
                    nas_frame = nas_frames / frame.name
                    if not nas_frame.exists():
                        shutil.copy2(str(frame), str(nas_frame))

            # Copy log file
            local_log = session_path / self.LOG_FILE
            if local_log.exists():
                shutil.copy2(str(local_log), str(nas_session / self.LOG_FILE))

            # Mark complete on NAS
            (nas_session / self.COMPLETE_MARKER).touch()

            self._log(session_path, "Session fully synced to NAS")

            # Clean up local session (everything is confirmed on NAS)
            try:
                shutil.rmtree(session_path)
                print(f"Cleaned up local session: {session_name}")
            except Exception as e:
                print(f"Warning: Could not clean up local session: {e}")

            return True

        except OSError as e:
            self._log(session_path, f"NAS sync failed: {e} — video saved locally")
            return False

    def _sync_completed_local_sessions(self):
        """Sync any completed local sessions to NAS.

        Called periodically when NAS is healthy. Handles sessions where
        video processing succeeded but NAS sync failed previously.
        """
        if not self.LOCAL_STORAGE_DIR.exists():
            return

        try:
            for session_dir in self.LOCAL_STORAGE_DIR.iterdir():
                if not session_dir.is_dir():
                    continue
                complete_marker = session_dir / self.COMPLETE_MARKER
                if complete_marker.exists():
                    session_name = session_dir.name
                    # Check if already on NAS
                    nas_session = self.storage_path / session_name
                    nas_complete = nas_session / self.COMPLETE_MARKER
                    try:
                        if nas_complete.exists():
                            # Already on NAS, clean up local
                            shutil.rmtree(session_dir)
                            print(f"Cleaned up already-synced local session: {session_name}")
                            continue
                    except OSError:
                        pass

                    print(f"Syncing completed session to NAS: {session_name}")
                    self._sync_session_to_nas(session_dir)
        except Exception as e:
            print(f"Error during local session sync: {e}")

    def process_session(self, session_path: Path) -> bool:
        """Process a single session to create video. Returns True on success."""
        session_name = session_path.name
        is_local = self._is_local_session(session_path)

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
        source_label = "local" if is_local else "NAS"
        self._log(session_path, f"Starting video processing for: {session_name} (source: {source_label})")

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

                # For local sessions, sync to NAS after successful encoding
                if is_local:
                    self._sync_session_to_nas(session_path)

                return True
            else:
                processing_marker.unlink(missing_ok=True)
                return False

        except OSError as e:
            self._log(session_path, f"ERROR: Storage error during processing: {e}")
            processing_marker.unlink(missing_ok=True)
            if not is_local:
                raise  # Propagate NAS errors for recovery
            return False
        except Exception as e:
            self._log(session_path, f"ERROR: Processing failed: {e}")
            processing_marker.unlink(missing_ok=True)
            return False

    def find_pending_sessions(self) -> List[Path]:
        """Find all sessions with ready_for_video marker.

        Scans local storage first (primary), then NAS for legacy sessions.
        Deduplicates by session name to avoid processing the same session twice.
        """
        pending = []
        seen_names = set()

        # Scan local storage first (always available, no NAS dependency)
        if self.LOCAL_STORAGE_DIR.exists():
            try:
                for session_dir in self.LOCAL_STORAGE_DIR.iterdir():
                    if not session_dir.is_dir():
                        continue
                    ready_marker = session_dir / self.READY_MARKER
                    complete_marker = session_dir / self.COMPLETE_MARKER
                    processing_marker = session_dir / self.PROCESSING_MARKER

                    if ready_marker.exists() and not complete_marker.exists() and not processing_marker.exists():
                        pending.append(session_dir)
                        seen_names.add(session_dir.name)
            except Exception as e:
                print(f"Error scanning local sessions: {e}")

        # Also scan NAS for legacy sessions (from before local-first migration)
        try:
            if self.storage_path.exists():
                for session_dir in self.storage_path.iterdir():
                    if not session_dir.is_dir():
                        continue
                    if session_dir.name in seen_names:
                        continue  # Already found locally
                    ready_marker = session_dir / self.READY_MARKER
                    complete_marker = session_dir / self.COMPLETE_MARKER
                    processing_marker = session_dir / self.PROCESSING_MARKER

                    if ready_marker.exists() and not complete_marker.exists() and not processing_marker.exists():
                        pending.append(session_dir)
        except OSError:
            pass  # NAS unavailable — local sessions still processable
        except Exception as e:
            print(f"Error scanning NAS sessions: {e}")

        return sorted(pending, key=lambda p: p.name)

    def _recover_stale_sessions_in_dir(self, search_dir: Path, max_age_seconds: int):
        """Recover stale sessions in a specific directory."""
        now = time.time()
        try:
            if not search_dir.exists():
                return
            for session_dir in search_dir.iterdir():
                if not session_dir.is_dir():
                    continue

                processing_marker = session_dir / self.PROCESSING_MARKER
                if not processing_marker.exists():
                    continue

                marker_age = now - processing_marker.stat().st_mtime
                if marker_age < max_age_seconds:
                    continue

                session_name = session_dir.name
                print(f"Recovering stale session: {session_name} (stuck for {marker_age/3600:.1f} hours)")

                video_file = session_dir / f"{session_name}.mp4"
                if video_file.exists():
                    try:
                        video_file.unlink()
                        print(f"  Deleted incomplete video: {video_file.name}")
                    except Exception as e:
                        print(f"  Warning: Could not delete video: {e}")

                try:
                    processing_marker.unlink()
                    print(f"  Deleted stale processing marker")
                except Exception as e:
                    print(f"  Warning: Could not delete marker: {e}")

                ready_marker = session_dir / self.READY_MARKER
                if not ready_marker.exists():
                    try:
                        ready_marker.touch()
                        print(f"  Created ready marker for retry")
                    except Exception as e:
                        print(f"  Warning: Could not create ready marker: {e}")
        except OSError as e:
            print(f"Storage unavailable during recovery scan: {e}")
        except Exception as e:
            print(f"Error during recovery scan: {e}")

    def _recover_stale_sessions(self, max_age_hours: int = 2):
        """Recover sessions stuck in processing state (both local and NAS)."""
        max_age_seconds = max_age_hours * 3600

        # Recover local sessions first (always available)
        self._recover_stale_sessions_in_dir(self.LOCAL_STORAGE_DIR, max_age_seconds)

        # Recover NAS sessions if available
        try:
            self._recover_stale_sessions_in_dir(self.storage_path, max_age_seconds)
        except OSError:
            print("NAS unavailable during recovery scan — skipping NAS")

    def run_monitor(self, check_interval: int = 60):
        """Run the video processor monitor loop."""
        print("=== Prusa Video Processor ===")
        print(f"Local storage: {self.LOCAL_STORAGE_DIR}")
        print(f"NAS sync target: {self.storage_path}")
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

        # Recover stale sessions (local always, NAS if available)
        self._recover_stale_sessions()

        # Check NAS availability
        nas_was_unavailable = False
        if self.nas.ensure_mounted():
            print("NAS: connected — videos will be synced after encoding")
            self._sync_completed_local_sessions()
        else:
            print("NAS: unavailable — videos will be saved locally until NAS returns")
            nas_was_unavailable = True
        print()

        # Track NAS sync interval (sync completed sessions every 5 minutes)
        last_nas_sync = time.time()
        NAS_SYNC_INTERVAL = 300

        while not self._should_stop:
            try:
                # Check NAS health and sync periodically
                now = time.time()
                nas_healthy = self.nas.is_healthy()

                if not nas_healthy:
                    if not nas_was_unavailable:
                        print("NAS became unavailable — videos saved locally")
                        nas_was_unavailable = True
                    # Try remount periodically
                    self.nas.try_remount()
                    nas_healthy = self.nas.is_healthy()

                if nas_healthy and nas_was_unavailable:
                    print("NAS is back online — syncing completed sessions...")
                    nas_was_unavailable = False
                    self._recover_stale_sessions()
                    self._sync_completed_local_sessions()
                    last_nas_sync = now

                # Periodic NAS sync of completed local sessions
                if nas_healthy and now - last_nas_sync >= NAS_SYNC_INTERVAL:
                    self._sync_completed_local_sessions()
                    last_nas_sync = now

                # Find and process pending sessions (local + NAS)
                pending = self.find_pending_sessions()

                if pending:
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
            except OSError as e:
                print(f"\nStorage error: {e}")
                nas_was_unavailable = True
                time.sleep(check_interval)
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
