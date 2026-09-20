---
id: J01
title: Prometheus 监控与告警
volume: J-可观测性
tags: [prometheus, promql, 告警, 监控, 指标, 高基数]
aliases: [prometheus, Prometheus, promql, PromQL, promql写法, 监控告警, 指标监控, 时序监控, 告警规则, 告警规则不生效, 告警不触发, 告警一直触发, 告警风暴, 告警抖动, alertmanager, Alertmanager, rate函数, rate用法, irate, histogram_quantile, P99计算, 分位数计算, 高基数, cardinality, 指标爆炸, 指标数量太多, prometheus内存高, scrape配置, exporter, node_exporter, 采集不到指标, target down, counter重置, recording rule, 预聚合规则, relabel, 标签重写, TSDB, 监控大盘, 采集间隔, evaluation_interval, 指标标签设计]
level: L2
prerequisites: []
related: [D02]
status: 生效
updated: 2026-01-01
---

# Prometheus 监控与告警

> 讲清 Prometheus 的数据模型与指标类型、PromQL 的高频正确写法、告警规则的必备要素，以及高基数、计数器重置、分位数聚合错误等最具破坏性的常见踩坑。

---

## 一、常见症状

| 现象 | 典型表现 | 首要怀疑 |
|---|---|---|
| 告警不触发 | 指标明显异常但无告警 | `rate` 窗口不足、`for` 时长过长 |
| 告警反复抖动 | 同一告警不断 firing / resolved | 阈值贴近稳态值、缺 `for` 抑制 |
| 查询超时 | Grafana 面板长时间转圈 | 高基数指标 + 大范围聚合 |
| Prometheus 内存暴涨 | OOM 或频繁重启 | 高基数 label、抓取目标过多 |

## 二、排查思路

**告警不生效**：按「指标存在 → 表达式正确 → 规则已加载 → Alertmanager 已路由」四步依次验证，不要跳过第一步直接改表达式；先在 Prometheus 的 Graph 页确认指标有数据点。

**查询变慢**：先确认返回的序列数量（Series 数），再看时间范围；基数问题靠减少 label 维度解决，不能靠加大内存。

**数据不准**：确认采样间隔与查询窗口的关系——`rate` 至少需要 2 个样本点，窗口过短时结果为空或剧烈跳变。

## 三、常用的定位命令

```promql
# 1. 目标存活：1=正常，0=抓取失败（排查"指标断流"的起点）
up == 0

# 2. CPU 使用率：对计数器求增速，窗口必须覆盖至少 2 个采样点
100 - (avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)

# 3. P99 延迟：直方图必须先按 le 聚合，再取分位数
histogram_quantile(0.99, sum by (le) (rate(http_request_duration_seconds_bucket[5m])))

# 4. 找出基数最高的指标名，定位「指标爆炸」的源头
topk(10, count by (__name__) ({__name__=~".+"}))

# 5. 检测指标断流：序列不存在时返回 1，用于「采集中断」类告警
absent(up{job="api"})

# 6. 与一周前同时段对比，用于容量趋势判断
sum(rate(http_requests_total[5m])) - sum(rate(http_requests_total[5m] offset 7d))
```

```yaml
groups:
  - name: node-alerts
    rules:
      - alert: NodeMemoryHigh
        expr: (1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes) * 100 > 90
        for: 10m                            # 持续时长：抑制瞬时抖动，避免告警噪音
        labels: {severity: warning}         # 供 Alertmanager 分组与路由
        annotations: {summary: "节点内存使用率过高（{{ $value | printf \"%.0f\" }}%）"}
```

```bash
# 改完规则文件必须校验语法，再热加载（需启动参数 --web.enable-lifecycle）
promtool check rules /etc/prometheus/rules/*.yml
```

## 四、典型根因与处理

### 4.1 指标类型速查

| 指标类型 | 语义 | 能否用 `rate` | 典型用途 |
|---|---|---|---|
| Counter | 只增不减的累计值 | 可以 | 请求总数、错误数、CPU 秒数 |
| Gauge | 可增可减的瞬时值 | 不可以 | 内存占用、连接数 |
| Histogram | 分桶分布（`_bucket`/`_sum`/`_count`） | 需先按 `le` 聚合 | 延迟分位、请求大小 |

### 4.2 高频根因对照

| 问题 | 根因 | 处理 |
|---|---|---|
| `rate` 结果为空或异常 | 窗口内样本点不足 2 个 | 窗口取 `scrape_interval` 的 2~4 倍 |
| `rate` 出现尖峰 | 进程重启导致 counter 归零 | 属预期行为，`rate` 已处理重置；勿用 `delta` 手工相减 |
| 分位数明显偏低 | 未按 `le` 聚合就求分位 | 必须 `sum by (le)` 后再 `histogram_quantile` |
| Prometheus 内存高 | 高基数 label（user_id、request_id、带参 URL） | 移除高基数 label，或用 relabel 丢弃 |
| target 显示 down | relabel 误丢弃、鉴权失败、端口不通 | `promtool check config`，查看 Targets 页报错 |
| 告警风暴 | 无分组、无抑制规则 | 配置 `group_by` 与 `inhibit_rules` |

## 五、容易踩的坑

1. **在 label 里放高基数数据**
   现象：Prometheus 内存与磁盘暴涨，查询超时。
   原因：每个 label 组合生成独立时间序列，`user_id`、`request_id`、带参数的 URL 会让序列数爆炸。
   正确做法：标签只放有限枚举维度（service、method、code、instance）；高基数信息交给日志与链路追踪。

2. **对 Gauge 使用 `rate()`**
   现象：内存使用率算出负数或剧烈跳变。
   原因：`rate` 语义是「每秒增量」，只对 Counter 有意义。
   正确做法：Gauge 直接用瞬时值，需要平滑时用 `avg_over_time` / `max_over_time`。

3. **`rate` 窗口设得太短**
   现象：曲线呈锯齿状并出现大量空值。
   原因：窗口不足 2 个样本点，无法计算增长率。
   正确做法：窗口取 `scrape_interval` 的 2~4 倍，常规用 `[5m]`；检测瞬时突刺才用 `irate`。

4. **直接对 `_bucket` 求分位数**
   现象：算出的 P99 低于 P95，或与业务侧统计差距明显。
   原因：`histogram_quantile` 要求输入已按 `le` 聚合，否则每个实例各算一次分位再叠加。
   正确做法：`histogram_quantile(0.99, sum by (le) (rate(..._bucket[5m])))`。

5. **没有配置 `for`，瞬时抖动直接触发**
   现象：告警频繁触发与恢复，值班人员产生告警疲劳。
   原因：规则缺少持续时长判定，秒级毛刺即命中阈值。
   正确做法：配置 `for`（非即时故障建议 5~10 分钟），并用 `avg_over_time` 抹平尖峰。

6. **用 `irate` 做长期趋势面板**
   现象：面板曲线毛刺严重，难以判断趋势。
   原因：`irate` 只取最后两个样本点，对抖动极其敏感。
   正确做法：趋势用 `rate`，仅在检测瞬时突刺时用 `irate`。

7. **改了规则文件既不校验也不热加载**
   现象：规则未生效，或 Prometheus 启动失败。
   原因：YAML 缩进或表达式语法错误，且未触发配置重载。
   正确做法：先 `promtool check rules`，再 `curl -X POST http://127.0.0.1:9090/-/reload`，最后在 Rules 页面确认已加载。
