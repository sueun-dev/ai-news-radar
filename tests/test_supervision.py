from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import json
import threading
from http.server import ThreadingHTTPServer
from urllib.request import Request,urlopen
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from radar import Radar

class SupervisionTests(unittest.TestCase):
    def test_first_notification_test_is_allowed_near_monotonic_zero(self):
        with tempfile.TemporaryDirectory() as td:
            config=dict(port=0,x_accounts=[],sources=[dict(id='fixture',name='Fixture',kind='feed',
                        url='https://example.test/rss',interval=60)])
            radar=Radar(root=Path(td),config=config,database=Path(td)/'test.sqlite')
            server=ThreadingHTTPServer(('127.0.0.1',0),radar.handler());config['port']=server.server_port
            threading.Thread(target=server.serve_forever,daemon=True).start()
            try:
                with patch('newsradar.web.time.monotonic',return_value=.1):
                    req=Request('http://127.0.0.1:%s/api/test'%server.server_port,data=b'{}',headers={'Content-Type':'application/json'})
                    with urlopen(req) as response:self.assertGreater(json.load(response)['id'],0)
            finally:server.shutdown();server.server_close();radar.store.db.close()

    def test_orderly_stop_does_not_report_a_dead_worker_failure(self):
        with tempfile.TemporaryDirectory() as td:
            config=dict(port=0,x_accounts=[],sources=[dict(id='fixture',name='Fixture',kind='feed',
                        url='https://example.test/rss',interval=60)])
            radar=Radar(root=Path(td),config=config,database=Path(td)/'test.sqlite')
            def orderly():radar.stop.set()
            try:
                with patch.object(radar,'run_polls',side_effect=orderly):radar.serve(False)
            finally:radar.store.db.close()

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
