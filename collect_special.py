# -*- coding: utf-8 -*-
"""专业录取线采集器 special-v1（零丢失版）：gk/score/special。

由投档线采集器 collect_cutoff.py v5.3 复制改造：四道闸门、34 省完备切分、
代理轮换、限流判死逻辑完全复用，仅 4 处差异：
  1) API = apidata/api/gk/score/special；输出 out/special_<省>_<年>.json
  2) split_oversize 切分顺序改「批次优先 → 科类兜底」（专业线按科类切不守恒）
  3) BASE 恒 10000 封顶，恒定走「34 省 Σ 为真值」分支（v5.1 已支持）
  4) version = special-v1，与投档线结果区分

实测依据（2026-11 直连 api.zjzw.cn，只读探针）：
  - province_id 34 省为确定性完备划分：54-2021 BASE=357 == 34省Σ=357
  - 41-2025 34省Σ=40891，无片自身封顶，仅 p41=7837 >4000 需二次切分
  - 翻页硬上限 200 页 / 4000 条（201 页起返回空），与投档线一致
  - p41 大片按 local_batch_name 切 Σ=7837 守恒；按 local_type_name 切
    Σ=7817 不守恒（漏艺术类 20 条）。故批次优先；本科批 4004 再按科类
    切 2969+1035=4004 守恒
  - size>20 返回空；2021-2026 各年均有数据（2026 补录中）
  - school_id 全集为 2970 所（db/CSV 同一套），但 34 省切分不依赖它

以下为投档线 v5 的原始设计说明（血缘保留，逻辑同样适用）：

与 v4 的根本区别：
  v4 = 靠样本猜科类/批次做启发式三级切分 + 探测失败片静默跳过
       （if not ok2 or not n: continue）+ 覆盖率 >=95% 就落盘算达标
       -> 会丢数据，且把不完整结果标成"完成"
  v5 = province_id 34 省确定性完备切分 + 四道闸门，任一不过不算完成

零丢失定义（单个 PROV-YEAR）：
  true_nf  = 应采总条数（BASE 未封顶时取 BASE，封顶时取 34 省分片 Σ）
  count    = 落盘的原始记录条数（含接口内重复，不去重）
  必须 count == true_nf，且中间每一步可审计、可复算。

BASE 封顶特例（v5.1，实测 41-2025 / 51-2025）：
  无筛选查询的 numFound 被接口截断在 10000，是假总数；而 34 省分片各自
  未封顶，Σ=10097 / 11327 才是真值。旧逻辑强制「Σ == BASE」使 G2 永远
  判死、一条页都没采（partition_count=0，count 只是上轮 prev 增量）。
  现改为：BASE 达到 NUMFOUND_CAP 且所有分片均未封顶时，以 Σ 分片为守恒
  基准；若某分片自身仍封顶，则 Σ 也不可信，明确判失败等待更细切分，
  绝不拿假数凑合、绝不降级为"覆盖率够就算完成"。

四道闸门：
  G1 base      无筛选首页成功取到 numFound（重试 PROBE_ROUNDS 轮）
  G2 partition 34 省探测零失败 + 无封顶分片 + 守恒基准成立
               （BASE 未封顶：Σ == BASE；BASE 封顶：Σ >= BASE 且以 Σ 为真值）
               >4000 条的片递归按科类/批次切分，每层同样验证 Σ 守恒
  G3 pages     所有页任务成功，失败页换代理补采到 0（MAX_PAGE_ROUNDS 轮）
  G4 count     页任务原始记录总数 part_count == true_nf

gate_pass = G1 && G2 && G3 && G4。
闸门未全过仍写结果文件并推分支（保留增量，绝不丢已采数据），但
collect.yml 会把该 job 标为失败，巡航据此重派；只有 gate_pass=true 才算达标。
G1 失败（连 BASE 都没取到）时不写结果文件，避免用空壳覆盖云端旧结果。

实测依据：province_id 是接口真实生效的筛选参数，34 个值构成确定性
完备划分，不依赖样本猜科类/批次。65-2026 7066 / 62-2025 7140 /
62-2024 6534 / 65-2025 7867 / 13-2026 5778 / 44-2025 8495 /
11-2021 1442 的 34 省 Σ 精确等于 BASE numFound，且无片 >4000 条。

v5.2（job 内失效代理绕过）：连不上(conn_err)累计 DEAD_THRESHOLD 次的代理
在本 job 进程内判死并跳过，不再反复拨。状态只在进程内存，job 结束即消失，
不改白名单文件、不改结果格式、不跨 job。

v5.3（限流号判死）：run #91 实测限流(1069/1074)是主要失败源（44-2026
throttled=4775 / conn_err=1631），被限流的号反复占用 QUERY_ROUNDS 额度，
页补采 12 轮零进展。改为：连续 THROTTLE_DEAD_THRESHOLD 次限流的号也在本
job 内判死跳过，把额度留给能返回数据的号；成功或全判死复活时清零。

special-v1.1（响应体超长自适应 size）：专业线记录字段多（含专业名、选科
编码等），同一分片 size=20 时响应 JSON 可能超过接口 30000 字符上限，接口
直接返回 code=1090「(api)数据长度超过30000个字符」且不返回 item/numFound
（实测 44-2026 的 p44 片：size=20 → 1090，size=10 → numFound=1328）。
run #1 专业线验证批即因此在 p44 片上 8 轮探测全败、G2 判死、一页未采。
1090 与代理无关（换号照样 1090），只能靠减小 size 解决；翻页上限硬编码
200 页且与 size 无关，故 size=10 时单片容量降为 2000 条，片级容量必须
按 ceil(n/size) <= PAGE_CAP 校验，超出则必须继续切分。
本版：探测期按 SIZE_LADDER 自动降 size 并把 size 记入 leaf；采集期若某页
才撞 1090，则该片整片降 size 重采（size 与页码绑定，改 size 会页内容错位）。

special-v1.2（放宽超时与死代理阈值）：run #3/#4 并发跑满 10 个 job 时，
23-2024 / 23-2025 / 13-2021 三个组合闸门未过，统计显示 conn_err 448~525、
ok 仅 94~95——免费代理在 3s 超时下大量超时，被 DEAD_THRESHOLD=3 连续
三次误判死亡，短时间内把整池 130 个代理判死耗尽。同一批成功的组合
（如 13-2024 conn_err=9176/ok=2478）反而能跑完，说明是阈值过紧而非
代理全坏。故：TIMEOUT 3→12s、DEAD_THRESHOLD 3→6、THROTTLE_DEAD_THRESHOLD
3→5、QUERY_ROUNDS 10→16、WORKERS 8→5（单 job 内并发下调，减少同池互踩）。

special-v1.3（科类优先切分 + 批次名实采）：v1.2 后 13-2021/13-2023/
23-2024 仍 G2 失败。直连实测定位：13-2021 的 p13 片父片 numFound=5909，
按批次名切 Σ=5891 少 18 条——漏的是「专科提前批」（18 条）；该省真实
批次名还有「地方专项计划本科批」，与硬编码候选表里的「地方专项计划批」
「高校专项计划批」写法不同，名字对不上必然漏采。改为：
  1) 一级切分改成科类优先（物理 3352 + 历史 2539 + 艺术 18 = 5909，
     与父片精确守恒；科类取值域小而稳定，远比批次名可靠）；
  2) 二级批次切不再用硬编码表，改为翻页实采该片真实出现的批次名，
     保证候选与接口字段完全一致。

special-v1.4（实采递进切分，彻底弃用硬编码候选表）：v1.3 改成科类优先后，
23-2024 的 p23 片按科类切 Σ=4428 仍少 160 条——该省存在硬编码候选表
覆盖不到的科类取值。结论：任何单一维度的硬编码候选表都不可靠。
改为：每一级切分先用翻页实采拿到该片真实出现的维度取值全集（∪兜底候选），
再逐个探测并以 Σ 守恒为唯一通过条件；科类/批次两个维度互为备选，
子片仍超限则换另一维度继续实采细分。任一级不守恒即整片判失败，绝不凑合。

special-v1.5（实采容错续扫）：v1.4 的 discover_vals 遇错即 break，一次限流
就导致取值集严重不全——实测 23-2024 的 p23 片扫到第 19 页即遇限流中断，
只拿到「物理类/历史类」两个值，漏掉「艺术类」，切分仍不守恒。改为：
单页失败重试 3 轮、连续失败 12 页才停止、期间跳页续扫不丢弃已有取值，
并记录 scanned_pages / failed_pages 供审计。
"""
import os, json, time, random, threading, hashlib, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

# 接口基址：默认生产地址；GK_API_BASE 仅供本地 mock 端到端测试覆盖
# （https 走代理需 CONNECT 隧道，mock 用 http 可免隧道，不影响线上行为）。
U = os.environ.get('GK_API_BASE', 'https://api.zjzw.cn/web/api/')
H = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                  'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36',
    'Origin': 'https://gkcx.eol.cn',
    'Referer': 'https://gkcx.eol.cn/',
    'Accept': 'application/json,text/plain,*/*',
    'Content-Type': 'application/json',
}
PROV = os.environ.get('PROV', '13')
YEAR = int(os.environ.get('YEAR', '2026'))
SIZE = 20
PAGE_CAP = 200
ITEM_CAP = PAGE_CAP * SIZE          # 4000：单维度硬翻页上限
SIZE_LADDER = (20, 10, 5)            # 响应体超长(1090)时的 size 降级阶梯
NUMFOUND_CAP = 10000               # 接口无筛选 numFound 封顶值，达到即为不可信假总数
TIMEOUT = 12                        # v1.2：免费代理响应慢，3s 误杀大量可用代理
WORKERS = 5                         # v1.2：单 job 内并发下调，避免自相踩踏
QUERY_ROUNDS = 16                   # 单次查询最多轮换 16 个代理
PROBE_ROUNDS = 8                   # 探测整体重试轮次
MAX_PAGE_ROUNDS = 15               # 页补采轮次
API = 'apidata/api/gk/score/special'

# ---- job 内失效代理绕过（v5.2 + v5.3 限流判死）----
# 一个 job = 一个进程 = 一个 PROV-YEAR，进程退出状态自动消失。
# 不落盘、不跨 job、不改 proxies_cnb.txt、不动 workflow / 共享层 / 结果格式。
# v5.2：conn_err（连不上）连续 DEAD_THRESHOLD 次判死。
# v5.3：1069/1074 限流连续 THROTTLE_DEAD_THRESHOLD 次也判死。run #91 实测
#       限流占失败请求的绝大多数（44-2026 throt=4775 / conn_err=1631），原逻辑
#       把限流当「号是通的」并清零 conn 计数，限流号永远留在池里反复吃掉
#       QUERY_ROUNDS 额度，页补采 12 轮零进展。判死后跳过不占额度，轮换自然
#       收敛到真正能返回数据的号上。
# 成功或全判死复活时清零；判死后本 job 内不再复活。
DEAD_PROXIES = set()               # 已判死代理，轮换时直接跳过
CONN_FAILS = {}                    # proxy -> 连续 conn_err 次数
DEAD_THRESHOLD = 6                 # v1.2：3 次太激进，慢代理被误判死亡清空整池
THROTTLE_FAILS = {}                # proxy -> 连续 1069/1074 次数
THROTTLE_DEAD_THRESHOLD = 5        # v1.2：限流号判死放宽，减少误伤可用号

# ---- v7：代理改为 Supabase 持有复用（第一次取，坏了才换）----
# 原实现启动时一次性读入 proxies_cnb.txt 全量池，之后按 (tag+idx)%n 轮换。
# 现在每个 worker 线程首次用代理时经 POOL.get() 领一个，之后一直复用；
# 连接失败/限流/异常业务码才 POOL.rotate() 归还并领新号。
# 领取按逆时间倒序（last_ok_at DESC, id DESC）保证取到最新鲜的出口；
# SKIP LOCKED 保证并发不取重；长期持有靠 renew_proxy 续租、atexit 归还。
from supabase_proxy import get_pool
POOL = get_pool()

# 34 个院校所在省级行政区代码，构成确定性完备划分
PROVINCE_IDS = ['11', '12', '13', '14', '15', '21', '22', '23', '31', '32',
                '33', '34', '35', '36', '37', '41', '42', '43', '44', '45',
                '46', '50', '51', '52', '53', '54', '61', '62', '63', '64',
                '65', '71', '81', '82']
# 仅当某省片 >4000 条时才启用，作为二级切分兜底
CANDIDATE_TYPES = ['物理类', '历史类', '文科', '理科', '综合', '理工', '文史',
                   '不分文理', '艺术类', '体育类', '物理科目组合', '历史科目组合',
                   '艺术类(不分文理)', '体育类(不分文理)']
CANDIDATE_BATCHES = [
    '本科批', '专科批', '本科提前批', '专科提前批',
    '本科批A段', '本科批B段', '本科批C段', '专科批A段', '专科批B段', '专科批C段',
    '本科提前批A段', '本科提前批B段', '本科提前批C段',
    '本科一批', '本科二批', '本科三批', '高职专科批', '高职(专科)批',
    '普通类一段', '普通类二段', '提前批A类', '提前批B类', '提前批C类',
    '国家专项计划批', '地方专项计划批', '高校专项计划批', '特殊类型批',
    '本科普通批', '专科普通批', '提前批', '艺术类本科批', '体育类本科批',
]


def opener(proxy):
    ph = urllib.request.ProxyHandler({'http': proxy, 'https': proxy} if proxy else {})
    return urllib.request.build_opener(ph)


def call(proxy, page, extra):
    # 硬性收口：job 跑在 GitHub runner 上，proxy=None 就会用 GitHub 共享出口
    # IP 直连目标站；被限流后会影响后续所有采集任务。任何路径都不允许直连。
    if not proxy:
        raise RuntimeError('拒绝直连：代理为空（禁止使用 runner 出口 IP）')
    q = {'uri': API, 'local_province_id': PROV, 'year': YEAR,
         'page': page, 'size': SIZE}
    q.update(extra)
    req = urllib.request.Request(U, data=json.dumps(q).encode(), headers=H, method='POST')
    with opener(proxy).open(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode('utf-8'))


def query_strict(page, extra, proxies, stats, lock, seed=0, min_items=0,
                 size_hint=None):
    """严格查询：worker 持有的出口一直用，发现不能用了才换。

    与 v6（每次请求领+还）的区别：POOL.get() 只在首次调用时领一个出口，
    之后一直复用；连接失败、限流、异常业务码才 POOL.rotate() 归还旧号并
    立即领新号。合法完整响应与 1090（与代理无关）都不换号。

    领取顺序由服务端保证：last_ok_at DESC NULLS LAST, id DESC，
    即逆时间倒序 / 后进先出，优先拿最新鲜的出口。
    SKIP LOCKED 保证并发 worker 不会领到同一个出口。

    proxies 参数保留仅为兼容下游调用链，不再参与选号。
    """
    used_size = int(size_hint or extra.get('size') or SIZE)
    extra = dict(extra)
    extra['size'] = used_size
    max_rounds = QUERY_ROUNDS * 2
    tried, rnd = 0, 0
    while tried < QUERY_ROUNDS and rnd < max_rounds:
        px = POOL.get()              # 首次领取，之后复用；到点自动续租
        rnd += 1
        if px is None:
            time.sleep(1.0)          # 池暂时空，稍等再试
            continue
        if px in DEAD_PROXIES:
            # 本 job 判过的号：号本身没问题，原样还回并换一个继续用
            px = POOL.rotate(ok=True)
            if px is None:
                continue
        tried += 1
        try:
            d = call(px, page, extra)
        except Exception:
            with lock:
                stats['conn_err'] += 1
                CONN_FAILS[px] = CONN_FAILS.get(px, 0) + 1
                if CONN_FAILS[px] >= DEAD_THRESHOLD:
                    DEAD_PROXIES.add(px)
            POOL.rotate(ok=False)    # 连不上：标劣迹 + 领新号
            continue
        code, dat = str(d.get('code')), d.get('data')
        if code == '0000' and isinstance(dat, dict):
            it = dat.get('item') or []
            if len(it) < min_items:
                with lock:
                    stats['short_page'] += 1
                    CONN_FAILS.pop(px, None)
                    THROTTLE_FAILS.pop(px, None)
                # 号是通的，只是页残缺：还回池（不算劣迹）换一个继续用
                POOL.rotate(ok=True)
                continue
            with lock:
                stats['ok'] += 1
                CONN_FAILS.pop(px, None)
                THROTTLE_FAILS.pop(px, None)
            return it, dat.get('numFound'), True, '0000', used_size
        with lock:
            thr = code in ('1069', '1074')
            stats['throttled' if thr else 'other'] += 1
            CONN_FAILS.pop(px, None)
            if thr:
                THROTTLE_FAILS[px] = THROTTLE_FAILS.get(px, 0) + 1
                if THROTTLE_FAILS[px] >= THROTTLE_DEAD_THRESHOLD:
                    DEAD_PROXIES.add(px)
            else:
                THROTTLE_FAILS.pop(px, None)
        if code == '1090':
            with lock:
                stats['oversize'] = stats.get('oversize', 0) + 1
            # 1090 与代理无关，换号没用，保持持有
            return [], None, False, '1090', used_size
        POOL.rotate(ok=False)        # 限流 / 异常业务码：换号
        if code == '1069':
            time.sleep(0.25)
    with lock:
        stats['query_failed'] += 1
    return [], None, False, None, used_size


def probe_slice(extra, label, proxies, stats, lock, seed=0, sizes=None):
    """探测单个分片的 numFound，并对响应体超长(1090)自动降 size 重试。

    numFound 与 size 无关（实测 p44 片 size=10/5 同为 1328；p41 片
    size=10/20 同为 7837），故降 size 只影响响应体长度，不影响守恒基准。

    返回 (numFound, size, ok, code, items)。非 1090 的失败不在本函数内
    重试（换 size 没用），交由 probe_strict_all 换 seed 重试。
    """
    ladder = sizes or SIZE_LADDER
    last_code = None
    for sz in ladder:
        items, nf, ok, code, _used = query_strict(
            1, dict(extra, size=sz), proxies, stats, lock, seed)
        if ok and nf is not None:
            return int(nf), sz, True, code, items
        last_code = code
        if code != '1090':
            return 0, sz, False, code, []
    return 0, ladder[-1], False, last_code, []


def probe_strict_all(specs, proxies, stats, lock, phase, t0):
    """并发严格探测：所有 spec 必须成功；未成功的换 seed 重试。

    specs: [(extra, label)]，label 必须唯一。
    返回 [(extra, label, n, items, ok, size)]，顺序与 specs 一致；
    探测失败的片明确 ok=False（不再静默跳过），交由 G2 判死。
    """
    done = {}
    pending = list(specs)
    for rnd in range(PROBE_ROUNDS):
        if not pending:
            break
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            futs = {ex.submit(probe_slice, e, l, proxies, stats, lock,
                              rnd * 131): (e, l)
                    for e, l in pending}
            for f in as_completed(futs):
                e, l = futs[f]
                n, sz, ok, code, items = f.result()
                if ok and n is not None:
                    done[l] = (e, l, int(n), items, True, sz)
        pending = [(e, l) for e, l in pending if l not in done]
        print('  [探测 %s 轮%d] 成功 %d/%d | %.0fs' %
              (phase, rnd + 1, len(done), len(specs), time.time() - t0), flush=True)
    res = []
    for e, l in specs:
        res.append(done[l] if l in done else (e, l, 0, [], False, SIZE))
    return res


def split_oversize(extra, label, parent_n, sample_items,
                   proxies, stats, lock, t0, audit, parent_capped=False):
    """>4000 条的省片：先按科类切、仍超限再按批次切。

    每层都验证「Σ 子片 numFound == 父片 numFound」且探测零失败；
    任何一层不守恒即返回 False，绝不凑合。
    """
    def one_level(e0, l0, pn, dim, candidates, extra_cands=None):
        vals = {it.get(dim) for it in sample_items if it.get(dim)} | set(candidates)
        specs = [(dict(e0, **{dim: v}), '%s|%s=%s' % (l0, dim, v))
                 for v in sorted(vals)]
        res = probe_strict_all(specs, proxies, stats, lock, l0 + '/' + dim, t0)
        s = sum(x[2] for x in res)
        failed = [x[1] for x in res if not x[4]]
        audit.setdefault('splits', []).append({
            'parent': l0, 'dim': dim, 'parent_n': pn,
            'sum': s, 'failed': failed,
        })
        if failed or s != pn:
            return None, s
        return [(e2, l2, n, sz) for e2, l2, n, it2, ok, sz in res if n > 0], s

    def discover_vals(e0, l0, pn, dim):
        """翻页实采该片真实出现的 dim 取值全集。

        硬编码候选表会漏名：实测 13-2021 的 p13 片真实批次含
        「地方专项计划本科批」「专科提前批」，而候选表写的是
        「地方专项计划批」「高校专项计划批」，名字对不上导致 Σ 少 18 条、
        G2 判死。此处直接翻片收集实际出现的取值，保证候选完备。

        v1.5 要点：翻页遇错（限流/连接失败）不能直接 break——一次限流就会
        让取值集严重不全（实测 23-2024 的 p23 片扫到第 19 页遇限流即中断，
        只拿到「物理类/历史类」两个值，漏掉「艺术类」）。改为：
          1) 单页失败重试若干轮；
          2) 连续失败累计到阈值才判定「扫不动了」，但仍继续跳页重试，
             不放弃已收集到的取值；
          3) 记录 scanned_pages / failed_pages 供审计。
        关键：翻页本身受 PAGE_CAP 限制，父片可能超出可翻范围，因此
        实采只用于「补全候选集合」，最终仍以 Σ 守恒为唯一判据。
        """
        found = set()
        scanned = 0
        failed = 0
        consecutive = 0
        for pg in range(1, PAGE_CAP + 1):
            items = []
            ok = False
            for _ in range(3):                 # 单页重试
                items, _nf, ok, _c, _sz = query_strict(
                    pg, dict(e0, size=SIZE), proxies, stats, lock, 7 * pg)
                if ok:
                    break
                time.sleep(0.5)
            if not ok:
                failed += 1
                consecutive += 1
                if consecutive >= 12:           # 连续失败过多，池可能整体失效
                    break
                continue                       # 跳到下一页继续试，不丢弃已有取值
            consecutive = 0
            if not items:
                break                          # 正常翻页结束（空页）
            scanned += 1
            for it in items:
                v = it.get(dim)
                if v:
                    found.add(v)
        audit.setdefault('discovered', []).append({
            'parent': l0, 'dim': dim, 'parent_n': pn, 'values': sorted(found),
            'scanned_pages': scanned, 'failed_pages': failed,
        })
        return found

    def split_by(e0, l0, pn, dim, fallback_cands, capped=False):
        """按 dim 切分该片：候选 = 实采值 ∪ 兜底候选，逐个探测并验证 Σ 守恒。

        返回 (子片列表, Σ)。Σ 不等于 pn 即不守恒，调用方须换维度或失败。
        capped=True 表示父片 numFound 是接口截断的假数（>=NUMFOUND_CAP），
        此时没有守恒基准，本级只要求子片均未封顶且 Σ 严格大于假数；
        完备性由调用方用「科类/批次两维度 Σ 相等」交叉验证。
        """
        discovered = discover_vals(e0, l0, pn, dim)
        vals = discovered | set(fallback_cands)
        specs = [(dict(e0, **{dim: v}), '%s|%s=%s' % (l0, dim, v))
                 for v in sorted(vals)]
        res = probe_strict_all(specs, proxies, stats, lock, l0 + '/' + dim, t0)
        s = sum(x[2] for x in res)
        failed = [x[1] for x in res if not x[4]]
        audit.setdefault('splits', []).append({
            'parent': l0, 'dim': dim, 'parent_n': pn,
            'sum': s, 'failed': failed, 'discovered': len(discovered),
        })
        if capped:
            # 父片是假数：Σ 必须严格大于它，且子片自身不能再封顶
            if failed or s <= pn or any(x[2] >= NUMFOUND_CAP for x in res):
                return None, s
        elif failed or s != pn:
            return None, s
        return [(e2, l2, n, sz) for e2, l2, n, it2, ok, sz in res if n > 0], s

    # v1.4：科类/批次「实采递进」切分。
    # v1.3 改成科类优先后，23-2024 的 p23 片按科类切 Σ=4428 仍少 160 条，
    # 说明该省存在硬编码候选表覆盖不到的科类取值。结论：任何单一维度的
    # 硬编码候选表都不可靠，必须靠翻页实采拿真实取值全集。
    # 策略：先按科类实采切分（不守恒则换批次实采切分），子片仍超限就
    # 继续用另一维度实采递归细分；每一级都以 Σ 守恒为唯一通过条件，
    # 任何一级不守恒即整片判失败，绝不凑合。
    leaves = []
    plans = []          # (leaves, Σ)，封顶父片时收集两个维度做交叉验证
    for dim1, dim2, cands1, cands2 in (
            ('local_type_name', 'local_batch_name', CANDIDATE_TYPES, CANDIDATE_BATCHES),
            ('local_batch_name', 'local_type_name', CANDIDATE_BATCHES, CANDIDATE_TYPES)):
        lv1, s1 = split_by(extra, label, parent_n, dim1, cands1,
                           capped=parent_capped)
        if lv1 is None:
            continue                      # 该维度不守恒，换另一个维度再试
        leaves = []
        ok_all = True
        for e2, l2, n, sz in lv1:
            # 容量按该片自己的 size 算（size=10 时单片上限 2000 条）
            if n <= PAGE_CAP * sz:
                leaves.append((e2, n, (n + sz - 1) // sz, l2, sz))
                continue
            lv2, _s2 = split_by(e2, l2, n, dim2, cands2)
            if lv2 is None:
                ok_all = False
                break
            for e3, l3, n3, sz3 in lv2:
                if n3 > PAGE_CAP * sz3:
                    ok_all = False       # 再超限 -> G2 失败，等待重派
                    break
                leaves.append((e3, n3, (n3 + sz3 - 1) // sz3, l3, sz3))
            if not ok_all:
                break
        if ok_all:
            if not parent_capped:
                return leaves, True       # 原行为不变，不做多余探测
            plans.append((leaves, s1))
    if parent_capped:
        # 父片总数是接口截断的假数，没有守恒基准。两个独立维度切出的
        # Σ 相等，才能说明候选值完备、没有静默漏片；否则宁可判失败。
        sums = [x[1] for x in plans]
        ok_cross = len(plans) == 2 and sums[0] == sums[1]
        audit.setdefault('capped_cross', []).append({
            'parent': label, 'false_total': parent_n,
            'sums': sums, 'ok': ok_cross,
        })
        if not ok_cross:
            return [], False
        return plans[0][0], True
    return [], False


def build_partition(base_nf, base_capped, sample_items, proxies, stats, lock, t0, audit):
    """34 省确定性完备切分 + Σ 守恒校验。返回 (leaves, complete, true_nf)。

    leaves = [(extra, n, pages, label)]
    true_nf = 本组合的应采总条数，G4 与结果 num_found 都以它为准。

    BASE 未封顶：沿用原严格判据 Σ 省 == BASE。
    BASE 封顶（>= NUMFOUND_CAP）：BASE 是接口截断产生的假总数，改以
    「34 省分片 Σ」为真值。前提是每个分片自身都未封顶且探测零失败——
    完备划分 + 各片未封顶，Σ 才等于真实总量；否则判 G2 失败。
    """
    specs = [({'province_id': pid}, 'p%s' % pid) for pid in PROVINCE_IDS]
    res = probe_strict_all(specs, proxies, stats, lock, '省', t0)
    audit['provinces'] = {x[1][1:]: x[2] for x in res}
    audit['prov_sizes'] = {x[1][1:]: x[5] for x in res}
    audit['prov_failed'] = [x[1] for x in res if not x[4]]
    audit['prov_sum'] = sum(x[2] for x in res)
    audit['base'] = base_nf
    audit['base_capped'] = base_capped
    if audit['prov_failed']:
        return [], False, 0
    # 分片自身封顶 -> 该省 numFound 是接口截断假数，不能直接计入 Σ。
    # 按科类、批次两个维度分别二次切分，两维度 Σ 相等才作为该省真值。
    capped_provs = [(x[1], x[2]) for x in res if x[2] >= NUMFOUND_CAP]
    audit['capped_provs'] = capped_provs
    capped_fixed, prov_sum = [], audit['prov_sum']
    for e, l, n, it, ok, sz in res:
        if n < NUMFOUND_CAP:
            continue
        sub, ok2 = split_oversize(e, l, n, sample_items,
                                  proxies, stats, lock, t0, audit,
                                  parent_capped=True)
        if not ok2 or not sub:
            return [], False, 0
        st = sum(x[1] for x in sub)
        if st <= n:
            return [], False, 0
        audit.setdefault('capped_true', []).append(
            {'prov': l, 'false_total': n, 'true_total': st, 'leaves': len(sub)})
        prov_sum = prov_sum - n + st
        capped_fixed += sub
    audit['prov_sum'] = prov_sum
    if base_capped:
        # 真值只能 >= 被截断的 BASE；若反而更小，说明有片异常，判失败
        if audit['prov_sum'] < base_nf:
            return [], False, 0
        true_nf = audit['prov_sum']
    else:
        if audit['prov_sum'] != base_nf:
            return [], False, 0
        true_nf = base_nf
    audit['true_nf'] = true_nf
    leaves = []
    for e, l, n, it, ok, sz in res:
        if n >= NUMFOUND_CAP:
            continue                      # 封顶片已由 capped_fixed 处理
        if n == 0:
            continue                      # 合法空片，不产生页任务
        # 容量按该片自己的 size 算（size=10 时单片上限 2000 条）
        if n <= PAGE_CAP * sz:
            leaves.append((e, n, (n + sz - 1) // sz, l, sz))
        else:
            sub, ok2 = split_oversize(e, l, n, sample_items,
                                      proxies, stats, lock, t0, audit)
            if not ok2:
                return [], False, 0
            leaves += sub
    leaves += capped_fixed
    # 递归切分后的叶子若仍封顶，同样不能作为守恒计数基准
    if any(n >= NUMFOUND_CAP for _, n, _, _, _ in leaves):
        audit['capped_leaves'] = True
        return [], False, 0
    # 完备切分自检：所有叶子计划条数之和必须等于真值
    if sum(n for _, n, _, _, _ in leaves) != true_nf:
        audit['leaf_sum'] = sum(n for _, n, _, _, _ in leaves)
        return [], False, 0
    audit['leaves'] = len(leaves)
    audit['pages'] = sum(x[2] for x in leaves)
    return leaves, True, true_nf


# ---- 探测计划落盘复用（v1.6）------------------------------------------
# 13-2023 实测：G2 完备切分探测耗时 135.6 分钟（省 7 + 科类 58 + 批次 70），
# 而结果文件只在采完且四闸门全过后才写。job 撞 180 分钟被砍时，这 135
# 分钟算出的 42 个叶子 / 2398 页 / 真值 47551 全部丢失，重跑又从零探测。
# 改动：G2 成功立即把叶子计划单独落盘；重跑时严格校验后直接复用，跳过探测。
PLAN_NEW = 'out/partition_plan.json'        # 本轮生成，待 workflow 推送
PLAN_PREV = 'out/partition_plan_prev.json'  # 上轮从结果分支取回


def save_partition_plan(leaves, true_nf, base_nf, base_capped, audit):
    """G2 完备切分成功后立即落盘，供被超时砍掉后重跑复用。"""
    payload = {
        'prov': PROV, 'year': YEAR,
        'true_nf': true_nf,
        'base_num_found': base_nf,
        'base_capped': bool(base_capped),
        'leaves': [[e, n, pages, label, sz]
                   for e, n, pages, label, sz in leaves],
        'audit': audit,
        'collector_rev': 'special-v1.6-plan-reuse',
        'created_at': time.strftime('%F %T'),
    }
    tmp = PLAN_NEW + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False)
    os.replace(tmp, PLAN_NEW)
    print('  [计划落盘] %d 个叶子 / 真值 %d -> %s' %
          (len(leaves), true_nf, PLAN_NEW), flush=True)


def load_partition_plan():
    """优先用本轮已生成的计划，其次用分支取回的上轮计划。"""
    for p in (PLAN_PREV, PLAN_NEW):
        try:
            return json.load(open(p, encoding='utf-8'))
        except Exception:
            pass
    return None


def from_plan(plan, base_nf, base_capped):
    """校验并重建 leaves；任何不一致都返回 False，回退正常探测路径。

    复用路径必须满足与 build_partition 相同的全部约束，否则 G2 不算通过：
    Σ叶子条数 == 真值、无封顶叶子、单叶子不超翻页容量、页数与条数自洽。
    """
    try:
        if plan.get('prov') != PROV or int(plan.get('year')) != YEAR:
            return [], False, 0
        if plan.get('base_num_found') != base_nf:
            return [], False, 0
        if bool(plan.get('base_capped')) != bool(base_capped):
            return [], False, 0
        true_nf = int(plan['true_nf'])
        leaves = [(e, int(n), int(pages), label, int(sz))
                  for e, n, pages, label, sz in plan['leaves']]
        if not leaves or sum(n for _, n, _, _, _ in leaves) != true_nf:
            return [], False, 0
        if any(n >= NUMFOUND_CAP for _, n, _, _, _ in leaves):
            return [], False, 0
        if any(n > PAGE_CAP * sz for _, n, _, _, sz in leaves):
            return [], False, 0
        if any(pages != (n + sz - 1) // sz for _, n, pages, _, sz in leaves):
            return [], False, 0
        return leaves, True, true_nf
    except Exception:
        return [], False, 0


def dedupe(items):
    """按整条记录内容哈希去重，保持原顺序。"""
    seen, out = set(), []
    for it in items:
        k = hashlib.md5(json.dumps(it, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        if k not in seen:
            seen.add(k)
            out.append(it)
    return out


def collect_pages(leaves, proxies, stats, lock, t0):
    """页任务全采，失败页换代理补采到 0。返回 (res, failed)。

    每个页任务带 expected：非末页须满该片 size 条，末页须为该片余数条；
    不足即由 query_strict 换代理重试，保证 G4 的原始条数守恒可达成。

    special-v1.1：每片带独立 size。若采集中途某页才撞 1090（响应体超长），
    该片整片按更小 size 重采——size 与页码绑定，改 size 后页内容错位，
    不能只补失败页，必须作废旧 size 的全部页后整片重来。
    """
    slices = {}
    for extra, n, pages, label, sz in leaves:
        slices[label] = {'extra': extra, 'n': n, 'size': sz, 'pages': pages}
    res = {}
    failed = []
    for label, st in slices.items():
        for p in range(1, st['pages'] + 1):
            failed.append((label, p))
    for rnd in range(1, MAX_PAGE_ROUNDS + 1):
        if not failed:
            break
        jobs = []
        for label, p in failed:
            st = slices[label]
            exp = (st['size'] if p < st['pages']
                   else st['n'] - (st['pages'] - 1) * st['size'])
            jobs.append((label, p, st['extra'], exp, st['size']))
        total = sum(st['pages'] for st in slices.values())
        new_failed = []
        oversize = set()
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            futs = {ex.submit(query_strict, p, dict(e, size=sz), proxies, stats,
                              lock, rnd * 977, exp, sz): (label, p, e, exp, sz)
                    for label, p, e, exp, sz in jobs}
            for f in as_completed(futs):
                label, p, e, exp, sz = futs[f]
                items, nf, ok, code, usz = f.result()
                if ok:
                    res[(label, p)] = items
                else:
                    if code == '1090':
                        oversize.add(label)
                    new_failed.append((label, p))
        # 1090 的片：整片降 size 重采（作废旧 size 的全部页）
        for label in oversize:
            st = slices[label]
            new_sz = st['size'] // 2
            if new_sz < SIZE_LADDER[-1]:
                print('  [页] 片 %s 响应体超长且 size 已到下限，判失败' % label,
                      flush=True)
                continue
            new_pages = (st['n'] + new_sz - 1) // new_sz
            if new_pages > PAGE_CAP:
                print('  [页] 片 %s 降 size=%d 后需 %d 页 > %d，判失败（需更细切分）'
                      % (label, new_sz, new_pages, PAGE_CAP), flush=True)
                continue
            print('  [页] 片 %s 响应体超长(1090)，整片降 size %d -> %d 重采'
                  % (label, st['size'], new_sz), flush=True)
            st['size'] = new_sz
            st['pages'] = new_pages
            for p in range(1, st['pages'] + 1):
                res.pop((label, p), None)
            new_failed = [(lb, pp) for lb, pp in new_failed if lb != label]
            for p in range(1, new_pages + 1):
                new_failed.append((label, p))
        failed = new_failed
        print('  [页 轮%d] 成功 %d/%d，仍失败 %d | %.0fs' %
              (rnd, len(res), total, len(failed), time.time() - t0), flush=True)
    return res, failed


def main():
    DEAD_PROXIES.clear()               # job 内状态重置（一个进程只跑一个组合）
    CONN_FAILS.clear()
    THROTTLE_FAILS.clear()
    random.seed('v5-%s-%s' % (PROV, YEAR))
    # 第一次取：领一个出口由主线程持有，之后各 worker 线程各自领一个长期复用。
    # 池或凭证不可用直接失败，绝不退化为直连；退出时 atexit 统一归还。
    proxies = []               # 仅为兼容下游调用链保留，不参与选号
    warm = POOL.warmup()
    if not warm:
        print('::error::Supabase 代理池不可用，拒绝直连，任务终止', flush=True)
        raise SystemExit(1)
    lock = threading.Lock()
    stats = {'ok': 0, 'throttled': 0, 'other': 0, 'conn_err': 0,
             'direct': 0,  # 恒为 0：直连已禁用，保留键以兼容结果文件结构
             'query_failed': 0, 'short_page': 0,
             'oversize': 0}  # 1090：响应体超长次数（size 自适应降级触发）
    t0 = time.time()
    os.makedirs('out', exist_ok=True)
    print('=== v7 零丢失采集 PROV=%s YEAR=%s 代理=Supabase持有复用(首次%s/LIFO) 并发=%d ===' %
          (PROV, YEAR, warm, WORKERS), flush=True)

    # 上轮结果作为增量基础（闸门未过时重派，已采数据累积不丢）
    prev_items = []
    if os.path.exists('out/prev.json'):
        try:
            prev_items = json.load(open('out/prev.json', encoding='utf-8')).get('items', [])
        except Exception:
            pass
    print('  上轮增量基础 %d 条' % len(prev_items), flush=True)

    # ---- G1：BASE numFound ----
    base_ok, base_nf, sample, base_size = False, 0, [], SIZE
    for rnd in range(PROBE_ROUNDS):
        nf, sz, ok, code, items = probe_slice({}, 'BASE', proxies, stats, lock,
                                             rnd * 31)
        if ok and nf is not None:
            base_ok, base_nf, sample, base_size = True, int(nf), list(items), sz
            break
        time.sleep(1.0)
    base_capped = bool(base_ok and base_nf >= NUMFOUND_CAP)
    print('  [G1] BASE numFound=%d ok=%s capped=%s size=%d%s' %
          (base_nf, base_ok, base_capped, base_size,
           '（接口截断假数，将以 34 省 Σ 为真值）' if base_capped else ''),
          flush=True)

    # ---- 探测计划复用（v1.6）----
    # 大省完备切分探测实测吃掉 135.6 分钟，job 撞 180 分钟被砍后成果全丢。
    # 优先载入上轮落盘的叶子计划，严格校验通过即跳过探测，直接翻页采集。
    audit = {}
    leaves, part_ok, true_nf = [], False, 0
    plan_used = False
    plan = load_partition_plan() if base_ok else None
    if plan:
        leaves, part_ok, true_nf = from_plan(plan, base_nf, base_capped)
        if part_ok:
            plan_used = True
            audit = dict(plan.get('audit') or {})
            audit['plan_reused'] = True
            print('  [计划复用] 校验通过，跳过完备切分探测：%d 叶子 / %d 页 / 真值 %d' %
                  (len(leaves), sum(x[2] for x in leaves), true_nf), flush=True)
        else:
            print('  [计划复用] 校验未通过（BASE 或切分约束不一致），回退正常探测', flush=True)

    # BASE 前 30 页样本：仅用于超限片递归切分时发现科类/批次实际值。
    # 复用计划时不需要样本，跳过可省下数分钟。
    sample_items = list(sample)
    if base_ok and not plan_used:
        last = min(31, (base_nf + base_size - 1) // base_size)
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            futs = [ex.submit(query_strict, p, {'size': base_size}, proxies,
                              stats, lock, 0)
                    for p in range(2, last + 1)]
            for f in as_completed(futs):
                it, _, ok, _, _ = f.result()
                if ok:
                    sample_items += it
        print('  样本 %d 条（仅用于超限片递归切分兜底）' % len(sample_items), flush=True)
    elif plan_used:
        print('  样本采集跳过（已复用切分计划）', flush=True)

    # ---- G2：34 省完备切分 ----
    if base_ok and not plan_used:
        leaves, part_ok, true_nf = build_partition(
            base_nf, base_capped, sample_items, proxies, stats, lock, t0, audit)
        if part_ok:
            save_partition_plan(leaves, true_nf, base_nf, base_capped, audit)
    print('  [G2] 完备切分 ok=%s 省Σ=%s BASE=%s capped=%s 真值=%s 叶子=%s 页=%s%s' %
          (part_ok, audit.get('prov_sum'), audit.get('base'),
           audit.get('base_capped'), audit.get('true_nf'),
           audit.get('leaves'), audit.get('pages'),
           ' （复用计划）' if plan_used else ''), flush=True)
    if audit.get('prov_failed'):
        print('  [G2] 探测失败省: %s' % ' '.join(audit['prov_failed']), flush=True)
    if audit.get('capped_provs'):
        print('  [G2] 分片自身封顶，Σ 不可信，需更细切分: %s' %
              audit['capped_provs'], flush=True)
    if audit.get('capped_leaves'):
        print('  [G2] 递归切分后仍有封顶叶子，判失败', flush=True)
    if audit.get('leaf_sum') is not None:
        print('  [G2] 叶子计划条数 Σ=%s != 真值 %s' %
              (audit['leaf_sum'], audit.get('true_nf')), flush=True)
    if part_ok is False and not base_capped and audit.get('prov_sum') is not None \
            and audit.get('prov_sum') != audit.get('base'):
        print('  [G2] Σ 不守恒，差 %s 条' %
              (audit.get('base', 0) - audit.get('prov_sum', 0)), flush=True)

    # ---- G3：页零失败 ----
    res, failed = {}, []
    if part_ok:
        res, failed = collect_pages(leaves, proxies, stats, lock, t0)
    print('  [G3] 页成功 %d / 失败 %d' % (len(res), len(failed)), flush=True)

    # ---- G4：条数守恒（按接口原始返回条数，而非去重数）----
    # 实测修正：接口 numFound 统计的是「原始记录条数」，其中可能含内容
    # 完全相同的重复记录（65-2026 实测 18 条）。v5 初版按整条内容 md5
    # 去重后只有 7048/7066 被 G4 判死，但那 18 条并未丢失，只是接口数据
    # 本身重复。零丢失的正确判据是「接口返回的每一条都采到」：
    #   part_count（完备切分页任务的原始记录总数）== numFound
    # 跨页抖动造成的重复会让 part_count > numFound，同样判失败并重派；
    # 某页返回空却算成功，part_count < numFound，也会被这里拦住。
    # BASE 样本页不参与 G4，避免把失败页记录补上造成误判。
    part_items = []
    for key in sorted(res.keys()):
        part_items += res[key]
    part_count = len(part_items)
    part_uniq_count = len(dedupe(part_items))

    gate = {
        'G1_base': base_ok,
        'G2_partition': part_ok,
        'G3_pages_zero_fail': bool(part_ok and len(failed) == 0),
        'G4_count_match': bool(base_ok and part_ok and part_count == true_nf),
    }
    gate_pass = all(gate.values())
    print('  [G4] raw=%d uniq=%d true_nf=%d (BASE=%d capped=%s) ok=%s' %
          (part_count, part_uniq_count, true_nf, base_nf,
           base_capped, gate['G4_count_match']), flush=True)
    print('  === 闸门 %s gate_pass=%s ===' % (gate, gate_pass), flush=True)
    print('  [绕过] job 内判死代理 %d/%d（连续 %d 次连不上）；'
          '进程内跳过，job 结束即释放，不落盘不跨 job' %
          (len(DEAD_PROXIES), len([p for p in proxies if p is not None]),
           DEAD_THRESHOLD), flush=True)

    # 达标 -> 落盘原始全量（一条不少，含接口内重复，count 严格等于 numFound）
    # 未达标 -> 增量暂存，最大化保留已采数据供下轮续采
    if gate_pass:
        out_items = part_items
    else:
        out_items = dedupe(prev_items + sample_items + part_items)

    path = 'out/special_%s_%s.json' % (PROV, YEAR)
    if not base_ok:
        # 连 BASE 都取不到：不写结果文件，避免空壳覆盖云端旧结果
        print('  G1 失败，本轮不落盘（云端旧结果保持不变，等待重派）', flush=True)
        print('RESULT_JSON ' + json.dumps(
            {'prov': PROV, 'year': YEAR, 'gate_pass': False, 'gate': gate,
             'num_found': 0, 'count': 0}, ensure_ascii=False), flush=True)
        return

    # num_found 写守恒基准真值：封顶组合以 34 省 Σ 为准，不再是假 BASE。
    # 未达标（G2 不过、true_nf=0）时保留 BASE，避免验收侧出现 nf=0 的误判。
    nf_out = true_nf if part_ok else base_nf
    out = {
        'version': 'special-v1', 'prov': PROV, 'year': YEAR, 'api': API,
        'collector_rev': 'special-v1.6-plan-reuse' if plan_used else
                         'special-v1.5-resilient',  # 审计标识
        'num_found': nf_out, 'count': len(out_items),
        'base_num_found': base_nf,       # 接口原始返回值（封顶时为假数，仅作审计）
        'base_capped': base_capped,
        'true_num_found': true_nf,       # 守恒基准真值（G4 判据）
        'partition_count': part_count,          # 页任务原始条数，G4 判据
        'dup_in_api': part_count - part_uniq_count,   # 接口数据内含重复条数，供审计
        'coverage_pct': round(100.0 * len(out_items) / nf_out, 1) if nf_out else 0,
        'gate_pass': gate_pass, 'gate': gate,
        'partition': audit,
        'pages_total': audit.get('pages', 0), 'pages_ok': len(res),
        'pages_failed': len(failed), 'prev_count': len(prev_items),
        'workers': WORKERS, 'timeout': TIMEOUT, 'item_cap': ITEM_CAP,
        'query_rounds': QUERY_ROUNDS, 'probe_rounds': PROBE_ROUNDS,
        'page_rounds': MAX_PAGE_ROUNDS,
        'elapsed': round(time.time() - t0, 1), 'stats': stats,
        'collected_at': time.strftime('%F %T'),
        'build_id': os.environ.get('GITHUB_RUN_ID', os.environ.get('CNB_BUILD_ID', '')),
        'items': out_items,
    }
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False)
    print('=== 完成: %d/%d 条 (BASE=%d capped=%s) / gate_pass=%s / 接口内重复 %d / %.0fs -> %s ===' %
          (len(out_items), nf_out, base_nf, base_capped, gate_pass,
           out['dup_in_api'], time.time() - t0, path), flush=True)
    print('RESULT_JSON ' + json.dumps(
        {'prov': PROV, 'year': YEAR, 'count': len(out_items), 'num_found': nf_out,
         'base_num_found': base_nf, 'base_capped': base_capped,
         'partition_count': part_count, 'dup_in_api': out['dup_in_api'],
         'gate_pass': gate_pass, 'gate': gate, 'pages_failed': len(failed)},
        ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
