# Copyright 2026 Mechatronics Academy
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Use cases: status, scan, audit, repair, switch, orphan actions.

Every change runs as one transaction: back up → arm a rollback ON THE ROUTER → uci batch +
reload → verify → confirm. If verification fails the service rolls back at once; if the service
or the controller dies mid-way, the router rolls itself back when the watchdog fires.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from ..domain.invariants import (
    Check, UplinkSettings, audit_config, plan_action, plan_switch, repair_plan, resolve_settings,
    uplink_sta)
from ..domain.uci import UciSnapshot
from ..domain.runtime import UplinkRuntime, runtime_checks
from ..domain.uci import UciCmd, packages_of, render_batch
from ..domain.wifi import NEEDS_KEY, validate_credentials
from .jobs import Job, JobRunner
from .ports import AUDIT_PACKAGES, CONFIG_PACKAGES, RouterError, RouterGateway


@dataclass
class Timing:
    verify_s: float = 75.0  # association + DHCP + first ping
    poll_s: float = 3.0
    watchdog_s: int = 180  # router-side rollback; must outlast apply + verify
    settle_s: float = 5.0  # after a rollback, before re-checking


class UplinkService:
    def __init__(self, gw: RouterGateway, settings: UplinkSettings, jobs: JobRunner,
                 timing: Timing | None = None, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic):
        self.gw = gw
        self.st = settings
        self.jobs = jobs
        self.t = timing or Timing()
        self._sleep = sleep
        self._clock = clock

    def _read(self) -> tuple[UciSnapshot, UplinkSettings]:
        """Current config + the settings with UPLINK_IFACE=auto pinned to what it holds now."""
        snap = self.gw.show(AUDIT_PACKAGES)
        return snap, resolve_settings(snap, self.st)

    # ---- read-only -----------------------------------------------------------------------

    def status(self) -> dict:
        snap, st = self._read()
        sta = uplink_sta(snap, st)
        rt = self.gw.runtime(st.uplink_iface)
        return {
            'configured': {
                'section': sta.name if sta else None,
                'ssid': sta.get('ssid') if sta else None,
                'encryption': sta.get('encryption') if sta else None,
                'radio': sta.get('device') if sta else None,
                'disabled': (sta.get('disabled') == '1') if sta else None,
            },
            'runtime': rt.to_dict(),
            'healthy': rt.healthy and bool(rt.masquerade),
            'settings': {'uplink_iface': st.uplink_iface, 'uplink_zone': st.uplink_zone,
                         'lan_zone': st.lan_zone, 'client_nets': list(st.client_nets),
                         'auto': self.st.uplink_iface == 'auto'},
        }

    def scan(self) -> list[dict]:
        return [n.to_dict() for n in self.gw.scan()]

    def audit(self) -> dict:
        snap, st = self._read()
        checks = audit_config(snap, st)
        rt = self.gw.runtime(st.uplink_iface)
        plan = repair_plan(checks)
        return {
            'checks': [_check_dict(c) for c in checks + runtime_checks(rt)],
            'repair': [c.render(redact=True) for c in plan],
            'pending': self.gw.pending_changes(CONFIG_PACKAGES),
        }

    # ---- changes (background jobs) -------------------------------------------------------

    def start_switch(self, ssid: str, encryption: str, key: str | None, radio: str | None) -> Job:
        key = key if encryption in NEEDS_KEY else None
        validate_credentials(ssid, encryption, key)  # fail fast, before a job exists

        def run(job: Job) -> str:
            snap, st = self._read()
            cmds = plan_switch(snap, st, ssid, encryption, key, radio)
            job.say(f'switching uplink {st.uplink_iface} to {ssid!r} ({encryption}, {radio})')
            self._transact(job, cmds, verify_internet=True)
            return f'connected to {ssid!r}'
        return self.jobs.start('switch', run)

    def start_repair(self) -> Job:
        def run(job: Job) -> str:
            snap, st = self._read()
            cmds = repair_plan(audit_config(snap, st))
            if not cmds:
                job.say('configuration already matches; nothing to repair')
                return 'nothing to repair'
            job.say(f'uplink network: {st.uplink_iface}')
            was_ok = self.gw.runtime(st.uplink_iface).healthy
            self._transact(job, cmds, verify_internet=was_ok or 'wireless' in packages_of(cmds))
            return 'repaired'
        return self.jobs.start('repair', run)

    def start_action(self, action_id: str) -> Job:
        def run(job: Job) -> str:
            snap, st = self._read()
            cmds = plan_action(snap, st, action_id)
            job.say(f'{action_id}')
            self._transact(job, cmds, verify_internet=action_id.startswith('adopt:'))
            return f'{action_id.split(":")[0]} done'
        return self.jobs.start(action_id.split(':')[0], run)

    # ---- the transaction -----------------------------------------------------------------

    def _transact(self, job: Job, cmds: list[UciCmd], verify_internet: bool) -> None:
        pending = self.gw.pending_changes(CONFIG_PACKAGES)
        if pending:
            raise RouterError('the router has uncommitted changes (RutOS web UI?); save or '
                              'discard them there first:\n' + pending)
        tx = time.strftime('%Y%m%d-%H%M%S')
        job.say(f'backup to /etc/netui-backup/{tx}, rollback armed on the router '
                f'({self.t.watchdog_s} s)')
        self.gw.begin(tx, CONFIG_PACKAGES, self.t.watchdog_s)
        job.say('uci batch:\n' + render_batch(cmds, redact=True))
        try:
            self.gw.apply(tx, render_batch(cmds), packages_of(cmds))
        except Exception as e:
            job.say(f'apply failed ({e}); rolling back')
            self.gw.rollback(tx)
            raise
        job.say('applied; reloading ' + ', '.join(packages_of(cmds)))

        problem = self._verify(job, verify_internet)
        if problem is None:
            self.gw.confirm(tx)
            job.say('verified; change kept (rollback disarmed)')
            return
        job.say(f'{problem}; rolling back to the previous configuration')
        self.gw.rollback(tx)
        self._sleep(self.t.settle_s)
        rt = self.gw.runtime(self._read()[1].uplink_iface)
        job.say(f'after rollback: associated to {rt.ssid!r}, ip {rt.ipv4}, '
                f'internet {"ok" if rt.internet else "not yet"}')
        raise RouterError(f'{problem}; previous configuration restored')

    def _verify(self, job: Job, verify_internet: bool) -> str | None:
        snap, st = self._read()
        bad = [c for c in audit_config(snap, st) if c.status == 'error' and c.fix]
        if bad:
            return 'configuration still wrong after apply: ' + ', '.join(c.id for c in bad)
        if not verify_internet:
            job.say('router reachable, configuration consistent (internet not required)')
            return None
        deadline = self._clock() + self.t.verify_s
        rt: UplinkRuntime | None = None
        while True:
            try:
                rt = self.gw.runtime(st.uplink_iface)
            except RouterError as e:  # SSH can drop while the network reloads
                job.say(f'router not answering yet ({e})')
                rt = None
            if rt is not None:
                job.say(f'assoc={rt.ssid!r} ip={rt.ipv4} internet={rt.internet} '
                        f'nat={rt.masquerade}')
                if rt.healthy and rt.masquerade:
                    return None
            if self._clock() >= deadline:
                break
            self._sleep(self.t.poll_s)
        if rt is None:
            return 'router unreachable after the change'
        if not rt.ssid:
            return 'did not associate (wrong password or network out of range)'
        if not rt.ipv4:
            return 'associated but got no IP address (DHCP)'
        if not rt.internet:
            return 'got an address but no internet through the new network'
        return 'NAT (masquerade) missing for the uplink'


def _check_dict(c: Check) -> dict:
    return {'id': c.id, 'title': c.title, 'status': c.status, 'detail': c.detail,
            'fix': [x.render(redact=True) for x in c.fix],
            'actions': [{'id': a.id, 'label': a.label,
                         'preview': [x.render(redact=True) for x in a.cmds]} for a in c.actions]}
