#!/usr/bin/env python3
"""Local public-source monitor. Python stdlib only; no credentials or paid APIs."""
import argparse
import fcntl
import gzip
import csv
import hashlib
import io
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import signal
import ssl
from http.client import HTTPException, IncompleteRead
import sqlite3
import statistics
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import wait, FIRST_COMPLETED
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.parse import urljoin, urlsplit, urlunsplit, parse_qsl, urlencode
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parent
UTC = timezone.utc
ATOM = "{http://www.w3.org/2005/Atom}"
LOG = logging.getLogger("radar")
TLS_CONTEXT = ssl.create_default_context()


def iso(ts=None):
    return datetime.fromtimestamp(time.time() if ts is None else ts, UTC).isoformat(timespec="milliseconds")


def parse_time(value):
    if not value:
        return None, "unknown"
    value = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return None, "date"
    try:
        d = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            d = parsedate_to_datetime(value)
        except (ValueError, TypeError, OverflowError):
            return None, "unknown"
    if d.tzinfo is None:
        return None, "timezone_unknown"
    return d.timestamp(), "instant"


def clean(text):
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]*>", " ", text or ""))).strip()


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


AI_TERMS = re.compile(r"\b(?:AI|AIs|AGI|ASI|LLMs?|GPT[\w.-]*|ChatGPT|Claude|Gemini|Anthropic|OpenAI|DeepSeek|Qwen|Llama|Mistral|Copilot|NVIDIA|CUDA|TPUs?|GPUs?|MLX|PyTorch|TensorFlow|vLLM|SGLang|Ollama|Hugging\s*Face|generative|diffusion|transformers?|robotics?|neural|reinforcement learning|machine learning|deep learning|large language|foundation models?|inference|text.to.video|text.to.image|language models?)\b|인공지능|생성형|챗GPT|챗지피티|오픈AI|앤트로픽|클로드|제미나이|딥시크|엔비디아|머신러닝|딥러닝|거대언어|언어모델|人工智能|大模型|生成AI|生成ＡＩ|大規模言語", re.I)
AI_TERMS = re.compile(AI_TERMS.pattern+r'|\b(?:Perplexity|Manus|Kimi|Grok|MiniMax|EXAONE|Nemotron|LoRA|RAG|VLMs?|multimodal|fine.tuning|retrieval.augmented)\b|\bA\.I\.?(?=\W|$)|에이전트|프롬프트|멀티모달|추론모델|파운데이션모델|智谱|通义千问',re.I)


def canonical_url(url):
    p = urlsplit(url.strip())
    query = [(k,v) for k,v in parse_qsl(p.query, keep_blank_values=True)
             if not k.lower().startswith("utm_") and k.lower() not in ("fbclid", "gclid", "mc_cid", "mc_eid")]
    path = p.path.rstrip("/") or "/"
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), path, urlencode(query), p.fragment))


def item(key, title, url, body="", raw_time=None, basis="unknown"):
    ts, precision = parse_time(raw_time)
    return dict(key=key, title=clean(title)[:300], url=url, content_hash=digest(clean(body)),
                source_time=ts, source_time_raw=raw_time, precision=precision, basis=basis,
                ai_match=bool(AI_TERMS.search(clean(title)+' '+clean(body)+' '+url)))


class NewsLinks(HTMLParser):
    def __init__(self, base):
        super().__init__()
        self.base, self.active, self.parts, self.found = base, None, [], {}

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "a":
            href = a.get("href", "")
            if re.match(r"^(?:https://www\.anthropic\.com)?/news/[^?#]+", href):
                self.active, self.parts = urljoin(self.base, href).split("#")[0], []

    def handle_data(self, data):
        if self.active:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self.active:
            title = clean(" ".join(self.parts))
            if title and len(title) > len(self.found.get(self.active, "")):
                self.found[self.active] = title
            self.active, self.parts = None, []


class DatedSections(HTMLParser):
    def __init__(self):
        super().__init__()
        self.skip = 0
        self.heading, self.heading_parts, self.heading_id = False, [], ""
        self.current, self.parts, self.found = None, [], []

    def flush(self):
        if self.current:
            self.found.append((self.current, clean(" ".join(self.parts))))

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        if self.skip:
            return
        if tag == "h2":
            self.flush()
            self.current, self.parts = None, []
            self.heading, self.heading_parts = True, []
            self.heading_id = dict(attrs).get("id", "")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.skip:
            self.skip -= 1
            return
        if self.skip:
            return
        if tag == "h2" and self.heading:
            title = clean(" ".join(self.heading_parts))
            if re.search(r"\b20\d{2}\b", title) and re.search(
                    r"January|February|March|April|May|June|July|August|September|October|November|December|\d{4}-\d{2}", title):
                self.current = (self.heading_id or digest(title)[:12], title)
            self.heading = False

    def handle_data(self, data):
        if self.skip:
            return
        if self.heading:
            self.heading_parts.append(data)
        elif self.current:
            self.parts.append(data)


class LinkIndex(HTMLParser):
    """Extract only the source-specific article URL pattern, never arbitrary page text."""
    def __init__(self, source):
        super().__init__()
        self.source, self.active, self.parts, self.found = source, None, [], {}
        self.pattern = re.compile(source["link_pattern"])
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        a=dict(attrs)
        if tag in (("script","style") if self.source.get('include_navigation') else ("script", "style", "nav", "footer")):self.skip+=1
        if tag=="a" and not self.skip:
            url=canonical_url(urljoin(self.source["url"],a.get("href","")))
            p=urlsplit(url)
            if p.scheme in ("http","https") and self.pattern.search(url):
                self.active,self.parts=canonical_url(url.split("#")[0]),[]
                if a.get("aria-label"):self.parts.append(a["aria-label"])

    def handle_endtag(self,tag):
        if tag=="a" and self.active:
            title=clean(" ".join(self.parts))
            if self.source.get('url_title_fallback') and len(title)<self.source.get('min_title_length',12):
                title='[제목 미제공] '+urlsplit(self.active).path
            if len(title)>=self.source.get("min_title_length",12) and title.lower() not in ("read more","learn more","view details"):
                if len(title)>len(self.found.get(self.active,"")):self.found[self.active]=title
            self.active,self.parts=None,[]
        if tag in (("script","style") if self.source.get('include_navigation') else ("script","style","nav","footer")) and self.skip:self.skip-=1

    def handle_data(self,data):
        if self.active and not self.skip:self.parts.append(data)


def parse_source(source, data):
    kind, base = source["kind"], source["url"]
    if kind=='hf_models':
        rows=json.loads(data)
        if not isinstance(rows,list):raise ValueError('Expected public model list')
        out=[]
        for r in rows:
            mid=r.get('id','')
            if not re.fullmatch(r'[\w.-]+/[\w.-]+',mid) or r.get('private'):continue
            if mid.split('/')[0].lower()!=source['author'].lower():continue
            out.append(item(mid,mid,'https://huggingface.co/'+mid,mid,r.get('createdAt'),'repository_created'))
        return out
    if kind == 'sitemap':
        root=ET.fromstring(data);pattern=re.compile(source['link_pattern']);out=[]
        for e in root:
            if e.tag.split('}')[-1]!='url':continue
            values={c.tag.split('}')[-1]:c.text or '' for c in e}
            url=canonical_url(values.get('loc',''))
            if urlsplit(url).scheme in ('http','https') and pattern.search(url):
                out.append(item(url,'[사이트맵 · 제목 미제공] '+urlsplit(url).path,url,url,
                                values.get('lastmod'),'sitemap_modified'))
        return out
    if kind == "feed":
        root = ET.fromstring(data)
        out = []
        entries = [e for e in root.iter() if e.tag.split("}")[-1] in ("item", "entry")]
        for e in entries[:source.get("max_items", 2000)]:
            atom = e.tag.startswith(ATOM)
            prefix = ATOM if atom else ""
            def val(name):
                child=next((c for c in e if c.tag.split("}")[-1]==name),None)
                return "".join(child.itertext()) if child is not None else ""
            if atom:
                links = [x.attrib.get("href", "") for x in e.findall(ATOM + "link")
                         if x.attrib.get("rel", "alternate") == "alternate"]
                url = links[0] if links else ""
                raw = val("published") or val("updated")
                basis = "published" if val("published") else "feed_updated"
            else:
                url, raw, basis = val("link"), val("pubDate") or val("date"), "published"
                if not url and val("guid").startswith(("https://","http://")):url=val("guid")
            basis=source.get('time_basis',basis)
            url=urljoin(base,url)
            if urlsplit(url).scheme not in ("https", "http"):
                continue
            # Stable URL deduplicates feed GUID churn. Title/content changes remain visible.
            url=canonical_url(urljoin(base,url))
            out.append(item(url, val("title"), url, val("content") or val("encoded") or val("description") or val("summary"),
                            raw, basis))
        return out
    text = data.decode("utf-8", errors="replace")
    if kind == "anthropic":
        parser = NewsLinks(base)
        parser.feed(text)
        return [item(url, title, url, title, basis="date_or_unknown") for url, title in parser.found.items()]
    if kind == "links":
        parser=LinkIndex(source);parser.feed(text)
        return [item(url,title,url,title,basis="date_or_unknown") for url,title in parser.found.items()]
    if kind == "changelog":
        parser = DatedSections()
        parser.feed(text)
        parser.flush()
        return [item(key, "Gemini API · " + title, base + "#" + key, body, basis="date_only")
                for (key, title), body in parser.found[:12]]
    raise ValueError("Unknown source kind")


def retry_after(headers, now):
    raw = headers.get("retry-after", "")
    try:
        return max(0.0, float(raw))
    except ValueError:
        ts, _ = parse_time(raw)
        return max(0.0, ts - now) if ts else 0.0


def cache_delay(headers):
    cc = headers.get("cache-control", "").lower()
    if any(x in cc for x in ("no-store", "no-cache")):
        return 0.0
    m = re.search(r"(?:^|,)\s*max-age\s*=\s*\"?(\d+)", cc)
    if m:
        return max(0, int(m.group(1)) - float(headers.get("age", "0") or 0))
    # HTTP Expires is considered only when no Cache-Control max-age exists.
    exp, _ = parse_time(headers.get("expires"))
    date, _ = parse_time(headers.get("date"))
    return max(0, exp - (date or time.time())) if exp else 0.0


class Store:
    def __init__(self, path, config):
        self.config, self.lock = config, threading.RLock()
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS sources(
          id TEXT PRIMARY KEY,name TEXT,url TEXT,kind TEXT,interval REAL,
          initialized_at REAL,last_checked REAL,last_success REAL,next_due REAL DEFAULT 0,
          status TEXT DEFAULT 'starting',http_status INTEGER,error TEXT,failures INTEGER DEFAULT 0,
          etag TEXT,last_modified TEXT,cache_control TEXT,cache_age TEXT,fetch_ms REAL,item_count INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS items(
          source_id TEXT,key TEXT,title TEXT,url TEXT,content_hash TEXT,first_seen REAL,last_seen REAL,
          source_time REAL,source_time_raw TEXT,precision TEXT,basis TEXT,PRIMARY KEY(source_id,key));
        CREATE TABLE IF NOT EXISTS events(
          id INTEGER PRIMARY KEY,source_id TEXT,title TEXT,url TEXT,kind TEXT,
          detected_at REAL,source_time REAL,source_time_raw TEXT,precision TEXT,basis TEXT,
          delay_seconds REAL,observation_gap_seconds REAL,measurement_note TEXT,
          is_test INTEGER DEFAULT 0,notify_state TEXT DEFAULT 'pending',notify_enqueued_at REAL,
          notify_error TEXT,opened_at REAL);
        CREATE TABLE IF NOT EXISTS checks(
          id INTEGER PRIMARY KEY,source_id TEXT,started_at REAL,finished_at REAL,status INTEGER,
          fetch_ms REAL,item_count INTEGER,error TEXT,cache_control TEXT,cache_age TEXT,next_due REAL);
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT);
        CREATE TABLE IF NOT EXISTS source_preferences(source_id TEXT PRIMARY KEY,notify INTEGER NOT NULL);
        CREATE INDEX IF NOT EXISTS events_url_time ON events(url,detected_at);
        CREATE INDEX IF NOT EXISTS checks_finished ON checks(finished_at);
        CREATE INDEX IF NOT EXISTS items_time ON items(source_time,first_seen);
        """)
        # Add telemetry without replacing any existing observations or event history.
        check_columns={r['name'] for r in self.db.execute('PRAGMA table_info(checks)')}
        for name,kind in [('queue_delay_seconds','REAL'),('transferred_bytes','INTEGER'),
                          ('decoded_bytes','INTEGER'),('content_encoding','TEXT')]:
            if name not in check_columns:self.db.execute('ALTER TABLE checks ADD COLUMN '+name+' '+kind)
        with self.lock, self.db:
            for s in config["sources"]:
                old=self.db.execute('SELECT interval,last_success,status FROM sources WHERE id=?',(s['id'],)).fetchone()
                self.db.execute("""INSERT INTO sources(id,name,url,kind,interval) VALUES(?,?,?,?,?)
                  ON CONFLICT(id) DO UPDATE SET name=excluded.name,url=excluded.url,
                  kind=excluded.kind,interval=excluded.interval""",
                                (s["id"], s["name"], s["url"], s["kind"], s["interval"]))
                if old and old['interval']!=s['interval'] and old['status']=='ok' and old['last_success']:
                    self.db.execute('UPDATE sources SET next_due=MAX(next_due,?) WHERE id=?',
                                    (old['last_success']+s['interval'],s['id']))
            if not self.db.execute("SELECT 1 FROM meta WHERE key='canonical_v2'").fetchone():
                for old in list(self.db.execute('SELECT source_id,key,url FROM items')):
                    key=canonical_url(old['key']) if old['key'].startswith(('http://','https://')) else old['key']
                    url=canonical_url(old['url'])
                    if key!=old['key']:
                        exists=self.db.execute('SELECT 1 FROM items WHERE source_id=? AND key=?',(old['source_id'],key)).fetchone()
                        if exists:self.db.execute('DELETE FROM items WHERE source_id=? AND key=?',(old['source_id'],old['key']));continue
                    self.db.execute('UPDATE items SET key=?,url=? WHERE source_id=? AND key=?',(key,url,old['source_id'],old['key']))
                for e in list(self.db.execute('SELECT id,url FROM events')):
                    self.db.execute('UPDATE events SET url=? WHERE id=?',(canonical_url(e['url']),e['id']))
                self.set_meta('canonical_v2',True)
            self.set_meta("service_started", iso())
            self.set_meta("pid", os.getpid())

    def set_meta(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, json.dumps(value, ensure_ascii=False)))

    def source(self, sid):
        with self.lock:
            return dict(self.db.execute("SELECT * FROM sources WHERE id=?", (sid,)).fetchone())

    def process(self, source, entries, now, previous):
        baseline = previous["initialized_at"] is None
        created = []
        for it in entries:
            old = self.db.execute("SELECT * FROM items WHERE source_id=? AND key=?",
                                  (source["id"], it["key"])).fetchone()
            changed = old is not None and (old["content_hash"] != it["content_hash"] or old["title"] != it["title"])
            if not baseline and (old is None or changed and source.get('detect_updates',True)):
                delay, note = None, "source_time_unknown"
                source_ts = it["source_time"]
                if changed and (old["source_time"] == source_ts or it["basis"] == "published"):
                    note = "edit_without_new_precise_timestamp"
                elif it.get('basis') in ('index_published','community_posted','batch_announcement','sitemap_modified','repository_created'):
                    note = 'not_original_publication_time'
                elif it["precision"] == "instant" and source_ts is not None:
                    if source_ts > now:
                        note = "source_clock_in_future"
                    elif source_ts < previous["initialized_at"]:
                        note = "older_than_initial_baseline"
                    else:
                        delay, note = now - source_ts, it["basis"]
                elif it["basis"] in ("date_only", "date_or_unknown"):
                    note = "date_only_or_unavailable"
                gap = now - previous["last_success"] if previous["last_success"] else None
                duplicate = old is None and self.db.execute(
                    "SELECT id FROM events WHERE url=? AND is_test=0 AND detected_at>? LIMIT 1",
                    (it["url"], now - 86400)).fetchone()
                prerelease = (source["id"] == "codex" or source.get('group')=='releases') and bool(re.search(r"alpha|beta|rc[.\d]|dev[.\d]", it["title"], re.I))
                pref=self.db.execute("SELECT notify FROM source_preferences WHERE source_id=?",(source['id'],)).fetchone()
                enabled=bool(pref[0]) if pref is not None else source.get("notify",True)
                stale_new=old is None and source_ts is not None and source_ts<previous['initialized_at']-300
                notify = "historical_silent" if stale_new else ("duplicate" if duplicate else ("source_silent" if not enabled else (
                    "prerelease_silent" if prerelease and not self.config.get("notify_prerelease") else "pending")))
                cursor = self.db.execute("""INSERT INTO events(
                  source_id,title,url,kind,detected_at,source_time,source_time_raw,precision,basis,
                  delay_seconds,observation_gap_seconds,measurement_note,notify_state)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (source["id"], it["title"], it["url"], "backfill" if stale_new else ("updated" if changed else "new"), now,
                   source_ts, it["source_time_raw"], it["precision"], it["basis"], delay, gap, note, notify))
                created.append(cursor.lastrowid)
            self.db.execute("""INSERT INTO items VALUES(?,?,?,?,?,?,?,?,?,?,?)
              ON CONFLICT(source_id,key) DO UPDATE SET title=excluded.title,url=excluded.url,
              content_hash=excluded.content_hash,last_seen=excluded.last_seen,
              source_time=excluded.source_time,source_time_raw=excluded.source_time_raw,
              precision=excluded.precision,basis=excluded.basis""",
              (source["id"], it["key"], it["title"], it["url"], it["content_hash"], now, now,
               it["source_time"], it["source_time_raw"], it["precision"], it["basis"]))
        return created

    def apply(self, source, result):
        now, status = result["finished"], result["status"]
        with self.lock, self.db:
            prev = self.source(source["id"])
            headers = result["headers"]
            error = result.get("error")
            entries = result.get("entries", [])
            if status == 200 and not entries and not (source.get("filter_ai") and result.get("parsed_count",0)>0) and not result.get('valid_empty_feed'):
                error = error or "Parsed zero entries; baseline/state was preserved."
            ok = not error and (status == 200 or status == 304 and prev["initialized_at"] is not None)
            failures = 0 if ok else prev["failures"] + 1
            # Cache freshness permits reuse; it is not a prohibition on revalidation.
            ttl=cache_delay(headers) if source.get('respect_cache_ttl',False) else 0
            interval = max(source["interval"], ttl, float(headers.get("x-poll-interval", "0") or 0))
            if not ok:
                if result.get('error_kind')=='transport' or status is None:
                    interval=max(min(900,15*2**min(failures-1,6)),retry_after(headers,now),float(headers.get('x-poll-interval','0') or 0))
                else:
                    interval = max(interval, min(3600, source["interval"] * 2 ** min(failures, 7)),
                                   retry_after(headers, now))
            next_due = now + interval
            created = self.process(source, entries, now, prev) if ok and status == 200 else []
            self.db.execute("""UPDATE sources SET initialized_at=?,last_checked=?,last_success=?,
              next_due=?,status=?,http_status=?,error=?,failures=?,etag=?,last_modified=?,
              cache_control=?,cache_age=?,fetch_ms=?,item_count=? WHERE id=?""",
              (prev["initialized_at"] or (now if ok else None), now, now if ok else prev["last_success"],
               next_due, "ok" if ok else "error", status, error, failures,
               headers.get("etag", prev["etag"]) if ok else prev["etag"],
               headers.get("last-modified", prev["last_modified"]) if ok else prev["last_modified"],
               headers.get("cache-control", prev["cache_control"]), headers.get("age"),
               result["fetch_ms"], len(entries) if status == 200 and ok else prev["item_count"], source["id"]))
            self.db.execute("INSERT INTO checks(source_id,started_at,finished_at,status,fetch_ms,item_count,error,cache_control,cache_age,next_due,queue_delay_seconds,transferred_bytes,decoded_bytes,content_encoding) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (source["id"], result["started"], now, status, result["fetch_ms"], len(entries),
                             error, headers.get("cache-control"), headers.get("age"), next_due,
                             result.get('queue_delay_seconds'),result.get('transferred_bytes'),
                             result.get('decoded_bytes'),headers.get('content-encoding')))
            # Bound raw polling-log growth; events and item history are retained.
            self.db.execute("DELETE FROM checks WHERE finished_at<?", (now - 30 * 86400,))
            LOG.info("%s status=%s items=%s new_events=%s next=%.1fs error=%s",
                     source["id"], status, len(entries), len(created), interval, error)
            return created

    def test_event(self):
        now = time.time()
        with self.lock, self.db:
            cursor = self.db.execute("""INSERT INTO events(
              source_id,title,url,kind,detected_at,source_time,precision,basis,
              measurement_note,is_test,notify_state) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
              ("self-test", "자체 시험 알림 — 실제 AI 소식이 아닙니다", "http://127.0.0.1:%s/" % self.config["port"],
               "test", now, None, "unknown", "synthetic", "synthetic_excluded", 1, "pending"))
            return cursor.lastrowid

    def pending(self):
        with self.lock:
            return [dict(r) for r in self.db.execute(
                "SELECT * FROM events WHERE notify_state='pending' ORDER BY id LIMIT 10")]

    def notification_result(self, eid, state, error=None):
        if state not in ("accepted", "denied", "error"):
            raise ValueError("invalid notification state")
        with self.lock, self.db:
            self.db.execute("""UPDATE events SET notify_state=?,notify_enqueued_at=?,
              notify_error=? WHERE id=? AND notify_state='pending'""", (state, time.time(), error, eid))

    def record_x(self, url, raw_time):
        p = urlsplit(url)
        allowed = {x.lower() for x in self.config["x_accounts"]}
        parts = p.path.strip("/").split("/")
        if p.scheme != "https" or p.hostname not in ("x.com", "www.x.com", "twitter.com") or (
                len(parts) != 3 or parts[0].lower() not in allowed or parts[1] != "status" or not parts[2].isdigit()):
            raise ValueError("설정된 공식 계정의 https://x.com/계정/status/숫자 URL만 입력하세요.")
        now = time.time()
        ts, precision = parse_time(raw_time)
        if raw_time and (precision != "instant" or ts > now):
            raise ValueError("공개 시각은 시간대가 포함된 과거 ISO 시각이어야 합니다.")
        with self.lock, self.db:
            c = self.db.execute("""INSERT INTO events(source_id,title,url,kind,detected_at,source_time,
              source_time_raw,precision,basis,delay_seconds,measurement_note,notify_state)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
              ("x-manual", "X 수동 도착 기록 · @" + parts[0], url, "manual", now, ts, raw_time,
               precision, "manual_input", now - ts if ts else None,
               "manual_record_time_not_push_receipt", "manual"))
            return c.lastrowid

    def state(self):
        now = time.time()
        with self.lock:
            sources = [dict(r) for r in self.db.execute("SELECT * FROM sources ORDER BY name")]
            configured={s['id']:s for s in self.config['sources']}
            preferences={r['source_id']:bool(r['notify']) for r in self.db.execute('SELECT * FROM source_preferences')}
            sources=[s for s in sources if s['id'] in configured]
            for s in sources:
                c=configured[s['id']]
                s.update(group=c.get('group','official'),region=c.get('region','global'),
                         notify=preferences.get(s['id'],c.get('notify',True)),filter_ai=c.get('filter_ai',False))
            events = [dict(r) for r in self.db.execute("SELECT * FROM events ORDER BY id DESC LIMIT 300")]
            precise = [dict(r) for r in self.db.execute("""SELECT delay_seconds,basis,observation_gap_seconds FROM events
              WHERE is_test=0 AND source_id!='x-manual' AND delay_seconds IS NOT NULL""")]
            by_basis = {}
            for basis in ("published", "feed_updated"):
                values = sorted(x["delay_seconds"] for x in precise if x["basis"] == basis)
                by_basis[basis] = {"count": len(values),
                                   "median": statistics.median(values) if values else None,
                                   "p95": values[max(0, int(len(values)*.95+.999)-1)] if values else None}
            meta = {r["key"]: json.loads(r["value"]) for r in self.db.execute("SELECT * FROM meta")}
            x_path = ROOT / "data" / "x-setup.json"
            x_setup = json.loads(x_path.read_text()) if x_path.exists() else {"status": "not_verified", "accounts": []}
            groups={g:sum(s['group']==g for s in sources) for g in {s['group'] for s in sources}}
            item_count=self.db.execute('SELECT count(*) FROM items').fetchone()[0]
            return dict(now=now, sources=sources, events=events, meta=meta, x_setup=x_setup,
                        coverage=dict(groups=groups,item_count=item_count,
                                      active_sources=len(sources),scope='Configured public sources only; completeness is not guaranteed'),
                        x_accounts=self.config["x_accounts"], metrics={
                            "by_basis": by_basis,
                            "precise_samples": len(precise),
                            "published_samples": sum(x["basis"] == "published" for x in precise),
                            "updated_samples": sum(x["basis"] == "feed_updated" for x in precise),
                            "long_observation_samples":sum((x['observation_gap_seconds'] or 0)>300 for x in precise),
                            "median_seconds": statistics.median([x["delay_seconds"] for x in precise]) if precise else None,
                            "p95_seconds": sorted(x["delay_seconds"] for x in precise)[max(0, int(len(precise)*.95+.999)-1)] if precise else None})

    def export(self, table):
        if table not in ('events','checks','items'):raise ValueError('Invalid export')
        with self.lock:
            rows = self.db.execute("SELECT * FROM " + table + (" ORDER BY first_seen DESC" if table=='items' else " ORDER BY id DESC"))
            out = io.StringIO()
            writer = csv.writer(out)
            writer.writerow([d[0] for d in rows.description])
            for row in rows:
                # Keep spreadsheet applications from interpreting untrusted titles as formulas.
                writer.writerow(["'" + v if isinstance(v, str) and v.startswith(("=", "+", "-", "@")) else v for v in row])
            return ("\ufeff" + out.getvalue()).encode("utf-8")

    def set_notification(self,sid,enabled):
        if sid not in {s['id'] for s in self.config['sources']} or not isinstance(enabled,bool):
            raise ValueError('Unknown source or invalid notification value')
        with self.lock,self.db:
            self.db.execute('INSERT OR REPLACE INTO source_preferences VALUES(?,?)',(sid,int(enabled)))

    def library(self,query='',group='',offset=0,limit=50):
        configured={s['id']:s for s in self.config['sources']}
        ids=[sid for sid,s in configured.items() if not group or s.get('group','official')==group]
        if not ids:return dict(items=[],total=0,offset=offset,limit=limit)
        where='source_id IN ('+','.join('?' for _ in ids)+')';params=list(ids)
        if query:
            matched=[sid for sid in ids if query.casefold() in configured[sid]['name'].casefold()]
            extra=' OR source_id IN ('+','.join('?' for _ in matched)+')' if matched else ''
            where+=' AND (title LIKE ? ESCAPE \'\\\' OR source_id LIKE ? ESCAPE \'\\\''+extra+')'
            escaped=query.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')
            params+=['%'+escaped+'%']*2+matched
        with self.lock:
            total=self.db.execute('SELECT count(DISTINCT url) FROM items WHERE '+where,params).fetchone()[0]
            quality="CASE WHEN title LIKE '[제목 미제공]%' OR title LIKE '[사이트맵%' THEN 1 ELSE 0 END"
            sql='WITH matched AS (SELECT *,row_number() OVER(PARTITION BY url ORDER BY '+quality+',first_seen,source_id,key) AS pick FROM items WHERE '+where+') SELECT * FROM matched WHERE pick=1 ORDER BY first_seen DESC,source_time DESC,source_id,key LIMIT ? OFFSET ?'
            rows=[dict(r) for r in self.db.execute(sql,params+[limit,offset])]
        for r in rows:
            c=configured[r['source_id']];r.update(source_name=c['name'],group=c.get('group','official'),region=c.get('region','global'))
        return dict(items=rows,total=total,offset=offset,limit=limit)


def read_response(response, deadline, limit=8_000_000):
    """Read bounded chunks so a trickling body cannot occupy a worker indefinitely."""
    parts=[];size=0
    while True:
        if time.monotonic()>deadline:raise TimeoutError('Response read budget exceeded')
        chunk=response.read1(min(65536,limit+1-size))
        if time.monotonic()>deadline:raise TimeoutError('Response read budget exceeded')
        if not chunk:break
        parts.append(chunk);size+=len(chunk)
        if size>limit:raise ValueError('Response exceeds 8 MB limit')
    # read1 does not always raise on a prematurely terminated Content-Length body.
    length=response.headers.get('Content-Length')
    if length and size!=int(length):raise IncompleteRead(b'',max(0,int(length)-size))
    return b''.join(parts)


def fetch_source(source, previous):
    started, tick = time.time(), time.monotonic()
    headers = {"User-Agent": "AI-News-Radar/1.0 (personal conditional public-feed monitor)",
               "Accept": "application/atom+xml,application/rss+xml,application/xml,text/html;q=0.9,*/*;q=0.1",
               "Accept-Encoding": "gzip"}
    if previous.get("etag"):
        headers["If-None-Match"] = previous["etag"]
    if previous.get("last_modified"):
        headers["If-Modified-Since"] = previous["last_modified"]
    if not source.get('respect_cache_ttl',False):headers['Cache-Control']='no-cache'
    status, response_headers, entries, error, parsed_count, valid_empty_feed, error_kind = None, {}, [], None, 0, False, None
    transferred_bytes=decoded_bytes=None
    try:
        with urlopen(Request(source["url"], headers=headers), timeout=source.get('timeout',15),context=TLS_CONTEXT) as response:
            status = response.status
            response_headers = {k.lower(): v for k, v in response.headers.items()}
            data = read_response(response,tick+source.get('read_budget_seconds',30))
            transferred_bytes=len(data)
            if response_headers.get("content-encoding", "").lower() == "gzip":
                with gzip.GzipFile(fileobj=io.BytesIO(data)) as compressed:
                    data = compressed.read(8_000_001)
                if len(data) > 8_000_000:
                    raise ValueError("Decompressed response exceeds 8 MB limit")
            elif response_headers.get('content-encoding','').lower() not in ('','identity'):
                raise ValueError('Unsupported content encoding')
            decoded_bytes=len(data)
            entries = parse_source(dict(source,url=response.url), data)
            parsed_count=len(entries)
            if not entries and source.get('allow_empty') and source['kind']=='feed':
                valid_empty_feed=ET.fromstring(data).tag.split('}')[-1] in ('rss','feed','RDF')
            if source.get('filter_ai'):
                # Search the actual per-entry title/body, never the whole feed (which would match every item).
                entries=[e for e in entries if e.get('ai_match',bool(AI_TERMS.search(e['title'])))]
    except HTTPError as exc:
        status = exc.code
        response_headers = {k.lower(): v for k, v in exc.headers.items()}
        if status == 304:
            transferred_bytes=decoded_bytes=0
        else:
            error = "HTTP %s" % status
    except Exception as exc:
        error = type(exc).__name__ + ": " + str(exc)[:240]
        error_kind='transport' if isinstance(exc,(OSError,HTTPException)) else 'parse'
    return dict(started=started, finished=time.time(), fetch_ms=round((time.monotonic()-tick)*1000, 1),
                status=status, headers=response_headers, entries=entries, parsed_count=parsed_count,
                valid_empty_feed=valid_empty_feed,error=error,error_kind=error_kind,
                queue_delay_seconds=max(0,started-previous['next_due']) if previous.get('next_due') else None,
                transferred_bytes=transferred_bytes,decoded_bytes=decoded_bytes)


class Radar:
    def __init__(self, root=ROOT, config=None, database=None):
        self.root = root
        self.config = config or json.loads((root / "config.json").read_text())
        self.store = Store(database or root / "data" / "radar.sqlite3", self.config)
        self.stop = threading.Event()
        self.last_test = float('-inf')
        self.poller_thread=None
        self.poller_heartbeat=None

    def run_polls(self):
        workers=self.config.get('workers',6)
        startup_due=time.time()
        with ThreadPoolExecutor(max_workers=workers) as pool:
            active={};hosts=set();host_due={}
            while not self.stop.is_set():
                now = time.time()
                self.poller_heartbeat=now
                for f in [f for f in active if f.done()]:
                    s,host=active.pop(f);hosts.remove(host)
                    host_due[host]=now+self.config.get('host_gap_seconds',3)
                    try:
                        result=f.result()
                        if result.get('status')==429 or (result.get('status')==503 and result.get('headers',{}).get('retry-after')):
                            host_due[host]=now+max(60,retry_after(result.get('headers',{}),now))
                        self.store.apply(s,result)
                    except Exception:
                        LOG.exception("poll processing failed: %s", s["id"])
                        with self.store.lock, self.store.db:
                            self.store.db.execute("UPDATE sources SET next_due=?,status='error',error=? WHERE id=?",
                                                  (time.time()+120, "Internal processing error; see service log", s["id"]))
                # Bound queued work. A slow host never stalls every other source.
                due=[]
                for s in self.config['sources']:
                    p=self.store.source(s['id']);host=urlsplit(s['url']).netloc
                    if p['next_due']<=now and host not in hosts and host_due.get(host,0)<=now:
                        due.append((p['next_due'] or startup_due,s,p,host))
                # Priority is a finite head start, not permission to starve older work.
                # Even a low-priority retry overtakes fresh core work after 120 seconds.
                priority_step=self.config.get('priority_step_seconds',10)
                for _,s,p,host in sorted(due,key=lambda x:(x[0]+max(0,min(12,x[1].get('priority',10)))*priority_step,x[0])):
                    if len(active)>=workers:break
                    if host in hosts:continue
                    active[pool.submit(fetch_source,s,p)]=(s,host);hosts.add(host)
                self.stop.wait(.5)

    def handler(self):
        radar = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def send(self, status, body, mime="application/json"):
                if isinstance(body, (dict, list)):
                    body = json.dumps(body, ensure_ascii=False).encode()
                elif isinstance(body, str):
                    body = body.encode()
                self.send_response(status)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Security-Policy",
                                 "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
                if mime.startswith("text/csv"):
                    self.send_header("Content-Disposition", 'attachment; filename="%s.csv"' % self.path.split("/")[1].split(".")[0])
                self.end_headers()
                self.wfile.write(body)

            def valid_host(self):
                return self.headers.get("Host") in ("127.0.0.1:%s" % radar.config["port"],
                                                   "localhost:%s" % radar.config["port"])

            def do_GET(self):
                if not self.valid_host():
                    return self.send(403, {"error": "Invalid host"})
                path = urlsplit(self.path).path
                if path == "/api/state":
                    return self.send(200, radar.store.state())
                if path == "/api/pending":
                    return self.send(200, radar.store.pending())
                if path == '/api/library':
                    try:
                        q=dict(parse_qsl(urlsplit(self.path).query));offset=max(0,int(q.get('offset','0')))
                        limit=max(1,min(100,int(q.get('limit','50'))))
                        return self.send(200,radar.store.library(q.get('q','')[:200],q.get('group',''),offset,limit))
                    except ValueError:return self.send(400,{'error':'Invalid pagination'})
                if path == '/api/coverage':
                    p=radar.root/'coverage.json'
                    return self.send(200,json.loads(p.read_text()) if p.exists() else {'excluded':[],'notes':[]})
                if path == "/health":
                    alive=radar.poller_thread is not None and radar.poller_thread.is_alive()
                    fresh=radar.poller_heartbeat is not None and time.time()-radar.poller_heartbeat<90
                    return self.send(200 if alive and fresh else 503, {
                        "running":alive and fresh,"pid":os.getpid(),"root":str(radar.root.resolve()),
                        "poller_alive":alive,"poller_heartbeat":radar.poller_heartbeat})
                if path in ("/events.csv", "/checks.csv",'/items.csv'):
                    return self.send(200, radar.store.export(path[1:-4]), "text/csv; charset=utf-8")
                files = {"/": "index.html", "/app.js": "app.js", "/style.css": "style.css"}
                if path in files:
                    mime = {"/": "text/html", "/app.js": "text/javascript", "/style.css": "text/css"}[path]
                    return self.send(200, (radar.root/"static"/files[path]).read_bytes(), mime+"; charset=utf-8")
                return self.send(404, {"error": "Not found"})

            def do_POST(self):
                if not self.valid_host():
                    return self.send(403, {"error": "Invalid host"})
                origin = self.headers.get("Origin")
                if origin and origin not in ("http://127.0.0.1:%s" % radar.config["port"],
                                             "http://localhost:%s" % radar.config["port"]):
                    return self.send(403, {"error": "Invalid origin"})
                if self.headers.get_content_type() != "application/json":
                    return self.send(415, {"error": "JSON required"})
                try:
                    n = int(self.headers.get("Content-Length", 0))
                    if n < 0 or n > 16384:
                        return self.send(413, {"error": "Request too large"})
                    data = json.loads(self.rfile.read(n) or b"{}")
                    path = urlsplit(self.path).path
                    if path == "/api/test":
                        if time.monotonic() - radar.last_test < 5:
                            return self.send(429, {"error": "5초 후 다시 시험하세요."})
                        radar.last_test = time.monotonic()
                        return self.send(200, {"id": radar.store.test_event()})
                    if path == "/api/notification-result":
                        radar.store.notification_result(int(data["id"]), data["state"], data.get("error"))
                        return self.send(200, {"ok": True})
                    if path == "/api/bridge":
                        with radar.store.lock, radar.store.db:
                            radar.store.set_meta("notification_bridge", dict(data, heartbeat=iso()))
                        return self.send(200, {"ok": True})
                    if path == "/api/opened":
                        with radar.store.lock, radar.store.db:
                            radar.store.db.execute("UPDATE events SET opened_at=? WHERE id=?", (time.time(), int(data["id"])))
                        return self.send(200, {"ok": True})
                    if path == "/api/x-log":
                        return self.send(200, {"id": radar.store.record_x(data["url"].strip(), data.get("published_at", "").strip())})
                    if path == '/api/source-settings':
                        radar.store.set_notification(data['id'],data['notify'])
                        return self.send(200,{'ok':True})
                    return self.send(404, {"error": "Not found"})
                except (KeyError, ValueError, TypeError) as exc:
                    return self.send(400, {"error": str(exc)})
        return Handler

    def serve(self, bridge=True):
        server = ThreadingHTTPServer(("127.0.0.1", self.config["port"]), self.handler())
        thread = threading.Thread(target=self.run_polls, daemon=True)
        self.poller_thread=thread
        thread.start()
        if bridge and (self.root/"native"/"AI News Radar.app").exists():
            subprocess.Popen(["/usr/bin/open", "-g", str(self.root/"native"/"AI News Radar.app")],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        server.timeout = 1
        try:
            while not self.stop.is_set():
                server.handle_request()
                if not self.stop.is_set() and (not thread.is_alive() or (self.poller_heartbeat and time.time()-self.poller_heartbeat>90)):
                    raise RuntimeError('Polling worker stopped or stalled; supervisor should restart collector')
        finally:
            self.stop.set()
            server.server_close()
            thread.join(timeout=45)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--no-bridge", action="store_true")
    args = parser.parse_args()
    (ROOT/"data").mkdir(exist_ok=True)
    process_lock=(ROOT/'data/radar.lock').open('a')
    try:fcntl.flock(process_lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:raise SystemExit('A collector already owns this data directory.')
    logger = RotatingFileHandler(ROOT/"data"/"service.log", maxBytes=2_000_000, backupCount=3)
    logger.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    LOG.addHandler(logger)
    LOG.setLevel(logging.INFO)
    radar = Radar()
    if args.once:
        for s in radar.config["sources"]:
            result = fetch_source(s, radar.store.source(s["id"]))
            radar.store.apply(s, result)
            print(s["id"], result["status"], len(result["entries"]), result.get("error"))
        return
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: radar.stop.set())
    radar.serve(not args.no_bridge)


if __name__ == "__main__":
    main()
