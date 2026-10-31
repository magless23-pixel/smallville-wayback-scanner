#!/usr/bin/env python3
"""Low-frequency, resumable Wayback CDX checker. Standard library only."""
import argparse
import csv
import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

COLS = ['original_url','category','indexed_save_count','earliest_date_utc','earliest_timestamp_utc','earliest_snapshot_url','status']
ROOT = Path('reports')
STATE = ROOT / 'results.csv'
HEADERS = {'User-Agent': 'SmallvilleArchiveResearch/1.0 (low-frequency personal archive research)', 'Accept': 'application/json'}


def fetch(url):
    q = urllib.parse.urlencode({'url': url, 'matchType': 'exact', 'output': 'json', 'fl': 'timestamp', 'gzip': 'false'})
    req = urllib.request.Request('https://web.archive.org/cdx/search/cdx?' + q, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=55) as resp:
        if resp.status != 200:
            raise RuntimeError(f'HTTP {resp.status}')
        body = resp.read(8_000_001)
        if len(body) > 8_000_000:
            raise ValueError('Response unexpectedly large')
    if not body.strip():
        raise ValueError('Blank CDX response; not interpreted as zero captures')
    table = json.loads(body.decode('utf-8-sig'))
    if not isinstance(table, list) or not table or table[0] != ['timestamp']:
        raise ValueError('Unexpected CDX response; not interpreted as zero captures')
    result = []
    for row in table[1:]:
        if not isinstance(row, list) or len(row) != 1 or not isinstance(row[0], str) or len(row[0]) != 14 or not row[0].isdigit():
            raise ValueError('Unexpected timestamp data')
        result.append(row[0])
    return result


def to_row(url, stamps):
    if not stamps:
        return [url, 'NO_SAVES', 0, '', '', '', 'OK']
    first = min(stamps)
    date = datetime.strptime(first, '%Y%m%d%H%M%S').replace(tzinfo=timezone.utc).strftime('%Y-%m-%d')
    category = 'SAVED_ONCE_AFTER_2008' if len(stamps) == 1 and first[:8] > '20081231' else 'REMAINING'
    return [url, category, len(stamps), date, first, f'https://web.archive.org/web/{first}/{url}', 'OK']


def load():
    if not STATE.exists():
        return {}
    with STATE.open(newline='', encoding='utf-8-sig') as file:
        reader = csv.DictReader(file)
        if reader.fieldnames != COLS:
            raise ValueError('Existing results.csv has unexpected columns. Refusing to overwrite it.')
        return {row['original_url']: row for row in reader if row.get('original_url')}


def write(urls, records):
    ROOT.mkdir(exist_ok=True)
    ordered = [records[url] for url in urls if url in records]
    temp = STATE.with_suffix('.tmp')
    with temp.open('w', newline='', encoding='utf-8-sig') as file:
        writer = csv.DictWriter(file, fieldnames=COLS)
        writer.writeheader(); writer.writerows(ordered)
    temp.replace(STATE)
    groups = {k: [] for k in ('NO_SAVES', 'SAVED_ONCE_AFTER_2008', 'REMAINING', 'UNVERIFIED')}
    for row in ordered:
        groups[row['category']].append(row)
    names = {'NO_SAVES': '01_no_saves.txt', 'SAVED_ONCE_AFTER_2008': '02_saved_once_after_2008.txt',
             'REMAINING': '03_remaining_earliest_full_timestamps.txt', 'UNVERIFIED': '04_unverified_retry_needed.txt'}
    for cat, filename in names.items():
        with (ROOT / filename).open('w', encoding='utf-8') as file:
            for row in groups[cat]:
                url = row['original_url']
                if cat == 'NO_SAVES': line = url
                elif cat == 'UNVERIFIED': line = f"{row['status']} | {url}"
                else: line = f"{row['indexed_save_count']} saves | {row['earliest_date_utc']} | {row['earliest_timestamp_utc']} | {row['earliest_snapshot_url']} | {url}"
                file.write(line + '\n')
    summary = {'total_urls': len(urls), 'checked': sum(row['category'] != 'UNVERIFIED' for row in ordered),
               'remaining_unchecked': len(urls) - len(records), 'counts': {k: len(v) for k, v in groups.items()},
               'note': 'Save count = CDX timestamp records; may differ from calendar UI. Failed requests are never classified as NO_SAVES.'}
    (ROOT / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n', encoding='utf-8')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--count', type=int, default=10, help='Max URLs attempted this run (any positive integer)')
    ap.add_argument('--delay', type=float, default=20, help='Seconds between requests, minimum 15')
    ap.add_argument('--retries', type=int, default=2, help='Extra tries after a temporary failure, up to 3')
    ap.add_argument('--retry-unverified', action='store_true', help='Retry previously unverified, otherwise only new URLs')
    args = ap.parse_args()
    if args.count < 1 or args.delay < 15 or not 0 <= args.retries <= 3:
        ap.error('count >=1; delay >=15; retries 0..3')
    urls = list(dict.fromkeys(x.strip() for x in Path('urls.txt').read_text(encoding='utf-8-sig').splitlines() if x.strip()))
    records = load()
    candidates = [url for url in urls if url not in records or (args.retry_unverified and records[url]['category'] == 'UNVERIFIED')]
    print(f'Total {len(urls)} URLs; {len(records)} prior results; {len(candidates)} eligible to check; this run up to {args.count}', flush=True)
    completed = 0
    for url in candidates[:args.count]:
        index = urls.index(url) + 1
        for attempt in range(args.retries + 1):
            print(f'URL #{index}, attempt {attempt + 1}: {url}', flush=True)
            try:
                stamps = fetch(url)
                values = to_row(url, stamps)
                records[url] = dict(zip(COLS, values))
                print(f"  {values[1]} — {values[2]} indexed captures, earliest {values[4]}", flush=True)
                break
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
                error = f'{type(exc).__name__}: {str(exc)[:130]}'
                print(f'  {error}', flush=True)
                if attempt < args.retries:
                    retry_after = None
                    if isinstance(exc, urllib.error.HTTPError):
                        retry_value = exc.headers.get('Retry-After') if exc.headers else None
                        if retry_value and retry_value.isdigit(): retry_after = int(retry_value)
                    backoff = min(300, max(45 * (2 ** attempt), retry_after or 0))
                    print(f'  Respecting rate limits; retry after {backoff} seconds', flush=True)
                    time.sleep(backoff)
                else:
                    records[url] = dict(zip(COLS, [url, 'UNVERIFIED', '', '', '', '', error]))
            except Exception as exc:
                error = f'{type(exc).__name__}: {str(exc)[:130]}'
                records[url] = dict(zip(COLS, [url, 'UNVERIFIED', '', '', '', '', error]))
                print('  Unexpected error, leaving URL unverified: ' + error, flush=True)
                break
        write(urls, records)
        completed += 1
        if records[url]['category'] == 'UNVERIFIED':
            print('Stopping this run after unsuccessful attempts. Retry the unverified URL in another run.', flush=True)
            break
        if completed < args.count: time.sleep(args.delay)
    write(urls, records)
    print(f'Run ended: {completed} URLs attempted; {len(records)} results stored in reports/results.csv', flush=True)


if __name__ == '__main__':
    main()
