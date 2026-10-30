"""Finite HTTPS callback reachability experiment; never exchanges provider codes.

Only the existing Manager may create/cancel grants. Provider query contents are
validated in memory, discarded, and never logged or persisted by this process.
"""
from __future__ import annotations

import hmac
import html
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import json
import secrets
import re
import threading
import time
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

ORIGIN = 'https://zhihui.kaogong.art'
PREFIX = '/trae-callback-test/'
COOKIE = 'zh_trae_callback_probe'
MAX_QUERY = 16384
MAX_SESSIONS = 8


class Rejected(Exception):
    def __init__(self, status=400, message='测试请求无效，请重新打开测试页。'):
        self.status, self.message = status, message


def pairs(raw):
    if len(raw) > MAX_QUERY or any(ord(c) < 32 for c in raw):
        raise Rejected()
    try:
        items = parse_qsl(raw, keep_blank_values=True, strict_parsing=True,
                          max_num_fields=32, errors='strict')
    except (ValueError, UnicodeError):
        raise Rejected() from None
    if len(dict(items)) != len(items):
        raise Rejected()
    return dict(items)


def cookie_value(header, name):
    try:
        if (len(header) > 16384 or any(ord(c) < 32 for c in header)
                or sum(part.strip().split('=', 1)[0] == name for part in header.split(';')) > 1):
            raise Rejected()
        value = SimpleCookie(header).get(name)
        result = value.value if value else ''
        if result and not re.fullmatch('[A-Za-z0-9_=.-]{1,8192}', result):
            raise Rejected()
        return result
    except Exception:
        raise Rejected() from None


class Manager:
    """Fixed loopback JSON calls, no redirects, aliases, retries or error echoes."""
    def call(self, cookie, method, path, body=None):
        allowed = {('GET', '/api/me'), ('POST', '/api/auth/review'),
                   ('POST', '/api/auth/start'), ('POST', '/api/auth/cancel')}
        if (method, path) not in allowed or not cookie:
            raise Rejected(401, '请先在智汇登录，再打开测试页。')
        conn = http.client.HTTPConnection('127.0.0.1', 7866, timeout=25)
        try:
            data = None if body is None else json.dumps(body).encode()
            conn.request(method, path, body=data, headers={
                'Host': 'zhihui.kaogong.art', 'X-Forwarded-Proto': 'https',
                'Origin': ORIGIN, 'Cookie': 'wb_session=' + cookie,
                'Content-Type': 'application/json'})
            response = conn.getresponse()
            raw = response.read(131073)
            if response.status != 200 or len(raw) > 131072:
                if response.status == 401:
                    raise Rejected(401, '登录已失效，请回智汇重新登录。')
                raise Rejected(409, 'Manager 未批准此测试。请先完成或取消其他授权；结果待核对时不要重试。')
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except Rejected:
            raise
        except Exception:
            raise Rejected(503, '未能确认 Manager 响应，请回控制台核对，不要重复发起授权。') from None
        finally:
            conn.close()


class Probe:
    def __init__(self, manager=None, clock=time.time, event=None):
        self.manager, self.clock = manager or Manager(), clock
        self.event = event or (lambda: print(json.dumps({'event': 'matching_callback_received',
                                                        'at': int(time.time()),
                                                        'code_exchanged': False}), flush=True))
        self.lock = threading.Lock()
        self.sessions = {}
        self.busy = False

    def identity(self, cookie):
        user = self.manager.call(cookie, 'GET', '/api/me')
        if user.get('role') not in ('admin', 'account_admin') or not user.get('username'):
            raise Rejected(403, '测试需要管理员本人登录。')
        return user

    def owned(self, token, cookie):
        user = self.identity(cookie)
        with self.lock:
            row = self.sessions.get(token)
            if not row or row['owner'] != user['username'] or self.clock() >= row['expires'] + 600:
                raise Rejected(404, '测试会话不存在或已失效。')
            row = dict(row)
        # Rechecks Manager session generation even if the username was recreated.
        result = self.manager.call(cookie, 'POST', '/api/auth/review', {
            'platform': 'trae', 'realm': 'cn', 'state': row['state']})
        items = result.get('sessions') or []
        if (len(items) != 1 or items[0].get('engine_binding') != 'current'
                or items[0].get('status') not in ('waiting', 'expired', 'invalid')
                or (items[0].get('status') == 'invalid' and not row['cancelled'])):
            raise Rejected(409, '原授权已失效或服务已重启，请在控制台核对。')
        return row

    def start(self, cookie):
        user = self.identity(cookie)
        with self.lock:
            self.sessions = {key: row for key, row in self.sessions.items()
                             if self.clock() < row['expires'] + 600}
            if self.busy or len(self.sessions) >= MAX_SESSIONS:
                raise Rejected(409, '当前已有测试正在创建，或测试名额已满，请稍后再试。')
            self.busy = True
        try:
            review = self.manager.call(cookie, 'POST', '/api/auth/review', {'platform': 'trae', 'realm': 'cn'})
            for item in review.get('sessions', []):
                if item.get('requires_reconciliation') or item.get('status') in (
                        'waiting', 'processing', 'engine_changed', 'unconfirmed'):
                    raise Rejected(409, '请先在控制台完成或取消现有 Trae 授权，再开始独立测试。')
            out = self.manager.call(cookie, 'POST', '/api/auth/start', {'platform': 'trae', 'realm': 'cn'})
            if (out.get('platform'), out.get('realm'), out.get('mode')) != ('trae', 'cn', 'callback'):
                raise Rejected(502, '授权响应与测试平台不一致，已停止。')
            parts = urlsplit(out.get('authUrl', ''))
            query = pairs(parts.query)
            trace = query.get('login_trace_id', '')
            if (parts.scheme != 'https' or parts.netloc != 'www.trae.cn' or parts.path != '/authorization'
                    or parts.fragment or query.get('auth_callback_url') != 'http://127.0.0.1:17388/authorize'
                    or query.get('code_challenge_method') != 'S256'
                    or not re.fullmatch('[A-Za-z0-9_-]{43,128}', query.get('code_challenge', ''))
                    or not 32 <= len(trace) <= 128 or any(c not in '0123456789abcdefABCDEF' for c in trace)
                    or not isinstance(out.get('state'), str) or not re.fullmatch('zh-[0-9a-f]{48}', out['state'])
                    or type(out.get('expires_at')) not in (float, int)
                    or not self.clock() < out['expires_at'] <= self.clock() + 600):
                raise Rejected(502, '授权响应未通过固定入口校验；请回控制台取消本次授权。')
            token = secrets.token_hex(32)
            query['auth_callback_url'] = ORIGIN + PREFIX + 'return/' + token
            url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query, quote_via=quote), ''))
            with self.lock:
                self.sessions[token] = {'owner': user['username'], 'state': out['state'], 'trace': trace,
                                        'url': url, 'expires': out['expires_at'], 'received': False,
                                        'cancelled': False}
            return token
        finally:
            with self.lock:
                self.busy = False

    def callback(self, token, cookie, query):
        row = self.owned(token, cookie)
        if row['cancelled'] or self.clock() >= row['expires']:
            raise Rejected(409, '测试授权已取消或过期，没有保存任何授权码。')
        values = pairs(query)
        if (set(values) - {'isRedirect', 'scope', 'authCodeInfo', 'loginTraceID', 'host', 'userRegion', 'userInfo'}
                or values.get('isRedirect') != 'true' or values.get('scope') != 'trae'
                or values.get('host') != 'https://api.trae.com.cn'
                or values.get('userRegion', 'cn') != 'cn'
                or not hmac.compare_digest(values.get('loginTraceID', '').encode(), row['trace'].encode())):
            raise Rejected(400, '回调与本次测试不匹配，没有保存任何授权码。')
        try:
            info = json.loads(values.get('authCodeInfo', ''))
            code = info.get('AuthCode') if isinstance(info, dict) else None
            if not isinstance(code, str) or not 1 <= len(code) <= 4096 or any(ord(c) < 32 for c in code):
                raise ValueError()
        except (ValueError, TypeError):
            raise Rejected(400, '回调不包含有效格式的授权码，没有保存。') from None
        # No code/userInfo/raw URL is stored, hashed, forwarded or reflected.
        with self.lock:
            if self.sessions.get(token) != row:
                raise Rejected(409, '测试状态已变化，请回结果页核对。')
            if row['received']:
                raise Rejected(409, '本次回调已核对，不重复处理。')
            self.sessions[token]['received'] = True
            self.sessions[token]['url'] = ''
        self.event()

    def cancel(self, token, cookie):
        self.owned(token, cookie)
        with self.lock:
            row = dict(self.sessions[token])
        if not row['cancelled']:
            self.manager.call(cookie, 'POST', '/api/auth/cancel', {
                'platform': 'trae', 'realm': 'cn', 'state': row['state']})
            with self.lock:
                self.sessions[token]['cancelled'] = True
                self.sessions[token]['url'] = ''


def page(content):
    return ('''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>智汇 · 回调验证</title>
<style>body{margin:0;background:#fcfdff;color:#253246;font:15px/1.8 "Microsoft YaHei",sans-serif}
main{max-width:620px;margin:8vh auto;padding:28px;background:white;border:1px solid #e4edf7;border-radius:24px}
small,p{color:#64748b}a{color:#42668c}button,.action{display:inline-block;padding:10px 20px;border:1px solid #dce8f3;border-radius:24px;background:#f5f9fd;color:#253246;text-decoration:none;cursor:pointer}form{margin-top:20px}
@media(max-width:680px){main{margin:24px 16px;padding:22px}}</style><main>
<small>ZHIHUI · HTTPS CALLBACK TEST</small>''' + content +
            '<p><a href="/accounts/">返回智汇账号池</a></p></main></html>').encode('utf-8')


class Handler(BaseHTTPRequestHandler):
    server_version = 'ZhihuiCallbackProbe'
    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, *args):
        pass

    def reply(self, status, content=b'', **headers):
        self.send_response(status)
        for key, value in {'Cache-Control': 'no-store', 'Pragma': 'no-cache',
                'Referrer-Policy': 'no-referrer', 'X-Content-Type-Options': 'nosniff',
                'X-Frame-Options': 'DENY', 'Content-Type': 'text/html; charset=utf-8',
                'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
                'Content-Length': str(len(content)), **headers}.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(content)

    def handle_request(self):
        if len(self.path) > MAX_QUERY + 256 or len(self.headers.get_all('Cookie', [])) > 1:
            raise Rejected(400)
        parts = urlsplit(self.path)
        if self.headers.get_all('Host') != ['zhihui.kaogong.art'] or parts.scheme or parts.netloc or parts.fragment:
            raise Rejected(400)
        if self.command == 'POST':
            if (self.headers.get_all('Origin') != [ORIGIN] or self.headers.get('Transfer-Encoding')
                    or not self.headers.get('Content-Length', '').isdigit()
                    or int(self.headers['Content-Length']) > 256):
                raise Rejected(403)
            self.rfile.read(int(self.headers['Content-Length']))
        elif self.command != 'GET':
            raise Rejected(405)
        header = self.headers.get('Cookie', '')
        cookie = cookie_value(header, 'wb_session')
        token = cookie_value(header, COOKIE)
        probe = self.server.probe
        if parts.path == PREFIX and self.command == 'GET' and not parts.query:
            probe.identity(cookie)
            self.reply(200, page('<h1>验证 Trae 能否回到智汇</h1><p>仅测试 HTTPS 回调到达，不兑换授权码，不导入账号。'
                '点击后在 Trae 官方页面由你本人完成登录；这不是新的账号接入方式。</p>'
                '<p>请先完成或取消控制台现有的 Trae 授权。本测试约五分钟有效，不要公开分享链接。</p>'
                '<form method="post" action="start"><button>开始一次回调验证</button></form>'))
        elif parts.path == PREFIX + 'start' and self.command == 'POST' and not parts.query:
            token = probe.start(cookie)
            self.reply(303, Location=PREFIX + 'result', **{'Set-Cookie': COOKIE + '=' + token +
                       '; Path=' + PREFIX + '; Max-Age=900; HttpOnly; Secure; SameSite=Lax'})
        elif parts.path.startswith(PREFIX + 'return/') and self.command == 'GET':
            callback_token = parts.path[len(PREFIX + 'return/'):]
            if len(token) != 64 or not hmac.compare_digest(token.encode(), callback_token.encode()):
                raise Rejected(403, '请在发起测试的同一浏览器完成授权，不要转发链接。')
            probe.callback(token, cookie, parts.query)
            self.reply(303, Location=PREFIX + 'result')
        elif parts.path == PREFIX + 'result' and self.command == 'GET' and not parts.query:
            row = probe.owned(token, cookie)
            if row['received']:
                content = '<h1>Linux 已收到匹配的回调</h1><p>已验证本次 trace 与回调格式。授权码已丢弃，未兑换、未验证有效性，也未添加账号。</p>'
            elif row['cancelled'] or probe.clock() >= row['expires']:
                content = '<h1>本次测试已结束或过期</h1><p>没有添加账号；可返回控制台核对授权状态。</p>'
            else:
                content = '<h1>等待官方页面返回</h1><p>请在同一浏览器登录，勿转发到另一设备。若官方拒绝自定义地址，请只反馈错误文字，不要分享完整回调。</p>'
                content += '<a class="action" target="_blank" rel="noreferrer noopener" href="' + html.escape(row['url'], quote=True) + '">前往 Trae 官方验证</a>'
                content += '<p><a href="result">刷新验证结果</a></p>'
            content += '<form method="post" action="cancel"><button>结束测试并释放授权</button></form>'
            self.reply(200, page(content))
        elif parts.path == PREFIX + 'rejected' and self.command == 'GET' and not parts.query:
            self.reply(400, page('<h1>回调未通过本次测试核对</h1><p>没有兑换或保存授权码。'
                '请使用发起测试的同一浏览器，返回结果页核对；不要分享完整回调地址。</p>'
                '<a href="result">核对测试状态</a>'))
        elif parts.path == PREFIX + 'cancel' and self.command == 'POST' and not parts.query:
            probe.cancel(token, cookie)
            self.reply(303, Location=PREFIX + 'result')
        else:
            raise Rejected(404)

    def do_GET(self):
        try:
            self.handle_request()
        except Rejected as exc:
            # Drop a sensitive callback URL from the address bar even on failure.
            if self.path.startswith(PREFIX + 'return/'):
                self.reply(303, Location=PREFIX + 'rejected')
            else:
                self.reply(exc.status, page('<h1>测试暂未完成</h1><p>' + html.escape(exc.message) + '</p>'))
        except Exception:
            if self.path.startswith(PREFIX + 'return/'):
                self.reply(303, Location=PREFIX + 'rejected')
            else:
                self.reply(503, page('<h1>测试暂不可用</h1><p>请返回控制台核对；没有记录回调内容。</p>'))

    do_POST = do_GET


class Server(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, *args, **kwargs):
        self.slots = threading.BoundedSemaphore(8)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, *args):
        try:
            super().process_request_thread(*args)
        finally:
            self.slots.release()

    def handle_error(self, *args):
        pass


def main():
    server = Server(('127.0.0.1', 17388), Handler)
    server.probe = Probe()
    server.serve_forever()


if __name__ == '__main__':
    main()
