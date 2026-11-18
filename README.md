# MACRO PULSE News

新闻快讯分级去重 + 事件卡片自治（规则/流水线/事件卡/边缘网关）。

- 规则真相源：`docs/分级标准.md`、`docs/去重规则全览.md`；LLM 用精简快照 `prompt_rules.md`。
- 流水线：`tools/llm_news_pipeline.py`（分级去重）、`tools/evolve_events.py`（卡片演进）、`tools/event_incubator.py`（自动建卡归档）。
- 边缘网关：`https://macropulse-api.pages.dev`（/rules 规则、/__proxy 快讯抓取）。
- 公开仓库，Actions 不限分钟。

