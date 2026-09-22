import time
import urllib.error
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


OPENER = urllib.request.build_opener(NoRedirect)


def check_site(site, store, previous=None):
    started = time.monotonic()
    code = None
    try:
        request = urllib.request.Request(site['url'], headers={'User-Agent': 'DeskDash/1.0'}, method='GET')
        with OPENER.open(request, timeout=6) as response:
            code = response.status
    except urllib.error.HTTPError as error:
        code = error.code
    except (urllib.error.URLError, TimeoutError, OSError):
        pass
    ms = round((time.monotonic() - started) * 1000) if code else None
    expected = site.get('expected_statuses', [200, 301, 302])
    status = 'OFFLINE' if code is None or code >= 500 else 'ONLINE' if code in expected and (ms is None or ms < site.get('degraded_ms', 1500)) else 'DEGRADED'
    if previous and status != previous:
        store.event('website', 'error' if status == 'OFFLINE' else 'success' if status == 'ONLINE' else 'warning', f"{site['name']}: {previous} → {status}")
    store.check(site['id'], status, code, ms)
    return {'id': site['id'], 'name': site['name'], 'url': site['url'], 'status': status, 'http_status': code, 'ms': ms, 'checked_at': time.time(), **store.site_history(site['id'])}
