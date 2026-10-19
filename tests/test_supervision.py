from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from radar import Radar

class SupervisionTests(unittest.TestCase):
    def test_dead_polling_thread_terminates_server_for_supervisor_restart(self):
        with tempfile.TemporaryDirectory() as td:
            config=dict(port=0,x_accounts=[],sources=[dict(id='fixture',name='Fixture',kind='feed',
                        url='https://example.test/rss',interval=60)])
            radar=Radar(root=Path(td),config=config,database=Path(td)/'test.sqlite')
            try:
                with patch.object(radar,'run_polls',return_value=None):
                    self.assertRaisesRegex(RuntimeError,'Polling worker stopped',radar.serve,False)
                self.assertTrue(radar.stop.is_set())
            finally:radar.store.db.close()

if __name__=='__main__':unittest.main()
