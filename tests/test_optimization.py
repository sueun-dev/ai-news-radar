import gzip
import io
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from http.client import IncompleteRead
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from radar import Radar, Store, fetch_source, item, read_response


class OptimizationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'radar.sqlite'
        self.source=dict(id='a',name='Source',url='https://example.test/feed',kind='feed',interval=60)
        self.config=dict(port=8741,sources=[self.source],x_accounts=[])

    def tearDown(self):self.tmp.cleanup()

    def test_old_background_retry_is_not_starved_by_core_priority(self):
        sources=[dict(self.source,id='core',interval=.01,priority=0),
                 dict(self.source,id='retry',interval=1800,priority=12)]
        radar=Radar(root=Path(self.tmp.name),database=self.path,
                    config=dict(self.config,sources=sources,workers=1,host_gap_seconds=0))
        now=time.time()
        with radar.store.db:
            radar.store.db.execute('UPDATE sources SET next_due=? WHERE id=?',(now,'core'))
            radar.store.db.execute('UPDATE sources SET next_due=? WHERE id=?',(now-180,'retry'))
        order=[]
        def fake(source,previous):
            order.append(source['id']);t=time.time()
            return dict(started=t,finished=t,status=200,headers={},entries=[item('a','Actual story',source['url'])],fetch_ms=0)
        try:
            with patch.object(radar,'fetch',fake):
                thread=threading.Thread(target=radar.run_polls);thread.start()
                time.sleep(1.15);radar.stop.set();thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(order[:2],['retry','core'])
        finally:radar.store.db.close()

    def test_upgrade_keeps_history_and_respects_existing_server_wait(self):
        store=Store(self.path,self.config)
        with store.db:
            store.db.execute("UPDATE sources SET initialized_at=1000,last_success=1000,status='ok',next_due=10000")
            store.db.execute("INSERT INTO checks(source_id,finished_at) VALUES('a',1000)")
        store.db.close()
        # Simulate the original table, before transfer telemetry was introduced.
        db=sqlite3.connect(self.path)
        db.execute('ALTER TABLE checks RENAME TO old_checks')
        db.execute('CREATE TABLE checks AS SELECT id,source_id,started_at,finished_at,status,fetch_ms,item_count,error,cache_control,cache_age,next_due FROM old_checks')
        db.execute('DROP TABLE old_checks');db.commit();db.close()
        reopened=Store(self.path,dict(self.config,sources=[dict(self.source,interval=300)]))
        try:
            self.assertEqual(reopened.source('a')['next_due'],10000)
            self.assertEqual(reopened.db.execute('SELECT count(*) FROM checks').fetchone()[0],1)
            self.assertIn('transferred_bytes',{r['name'] for r in reopened.db.execute('PRAGMA table_info(checks)')})
        finally:reopened.db.close()

    def test_first_collection_also_ages_while_core_keeps_polling(self):
        sources=[dict(self.source,id='core',interval=.01,priority=0),
                 dict(self.source,id='first',interval=1800,priority=12)]
        radar=Radar(root=Path(self.tmp.name),database=self.path,
                    config=dict(self.config,sources=sources,workers=1,host_gap_seconds=0,priority_step_seconds=.05))
        order=[]
        def fake(source,previous):
            order.append(source['id']);t=time.time()
            return dict(started=t,finished=t,status=200,headers={},entries=[item('a','Actual story',source['url'])],fetch_ms=0)
        try:
            with patch.object(radar,'fetch',fake):
                thread=threading.Thread(target=radar.run_polls);thread.start()
                time.sleep(2.15);radar.stop.set();thread.join(3)
            self.assertFalse(thread.is_alive());self.assertIn('first',order)
        finally:radar.store.db.close()

    def test_real_http_gzip_negotiation_and_transfer_telemetry(self):
        body=b'<rss><channel><item><title>AI release</title><link>https://example.test/new</link><description>'+b'text '*3000+b'</description></item></channel></rss>'
        packed=gzip.compress(body);seen=[]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*_):pass
            def do_GET(self):
                seen.append(self.headers.get('Accept-Encoding'));self.send_response(200)
                self.send_header('Content-Encoding','gzip');self.send_header('Content-Length',str(len(packed)))
                self.end_headers();self.wfile.write(packed)
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        store=Store(self.path,self.config)
        try:
            source=dict(self.source,url='http://127.0.0.1:%s/feed'%server.server_port)
            previous=store.source('a');previous['next_due']=time.time()-20
            result=fetch_source(source,previous);store.apply(source,result)
            row=store.db.execute('SELECT * FROM checks').fetchone()
            self.assertEqual(seen,['gzip']);self.assertIsNone(result['error'])
            self.assertEqual(row['transferred_bytes'],len(packed));self.assertEqual(row['decoded_bytes'],len(body))
            self.assertGreaterEqual(row['queue_delay_seconds'],20)
            self.assertEqual(store.source('a')['item_count'],1)
        finally:store.db.close();server.shutdown();server.server_close()

    def test_truncated_response_is_rejected_even_if_xml_is_complete(self):
        class Response:
            headers={'Content-Length':'100'}
            def __init__(self):self.body=io.BytesIO(b'<rss/>')
            def read1(self,size):return self.body.read(size)
        self.assertRaises(IncompleteRead,read_response,Response(),time.monotonic()+10)

    def test_trickling_body_releases_worker_at_read_budget(self):
        class Response:
            headers={}
            def read1(self,size):time.sleep(.03);return b'X'
        self.assertRaises(TimeoutError,read_response,Response(),time.monotonic()+.01)


if __name__=='__main__':unittest.main()
