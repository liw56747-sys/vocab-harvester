"""Durable image references, bounded downloads and portable image-only packages."""
import asyncio
import csv
import hashlib
import html
import io
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import warnings
import zipfile
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from PIL import Image
from src.collection.store import now
from src.crawlers.comment_results import search_export_rows

MAX_BYTES = 20 * 1024 * 1024
MAX_PIXELS = 40_000_000
MAX_URLS = 10_000
MAX_RUN_BYTES = 2 * 1024**3
ATTEMPT_SECONDS = 45
_download_slots = threading.BoundedSemaphore(2)
_export_slots = threading.BoundedSemaphore(2)
HOSTS = {'pbs.twimg.com', 'i.redd.it', 'preview.redd.it', 'external-preview.redd.it'}
FIELDS = ['platform','keyword','post_id','comment_id','source_url','image_url','status','attempts',
          'error','download_url','sha256','file','width','height','bytes']


def allowed_url(url):
    try:
        p = urlsplit(url)
        return (p.scheme == 'https' and p.hostname in HOSTS and not p.username and not p.password
                and p.port in (None, 443) and (p.hostname != 'pbs.twimg.com' or p.path.startswith('/media/')))
    except ValueError:
        return False


def canonical(url):
    url = html.unescape(str(url)).strip()
    p = urlsplit(url)
    if p.hostname == 'pbs.twimg.com' and p.path.startswith('/media/'):
        query = dict(parse_qsl(p.query)); query['name'] = 'orig'
        return urlunsplit((p.scheme,p.netloc,p.path,urlencode(query),''))
    return url


def reddit_images(data, comment=False):
    """Only embedded native images; video previews and external link thumbnails are excluded."""
    urls = []
    metadata = data.get('media_metadata') or {}
    ids = [x.get('media_id') for x in (data.get('gallery_data') or {}).get('items', [])]
    if comment:
        ids = re.findall(r'!\[img\]\(([^)\s]+)\)', data.get('body') or '')
        # Native metadata is returned only for the comment's embedded media.
        ids += list(metadata)
    for ident in ids:
        item = metadata.get(ident) or {}
        if item.get('e') in ('Image', 'AnimatedImage') or str(item.get('m','')).startswith('image/'):
            source = item.get('s') or {}
            url = source.get('gif') or source.get('u')
            if url: urls.append(url)
    text = data.get('body') or '' if comment else data.get('selftext') or ''
    urls += re.findall(r'!\[[^\]]*\]\((https://[^\s)]+)', text)
    if not comment and not data.get('is_video'):
        url = data.get('url_overridden_by_dest') or data.get('url') or ''
        if data.get('post_hint') == 'image' or re.search(r'\.(?:jpe?g|png|webp|gif)(?:\?|$)',url,re.I):
            previews = (data.get('preview') or {}).get('images') or []
            preview = ((previews[0].get('source') or {}).get('url') or '') if previews else ''
            # Reddit may expose a native preview for an externally hosted image.
            urls.append(url if allowed_url(url) or not allowed_url(html.unescape(preview)) else preview)
    return list(dict.fromkeys(html.unescape(u) for u in urls if u))


def without_text(rows):
    result = []
    for row in rows:
        row = dict(row)
        for field in ('content','title','text','full_text'):
            row.pop(field, None)
        raw = row.get('replies_data')
        if raw:
            row['replies_data'] = without_text(json.loads(raw) if isinstance(raw,str) else raw)
        result.append(row)
    return result


def image_urls(row):
    if 'image_urls' in row:
        urls = row['image_urls']
    elif row.get('media_type') == 'image':
        urls = row.get('media_urls') or []
    else:
        urls = []
    return list(dict.fromkeys(u for u in (urls.split(';') if isinstance(urls,str) else urls or []) if u))


class Images:
    def __init__(self, store):
        self.store = store
        self.root = store.path.parent / 'images'
        self.root.mkdir(parents=True, exist_ok=True)
        with store.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS image_assets (
                run_id TEXT, url TEXT, fallback TEXT, status TEXT DEFAULT 'pending', attempts INTEGER DEFAULT 0,
                error TEXT DEFAULT '', download_url TEXT DEFAULT '', sha256 TEXT DEFAULT '', file TEXT DEFAULT '',
                width INTEGER DEFAULT 0, height INTEGER DEFAULT 0, bytes INTEGER DEFAULT 0,
                PRIMARY KEY(run_id,url));
            CREATE TABLE IF NOT EXISTS image_sources (
                run_id TEXT, ref_id TEXT, url TEXT, platform TEXT, keyword TEXT, post_id TEXT,
                comment_id TEXT, source_url TEXT, PRIMARY KEY(run_id,ref_id));
            CREATE INDEX IF NOT EXISTS image_sources_url ON image_sources(run_id,url);
            ''')

    def discover(self, rid, rows):
        with self.store.connect() as db:
            for row in search_export_rows(rows):
                comment = row.get('type') == 'comment'
                cid = str(row.get('comment_id') or row.get('tweet_id') or '') if comment else ''
                pid = str((row.get('root_post_id') or row.get('parent_id') or row.get('post_id')) if comment else (row.get('tweet_id') or row.get('post_id')))
                source = row.get('url') or ''
                for url in image_urls(row):
                    try:
                        url = html.unescape(url).strip()
                        target = canonical(url)
                    except ValueError:
                        continue
                    fields = [row.get('platform',''),row.get('keyword',''),pid,cid,source]
                    ref_id = hashlib.sha256(json.dumps([*fields,target],ensure_ascii=False).encode()).hexdigest()
                    status, error = ('pending','') if allowed_url(target) else ('unsupported','不支持的平台图片地址')
                    db.execute('INSERT OR IGNORE INTO image_assets(run_id,url,fallback,status,error) VALUES(?,?,?,?,?)',
                               (rid,target,url,status,error))
                    db.execute('INSERT OR IGNORE INTO image_sources VALUES(?,?,?,?,?,?,?,?)',
                               (rid,ref_id,target,*fields))

    def summary(self, rid):
        with self.store.connect() as db:
            counts = {r[0]:r[1] for r in db.execute('SELECT status,COUNT(*) FROM image_assets WHERE run_id=? GROUP BY status',(rid,))}
            refs = db.execute('SELECT COUNT(*) FROM image_sources WHERE run_id=?',(rid,)).fetchone()[0]
            files = db.execute("SELECT COUNT(DISTINCT sha256) FROM image_assets WHERE run_id=? AND status='downloaded'",(rid,)).fetchone()[0]
        return {'content_mode':'images','images_found':sum(counts.values()), 'image_sources':refs,
                'images_downloaded':counts.get('downloaded',0),'image_files':files,
                'images_pending':counts.get('pending',0),'images_failed':sum(n for k,n in counts.items() if k not in ('pending','downloaded'))}

    def assets(self, rid):
        with self.store.connect() as db:
            return [dict(r) for r in db.execute('SELECT * FROM image_assets WHERE run_id=? ORDER BY rowid',(rid,))]

    def update(self, rid, url, **changes):
        with self.store.connect() as db:
            db.execute('UPDATE image_assets SET '+','.join(k+'=?' for k in changes)+' WHERE run_id=? AND url=?',
                       (*changes.values(),rid,url))

    def valid_file(self, row):
        path = self.root / row['file']
        if not row['file'] or not re.fullmatch(r'[0-9a-f]{64}\.(jpg|png|webp|gif)',row['file']):
            return False
        try:
            if path.stat().st_size != row['bytes']: return False
            digest = hashlib.sha256()
            with path.open('rb') as f:
                for chunk in iter(lambda:f.read(1024*1024),b''): digest.update(chunk)
            return digest.hexdigest() == row['sha256']
        except OSError:
            return False

    def manifest(self, rid):
        with self.store.connect() as db:
            return [dict(r) for r in db.execute('''SELECT s.platform,s.keyword,s.post_id,s.comment_id,s.source_url,
                a.url AS image_url,a.status,a.attempts,a.error,a.download_url,a.sha256,a.file,a.width,a.height,a.bytes
                FROM image_sources s JOIN image_assets a ON a.run_id=s.run_id AND a.url=s.url
                WHERE s.run_id=? ORDER BY s.rowid''',(rid,))]

    async def download(self, rid, proxies=None, cancelled=lambda:False, progress=lambda:None, transport=None, attempted=None):
        """Two fixed workers, globally bounded across all runs/threads; no task per URL."""
        attempted = attempted if attempted is not None else set()
        assets = self.assets(rid)
        queue = asyncio.Queue()
        used = sum(r['bytes'] for r in assets if r['status']=='downloaded')
        verified = {}
        for index, row in enumerate(assets):
            if row['url'] in attempted: continue
            if row['status'] == 'downloaded':
                fingerprint=(row['file'],row['bytes'],row['sha256'])
                if fingerprint not in verified:
                    verified[fingerprint] = await asyncio.to_thread(self.valid_file,row)
                if verified[fingerprint]:
                    attempted.add(row['url']); continue
                used -= row['bytes']
            if row['status'] == 'unsupported' or row['url'] in attempted: continue
            if index >= MAX_URLS:
                self.update(rid,row['url'],status='limit',error='本轮图片地址超过 10000 上限'); continue
            queue.put_nowait(row)
        # Reserve maximum file size before a download to bound the run even with two workers.
        reserved = 0
        async def worker():
            nonlocal used, reserved
            while not queue.empty():
                if cancelled(): raise asyncio.CancelledError
                row = queue.get_nowait()
                attempted.add(row['url'])
                if used + reserved + MAX_BYTES > MAX_RUN_BYTES:
                    self.update(rid,row['url'],status='limit',error='本轮图片容量达到 2 GiB 上限'); continue
                reserved += MAX_BYTES
                try:
                    while not _download_slots.acquire(blocking=False):
                        if cancelled(): raise asyncio.CancelledError
                        await asyncio.sleep(.1)
                    try:
                        host = urlsplit(row['url']).hostname
                        proxy = (proxies or {}).get('twitter' if host=='pbs.twimg.com' else 'reddit')
                        if proxy and not proxy.startswith(('http://','https://','socks5://')): proxy='http://'+proxy
                        async with httpx.AsyncClient(timeout=15,follow_redirects=False,proxy=proxy,transport=transport,
                                                     trust_env=False,headers={'User-Agent':'Mozilla/5.0'}) as client:
                            size = await self._one(client,rid,row,cancelled)
                            used += size
                    finally:
                        _download_slots.release()
                finally:
                    reserved -= MAX_BYTES
                    progress()
        tasks = [asyncio.create_task(worker()) for _ in range(2)]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks: task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)

    async def _one(self, client, rid, row, cancelled):
        attempts = row['attempts']
        urls = [row['url']]
        if row['fallback'] != row['url'] and allowed_url(row['fallback']): urls.append(row['fallback'])
        last = '下载未完成'
        for attempt in range(2):
            if cancelled(): raise asyncio.CancelledError
            attempts += 1
            self.update(rid,row['url'],status='pending',attempts=attempts,error='')
            try:
                result = await asyncio.wait_for(self._fetch(client,urls[min(attempt,len(urls)-1)],cancelled),ATTEMPT_SECONDS)
                self.update(rid,row['url'],status='downloaded',error='',**result)
                return result['bytes']
            except asyncio.CancelledError:
                self.update(rid,row['url'],status='pending',error='下载中断，可只补图片')
                raise
            except httpx.HTTPStatusError as exc:
                last = f'HTTP {exc.response.status_code}'
                if exc.response.status_code in (401,403,404,410) and len(urls)==1: break
                if exc.response.status_code == 429: break
            except (httpx.HTTPError, TimeoutError): last='网络或下载超时'
            except (OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
                last = str(exc)[:180] or type(exc).__name__; break
            if attempt==0: await asyncio.sleep(.2)
        self.update(rid,row['url'],status='failed',error=last)
        return 0

    async def _fetch(self, client, url, cancelled):
        fd, temp = tempfile.mkstemp(prefix='.image-',suffix='.part',dir=self.root)
        os.close(fd)
        try:
            for _ in range(4):
                if not allowed_url(url): raise ValueError('不支持的图片地址或重定向')
                async with client.stream('GET',url) as response:
                    if response.is_redirect:
                        url = str(response.url.join(response.headers.get('location',''))); continue
                    response.raise_for_status()
                    if int(response.headers.get('content-length','0')) > MAX_BYTES: raise ValueError('图片超过 20 MiB')
                    size = 0; digest = hashlib.sha256()
                    with open(temp,'wb') as f:
                        async for chunk in response.aiter_bytes(chunk_size=64*1024):
                            if cancelled(): raise asyncio.CancelledError
                            size += len(chunk)
                            if size > MAX_BYTES: raise ValueError('图片超过 20 MiB')
                            digest.update(chunk); f.write(chunk)
                        f.flush(); os.fsync(f.fileno())
                    def inspect():
                        with warnings.catch_warnings():
                            warnings.simplefilter('error',Image.DecompressionBombWarning)
                            with Image.open(temp) as im:
                                width,height = im.size
                                if width*height > MAX_PIXELS: raise ValueError('图片超过 4000 万像素')
                                ext = {'JPEG':'jpg','PNG':'png','GIF':'gif','WEBP':'webp'}.get(im.format)
                                if not ext: raise ValueError('不支持的图片格式')
                                im.verify()
                            # Verify actual pixel decoding too (JPEG verify alone checks only headers).
                            with Image.open(temp) as decoded:
                                decoded.load()
                            return width,height,ext
                    width,height,ext = inspect()
                    sha = digest.hexdigest(); filename = sha+'.'+ext
                    # Avoid replacing an identical file currently being read by a Windows exporter.
                    if not self.valid_file({'file':filename,'bytes':size,'sha256':sha}):
                        os.replace(temp,self.root/filename)
                    return dict(download_url=url,sha256=sha,file=filename,width=width,height=height,bytes=size)
            raise ValueError('图片重定向次数过多')
        finally:
            Path(temp).unlink(missing_ok=True)

    def export(self, rid, folder=None):
        """Disk-backed ZIP; no base64 images or archives in API/UI memory."""
        if not _export_slots.acquire(blocking=False): raise ValueError('图片导出繁忙，请稍后重试')
        dest = None
        try:
            record = self.store.get(rid)
            snapshot_time = now()
            progress = self.store.progress(rid) or {}
            rows = self.manifest(rid)
            verified = {}
            for row in rows:
                fingerprint=(row['file'],row['bytes'],row['sha256'])
                if row['status']=='downloaded' and fingerprint not in verified:
                    verified[fingerprint]=self.valid_file(row)
                if row['status']=='downloaded' and not verified[fingerprint]:
                    row.update(status='failed',error='图片缓存缺失或损坏，请只补图片',file='')
                    self.update(rid,row['image_url'],status='failed',error=row['error'])
            stem = self.store.export_stem(rid) or 'collection_'+rid[:12]
            stem = Path(stem).name
            folder = Path(folder) if folder else Path.home()/'Downloads'
            folder.mkdir(parents=True,exist_ok=True)
            index = 0
            while True:
                dest = folder/(stem+(f'_{index}' if index else '')+'.zip')
                try:
                    fd = os.open(dest,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600); break
                except FileExistsError: index+=1
            with os.fdopen(fd,'wb') as file, zipfile.ZipFile(file,'w',compression=zipfile.ZIP_STORED,allowZip64=True) as archive:
                seen = set()
                for row in rows:
                    if row['status']=='downloaded':
                        filename=row['file']; row['file']='images/'+filename
                        if filename not in seen:
                            archive.write(self.root/filename,row['file']);seen.add(filename)
                    else: row['file']=''
                buf=io.StringIO();writer=csv.DictWriter(buf,fieldnames=FIELDS);writer.writeheader();writer.writerows(rows)
                archive.writestr('manifest.csv',buf.getvalue().encode('utf-8-sig'))
                archive.writestr('manifest.json',json.dumps({'schema_version':1,'images':rows},ensure_ascii=False,indent=2))
                assets={r['image_url']:r for r in rows}
                downloaded=sum(r['status']=='downloaded' for r in assets.values())
                pending=sum(r['status']=='pending' for r in assets.values())
                summary={**progress,'content_mode':'images','images_found':len(assets),
                         'images_downloaded':downloaded,'images_pending':pending,
                         'images_failed':len(assets)-downloaded-pending,'image_files':len(seen),'image_sources':len(rows),
                         'run_id':rid,'status':record['status'],
                         'snapshot_at':snapshot_time,'comments_requested':record['spec'].get('include_replies',False),
                         'note':'已保存图片快照；下载齐全不等于评论已遍历完整；仅含可访问的支持格式图片。'}
                archive.writestr('summary.json',json.dumps(summary,ensure_ascii=False,indent=2))
                archive.writestr('README.txt','manifest.json 中的 file 相对本 ZIP；sha256 用于去重。审核方按图片文件及来源关联接入预留接口；本工具不自动送审。\n')
            return str(dest)
        except BaseException:
            if dest is not None: dest.unlink(missing_ok=True)
            raise
        finally:
            _export_slots.release()
