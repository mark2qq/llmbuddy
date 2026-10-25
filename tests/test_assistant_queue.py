# -*- coding: utf-8 -*-
"""
引导会话队列核心单测（tests/test_assistant_queue.py，后端套）。

覆盖（对齐架构设计 T01 验收点）：
    - FIFO：连续入队 3 条，snapshot 顺序与 pop_next 顺序一致
    - 上限：第 21 条 enqueue 抛 2012，前 20 条完好
    - 编辑/删除：排队中可改可删；引导条目 update/remove 抛 2016；
      pop_next 后 update/remove 抛 2015；未知 id 抛 2014
    - 排序：move(-1) 首条抛错、末条 +1 抛错，中间条目交换正确
    - clear 只清非引导并返回清除数；set_paused/is_paused；
      drop_session 清空该会话全部状态

该文件不依赖 env 夹具（队列是纯内存模块），但 get_queue() 是进程级单例，
每个用例用独立 session_id 隔离，收尾 drop_session。
"""
import os
import sys
import time as _time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.assistant.queue import (MAX_QUEUE_ITEMS, RECENT_KEEP,  # noqa: E402
                                  AssistantQueue, QueueError, QueueItem,
                                  get_queue, hhmm)


# ----------------------------------------------------------------------------
# 基础：单例 / 条目字典 / 时间格式
# ----------------------------------------------------------------------------
def test_get_queue_is_singleton():
    assert get_queue() is get_queue()
    assert isinstance(get_queue(), AssistantQueue)


def test_item_to_dict_shape():
    it = QueueItem(id="qabc", text="你好", guided=False, created_at=0.0)
    d = it.to_dict()
    assert d["id"] == "qabc"
    assert d["text"] == "你好"
    assert d["guided"] is False
    assert d["created_at"] == 0.0
    assert d["edited_at"] is None
    # time 一律 HH:MM（本机本地时区）
    assert len(d["time"]) == 5 and d["time"][2] == ":"
    it.edited_at = 1.0
    assert it.to_dict()["edited_at"] == 1.0


def test_hhmm_formats_local_time():
    ts = _time.mktime((2026, 8, 8, 9, 5, 0, 0, 0, -1))
    assert hhmm(ts) == "09:05"
    assert hhmm("not-a-number") == "--:--"


# ----------------------------------------------------------------------------
# FIFO
# ----------------------------------------------------------------------------
def test_fifo_snapshot_matches_pop_order():
    q = get_queue()
    sid = "s-fifo"
    q.drop_session(sid)
    try:
        for i in range(3):
            q.enqueue(sid, f"消息{i}")
        assert [it["text"] for it in q.snapshot(sid)] == \
            ["消息0", "消息1", "消息2"]
        assert q.pending_count(sid) == 3
        popped = [q.pop_next(sid).text for _ in range(3)]
        assert popped == ["消息0", "消息1", "消息2"]
        assert q.pop_next(sid) is None
        assert q.pending_count(sid) == 0
    finally:
        q.drop_session(sid)


def test_enqueue_strips_and_rejects_empty():
    q = get_queue()
    sid = "s-empty"
    q.drop_session(sid)
    try:
        item = q.enqueue(sid, "  两边有空格  ")
        assert item.text == "两边有空格"
        assert item.id.startswith("q") and len(item.id) == 11
        try:
            q.enqueue(sid, "   ")
            assert False, "空消息应抛 2002"
        except QueueError as e:
            assert e.code == 2002
    finally:
        q.drop_session(sid)


# ----------------------------------------------------------------------------
# 上限 20
# ----------------------------------------------------------------------------
def test_queue_full_rejects_21st():
    q = get_queue()
    sid = "s-full"
    q.drop_session(sid)
    try:
        for i in range(MAX_QUEUE_ITEMS):
            q.enqueue(sid, f"m{i}")
        assert q.pending_count(sid) == MAX_QUEUE_ITEMS
        try:
            q.enqueue(sid, "第 21 条")
            assert False, "超出上限应抛 2012"
        except QueueError as e:
            assert e.code == 2012
            assert "上限" in e.message
        # 前 20 条完好
        assert q.pending_count(sid) == MAX_QUEUE_ITEMS
        assert q.snapshot(sid)[0]["text"] == "m0"
        assert q.snapshot(sid)[-1]["text"] == f"m{MAX_QUEUE_ITEMS - 1}"
    finally:
        q.drop_session(sid)


# ----------------------------------------------------------------------------
# 编辑 / 删除
# ----------------------------------------------------------------------------
def test_update_and_remove_pending_items():
    q = get_queue()
    sid = "s-edit"
    q.drop_session(sid)
    try:
        a = q.enqueue(sid, "原始文本")
        b = q.enqueue(sid, "第二条")
        updated = q.update(sid, a.id, "改过的文本")
        assert updated.text == "改过的文本"
        assert updated.edited_at is not None
        assert q.snapshot(sid)[0]["text"] == "改过的文本"

        assert q.remove(sid, b.id) == b.id
        assert [it["id"] for it in q.snapshot(sid)] == [a.id]
    finally:
        q.drop_session(sid)


def test_update_remove_unknown_id_raises_2014():
    q = get_queue()
    sid = "s-unknown"
    q.drop_session(sid)
    try:
        q.enqueue(sid, "占位")
        try:
            q.update(sid, "qdoes-not-exist", "x")
            assert False, "未知 id 应抛 2014"
        except QueueError as e:
            assert e.code == 2014
        try:
            q.remove(sid, "qdoes-not-exist")
            assert False, "未知 id 应抛 2014"
        except QueueError as e:
            assert e.code == 2014
    finally:
        q.drop_session(sid)


def test_guided_items_are_readonly_2016():
    q = get_queue()
    sid = "s-guided"
    q.drop_session(sid)
    try:
        g = q.enqueue(sid, "引导消息", guided=True)
        assert q.snapshot(sid)[0]["guided"] is True
        try:
            q.update(sid, g.id, "想改引导消息")
            assert False, "引导消息 update 应抛 2016"
        except QueueError as e:
            assert e.code == 2016
        try:
            q.remove(sid, g.id)
            assert False, "引导消息 remove 应抛 2016"
        except QueueError as e:
            assert e.code == 2016
        try:
            q.move(sid, g.id, -1)
            assert False, "引导消息 move 应抛 2016"
        except QueueError as e:
            assert e.code == 2016
        # 引导消息仍在队列里
        assert q.pending_count(sid) == 1
    finally:
        q.drop_session(sid)


def test_dispatched_items_raise_2015():
    q = get_queue()
    sid = "s-dispatch"
    q.drop_session(sid)
    try:
        a = q.enqueue(sid, "会被出队")
        q.pop_next(sid)
        try:
            q.update(sid, a.id, "迟到的编辑")
            assert False, "已出队 update 应抛 2015"
        except QueueError as e:
            assert e.code == 2015
        try:
            q.remove(sid, a.id)
            assert False, "已出队 remove 应抛 2015"
        except QueueError as e:
            assert e.code == 2015
    finally:
        q.drop_session(sid)


def test_recent_memory_evicts_oldest_beyond_keep(monkeypatch):
    """_recent 只记 RECENT_KEEP 条：更早出队的条目退化成 2014（不存在）。"""
    from core.assistant import queue as queue_mod
    monkeypatch.setattr(queue_mod, "RECENT_KEEP", 3)
    q = get_queue()
    sid = "s-recent"
    q.drop_session(sid)
    try:
        ids = [q.enqueue(sid, f"m{i}").id for i in range(4)]
        for _ in range(4):
            q.pop_next(sid)
        # 最早出队的那条已被淘汰 -> 2014
        try:
            q.update(sid, ids[0], "x")
            assert False, "被淘汰的条目应报 2014"
        except QueueError as e:
            assert e.code == 2014
        # 最近出队的仍在记忆里 -> 2015
        try:
            q.update(sid, ids[-1], "x")
            assert False, "记忆中的条目应报 2015"
        except QueueError as e:
            assert e.code == 2015
    finally:
        q.drop_session(sid)


# ----------------------------------------------------------------------------
# 排序
# ----------------------------------------------------------------------------
def test_move_swaps_neighbors_and_edges_raise():
    q = get_queue()
    sid = "s-move"
    q.drop_session(sid)
    try:
        a = q.enqueue(sid, "一")
        b = q.enqueue(sid, "二")
        c = q.enqueue(sid, "三")
        # 首条上移 / 末条下移：越界抛 2002
        try:
            q.move(sid, a.id, -1)
            assert False, "首条上移应抛 2002"
        except QueueError as e:
            assert e.code == 2002
        try:
            q.move(sid, c.id, 1)
            assert False, "末条下移应抛 2002"
        except QueueError as e:
            assert e.code == 2002
        # 非法 delta
        try:
            q.move(sid, b.id, 2)
            assert False, "非法 delta 应抛 2002"
        except QueueError as e:
            assert e.code == 2002
        # 中间条目下移一位：一、三、二
        items = q.move(sid, b.id, 1)
        assert [it["text"] for it in items] == ["一", "三", "二"]
        # 再上移回去
        items = q.move(sid, b.id, -1)
        assert [it["text"] for it in items] == ["一", "二", "三"]
        # 移动后快照同步
        assert [it["text"] for it in q.snapshot(sid)] == ["一", "二", "三"]
    finally:
        q.drop_session(sid)


# ----------------------------------------------------------------------------
# 全部清除（不清引导）/ 暂停 / drop_session
# ----------------------------------------------------------------------------
def test_clear_keeps_guided_and_returns_count():
    q = get_queue()
    sid = "s-clear"
    q.drop_session(sid)
    try:
        q.enqueue(sid, "普通一")
        q.enqueue(sid, "引导一", guided=True)
        q.enqueue(sid, "普通二")
        q.enqueue(sid, "引导二", guided=True)
        assert q.clear(sid) == 2
        left = q.snapshot(sid)
        assert [it["text"] for it in left] == ["引导一", "引导二"]
        assert all(it["guided"] for it in left)
        # 再清一次没有可清的
        assert q.clear(sid) == 0
    finally:
        q.drop_session(sid)


def test_pause_flag_roundtrip():
    q = get_queue()
    sid = "s-pause"
    q.drop_session(sid)
    try:
        assert q.is_paused(sid) is False
        q.set_paused(sid, True)
        assert q.is_paused(sid) is True
        q.set_paused(sid, False)
        assert q.is_paused(sid) is False
        # 暂停不影响 FIFO 出队本身（由 gen() 决定是否消费）
        q.enqueue(sid, "x")
        q.set_paused(sid, True)
        assert q.pop_next(sid) is not None
    finally:
        q.drop_session(sid)


def test_drop_session_wipes_all_state():
    q = get_queue()
    sid = "s-drop"
    q.drop_session(sid)
    try:
        a = q.enqueue(sid, "一")
        q.enqueue(sid, "引导", guided=True)
        q.set_paused(sid, True)
        q.pop_next(sid)
        assert a.id in q._recent.get(sid, {})
        q.drop_session(sid)
        assert q.pending_count(sid) == 0
        assert q.is_paused(sid) is False
        assert sid not in q._recent
        # 删除后该会话的旧 id 视为不存在
        try:
            q.update(sid, a.id, "x")
            assert False, "删除会话后应报 2014"
        except QueueError as e:
            assert e.code == 2014
    finally:
        q.drop_session(sid)
