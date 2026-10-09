"""Planning never writes memory; apply never calls a model or authenticates a user."""
from copy import deepcopy
import time

from .models import MemoryPlan, MemoryPlanner, PlanningContext, canonical_json
from .validation import parse_decisions
from ..storage.commits import MemoryCommits
from ..storage.repository import MemoryRepository
from ..storage.transactions import transaction


class MemoryWritingService:
    def __init__(self, database, repository: MemoryRepository, planner: MemoryPlanner | None = None):
        self.database = database
        self.repository = repository
        self.planner = planner
        self.commits = MemoryCommits(database)

    @staticmethod
    def _validate_context(context: PlanningContext) -> None:
        if not isinstance(context.requests, list) or not context.requests:
            raise ValueError('memory plan requires requests')
        if not isinstance(context.evidence, dict) or any(
            not isinstance(key, str) or not key or not isinstance(value, str)
            for key, value in context.evidence.items()
        ):
            raise ValueError('memory evidence must map source IDs to original text')
        seen = set()
        for request in context.requests:
            if (not isinstance(request, dict)
                    or set(request) - {'target_id'} != {'id', 'type', 'event_id', 'content', 'evidence'}
                    or not isinstance(request.get('id'), str) or not request['id']
                    or request['id'] in seen
                    or request.get('type') not in ('add', 'replace', 'forget')
                    or not isinstance(request.get('event_id'), str)
                    or request['event_id'] not in context.evidence):
                raise ValueError('invalid memory request')
            seen.add(request['id'])
            if 'target_id' in request and (type(request['target_id']) is not int or request['target_id'] <= 0):
                raise ValueError('invalid memory request target')
            for key in ('content', 'evidence'):
                if not isinstance(request[key], str) or not request[key].strip() or len(request[key]) > 2000:
                    raise ValueError(f'invalid memory request {key}')
            if 'evidence' in request and request['evidence'] not in context.evidence[request['event_id']]:
                raise ValueError('memory request evidence changed')
        if not isinstance(context.snapshots, dict) or any(
            type(key) is not int or key <= 0 or not isinstance(row, dict)
            or type(row.get('id')) is not int or row['id'] != key
            for key, row in context.snapshots.items()
        ):
            raise ValueError('invalid memory snapshots')

    def review(self, context: PlanningContext, arguments: dict[str, object]) -> MemoryPlan:
        """Validate an already produced decision batch without touching the database."""
        self._validate_context(context)
        try:
            decisions = parse_decisions(arguments, context.requests, context.snapshots,
                                        context.evidence, tags=self.repository.tags)
        except (TypeError, KeyError) as error:
            raise ValueError('invalid memory decision structure') from error
        return MemoryPlan(canonical_json({
            'version': 1, 'requests': context.requests, 'evidence': context.evidence,
            'snapshots': {str(key): value for key, value in context.snapshots.items()},
            'decisions': decisions,
        }))

    async def plan(self, requests, *, evidence, snapshots=None, planner=None) -> MemoryPlan:
        if self.database.in_transaction:
            raise ValueError('memory planning requires an idle database connection')
        adapter = planner or self.planner
        if adapter is None:
            raise ValueError('memory planning requires an injected planner')
        context = PlanningContext(deepcopy(requests), deepcopy(evidence),
                                  {} if snapshots is None else deepcopy(snapshots))
        self._validate_context(context)
        requests_before = deepcopy(context.requests)
        evidence_before = deepcopy(context.evidence)
        arguments = await adapter(context)
        # The adapter can add host-verified citations and candidate snapshots,
        # but cannot rewrite the authenticated request or its original evidence.
        if context.requests != requests_before or any(
            context.evidence.get(key) != value for key, value in evidence_before.items()
        ):
            raise ValueError('memory planner changed the request or evidence')
        return self.review(context, arguments)

    def apply(self, plan: MemoryPlan, *, operation_id: str) -> dict[str, object]:
        if not isinstance(plan, MemoryPlan):
            raise ValueError('memory apply requires a MemoryPlan')
        if not isinstance(operation_id, str) or not operation_id.strip() or len(operation_id) > 500:
            raise ValueError('invalid memory operation_id')
        payload = plan.payload()
        if not isinstance(payload, dict) or set(payload) != {
            'version', 'requests', 'evidence', 'snapshots', 'decisions'
        } or type(payload['version']) is not int or payload['version'] != 1:
            raise ValueError('invalid memory plan version or fields')
        input_hash = self.commits.input_hash(payload)
        with transaction(self.database):
            previous = self.commits.result(operation_id, input_hash)
            if previous is not None:
                return previous
            context = PlanningContext(payload['requests'], payload['evidence'], plan.snapshots)
            # A serialized plan can be constructed by a caller: validate again.
            reviewed = self.review(context, {'decisions': payload['decisions']})
            current = self.repository.validate_snapshots(context.snapshots)
            now = time.time()
            results = []
            for decision in reviewed.decisions:
                action = decision['action']
                result = {'operation_ids': decision['operation_ids'], 'action': action, 'memory_ids': []}
                if action not in {'noop', 'defer'}:
                    evidence = decision['evidence']
                    last_request = next(request for request in reversed(context.requests)
                                        if request['id'] in decision['operation_ids'])
                    source = next(item for item in evidence if item['event_id'] == last_request['event_id'])
                    targets = decision['target_ids']
                    if action == 'forget':
                        for identifier in targets:
                            self.repository.forget(current[identifier], source, now=now)
                        result['memory_ids'] = list(targets)
                    else:
                        identifier = self.repository.write(decision['memory'], targets, source, evidence, now=now)
                        result['memory_ids'] = [identifier]
                results.append(result)
            result = {'operation_id': operation_id, 'decisions': results}
            self.commits.record(operation_id, input_hash, result, now)
            return result
