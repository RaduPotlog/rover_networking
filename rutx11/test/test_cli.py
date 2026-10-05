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

import pytest
from fastapi.testclient import TestClient

from uplink_manager.presentation import cli

HOSTILE = 'http://evil.example'


def build(monkeypatch, *argv, page_pw=None):
    monkeypatch.delenv('NETUI_PASSWORD', raising=False)
    if page_pw:
        monkeypatch.setenv('NETUI_PASSWORD', page_pw)
    return cli.build(cli.parse_args(['--demo', *argv]))


def test_loopback_without_password_serves_without_login(monkeypatch):
    app, pw = build(monkeypatch)
    assert pw is None
    c = TestClient(app, base_url='http://127.0.0.1:5080')
    assert c.get('/api/audit').status_code == 200
    assert c.get('/').status_code == 200


def test_no_login_mode_rejects_foreign_host_header(monkeypatch):
    # DNS rebinding: a page on evil.example resolving to 127.0.0.1.
    app, _ = build(monkeypatch)
    c = TestClient(app, base_url=HOSTILE)
    assert c.get('/api/status').status_code == 400


def test_non_loopback_needs_a_password(monkeypatch):
    with pytest.raises(SystemExit):
        build(monkeypatch, '--bind', '0.0.0.0')
    app, pw = build(monkeypatch, '--bind', '0.0.0.0', page_pw='s3cret')
    c = TestClient(app, base_url='http://192.168.1.201:5080')
    assert c.get('/api/status').status_code == 401
    assert c.get('/api/status', auth=('rover', 's3cret')).status_code == 200


def test_icons_are_public_and_limited_to_the_folder(monkeypatch):
    app, _ = build(monkeypatch, '--bind', '0.0.0.0', page_pw='s3cret')
    c = TestClient(app, base_url='http://192.168.1.201:5080')
    r = c.get('/icons/LogoMATransparentRound-80x80-1.png')
    assert r.status_code == 200 and r.headers['content-type'] == 'image/png'
    assert c.get('/icons/..%2Fpyproject.toml').status_code == 404
    assert c.get('/icons/nope.png').status_code == 404


def test_demo_detects_the_new_uplink(monkeypatch):
    app, _ = build(monkeypatch)
    c = TestClient(app, base_url='http://127.0.0.1:5080')
    s = c.get('/api/status').json()
    assert s['settings']['uplink_iface'] == 'wan1' and s['settings']['auto']


def test_loopback_detection():
    assert cli.is_loopback('127.0.0.1') and cli.is_loopback('localhost') and cli.is_loopback('::1')
    assert not cli.is_loopback('0.0.0.0') and not cli.is_loopback('192.168.1.201')
