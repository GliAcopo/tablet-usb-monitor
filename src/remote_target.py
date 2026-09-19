"""Which tablet the remote-control shortcuts drive.

KDE has one Meta+Shift+T and one Meta+Shift+D, and every running host hears
them (they all register the same actions with kglobalaccel, so the press is
delivered to each). With two tablets that sent *both* to their own desktop.
Now exactly one host acts on a press: the *target*.

* ``./tabs9 target MODEL`` chooses it explicitly; the choice is kept in
  ``.local/state/remote_target.json`` and survives restarts.
* Without a choice (or when the chosen tablet has no host running) the
  target is the only running host, or the one in the lowest slot -- the
  tablet started first.
* ``tablet-screen`` (Meta+Shift+D) is the way back to normal, so a host that
  is *not* the target still obeys it when it is not in screen mode, quietly:
  after this press every tablet is a screen again.

Each host publishes ``instance-<slug>.json`` (slot, label, mode, pid) next to
its lock file; a host is "running" when its lock is held. Nothing here
prints a serial number.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
from pathlib import Path
import tempfile
import time

TARGET_NAME = 'remote_target.json'


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.target-')
    with os.fdopen(fd, 'w') as handle:
        json.dump(data, handle)
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def lock_held(path: Path) -> bool:
    """True when another process holds the flock on ``path`` (a host is up)."""
    try:
        with path.open('r') as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(handle, fcntl.LOCK_UN)
            return False
    except OSError:
        return False


class Instances:
    """The running hosts, as seen through the state directory."""

    def __init__(self, state_dir: Path):
        self.state_dir = Path(state_dir)

    def instance_file(self, slug: str) -> Path:
        return self.state_dir / f'instance-{slug}.json'

    def publish(self, slug: str, *, slot: int, label: str, mode: str = 'screen') -> None:
        """Called by the host at start and whenever its tablet mode changes."""
        _write_json(self.instance_file(slug), {
            'slug': slug, 'slot': int(slot), 'label': label, 'mode': mode,
            'pid': os.getpid(), 'timestamp': time.time()})

    def retire(self, slug: str) -> None:
        with contextlib.suppress(OSError):
            self.instance_file(slug).unlink()

    def running(self) -> list[dict]:
        """Every host whose lock is held, lowest slot first."""
        found = []
        for path in sorted(self.state_dir.glob('instance-*.json')):
            data = _read_json(path)
            slug = data.get('slug')
            if not slug or not lock_held(self.state_dir / f'host-{slug}.lock'):
                continue
            found.append(data)
        found.sort(key=lambda item: (int(item.get('slot') or 99), str(item.get('slug'))))
        return found

    # -- the target -------------------------------------------------------------
    @property
    def target_file(self) -> Path:
        return self.state_dir / TARGET_NAME

    def chosen(self) -> dict:
        """What ``./tabs9 target`` saved (may name a tablet that is not running)."""
        return _read_json(self.target_file)

    def choose(self, slug: str, label: str = '') -> None:
        _write_json(self.target_file, {'slug': slug, 'label': label or slug, 'timestamp': time.time()})

    def forget_choice(self) -> None:
        with contextlib.suppress(OSError):
            self.target_file.unlink()

    def target(self, running: list[dict] | None = None) -> dict | None:
        """The host the shortcuts drive right now, or None when none runs."""
        running = self.running() if running is None else running
        if not running:
            return None
        wanted = self.chosen().get('slug')
        for item in running:
            if item.get('slug') == wanted:
                return item
        return running[0]

    def is_target(self, slug: str) -> bool:
        item = self.target()
        return item is not None and item.get('slug') == slug

    def describe(self, slug: str) -> str:
        """One line for the host log: who the shortcuts drive and how to change it."""
        running = self.running()
        item = self.target(running)
        if item is None or len(running) < 2:
            return ''
        if item.get('slug') == slug:
            others = ', '.join(str(o.get('label')) for o in running if o.get('slug') != slug)
            return (f'Shortcuts drive this tablet; {others} ignore{"s" if len(running) == 2 else ""} '
                    f'them (./tabs9 target MODEL changes that).')
        return (f'Shortcuts drive {item.get("label")}, not this tablet '
                f'(./tabs9 target MODEL changes that).')


def main(argv=None) -> int:
    """``./tabs9 target``: show or choose the tablet the shortcuts drive."""
    import argparse
    import sys
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / 'src'))
    from tablets import ADB, TabletChoice, choose, list_tablets

    parser = argparse.ArgumentParser(description='Which tablet Meta+Shift+T drives.')
    parser.add_argument('model', nargs='?', help='tablet model (as ./tabs9 status lists it)')
    parser.add_argument('--tablet', dest='tablet', help='same as MODEL')
    parser.add_argument('--auto', action='store_true', help='forget the choice: the first started host')
    parser.add_argument('--state-dir', type=Path, default=root / '.local/state')
    args = parser.parse_args(argv)
    instances = Instances(args.state_dir)
    if args.auto:
        instances.forget_choice()
    selector = args.tablet or args.model
    if selector:
        try:
            tablet = choose(selector, list_tablets(ADB))
        except TabletChoice:
            # Not attached right now: accept a slug of a known host instead.
            running = {i.get('slug'): i for i in instances.running()}
            slug = selector.lower().replace('-', '_').replace(' ', '_')
            if slug not in running:
                print(f'No tablet matches {selector!r}; attach it or name a running one.',
                      file=sys.stderr)
                return 1
            instances.choose(slug, str(running[slug].get('label') or slug))
        else:
            instances.choose(tablet.slug, tablet.label)
    running = instances.running()
    target = instances.target(running)
    chosen = instances.chosen()
    if not running:
        if chosen:
            print(f'No tablet display is running; the shortcuts will drive {chosen.get("label")} '
                  'once its host starts.')
        else:
            print('No tablet display is running.')
        return 0
    for item in running:
        mark = '*' if target is item else ' '
        print(f'{mark} {item.get("label")}  (slot {item.get("slot")}, {item.get("mode")})')
    if len(running) > 1:
        how = 'chosen with ./tabs9 target' if chosen.get('slug') == target.get('slug') else \
              'started first; ./tabs9 target MODEL chooses another'
        print(f'Meta+Shift+T drives {target.get("label")} ({how}).')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
