import gzip
import hashlib
import importlib.util
import os
import plistlib
import sqlite3
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path('/Users/mutlupolatcan/.hermes')
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
        self.assertIn('unset secret OP_SERVICE_ACCOUNT_TOKEN', text)


if __name__ == '__main__':
    unittest.main()
