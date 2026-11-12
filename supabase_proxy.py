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

去重由服务端 FOR UPDATE SKIP LOCKED 保证：并发 worker 领到的必然是
不同出口，跨 job / 跨仓库也一样。

线程退出回收（2026-11-12 实测修复）：
  采集器在 G1 探测 / 切分 / 翻页等阶段各自 new 一个 ThreadPoolExecutor，
  线程池销毁时 threading.local 数据随线程消失，但没有任何归还钩子，
  它持有的出口会一直挂在 leased 直到租约到期；CPython 还会复用线程 ID，
  表现为「同一个 worker 名下挂着多个租约」。不修的话池会被逐步占满。
  这里用 _holders 记录 (ident -> 出口/线程对象)，在 get() 里按 5 秒节流
  回收 is_alive()==False 的线程持有的出口。绝大多数 get() 仍是纯本地返回。
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
_REAP_EVERY = 5.0
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
        self._holders = {}        # ident -> {'ep', 'thread'}
        self._last_reap = 0.0
        self.claims = 0          # 领新号次数（含首次）
        self.rotates = 0         # 因失效换号次数
        self.renews = 0          # 续租次数（不换号）
        self.reaped = 0          # 回收已退出线程的出口数
        atexit.register(self.close)

    def _worker(self):
        w = getattr(self._local, 'worker', None)
        if w is None:
            w = '%s-t%d' % (_PREFIX, threading.get_ident())
            self._local.worker = w
        return w

    def _reap(self):
        """归还已退出线程遗留的出口。只在节流点执行，正常路径不碰接口。

        用线程对象身份而不是 get_ident()：CPython 会复用已销毁线程的 ident，
        按 ident 排除「自己」会漏掉恰好复用同一 ident 的旧线程登记。
        """
        me = threading.current_thread()
        to_free = []
        with self._lock:
            for ident, h in list(self._holders.items()):
                if h['thread'] is not me and not h['thread'].is_alive():
                    to_free.append(h['ep'])
                    del self._holders[ident]
                    self._all.discard(h['ep'])
        for ep in to_free:
            self.release(ep, True)
            self.reaped += 1

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
            self._holders[threading.get_ident()] = {
                'ep': ep, 'thread': threading.current_thread()}
        return ep

    def get(self):
        """返回当前持有的出口。首次调用才领，之后直接复用。"""
        now = time.time()
        if now - self._last_reap >= _REAP_EVERY:
            self._last_reap = now
            self._reap()

        cur = getattr(self._local, 'current', None)
        if cur is None:
            cur = self._claim()
            if cur is None:
                return None          # 池暂时空，调用方稍等重试
            self._local.current = cur
            self._local.since = now
            return cur
        # 持有时间到阈值：只续租，不换号、不领新号。
        # 纯本地计时判断，绝大多数 get() 调用不碰 Supabase 接口。
        if now - self._local.since >= _RENEW_AFTER:
            try:
                ok = _rpc('renew_proxy', {
                    'p_endpoint': cur, 'p_worker': self._worker(),
                    'p_lease_seconds': _LEASE,
                })
                self.renews += 1
            except Exception as e:
                ok = None
                print('[pool] renew 异常: %s' % e, flush=True)
            self._local.since = now
            if ok is not True:       # 租约已被回收，换一个继续用
                return self.rotate(ok=True)
        return cur

    def rotate(self, ok=True):
        """当前号不能用了：先领新号，再归还旧号，之后继续长期持有。

        顺序不能反：若先归还，LIFO 按 last_ok_at/id 排序会把刚放掉的那个
        号当成最「新鲜」的立刻再发给同一个 worker，等于没换（实测复现）。
        旧号在领取瞬间仍是 leased，claim_proxy 不会选中它。
        """
        old = getattr(self._local, 'current', None)
        self._local.current = None
        with self._lock:
            if old:
                self._all.discard(old)
                self._holders.pop(threading.get_ident(), None)
        self.rotates += 1
        try:
            new = self.get()
        finally:
            if old:
                self.release(old, ok)
        return new

    def release(self, endpoint, ok=True):
        """归还。ok=False 让服务端累计 fail_count、新鲜度回退 1 小时
        （否则 LIFO 会把坏号一直排在队首），连续 3 次标 dead。"""
        try:
            _rpc('release_proxy', {'p_endpoint': endpoint, 'p_ok': bool(ok)})
        except Exception as e:
            print('[pool] release 异常: %s' % e, flush=True)

    def warmup(self):
        """启动自检：第一次取，取完就持有（不归还）。"""
        return self.get()

    def close(self):
        if getattr(self, '_closed', False):
            return
        self._closed = True
        with self._lock:
            eps = list(self._all)
            self._all.clear()
            self._holders.clear()
        for ep in eps:
            self.release(ep, True)
        print('[pool] 退出归还 %d 个出口 | 领取 %d / 换号 %d / 续租 %d / 回收死线程 %d'
              % (len(eps), self.claims, self.rotates, self.renews, self.reaped),
              flush=True)


_POOL = None


def get_pool():
    global _POOL
    if _POOL is None:
        if not _KEY:
            raise RuntimeError('SUPABASE_ANON_KEY 未配置，拒绝退化为直连')
        _POOL = LeasePool()
    return _POOL
