import time
import urllib.error
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


class LimitedRedirect(urllib.request.HTTPRedirectHandler):
    max_redirections = 5


OPENER = urllib.request.build_opener(LimitedRedirect)
NO_REDIRECT_OPENER = urllib.request.build_opener(NoRedirect)


def check_site(site, store, previous=None):
    started = time.monotonic()
    code = None
    try:
        request = urllib.request.Request(site['url'], headers={'User-Agent': 'DeskDash/1.0'}, method='GET')
        opener = OPENER if site.get('allow_redirects', True) else NO_REDIRECT_OPENER
        with opener.open(request, timeout=site.get('timeout_seconds', 6)) as response:
            code = response.status
    except urllib.error.HTTPError as error:
        code = error.code
    except (urllib.error.URLError, TimeoutError, OSError):
        pass
    ms = round((time.monotonic() - started) * 1000)
    expected = site.get('expected_statuses', [200])
    success = code in expected
    streak = 0 if success else store.site_failure_streak(site['id']) + 1
    threshold = site.get('failure_threshold', 3)
    status = ('ONLINE' if ms < site.get('degraded_ms', 1500) else 'DEGRADED') if success else 'OFFLINE' if streak >= threshold else 'DEGRADED'
    if previous and status != previous:
        store.event('website', 'error' if status == 'OFFLINE' else 'success' if status == 'ONLINE' else 'warning', f"{site['name']}: {previous} → {status}")
    store.check(site['id'], status, code, ms, success=success)
    return {'id': site['id'], 'name': site['name'], 'url': site['url'], 'status': status, 'http_status': code, 'ms': ms, 'checked_at': time.time(), 'consecutive_failures': streak, **store.site_history(site['id'])}
