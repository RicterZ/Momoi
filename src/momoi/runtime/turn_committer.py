from typing import Any


class TurnCommitter:
    def _commit_owner(self, *args: Any, **kwargs: Any) -> str:
        return self.store.commit_turn(*args, **kwargs)

    def _commit_autonomous(self, *args: Any, **kwargs: Any) -> str:
        return self.store.commit_autonomous_turn(*args, **kwargs)

    def _commit_reply_wait_state(self, *args: Any, **kwargs: Any) -> int:
        return self.store.commit_reply_wait(*args, **kwargs)

    def _commit_heartbeat_state(self, *args: Any, **kwargs: Any) -> int:
        return self.store.commit_heartbeat(*args, **kwargs)

    def _commit_reflection_state(self, *args: Any, **kwargs: Any) -> None:
        self.store.commit_reflection(*args, **kwargs)
