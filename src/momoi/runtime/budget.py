"""Calendar budgets use the same local LLM pricing as the dashboard."""
import sqlite3
import time
from datetime import datetime
from zoneinfo import ZoneInfo


class BudgetExceeded(RuntimeError):
    pass


class BudgetGuard:
    def __init__(self, config, accounting):
        self.config = config
        self.accounting = accounting

    def status(self, now=None):
        budget = self.config.budget
        current = datetime.fromtimestamp(time.time() if now is None else now, ZoneInfo(self.config.timezone))
        start = current.replace(hour=0, minute=0, second=0, microsecond=0)
        if budget.period == "monthly":
            start = start.replace(day=1)
        spent = 0.0
        if self.accounting is not None:
            with sqlite3.connect(self.config.database) as db:
                rows = db.execute("SELECT model, created_at, cache_read_tokens, uncached_tokens, cache_write_tokens, output_tokens FROM llm_usage WHERE created_at>=? AND created_at<=?", (start.timestamp(), current.timestamp()))
                for model, stamp, hit, miss, write, output in rows:
                    spent += self.accounting.estimate_cost(model, stamp, cache_read=hit, uncached=miss, cache_write=write, output=output)
        available = self.accounting is not None
        return {"enabled": budget.enabled, "period": budget.period, "amount": budget.amount,
                "spent": spent, "available": available,
                "blocked": budget.enabled and (not available or spent >= budget.amount),
                "reason": "达到费用预算，调度已暂停。" if available else "费用估算不可用，请启用账户余额中的费用估算。"}

    def check(self):
        if self.config.budget.enabled and self.status()["blocked"]:
            raise BudgetExceeded("费用预算已暂停运行")
