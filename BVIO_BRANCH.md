# litellm-Bvio 分支维护说明

## 这个分支是什么

`litellm-Bvio` = **官方 `upstream/main` + 两个上游没有的补丁**。后续构建镜像用这个分支。

当前基线 `0e9cd9893e`（`upstream/main`，2026-08-11，version 1.98.0）。

设计原则：凡是官方已经修的，一律用官方版本，本分支不留自研实现。只保留官方确实没有、而我们排障必需的东西。目前只有两项，见下文。

## 为什么不能直接打官方 release 镜像

我们依赖的三个 Bedrock 修复只在 `main` 上，还没进任何正式发布：

| 官方提交 | 内容 | v1.96.2（最新正式） | v1.97.0-rc.1 | main |
| --- | --- | --- | --- | --- |
| `1018d18e6b` | mixed chunk 按 payload 种类拆分 (#35289) | 已含 | 已含 | 已含 |
| `0f41365c34` | ARN 的 `output_config` effort 转发 | 未含 | 未含 | 已含 |
| `929ee52b87` | adaptive thinking 过 `/v1/messages` 桥 | 未含 | 未含 | 已含 |
| `363d56f917` | per-deployment `keepalive_seconds` (#34423) | 未含 | 未含 | 已含 |

打 `v1.96.2` 或 `v1.97.0-rc.1`，Opus 5 的 effort 参数会被丢掉 —— 也就是当初 `cd932ae2a8` 要解决的问题会复发。

**等 v1.97.0 / v1.98.0 正式发布后**，重新核对这三个提交是否进入 tag。若都进了，本分支可以 rebase 到该 tag，不必再跟 `main` 这条滚动线。

## 已被官方取代、本分支不再携带的自研补丁

这些提交仍保留在 `litellm_sse_keepalive` 和 `litellm-Bvio-pre-upstream` 分支上以备查，但**不再进入构建**。

| 原自研 | 官方对应 | 为什么用官方的 |
| --- | --- | --- |
| `cd932ae2a8` support Opus 5 application profiles | `0f41365c34` | 官方方案更简单：ARN 本来就藏了底层模型，本地判定不了，于是原样转发 `output_config` 交给 Bedrock 校验。我们那套 `_output_config_model` / `capability_model` 传递管道不再需要 |
| `2614be0301` emit SSE keepalives | `363d56f917` | 官方支持 per-deployment 配置，粒度更细。注意配置项变了，见下节 |
| `litellm-fork` 工作区里未提交的 adaptive 门控（3 文件 62 行） | `0f41365c34` | 同上，官方方案不需要这层门控。这批改动**可以直接丢弃** |
| 本次曾自研的 `_CombinedChunkSplitter` reasoning/content 拆分 | `1018d18e6b` | 官方实现更彻底：按三类 payload 拆（reasoning / text / tool_calls），从头重建 `Delta` 而非 deepcopy 清字段，并额外处理了四个边缘情况 —— tool_calls 被拆成两个同 id 的块、multi-choice 丢次要 choices、tool 参数续传被拆断、无 signature 的 thinking 被收集两次 |

官方从未采用我们那套机制：在 `upstream/main` 里 grep `_output_config_model`、`capability_model`、`_translate_legacy_thinking_for_adaptive_model` 全部为 0 处。

## 部署时必须改的配置

keepalive 的配置位置从我们的全局开关换成了官方的按 deployment 配置。**不改配置的话 keepalive 会静默失效。**

```
旧（bvio 2614be0301）：litellm.sse_keepalive_interval_seconds   全局
新（官方 363d56f917）：litellm_params.keepalive_seconds         每个 deployment，或请求体
```

官方实现在 `keepalive_seconds > 0` 时才启用，每 N 秒发 `: ping\n\n` SSE 注释帧。

## 本分支保留的两个补丁

上游没有，也没有相关 PR。

### 1. `content_filter` 映射为 `refusal`

`litellm/llms/anthropic/experimental_pass_through/adapters/transformation.py`
`_translate_openai_finish_reason_to_anthropic` 增加 `content_filter → refusal` 分支。

`litellm/types/llms/anthropic.py`

```python
AnthropicFinishReason = Literal[
    "end_turn", "max_tokens", "stop_sequence", "tool_use", "refusal"
]
```

官方仍是三分支加兜底 `end_turn`，类型里也没有 `refusal`。

为什么必须改：`end_turn` 语义是"模型自然说完"，`content_filter` 是被外部拦断。内容为空时两者在客户端看来完全一样，于是

- Claude Code 判断不出该不该重试，注入 `[no visible output]` 再发一次，第二次注定同样被拦，白烧一次调用
- violoop-device 的 `core/ai/providers/anthropic.js` 会走到 `recordSuccess()` 并返回 `success: true, content: ''`，把一次拒绝记成成功样本，污染 provider 健康统计

空内容加 `end_turn` 至少对应三种情况，压成一个值后无法区分：被内容过滤（该提示被拒、不重试）、思考吃满 `max_tokens` 正文未开始（该提高上限）、模型确实无话可说（正常收尾）。

### 2. 零可见内容时打一条 warning

`litellm/llms/anthropic/experimental_pass_through/adapters/streaming_iterator.py`

- 实例标志 `emitted_visible_delta` / `warned_no_visible_output`
- `_visible_delta_gate()`：包装官方的 `_delta_has_content()`，返回值语义不变，额外记录是否曾发出 `text_delta` / `input_json_delta`。四个原调用点（同步两处、异步两处）改调它。`_delta_has_content` 保持 staticmethod 不动，官方测试不受影响
- `_warn_if_no_visible_output()`：在 `_augment_message_delta_usage()` 开头调用。该方法是所有最终 `message_delta` 的共同出口（覆盖同步与异步全部路径），单点接入即可，靠 `warned_no_visible_output` 保证一条流只打一次

```
Anthropic Adapter - stream produced no client-visible content block;
the client will render an empty reply
(model=..., stop_reason=..., output_tokens=..., last_block_type=...)
```

为什么必须有：Anthropic 客户端只渲染 `text` 和 `tool_use` 块。Bedrock Converse 返回的 reasoning 只有 signature、没有明文（实测 6/6 请求 `thinking_delta` 恒为 0），所以一个正文被拦或正文未开始的回合，到客户端就是一个空 `thinking` 块加一个非零 `output_tokens`，两侧都不报错。而 `output_tokens` 本身包含 reasoning tokens，单看它无法区分成因 —— 定位这个问题时正是因为缺这条日志，只能靠 `output_tokens` 反推，中途推错过一次。

## 验证结果

```
tests/test_litellm/llms/anthropic/experimental_pass_through/adapters/    192 passed
  其中 test_empty_turn_diagnostics.py                                     9 passed
```

补丁的测试全部集中在 `test_empty_turn_diagnostics.py` 这一个新文件里，官方测试文件一行未改 —— 这样 rebase 到官方新版本时冲突面只有三个源文件的小改动。

| 用例 | 覆盖 |
| --- | --- |
| `test_finish_reason_mapping`（5 例参数化） | 四种取值映射 + 未知值兜底 `end_turn` |
| `test_no_visible_output_warns_with_upstream_context` | 只有 thinking 块的流恰好一条 warning，且带上 model 与 output_tokens |
| `test_refusal_stream_warns_and_reports_refusal` | 被拦的流同时产出 `refusal` 和 warning |
| `test_visible_output_does_not_warn` | 正常有文本的流不打 warning |
| `test_warning_fires_once_per_stream` | 多条 reasoning chunk 不会让 warning 重复 |

## 线上排查顺序

出现空白回复时：

1. 网关日志搜 `no client-visible content block`，读其中的 `stop_reason` 与 `output_tokens`
2. `stop_reason=refusal` → 内容被拦，属预期行为，让调用方换措辞，不要重试
3. `stop_reason=max_tokens` 且 `output_tokens` 接近上限 → 思考吃满预算、正文未开始，提高 max_tokens
4. `stop_reason=end_turn` 且 `output_tokens > 0` → 还有未知的内容丢失路径，抓该请求的 Bedrock Converse 原始事件流继续查
5. 完全没有这条 warning 但客户端仍空白 → 问题不在本适配器，查客户端渲染或连接中断

## rebase 维护指南

补丁面很小，跟随上游的成本已经压到最低：

```
litellm/llms/anthropic/experimental_pass_through/adapters/transformation.py       +8
litellm/llms/anthropic/experimental_pass_through/adapters/streaming_iterator.py  +60/-5
litellm/types/llms/anthropic.py                                                  +4/-2
tests/.../test_empty_turn_diagnostics.py                                         新文件
```

跟进上游时：

1. `git fetch upstream main`（或 fetch 目标 tag）
2. `git rebase upstream/main`（或该 tag）
3. 只需关注 `streaming_iterator.py` —— 若官方重构了 `_delta_has_content` 或 `_augment_message_delta_usage`，`_visible_delta_gate` 的四个调用点和 warning 的接入点需要重新对齐
4. 跑 `pytest tests/test_litellm/llms/anthropic/experimental_pass_through/adapters/`

如果哪天官方自己实现了这两项（可关注 `AnthropicFinishReason` 是否加入 `refusal`），本分支即可退役，直接打官方镜像。

## 当前状态

- `litellm-Bvio`：基线 `upstream/main` + 两个补丁，已提交，未推送
- `litellm-Bvio-pre-upstream`：重建前的旧分支，保留自研 Opus 5 / keepalive 补丁以备查
- `litellm_sse_keepalive` / `litellm_opus_5_latest_staging`：原自研分支，未动
- `litellm-fork` 工作区里那 3 个文件的未提交改动可以丢弃（已被官方 `0f41365c34` 取代），但它们没有任何 git 备份，确认后再清
- 与本次无关的独立问题：客户端记录里另有 56 次 `Connection closed mid-response` 和 22 次 504，属 CloudFront 到源站那一跳，本次未处理
