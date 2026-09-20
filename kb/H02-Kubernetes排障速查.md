---
id: H02
title: Kubernetes 排障速查
volume: H-云原生
tags: [kubernetes, 排障, pod, crashloopbackoff, oomkilled, pending, notready, service]
aliases: [k8s排障, kubernetes排障, pod一直重启, pod反复重启, 容器一直重启, Pod启动失败, Pod起不来, 容器起不来, CrashLoopBackOff, ImagePullBackOff, ErrImagePull, CreateContainerConfigError, Pod Pending, pod一直Pending, pod调度不上去, 调度失败, OOMKilled, 容器被OOM杀死, 内存超限被杀, Evicted, 节点NotReady, node NotReady, 节点失联, 节点挂了, Service访问不通, service不通, 服务访问不了, ClusterIP访问失败, DNS解析失败, CoreDNS异常, no space left on device, DiskPressure, 0/3 nodes are available, pod卡在Terminating, 端口不通]
level: L2
prerequisites: []
related: [D02, J01]
status: 生效
updated: 2026-01-01
---

# Kubernetes 排障速查

> 本文覆盖 Kubernetes 集群中最常见的五类故障——Pod 反复重启、镜像拉取失败、资源不足被终止、Service/DNS 不通、节点 NotReady，给出一条可复制的命令链与根因对照表，适合作为值班速查手册使用。

---

## 一、常见症状

| 现象 | 状态字段 | 首要排查方向 |
|---|---|---|
| 容器反复重启 | `CrashLoopBackOff` | 应用启动报错、配置缺失、探针阈值过严 |
| 镜像拉不下来 | `ImagePullBackOff` / `ErrImagePull` | 镜像名或标签错、私有仓库凭据缺失 |
| 容器被内核杀死 | `OOMKilled`（退出码 137） | `limits.memory` 偏小、应用未感知 cgroup 限制 |
| Pod 一直不调度 | `Pending` | 资源不足、亲和性/污点、PVC 未绑定 |
| 容器创建前失败 | `CreateContainerConfigError` | 引用的 Secret / ConfigMap 不存在或 key 名错 |
| 节点异常 | `NotReady` | kubelet 挂起、磁盘/内存压力、网络插件异常 |
| Service 访问不通 | Pod 全部 `Running` | Endpoints 为空、selector 不匹配、NetworkPolicy |
| Pod 删不掉 | `Terminating` | finalizer 未清理、kubelet 与 API Server 失联 |

## 二、排查思路

按「从外到内、先状态后日志」的顺序推进，避免一上来就翻应用日志：

1. **看状态**：`kubectl get pod -o wide`，拿到 STATUS、RESTARTS、NODE、IP 四要素。
2. **看事件**：`kubectl describe pod` 末尾的 `Events` 是投入产出比最高的信息源，调度失败、拉镜像失败、探针失败都会在此出现。
3. **看日志**：`kubectl logs`，并区分「当前实例」与「上一次崩溃实例」（`--previous`）。
4. **看配置**：resources、探针、env、volume、serviceAccount 是否与预期一致。
5. **看节点**：下沉到 Node 层，检查 kubelet、容器运行时、CNI 与内核日志。

关键判断：先区分这是**「起不来」**还是**「跑着跑着挂了」**——前者看 `describe`，后者看 `--previous` 与监控曲线。

## 三、常用的定位命令

```bash
# 1. 全局扫描：列出所有非 Running 的 Pod（最快定位故障面）
kubectl get pods -A --field-selector=status.phase!=Running

# 2. 按容器重启次数降序排，快速找到反复重启的对象
kubectl get pods -A --sort-by=.status.containerStatuses[0].restartCount

# 3. 只看 Events 段，跳过冗长的 spec 输出
kubectl describe pod <pod-name> -n <ns> | sed -n '/Events/,$p'

# 4. 查看上一次崩溃容器的日志（CrashLoopBackOff 必用）
kubectl logs <pod-name> -n <ns> --previous

# 5. 提取退出码与终止原因：137=SIGKILL(常见 OOM)，143=SIGTERM，1=应用错误
kubectl get pod <pod-name> -n <ns> -o jsonpath='{range .status.containerStatuses[*]}{.name}{"\t"}{.lastState.terminated.reason}{"\t"}{.lastState.terminated.exitCode}{"\n"}{end}'

# 6. 查看实际生效的资源请求与限制
kubectl get pod <pod-name> -n <ns> -o jsonpath='{.spec.containers[*].resources}'; echo

# 7. 查看节点状态与压力条件（DiskPressure / MemoryPressure / Ready）
kubectl describe node <node-name> | sed -n '/Conditions/,/Addresses/p'

# 8. 借用 debug 容器进入目标 Pod 的网络命名空间排查（需集群开启 ephemeral container）
kubectl debug -it <pod-name> -n <ns> --image=nicolaka/netshoot --target=<container-name>

# 9. Service 不通第一查：Endpoints 是否为空
kubectl get endpoints <svc-name> -n <ns>

# 10. 核对 Service selector 与 Pod 实际标签是否匹配
kubectl get svc <svc-name> -n <ns> -o jsonpath='{.spec.selector}'; echo
kubectl get pods -n <ns> --show-labels

# 11. 集群内 DNS 自检
kubectl run dnstest --rm -it --image=busybox:1.36 --restart=Never -- \
  nslookup kubernetes.default.svc.cluster.local

# 12. 查看 PVC 绑定与 StorageClass 状态
kubectl get pvc -A

# 13. 滚动发布是否卡住
kubectl rollout status deploy/<name> -n <ns>

# 14. 节点侧查看 kubelet 日志（在目标节点上执行）
journalctl -u kubelet -n 200 --no-pager
```

> 说明：`<pod-name>`、`<ns>`、`<container-name>` 为占位符，执行时按实际对象替换。命令语法在 Kubernetes 1.20+ 上通用；`--field-selector` 支持的字段随版本略有差异，以当前集群 API 文档为准。

## 四、典型根因与处理

| 状态 | 高频根因 | 处理动作 |
|---|---|---|
| `CrashLoopBackOff` | 启动参数错、依赖服务未就绪、探针阈值过严 | 用 `--previous` 看真实报错；调大 `initialDelaySeconds`；为慢启动应用加 startupProbe |
| `ImagePullBackOff` | 镜像 tag 不存在、仓库网络不可达、`imagePullSecrets` 缺失 | 核对 tag/digest；在节点上手工验证拉取；补齐 Pull Secret |
| `OOMKilled` | `limits.memory` 偏小、语言运行时未感知 cgroup 限制 | 调大 limit；JVM 设 `-XX:MaxRAMPercentage`，Go 设 `GOMEMLIMIT`，Node.js 设 `--max-old-space-size` |
| `Pending` | 节点资源不足、nodeSelector/affinity 无匹配、污点未容忍、PVC 未绑定 | 看 Events 中的 `0/N nodes are available` 明细；调整 requests；检查 StorageClass 与污点容忍 |
| `CreateContainerConfigError` | 引用的 Secret/ConfigMap 不存在或 key 拼错 | `kubectl get cm/secret <name> -o yaml` 逐项核对 key |
| `Evicted` | 节点 DiskPressure / MemoryPressure | 清理节点磁盘与镜像；显式设置 `ephemeral-storage` requests |
| `NotReady` | kubelet 异常、运行时无响应、CNI 异常、证书过期 | `systemctl status kubelet`；检查 CNI 组件 Pod；核对证书有效期 |
| Service 不通 | selector 不匹配、`targetPort` 写错、NetworkPolicy 拦截 | 核对 Endpoints、端口映射与网络策略 |
| `Terminating` 卡住 | 对象带 finalizer、节点失联 | `kubectl get pod -o yaml` 看 finalizers；必要时 `kubectl delete pod --force --grace-period=0`（先确认已无副作用） |

## 五、容易踩的坑

1. **只看 `kubectl logs`，不看 `--previous`**
   现象：Pod 卡在 `CrashLoopBackOff`，`logs` 却只提示容器尚未启动。
   原因：崩溃发生在日志落盘之前，当前实例的日志为空，真实输出在被替换的旧实例里。
   正确做法：加上 `--previous`；若旧实例已被回收，到节点侧查容器运行时日志或配置日志落盘目录。

2. **把 `restartPolicy: Always` 当成高可用方案**
   现象：进程退出后立刻重启，问题长期被掩盖，只在重启次数累积后才被发现。
   原因：kubelet 只负责重启容器，不负责修复应用缺陷。
   正确做法：把 `restartCount` 纳入监控与告警，结合启动日志定位真实原因。

3. **`requests` 与 `limits` 只设其中一个**
   现象：Pod 被调度到内存紧张的节点，随后频繁 OOM 或被驱逐。
   原因：调度器只看 `requests`，`limits` 只约束运行时上限，两者语义不同。
   正确做法：两者都设，`requests` 供调度参考、`limits` 做安全兜底，且不建议相差过大。

4. **Service 不通先查应用日志**
   现象：翻了大量应用日志，最后发现是 Endpoints 为空。
   原因：Service 是四层转发抽象，断点常出现在 selector、端口或网络策略，而非应用本身。
   正确做法：第一步执行 `kubectl get endpoints`，为空则回到 selector 与 `targetPort` 核对。

5. **改了 Deployment 但 Pod 没有更新**
   现象：`kubectl apply` 成功，Pod 仍运行旧镜像。
   原因：镜像 tag 未变而 `imagePullPolicy: IfNotPresent`，节点上沿用旧镜像；或修改了不可变字段。
   正确做法：使用不可变 tag 或 image digest；用 `kubectl rollout status` 与 `rollout history` 确认是否真正触发滚动。

6. **节点 NotReady 就立刻重启 kubelet**
   现象：重启后仍 `NotReady`，且原始现场已丢失。
   原因：根因可能是证书过期、磁盘写满或 CNI 异常，重启 kubelet 无法解决。
   正确做法：先看 Node `Conditions` 与 kubelet 日志定位根因，再决定处置动作。

7. **忘记指定 namespace**
   现象：命令返回空列表，误判为资源不存在。
   原因：kubectl 默认只操作 `default` 命名空间，实际工作负载多在业务命名空间。
   正确做法：统一显式加 `-n <ns>`，跨命名空间排查用 `-A`。

8. **把容器退出码 137 一律当成应用缺陷**
   现象：反复排查应用代码但问题依旧。
   原因：137 = 128 + 9，表示进程收到 SIGKILL，常见于超出内存上限被 cgroup 终止，也可能是节点驱逐。
   正确做法：先确认 `lastState.terminated.reason` 是否为 `OOMKilled`，再决定是调资源还是改代码。
