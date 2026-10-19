import json
from pathlib import Path
import sys,tempfile,threading,time,unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from radar import Store,Radar,item,iso,parse_source,AI_TERMS,canonical_url


class ExpansionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'test.sqlite'
        self.a=dict(id='a',name='Alpha Official',url='https://a.test/feed',kind='feed',group='official',interval=60)
        self.b=dict(id='b',name='Beta Papers',url='https://b.test/feed',kind='feed',group='research',interval=60,notify=False)
        self.config=dict(sources=[self.a,self.b],port=8741,x_accounts=['OpenAI'])
        self.store=Store(self.path,self.config)
    def tearDown(self):self.store.db.close();self.tmp.cleanup()
    def apply(self,source,items,t,**kw):
        result=dict(started=t-.1,finished=t,status=200,headers={},entries=items,fetch_ms=100)
        result.update(kw);return self.store.apply(source,result)

    def test_rdf_relative_links_and_namespaced_dates(self):
        xml=b'<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns="http://purl.org/rss/1.0/" xmlns:dc="http://purl.org/dc/elements/1.1/"><item><title>AI release</title><link>/new/</link><dc:date>2026-10-19T00:00:00Z</dc:date></item></rdf:RDF>'
        rows=parse_source(self.a,xml);self.assertEqual(len(rows),1);self.assertEqual(rows[0]['precision'],'instant')
        self.assertEqual(rows[0]['url'],'https://a.test/new')

    def test_more_than_sixty_entries_are_collected(self):
        xml=('<rss><channel>'+''.join('<item><title>AI %s</title><link>https://a.test/%s</link></item>'%(i,i) for i in range(250))+'</channel></rss>').encode()
        self.assertEqual(len(parse_source(self.a,xml)),250)

    def test_ai_match_uses_individual_body_and_word_boundaries(self):
        self.assertFalse(AI_TERMS.search('chair rail retailer'))
        self.assertTrue(item('a','New product','https://a.test/a','A large language model')['ai_match'])
        self.assertFalse(item('b','New phone','https://a.test/b','a chair')['ai_match'])

    def test_normalization_removes_only_tracking_and_preserves_identity(self):
        self.assertEqual(canonical_url('https://A.test/new/?id=2&utm_source=x#section'),'https://a.test/new?id=2#section')

    def test_verified_link_pattern_handles_trailing_slash_and_ignores_navigation(self):
        source=dict(self.a,kind='links',url='https://a.test/blog',link_pattern=r'^https://a.test/blog/[^/?#]+$')
        xml=b'<nav><a href="/blog/old">Old navigation story</a></nav><a href="/blog/new/">Actual new AI story</a><a href="/pricing">Pricing</a>'
        rows=parse_source(source,xml);self.assertEqual([x['url'] for x in rows],['https://a.test/blog/new'])

    def test_empty_anchor_fallback_is_explicit(self):
        source=dict(self.a,kind='links',url_title_fallback=True,link_pattern=r'^https://a.test/blog/[^/?#]+$')
        rows=parse_source(source,b'<a href="/blog/new"></a>')
        self.assertEqual(rows[0]['title'],'[제목 미제공] /blog/new')

    def test_sitemap_timestamp_is_not_a_publication(self):
        source=dict(self.a,kind='sitemap',link_pattern=r'/news/')
        xml=b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://a.test/pricing</loc></url><url><loc>https://a.test/news/new</loc><lastmod>2026-10-19T00:00:00Z</lastmod></url></urlset>'
        rows=parse_source(source,xml);self.assertEqual(len(rows),1);self.assertEqual(rows[0]['basis'],'sitemap_modified')

    def test_model_metadata_ignores_popularity_counters(self):
        source=dict(self.a,kind='hf_models',author='lab')
        data=[dict(id='lab/new-model',createdAt='2026-10-19T00:00:00Z',likes=1,downloads=10)]
        first=parse_source(source,json.dumps(data).encode())[0]
        data[0]['likes']=100;data[0]['downloads']=1000
        second=parse_source(source,json.dumps(data).encode())[0]
        self.assertEqual(first['content_hash'],second['content_hash'])
        self.assertEqual(first['basis'],'repository_created')
        self.assertEqual(first['url'],'https://huggingface.co/lab/new-model')

    def test_unrelated_or_private_model_metadata_is_ignored(self):
        source=dict(self.a,kind='hf_models',author='lab')
        data=[dict(id='other/model'),dict(id='lab/private',private=True),dict(id='lab/a/b')]
        self.assertEqual(parse_source(source,json.dumps(data).encode()),[])

    def test_filtered_zero_and_legitimate_empty_do_not_fail(self):
        self.apply(self.a,[item('old','Old','https://a.test/old')],1000)
        filtered=dict(self.a,filter_ai=True)
        self.apply(filtered,[],1010,parsed_count=20)
        self.assertEqual(self.store.source('a')['status'],'ok')
        self.apply(self.a,[],1020,valid_empty_feed=True)
        self.assertEqual(self.store.source('a')['last_success'],1020)

    def test_cache_ttl_does_not_prevent_conditional_revalidation(self):
        self.apply(self.a,[item('old','Old','https://a.test/old')],1000,headers={'cache-control':'max-age=86400'})
        self.assertEqual(self.store.source('a')['next_due'],1060)
        self.apply(dict(self.a,respect_cache_ttl=True),[],1010,status=304,headers={'cache-control':'max-age=86400'})
        self.assertEqual(self.store.source('a')['next_due'],87410)

    def test_transport_retry_is_prompt_but_exponential(self):
        self.apply(self.a,[],1000,status=None,error='connection timeout',error_kind='transport')
        self.assertEqual(self.store.source('a')['next_due'],1015)
        self.apply(self.a,[],1015,status=None,error='connection timeout',error_kind='transport')
        self.assertEqual(self.store.source('a')['next_due'],1045)

    def test_preferences_survive_restart_and_do_not_stop_collection(self):
        self.apply(self.b,[item('old','Old','https://b.test/old')],1000)
        self.apply(self.b,[item('one','One','https://b.test/one')],1001)
        self.assertEqual(self.store.state()['events'][0]['notify_state'],'source_silent')
        self.store.set_notification('b',True)
        reopened=Store(self.path,self.config)
        self.assertTrue(next(x for x in reopened.state()['sources'] if x['id']=='b')['notify']);reopened.db.close()
        self.apply(self.b,[item('two','Two','https://b.test/two')],1002)
        self.assertEqual(self.store.state()['events'][0]['notify_state'],'pending')
        self.assertRaises(ValueError,self.store.set_notification,'unknown',True)

    def test_library_filters_deduplicates_urls_and_paginates(self):
        rows=[item(str(i),'AI title %s'%i,'https://a.test/%s'%i) for i in range(5)]
        self.apply(self.a,rows,1000);self.apply(self.b,rows[:1],1001)
        page=self.store.library(limit=2);self.assertEqual(page['total'],5);self.assertEqual(len(page['items']),2)
        self.assertEqual(len(self.store.library(offset=2,limit=2)['items']),2)
        self.assertEqual(self.store.library(group='research')['total'],1)
        self.assertEqual(self.store.library(query='Alpha Official')['total'],5)
        self.assertEqual(self.store.library(query='%')['total'],0)
        self.assertEqual(self.store.library(group='unknown')['total'],0)

    def test_aggregator_clock_is_not_in_precision_statistics(self):
        self.apply(self.a,[item('old','Old','https://a.test/old')],1000)
        self.apply(self.a,[item('new','News','https://a.test/new',raw_time=iso(1005),basis='index_published')],1006)
        self.assertEqual(self.store.state()['metrics']['precise_samples'],0)
        self.assertEqual(self.store.state()['events'][0]['measurement_note'],'not_original_publication_time')

    def test_old_rediscovery_is_logged_without_stale_alert(self):
        self.apply(self.a,[item('old','Old','https://a.test/old')],1000)
        self.apply(self.a,[item('ancient','Ancient','https://a.test/ancient',raw_time=iso(100),basis='published')],1010)
        self.assertEqual(self.store.state()['events'][0]['notify_state'],'historical_silent')

    def test_community_score_changes_do_not_create_news_events(self):
        source=dict(self.b,detect_updates=False)
        self.apply(source,[item('old','Old','https://b.test/old','one point')],1000)
        self.apply(source,[item('old','Old','https://b.test/old','two points')],1010)
        self.assertEqual(self.store.state()['events'],[])

    def test_slow_host_does_not_block_fast_host(self):
        sources=[dict(self.a,interval=.1),dict(self.b,interval=.1)]
        config=dict(self.config,sources=sources,workers=2,host_gap_seconds=0)
        radar=Radar(root=Path(self.tmp.name),config=config,database=Path(self.tmp.name)/'schedule.sqlite')
        counts={'a':0,'b':0};lock=threading.Lock()
        def fetch(source,previous):
            with lock:counts[source['id']]+=1
            started=time.time()
            if source['id']=='b':time.sleep(1.8)
            return dict(started=started,finished=time.time(),status=200,headers={},entries=[item('key','AI story',source['url'])],fetch_ms=1)
        with patch('radar.fetch_source',fetch):
            thread=threading.Thread(target=radar.run_polls);thread.start();time.sleep(1.65);radar.stop.set();thread.join(4)
        self.assertFalse(thread.is_alive());self.assertGreaterEqual(counts['a'],2);self.assertEqual(counts['b'],1)
        radar.store.db.close()

    def test_core_priority_is_not_starved_by_older_same_host_work(self):
        sources=[dict(self.a,id='bg1',interval=1000,priority=10),dict(self.a,id='bg2',interval=1000,priority=10),dict(self.a,id='core',interval=1000,priority=0)]
        cfg=dict(self.config,sources=sources,workers=1,host_gap_seconds=0)
        radar=Radar(root=Path(self.tmp.name),config=cfg,database=Path(self.tmp.name)/'priority.sqlite')
        with radar.store.db:radar.store.db.execute("UPDATE sources SET next_due=? WHERE id='core'",(time.time()+.2,))
        order=[]
        def fetch(source,previous):
            order.append(source['id']);now=time.time()
            return dict(started=now,finished=now,status=200,headers={},entries=[item('a','Story','https://a.test/a')],fetch_ms=0)
        with patch('radar.fetch_source',fetch):
            thread=threading.Thread(target=radar.run_polls);thread.start();time.sleep(1.15);radar.stop.set();thread.join(3)
        self.assertEqual(order[:2],['bg1','core']);radar.store.db.close()

if __name__=='__main__':unittest.main()
