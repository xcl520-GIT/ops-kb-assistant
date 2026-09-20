---
id: F02
title: MySQL 慢查询与主从延迟
volume: F-数据存储与中间件
tags: [mysql, 慢查询, 索引, explain, 主从复制, 复制延迟, sql优化]
aliases: [mysql慢查询, MySQL慢查询, 慢SQL, 慢sql, SQL执行慢, 查询慢, 数据库查询慢, 接口变慢是数据库, 慢查询日志, slow_query_log, 主从延迟, 主从同步延迟, 从库延迟, 复制延迟, Seconds_Behind_Master, 主从不一致, 读写分离读到旧数据, 索引失效, 索引没走, 没用到索引, 全表扫描, explain分析, EXPLAIN, 执行计划, filesort, Using temporary, 回表, 覆盖索引, 联合索引最左前缀, 隐式类型转换, like百分号开头, 大事务, 长事务, relay log, 并行复制, GTID, binlog, 表锁, 元数据锁, MDL, pt-query-digest]
level: L2
prerequisites: []
related: [J01]
status: 生效
updated: 2026-01-01
---

# MySQL 慢查询与主从延迟

> 围绕 MySQL 生产环境最高频的两类问题——单条 SQL 变慢与从库复制延迟，给出诊断入口、索引失效的典型场景清单、以及复制链路的排查与治理方法。

---

## 一、常见症状

| 现象 | 典型表现 | 首要怀疑 |
|---|---|---|
| 单条 SQL 变慢 | 某接口 P99 上升，DB CPU 正常 | 执行计划劣化、索引失效 |
| 全库整体变慢 | 大量 SQL 同时变慢、连接数飙升 | 锁等待、元数据锁、磁盘 IO 饱和 |
| 从库延迟 | `Seconds_Behind_Master` 持续增大 | 大事务、单线程复制、从库规格不足 |
| 读到旧数据 | 刚写入即查询读不到 | 读写分离下的复制延迟 |
| 主从数据不一致 | 校验发现行数/校验和差异 | 非确定性 SQL、复制中断后跳过 |
| 慢查询日志暴涨 | `Slow_queries` 计数突增 | 新增无索引条件、数据量增长触发计划切换 |

## 二、排查思路

**慢查询方向**（自下而上，先拿事实再猜）：

1. 打开并采集慢查询日志，用 `pt-query-digest` 做聚合排序，找出**总耗时贡献最大**的 SQL（而非单次最慢的）。
2. 对目标 SQL 执行 `EXPLAIN` / `EXPLAIN ANALYZE`，看 `type`、`key`、`rows`、`filtered`、`Extra`。
3. 对照索引定义，判断是否因**最左前缀、函数包裹、隐式转换**导致索引不可用。
4. 检查锁：`performance_schema` 与 `information_schema.innodb_trx` 确认是否存在锁等待。
5. 最后才考虑改 SQL 或加索引——**优先消除全表扫描与回表，其次再考虑改写语句**。

**主从延迟方向**（沿着复制链路逐段核对）：

1. 在从库执行 `SHOW REPLICA STATUS`（旧版本为 `SHOW SLAVE STATUS`），确认 `IO_Running` / `SQL_Running` 是否为 `Yes`。
2. 区分延迟来源：是**拉取慢**（网络/主库 binlog 生成慢）还是**回放慢**（从库执行跟不上）。
3. 定位大事务：查主库 `information_schema.innodb_trx` 与 binlog 事件大小。
4. 检查从库回放能力：并行复制是否开启、`replica_parallel_workers` 配置、从库是否被慢查询占用。
5. 检查结构差异：无主键表、缺少索引的表都会让回放退化为全表扫描。

## 三、常用的定位命令

```bash
# 1. 确认慢查询日志已开启（生产建议 long_query_time 设为 0.5~1 秒）
mysql -e "SHOW VARIABLES LIKE 'slow_query_log%'; SHOW VARIABLES LIKE 'long_query_time';"

# 2. 实时查看当前会话与执行时间最长的 SQL
mysql -e "SELECT id,user,time,state,LEFT(info,120) FROM information_schema.processlist ORDER BY time DESC LIMIT 20;"

# 3. 聚合分析慢日志：按总耗时排序，找出真正的 TOP SQL
pt-query-digest /var/log/mysql/slow.log | head -60

# 4. 查看执行计划（8.0.18+ 可用 EXPLAIN ANALYZE 看真实耗时）
mysql -e "EXPLAIN SELECT ... FROM t WHERE ...;"
mysql -e "EXPLAIN ANALYZE SELECT ... FROM t WHERE ...;"

# 5. 查看表上的索引定义，核对最左前缀是否匹配
mysql -e "SHOW INDEX FROM db.t;"

# 6. 查看锁等待（谁在等谁）
mysql -e "SELECT * FROM performance_schema.data_lock_waits\G"

# 7. 查看长事务与未提交事务（大事务是主从延迟的常见元凶）
mysql -e "SELECT trx_id,trx_state,trx_started,TIMESTAMPDIFF(SECOND,trx_started,NOW()) AS secs,trx_rows_modified FROM information_schema.innodb_trx ORDER BY secs DESC;"

# 8. 从库复制状态（8.0.22+ 新语法；旧版本用 SHOW SLAVE STATUS\G）
mysql -e "SHOW REPLICA STATUS\G"

# 9. 只看关键延迟字段（Seconds_Behind_Master 为 NULL 表示复制中断或无法判断）
mysql -e "SHOW REPLICA STATUS\G" | grep -E 'Running|Seconds_Behind|Retrieved_Gtid|Executed_Gtid|Relay_Log_Space'

# 10. 查看并行复制配置（MySQL 8.0 建议 replica_parallel_type=LOGICAL_CLOCK）
mysql -e "SHOW VARIABLES LIKE 'replica_parallel%'; SHOW VARIABLES LIKE 'slave_parallel%';"

# 12. 复制配置核对（与主库比对可发现配置漂移）
mysql -e "SHOW VARIABLES LIKE 'replicate%';"
```

> 说明：`Seconds_Behind_Master` 是粗略值，主库长时间无写入时可能长期显示固定值，跨版本命名存在 `SLAVE_*` 与 `REPLICA_*` 两种写法，以实际版本为准；`pt-query-digest` 来自 Percona Toolkit。

## 四、典型根因与处理

### 4.1 索引失效的六个高频场景

| 场景 | 反例 | 修正 |
|---|---|---|
| 列上做函数/运算 | `WHERE DATE(created_at)='2026-01-01'` | 改范围条件 `created_at >= '2026-01-01' AND created_at < '2026-01-02'` |
| 前导模糊匹配 | `WHERE name LIKE '%abc'` | 改 `LIKE 'abc%'`，或使用全文索引/ES 类方案 |
| 隐式类型转换 | `WHERE phone=13800138000`（列为 varchar） | 参数加引号，保持类型一致 |
| 违反最左前缀 | 索引 `(a,b,c)`，条件只用 `b` | 调整索引顺序或补建索引 |
| 使用 `!=` / `NOT IN` 且区分度低 | `WHERE status != 'deleted'` | 改为正向枚举，或接受全表扫描并做缓存 |

### 4.2 主从延迟的根因矩阵

| 根因 | 判断依据 | 处理 |
|---|---|---|
| 主库大事务 | binlog 事件巨大、`trx_rows_modified` 很高 | 拆分批量操作，小批次提交 |
| 从库单线程回放 | `replica_parallel_workers=0` | 开启 `LOGICAL_CLOCK` 并行复制并调大 worker 数 |
| 从库规格不足 | 从库 CPU/IO 长期饱和 | 提升从库规格或减少从库上额外查询 |
| 无主键/无索引表 | 回放时 `Rows_examined` 很高 | 补主键与必要索引 |
| 复制链路网络差 | `Relay_Log_Space` 持续增长而 SQL 线程忙 | 排查带宽与丢包 |
| 从库被重查询占用 | 从库 `processlist` 有大量长查询 | 读写分离时限制从库重查询，或加专用只读副本 |
| 非确定性 SQL | 语句形式复制下的 `NOW()`/`RAND()` | 使用 `binlog_format=ROW` |

## 五、容易踩的坑

1. **只优化「单次最慢」的 SQL**
   现象：优化了耗时 10 秒的一条，整体负载没变化。
   原因：真正压垮数据库的是「单次 20ms 但每秒上万次」的 SQL。
   正确做法：用 `pt-query-digest` 按**总耗时**（Response time 汇总）排序，优先治理累计贡献大的。

2. **给每个查询条件都建单列索引**
   现象：索引越加越多，写入变慢，优化器反而选错索引。
   原因：单列索引无法覆盖多条件组合，回表次数多，且增加写入与统计信息负担。
   正确做法：按实际查询组合建**联合索引**，遵循最左前缀，并优先做成覆盖索引。

3. **认为加了索引就一定走索引**
   现象：`EXPLAIN` 显示 `type=ALL`，明明有索引。
   原因：优化器基于代价估算，若预估走索引要回表的数据比例过高，会主动选择全表扫描。
   正确做法：降低回表比例（覆盖索引）、更新统计信息（`ANALYZE TABLE`），必要时用 `FORCE INDEX` 并说明理由。

4. **用 `LIMIT` 大偏移分页**
   现象：`LIMIT 1000000,20` 越来越慢。
   原因：MySQL 需要先扫描并丢弃前 100 万行。
   正确做法：改游标分页（`WHERE id > last_id ORDER BY id LIMIT 20`）或使用延迟关联。

5. **见延迟就重启从库或跳过错误**
   现象：`SET GLOBAL SQL_SLAVE_SKIP_COUNTER=1` 后恢复，但数据已经不一致。
   原因：跳过事件直接破坏数据一致性，且掩盖了真实根因。
   正确做法：先定位是网络、大事务还是回放能力问题；确需跳过时先备份并记录 GTID 位置，事后校验数据。

6. **在从库上跑重查询/备份导致延迟**
   现象：白天复制正常，备份窗口一到延迟飙升。
   原因：备份与回放争抢 IO 与 CPU。
   正确做法：错峰执行；`mysqldump` 加 `--single-transaction`；重查询路由到专用只读副本。

7. **忽视长事务与元数据锁**
   现象：`ALTER TABLE` 卡住，随后大量查询堆积。
   原因：长事务持有 MDL，DDL 需要拿排他锁从而阻塞后续所有查询。
   正确做法：先用 `innodb_trx` 找出长事务并清理；DDL 使用在线变更工具（`gh-ost`、`pt-online-schema-change`）并设置锁等待超时。

8. **只看复制是否 Running，不看延迟趋势**
   现象：`IO_Running` 与 `SQL_Running` 都是 `Yes`，业务仍读到旧数据。
   原因：线程正常但延迟长期存在，与"复制中断"是两回事。
   正确做法：把 `Seconds_Behind_Master` 与 GTID 差值纳入监控告警，而不是只看线程状态。
