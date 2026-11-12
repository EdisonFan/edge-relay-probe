"""Supabase 代理池客户端：第一次领一个就一直用，发现不能用了再换。

三条设计约束（2026-11-12 用户明确）：
  1. 不是每次 HTTP 请求都去池里取。每个 worker 线程第一次领一个出口，
     之后一直复用；只在「这个号不能用了」时才归还并领新的。
  2. 领取按逆时间倒序 / 后进先出：服务端 claim_proxy 用
     ORDER BY last_ok_at DESC NULLS LAST, id DESC，优先给最近探活确认、
     最新入池的出口；sync_proxy_pool 还会按池数组顺序错开 last_ok_at，
     让新补进池的出口（keep+added 的尾部）时间戳更新。
  3. 长期持有期间不换号，只在租约快到时续租（renew_proxy 只推
     lease_until，不领新号）。进程退出由 atexit 统一归还，不锁死池。

去重仍然由服务端 FOR UPDATE SKIP LOCKED 保证：并发 worker 领到的
必然是不同出口，跨 job / 跨仓库也一样。
"""
import atexit
import json
import os
import threading
import time
import urllib.request

_BASE = os.environ.get(
    'SUPABASE_URL', 'https://ripobzmwgmdxyixoevee.supabase.co').rstrip('/')
_KEY = os.environ.get('SUPABASE_ANON_KEY', '')
_LEASE = int(os.environ.get('PROXY_LEASE_SECONDS', '1800'))
_RENEW_AFTER = int(os.environ.get('PROXY_RENEW_AFTER', '1500'))
_PREFIX = os.environ.get('WORKER_ID', 'local')

_H = {
    'apikey': _KEY,
    'Authorization': 'Bearer ' + _KEY,
    'Content-Type': 'application/json',
    'Accept': 'application/json',
}


def _rpc(name, payload, timeout=20):
    req = urllib.request.Request(
        _BASE + '/rest/v1/rpc/' + name,
        data=json.dumps(payload).encode(), headers=_H, method='POST')
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read().decode('utf-8')
    return json.loads(body) if body.strip() else None


class LeasePool:
    """每线程持有一个出口，坏了才换；退出时统一归还。"""

    def __init__(self):
        self._local = threading.local()
        self._all = set()
        self._lock = threading.Lock()
        self.claims = 0        # 领新号次数（含首次）
        self.rotates = 0       # 因失效换号次数
        self.renews = 0        # 续租次数（不换号）
        atexit.register(self.close)

    def _worker(self):
        w = getattr(self._local, 'worker', None)
        if w is None:
            w = '%s-t%d' % (_PREFIX, threading.get_ident())
            self._local.worker = w
        return w

    def _claim(self):
        rows = _rpc('claim_proxy', {
            'p_worker': self._worker(),
            'p_lease_seconds': _LEASE,
        })
        if not rows:
            return None
        ep = rows[0]['out_endpoint']
        self.claims += 1
        with self._lock:
            self._all.add(ep)
        return ep

    def get(self):
        """返回当前持有的出口。首次调用才领，之后直接复用。"""
        cur = getattr(self._local, 'current', None)
        if cur is None:
            cur = self._claim()
            if cur is None:
                return None          # 池暂时空，调用方稍等重试
            self._local.current = cur
            self._local.since = time.time()
            return cur
        # 持有时间到阈值：只续租，不换号、不领新号。
        # 纯本地计时判断，绝大多数 get() 调用不碰 Supabase 接口。
        if time.time() - self._local.since >= _RENEW_AFTER:
            try:
                ok = _rpc('renew_proxy', {
                    'p_endpoint': cur, 'p_worker': self._worker(),
                    'p_lease_seconds': _LEASE,
                })
                self.renews += 1
            except Exception as e:
                ok = None
                print('[pool] renew 异常: %s' % e, flush=True)
            self._local.since = time.time()
            if ok is not True:       # 租约已被回收，换一个继续用
                return self.rotate(ok=True)
        return cur

    def rotate(self, ok=True):
        """当前号不能用了：归还它并立即领一个新号，之后继续长期持有。"""
        old = getattr(self._local, 'current', None)
        if old:
            self.release(old, ok)
            with self._lock:
                self._all.discard(old)
        self._local.current = None
        self.rotates += 1
        return self.get()

    def release(self, endpoint, ok=True):
        """归还。ok=False 让服务端累计 fail_count，连续 3 次标 dead。"""
        try:
            _rpc('release_proxy', {'p_endpoint': endpoint, 'p_ok': bool(ok)})
        except Exception as e:
            print('[pool] release 异常: %s' % e, flush=True)

    def warmup(self):
        """启动自检：第一次取，取完就持有（不归还）。"""
        return self.get()

    def close(self):
        with self._lock:
            eps = list(self._all)
            self._all.clear()
        for ep in eps:
            self.release(ep, True)
        print('[pool] 退出归还 %d 个出口 | 领取 %d / 换号 %d / 续租 %d'
              % (len(eps), self.claims, self.rotates, self.renews), flush=True)


_POOL = None


def get_pool():
    global _POOL
    if _POOL is None:
        if not _KEY:
            raise RuntimeError('SUPABASE_ANON_KEY 未配置，拒绝退化为直连')
        _POOL = LeasePool()
    return _POOL
