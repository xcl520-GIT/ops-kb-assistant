---
id: F03
title: Redis 常见问题
volume: F-数据存储与中间件
tags: [redis, 大key, 热key, 缓存, 淘汰策略, 持久化, 缓存穿透]
aliases: [redis大key, Redis大key, bigkey, 大key排查, 热key, hotkey, 热点key, redis内存, redis内存满了, 淘汰策略, maxmemory, maxmemory-policy, OOM command not allowed, 缓存穿透, 缓存击穿, 缓存雪崩, redis持久化, RDB, AOF, 混合持久化, redis慢查询, slowlog, redis变慢, 请求超时, redis连接数打满, redis主从, redis内存碎片, fork耗时, 布隆过滤器, 缓存与数据库一致性, 热key打爆单核, redis key过多, SCAN命令, KEYS命令]
level: L2
prerequisites: []
related: [F02, J01]
status: 生效
updated: 2026-01-01
---

# Redis 常见问题

> 覆盖 Redis 生产运维最高频的四类问题：大 key / 热 key 引发的性能与容量异常、内存与淘汰策略、持久化方案取舍、缓存穿透/击穿/雪崩的防护设计，适合缓存治理与故障响应时查阅。

---

## 一、常见症状

| 现象 | 典型表现 | 首要怀疑 |
|---|---|---|
| 单实例某核 CPU 打满 | 整体 QPS 不高但延迟抖动 | 热 key 集中到同一分片 |
| 内存超限 | 写入报 `OOM command not allowed` | `maxmemory` 已满且策略为 `noeviction` |
| 请求偶发超时 | P99 突刺至数百毫秒 | 大 key 阻塞单线程、fork 卡顿 |
| 数据库被打满 | 缓存失效率高，请求全部回源 | 缓存穿透 / 雪崩 |

## 二、排查思路

1. **先分清容量问题与性能问题**：容量看 `INFO memory`，性能看 `SLOWLOG` 与命令耗时统计。
2. **查大 key**：用 `redis-cli --bigkeys` 定位，该命令依赖 `SCAN`，建议在从库执行以免阻塞主库。
3. **查热 key**：用 `--hotkeys`（需 LFU 淘汰策略）、短时 `MONITOR` 采样或客户端埋点统计访问频次。
4. **查持久化与内存回收**：核对 `rdb_last_bgsave_status`、`aof_last_write_status`、`latest_fork_usec`、`evicted_keys`。

## 三、常用的定位命令

```bash
# 1. 内存全景：used_memory / maxmemory / 碎片率 / 命中率
redis-cli INFO memory

# 2. 扫描大 key（在从库执行，避免阻塞主库）
redis-cli --bigkeys

# 3. 查看热 key（要求淘汰策略为 lfu 系列，否则命令不可用）
redis-cli --hotkeys

# 4. 慢查询日志：阈值单位为微秒，10000 表示 10ms
redis-cli SLOWLOG GET 20
redis-cli CONFIG SET slowlog-log-slower-than 10000

# 5. 分批枚举 key，禁止在生产使用 KEYS *
redis-cli --scan --pattern 'user:*' --count 1000 | head -50

# 6. 持久化状态与最近一次 fork 耗时（微秒，越大卡顿越明显）
redis-cli INFO persistence | grep -E 'rdb_last_bgsave_status|aof_last_write_status|latest_fork_usec'

# 7. 淘汰策略与淘汰速率（evicted_keys 持续增长说明内存已不足）
redis-cli CONFIG GET maxmemory-policy
redis-cli INFO stats | grep -E 'evicted_keys|keyspace_hits|keyspace_misses'

# 8. 查看客户端来源分布，定位连接泄漏
redis-cli CLIENT LIST | awk '{print $2}' | cut -d= -f2 | cut -d: -f1 | sort | uniq -c | sort -rn | head
```

## 四、典型根因与处理

### 4.1 大 key 与热 key（判定阈值视版本与业务规模而定）

| 类型 | 判定参考 | 危害 | 处理 |
|---|---|---|---|
| 大 key | String 超 10 KB 或集合元素数万级 | 单线程阻塞、主从同步慢、带宽抖动 | 拆分为多个 key、改 Hash 分片、用 HSCAN 分批遍历 |
| 热 key | 单 key QPS 远超均值 | 集群下某分片单核打满 | 本地缓存、key 加随机后缀打散、多级缓存 |

### 4.2 内存与淘汰策略

| 策略 | 行为 | 适用场景 |
|---|---|---|
| `noeviction` | 拒绝写入并返回错误 | 数据不可丢、把 Redis 当存储用 |
| `allkeys-lru` | 在所有 key 中按最近最少使用淘汰 | 通用纯缓存 |
| `allkeys-lfu` | 在所有 key 中按访问频率淘汰 | 热 key 明显、需抗扫描 |
| `volatile-lru` / `volatile-ttl` | 仅在设置了 TTL 的 key 中淘汰 | 缓存与持久数据混布 |

### 4.3 缓存三大异常

| 问题 | 特征 | 防护 |
|---|---|---|
| 穿透 | 查询不存在的 key，请求全部落到 DB | 布隆过滤器、缓存空值（短 TTL）、参数校验 |
| 击穿 | 单个热 key 过期瞬间并发回源 | 互斥锁重建、逻辑过期 + 异步刷新 |
| 雪崩 | 大量 key 同时过期或实例整体故障 | TTL 加随机抖动、多级缓存、DB 侧限流降级 |

## 五、容易踩的坑

1. **在生产使用 `KEYS *` 或 `FLUSHALL`**
   现象：一条命令导致实例卡住数秒，全站请求超时。
   原因：`KEYS` 是 O(N) 操作且在主线程执行，会阻塞所有其他命令。
   正确做法：改用 `SCAN` 分批遍历；删除大集合用 `UNLINK` 异步释放（4.0+）。

2. **不设置 `maxmemory`，靠操作系统 OOM 兜底**
   现象：进程被 OOM killer 杀掉，或写入直接报错。
   原因：`maxmemory=0` 表示不限制，内存最终由系统层面回收。
   正确做法：显式设置 `maxmemory`（预留 fork 与碎片余量）并选择合适的淘汰策略。

3. **为「防丢数据」把 `appendfsync` 改成 `always`**
   现象：写入吞吐断崖式下降。
   原因：每条命令都执行 fsync，受磁盘同步延迟限制。
   正确做法：多数场景使用 `everysec`；强一致需求应交给关系型数据库而非缓存。

4. **所有缓存 key 使用相同 TTL**
   现象：某一时刻数据库 QPS 突增。
   原因：批量写入的 key 同时过期，流量集中回源形成雪崩。
   正确做法：TTL 加随机抖动（如基础值 ±20%），热点数据用逻辑过期 + 异步重建。

5. **用 `HGETALL` / `SMEMBERS` 读取大集合**
   现象：偶发几百毫秒的延迟尖刺。
   原因：一次性返回全部元素，阻塞主线程并占用大量网络带宽。
   正确做法：改用 `HSCAN` / `SSCAN` 分批获取，或从数据结构层面拆分。

6. **认为「主从 + 哨兵」就不会丢数据**
   现象：主节点故障切换后，最近一段写入丢失。
   原因：复制是异步的，主节点尚未同步的数据在切换后无法恢复。
   正确做法：接受异步复制的丢失窗口；关键路径由数据库兜底，或用 `WAIT` 降低风险。
