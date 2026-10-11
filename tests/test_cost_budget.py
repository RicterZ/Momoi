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
    config = replace(manager.validate(), timezone='Asia/Shanghai', budget=BudgetConfig(True, daily_amount=1))
    store = Store(config.database, config.workspace)
    try:
        stamp = datetime(2026, 10, 11, 10, tzinfo=ZoneInfo(config.timezone)).timestamp()
        store.record_llm_call(created_at=stamp, model='test', metrics={'output': 100})
    finally:
        store.close()
    guard = BudgetGuard(config, Pricing())
    assert guard.status(stamp)['blocked']
    assert not guard.status(stamp + 86400)['blocked']
    monthly = BudgetGuard(replace(config, budget=BudgetConfig(True, monthly_amount=1)), Pricing())
    assert monthly.status(stamp + 86400)['blocked']
    assert not monthly.status(datetime(2026, 11, 1, tzinfo=ZoneInfo(config.timezone)).timestamp())['blocked']
    with pytest.raises(BudgetExceeded):
        BudgetGuard(config, None).check()


def test_budget_configuration_validation(tmp_path):
    path = tmp_path / 'config.json'
    bootstrap(path)
    manager = ConfigurationManager(path)
    manager.save_runtime({'budget': {'enabled': True, 'monthly_amount': 12}}, manager.revision())
    assert manager.validate().budget == BudgetConfig(True, monthly_amount=12)
    for value in (0, -1, float('nan'), float('inf')):
        with pytest.raises(ConfigError):
            manager.save_runtime({'budget': {'monthly_amount': value}}, manager.revision())


def test_daily_display_precedence_and_independent_monthly_limit(tmp_path):
    path = tmp_path / 'config.json'
    bootstrap(path)
    config = replace(ConfigurationManager(path).validate(), timezone='Asia/Shanghai',
                     budget=BudgetConfig(True, daily_amount=2, monthly_amount=3))
    stamp = datetime(2026, 10, 11, 10, tzinfo=ZoneInfo(config.timezone)).timestamp()
    store = Store(config.database, config.workspace)
    try:
        store.record_llm_call(created_at=stamp - 86400, model='test', metrics={'output': 200})
        store.record_llm_call(created_at=stamp, model='test', metrics={'output': 100})
    finally:
        store.close()
    status = BudgetGuard(config, Pricing()).status(stamp)
    assert (status['period'], status['spent'], status['amount']) == ('daily', 1, 2)
    assert status['monthly']['spent'] == 3
    assert status['blocked']
    assert '月预算' in status['reason']
    assert BudgetGuard(config, Pricing()).status(stamp + 86400)['blocked']
    daily_only = replace(config, budget=BudgetConfig(True, daily_amount=1))
    assert BudgetGuard(daily_only, Pricing()).status(stamp)['blocked']
    assert not BudgetGuard(daily_only, Pricing()).status(stamp + 86400)['blocked']


def test_existing_single_period_budget_is_preserved_and_editable(tmp_path):
    import json
    path = tmp_path / 'config.json'
    bootstrap(path)
    raw = json.loads(path.read_text())
    raw['budget'] = {'enabled': True, 'period': 'monthly', 'amount': 60}
    path.write_text(json.dumps(raw))
    manager = ConfigurationManager(path)
    assert manager.validate().budget == BudgetConfig(True, monthly_amount=60)
    assert manager.snapshot()['app']['budget'] == {'enabled': True, 'daily_amount': 0, 'monthly_amount': 60}
    manager.save_runtime({'budget': {'daily_amount': 2}}, manager.revision())
    assert manager.validate().budget == BudgetConfig(True, daily_amount=2, monthly_amount=60)
    assert 'period' not in manager.read_app()['budget']
    manager.save_runtime({'budget': {'monthly_amount': 0}}, manager.revision())
    assert manager.validate().budget == BudgetConfig(True, daily_amount=2)
