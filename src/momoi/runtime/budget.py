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
        day = current.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        month = current.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()
        daily_spent = monthly_spent = 0.0
        if self.accounting is not None:
            with sqlite3.connect(self.config.database) as db:
                rows = db.execute("SELECT model, created_at, cache_read_tokens, uncached_tokens, cache_write_tokens, output_tokens FROM llm_usage WHERE created_at>=? AND created_at<=?", (month, current.timestamp()))
                for model, stamp, hit, miss, write, output in rows:
                    cost = self.accounting.estimate_cost(model, stamp, cache_read=hit, uncached=miss, cache_write=write, output=output)
                    monthly_spent += cost
                    if stamp >= day:
                        daily_spent += cost
        available = self.accounting is not None
        daily_blocked = budget.daily_amount > 0 and daily_spent >= budget.daily_amount
        monthly_blocked = budget.monthly_amount > 0 and monthly_spent >= budget.monthly_amount
        daily = budget.daily_amount > 0
        reason = ("费用估算不可用，请启用账户余额中的费用估算。" if not available
                  else "本月费用已达月预算，调度已暂停。" if monthly_blocked
                  else "今日费用已达日预算，调度已暂停。" if daily_blocked else "")
        return {"enabled": budget.enabled, "period": "daily" if daily else "monthly",
                "amount": budget.daily_amount if daily else budget.monthly_amount,
                "spent": daily_spent if daily else monthly_spent,
                "daily": {"amount": budget.daily_amount, "spent": daily_spent},
                "monthly": {"amount": budget.monthly_amount, "spent": monthly_spent},
                "available": available, "blocked": budget.enabled and (not available or daily_blocked or monthly_blocked),
                "reason": reason}

    def check(self):
        if self.config.budget.enabled and self.status()["blocked"]:
            raise BudgetExceeded("费用预算已暂停运行")
