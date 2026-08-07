import gzip
import hashlib
import importlib.util
import os
import plistlib
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path('/Users/mutlupolatcan/.hermes')
REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / 'scripts' / 'backup_ops.py'


def load_module():
    spec = importlib.util.spec_from_file_location('backup_ops', MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BackupOpsUnitTests(unittest.TestCase):
    def setUp(self):
        self.ops = load_module()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_secure_directory_and_file_permissions(self):
        directory = self.root / 'backups'
        directory.mkdir()
        os.chmod(directory, 0o755)
        payload = directory / 'secret.bin'
        payload.write_bytes(b'secret')
        os.chmod(payload, 0o644)
        self.ops.secure_directory(directory)
        self.ops.secure_file(payload)
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(payload.stat().st_mode), 0o600)

    def test_checksum_manifest_is_correct_and_private(self):
        payload = self.root / 'dump.sql.gz'
        payload.write_bytes(b'payload')
        manifest = self.ops.write_sha256_manifest(payload)
        expected = hashlib.sha256(b'payload').hexdigest()
        self.assertEqual(manifest.read_text().strip(), f'{expected}  {payload.name}')
        self.assertEqual(stat.S_IMODE(manifest.stat().st_mode), 0o600)

    def test_gzip_validation_accepts_good_and_rejects_bad(self):
        good = self.root / 'good.gz'
        with gzip.open(good, 'wb') as fh:
            fh.write(b'ok')
        bad = self.root / 'bad.gz'
        bad.write_bytes(b'not gzip')
        self.ops.verify_gzip(good)
        with self.assertRaises(ValueError):
            self.ops.verify_gzip(bad)

    def test_hermes_zip_validates_crc_and_sqlite_snapshots(self):
        db = self.root / 'state.db'
        conn = sqlite3.connect(db)
        conn.execute('create table sessions(id text primary key)')
        conn.execute("insert into sessions values ('s1')")
        conn.commit()
        conn.close()
        archive = self.root / 'hermes.zip'
        with zipfile.ZipFile(archive, 'w') as zf:
            zf.writestr('config.yaml', 'model: test\n')
            zf.write(db, 'state.db')
        report = self.ops.verify_hermes_zip(archive)
        self.assertEqual(report['sqlite_files'], 1)
        self.assertEqual(report['sqlite_ok'], 1)

    def test_sqlite_verification_uses_immutable_uri_for_snapshot(self):
        directory = self.root / 'snapshot with spaces'
        directory.mkdir()
        db = directory / 'state.db'
        conn = sqlite3.connect(db)
        conn.execute('create table health(id integer primary key)')
        conn.commit()
        conn.close()
        self.ops._verify_sqlite(db)
        source = MODULE.read_text()
        self.assertIn('mode=ro&immutable=1', source)

    def test_retention_keeps_newest_files(self):
        paths = []
        for idx in range(5):
            p = self.root / f'b-{idx}.zip'
            p.write_text(str(idx))
            os.utime(p, (idx + 1, idx + 1))
            paths.append(p)
        removed = self.ops.prune_to_count(paths, keep=2)
        self.assertEqual({p.name for p in removed}, {'b-0.zip', 'b-1.zip', 'b-2.zip'})
        self.assertTrue((self.root / 'b-3.zip').exists())
        self.assertTrue((self.root / 'b-4.zip').exists())


class BackupPolicyContractTests(unittest.TestCase):
    def test_honcho_canonical_script_contract(self):
        text = (ROOT / 'services/honcho-stack/backup-honcho.sh').read_text()
        for required in ('set -euo pipefail', 'umask 077', '.partial', 'gzip -t', 'write-sha256', 'prune'):
            self.assertIn(required, text)

    def test_honcho_legacy_scripts_are_thin_wrappers(self):
        canonical = '/Users/mutlupolatcan/.hermes/services/honcho-stack/backup-honcho.sh'
        for path in (ROOT / 'scripts/backup-honcho.sh', ROOT / 'profiles/general/scripts/backup-honcho.sh'):
            text = path.read_text()
            self.assertIn('exec', text)
            self.assertIn(canonical, text)
            self.assertNotIn('pg_dump', text)

    def test_hermes_script_is_native_quick_only(self):
        text = (ROOT / 'scripts/profile-backup-quick.sh').read_text()
        self.assertIn('backup --quick --label scheduled-daily', text)
        self.assertIn('verify-snapshot', text)
        self.assertIn('prune-snapshots', text)
        self.assertNotIn('tar czf', text)
        self.assertIn('umask 077', text)
        self.assertIn("grep -q 'CRITICAL: could not snapshot DB'", text)
        self.assertNotIn('verify-hermes-zip', text)
        self.assertNotIn(' backup -o ', text)
        self.assertNotIn('grep -qv', text)

    def test_profile_failure_does_not_starve_later_backups(self):
        text = (REPO_ROOT / 'scripts/profile-backup-quick.sh').read_text()
        self.assertIn('record_profile_failure', text)
        self.assertIn('continue', text)
        self.assertIn('FAILED_PROFILES', text)
        self.assertIn('completed with failures', text)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_hermes = root / 'hermes'
            fake_ops = root / 'backup_ops.py'
            fake_send = root / 'send'
            script = root / 'profile-backup-quick.sh'
            fake_hermes.write_text(
                '#!/bin/bash\n'
                'set -e\n'
                'snap="$HERMES_HOME/state-snapshots/$(date +%s)-scheduled-daily"\n'
                'mkdir -p "$snap"\n'
                'printf "State snapshot created: %s\\n" "$snap"\n'
            )
            fake_ops.write_text(
                '#!/usr/bin/env python3\n'
                'import sys\n'
                'if sys.argv[1] == "verify-snapshot" and "/bad/" in sys.argv[2]:\n'
                '    raise SystemExit(1)\n'
            )
            fake_send.write_text('#!/bin/bash\nexit 0\n')
            for executable in (fake_hermes, fake_ops, fake_send):
                executable.chmod(0o700)
            script.write_text(
                text.replace('ROOT="/Users/mutlupolatcan/.hermes"', f'ROOT="{root}"')
                .replace('OPS="$ROOT/scripts/backup_ops.py"', f'OPS="{fake_ops}"')
                .replace('HERMES="/Users/mutlupolatcan/.local/bin/hermes"', f'HERMES="{fake_hermes}"')
                .replace('SEND="$ROOT/scripts/hermes-send-keychain.sh"', f'SEND="{fake_send}"')
                .replace(
                    'PROFILES="general assistant coder finance health marketing producer researcher writer"',
                    'PROFILES="bad good"',
                )
            )
            result = subprocess.run(['/bin/bash', str(script)], check=False)
            log = (root / 'backups/hermes/quick-backup.log').read_text()
            self.assertEqual(result.returncode, 1)
            self.assertTrue(any((root / 'profiles/good/state-snapshots').iterdir()))
            self.assertIn('verified=1 failed=bad', log)

    def test_marketing_recovery_helper_is_fail_closed(self):
        text = (REPO_ROOT / 'scripts/recover-marketing-state-approved.sh').read_text()
        required = (
            'EXPECTED_UID="501"',
            'EXPECTED_SESSIONS="26"',
            'EXPECTED_MESSAGES="321"',
            'stop_service_strict',
            'no_live_handles',
            'raw.sha256',
            "'.recover --ignore-freelist'",
            "VALUES('integrity-check', 1)",
            'FTS_COUNTS',
            'CORRUPT_LIVE',
            'rollback',
            'refusing bootstrap without quiescence proof',
        )
        for marker in required:
            self.assertIn(marker, text)
        self.assertNotIn('rm -rf', text)
        subprocess.run(
            ['/bin/bash', '-n', str(REPO_ROOT / 'scripts/recover-marketing-state-approved.sh')],
            check=True,
        )

    def test_marketing_recovery_detects_open_db_without_sidecars(self):
        helper = REPO_ROOT / 'scripts/recover-marketing-state-approved.sh'
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / 'state.db'
            db.write_bytes(b'sqlite-placeholder')
            holder = subprocess.Popen(
                [
                    sys.executable,
                    '-c',
                    'import sys; f=open(sys.argv[1], "rb"); print("ready", flush=True); sys.stdin.read()',
                    str(db),
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                text=True,
            )
            try:
                self.assertIsNotNone(holder.stdout)
                self.assertIsNotNone(holder.stdin)
                assert holder.stdout is not None
                assert holder.stdin is not None
                self.assertEqual(holder.stdout.readline().strip(), 'ready')
                result = subprocess.run(
                    [
                        '/bin/bash',
                        '-c',
                        'export HERMES_RECOVERY_SOURCE_ONLY=1; source "$1"; LIVE_DB="$2"; no_live_handles',
                        'bash',
                        str(helper),
                        str(db),
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('not accepting absence', result.stdout)
            finally:
                assert holder.stdin is not None
                assert holder.stdout is not None
                holder.stdin.close()
                holder.wait(timeout=5)
                holder.stdout.close()

    def test_marketing_recovery_rejects_empty_successful_handle_probe(self):
        helper = REPO_ROOT / 'scripts/recover-marketing-state-approved.sh'
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / 'state.db'
            db.write_bytes(b'sqlite-placeholder')
            result = subprocess.run(
                [
                    '/bin/bash',
                    '-c',
                    'export HERMES_RECOVERY_SOURCE_ONLY=1; source "$1"; LIVE_DB="$2"; lsof(){ return 0; }; no_live_handles',
                    'bash',
                    str(helper),
                    str(db),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('not accepting absence', result.stdout)

    def test_marketing_recovery_invalidates_quiescence_before_bootstrap(self):
        helper = REPO_ROOT / 'scripts/recover-marketing-state-approved.sh'
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / 'state'
            result = subprocess.run(
                [
                    '/bin/bash',
                    '-c',
                    (
                        'export HERMES_RECOVERY_SOURCE_ONLY=1; source "$1"; '
                        'QUIESCENT=1; SERVICE_STOPPED=1; PLIST="$2/fake.plist"; '
                        'launchctl(){ return 42; }; '
                        'STATE_FILE="$2/state"; '
                        "trap 'printf \"%s|%s\" \"$QUIESCENT\" \"$BOOTSTRAP_ATTEMPTED\" > \"$STATE_FILE\"' EXIT; "
                        'start_service'
                    ),
                    'bash',
                    str(helper),
                    tmp,
                ],
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(state.read_text(), '0|1')

    def test_marketing_recovery_restores_complete_sqlite_set_after_bootstrap_failure(self):
        helper = REPO_ROOT / 'scripts/recover-marketing-state-approved.sh'
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            live = root / 'state.db'
            corrupt = root / 'corrupt.db'
            failed = root / 'failed.db'
            for suffix in ('', '-wal', '-shm'):
                (Path(str(live) + suffix)).write_text('recovered' + suffix)
                (Path(str(corrupt) + suffix)).write_text('original' + suffix)
            result = subprocess.run(
                [
                    '/bin/bash',
                    '-c',
                    (
                        'export HERMES_RECOVERY_SOURCE_ONLY=1; source "$1"; ROOT="$2"; '
                        'LIVE_DB="$ROOT/state.db"; '
                        'CORRUPT_LIVE="$ROOT/corrupt.db"; CORRUPT_WAL="$ROOT/corrupt.db-wal"; CORRUPT_SHM="$ROOT/corrupt.db-shm"; '
                        'FAILED_RECOVERED="$ROOT/failed.db"; FAILED_WAL="$ROOT/failed.db-wal"; FAILED_SHM="$ROOT/failed.db-shm"; '
                        'SWAP_STARTED=1; SWAPPED=1; ORIGINAL_DB_MOVED=1; ORIGINAL_WAL_MOVED=1; ORIGINAL_SHM_MOVED=1; RECOVERED_DB_INSTALLED=1; '
                        'QUIESCENT=0; SERVICE_STOPPED=0; BOOTSTRAP_ATTEMPTED=1; '
                        'stop_service_strict(){ printf stopped > "$ROOT/stopped"; QUIESCENT=1; SERVICE_STOPPED=1; return 0; }; '
                        'start_service(){ SERVICE_STOPPED=0; return 0; }; '
                        'rollback 17'
                    ),
                    'bash',
                    str(helper),
                    tmp,
                ],
                check=False,
            )
            self.assertEqual(result.returncode, 17)
            self.assertTrue((root / 'stopped').exists())
            for suffix in ('', '-wal', '-shm'):
                self.assertEqual(Path(str(live) + suffix).read_text(), 'original' + suffix)
                self.assertEqual(Path(str(failed) + suffix).read_text(), 'recovered' + suffix)

    def test_marketing_recovery_reconciles_every_partial_swap_boundary(self):
        helper = REPO_ROOT / 'scripts/recover-marketing-state-approved.sh'
        scenarios = {
            'after-wal': {'live': ('', '-shm'), 'corrupt': ('-wal',)},
            'after-shm': {'live': ('',), 'corrupt': ('-wal', '-shm')},
            'after-db': {'live': (), 'corrupt': ('', '-wal', '-shm')},
        }
        for name, layout in scenarios.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                live = root / 'state.db'
                corrupt = root / 'corrupt.db'
                failed = root / 'failed.db'
                for suffix in layout['live']:
                    Path(str(live) + suffix).write_text('original' + suffix)
                for suffix in layout['corrupt']:
                    Path(str(corrupt) + suffix).write_text('original' + suffix)
                result = subprocess.run(
                    [
                        '/bin/bash',
                        '-c',
                        (
                            'export HERMES_RECOVERY_SOURCE_ONLY=1; source "$1"; ROOT="$2"; '
                            'LIVE_DB="$ROOT/state.db"; '
                            'CORRUPT_LIVE="$ROOT/corrupt.db"; CORRUPT_WAL="$ROOT/corrupt.db-wal"; CORRUPT_SHM="$ROOT/corrupt.db-shm"; '
                            'FAILED_RECOVERED="$ROOT/failed.db"; FAILED_WAL="$ROOT/failed.db-wal"; FAILED_SHM="$ROOT/failed.db-shm"; '
                            'SWAP_STARTED=1; SWAPPED=0; QUIESCENT=1; SERVICE_STOPPED=1; BOOTSTRAP_ATTEMPTED=0; '
                            'stop_service_strict(){ printf stopped > "$ROOT/stopped"; QUIESCENT=1; SERVICE_STOPPED=1; return 0; }; '
                            'start_service(){ SERVICE_STOPPED=0; return 0; }; '
                            'rollback 23'
                        ),
                        'bash',
                        str(helper),
                        tmp,
                    ],
                    check=False,
                )
                self.assertEqual(result.returncode, 23)
                self.assertTrue((root / 'stopped').exists())
                for suffix in ('', '-wal', '-shm'):
                    self.assertEqual(Path(str(live) + suffix).read_text(), 'original' + suffix)
                    self.assertFalse(Path(str(failed) + suffix).exists())

    def test_marketing_recovery_stop_failure_causes_zero_file_mutation(self):
        helper = REPO_ROOT / 'scripts/recover-marketing-state-approved.sh'
        for mode in ('launch-error', 'wrong-launch-message', 'listener-error', 'listener-zero-empty'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                live = root / 'state.db'
                corrupt = root / 'corrupt.db'
                failed = root / 'failed.db'
                for suffix in ('', '-wal', '-shm'):
                    Path(str(live) + suffix).write_text('recovered' + suffix)
                    Path(str(corrupt) + suffix).write_text('original' + suffix)
                result = subprocess.run(
                    [
                        '/bin/bash',
                        '-c',
                        (
                            'export HERMES_RECOVERY_SOURCE_ONLY=1; source "$1"; ROOT="$2"; MODE="$3"; '
                            'LIVE_DB="$ROOT/state.db"; '
                            'CORRUPT_LIVE="$ROOT/corrupt.db"; CORRUPT_WAL="$ROOT/corrupt.db-wal"; CORRUPT_SHM="$ROOT/corrupt.db-shm"; '
                            'FAILED_RECOVERED="$ROOT/failed.db"; FAILED_WAL="$ROOT/failed.db-wal"; FAILED_SHM="$ROOT/failed.db-shm"; '
                            'SWAP_STARTED=1; SWAPPED=1; QUIESCENT=0; SERVICE_STOPPED=0; BOOTSTRAP_ATTEMPTED=1; '
                            'launchctl(){ if [ "$MODE" = launch-error ]; then return 2; fi; '
                            'if [ "$MODE" = wrong-launch-message ]; then printf \'Bad request.\\nCould not find service "other" in domain for user gui: %s\\n\' "$EXPECTED_UID" >&2; return 113; fi; '
                            'printf \'Bad request.\\nCould not find service "%s" in domain for user gui: %s\\n\' "$LABEL" "$EXPECTED_UID" >&2; return 113; }; '
                            'lsof(){ if [ "$MODE" = listener-error ]; then return 2; fi; '
                            'if [ "$MODE" = listener-zero-empty ]; then return 0; fi; return 1; }; rollback 29'
                        ),
                        'bash',
                        str(helper),
                        tmp,
                        mode,
                    ],
                    check=False,
                )
                self.assertEqual(result.returncode, 29)
                for suffix in ('', '-wal', '-shm'):
                    self.assertEqual(Path(str(live) + suffix).read_text(), 'recovered' + suffix)
                    self.assertEqual(Path(str(corrupt) + suffix).read_text(), 'original' + suffix)
                    self.assertFalse(Path(str(failed) + suffix).exists())

    def test_marketing_quiescence_accepts_only_canonical_absence(self):
        helper = REPO_ROOT / 'scripts/recover-marketing-state-approved.sh'
        cases = (
            ('canonical', 'ok'),
            ('launch-error', 'fail'),
            ('wrong-launch-message', 'fail'),
            ('listener-error', 'fail'),
            ('listener-zero-empty', 'fail'),
        )
        for mode, expected in cases:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                result = subprocess.run(
                    [
                        '/bin/bash',
                        '-c',
                        (
                            'export HERMES_RECOVERY_SOURCE_ONLY=1; source "$1"; MODE="$2"; LIVE_DB="$3/missing.db"; '
                            'launchctl(){ if [ "$MODE" = launch-error ]; then return 2; fi; '
                            'if [ "$MODE" = wrong-launch-message ]; then printf \'Bad request.\\nCould not find service "other" in domain for user gui: %s\\n\' "$EXPECTED_UID" >&2; return 113; fi; '
                            'printf \'Bad request.\\nCould not find service "%s" in domain for user gui: %s\\n\' "$LABEL" "$EXPECTED_UID" >&2; return 113; }; '
                            'lsof(){ if [ "$MODE" = listener-error ]; then return 2; fi; '
                            'if [ "$MODE" = listener-zero-empty ]; then return 0; fi; return 1; }; '
                            'if wait_for_quiescence; then printf ok; else printf fail; fi'
                        ),
                        'bash',
                        str(helper),
                        mode,
                        tmp,
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 0)
                self.assertTrue(result.stdout.rstrip().endswith(expected), result.stdout)

    def test_marketing_recovery_rejects_service_reappearance_before_swap(self):
        helper = REPO_ROOT / 'scripts/recover-marketing-state-approved.sh'
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                [
                    '/bin/bash',
                    '-c',
                    (
                        'export HERMES_RECOVERY_SOURCE_ONLY=1; source "$1"; LIVE_DB="$2/missing.db"; QUIESCENT=1; '
                        'launchctl(){ return 0; }; lsof(){ return 1; }; sleep(){ :; }; '
                        'if reprove_quiescence; then printf unexpected-success; else printf "rejected|%s" "$QUIESCENT"; fi'
                    ),
                    'bash',
                    str(helper),
                    tmp,
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout, 'rejected|0')

    def test_launchd_schedules_are_spread_and_layered(self):
        with (Path.home() / 'Library/LaunchAgents/ai.hermes.backup-honcho.plist').open('rb') as fh:
            honcho = plistlib.load(fh)
        schedule = honcho['StartCalendarInterval']
        self.assertEqual({(x['Hour'], x['Minute']) for x in schedule}, {(3, 0), (15, 0)})
        with (Path.home() / 'Library/LaunchAgents/ai.hermes.backup-state.plist').open('rb') as fh:
            hermes = plistlib.load(fh)
        self.assertEqual(
            hermes['ProgramArguments'],
            ['/bin/bash', '/Users/mutlupolatcan/.hermes/scripts/profile-backup-quick.sh'],
        )
        self.assertFalse((Path.home() / 'Library/LaunchAgents/ai.hermes.backup-state-full.plist').exists())

    def test_restore_drill_is_real_and_always_cleans_up(self):
        text = (ROOT / 'services/honcho-stack/verify-honcho-restore.sh').read_text()
        for required in ('docker run', 'pg_isready', 'gzip -dc', 'ON_ERROR_STOP=1', 'trap cleanup', 'restore-drill-latest.json'):
            self.assertIn(required, text)

    def test_compose_has_api_and_deriver_healthchecks_and_metrics(self):
        text = (ROOT / 'services/honcho-stack/server/docker-compose.yml').read_text()
        self.assertGreaterEqual(text.count('healthcheck:'), 4)
        self.assertEqual(text.count('METRICS_ENABLED=true'), 2)
        self.assertIn('http://127.0.0.1:8000/health', text)

    def test_watchdog_checks_real_endpoints_and_backup_freshness(self):
        text = (ROOT / 'scripts/watchdog.sh').read_text()
        for required in ('127.0.0.1:8000/health', '127.0.0.1:18080/healthz', 'honcho-*.sql.gz', 'state-snapshots', 'manifest.json'):
            self.assertIn(required, text)
        self.assertNotIn('hermes-full-*.zip', text)

    def test_honcho_recreate_uses_fail_closed_onepassword_injection(self):
        text = (ROOT / 'services/honcho-stack/recreate-honcho.sh').read_text()
        self.assertIn('find-generic-password', text)
        self.assertIn('resolve_onepassword_secret.py', text)
        self.assertIn('export HONCHO_CODEX_ADAPTER_API_KEY', text)
        self.assertIn('docker compose', text)
        self.assertNotIn('--env-file .env', text)
        self.assertIn(
            'unset HONCHO_JWT_ROOT adapter_root auth_secret embedding_key OP_SERVICE_ACCOUNT_TOKEN',
            text,
        )
        self.assertIn(
            "trap 'unset HONCHO_CODEX_ADAPTER_API_KEY AUTH_JWT_SECRET LLM_OPENAI_API_KEY' EXIT",
            text,
        )


if __name__ == '__main__':
    unittest.main()
