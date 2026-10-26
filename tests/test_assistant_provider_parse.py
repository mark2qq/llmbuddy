# -*- coding: utf-8 -*-
"""
模型接入层解析回归（tests/test_assistant_provider_parse.py）。

1.0.9 修复（用户反馈「模型没有返回正文」）针对三处：
    1. 上游忽略 `stream=true` 回整包 JSON → 旧实现静默得到空回复；
    2. 200 响应体里塞错误（`{"code":50508,"message":"System is too busy"}`）→
       同样变成「空正文」，把「上游繁忙」误报成「token 用光了」；
    3. 思考型模型把预算花在思考上 → `finish_reason=length` + content 为空，
       provider 应自动放大 max_tokens 重试一次。
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.assistant import providers as pv  # noqa: E402


class _FakeResp:
    """最小 httpx.Response 替身：只需要 iter_lines()。"""

    def __init__(self, lines):
        self._lines = lines

    def iter_lines(self):
        for ln in self._lines:
            yield ln


def _run(lines, reply=None):
    reply = reply or pv.ChatReply()
    pv.Provider._consume_sse(_FakeResp(lines), reply, None, None)
    return reply


def test_sse_normal_stream():
    reply = _run([
        'data: {"choices":[{"delta":{"reasoning_content":"想一想"}}]}',
        'data: {"choices":[{"delta":{"content":"你好"},"finish_reason":"stop"}]}',
        "data: [DONE]",
    ])
    assert reply.content == "你好"
    assert reply.reasoning == "想一想"
    assert reply.finish_reason == "stop"
    assert reply.error == ""


def test_non_stream_json_body_is_parsed():
    """网关忽略 stream：整包 JSON 也要能拿到正文（旧实现直接空回复）。"""
    body = ('{"id":"x","choices":[{"message":{"role":"assistant",'
            '"content":"整包回复","reasoning_content":"思考"},"finish_reason":"stop"}]}')
    reply = _run([body])
    assert reply.content == "整包回复"
    assert reply.reasoning == "思考"
    assert reply.error == ""


def test_error_body_in_200_is_reported():
    """200 里塞错误体：必须报「上游错误」，而不是「模型没有返回正文」。"""
    reply = _run(['{"code":50508,"message":"System is too busy now."}'])
    assert "上游错误" in reply.error
    assert "too busy" in reply.error
    assert reply.content == ""


def test_unparsable_body_is_reported():
    reply = _run(["<html>502 Bad Gateway</html>"])
    assert "没有按流式协议返回" in reply.error


def test_empty_body_is_reported():
    reply = _run([])
    assert "空响应" in reply.error


def test_stream_with_only_finish_reason_has_no_error():
    """只有 finish_reason=length（思考吃光预算）：不报错，交给 chat 层放大重试。"""
    reply = _run(['data: {"choices":[{"delta":{},"finish_reason":"length"}]}'])
    assert reply.error == ""
    assert reply.finish_reason == "length"
    assert reply.content == ""


def test_status_hint_maps_common_codes():
    assert "限流" in pv._status_hint(429)
    assert "繁忙" in pv._status_hint(503)
    assert "401" in pv._status_hint(401)


class _StubProvider(pv.Provider):
    """把网络层换掉：按顺序返回预置的 ChatReply，验证放大重试。"""

    def __init__(self, sequence):
        super().__init__({"id": "local", "kind": "local", "model": "m"})
        self._seq = list(sequence)
        self.payloads = []

    def _stream_once(self, payload, on_delta, cancel_check, timeout):  # noqa: D401
        self.payloads.append(dict(payload))
        return self._seq.pop(0) if self._seq else pv.ChatReply(content="x")

    def resolve_model(self, preferred=""):        # 跳过本地模型探测
        return "m"


def test_empty_content_escalates_max_tokens():
    first = pv.ChatReply(finish_reason="length")
    second = pv.ChatReply(content="终于有正文了", finish_reason="stop")
    prov = _StubProvider([first, second])
    resets = []
    reply = prov.chat([{"role": "user", "content": "hi"}], max_tokens=2048,
                      on_delta=lambda ch, t: resets.append(ch))
    assert reply.content == "终于有正文了"
    assert [p["max_tokens"] for p in prov.payloads] == [2048, 8192]
    assert "自动放大到 8192" in reply.note
    # 首轮什么都没渲染，不需要发 reset
    assert resets == []


def test_escalation_emits_reset_when_partial_reasoning_streamed():
    """首轮已把思考流到前端 → 重试前必须发 reset，否则重复叠字。"""
    first = pv.ChatReply(reasoning="思考到一半没预算了", finish_reason="length")
    second = pv.ChatReply(content="正文", finish_reason="stop")
    prov = _StubProvider([first, second])
    resets = []
    reply = prov.chat([{"role": "user", "content": "hi"}], max_tokens=2048,
                      on_delta=lambda ch, t: resets.append(ch))
    assert reply.content == "正文"
    assert "reset" in resets


def test_empty_content_does_not_escalate_when_only_reasoning():
    """有思考内容但不截断 → 直接走 reasoning 兜底，不重复消耗额度。"""
    first = pv.ChatReply(reasoning="只有思考", finish_reason="stop")
    prov = _StubProvider([first])
    reply = prov.chat([{"role": "user", "content": "hi"}], max_tokens=2048)
    assert reply.content == "只有思考"
    assert reply.fallback_reasoning is True
    assert len(prov.payloads) == 1


def test_escalation_stops_at_cap():
    first = pv.ChatReply(finish_reason="length")
    second = pv.ChatReply(finish_reason="length")
    prov = _StubProvider([first, second])
    reply = prov.chat([{"role": "user", "content": "hi"}],
                      max_tokens=pv.MAX_TOKENS_CAP)
    assert len(prov.payloads) == 1        # 已在上限，不再重试
    assert reply.content == ""
