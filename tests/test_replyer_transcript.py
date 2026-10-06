from momoi.runtime.transcript.replyer import visible_dialogue


def test_projection_keeps_dialogue_and_rejects_unsent_or_workflow_content():
    rows = [{"role": role, "content": text, "delivery_state": state, "created_at": 1}
            for role, text, state in [
                ("user", "测试输入", "delivered"),
                ("assistant", "内部工作", "internal"),
                ("assistant", "失败", "failed"),
                ("assistant", "排队", "queued"),
                ("assistant", "不确定", "uncertain"),
                ("event", "自主活动", "delivered"),
                ("assistant", "实际回复", "delivered"),
            ]]
    messages = visible_dialogue(rows)
    assert len(messages) == 2
    assert "测试输入" in messages[0]["content"]
    assert messages[1] == {"role": "assistant", "content": "实际回复"}


def test_projection_preserves_recent_order_with_bounded_history():
    rows = [{"role": "user", "content": str(n), "created_at": n} for n in range(60)]
    messages = visible_dialogue(rows)
    assert len(messages) == 48
    assert ">\n12\n" in messages[0]["content"]
    assert ">\n59\n" in messages[-1]["content"]
