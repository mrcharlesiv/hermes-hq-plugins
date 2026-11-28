"""Real interprocess watcher lock; no accounts, model calls or network."""
import importlib.util
from contextlib import closing
import os
import sqlite3
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch,Mock

HERE=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('watcher_lock_push',HERE/'push.py')
push=importlib.util.module_from_spec(spec)
sys.modules[spec.name]=push
spec.loader.exec_module(push)


class ReplyWatcherLock(unittest.TestCase):
    def test_bound_local_chat_reply_alerts_but_working_sessions_do_not(self):
        with closing(sqlite3.connect(':memory:')) as db:
            db.executescript('''CREATE TABLE sessions(id TEXT,source TEXT,model_config TEXT);
                CREATE TABLE messages(id INTEGER,session_id TEXT,role TEXT,content TEXT,finish_reason TEXT);''')
            rows=[('local-owner','local','{}'),('cli-history','local','{}'),
                  ('local-child','local','{"_delegate_from":"parent"}'),('cron-run','cron','{}'),
                  ('old-desktop','desktop','{}')]
            db.executemany('INSERT INTO sessions VALUES (?,?,?)',rows)
            db.executemany('INSERT INTO messages VALUES (?,?,?,?,?)',
                           [(i,sid,'assistant','Ready','stop') for i,(sid,*_) in enumerate(rows,1)])
            self.assertEqual([r[1] for r in db.execute(push._REPLIES_SQL,(0,5))],
                             ['local-owner','old-desktop'])

    def test_watcher_starts_once_on_this_platform(self):
        with tempfile.TemporaryDirectory() as folder:
            worker=Mock()
            with patch.object(push,'_watcher_started',False),patch.object(push,'data_dir',return_value=Path(folder)),patch.object(push,'Store'),patch.object(push,'Dispatcher'),patch.object(push,'LiveFeed'),patch.object(push,'ReplyWatcher'),patch.object(push.threading,'Thread',return_value=worker):
                try:
                    self.assertTrue(push.start_reply_watcher(Path(folder)))
                    self.assertFalse(push.start_reply_watcher(Path(folder)))
                    worker.start.assert_called_once()
                finally:
                    lock=vars(worker).get('_push_lock')
                    if lock is not None:lock.close()

    def test_lock_excludes_another_process_then_releases(self):
        child='''import importlib.util,sys
from pathlib import Path
s=importlib.util.spec_from_file_location('child_push',sys.argv[1])
p=importlib.util.module_from_spec(s);sys.modules[s.name]=p;s.loader.exec_module(p)
try:
 lock=p._reply_watcher_lock(Path(sys.argv[2]))
except OSError:
 print('busy')
else:
 lock.close();print('acquired')
'''
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'reply-watcher.lock'
            def contender():
                result=subprocess.run([sys.executable,'-c',child,str(HERE/'push.py'),str(path)],capture_output=True,text=True,timeout=15)
                self.assertEqual(result.returncode,0,result.stderr)
                return result.stdout.strip()
            lock=push._reply_watcher_lock(path)
            try:self.assertEqual(contender(),'busy')
            finally:lock.close()
            self.assertEqual(contender(),'acquired')


if __name__=='__main__':unittest.main()
