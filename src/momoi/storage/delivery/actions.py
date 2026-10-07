"""Internal expression markers consumed by the delivery layer."""
QQ_POKE_MARKER = "qq://poke"
QQ_POKE_POLICY = """<qq_expression>
QQ 私聊支持戳一戳。文字模式需要发送这个动作时，将 qq://poke 单独作为一个气泡，用空行与其他气泡分隔；发送层会执行动作，不会发送标记文字。可以只戳一戳，也可以配合文字或表情，按本次意图和语境选择。语音模式不输出动作标记。
</qq_expression>"""
