#!/usr/bin/env python3
"""Local macOS installation, supervision and private SQLite migration."""
import argparse
from contextlib import contextmanager, closing
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from urllib.request import Request,urlopen
import zipfile
from build_native import build

SOURCE=Path(__file__).resolve().parent
ROOT=Path.home()/'Library/Application Support/AI News Radar'
LABEL='com.sueun.ai-news-radar'
HELPER=LABEL+'.notifier'
DOMAIN='gui/'+str(os.getuid())
AGENTS=Path.home()/'Library/LaunchAgents'
FILES=('radar.py','manage.py','build_native.py','config.json','coverage.json','static','native')

def stamp():return datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
def run(*args,check=True):return subprocess.run(args,check=check,capture_output=True,text=True)
def plist_path(label):return AGENTS/(label+'.plist')
def loaded(label):return run('/bin/launchctl','print',DOMAIN+'/'+label,check=False).returncode==0

def validate_config(c):
    if not isinstance(c,dict) or not isinstance(c.get('port'),int) or not 1024<=c['port']<=65535:
        raise ValueError('config: port must be 1024..65535')
    rows=c.get('sources');ids=set()
    if not isinstance(rows,list) or not rows:raise ValueError('config: nonempty sources required')
    for s in rows:
        if not isinstance(s.get('id'),str) or not s['id'] or s['id'] in ids:raise ValueError('config: source IDs must be unique')
        ids.add(s['id'])
        if s.get('kind') not in ('feed','anthropic','changelog','links','sitemap','hf_models'):raise ValueError('config: unknown parser')
        if not isinstance(s.get('interval'),(int,float)) or s['interval']<1:raise ValueError('config: positive polling interval required')
        if not s.get('url','').startswith(('https://','http://')):raise ValueError('config: HTTP(S) source required')
    return c

def read_config(path=None):
    # Preserve an installed/restored configuration on upgrades unless explicitly replaced.
    selected=Path(path) if path else (ROOT/'config.json' if (ROOT/'config.json').exists() else SOURCE/'config.json')
    return validate_config(json.loads(selected.read_text()))

def assert_owned(label):
    p=plist_path(label)
    if p.exists():
        spec=plistlib.loads(p.read_bytes())
        expected=str(ROOT/'radar.py') if label==LABEL else str(ROOT/'native/AI News Radar.app/Contents/MacOS/AIRadar')
        if spec.get('Label')!=label or expected not in spec.get('ProgramArguments',[]):
            raise RuntimeError('A different job owns '+str(p))
    elif loaded(label):raise RuntimeError('A loaded job has no owned plist: '+label)

def stop_job(label):
    assert_owned(label)
    if loaded(label):
        run('/bin/launchctl','bootout',DOMAIN+'/'+label)
        for _ in range(200):
            if not loaded(label):return
            time.sleep(.1)
        raise RuntimeError('launchd job is still unloading: '+label)

def start_job(label):
    assert_owned(label)
    if not plist_path(label).exists():raise RuntimeError('Install first: missing '+label)
    if loaded(label):run('/bin/launchctl','kickstart',DOMAIN+'/'+label)
    else:run('/bin/launchctl','bootstrap',DOMAIN,str(plist_path(label)))

def stop_legacy_helper():
    binary=str(ROOT/'native/AI News Radar.app/Contents/MacOS/AIRadar');stopped=False
    for line in run('/bin/ps','-axo','pid=,command=').stdout.splitlines():
        parts=line.strip().split(None,1)
        if len(parts)==2 and parts[1]==binary:
            try:os.kill(int(parts[0]),signal.SIGTERM);stopped=True
            except ProcessLookupError:pass
    return stopped

def job_specs(python,notifications=True,keep_awake=False):
    command=[python,str(ROOT/'radar.py'),'--no-bridge']
    if keep_awake:command=['/usr/bin/caffeinate','-i']+command
    def spec(label,args,keep):
        return dict(Label=label,ProgramArguments=args,WorkingDirectory=str(ROOT),RunAtLoad=True,
                    KeepAlive=keep,ThrottleInterval=15,ProcessType='Background',ExitTimeOut=50,
                    StandardOutPath=str(ROOT/'data'/('collector.out.log' if label==LABEL else 'notifier.out.log')),
                    StandardErrorPath=str(ROOT/'data'/('collector.err.log' if label==LABEL else 'notifier.err.log')),
                    EnvironmentVariables=dict(PYTHONUNBUFFERED='1',PATH='/usr/bin:/bin:/usr/sbin:/sbin'))
    result={LABEL:spec(LABEL,command,True)}
    if notifications:
        result[HELPER]=spec(HELPER,[str(ROOT/'native/AI News Radar.app/Contents/MacOS/AIRadar')],{'SuccessfulExit':False})
    return result

def health(config):
    with urlopen('http://127.0.0.1:%s/health'%config['port'],timeout=3) as r:return json.load(r)

def wait_ready(config):
    last=None
    for _ in range(50):
        try:
            h=health(config)
            if h.get('root')==str(ROOT.resolve()) and h.get('poller_alive'):return h
        except Exception as e:last=e
        time.sleep(.3)
    raise RuntimeError('Collector did not become healthy: '+str(last))

@contextmanager
def install_lock():
    ROOT.parent.mkdir(parents=True,exist_ok=True)
    with (ROOT.parent/'.ai-news-radar-install.lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise RuntimeError('Another install/restore is running')
        yield

def install(config,notifications=True,keep_awake=False):
    if platform.system()!='Darwin':raise RuntimeError('LaunchAgent install requires macOS.')
    if sys.version_info<(3,9) or sqlite3.sqlite_version_info<(3,25):raise RuntimeError('Python 3.9+ and SQLite 3.25+ required')
    python=shutil.which('python3') or sys.executable
    for label in (LABEL,HELPER):assert_owned(label)
    if not loaded(LABEL):
        with socket.socket() as s:
            try:s.bind(('127.0.0.1',config['port']))
            except OSError:raise RuntimeError('Local port is occupied. Stop the other collector/process first.')
    with install_lock(),tempfile.TemporaryDirectory(prefix='.radar-stage-',dir=str(ROOT.parent)) as td:
        stage=Path(td)
        for name in ('radar.py','manage.py','build_native.py','coverage.json'):
            shutil.copy2(SOURCE/name,stage/name)
        for name in ('static','native'):
            shutil.copytree(SOURCE/name,stage/name,ignore=shutil.ignore_patterns('*.app','__pycache__'))
        (stage/'config.json').write_text(json.dumps(config,ensure_ascii=False,indent=2)+'\n')
        if notifications:build(stage/'native/AI News Radar.app',config['port'],ROOT/'data')
        specs=job_specs(python,notifications,keep_awake)
        old_plists={l:plist_path(l).read_bytes() for l in (LABEL,HELPER) if plist_path(l).exists()}
        old_loaded={l:loaded(l) for l in (LABEL,HELPER)}
        ROOT.mkdir(parents=True,exist_ok=True,mode=0o700);(ROOT/'data').mkdir(exist_ok=True,mode=0o700)
        rollback=ROOT/'backups'/('program-'+stamp());rollback.mkdir(parents=True,mode=0o700)
        moved=[];written=[];legacy=False
        try:
            stop_job(HELPER);stop_job(LABEL);legacy=stop_legacy_helper()
            for name in FILES:
                if (ROOT/name).exists():os.replace(ROOT/name,rollback/name);moved.append(name)
                os.replace(stage/name,ROOT/name);written.append(name)
            AGENTS.mkdir(parents=True,exist_ok=True)
            for label in (LABEL,HELPER):
                if label in specs:
                    plist_path(label).write_bytes(plistlib.dumps(specs[label]));os.chmod(plist_path(label),0o600)
                elif plist_path(label).exists():plist_path(label).unlink()
            start_job(LABEL);wait_ready(config)
            if notifications:start_job(HELPER)
        except Exception:
            for label in (HELPER,LABEL):
                try:stop_job(label)
                except Exception:pass
            for name in reversed(written):
                p=ROOT/name
                if p.is_dir():shutil.rmtree(p)
                elif p.exists():p.unlink()
            for name in moved:os.replace(rollback/name,ROOT/name)
            for label in (LABEL,HELPER):
                if label in old_plists:plist_path(label).write_bytes(old_plists[label])
                elif plist_path(label).exists():plist_path(label).unlink()
                if old_loaded[label]:start_job(label)
            if legacy:run('/usr/bin/open','-g',str(ROOT/'native/AI News Radar.app'),check=False)
            raise
    print('Installed; data preserved. Dashboard: http://127.0.0.1:%s/'%config['port'])

def backup(output):
    output=Path(output).expanduser().resolve()
    if output.exists():raise ValueError('Backup output already exists')
    dbpath=ROOT/'data/radar.sqlite3'
    if not dbpath.exists():raise ValueError('No installed database to back up')
    output.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        snapshot=Path(td)/'radar.sqlite3'
        with closing(sqlite3.connect('file:'+str(dbpath)+'?mode=ro',uri=True)) as src,closing(sqlite3.connect(snapshot)) as dst:src.backup(dst)
        contents={'radar.sqlite3':snapshot.read_bytes(),'config.json':(ROOT/'config.json').read_bytes()}
        manifest={'format':1,'created_at':stamp(),'sha256':{k:hashlib.sha256(v).hexdigest() for k,v in contents.items()}}
        with output.open('xb') as f:
            os.chmod(output,0o600)
            with zipfile.ZipFile(f,'w',zipfile.ZIP_DEFLATED) as z:
                for name,data in contents.items():z.writestr(name,data)
                z.writestr('manifest.json',json.dumps(manifest))
    print('PRIVATE backup (do not commit/upload):',output)

def restore(archive):
    # Explicit names only; never extract untrusted archive paths or replace an existing DB.
    with zipfile.ZipFile(archive) as z:
        if sorted(z.namelist())!=['config.json','manifest.json','radar.sqlite3']:raise ValueError('Unexpected backup entries')
        if any(i.file_size>512_000_000 for i in z.infolist()):raise ValueError('Backup exceeds 512 MB per-file limit')
        manifest=json.loads(z.read('manifest.json'));contents={k:z.read(k) for k in ('config.json','radar.sqlite3')}
        if manifest.get('format')!=1:raise ValueError('Unknown backup version')
        for name,data in contents.items():
            if hashlib.sha256(data).hexdigest()!=manifest['sha256'].get(name):raise ValueError('Backup checksum mismatch')
        validate_config(json.loads(contents['config.json']))
    with install_lock():
        if (ROOT/'data/radar.sqlite3').exists():raise ValueError('Target database exists; restore requires a fresh target. Existing data was not changed.')
        if loaded(LABEL) or loaded(HELPER):raise ValueError('Stop the installed jobs before restoring')
        ROOT.mkdir(parents=True,exist_ok=True,mode=0o700);(ROOT/'data').mkdir(exist_ok=True,mode=0o700)
        with tempfile.TemporaryDirectory(dir=str(ROOT)) as td:
            dbpath=Path(td)/'radar.sqlite3';dbpath.write_bytes(contents['radar.sqlite3'])
            with closing(sqlite3.connect(dbpath)) as d:
                with d:
                    if d.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise ValueError('Invalid SQLite backup')
                    d.execute("UPDATE events SET notify_state='historical_silent' WHERE notify_state='pending'")
                    d.execute("DELETE FROM meta WHERE key IN ('notification_bridge','service_started','pid')")
                    d.execute('UPDATE sources SET next_due=0')
            if (ROOT/'config.json').exists():
                shutil.copy2(ROOT/'config.json',ROOT/('config-before-restore-'+stamp()+'.json'))
            os.replace(dbpath,ROOT/'data/radar.sqlite3')
            (ROOT/'config.json').write_bytes(contents['config.json'])
    print('Restored history/preferences. Old pending alerts silenced; X/browser permissions were not migrated. Run install next.')

def status():
    c=read_config();result={'collector_loaded':loaded(LABEL),'notifier_loaded':loaded(HELPER)}
    try:
        result['health']=health(c)
        with urlopen('http://127.0.0.1:%s/api/state'%c['port'],timeout=5) as r:s=json.load(r)
        result.update(source_count=len(s['sources']),errors=[{'id':x['id'],'error':x['error']} for x in s['sources'] if x['status']!='ok'],
                      metrics=s['metrics'],notification_bridge=s['meta'].get('notification_bridge'))
    except Exception as e:result['error']=str(e)
    print(json.dumps(result,ensure_ascii=False,indent=2))

def main():
    os.umask(0o077)
    p=argparse.ArgumentParser();sub=p.add_subparsers(dest='action',required=True)
    i=sub.add_parser('install');i.add_argument('--config',type=Path);i.add_argument('--no-notifications',action='store_true');i.add_argument('--keep-awake',action='store_true')
    for a in ('start','stop','disable','status','doctor','test'):sub.add_parser(a)
    b=sub.add_parser('backup');b.add_argument('--output',type=Path,required=True)
    r=sub.add_parser('restore');r.add_argument('--input',type=Path,required=True)
    args=p.parse_args()
    try:
        if args.action=='install':install(read_config(args.config),not args.no_notifications,args.keep_awake)
        elif args.action=='backup':backup(args.output)
        elif args.action=='restore':restore(args.input)
        elif args.action in ('status','doctor'):status()
        elif args.action=='start':
            start_job(LABEL);wait_ready(read_config())
            if plist_path(HELPER).exists():start_job(HELPER)
        elif args.action in ('stop','disable'):
            stop_job(HELPER);stop_job(LABEL);stop_legacy_helper()
            if args.action=='disable':
                for label in (HELPER,LABEL):
                    if plist_path(label).exists():plist_path(label).unlink()
            print('Stopped; data retained.')
        elif args.action=='test':
            req=Request('http://127.0.0.1:%s/api/test'%read_config()['port'],data=b'{}',headers={'Content-Type':'application/json'})
            with urlopen(req,timeout=5) as r:print(r.read().decode())
    except (ValueError,RuntimeError,OSError,subprocess.CalledProcessError,zipfile.BadZipFile,sqlite3.DatabaseError,KeyError) as e:
        p.exit(1,str(e)+'\n')

if __name__=='__main__':main()
