#!/usr/bin/env python3
"""Low-frequency, resumable Wayback CDX checker. Standard library only."""
import argparse
import csv
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

COLS = ['original_url','category','indexed_save_count','earliest_date_utc','earliest_timestamp_utc','earliest_snapshot_url','status']
ROOT = Path('reports')
STATE = ROOT / 'results.csv'
QUEUE = ROOT / 'scan_queue.json'
CONTROL = ROOT / 'scan_control.json'
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
    category = 'EARLIEST_AFTER_2018' if first[:8] > '20181231' else 'REMAINING'
    return [url, category, len(stamps), date, first, f'https://web.archive.org/web/{first}/{url}', 'OK']


def load():
    if not STATE.exists():
        return {}
    with STATE.open(newline='', encoding='utf-8-sig') as file:
        reader = csv.DictReader(file)
        if reader.fieldnames != COLS:
            raise ValueError('Existing results.csv has unexpected columns. Refusing to overwrite it.')
        records = {row['original_url']: row for row in reader if row.get('original_url')}
        # Reclassify previously saved results without requesting their URLs again.
        # Do not reinterpret failed requests or genuinely empty capture histories.
        for row in records.values():
            if row['category'] not in ('UNVERIFIED', 'NO_SAVES'):
                first = row.get('earliest_timestamp_utc', '')
                if len(first) != 14 or not first.isdigit():
                    raise ValueError(f"Invalid saved earliest timestamp for {row['original_url']}")
                row['category'] = 'EARLIEST_AFTER_2018' if first[:8] > '20181231' else 'REMAINING'
        return records


def write(urls, records):
    ROOT.mkdir(exist_ok=True)
    ordered = [records[url] for url in urls if url in records]
    temp = STATE.with_suffix('.tmp')
    with temp.open('w', newline='', encoding='utf-8-sig') as file:
        writer = csv.DictWriter(file, fieldnames=COLS)
        writer.writeheader(); writer.writerows(ordered)
    temp.replace(STATE)
    groups = {k: [] for k in ('NO_SAVES', 'EARLIEST_AFTER_2018', 'REMAINING', 'UNVERIFIED')}
    for row in ordered:
        groups[row['category']].append(row)
    names = {'NO_SAVES': '01_no_saves.txt', 'EARLIEST_AFTER_2018': '02_earliest_after_2018.txt',
             'REMAINING': '03_remaining_earliest_full_timestamps.txt', 'UNVERIFIED': '04_unverified_retry_needed.txt'}
    for cat, filename in names.items():
        with (ROOT / filename).open('w', encoding='utf-8') as file:
            for row in groups[cat]:
                url = row['original_url']
                if cat == 'NO_SAVES': line = url
                elif cat == 'UNVERIFIED': line = f"{row['status']} | {url}"
                else: line = f"{row['indexed_save_count']} saves | {row['earliest_date_utc']} | {row['earliest_timestamp_utc']} | {row['earliest_snapshot_url']} | {url}"
                file.write(line + '\n')
    # Remove the obsolete report so it cannot be mistaken for current results.
    (ROOT / '02_saved_once_after_2008.txt').unlink(missing_ok=True)
    summary = {'total_urls': len(urls), 'checked': sum(row['category'] != 'UNVERIFIED' for row in ordered),
               'remaining_unchecked': len(urls) - len(records), 'counts': {k: len(v) for k, v in groups.items()},
               'note': 'Save count = CDX timestamp records; may differ from calendar UI. Failed requests are never classified as NO_SAVES.'}
    (ROOT / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n', encoding='utf-8')


def save_queue(queue):
    ROOT.mkdir(exist_ok=True)
    temp = QUEUE.with_suffix('.tmp')
    temp.write_text(json.dumps(queue, indent=2) + '\n', encoding='utf-8')
    temp.replace(QUEUE)


def save_control(queue, stopped_for_time, completed):
    # A follow-up is needed ONLY if this run ran out of scanning time and
    # there are still selected URLs left. Never loop over previously failed URLs.
    ROOT.mkdir(exist_ok=True)
    control = {
        'auto_continue': bool(stopped_for_time and queue['pending'] and completed > 0),
        'remaining_in_selection': len(queue['pending']),
        'attempted_this_run': completed,
        'stopped_for_time': stopped_for_time,
    }
    CONTROL.write_text(json.dumps(control, indent=2) + '\n', encoding='utf-8')
    return control


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--count', type=int, default=None, help='Optional number of URLs to check in this scan series')
    ap.add_argument('--delay', type=float, default=20, help='Seconds between requests, minimum 15')
    ap.add_argument('--retries', type=int, default=2, help='Extra tries after a temporary failure, up to 3')
    ap.add_argument('--retry-unverified', action='store_true', help='Retry previously unverified, otherwise only new URLs')
    ap.add_argument('--max-minutes', type=float, default=350, help='Graceful scan time budget, default 350 minutes (5h50m)')
    ap.add_argument('--continuation', action='store_true', help='Continue the previously saved selected URL queue')
    args = ap.parse_args()
    if (args.count is not None and args.count < 1) or args.delay < 15 or not 0 <= args.retries <= 3 or args.max_minutes <= 0:
        ap.error('count >= 1 when supplied; delay >=15; retries 0..3; max-minutes > 0')

    deadline = time.monotonic() + args.max_minutes * 60
    request_margin = 60
    urls = list(dict.fromkeys(x.strip() for x in Path('urls.txt').read_text(encoding='utf-8-sig').splitlines() if x.strip()))
    positions = {url: i + 1 for i, url in enumerate(urls)}
    records = load()

    if args.continuation:
        if not QUEUE.exists():
            raise RuntimeError('Missing scan_queue.json; cannot safely continue scan')
        queue = json.loads(QUEUE.read_text(encoding='utf-8'))
        if not isinstance(queue.get('pending'), list) or not isinstance(queue.get('initial_size'), int):
            raise ValueError('Invalid saved scan queue')
        if any(u not in positions for u in queue['pending']):
            raise ValueError('urls.txt changed since scan started; refusing to continue mismatched queue')
    else:
        candidates = [u for u in urls if u not in records or (args.retry_unverified and records[u]['category'] == 'UNVERIFIED')]
        selected = candidates if args.count is None else candidates[:args.count]
        queue = {'pending': selected, 'initial_size': len(selected), 'retry_unverified': args.retry_unverified}
        save_queue(queue)

    print(f"Total {len(urls)} URLs; {len(records)} previous records; "
          f"{len(queue['pending'])} remaining in this scan series (originally {queue['initial_size']})", flush=True)

    completed = 0
    stopped_for_time = False
    while queue['pending']:
        if time.monotonic() + request_margin >= deadline:
            print('Time budget reached; stopping before next URL.', flush=True)
            stopped_for_time = True
            break
        url = queue['pending'][0]
        finished_url = False
        for attempt in range(args.retries + 1):
            if time.monotonic() + request_margin >= deadline:
                print('Time budget reached; leaving this URL for next run.', flush=True)
                stopped_for_time = True
                break
            print(f'URL #{positions[url]}, attempt {attempt + 1}: {url}', flush=True)
            try:
                stamps = fetch(url)
                values = to_row(url, stamps)
                records[url] = dict(zip(COLS, values))
                print(f"  {values[1]} — {values[2]} indexed captures, earliest {values[4]}", flush=True)
                finished_url = True
                break
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
                error = f'{type(exc).__name__}: {str(exc)[:130]}'
                print(f'  {error}', flush=True)
                if attempt < args.retries:
                    retry_after = None
                    if isinstance(exc, urllib.error.HTTPError):
                        retry_value = exc.headers.get('Retry-After') if exc.headers else None
                        if retry_value and retry_value.isdigit():
                            retry_after = int(retry_value)
                    backoff = min(300, max(45 * (2 ** attempt), retry_after or 0))
                    remaining = deadline - time.monotonic() - request_margin
                    if remaining <= 0:
                        stopped_for_time = True
                        break
                    print(f'  Respecting rate limits; retry after {backoff} seconds', flush=True)
                    time.sleep(min(backoff, remaining))
                else:
                    records[url] = dict(zip(COLS, [url, 'UNVERIFIED', '', '', '', '', error]))
                    finished_url = True
            except Exception as exc:
                error = f'{type(exc).__name__}: {str(exc)[:130]}'
                records[url] = dict(zip(COLS, [url, 'UNVERIFIED', '', '', '', '', error]))
                print('  Unexpected error, leaving URL unverified: ' + error, flush=True)
                finished_url = True
                break

        if not finished_url:
            # Do not remove a partially attempted URL from the queue.
            break
        queue['pending'].pop(0)
        completed += 1
        # Checkpoint both outputs after EVERY finished URL.
        write(urls, records)
        save_queue(queue)
        if records[url]['category'] == 'UNVERIFIED':
            print('URL marked UNVERIFIED after failed attempts. Not retrying it in this scan series.', flush=True)
        if queue['pending']:
            remaining = deadline - time.monotonic() - request_margin
            if remaining <= 0:
                stopped_for_time = True
                break
            time.sleep(min(args.delay, remaining))

    write(urls, records)
    save_queue(queue)
    control = save_control(queue, stopped_for_time, completed)
    print(f"Run ended: {completed} URLs attempted; {len(queue['pending'])} left in this selection; "
          f"automatic continuation={control['auto_continue']}", flush=True)
    if queue['pending'] and completed == 0:
        print('No progress this run; automatic continuation disabled to avoid a restart loop.', flush=True)


if __name__ == '__main__':
    main()
