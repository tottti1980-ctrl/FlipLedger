"""FlipLedger V7 private test server. No secrets belong in this file."""
import base64
import hmac
import json
import os
import re
import threading
import time
from collections import deque
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, parse_qs
from urllib.request import Request, urlopen

HERE = Path(__file__).resolve().parent
LOCK = threading.Lock()
REQUESTS = deque()
DAILY = {'date': date.today().isoformat(), 'count': 0}
MAX_PER_DAY = 20
MAX_PER_MINUTE = 3
EBAY_TOKEN = {'value': '', 'expires_at': 0}


def ebay_access_token():
    """Application token for public Browse API searches (not seller OAuth)."""
    with LOCK:
        if EBAY_TOKEN['value'] and time.monotonic() < EBAY_TOKEN['expires_at']:
            return EBAY_TOKEN['value']
    client_id = os.environ.get('EBAY_CLIENT_ID', '').strip()
    client_secret = os.environ.get('EBAY_CLIENT_SECRET', '').strip()
    if not client_id or not client_secret:
        raise RuntimeError('eBay Production credentials not configured on Render.')
    credentials = base64.b64encode(f'{client_id}:{client_secret}'.encode()).decode()
    body = urlencode({'grant_type': 'client_credentials',
                      'scope': 'https://api.ebay.com/oauth/api_scope'}).encode()
    request = Request('https://api.ebay.com/identity/v1/oauth2/token', data=body,
                      headers={'Authorization': 'Basic ' + credentials,
                               'Content-Type': 'application/x-www-form-urlencoded'},
                      method='POST')
    with urlopen(request, timeout=20) as response:
        data = json.load(response)
    token = data['access_token']
    expires = max(60, int(data.get('expires_in', 7200)) - 120)
    with LOCK:
        EBAY_TOKEN.update(value=token, expires_at=time.monotonic() + expires)
    return token


def ebay_search(query):
    token = ebay_access_token()
    url = ('https://api.ebay.com/buy/browse/v1/item_summary/search?' +
           urlencode({'q': query, 'limit': 10, 'filter': 'buyingOptions:{FIXED_PRICE}'}))
    request = Request(url, headers={
        'Authorization': 'Bearer ' + token,
        'X-EBAY-C-MARKETPLACE-ID': 'EBAY_AU',
        'Accept': 'application/json'
    })
    with urlopen(request, timeout=25) as response:
        data = json.load(response)
    items = []
    for item in data.get('itemSummaries', []):
        price = item.get('price') or {}
        if price.get('currency') != 'AUD':
            continue
        items.append({
            'title': item.get('title', ''),
            'price': price.get('value', ''),
            'currency': 'AUD',
            'condition': item.get('condition', ''),
            'url': item.get('itemWebUrl', ''),
            'shipping': 'Not included; check listing',
        })
    return {'query': query, 'marketplace': 'EBAY_AU',
            'price_type': 'ACTIVE_ASKING_PRICES_NOT_SOLD',
            'verified_sold': False,
            'valuation_available': False,
            'warning': 'These are active asking prices, NOT completed sales. Do not use as verified sold comparables.',
            'items': items}

class Handler(BaseHTTPRequestHandler):
    def send_headers(self, status, content_type, length, extra=None):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(length))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Frame-Options', 'DENY')
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()

    def reply(self, code, data):
        out = json.dumps(data).encode('utf-8')
        self.send_headers(code, 'application/json; charset=utf-8', len(out))
        self.wfile.write(out)

    def authorised(self):
        password = os.environ.get('FLIPLEDGER_PASSWORD', '')
        if not password:
            self.reply(503, {'error': 'Private testing password not configured on Render.'})
            return False
        header = self.headers.get('Authorization', '')
        try:
            scheme, value = header.split(' ', 1)
            if scheme.lower() != 'basic':
                raise ValueError('Invalid scheme')
            credentials = base64.b64decode(value, validate=True).decode('utf-8')
            username, submitted = credentials.split(':', 1)
            if username == 'flipledger' and hmac.compare_digest(submitted, password):
                return True
        except (ValueError, UnicodeError):
            pass
        body = b'Private FlipLedger test. Sign in to continue.'
        self.send_headers(401, 'text/plain; charset=utf-8', len(body), {'WWW-Authenticate': 'Basic realm="FlipLedger private test", charset="UTF-8"'})
        self.wfile.write(body)
        return False

    def do_GET(self):
        if not self.authorised():
            return
        path = urlsplit(self.path)
        if path.path == '/api/ebay/sold':
            return self.reply(503, {
                'price_type': 'VERIFIED_SOLD_UNAVAILABLE',
                'verified_sold': False,
                'items': [],
                'error': 'Verified eBay sold data is not connected. Active listings cannot substitute for completed sales.'
            })
        if path.path == '/api/ebay/search':
            values = parse_qs(path.query)
            query = values.get('q', [''])[0].strip()
            if len(query) < 2 or len(query) > 120:
                return self.reply(400, {'error': 'Search query must be 2–120 characters.'})
            try:
                return self.reply(200, ebay_search(query))
            except RuntimeError as error:
                return self.reply(503, {'error': str(error)})
            except HTTPError as error:
                print('eBay API HTTP status:', error.code, flush=True)
                return self.reply(502, {'error': 'eBay API returned HTTP ' + str(error.code) + '. Check Production credentials and Browse API access.'})
            except (URLError, TimeoutError, ValueError, KeyError) as error:
                print('eBay API request failed:', type(error).__name__, flush=True)
                return self.reply(502, {'error': 'eBay request failed. Check Render logs.'})
        if path.path not in ('/', '/index.html') or path.query:
            return self.reply(404, {'error': 'Not found'})
        try:
            content = (HERE / 'index.html').read_bytes()
        except OSError:
            return self.reply(503, {'error': 'index.html missing from server directory'})
        self.send_headers(200, 'text/html; charset=utf-8', len(content))
        self.wfile.write(content)

    def do_POST(self):
        if not self.authorised():
            return
        if self.path != '/api/identify':
            return self.reply(404, {'error': 'Not found'})
        api_key = os.environ.get('OPENAI_API_KEY', '')
        if not api_key:
            return self.reply(503, {'error': 'AI key not configured yet.'})
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if length < 1 or length > 9_000_000:
                return self.reply(413, {'error': 'Photo too large or missing.'})
            obj = json.loads(self.rfile.read(length))
            image = obj.get('image', '')
            if not isinstance(image, str) or not re.fullmatch(r'data:image/(jpeg|png|webp);base64,[A-Za-z0-9+/=]+', image):
                return self.reply(400, {'error': 'Please use a JPEG, PNG or WebP image.'})
            if len(image) > 8_500_000:
                return self.reply(413, {'error': 'Image too large.'})
            # The limit is shared across all users and resets when the service restarts.
            with LOCK:
                now = time.monotonic()
                today = date.today().isoformat()
                if DAILY['date'] != today:
                    DAILY.update(date=today, count=0)
                while REQUESTS and now - REQUESTS[0] > 60:
                    REQUESTS.popleft()
                if DAILY['count'] >= MAX_PER_DAY or len(REQUESTS) >= MAX_PER_MINUTE:
                    return self.reply(429, {'error': 'Testing request limit reached. Please try later.'})
                DAILY['count'] += 1
                REQUESTS.append(now)
            payload = {
                'model': os.environ.get('FLIPLEDGER_VISION_MODEL', 'gpt-4.1-mini'),
                'messages': [
                    {'role': 'system', 'content': 'Identify resale items strictly from visible photo evidence. Read brand, exact model number, platform, region, format, edition, disc count and barcode only when clearly visible. Do not guess missing details, authenticity, completeness, working condition or sealed status. Distinguish similar-looking models and editions; if exact identification is uncertain, say what label or extra photo is needed in notes. Respond ONLY with a JSON object with keys title, category, edition, confidence, notes. Category must be one of DVD / Blu-ray, Video games, Collectibles, Electronics, Clothing & accessories, Other. confidence must be low, medium, or high. Use empty string for unknown title or edition. Title must be descriptive, factual and at most 80 characters.'},
                    {'role': 'user', 'content': [{'type': 'text', 'text': 'Identify this item based only on what is visible. If uncertain, state uncertainty in notes.'}, {'type': 'image_url', 'image_url': {'url': image}}]}
                ],
                'response_format': {'type': 'json_object'},
                'max_tokens': 350
            }
            request = Request('https://api.openai.com/v1/chat/completions', data=json.dumps(payload).encode(), headers={'Authorization': 'Bearer ' + api_key, 'Content-Type': 'application/json'})
            with urlopen(request, timeout=50) as response:
                result = json.load(response)
            answer = json.loads(result['choices'][0]['message']['content'])
            self.reply(200, {key: answer.get(key, '') for key in ('title', 'category', 'edition', 'confidence', 'notes')})
        except HTTPError as error:
            self.reply(502, {'error': 'AI provider returned error ' + str(error.code) + '. Check billing and model access.'})
        except (ValueError, json.JSONDecodeError):
            self.reply(400, {'error': 'Invalid request data.'})
        except Exception:
            self.reply(500, {'error': 'Recognition failed. Check server logs.'})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', '8765'))
    print('FlipLedger private testing server on port', port, flush=True)
    ThreadingHTTPServer(('0.0.0.0', port), Handler).serve_forever()
