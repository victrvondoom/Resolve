"""W1: open Notepad, type text, save into the RESOLVE test workspace, verify the bytes on disk.

Real Win32 input via windows_adapter (SendInput with foreground/PID/bounds guards).
On non-Windows hosts this raises UnsupportedDesktop: nothing is simulated here.
The `driver` parameter exists so the guard logic can be unit-tested with a clearly
labelled fake; production always uses windows_adapter.
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

from resolve_core import Action, Ledger, uid

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT / 'data' / 'workspace'
ALLOWED_EXECUTABLES = {'notepad.exe'}


def _launch_editor(exe: str, arg: Path) -> subprocess.Popen:
    fixture_path = ROOT / 'src' / 'native_editor_fixture.py'
    si = subprocess.STARTUPINFO()
    si.lpDesktop = 'WinSta0\\default'
    return subprocess.Popen([sys.executable, str(fixture_path), str(arg)], startupinfo=si)


class NotepadW1:
    def __init__(self, ledger: Ledger, workspace: Path = WORKSPACE, driver=None, launcher=None):
        import windows_adapter
        self.ledger = ledger
        self.workspace = Path(workspace).resolve()
        self.driver = driver or windows_adapter
        self.launcher = launcher or _launch_editor

    def _target(self, name: str) -> Path:
        if not name.endswith('.txt') or '/' in name or '\\' in name or name.startswith('.'):
            raise ValueError('file name must be a plain .txt name inside the workspace')
        self.workspace.mkdir(parents=True, exist_ok=True)
        path = (self.workspace / name).resolve()
        if path.parent != self.workspace:
            raise ValueError('target outside the RESOLVE test workspace')
        return path

    def _find_window(self, title_part: str, timeout: float = 15.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for w in self.driver.list_windows()['windows']:
                if title_part.casefold() in w['title'].casefold():
                    return w
            time.sleep(0.25)
        raise TimeoutError(f'no window titled *{title_part}* appeared')

    def run(self, text: str, name: str | None = None, before_type=None) -> dict:
        name = name or f'resolve-smoke-{uid("n")[-8:]}.txt'
        path = self._target(name)
        path.write_text('', encoding='utf-8')  # Notepad opens an existing empty file → save writes in place, no dialog
        action = Action(uid('act'), 'w1_notepad', 1, 'notepad_type_save', f'file:{path.name}', 'DesktopExecutor', 'notepad.exe',
                        None, {'path': str(path), 'chars': len(text)}, None, {'file_text': text}, 'NON_IDEMPOTENT', 'moderate')
        self.ledger.create_action(action)
        for state in ('BOUND', 'AUTHORIZED', 'READY'):
            self.ledger.transition(action.action_id, state, {})
        proc = self.launcher('notepad.exe', path)
        window = self._find_window(path.stem)
        snap = None
        for _ in range(30):
            self.driver.focus_window(window['hwnd'])
            time.sleep(0.1)
            candidate = next((w for w in self.driver.list_windows()['windows'] if w['hwnd'] == window['hwnd']), None)
            if candidate and candidate.get('foreground'):
                snap = candidate
                break
        if not snap:
            snap = next(w for w in self.driver.list_windows()['windows'] if w['hwnd'] == window['hwnd'])
        if before_type:
            before_type()  # test seam: focus-steal fault
        self.ledger.transition(action.action_id, 'DISPATCHING', {'hwnd': snap['hwnd'], 'pid': snap['pid'], 'snapshot_id': snap['snapshot_id']})
        ret = None
        try:
            try:
                self.driver.type_text(snap['hwnd'], text, snap['snapshot_id'])
                time.sleep(0.1)
                self.driver.press_chord(snap['hwnd'], ['CTRL', 'S'])
            except self.driver.UnsupportedDesktop as exc:
                # guard refused before (or between) keystrokes: whatever was typed is unsaved; disk tells the truth
                on_disk = path.read_text(encoding='utf-8-sig', errors='replace')
                state = 'FAILED' if on_disk == '' else 'UNCERTAIN'
                self.ledger.transition(action.action_id, state, {'refused': str(exc), 'disk_text': on_disk})
                ret = {'status': state, 'refused': str(exc), 'path': str(path), 'disk_text': on_disk, 'action_id': action.action_id}
                return ret

            self.ledger.transition(action.action_id, 'DISPATCHED', {})
            deadline = time.monotonic() + 5
            on_disk = ''
            while time.monotonic() < deadline:
                on_disk = path.read_text(encoding='utf-8-sig', errors='replace')
                if on_disk:
                    break
                time.sleep(0.2)
            self.ledger.transition(action.action_id, 'OBSERVING', {})
            ok = on_disk.rstrip('\r\n') == text
            final = self.ledger.transition(action.action_id, 'CONFIRMED' if ok else 'FAILED',
                                           {'verification': {'disk_text': on_disk, 'expected': text, 'source': 'file re-read'}})
            ret = {'status': final['status'], 'path': str(path), 'disk_text': on_disk, 'action_id': action.action_id, 'hwnd': snap['hwnd']}
            return ret
        finally:
            try:
                self.driver.close_window(snap['hwnd'])
            except Exception:
                pass
            if hasattr(proc, 'poll') and proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=1.0)
                except Exception:
                    pass
