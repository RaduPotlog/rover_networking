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

"""`rover-netui` / `python -m uplink_manager`: run the page on a laptop or in the rover container.

Every option also reads an environment variable (the container sets those). The router
password is never an option: it comes from RUTX11_PASSWORD or an interactive prompt, so it
stays out of shell history and `ps`.
"""

from __future__ import annotations

import argparse
import getpass
import ipaddress
import logging
import os
import sys
import threading
import webbrowser
from pathlib import Path

from ..application.jobs import JobRunner
from ..application.service import Timing, UplinkService
from ..domain.invariants import UplinkSettings
from .api import create_app, job_logger

log = logging.getLogger('uplink_manager')

# <repo>/rutx11/uplink_manager/presentation/cli.py → <repo>/rutx11. In the image the package
# sits in /app next to /app/icons, so the same relative lookup works there.
REPO = Path(__file__).resolve().parents[2]
DEFAULT_DEMO = REPO / 'test' / 'fixtures' / 'rutos_join_wan1.uci'


def _env(name: str, default: str) -> str:
    return os.environ.get(name) or default


def is_loopback(bind: str) -> bool:
    if bind == 'localhost':
        return True
    try:
        return ipaddress.ip_address(bind).is_loopback
    except ValueError:
        return False


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog='rover-netui',
        description="Switch the RUTX11's Wi-Fi uplink in place and keep NAT/firewall consistent.")
    p.add_argument('--router', default=_env('RUTX11_HOST', '192.168.1.1'),
                   help='router address (env RUTX11_HOST, default %(default)s)')
    p.add_argument('--user', default=_env('RUTX11_USER', 'root'),
                   help='router SSH user (env RUTX11_USER, default %(default)s)')
    p.add_argument('--bind', default=_env('NETUI_BIND', '127.0.0.1'),
                   help='address to serve on (env NETUI_BIND, default %(default)s). Anything but '
                        'loopback requires NETUI_PASSWORD')
    p.add_argument('--port', type=int, default=int(_env('NETUI_PORT', '5080')),
                   help='port (env NETUI_PORT, default %(default)s)')
    p.add_argument('--data-dir', type=Path,
                   default=Path(_env('NETUI_DATA', str(Path.home() / '.local/state/rover-netui'))),
                   help='router host key + job log (env NETUI_DATA, default %(default)s)')
    p.add_argument('--icons', type=Path, default=Path(_env('NETUI_ICONS', str(REPO / 'icons'))),
                   help='folder with the logos (env NETUI_ICONS, default %(default)s)')
    p.add_argument('--uplink', default=_env('UPLINK_IFACE', 'auto'),
                   help='uci network of the Wi-Fi uplink, or auto (env UPLINK_IFACE)')
    p.add_argument('--uplink-zone', default=_env('UPLINK_ZONE', 'wwan'),
                   help='firewall zone that NATs the uplink (env UPLINK_ZONE, default %(default)s)')
    p.add_argument('--lan-zone', default=_env('LAN_ZONE', 'lan'),
                   help='firewall zone of the client networks (env LAN_ZONE, default %(default)s)')
    p.add_argument('--client-nets', default=_env('CLIENT_NETS', 'lan WWAN'),
                   help='networks that must reach the internet (env CLIENT_NETS, default "%(default)s")')
    p.add_argument('--open', action='store_true', help='open the page in the default browser')
    p.add_argument('--demo', nargs='?', const=DEFAULT_DEMO, type=Path, metavar='UCI_DUMP',
                   help='no router: simulate one from a `uci -X show` dump '
                        '(default: the "after a RutOS join" example)')
    return p.parse_args(argv)


def router_password() -> str:
    pw = os.environ.get('RUTX11_PASSWORD')
    if pw:
        return pw
    if sys.stdin.isatty():
        pw = getpass.getpass('RUTX11 root password: ')
        if pw:
            return pw
    sys.exit('rover-netui: set RUTX11_PASSWORD or run it in a terminal to be asked for it')


def build(args: argparse.Namespace):
    settings = UplinkSettings(uplink_iface=args.uplink, uplink_zone=args.uplink_zone,
                              lan_zone=args.lan_zone, client_nets=tuple(args.client_nets.split()))
    page_pw = os.environ.get('NETUI_PASSWORD') or None
    if page_pw is None and not is_loopback(args.bind):
        sys.exit(f'rover-netui: refusing to serve on {args.bind} without a login; '
                 'set NETUI_PASSWORD (or use --bind 127.0.0.1)')

    if args.demo:
        from ..infrastructure.demo_router import FakeRouter
        gw = FakeRouter(args.demo, settings=settings, delay_s=1.5)
        timing = Timing(verify_s=8, poll_s=1.5, settle_s=0.5)
        job_log = None
    else:
        from ..infrastructure.ssh_router import SshRouter
        gw = SshRouter(args.router, args.user, router_password(),
                       known_hosts=args.data_dir / 'known_hosts')
        timing = Timing()
        job_log = args.data_dir / 'jobs.log'
    service = UplinkService(gw, settings, JobRunner(audit_log=job_logger(job_log)), timing)
    app = create_app(service, _env('NETUI_USER', 'rover'), page_pw, args.icons)
    return app, page_pw


def main(argv: list[str] | None = None) -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(name)s %(levelname)s %(message)s')
    args = parse_args(argv)
    app, page_pw = build(args)
    host = 'localhost' if is_loopback(args.bind) else args.bind
    url = f'http://{host}:{args.port}/'
    what = f'DEMO ({args.demo})' if args.demo else f'router {args.user}@{args.router}'
    login = f"login '{_env('NETUI_USER', 'rover')}' + NETUI_PASSWORD" if page_pw else 'no login, loopback only'
    print(f'rover-netui: {what}; serving {url} ({login})', flush=True)
    if args.open:
        threading.Timer(1.5, webbrowser.open, args=(url,)).start()
    uvicorn.run(app, host=args.bind, port=args.port, log_level='warning')


if __name__ == '__main__':
    main()
