# -*- coding: utf-8 -*-
"""
智能助理内核（core/assistant）。

职责划分（对齐 OpenClaw 的分层，1.0.8 内置实现）：
    store.py        配置/目录/审计持久化
    providers.py    模型接入（本地引擎 / 硅基流动 / 任意 OpenAI 兼容）
    tools.py        工具注册表（本机文件、命令、网页、技能、MCP）
    approval.py     高危动作审批策略 + 待办 + 审计留痕
    mcp.py          MCP 客户端（stdio / http）
    skills.py       SKILL.md 技能包（导入 / 导出 / 脚手架）
    orchestrator.py LangGraph 复杂子任务编排（缺失时降级为顺序执行）
    agent.py        会话级 Agent 循环 + SSE 事件流
    queue.py        引导会话队列（排队插话 / 引导发送，1.0.9 增量）

对外只暴露 get_assistant()，由 routers/assistant.py 挂载；
队列单例 get_queue() 由路由层的队列端点与 SSE 消费循环使用。
"""
from core.assistant.agent import Assistant, get_assistant
from core.assistant.queue import QueueError, QueueItem, get_queue

__all__ = ["Assistant", "get_assistant", "QueueItem", "QueueError",
           "get_queue"]
