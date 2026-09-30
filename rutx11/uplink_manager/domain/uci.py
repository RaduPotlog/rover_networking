"""UCI config model: parse `uci -X show`, express changes as batch commands, apply them offline.

Pure Python. The same `UciCmd` list is rendered for `uci batch` on the router and applied to a
`UciSnapshot` here, so a repair plan can be checked against the invariants before it is sent.
"""

from __future__ import annotations

import copy
import shlex
from dataclasses import dataclass, field

# Options whose values must never reach a log or the browser.
SECRET_OPTIONS = frozenset({'key', 'password', 'auth_secret', 'priv_key', 'sae_password'})


@dataclass
class UciSection:
    name: str
    type: str
    options: dict[str, list[str]] = field(default_factory=dict)

    def get(self, opt: str, default: str | None = None) -> str | None:
        """Option value as one string (a list is space-joined, as RutOS stores zone networks)."""
        vals = self.options.get(opt)
        return ' '.join(vals) if vals is not None else default

    def words(self, opt: str) -> list[str]:
        """Option split on whitespace; works for both the option and the list form."""
        return [w for v in self.options.get(opt, []) for w in v.split()]


@dataclass
class UciSnapshot:
    """Sections per package, in file order."""

    packages: dict[str, dict[str, UciSection]] = field(default_factory=dict)

    def sections(self, pkg: str, type_: str | None = None) -> list[UciSection]:
        return [s for s in self.packages.get(pkg, {}).values() if type_ is None or s.type == type_]

    def section(self, pkg: str, name: str) -> UciSection | None:
        return self.packages.get(pkg, {}).get(name)

    def has_package(self, pkg: str) -> bool:
        return pkg in self.packages

    def copy(self) -> UciSnapshot:
        return copy.deepcopy(self)

    def apply(self, cmds: list[UciCmd]) -> UciSnapshot:
        out = self.copy()
        for c in cmds:
            c.apply_to(out)
        return out


def parse_uci_show(text: str) -> UciSnapshot:
    """Parse `uci -X show [pkg...]` output.

    ``pkg.sec=type`` opens a section; ``pkg.sec.opt='v'`` is an option and ``pkg.sec.opt='a' 'b'``
    a list. A one-element list is indistinguishable from an option in this output; both are
    stored as a one-element list, which is all the invariants need.
    """
    snap = UciSnapshot()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or '=' not in line:
            continue
        lhs, rhs = line.split('=', 1)
        parts = lhs.split('.')
        try:
            values = shlex.split(rhs)
        except ValueError:
            values = [rhs.strip("'")]
        if len(parts) == 2:
            pkg, sec = parts
            snap.packages.setdefault(pkg, {})[sec] = UciSection(sec, values[0] if values else '')
        elif len(parts) >= 3:
            pkg, sec, opt = parts[0], parts[1], '.'.join(parts[2:])
            s = snap.packages.setdefault(pkg, {}).setdefault(sec, UciSection(sec, ''))
            s.options[opt] = values
    return snap


def quote(value: str) -> str:
    """Quote for `uci batch` (libuci's tokenizer concatenates adjacent quoted runs like sh)."""
    return "'" + value.replace("'", "'\\''") + "'"


@dataclass(frozen=True)
class UciCmd:
    """One `uci batch` line. ``op`` is set / delete / add_list / del_list."""

    op: str
    pkg: str
    section: str
    option: str | None = None
    value: str | None = None

    @property
    def path(self) -> str:
        return '.'.join(p for p in (self.pkg, self.section, self.option) if p)

    def render(self, redact: bool = False) -> str:
        if self.value is None:
            return f'{self.op} {self.path}'
        v = self.value
        if redact and self.option in SECRET_OPTIONS:
            v = '********'
        return f'{self.op} {self.path}={quote(v)}'

    def apply_to(self, snap: UciSnapshot) -> None:
        pkg = snap.packages.setdefault(self.pkg, {})
        if self.op == 'set' and self.option is None:
            pkg.setdefault(self.section, UciSection(self.section, self.value or ''))
            pkg[self.section].type = self.value or ''
            return
        sec = pkg.get(self.section)
        if self.op == 'delete':
            if sec is None:
                return
            if self.option is None:
                del pkg[self.section]
            else:
                sec.options.pop(self.option, None)
            return
        if sec is None:
            raise KeyError(f'uci: no section {self.pkg}.{self.section}')
        if self.op == 'set':
            sec.options[self.option] = [self.value or '']
        elif self.op == 'add_list':
            sec.options.setdefault(self.option, []).append(self.value or '')
        elif self.op == 'del_list':
            vals = sec.options.get(self.option, [])
            sec.options[self.option] = [v for v in vals if v != self.value]
        else:
            raise ValueError(f'unsupported uci op {self.op}')


def uci_set(pkg: str, sec: str, opt: str | None, value: str) -> UciCmd:
    return UciCmd('set', pkg, sec, opt, value)


def uci_delete(pkg: str, sec: str, opt: str | None = None) -> UciCmd:
    return UciCmd('delete', pkg, sec, opt)


def uci_del_list(pkg: str, sec: str, opt: str, value: str) -> UciCmd:
    return UciCmd('del_list', pkg, sec, opt, value)


def set_words(pkg: str, sec: UciSection, opt: str, words: list[str]) -> list[UciCmd]:
    """Write a space-separated option, dropping a list form first.

    RutOS keeps firewall zone ``network`` as ONE option string; add_list/del_list on it silently
    do nothing, so always rewrite the whole value. (`delete` is only emitted for a real list:
    deleting a missing option makes `uci batch` report an error.)
    """
    cmds = [uci_delete(pkg, sec.name, opt)] if len(sec.options.get(opt, [])) > 1 else []
    return cmds + [uci_set(pkg, sec.name, opt, ' '.join(words))]


def render_batch(cmds: list[UciCmd], redact: bool = False) -> str:
    return '\n'.join(c.render(redact) for c in cmds)


def packages_of(cmds: list[UciCmd]) -> list[str]:
    seen: list[str] = []
    for c in cmds:
        if c.pkg not in seen:
            seen.append(c.pkg)
    return seen
