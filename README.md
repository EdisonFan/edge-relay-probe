# edge-relay-probe

专业录取线 `apidata/api/gk/score/special` 第三采集通道。

与第二通道 `edge-cache-lab` 的区别只有一个：**代理不再预加载**。

| | edge-cache-lab（第二仓） | edge-relay-probe（第三仓） |
|---|---|---|
| 代理来源 | 启动时一次性读入 `proxies_cnb.txt` 全量池 | 每发一个请求从 Supabase 原子领一个 |
| 选号 | `(tag + idx) % n` 轮询 | `claim_proxy()` / `release_proxy()` |
| 去重 | 单 job 内靠下标错开，跨 job 会撞号 | `FOR UPDATE SKIP LOCKED`，跨仓库全局不重复 |
| 池维护 | 每个 job 启动前 `probe_pool.py` 探活 | pool-keeper 每 2 小时 `sync_proxy_pool` upsert |

## 按需领取

`supabase_proxy.py` 的 `LeasePool`：

- **acquire**：调 `claim_proxy(worker, lease)`。服务端把一行置 `leased` 并返回 `endpoint`；并发时 `SKIP LOCKED` 让后到的请求跳过已锁行，因此不同 worker / job / 仓库绝不会领到同一个出口。池暂时全在租约中时短等重试，返回 `None` 表示池空。
- **release**：用完归还。业务成功 `ok=True`（回 `idle`、清零 `fail_count`）；限流、页残缺、连接失败 `ok=False`（`fail_count+1`，连续 3 次标 `dead`）。租约到期未还会被下一次 `claim_proxy` 自动回收。
- **worker_id**：`repo-run_id-attempt-combo-thread`，池侧 `leased_by` 可审计租约归属。

采集器 `collect_special.py` 只改了 `query_strict()` 的选号方式和 `main()` 的启动段，四闸门、A/B 双维度守恒、页级断点等 900 行逻辑与第二仓完全一致。

## 结果去向

本仓只是执行通道。结果分支 `special-<省>-<年>` 推到本仓后，`notify.yml` 派发主仓 `sync-from-lab.yml`，由主仓统一回收（防覆盖：主仓已有 `gate_pass=True` 则跳过）。主仓才是唯一真源，负责达标统计与 CNB 1:1 镜像。

## Secrets

| 名称 | 用途 |
|---|---|
| `SUPABASE_ANON_KEY` | 调用 `claim_proxy` / `release_proxy` RPC |
| `LAB_DISPATCH_TOKEN` | 跨仓派发主仓转运工作流 |
