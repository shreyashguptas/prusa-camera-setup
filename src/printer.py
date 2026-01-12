"""Prusa Connect API for printer status monitoring."""

import requests
from typing import Optional
from dataclasses import dataclass


@dataclass
class PrinterState:
    """Represents the current printer state."""

    is_printing: bool
    state_text: str
    job_name: Optional[str] = None
    progress: Optional[float] = None


class PrinterStatus:
    """Monitors printer status via Prusa Connect API."""

    API_BASE = "https://connect.prusa3d.com/api/v1"

    def __init__(self, api_key: str, printer_uuid: str):
        """
        Initialize printer status monitor.

        Args:
            api_key: PrusaConnect API key from Account > API Access
            printer_uuid: Printer UUID from the printer URL
        """
        self.api_key = api_key
        self.printer_uuid = printer_uuid

    def _get_headers(self) -> dict:
        """Get API request headers."""
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
        }

    def get_status(self, timeout: int = 15) -> Optional[PrinterState]:
        """
        Get current printer status.

        Returns:
            PrinterState object or None on error.
        """
        url = f"{self.API_BASE}/printers/{self.printer_uuid}/status"

        try:
            response = requests.get(
                url,
                headers=self._get_headers(),
                timeout=timeout,
            )

            if response.status_code != 200:
                return None

            data = response.json()

            # Parse printer state
            is_printing = False
            state_text = "UNKNOWN"
            job_name = None
            progress = None

            # Check job state
            if "job" in data and data["job"]:
                job = data["job"]
                state_text = job.get("state", "UNKNOWN")
                is_printing = state_text == "PRINTING"
                job_name = job.get("display_name")
                progress = job.get("progress")

            # Also check printer state
            if "printer" in data and data["printer"]:
                printer = data["printer"]
                if "state" in printer:
                    state_text = printer["state"]
                    is_printing = state_text == "PRINTING"

            return PrinterState(
                is_printing=is_printing,
                state_text=state_text,
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
        url = f"{self.API_BASE}/printers/{self.printer_uuid}/status"

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
            elif response.status_code == 404:
                return False, "Printer not found (check UUID)"
            else:
                return False, f"HTTP {response.status_code}"

        except requests.RequestException as e:
            return False, str(e)
