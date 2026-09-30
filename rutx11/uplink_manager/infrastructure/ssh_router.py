"""RouterGateway over SSH (paramiko) to a Teltonika RutOS 7 / OpenWrt router.

Nothing secret goes on a command line: the Wi-Fi key only travels inside the `uci batch` stdin,
and the router password only to paramiko. Every name interpolated into shell is validated first.

Reloads and rollbacks run detached on the router (`trap '' HUP` subshell). A network reload can
drop this SSH session, but it can never stop a reload or a restore halfway through.
"""

from __future__ import annotations

import json
import logging
import re
import shlex
import threading
import time
from pathlib import Path

import paramiko

from ..application.ports import CONFIG_PACKAGES, RouterError, RouterGateway
from ..domain.runtime import UplinkRuntime
from ..domain.uci import UciSnapshot, parse_uci_show
from ..domain.wifi import WifiNetwork, merge_scans, parse_iwinfo_scan, parse_ubus_scan

log = logging.getLogger(__name__)

_NAME = re.compile(r'^[A-Za-z0-9_.@-]{1,32}$')
_TX = re.compile(r'^[0-9-]{8,20}$')
BACKUP_DIR = '/etc/netui-backup'
KEEP_BACKUPS = 5
PING_TARGETS = ('1.1.1.1', '8.8.8.8')


def _name(v: str) -> str:
    if not _NAME.match(v or ''):
        raise RouterError(f'refusing unsafe name {v!r}')
    return v


def _tx(v: str) -> str:
    if not _TX.match(v):
        raise RouterError(f'bad transaction id {v!r}')
    return v


def _pkgs(pkgs) -> list[str]:
    bad = [p for p in pkgs if p not in CONFIG_PACKAGES + ('dhcp',)]
    if bad:
        raise RouterError(f'unexpected uci package(s) {bad}')
    return list(pkgs)


def reload_commands(pkgs: list[str]) -> list[str]:
    """Service reloads for the changed packages, in dependency order."""
    s = set(pkgs)
    out = []
    if 'network' in s:
        out.append('/etc/init.d/network reload')
    if s & {'wireless', 'network'}:
        out.append('wifi reload || wifi up')
    if s & {'firewall', 'network', 'wireless'}:
        out.append('/etc/init.d/firewall reload')
    if 'mwan3' in s:
        out.append('[ -x /etc/init.d/mwan3 ] && /etc/init.d/mwan3 reload')
    return out


def rollback_script(tx: str) -> str:
    """Restore /etc/config from the tx backup. `mkdir` is the atomic "decided" flag shared with
    confirm(): whichever runs first wins, so a late watchdog can't undo a confirmed change."""
    pkgs = ' '.join(CONFIG_PACKAGES)
    return f'''#!/bin/sh
TX={tx}
B={BACKUP_DIR}/$TX
LOG=/tmp/netui-rollback-$TX.log
mkdir /tmp/netui-done-$TX 2>/dev/null || {{ touch /tmp/netui-skipped-$TX; exit 0; }}
echo "$(date) rollback ($1)" >> $LOG
changed=""
for p in {pkgs}; do
  uci revert $p 2>/dev/null
  [ -f "$B/$p" ] || continue
  if [ "$(md5sum < "$B/$p")" != "$(md5sum < /etc/config/$p)" ]; then
    cp "$B/$p" /etc/config/$p && changed="$changed $p"
  fi
done
echo "restored:$changed" >> $LOG
case " $changed " in *" network "*) /etc/init.d/network reload ;; esac
case " $changed " in *" wireless "*|*" network "*) wifi reload || wifi up ;; esac
case " $changed " in *" firewall "*|*" network "*|*" wireless "*) /etc/init.d/firewall reload ;; esac
case " $changed " in *" mwan3 "*) [ -x /etc/init.d/mwan3 ] && /etc/init.d/mwan3 reload ;; esac
echo "$(date) rollback finished" >> $LOG
touch /tmp/netui-rolledback-$TX
'''


def _transient(msg: str) -> bool:
    m = msg.lower()
    return 'i/o error' in m or 'lock' in m or 'resource temporarily unavailable' in m


def detached(cmd: str) -> str:
    """Run cmd in a subshell that ignores the HUP dropbear sends when the session closes."""
    return f"( trap '' HUP; {cmd} ) </dev/null >/dev/null 2>&1 &"


class SshRouter(RouterGateway):
    def __init__(self, host: str, user: str, password: str, known_hosts: Path | None = None,
                 connect_timeout: float = 8.0):
        self.host, self.user, self._password = host, user, password
        self.known_hosts = known_hosts
        self.connect_timeout = connect_timeout
        self._client: paramiko.SSHClient | None = None
        self._lock = threading.Lock()

    # ---- transport -----------------------------------------------------------------------

    def _connect(self) -> paramiko.SSHClient:
        with self._lock:
            c = self._client
            if c is not None and c.get_transport() is not None and c.get_transport().is_active():
                return c
            c = paramiko.SSHClient()
            if self.known_hosts is not None:
                self.known_hosts.parent.mkdir(parents=True, exist_ok=True)
                self.known_hosts.touch(exist_ok=True)
                c.load_host_keys(str(self.known_hosts))  # pinned: a changed key is refused
            c.set_missing_host_key_policy(paramiko.AutoAddPolicy())  # trust on first use
            try:
                c.connect(self.host, username=self.user, password=self._password,
                          timeout=self.connect_timeout, banner_timeout=self.connect_timeout,
                          auth_timeout=self.connect_timeout, look_for_keys=False,
                          allow_agent=False)
            except paramiko.BadHostKeyException as e:
                raise RouterError(f'router host key changed ({e}); if the router was replaced, '
                                  f'delete {self.known_hosts}') from e
            except (OSError, paramiko.SSHException) as e:
                raise RouterError(f'cannot reach router {self.host}: {e}') from e
            if self.known_hosts is not None:
                c.save_host_keys(str(self.known_hosts))
            c.get_transport().set_keepalive(10)
            self._client = c
            return c

    def _drop(self) -> None:
        with self._lock:
            if self._client is not None:
                self._client.close()
            self._client = None

    def run(self, cmd: str, stdin: str | None = None, timeout: float = 30.0,
            retries: int = 1) -> tuple[int, str, str]:
        for attempt in range(retries + 1):
            try:
                c = self._connect()
                chan_in, out, err = c.exec_command(cmd, timeout=timeout)
                if stdin is not None:
                    chan_in.write(stdin)
                chan_in.channel.shutdown_write()
                o = out.read().decode(errors='replace')
                e = err.read().decode(errors='replace')
                return out.channel.recv_exit_status(), o, e
            except (OSError, EOFError, paramiko.SSHException) as e:
                self._drop()
                if attempt == retries:
                    raise RouterError(f'router command failed: {e}') from e
                time.sleep(1.0)
        raise AssertionError('unreachable')

    def _ok(self, cmd: str, **kw) -> str:
        rc, o, e = self.run(cmd, **kw)
        if rc != 0:
            raise RouterError(f'router: `{cmd.split()[0]}` failed (rc {rc}): {e.strip() or o.strip()}')
        return o

    def _json(self, cmd: str) -> dict | None:
        rc, o, _ = self.run(cmd)
        if rc != 0 or not o.strip():
            return None
        try:
            return json.loads(o)
        except ValueError:
            return None

    # ---- RouterGateway -------------------------------------------------------------------

    def show(self, packages):
        pk = ' '.join(_pkgs(packages))
        return parse_uci_show(self._ok(
            f'for p in {pk}; do [ -f /etc/config/$p ] && uci -X show $p; done; true'))

    def pending_changes(self, packages):
        pk = ' '.join(_pkgs(packages))
        _, o, _ = self.run(f'for p in {pk}; do [ -f /etc/config/$p ] && uci changes $p; done; true')
        return re.sub(r"(\.(key|password|sae_password)=)'[^']*'", r"\1'********'", o.strip())

    def _wireless_status(self) -> dict:
        return self._json('ubus call network.wireless status') or {}

    def scan(self):
        """Scan every radio. A radio with an interface up is scanned through it (ubus JSON, text
        fallback). A radio without one (the 5 GHz radio when nothing uses it) is scanned by
        name: `iwinfo radioN scan` brings up a temporary interface for the scan, which the
        ubus call does not."""
        nets: list[WifiNetwork] = []
        for radio, info in self._wireless_status().items():
            if not isinstance(info, dict):
                continue
            radio = _name(radio)
            ifnames = [i.get('ifname') for i in info.get('interfaces', []) if i.get('ifname')]
            if info.get('up') and ifnames:
                dev = _name(ifnames[0])
                res = self._json(f'ubus call iwinfo scan {shlex.quote(json.dumps({"device": dev}))}')
                if res and isinstance(res.get('results'), list):
                    nets += parse_ubus_scan(res['results'], radio)
                    continue
            else:
                dev = radio
            _, o, _ = self.run(f'iwinfo {dev} scan', timeout=45)
            nets += parse_iwinfo_scan(o, radio)
        return merge_scans(nets)

    def _sta_ifname(self, iface: str) -> str | None:
        for info in self._wireless_status().values():
            if not isinstance(info, dict):
                continue
            for i in info.get('interfaces', []):
                cfg = i.get('config', {})
                nets = cfg.get('network', [])
                nets = nets.split() if isinstance(nets, str) else nets
                if cfg.get('mode') == 'sta' and iface in nets and i.get('ifname'):
                    return i['ifname']
        return None

    def runtime(self, iface):
        iface = _name(iface)
        rt = UplinkRuntime(iface)
        st = self._json(f'ubus call network.interface.{iface} status') or {}
        rt.up = bool(st.get('up'))
        addrs = st.get('ipv4-address') or []
        rt.ipv4 = addrs[0].get('address') if addrs else None
        for r in st.get('route') or []:
            if r.get('target') == '0.0.0.0' and r.get('mask') == 0:
                rt.gateway = r.get('nexthop')
        rt.device = st.get('l3_device') or st.get('device') or self._sta_ifname(iface)
        if not rt.device:
            return rt
        dev = _name(rt.device)
        info = self._json(f'ubus call iwinfo info {shlex.quote(json.dumps({"device": dev}))}')
        if info is not None:
            bssid = (info.get('bssid') or '').upper()
            if info.get('ssid') and bssid and bssid != '00:00:00:00:00:00':
                rt.ssid, rt.bssid = info['ssid'], bssid
                rt.signal, rt.channel = info.get('signal'), info.get('channel')
        else:
            _, o, _ = self.run(f'iwinfo {dev} info')
            m = re.search(r'ESSID:\s*"(.*)"', o)
            ap = re.search(r'Access Point:\s*([0-9A-F:]{17})', o)
            if m and ap and ap.group(1) != '00:00:00:00:00:00':
                rt.ssid, rt.bssid = m.group(1), ap.group(1)
                if (s := re.search(r'Signal:\s*(-?\d+)', o)):
                    rt.signal = int(s.group(1))
                if (ch := re.search(r'Channel:\s*(\d+)', o)):
                    rt.channel = int(ch.group(1))
        if rt.up and rt.ipv4:
            pings = ' || '.join(f'ping -c 2 -W 2 -I {dev} {t} >/dev/null 2>&1' for t in PING_TARGETS)
            rc, _, _ = self.run(f'{pings}', timeout=20)
            rt.internet = rc == 0
        rt.masquerade = self._masquerade(dev)
        return rt

    def _masquerade(self, dev: str) -> bool | None:
        rc, o, _ = self.run('iptables -t nat -S 2>/dev/null')
        if rc == 0 and o:
            chains = set(re.findall(rf'-A POSTROUTING -o {re.escape(dev)} .*-j (\S+)', o))
            return any(re.search(rf'-A {re.escape(ch)} .*-j MASQUERADE', o) for ch in chains)
        rc, o, _ = self.run('nft list ruleset 2>/dev/null')  # fw4 fallback
        if rc == 0 and o:
            return dev in o and 'masquerade' in o
        return None

    def begin(self, tx, packages, watchdog_s):
        tx = _tx(tx)
        pk = ' '.join(_pkgs(packages))
        self._ok(self._begin_cmd(tx, pk, int(watchdog_s)), stdin=rollback_script(tx))

    @staticmethod
    def _begin_cmd(tx: str, pk: str, watchdog_s: int) -> str:
        # stdin (the rollback script) goes to `cat`; the watchdog starts only after it is written.
        return (f'set -e; mkdir -p {BACKUP_DIR}/{tx}; '
                f'for p in {pk}; do if [ -f /etc/config/$p ]; then '
                f'cp /etc/config/$p {BACKUP_DIR}/{tx}/$p; fi; done; '
                f'ls -1 {BACKUP_DIR} | sort -r | tail -n +{KEEP_BACKUPS + 1} | '
                f'while read d; do rm -rf "{BACKUP_DIR}/$d"; done; '
                f'cat > /tmp/netui-rollback-{tx}.sh; '
                + detached(f'sleep {watchdog_s}; sh /tmp/netui-rollback-{tx}.sh watchdog'))

    COMMIT_ATTEMPTS = 3

    def apply(self, tx, batch, packages):
        tx = _tx(tx)
        pk = _pkgs(packages)
        self._stage_and_commit(batch, pk)
        reloads = ' ; '.join(reload_commands(pk))
        self._ok(detached(f'{reloads} ; touch /tmp/netui-reloaded-{tx}'))
        self._wait_file(f'/tmp/netui-reloaded-{tx}', 90, 'reload')

    def _stage_and_commit(self, batch: str, pk: list[str]) -> None:
        """`uci batch` + `uci commit`, retried on transient lock / I/O errors.

        RutOS daemons hold per-package locks in /tmp/.uci; a commit racing one of them fails
        with "uci: I/O error" (seen live on 2026-09-29). The batch only sets/deletes, so it can
        be re-staged safely after a revert. Anything else (a rejected command) fails at once.
        """
        revert = ' ; '.join(f'uci revert {p}' for p in pk)
        last = ''
        for attempt in range(1, self.COMMIT_ATTEMPTS + 1):
            rc, o, e = self.run('uci batch', stdin=batch + '\n', retries=0)
            msg = (e or o).strip()
            if rc == 0 and not msg:
                rc, o, e = self.run(' && '.join(f'uci commit {p}' for p in pk), retries=0)
                if rc == 0:
                    return
                last = f'uci commit failed: {(e or o).strip()}'
            else:
                last = f'uci batch rejected the change: {msg}'
            self.run(revert)
            if not _transient(last):
                raise RouterError(last)
            log.warning('attempt %d/%d: %s', attempt, self.COMMIT_ATTEMPTS, last)
            time.sleep(1.5 * attempt)
        raise RouterError(f'{last} (after {self.COMMIT_ATTEMPTS} attempts)')

    def _wait_file(self, path: str, timeout_s: float, what: str) -> str:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                _, o, _ = self.run(f'[ -e {path} ] && echo yes; true', timeout=10)
                if o.strip() == 'yes':
                    return path
            except RouterError as e:
                log.info('waiting for %s: %s', what, e)
            time.sleep(2.0)
        raise RouterError(f'{what} did not finish within {timeout_s:.0f} s')

    def confirm(self, tx):
        tx = _tx(tx)
        _, o, _ = self.run(f'mkdir /tmp/netui-done-{tx} 2>/dev/null && echo kept || echo late')
        if o.strip() != 'kept':
            raise RouterError('the router already rolled the change back (watchdog expired)')

    def rollback(self, tx):
        tx = _tx(tx)
        self._ok(detached(f'sh /tmp/netui-rollback-{tx}.sh now'))
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            try:
                _, o, _ = self.run(
                    f'[ -e /tmp/netui-rolledback-{tx} ] && echo done; '
                    f'[ -e /tmp/netui-skipped-{tx} ] && echo skipped; true', timeout=10)
                if 'done' in o or 'skipped' in o:
                    return
            except RouterError as e:
                log.info('waiting for rollback: %s', e)
            time.sleep(2.0)
        raise RouterError('rollback did not report completion within 90 s; check the router')
