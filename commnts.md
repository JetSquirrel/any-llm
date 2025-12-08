# Gateway 方案分析与建议

> 基于源代码分析和 Jet & Jerry 讨论记录

## 建议总结

根据源代码分析和讨论记录，我认为**两种方案各有优劣，建议同时提供两种方案供客户选择**，但需要明确各自的适用场景和风险。

---

## 当前 Gateway 架构分析

```
应用 → any-llm-gateway → AI Provider
          ↓
    GuardrailsMiddleware (拦截 /v1/chat/completions)
          ↓
    Plugin扫描 (httpx调用外部服务如qwen)
          ↓
    allowed=true → 放行
    allowed=false → 返回400阻断
```

**核心代码逻辑**（`middleware/guardrails.py`）：

- `fail_open: bool = True` - **默认降级放行**，plugin失败不阻断请求
- 只拦截 `/v1/chat/completions` 路径
- 通过配置文件注入 plugins（外部guardrail服务）

---

## 方案对比

| 维度 | 方案A：Gateway代理模式 | 方案B：Sidecar/SDK模式 |
|------|------------------------|------------------------|
| **架构** | `应用 → Gateway → AI` | `应用 → AI`（应用主动调Gateway做检测） |
| **客户改动** | 仅改endpoint URL | 需改后端代码集成SDK |
| **单点故障** | Gateway挂了会影响请求 | Gateway挂了只影响guardrail，不影响主流程 |
| **故障降级** | 代码已支持 `fail_open=true` | 客户自行实现降级 |
| **流式响应处理** | 当前只做**请求前**扫描，**响应后**扫描较难实现 | 客户可自行处理response扫描 |
| **责任归属** | 甩锅风险高 | 责任清晰 |
| **部署复杂度** | 简单（K8s一个deployment） | 需SDK集成+运维 |

---

## 我的建议

### 1. 短期：保留Gateway代理模式，但增强健壮性

当前代码已有 `fail_open` 机制，但可以增强：

```python
# 现有配置 (config.py)
class GuardrailsConfig(BaseModel):
    enabled: bool = False
    fail_open: bool = True  # ← 已支持降级
    max_body_size: int = 10485760  # 10MB
    plugins: list[GuardrailPlugin] = Field(default_factory=list)
```

**建议补充**：

- [ ] 添加 **健康检查端点暴露guardrail状态**（当前 `health.py` 只检查DB）
- [ ] 添加 **超时熔断机制**（当前只有 `timeout: float = 5.0`）
- [ ] 添加 **metrics/告警**，让客户能感知Gateway状态

### 2. 中期：提供SDK模式作为可选方案

为jerry提议的方案提供支持，新增独立的 `/v1/scan` 接口：

```python
# 新增接口（不走middleware，直接调用）
@router.post("/v1/scan/input")
async def scan_input(request: ScanRequest) -> ScanResult:
    """扫描用户输入的prompt"""
    ...

@router.post("/v1/scan/output")  
async def scan_output(request: ScanRequest) -> ScanResult:
    """扫描AI返回的response"""
    ...
```

**客户使用流程**：

```
应用发prompt → 调/v1/scan/input → safe → 直接调AI
                                → unsafe → 拒绝

应用收response → 调/v1/scan/output → safe → 显示
                                   → unsafe → 撤回/屏蔽
```

### 3. 给客户的选择指南

| 场景 | 推荐方案 |
|------|---------|
| 快速POC/改动成本敏感 | Gateway代理模式 |
| 生产环境/高可用要求 | Sidecar/SDK模式 |
| 需要response扫描 | Sidecar/SDK模式 |
| K8s环境有成熟灰度能力 | Gateway代理模式（配合灰度降级） |

---

## 回应Jerry的担忧

Jerry的核心担忧是对的：

> "挂在中间，维护起来不只是技术上，而是客户甩锅的时候都要理半天"

**但现有代码已有缓解措施**：

1. `fail_open=true` - plugin失败默认放行，不阻断业务
2. `timeout=5.0` - 超时控制
3. 日志完整 - 每次扫描都有log（见 `guardrails.py` 中的 logger）

**需要补充**：

- [ ] **SLA文档** - 明确gateway的可用性承诺和降级策略
- [ ] **监控告警** - 让客户能主动感知gateway状态
- [ ] **灰度方案** - K8s场景下检测到gateway不健康自动切换

---

## 结论

**两个方案都做**，让客户根据自身情况选择：

- **Gateway模式** = 简单快速，但承担更多责任
- **SDK模式** = 侵入代码，但责任边界清晰

> ⚠️ 如果只能选一个，考虑到Jerry是后续维护者，**建议优先推SDK模式**，减少运维风险和甩锅可能。

---

## 附录：关键源代码位置

| 文件 | 说明 |
|------|------|
| `src/any_llm/gateway/server.py` | FastAPI应用创建，加载GuardrailsMiddleware |
| `src/any_llm/gateway/middleware/guardrails.py` | Guardrail中间件核心逻辑 |
| `src/any_llm/gateway/config.py` | 配置定义（GuardrailsConfig, GuardrailPlugin） |
| `src/any_llm/gateway/routes/chat.py` | Chat completions端点实现 |
| `demos/guardrail/app.py` | Guardrail插件示例（prompt injection检测） |