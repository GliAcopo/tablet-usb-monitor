#!/usr/bin/env python3
"""What the S Pen's gestures do, and what they could be pointed at.

    ./tabs9 pen-actions                      what each gesture does now
    ./tabs9 pen-actions click "kwin:Overview"    bind one
    ./tabs9 pen-actions up "exec:kate"           ... or a command
    ./tabs9 shortcuts                        KDE components that have actions
    ./tabs9 shortcuts kwin                   the action names of one component

The host reads the file at start-up, so restart it after a change
(./tabs9 stop && ./tabs9 start).
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from air import GESTURES                          # noqa: E402
from host import DEFAULT_PEN_ACTIONS, PEN_ACTIONS_FILE, load_pen_actions   # noqa: E402


def print_actions():
    actions = load_pen_actions()
    print(f'S Pen gestures ({PEN_ACTIONS_FILE}):')
    width = max(len(name) for name in GESTURES)
    for name in GESTURES:
        target = actions.get(name)
        note = '' if target else '  (nothing bound)'
        print(f'  {name:<{width}}  {target or "-"}{note}')
    print('\nA target is "component:action name" (see ./tabs9 shortcuts) or '
          '"exec:command".')


def set_action(gesture, target):
    if gesture not in GESTURES:
        raise SystemExit(f'unknown gesture {gesture!r}; one of: {", ".join(GESTURES)}')
    actions = load_pen_actions()
    if target in ('', '-', 'none'):
        actions.pop(gesture, None)
    else:
        if ':' not in target:
            raise SystemExit('a target is "component:action name" or "exec:command"')
        actions[gesture] = target
    PEN_ACTIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    PEN_ACTIONS_FILE.write_text(json.dumps(actions, indent=2) + '\n')
    print(f'{gesture} -> {actions.get(gesture, "nothing")}')
    print('Restart the host for it to take effect: ./tabs9 stop && ./tabs9 start')


def list_shortcuts(component):
    import dbus
    from shortcuts import KdeShortcuts
    accel = KdeShortcuts(dbus.SessionBus())
    if component:
        names = accel.known_actions(component)
        if not names:
            raise SystemExit(f'no component {component!r} (or it has no actions)')
        print(f'{component}:')
        for name in names:
            print(f'  {component}:{name}')
        return
    print('Components with global shortcuts (ask for one to see its actions):')
    for name in sorted(set(accel.components())):
        print(f'  {name}')


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--list', action='store_true', help='list KDE shortcut components')
    parser.add_argument('--defaults', action='store_true',
                        help='print the built-in gesture bindings')
    parser.add_argument('words', nargs='*', metavar='ARG')
    args = parser.parse_args()
    if args.defaults:
        print(json.dumps(DEFAULT_PEN_ACTIONS, indent=2))
    elif args.list:
        list_shortcuts(args.words[0] if args.words else None)
    elif len(args.words) >= 2:
        set_action(args.words[0], ' '.join(args.words[1:]))
    elif args.words:
        raise SystemExit('give a gesture and a target, or nothing to see the current ones')
    else:
        print_actions()


if __name__ == '__main__':
    main()
