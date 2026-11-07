"""Read-only local panel acceptance checks; output never contains source records.

HTTP 200 and top-level ok are insufficient: validate each page's actual nested
source. Empty diaries and verified empty lists are distinct from unavailable.
"""
import argparse
import datetime as dt
import ipaddress
import json
import math
import re
import time
import urllib.parse
import urllib.request

ROUTES = ('/service-health', '/state', '/budget/summary', '/budget', '/bili', '/live',
          '/autonomy/settings', '/activity/journal', '/memory', '/memory/diary',
          '/memes', '/memes/config', '/works', '/files?dir=diary', '/tools',
          '/schedule', '/persona', '/config', '/qq/backend', '/qq/backend/status',
          '/qq/backend/health', '/qq/profile', '/qq/friends', '/qq/groups')


def _need(value):
    if not value:
        raise ValueError('invalid_page_contract')


def _amount(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _timestamp(value):
    if _amount(value):
        return value > 0
    if not isinstance(value, str):
        return False
    try:
        return dt.datetime.fromisoformat(value.replace('Z', '+00:00')).utcoffset() is not None
    except ValueError:
        return False


def _budget(value):
    _need(isinstance(value, dict) and value.get('schema') == 'unified-budget/v2' and
          value.get('availability') == 'available' and value.get('currency') == 'CNY' and
          value.get('basis') == 'owner_confirmed_numeric_cny_estimate' and
          value.get('numeric_scale') == 1 and type(value.get('numeric_scale')) is int)


def _native(value, list_key):
    _need(isinstance(value, dict) and value.get('status') in ('ok', 'partial') and
          isinstance(value.get(list_key), list))
    coverage = value.get('coverage') or {}
    _need(type(coverage.get('owner_verified_scopes')) is int and coverage['owner_verified_scopes'] > 0)
    return value['status'] == 'partial'


def validate(route, value):
    """Return healthy/degraded or raise; partial estimates are not zero costs."""
    _need(isinstance(value, dict))
    bare = route.split('?')[0]
    if bare != '/state' and bare != '/activity/journal':
        _need(value.get('ok') is True)
    degraded = False
    if bare == '/state':
        _need(_timestamp(value.get('at')) and isinstance(value.get('components'), list)
              and isinstance(value.get('plugins'), list) and isinstance(value.get('memory'), dict))
    elif bare in ('/budget', '/budget/summary'):
        budget = value.get('budget'); _budget(budget)
        if bare == '/budget':
            daily = budget.get('daily_spending') or {}
            _need(daily.get('schema') == 'daily-spending/v2' and daily.get('availability') == 'available' and
                  daily.get('currency') == 'CNY' and daily.get('basis') == 'owner_confirmed_numeric_cny_estimate' and
                  type(daily.get('numeric_scale')) is int and daily['numeric_scale'] == 1 and
                  daily.get('timezone') == 'Asia/Shanghai' and isinstance(daily.get('days'), list))
            coverage = daily.get('coverage') or {}
            _need(coverage.get('owner_verified') is True and type(coverage.get('owner_verified_scopes')) is int and
                  coverage['owner_verified_scopes'] > 0 and coverage.get('scope') == 'current_mapped_main_agent_native_turns')
            dates = [row.get('date') for row in daily['days']]
            _need(all(isinstance(day, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', day) for day in dates) and
                  len(dates) == len(set(dates)) and dates == sorted(dates) and daily.get('today') in dates)
            today = next(row for row in daily['days'] if row['date'] == daily['today'])
            _need(today.get('status') in ('partial', 'complete', 'unknown') and
                  (today.get('total') is None or _amount(today['total'])) and
                  (today.get('known_total_cny') is None or _amount(today['known_total_cny'])))
            degraded = bool(coverage.get('reasons')) or today.get('total') is None
    elif bare == '/bili':
        main = (value.get('bili') or {}).get('mainAgent')
        degraded = _native(main, 'recent_videos'); _need(isinstance(main.get('replies'), list))
    elif bare == '/live':
        live = value.get('live') or {}
        _need((live.get('autonomy') or {}).get('mode') == 'unified')
        degraded = _native(live.get('recentActivity'), 'episodes'); _budget(live.get('budget'))
    elif bare == '/activity/journal':
        _need(value.get('status') in ('ok', 'empty', 'not_started') and isinstance(value.get('days'), list))
        degraded = (value.get('freshness') or {}).get('status') == 'degraded'
    elif bare == '/memory':
        memory = value.get('memory') or {}
        _need((memory.get('availability') or {}).get('status') == 'available' and isinstance(memory.get('entries'), list))
    elif bare == '/memory/diary':
        diary = value.get('diary') or {}
        _need(diary.get('status') in ('ok', 'empty', 'partial', 'not_started'))
        degraded = diary['status'] == 'partial'
    elif bare == '/autonomy/settings':
        _need((value.get('settings') or {}).get('availability') == 'available')
    elif bare in ('/memes', '/tools', '/works', '/files'):
        _need(isinstance(value.get({'/memes':'groups', '/tools':'tools', '/works':'files', '/files':'files'}[bare]), list))
    elif bare == '/schedule':
        _need(value.get('mode') in ('unified', 'legacy') and isinstance(value.get('tasks'), list) and
              isinstance(value.get('reminders'), list))
    elif bare in ('/config', '/memes/config'):
        _need(isinstance(value.get('config'), dict))
    elif bare == '/persona':
        _need(isinstance(value.get('text'), str))
    elif bare == '/qq/backend':
        backend = value.get('backend') or {}
        _need(backend.get('schema') == 'qq-backend-selection/v1' and backend.get('selected') in ('napcat', 'snowluma') and
              isinstance(backend.get('revision'), str) and re.fullmatch(r'[a-f0-9]{64}', backend['revision']) and
              isinstance(backend.get('providers'), list) and
              type((backend.get('runtime_guard') or {}).get('ready')) is bool)
        _need(sum(row.get('provider') == backend['selected'] for row in backend['providers'] if isinstance(row, dict)) == 1)
        degraded = backend['runtime_guard']['ready'] is not True
    elif bare == '/qq/backend/status':
        backend = value.get('backend') or {}
        _need(backend.get('provider') in ('napcat', 'snowluma') and type(backend.get('verified')) is bool)
        degraded = backend['verified'] is not True or backend.get('status') != 'online'
    elif bare == '/qq/backend/health':
        backend = value.get('health') or {}
        _need(backend.get('provider') in ('napcat', 'snowluma') and
              all(backend.get(key) is True for key in ('online', 'good', 'logged_in', 'provider_matches', 'account_matches')))
    elif bare in ('/qq/profile', '/napcat/profile'):
        profile = value.get('profile') or {}
        _need(re.fullmatch(r'[1-9][0-9]{0,19}', str(profile.get('user_id', ''))) and isinstance(profile.get('nickname'), str))
    elif bare in ('/qq/friends', '/qq/groups'):
        _need(isinstance(value.get('friends' if bare.endswith('friends') else 'groups'), list))
    return 'degraded' if degraded else 'healthy'


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('redirect_refused')


def local_origin(value):
    url = urllib.parse.urlsplit(value)
    _need(url.scheme == 'http' and url.hostname and not url.username and not url.password and
          not url.query and not url.fragment and url.path in ('', '/') and url.port and 1 <= url.port <= 65535)
    _need(url.hostname == 'localhost' or ipaddress.ip_address(url.hostname).is_loopback)
    return value.rstrip('/')


def check(base, routes=ROUTES, timeout=20):
    base = local_origin(base)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    results = []
    for route in routes:
        _need(route in ROUTES)
        start = time.monotonic()
        try:
            with opener.open(base + '/api' + route, timeout=timeout) as response:
                raw = response.read(8 * 1024 * 1024 + 1)
                _need(response.status == 200 and len(raw) <= 8 * 1024 * 1024)
            state = validate(route, json.loads(raw))
        except Exception:
            # No raw exception, URL, account, file path, text or HTTP body escapes.
            state = 'unavailable'
        results.append(dict(route=route, state=state, seconds=round(time.monotonic() - start, 4)))
    return dict(schema='panel-health/v1', read_only=True,
                ok=all(row['state'] != 'unavailable' for row in results), checks=results)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://127.0.0.1:19388')
    args = parser.parse_args()
    try:
        report = check(args.base_url)
    except (ValueError, TypeError):
        report = dict(schema='panel-health/v1', ok=False, error='invalid_local_origin', read_only=True, checks=[])
    print(json.dumps(report, ensure_ascii=True))
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
