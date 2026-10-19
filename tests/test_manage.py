import json
from pathlib import Path
import plistlib
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import manage
from radar import Store,item


class ManagementTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.base=Path(self.tmp.name)
        self.root=self.base/'original';(self.root/'data').mkdir(parents=True)
        self.source=dict(id='fixture',name='Fixture',url='https://example.test/rss',kind='feed',interval=60)
        self.config=dict(port=8741,x_accounts=[],sources=[self.source])
        (self.root/'config.json').write_text(json.dumps(self.config))

    def tearDown(self):self.tmp.cleanup()

    def seed(self):
        s=Store(self.root/'data/radar.sqlite3',self.config)
        def result(entries,t):return dict(started=t,finished=t,status=200,headers={},entries=entries,fetch_ms=1)
        s.apply(self.source,result([item('old','Old','https://example.test/old')],1000))
        s.apply(self.source,result([item('new','New','https://example.test/new')],1001))
        with s.db:s.set_meta('notification_bridge',{'authorization':2})
        s.set_notification('fixture',False)
        return s

    def test_live_backup_and_restore_preserves_wal_history_and_preferences(self):
        store=self.seed();archive=self.base/'private.zip'
        # Original WAL writer stays open while taking the backup.
        with patch.object(manage,'ROOT',self.root):manage.backup(archive)
        self.assertEqual(archive.stat().st_mode&0o777,0o600)
        with zipfile.ZipFile(archive) as z:self.assertEqual(set(z.namelist()),{'radar.sqlite3','config.json','manifest.json'})
        target=self.base/'fresh'
        with patch.object(manage,'ROOT',target),patch.object(manage,'loaded',return_value=False):manage.restore(archive)
        db=sqlite3.connect(target/'data/radar.sqlite3')
        try:
            self.assertEqual(db.execute('SELECT count(*) FROM items').fetchone()[0],2)
            self.assertEqual(db.execute('SELECT notify FROM source_preferences').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT notify_state FROM events').fetchone()[0],'historical_silent')
            self.assertIsNone(db.execute("SELECT value FROM meta WHERE key='notification_bridge'").fetchone())
            self.assertEqual(db.execute('SELECT next_due FROM sources').fetchone()[0],0)
            self.assertEqual(db.execute('PRAGMA quick_check').fetchone()[0],'ok')
        finally:db.close();store.db.close()

    def test_restore_refuses_existing_database_without_touching_it(self):
        store=self.seed();archive=self.base/'private.zip'
        with patch.object(manage,'ROOT',self.root):
            manage.backup(archive)
            with self.assertRaisesRegex(ValueError,'Target database exists'):manage.restore(archive)
        self.assertEqual(store.db.execute('SELECT count(*) FROM items').fetchone()[0],2)
        store.db.close()

    def test_corrupt_checksum_and_archive_paths_are_rejected(self):
        store=self.seed();archive=self.base/'private.zip'
        with patch.object(manage,'ROOT',self.root):manage.backup(archive)
        with zipfile.ZipFile(archive) as z:files={k:z.read(k) for k in z.namelist()}
        files['config.json']=b'{}';corrupt=self.base/'bad.zip'
        with zipfile.ZipFile(corrupt,'w') as z:
            for k,v in files.items():z.writestr(k,v)
        with patch.object(manage,'ROOT',self.base/'fresh'):
            self.assertRaisesRegex(ValueError,'checksum',manage.restore,corrupt)
        with zipfile.ZipFile(corrupt,'w') as z:z.writestr('../outside','bad')
        self.assertRaisesRegex(ValueError,'Unexpected backup entries',manage.restore,corrupt)
        self.assertFalse((self.base/'outside').exists());store.db.close()

    def test_launch_specs_are_portable_and_supervise_both_processes(self):
        with patch.object(manage,'ROOT',self.root):specs=manage.job_specs('/usr/bin/python3',True,True)
        collector=specs[manage.LABEL];helper=specs[manage.HELPER]
        self.assertTrue(collector['KeepAlive'])
        self.assertEqual(collector['ProgramArguments'][:2],['/usr/bin/caffeinate','-i'])
        self.assertIn('--no-bridge',collector['ProgramArguments'])
        self.assertEqual(helper['KeepAlive'],{'SuccessfulExit':False})
        self.assertEqual(collector['ExitTimeOut'],50)
        self.assertEqual(plistlib.loads(plistlib.dumps(collector)),collector)

    def test_owned_job_check_does_not_accept_similar_directory_prefix(self):
        agents=self.base/'agents';agents.mkdir()
        bad={'Label':manage.LABEL,'ProgramArguments':['python3',str(self.root)+'-other/radar.py']}
        (agents/(manage.LABEL+'.plist')).write_bytes(plistlib.dumps(bad))
        with patch.object(manage,'ROOT',self.root),patch.object(manage,'AGENTS',agents):
            self.assertRaisesRegex(RuntimeError,'different job',manage.assert_owned,manage.LABEL)

    def test_config_validation_rejects_duplicates_and_preserves_installed_config(self):
        self.assertRaises(ValueError,manage.validate_config,dict(self.config,sources=[self.source,self.source]))
        self.assertRaises(ValueError,manage.validate_config,dict(self.config,port=0))
        with patch.object(manage,'ROOT',self.root):self.assertEqual(manage.read_config(),self.config)

if __name__=='__main__':unittest.main()
