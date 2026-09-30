"""What the use cases need from the router. Implemented over SSH in infrastructure/."""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..domain.runtime import UplinkRuntime
from ..domain.uci import UciSnapshot
from ..domain.wifi import WifiNetwork

# Everything a switch or repair may touch; all of it is backed up per transaction.
CONFIG_PACKAGES = ('wireless', 'network', 'firewall', 'mwan3')
AUDIT_PACKAGES = CONFIG_PACKAGES + ('dhcp',)


class RouterError(RuntimeError):
    pass


class RouterGateway(ABC):
    @abstractmethod
    def show(self, packages: tuple[str, ...]) -> UciSnapshot:
        """`uci -X show` of the packages that exist (a missing mwan3 is not an error)."""

    @abstractmethod
    def pending_changes(self, packages: tuple[str, ...]) -> str:
        """Uncommitted uci changes (e.g. staged in the RutOS web UI); '' when clean."""

    @abstractmethod
    def scan(self) -> list[WifiNetwork]:
        """Scan on every radio that has an interface up."""

    @abstractmethod
    def runtime(self, iface: str) -> UplinkRuntime:
        """Association, address, internet reachability and NAT of the uplink network."""

    @abstractmethod
    def begin(self, tx: str, packages: tuple[str, ...], watchdog_s: int) -> None:
        """Back the packages up on the router and arm a router-side rollback after watchdog_s."""

    @abstractmethod
    def apply(self, tx: str, batch: str, packages: list[str]) -> None:
        """`uci batch` + commit, then reload the affected services (detached, survives SSH loss)."""

    @abstractmethod
    def confirm(self, tx: str) -> None:
        """Keep the change: disarm the rollback."""

    @abstractmethod
    def rollback(self, tx: str) -> None:
        """Restore the backup now and reload; no-op if already confirmed or rolled back."""
