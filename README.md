# edge-relay-probe

专业录取线 `apidata/api/gk/score/special` 第三采集通道。

与第二通道 `edge-cache-lab` 的区别只有一个：**代理改为从 Supabase 池持有复用**。

| | edge-cache-lab（第二仓） | edge-relay-probe（第三仓） |
|---|---|---|
| 代理来源 | 启动时一次性读入 `proxies_cnb.txt` 全量池 | 第一次用代理时从 Supabase 领一个 |
| 使用方式 | `(tag + idx) % n` 全池轮询 | **领到就一直用着，发现不能用了才换** |
| 领取顺序 | 随机 shuffle | **逆时间倒序 / 后进先出**，优先取最新鲜的出口 |
| 去重 | 单 job 内靠下标错开，跨 job 会撞号 | `FOR UPDATE SKIP LOCKED`，跨仓库全局不重复 |
| 池维护 | 每个 job 启动前 `probe_pool.py` 探活 | pool-keeper **每 30 分钟** `sync_proxy_pool` upsert |

## 持有复用（`supabase_proxy.py`）

- **get()**：每个 worker 线程第一次调用时领一个出口（首次领取），之后一直复用。绝大多数 `get()` 是纯本地返回，**不会访问 Supabase 接口**。
- **rotate(ok)**：连接失败、限流、异常业务码时才调用——归还旧号并立即领一个新号。`ok=False` 让服务端累计 `fail_count`，连续 3 次标 `dead`；号是通的但页残缺用 `ok=True` 原样还回。合法完整响应和 1090（与代理无关）都不换号。
- **续租**：租约 30 分钟，持有满 25 分钟在 `get()` 里调一次 `renew_proxy` 只推 `lease_until`，**不换号、不领新号**。崩溃的 job 租约到期由下一次 `claim_proxy` 自动回收。
- **close()**：`atexit` 统一归还所有持有号，不锁死池。
- **worker_id**：`repo-run_id-attempt-combo-thread`，池侧 `leased_by` 可审计租约归属。

## 逆时间倒序 / 后进先出

`claim_proxy` 用

```sql
ORDER BY last_ok_at DESC NULLS LAST, id DESC
FOR UPDATE SKIP LOCKED LIMIT 1
```

`sync_proxy_pool` 在 upsert 时按池数组顺序（`new_pool = keep + added`）给每个出口错开 1 毫秒的 `last_ok_at`，越靠后（刚补进池）的出口时间戳越新，因此**后进先出、优先取到新鲜号**；`id DESC` 作为同一时间戳下的次序保证。

## 结果去向

本仓只是执行通道。结果分支 `special-<省>-<年>` 推到本仓后，`notify.yml` 派发主仓 `sync-from-lab.yml`，由主仓统一回收（防覆盖：主仓已有 `gate_pass=True` 则跳过）。主仓才是唯一真源，负责达标统计与 CNB 1:1 镜像。

## Secrets

| 名称 | 用途 |
|---|---|
| `SUPABASE_ANON_KEY` | 调用 `claim_proxy` / `renew_proxy` / `release_proxy` RPC |
| `LAB_DISPATCH_TOKEN` | 跨仓派发主仓转运工作流 |
