#!/usr/bin/env python3
"""Wayback CDX batch scanner. Uses only Python standard library.
A failed lookup is NEVER treated as zero captures.
"""
import argparse, csv, json, os, time, urllib.parse, urllib.request, urllib.error
from pathlib import Path
from datetime import datetime, timezone

FIELDS=['original_url','category','indexed_save_count','earliest_date_utc','earliest_timestamp_utc','earliest_snapshot_url','status']
HEADERS={'User-Agent':'SmallvilleArchiveResearch/1.0 (personal preservation, low-frequency CDX requests)','Accept':'application/json'}

def get_stamps(url):
    q=urllib.parse.urlencode({'url':url,'matchType':'exact','output':'json','fl':'timestamp','gzip':'false'})
    req=urllib.request.Request('https://web.archive.org/cdx/search/cdx?'+q,headers=HEADERS)
    with urllib.request.urlopen(req,timeout=45) as resp:
        data=resp.read(8_000_001)
        if len(data)>8_000_000: raise ValueError('CDX response too large')
    body=data.decode('utf-8-sig').strip()
    if not body: raise ValueError('CDX returned empty response (not counted as no saves)')
    table=json.loads(body)
    if not isinstance(table,list) or not table or table[0]!=['timestamp']:
        raise ValueError('Unexpected CDX data or header')
    stamps=[]
    for row in table[1:]:
        if not isinstance(row,list) or len(row)!=1 or not isinstance(row[0],str) or not (len(row[0])==14 and row[0].isdigit()):
            raise ValueError('Invalid timestamp row')
        stamps.append(row[0])
    return stamps

def make_row(url,stamps):
    stamps=sorted(stamps)
    if not stamps: return [url,'NO_SAVES',0,'','','','OK']
    first=stamps[0]
    date=datetime.strptime(first,'%Y%m%d%H%M%S').replace(tzinfo=timezone.utc).strftime('%Y-%m-%d')
    category='SAVED_ONCE_AFTER_2008' if len(stamps)==1 and first[:8]>'20081231' else 'REMAINING'
    return [url,category,len(stamps),date,first,f'https://web.archive.org/web/{first}/{url}','OK']

def export(rows,out):
    out.mkdir(parents=True,exist_ok=True)
    with (out/'results.csv').open('w',newline='',encoding='utf-8-sig') as f:
        w=csv.writer(f);w.writerow(FIELDS);w.writerows(rows)
    groups={c:[] for c in ('NO_SAVES','SAVED_ONCE_AFTER_2008','REMAINING','UNVERIFIED')}
    for r in rows: groups[r[1]].append(r)
    for cat,name in [('NO_SAVES','01_no_saves.txt'),('SAVED_ONCE_AFTER_2008','02_saved_once_after_2008.txt'),('REMAINING','03_remaining_earliest_full_timestamp.txt'),('UNVERIFIED','04_unverified_retry_needed.txt')]:
        with (out/name).open('w',encoding='utf-8') as f:
            for r in groups[cat]:
                if cat=='NO_SAVES': line=r[0]
                elif cat=='SAVED_ONCE_AFTER_2008': line=f'{r[2]} save | {r[3]} | {r[4]} | {r[0]}'
                elif cat=='REMAINING': line=f'{r[2]} saves | {r[3]} | {r[4]} | {r[5]} | {r[0]}'
                else: line=f'{r[6]} | {r[0]}'
                f.write(line+'\n')
    summary={'processed':len(rows),'categories':{cat:len(arr) for cat,arr in groups.items()},'note':'Count is CDX timestamp records; it may differ from Wayback calendar Saved X figure. Failure does not mean no saves.'}
    (out/'summary.json').write_text(json.dumps(summary,indent=2),encoding='utf-8')

def main():
    p=argparse.ArgumentParser()
    p.add_argument('--start',type=int,default=1,help='1-based starting URL index')
    p.add_argument('--count',type=int,default=5,help='max URLs in this batch')
    p.add_argument('--delay',type=float,default=12,help='seconds between successful requests')
    p.add_argument('--output',default='reports')
    a=p.parse_args()
    if a.start<1 or not 1<=a.count<=100 or a.delay<5: p.error('start>=1, count 1..100, delay>=5')
    urls=list(dict.fromkeys(x.strip() for x in Path('urls.txt').read_text(encoding='utf-8-sig').splitlines() if x.strip()))
    print(f'Loaded {len(urls)} URLs. Batch {a.start} to {min(a.start+a.count-1,len(urls))}',flush=True)
    rows=[]; selection=urls[a.start-1:a.start-1+a.count]
    for offset,url in enumerate(selection):
        print(f'Checking #{a.start+offset}: {url}',flush=True)
        try:
            stamps=get_stamps(url)
            row=make_row(url,stamps)
        except urllib.error.HTTPError as e:
            row=[url,'UNVERIFIED','','','','',f'HTTP {e.code}']
            rows.append(row);export(rows,Path(a.output))
            print(f'HTTP {e.code}: stopping batch, not marking no saves',flush=True)
            break
        except Exception as e:
            row=[url,'UNVERIFIED','','','','',f'{type(e).__name__}: {str(e)[:160]}']
            rows.append(row);export(rows,Path(a.output))
            print(f'{row[6]}: stopping batch',flush=True)
            break
        rows.append(row);export(rows,Path(a.output))
        print(f' -> {row[1]}, {row[2]} capture records',flush=True)
        if offset<len(selection)-1:time.sleep(a.delay)
    print(f'Completed {len(rows)} rows; reports in {a.output}/',flush=True)
if __name__=='__main__':main()
