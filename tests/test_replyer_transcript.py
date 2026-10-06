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
    assert messages[0]["role"] == "user"
    assert messages[0]["content"].startswith('<message time="')
    assert "role=" not in messages[0]["content"]
    assert messages[1] == {"role": "assistant", "content": "实际回复"}


def test_projection_preserves_store_selected_history():
    rows = [{"role": "user", "content": str(n), "created_at": n} for n in range(60)]
    messages = visible_dialogue(rows)
    assert len(messages) == 60
    assert ">\n0\n" in messages[0]["content"]
    assert ">\n59\n" in messages[-1]["content"]


from momoi.storage.conversation.replyer import select_history_rows

def history(count):
    return [{"id": n, "content": "内容" * 1000, "role": "user"} for n in range(count)]

def test_history_keeps_prefix_until_rotation_and_recovers_edits():
    rows = history(60)
    selected = select_history_rows(rows, [])
    assert selected == rows[-12:]
    for count in range(61, 96):
        updated = select_history_rows(history(count), selected)
        assert updated[:len(selected)] == selected
        selected = updated
    assert len(selected) == 47
    selected = select_history_rows(history(96), selected)
    assert selected == history(96)[-12:]
    updated = select_history_rows(history(97), selected)
    assert updated[:12] == selected
    edited = history(97)
    edited[-5]["content"] = "更正"
    assert select_history_rows(edited, updated) == edited[-12:]
