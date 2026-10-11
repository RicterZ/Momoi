from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo
import pytest

from momoi.config.manager import ConfigurationManager
from momoi.config.workspace import bootstrap
from momoi.config.models import BudgetConfig, ConfigError
from momoi.runtime.budget import BudgetGuard, BudgetExceeded
from momoi.storage import Store


class Pricing:
    def estimate_cost(self, model, stamp, **usage):
        return usage['output'] / 100


def test_calendar_budget_and_missing_pricing(tmp_path):
    path = tmp_path / 'config.json'
    bootstrap(path)
    manager = ConfigurationManager(path)
    config = replace(manager.validate(), timezone='Asia/Shanghai', budget=BudgetConfig(True, 'daily', 1))
    store = Store(config.database, config.workspace)
    try:
        stamp = datetime(2026, 10, 11, 10, tzinfo=ZoneInfo(config.timezone)).timestamp()
        store.record_llm_call(created_at=stamp, model='test', metrics={'output': 100})
    finally:
        store.close()
    guard = BudgetGuard(config, Pricing())
    assert guard.status(stamp)['blocked']
    assert not guard.status(stamp + 86400)['blocked']
    monthly = BudgetGuard(replace(config, budget=BudgetConfig(True, 'monthly', 1)), Pricing())
    assert monthly.status(stamp + 86400)['blocked']
    assert not monthly.status(datetime(2026, 11, 1, tzinfo=ZoneInfo(config.timezone)).timestamp())['blocked']
    with pytest.raises(BudgetExceeded):
        BudgetGuard(config, None).check()


def test_budget_configuration_validation(tmp_path):
    path = tmp_path / 'config.json'
    bootstrap(path)
    manager = ConfigurationManager(path)
    manager.save_runtime({'budget': {'enabled': True, 'period': 'monthly', 'amount': 12}}, manager.revision())
    assert manager.validate().budget == BudgetConfig(True, 'monthly', 12)
    for value in (0, -1, float('nan'), float('inf')):
        with pytest.raises(ConfigError):
            manager.save_runtime({'budget': {'amount': value}}, manager.revision())
