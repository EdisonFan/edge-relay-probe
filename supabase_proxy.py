"""Supabase 代理池客户端：每用一个取一个，绝不取重。

服务端 public.proxy_pool + claim_proxy() 用
    SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1
原子领取并置 leased。并发 worker 抢同一行时，后到的直接跳过被锁住的行，
因此不同 worker / 不同 job / 不同仓库拿到的必然是不同出口。

release_proxy 归还：
  ok=True  -> 回 idle、last_ok_at=now()、fail_count 清零
  ok=False -> fail_count+1，连续 3 次标 dead，不再被领取
pool-keeper 每 2 小时调用 sync_proxy_pool upsert，把新探活出口写进来并复活 dead。
"""
import json
import os
import threading
import time
import urllib.request

_BASE = os.environ.get(
    'SUPABASE_URL', 'https://ripobzmwgmdxyixoevee.supabase.co').rstrip('/')
_KEY = os.environ.get('SUPABASE_ANON_KEY', '')
_LEASE = int(os.environ.get('PROXY_LEASE_SECONDS', '180'))
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
    """线程安全的按需领取池。每个线程一个 worker_id，便于审计租约归属。"""

    def __init__(self):
        self._local = threading.local()
        self.claimed = 0
        self.released = 0
        self.empty = 0

    def _worker(self):
        w = getattr(self._local, 'worker', None)
        if w is None:
            w = '%s-t%d' % (_PREFIX, threading.get_ident())
            self._local.worker = w
        return w

    def acquire(self):
        """领一个出口；池暂时全在租约中时短等重试，返回 None 表示池空。"""
        for attempt in range(4):
            try:
                rows = _rpc('claim_proxy', {
                    'p_worker': self._worker(),
                    'p_lease_seconds': _LEASE,
                })
            except Exception as e:
                print('[pool] claim 异常: %s' % e, flush=True)
                return None
            if rows:
                self.claimed += 1
                return rows[0]['out_endpoint']
            self.empty += 1
            time.sleep(1.0 + attempt * 0.5)
        return None

    def release(self, endpoint, ok=True):
        """归还出口。ok=False 让服务端累计劣迹，连续 3 次标 dead。"""
        try:
            _rpc('release_proxy', {'p_endpoint': endpoint, 'p_ok': bool(ok)})
            self.released += 1
        except Exception as e:
            print('[pool] release 异常: %s' % e, flush=True)

    def selfcheck(self):
        """启动自检：领一个立刻归还，确认凭证与池都可用。"""
        ep = self.acquire()
        if not ep:
            return ''
        self.release(ep, True)
        return ep.split('//')[-1]


_POOL = None


def get_pool():
    global _POOL
    if _POOL is None:
        if not _KEY:
            raise RuntimeError('SUPABASE_ANON_KEY 未配置，拒绝退化为直连')
        _POOL = LeasePool()
    return _POOL
