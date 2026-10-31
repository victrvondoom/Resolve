"""RESOLVE operator commands: migration state, consistent backup, verified restore.

  python scripts/resolve_admin.py migrations [--db data/inbox.sqlite3]
  python scripts/resolve_admin.py backup --out backups/2026-10-31 [--data data]
  python scripts/resolve_admin.py verify --from backups/2026-10-31
  python scripts/resolve_admin.py restore --from backups/2026-10-31 [--data data] --yes

What a backup contains: every *.sqlite3 database under the data directory (and documents/), copied with SQLite's online
backup API so each copy is a consistent snapshot even while the server runs, plus manifest.json with SHA-256 of each file.
Sensitive fields inside those databases are already sealed (AES-256-GCM, see docs/INBOX_SECURITY.md), so the backup holds
ciphertext for them. The master key (RESOLVE_MASTER_KEY or documents/keys/master.key) is deliberately NOT copied: store it
separately. A backup without its key cannot be read; a key without its backup reveals nothing.
Restore refuses to run while files fail verification, keeps the replaced files as *.pre-restore, and never deletes data.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def databases(data: Path) -> list[Path]:
    out = []
    for p in sorted(data.rglob('*.sqlite3')):
        rel = p.relative_to(data)
        if any(part in ('sandboxes', 'fixtures', 'localmail') for part in rel.parts[:-1]):
            continue
        out.append(p)
    return out


def migrations(db: Path) -> dict:
    from inbox_store import ALL_MIGRATIONS
    conn = sqlite3.connect(db)
    try:
        applied = sorted(r[0] for r in conn.execute('SELECT version FROM schema_migrations'))
        journal = conn.execute('PRAGMA journal_mode').fetchone()[0]
        integrity = conn.execute('PRAGMA quick_check').fetchone()[0]
    finally:
        conn.close()
    known = [v for v, _ in ALL_MIGRATIONS]
    return {'database': str(db), 'applied': applied, 'known': known, 'pending': [v for v in known if v not in applied],
            'unknown_applied': [v for v in applied if v not in known], 'journal_mode': journal, 'integrity': integrity}


def backup(data: Path, out: Path) -> dict:
    out.mkdir(parents=True, exist_ok=False)
    files = []
    for src in databases(data):
        rel = src.relative_to(data)
        dest = out / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        a, b = sqlite3.connect(src, timeout=30), sqlite3.connect(dest)
        try:
            a.backup(b)
            ok = b.execute('PRAGMA integrity_check').fetchone()[0]
        finally:
            b.close()
            a.close()
        files.append({'path': str(rel), 'sha256': _sha(dest), 'bytes': dest.stat().st_size, 'integrity': ok})
    manifest = {'created_at': time.time(), 'source': str(data), 'files': files, 'key_included': False,
                'note': 'Sealed fields are ciphertext. Keep the master key separately; it is not in this backup.'}
    (out / 'manifest.json').write_text(json.dumps(manifest, indent=1))
    return manifest


def verify(src: Path) -> dict:
    manifest = json.loads((src / 'manifest.json').read_text())
    problems = []
    for f in manifest['files']:
        p = src / f['path']
        if not p.exists():
            problems.append({'path': f['path'], 'problem': 'MISSING'})
        elif _sha(p) != f['sha256']:
            problems.append({'path': f['path'], 'problem': 'HASH_MISMATCH'})
        else:
            conn = sqlite3.connect(p)
            try:
                if conn.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    problems.append({'path': f['path'], 'problem': 'INTEGRITY'})
            finally:
                conn.close()
    return {'backup': str(src), 'files': len(manifest['files']), 'ok': not problems, 'problems': problems}


def restore(src: Path, data: Path) -> dict:
    check = verify(src)
    if not check['ok']:
        raise SystemExit(f'refusing to restore: {check["problems"]}')
    manifest = json.loads((src / 'manifest.json').read_text())
    restored = []
    for f in manifest['files']:
        dest = data / f['path']
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            shutil.copy2(dest, dest.with_name(dest.name + '.pre-restore'))
        for side in ('-wal', '-shm'):                     # stale WAL frames must not be replayed over the restored file
            sp = dest.with_name(dest.name + side)
            if sp.exists():
                sp.unlink()
        shutil.copy2(src / f['path'], dest)
        restored.append(f['path'])
    return {'restored': restored, 'previous_copies': [p + '.pre-restore' for p in restored]}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    sub = ap.add_subparsers(dest='cmd', required=True)
    m = sub.add_parser('migrations')
    m.add_argument('--db', default=str(ROOT / 'data' / 'inbox.sqlite3'))
    b = sub.add_parser('backup')
    b.add_argument('--out', required=True)
    b.add_argument('--data', default=str(ROOT / 'data'))
    v = sub.add_parser('verify')
    v.add_argument('--from', dest='src', required=True)
    r = sub.add_parser('restore')
    r.add_argument('--from', dest='src', required=True)
    r.add_argument('--data', default=str(ROOT / 'data'))
    r.add_argument('--yes', action='store_true', help='required: restore replaces the current databases (copies are kept)')
    a = ap.parse_args(argv)
    if a.cmd == 'migrations':
        out = migrations(Path(a.db))
    elif a.cmd == 'backup':
        out = backup(Path(a.data), Path(a.out))
    elif a.cmd == 'verify':
        out = verify(Path(a.src))
    else:
        if not a.yes:
            raise SystemExit('restore replaces the current databases; re-run with --yes (stop the server first)')
        out = restore(Path(a.src), Path(a.data))
    print(json.dumps(out, indent=1))
    return 0 if out.get('ok', True) and not out.get('pending') else 1


if __name__ == '__main__':
    raise SystemExit(main())
