---
id: D02
title: Linux 性能排查
volume: D-操作系统与系统编程
tags: [linux, 性能, cpu, 内存, io, 网络, 排查, use方法]
aliases: [linux性能排查, 服务器很卡, 机器变慢, 系统卡顿, 系统响应慢, 服务响应变慢, CPU使用率高, cpu被打满, 负载高, load average高, 平均负载, 内存不足, 内存快满了, 内存泄漏, 内存一直涨, OOM, 被OOM杀掉, swap用满, swap被大量使用, 磁盘IO高, iowait高, io等待高, 磁盘读写慢, 磁盘打满, 网络丢包, 网络延迟高, 网络抖动, TIME_WAIT过多, CLOSE_WAIT过多, 端口耗尽, 上下文切换多, 软中断高, si高, CPU steal高, top命令, vmstat, iostat, sar, ss命令, perf, USE方法, 性能分析思路]
level: L2
prerequisites: []
related: [H02]
status: 生效
updated: 2026-01-01
---

# Linux 性能排查

> 从 CPU、内存、磁盘 IO、网络四个维度出发，给出 Linux 服务器性能问题的标准定位路径、关键指标含义与可直接执行的诊断命令，适合作为故障响应与容量评估的通用参考。

---

## 一、常见症状

| 现象 | 首要怀疑维度 | 关键信号 |
|---|---|---|
| 服务响应变慢、QPS 下跌 | CPU / IO | `load average` 升高、`iowait` 占比高 |
| 系统卡顿、SSH 输入延迟 | 内存 | `available` 极低、swap 换入换出频繁 |
| 接口超时但 CPU 空闲 | 磁盘 IO / 网络 | `%util` 接近 100%、`await` 显著上升 |
| 进程随机被杀 | 内存 | `dmesg` 出现 OOM killer 记录 |
| 请求偶发失败、重传多 | 网络 | `ss -ti` 重传计数增长、网卡 drop 增加 |
| 定时任务堆积、日志延迟 | CPU / IO | 队列长度 `runq-sz` 持续大于核数 |
| 应用吞吐上不去，CPU 却不满 | 软中断 / 锁 | `si` 高、上下文切换 `cs` 激增 |

## 二、排查思路

推荐以 **USE 方法**（Utilization 使用率 / Saturation 饱和度 / Errors 错误）逐层收敛，避免盲目猜测：

1. **先量化，不凭感觉**：用 `uptime`、`vmstat` 做 10 秒级采样，确认问题属于哪一类资源。
2. **区分「谁在消耗」和「被谁拖慢」**：CPU 高可能是结果（等 IO 后集中处理），`iowait` 高才是原因。
3. **定位到进程**：`pidstat`、`iotop` 从系统级下钻到进程级。
4. **定位到线程与调用栈**：`perf top`、`perf record`、`strace`、`bpftrace` 进一步下钻。
5. **对比基线**：与历史同时段（`sar`）或同角色其他机器横向对比，判断是常态还是突变。
6. **验证根因**：提出假设 → 用一条命令证伪 → 再下结论，避免把相关性当因果。

四类资源的一级判断：`load average` 高但 CPU 空闲 → 看 IO；`free` 少但 `available` 足 → 属正常缓存；`%util` 高且 `await` 大 → 磁盘瓶颈；`si` 高 → 网络软中断。

## 三、常用的定位命令

```bash
# 1. 总体负载：1/5/15 分钟平均负载，需与 CPU 核数对比才有意义
uptime

# 2. CPU 分解：us 用户态 / sy 内核态 / wa IO 等待 / st 被虚拟化抢占 / cs 上下文切换
vmstat 1 10

# 3. 每核使用率与软中断占比，用于发现单核跑满或中断不均衡
mpstat -P ALL 1 5

# 4. 定位到进程：CPU、IO、上下文切换三个维度同时看
pidstat -u -d -w 1 10

# 5. 内存全景：重点看 available 列，而非 free 列
free -h

# 6. 内存细节：page cache、slab、swap 换入换出速率
vmstat -s | head -30

# 7. 磁盘 IO 扩展指标：await 单次 IO 平均耗时(ms)、aqu-sz 队列长度、%util 繁忙率
iostat -x 1 5

# 8. 按进程看 IO 排行（-o 只显示有效 IO，-P 按进程聚合）
iotop -oP

# 9. 历史回溯：需 sysstat 定时采集，用于确认「是不是刚变的」
sar -u 1 5; sar -r 1 5; sar -b 1 5

# 10. 连接状态汇总：TIME_WAIT / ESTABLISHED / CLOSE_WAIT 数量
ss -s

# 11. 按状态统计连接数并排序
ss -ant | awk 'NR>1{print $1}' | sort | uniq -c | sort -rn

# 12. TCP 细节：重传、接收/发送队列积压
ss -ti | grep -E 'retrans|rtt|rcv|send'

# 13. 网卡层丢包、错误、队列溢出（接口名按实际替换）
ip -s link show eth0

# 14. 软中断分布，网络高负载时用于确认中断是否集中在单核
cat /proc/softirqs

# 15. 磁盘空间与 inode 使用率（inode 耗尽同样会写失败）
df -h; df -i
```

> 说明：`iostat`、`sar`、`pidstat` 来自 `sysstat` 包；`iotop` 来自 `iotop` 包，读取 `%util` 等具体阈值随内核版本与设备类型（HDD/SSD/NVMe）差异很大，需结合自身基线判断，不要套用固定数值。

## 四、典型根因与处理

| 维度 | 关键指标 | 常见根因 | 处理方向 |
|---|---|---|---|
| CPU | `us` 高 | 业务计算密集、死循环、正则回溯 | 用 `perf` 定位热点函数；优化算法或扩容 |
| CPU | `sy` 高 | 系统调用频繁、锁竞争 | `strace -c` 统计 syscall；减少小包读写 |
| CPU | `si` 高 | 网络软中断集中、RPS/RSS 未开启 | 开启多队列与 RPS；必要时多网卡分流 |
| CPU | `st` 高 | 虚拟机被宿主机抢占 | 与平台侧核对宿主机负载与超卖比 |
| CPU | `load` 高但 `us/sy` 低 | 大量进程处于不可中断睡眠 | 看 `vmstat` 的 `b` 列与 IO 指标 |
| 内存 | `available` 低 | 应用内存泄漏、缓存膨胀 | 结合 RSS 增长曲线定位；配置 cgroup/limit 兜底 |
| 内存 | swap 换入换出活跃 | 内存不足导致换页抖动 | 优先扩容内存；谨慎关闭 swap 以免触发 OOM |
| 内存 | OOM killer 触发 | 单进程超限、cgroup 限额过小 | 看 `dmesg` 记录确认被杀进程；调整限额 |
| 磁盘 IO | `await` 高、`%util` 满 | 随机小 IO 多、设备已达上限 | 合并写、加大队列深度、换 NVMe、加缓存 |
| 磁盘 IO | `%util` 低但慢 | 文件系统元数据或 fsync 阻塞 | 检查 `df -i`；确认是否写放大 |
| 网络 | 重传率高 | 链路质量差、缓冲区不足 | 调 `tcp_rmem/tcp_wmem`；排查链路 |
| 网络 | `TIME_WAIT` 过多 | 短连接高频、主动关闭方 | 开启 `tcp_tw_reuse`；改连接池复用 |
| 网络 | `CLOSE_WAIT` 堆积 | 应用未正确关闭 socket | 修应用层连接释放逻辑，勿只调内核参数 |

## 五、容易踩的坑

1. **用 `free` 的 free 列判断内存不足**
   现象：看到 `free` 只剩几百 MB 就紧急扩容。
   原因：Linux 会主动把空闲内存用作 page cache，`free` 列低并不代表内存紧张。
   正确做法：看 `available` 列；`buff/cache` 可被回收，`available` 才是"还能给新进程用多少"。

2. **把 `load average` 直接等同于 CPU 使用率**
   现象：负载 8 就断定 CPU 打满，扩容后无改善。
   原因：`load` 统计的是运行中与不可中断睡眠的进程数，大量 IO 阻塞同样推高负载。
   正确做法：结合核数、`vmstat` 的 `us/sy/wa` 与 `iostat` 一起判断。

3. **磁盘 `%util` 高就认定磁盘是瓶颈**
   现象：`%util` 100% 却查不到慢在哪。
   原因：`%util` 基于设备繁忙时间比例，对支持并行的 NVMe/RAID 不具备准确含义。
   正确做法：以 `await` 与 `aqu-sz` 为主，并与设备基线对比。

4. **只看 `top` 的平均值，忽略短时毛刺**
   现象：`top` 显示 CPU 平稳，但业务侧频繁超时。
   原因：秒级平均值会平滑掉毫秒级尖峰。
   正确做法：用 `vmstat 1`、`mpstat -P ALL 1` 连续采样，或用 `perf`、eBPF 观察短时事件。

5. **`iowait` 高就加磁盘**
   现象：换成更快的盘，`wa` 依然高。
   原因：`wa` 表示 CPU 空闲等待 IO 的时间占比，也可能来自网络文件系统或锁等待。
   正确做法：先用 `pidstat -d`、`iotop` 定位到具体进程与文件，再决定优化对象。

6. **误把单核跑满当成整机 CPU 不足**
   现象：整机 CPU 平均只有 25%，服务却明显变慢。
   原因：单线程应用或全局锁导致只有一个核在跑，其余核心闲置。
   正确做法：用 `mpstat -P ALL 1` 看分布；优化为多进程/多线程模型或做 CPU 亲和性规划。

7. **一有性能问题就重启服务**
   现象：重启后短暂恢复，随后问题复现，且现场已丢失。
   原因：重启只是清空了内存与连接，未解决根因。
   正确做法：先留存 `perf`、`strace`、堆栈与监控快照，再决定是否重启。

8. **调内核参数解决应用层问题**
   现象：调大 `tcp_tw_reuse`、`somaxconn` 后改善有限。
   原因：`CLOSE_WAIT` 堆积、连接不释放属于应用缺陷，内核参数无法修复。
   正确做法：内核参数只用于放大承载能力，连接生命周期管理必须回到应用代码。
