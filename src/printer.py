"""PrusaLink local API for printer status monitoring."""

import requests
from typing import Optional
from dataclasses import dataclass


@dataclass
class PrinterState:
    """Represents the current printer state."""

    is_printing: bool  # True only when actively printing
    is_job_active: bool  # True when job exists and not ended (PRINTING, PAUSED, ATTENTION)
    state_text: str
    job_id: Optional[int] = None
    job_name: Optional[str] = None
    progress: Optional[float] = None


# Terminal states that mean the job has ended
TERMINAL_STATES = {"FINISHED", "STOPPED", "ERROR"}


class PrinterStatus:
    """Monitors printer status via PrusaLink local API."""

    def __init__(self, printer_ip: str, api_key: str):
        """
        Initialize printer status monitor.

        Args:
            printer_ip: Printer's local IP address
            api_key: PrusaLink API key (from printer settings)
        """
        self.printer_ip = printer_ip
        self.api_key = api_key
        self.base_url = f"http://{printer_ip}"

    def _get_headers(self) -> dict:
        """Get API request headers."""
        return {
            "X-Api-Key": self.api_key,
            "Accept": "application/json",
        }

    def get_status(self, timeout: int = 10) -> Optional[PrinterState]:
        """
        Get current printer status from PrusaLink.

        Returns:
            PrinterState object or None on error.
        """
        url = f"{self.base_url}/api/v1/status"

        try:
            response = requests.get(
                url,
                headers=self._get_headers(),
                timeout=timeout,
            )

            if response.status_code != 200:
                return None

            data = response.json()

            # Parse PrusaLink response
            job = data.get("job", {})
            printer = data.get("printer", {})

            # Job state: PRINTING, PAUSED, FINISHED, STOPPED, ERROR
            # Printer state: IDLE, BUSY, PRINTING, PAUSED, FINISHED, STOPPED, ERROR, ATTENTION
            job_state = job.get("state", "")
            printer_state = printer.get("state", "IDLE")

            is_printing = job_state == "PRINTING" or printer_state == "PRINTING"
            state_text = job_state if job_state else printer_state

            # Get job ID, name, and progress if available
            job_id = job.get("id")
            job_file = job.get("file", {})
            job_name = job_file.get("display_name") or job_file.get("name") if job_file else None
            progress = job.get("progress", 0)

            # Job is active if it exists and hasn't reached a terminal state
            # This remains True during PAUSED/ATTENTION states
            is_job_active = job_id is not None and job_state not in TERMINAL_STATES

            return PrinterState(
                is_printing=is_printing,
                is_job_active=is_job_active,
                state_text=state_text,
                job_id=job_id,
                job_name=job_name,
                progress=progress,
            )

        except requests.RequestException:
            return None
        except (KeyError, ValueError):
            return None

    def is_printing(self) -> bool:
        """Check if printer is currently printing."""
        status = self.get_status()
        return status.is_printing if status else False

    def test_connection(self) -> tuple[bool, Optional[str]]:
        """
        Test the API connection.

        Returns:
            Tuple of (success, error_message)
        """
        url = f"{self.base_url}/api/v1/status"

        try:
            response = requests.get(
                url,
                headers=self._get_headers(),
                timeout=10,
            )

            if response.status_code == 200:
                return True, None
            elif response.status_code == 401:
                return False, "Invalid API key"
            elif response.status_code == 403:
                return False, "Access forbidden"
            else:
                return False, f"HTTP {response.status_code}"

        except requests.RequestException as e:
            return False, str(e)
