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

"""Run the router-side shell (backup, watchdog, rollback) under a POSIX sh with stubbed tools."""

import os
import re
import subprocess
from pathlib import Path

import pytest

from uplink_manager.infrastructure.ssh_router import (
    SshRouter, reload_commands, rollback_script)

TX = '20260928-231500'


@pytest.fixture
def root(tmp_path):
    for d in ('etc/config', 'etc/init.d', 'tmp', 'bin'):
        (tmp_path / d).mkdir(parents=True)
    for p in ('wireless', 'network', 'firewall'):
        (tmp_path / 'etc/config' / p).write_text(f'{p} original\n')
    calls = tmp_path / 'calls'
    stub = f'#!/bin/sh\necho "$(basename $0) $*" >> {calls}\n'
    for tool in ('uci', 'wifi'):
        (tmp_path / 'bin' / tool).write_text(stub)
    for svc in ('network', 'firewall'):
        (tmp_path / 'etc/init.d' / svc).write_text(stub)
    for f in list((tmp_path / 'bin').iterdir()) + list((tmp_path / 'etc/init.d').iterdir()):
        f.chmod(0o755)
    return tmp_path


def rooted(script: str, root: Path) -> str:
    # One pass, so the (itself /tmp-based) root is not rewritten again.
    return re.sub(r'(?<![\w.-])/(etc|tmp)/', lambda m: f'{root}/{m.group(1)}/', script)


def sh(cmd: str, root: Path, stdin: str | None = None):
    env = {'PATH': f'{root}/bin:/usr/bin:/bin'}
    return subprocess.run(['sh', '-c', cmd], input=stdin, env=env, capture_output=True,
                          text=True, timeout=20, check=True)


def begin(root: Path, watchdog_s: int = 3600):
    cmd = SshRouter._begin_cmd(TX, 'wireless network firewall mwan3', watchdog_s)
    sh(rooted(cmd, root), root, stdin=rooted(rollback_script(TX), root))


def test_backup_then_rollback_restores_and_reloads(root):
    begin(root)
    assert (root / f'etc/netui-backup/{TX}/wireless').read_text() == 'wireless original\n'
    assert not (root / f'etc/netui-backup/{TX}/mwan3').exists()  # absent package skipped
    (root / 'etc/config/wireless').write_text('wireless CHANGED\n')

    sh(f'sh {root}/tmp/netui-rollback-{TX}.sh now', root)

    assert (root / 'etc/config/wireless').read_text() == 'wireless original\n'
    assert (root / f'tmp/netui-rolledback-{TX}').exists()
    calls = (root / 'calls').read_text()
    assert 'wifi reload' in calls and 'firewall reload' in calls
    assert 'network reload' not in calls  # network config was untouched
    assert 'restored: wireless' in (root / f'tmp/netui-rollback-{TX}.log').read_text()


def test_confirm_beats_watchdog(root):
    begin(root)
    (root / 'etc/config/firewall').write_text('firewall NEW\n')
    os.mkdir(root / f'tmp/netui-done-{TX}')  # what confirm() does on the router
    sh(f'sh {root}/tmp/netui-rollback-{TX}.sh watchdog', root)
    assert (root / 'etc/config/firewall').read_text() == 'firewall NEW\n'
    assert (root / f'tmp/netui-skipped-{TX}').exists()


def test_second_rollback_is_noop(root):
    begin(root)
    sh(f'sh {root}/tmp/netui-rollback-{TX}.sh now', root)
    (root / 'calls').write_text('')
    sh(f'sh {root}/tmp/netui-rollback-{TX}.sh watchdog', root)
    assert (root / 'calls').read_text() == ''


def test_old_backups_pruned(root):
    for i in range(7):
        (root / f'etc/netui-backup/20260101-00000{i}').mkdir(parents=True)
    begin(root)
    kept = sorted(p.name for p in (root / 'etc/netui-backup').iterdir())
    assert len(kept) == 5 and TX in kept


def test_reload_order():
    assert reload_commands(['wireless']) == ['wifi reload || wifi up',
                                             '/etc/init.d/firewall reload']
    assert reload_commands(['firewall']) == ['/etc/init.d/firewall reload']
    assert reload_commands(['network', 'mwan3'])[0] == '/etc/init.d/network reload'


class ScriptedRouter(SshRouter):
    """SshRouter whose `run` answers from a script instead of SSH."""

    def __init__(self, answers):
        super().__init__('192.0.2.1', 'root', 'x')
        self.answers, self.calls = list(answers), []

    def run(self, cmd, stdin=None, timeout=30.0, retries=1):
        self.calls.append(cmd)
        for prefix, answer in self.answers:
            if cmd.startswith(prefix):
                self.answers.remove((prefix, answer))
                return answer
        return 0, '', ''


def test_commit_retries_transient_io_error(monkeypatch):
    monkeypatch.setattr('time.sleep', lambda s: None)
    r = ScriptedRouter([('uci commit', (1, '', 'uci: I/O error'))])
    r._stage_and_commit("set wireless.3.ssid='X'", ['wireless'])
    assert [c for c in r.calls if c.startswith('uci')] == [
        'uci batch', 'uci commit wireless', 'uci revert wireless', 'uci batch', 'uci commit wireless']


def test_commit_gives_up_after_three_attempts(monkeypatch):
    from uplink_manager.application.ports import RouterError
    monkeypatch.setattr('time.sleep', lambda s: None)
    r = ScriptedRouter([('uci commit', (1, '', 'uci: I/O error'))] * 3)
    with pytest.raises(RouterError, match='after 3 attempts'):
        r._stage_and_commit("set wireless.3.ssid='X'", ['wireless'])


def test_rejected_batch_fails_immediately(monkeypatch):
    from uplink_manager.application.ports import RouterError
    monkeypatch.setattr('time.sleep', lambda s: None)
    r = ScriptedRouter([('uci batch', (0, '', 'uci: Parse error (invalid command) at line 1'))])
    with pytest.raises(RouterError, match='rejected'):
        r._stage_and_commit('bogus', ['wireless'])
    assert r.calls.count('uci batch') == 1


def test_scan_covers_a_radio_without_interfaces():
    import json
    status = {'radio0': {'up': True, 'interfaces': [{'ifname': 'wlan0-1'}]},
              'radio1': {'up': False, 'interfaces': []}}
    ubus = {'results': [{'ssid': 'Two', 'bssid': 'aa:00:00:00:00:01', 'channel': 6, 'signal': -50,
                         'encryption': {'enabled': True, 'wpa': [2], 'authentication': ['psk']}}]}
    text = 'Cell 01 - Address: BB:00:00:00:00:01\n  ESSID: "Five"\n  Channel: 36\n' \
           '  Signal: -60 dBm\n  Encryption: WPA2 PSK (CCMP)\n'
    r = ScriptedRouter([('ubus call network.wireless status', (0, json.dumps(status), '')),
                        ('ubus call iwinfo scan', (0, json.dumps(ubus), '')),
                        ('iwinfo radio1 scan', (0, text, ''))])
    nets = {n.ssid: n for n in r.scan()}
    assert nets['Two'].radio == 'radio0' and nets['Five'].radio == 'radio1'
    assert nets['Five'].band == '5 GHz'
