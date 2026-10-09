"""Planning never writes memory; apply never calls a model or authenticates a user."""
from copy import deepcopy
import time
import math

from .candidates import CandidateBudgetExceeded, WriteCandidates, check_budget
from .models import MemoryPlan, MemoryPlanner, PlanningContext, canonical_json
from .validation import parse_decisions
from ..storage.records import memory_scope
from ..metadata import validate_scope
from ..storage.commits import MemoryCommits
from ..storage.repository import MemoryRepository
from ..storage.transactions import transaction


class MemoryWritingService:
    def __init__(self, database, repository: MemoryRepository, planner: MemoryPlanner | None = None, *, recall=None):
        self.database = database
        self.repository = repository
        self.planner = planner
        self.commits = MemoryCommits(database)
        self.candidates = WriteCandidates(repository, recall)

    @staticmethod
    def _validate_context(context: PlanningContext) -> None:
        if not isinstance(context.requests, list) or not context.requests:
            raise ValueError('memory plan requires requests')
        if not isinstance(context.evidence, dict) or any(
            not isinstance(key, str) or not key or not isinstance(value, str)
            for key, value in context.evidence.items()
        ):
            raise ValueError('memory evidence must map source IDs to original text')
        if not isinstance(context.evidence_times, dict) or any(
            key not in context.evidence or type(value) not in (int, float) or not math.isfinite(value)
            for key, value in context.evidence_times.items()
        ):
            raise ValueError('invalid memory evidence times')
        if not isinstance(context.retrieval_fallback, str):
            raise ValueError('invalid memory retrieval fallback')
        if not isinstance(context.snapshots, dict) or any(
            type(key) is not int or key <= 0 or not isinstance(row, dict)
            or type(row.get('id')) is not int or row['id'] != key
            for key, row in context.snapshots.items()
        ):
            raise ValueError('invalid memory snapshots')

        seen = set()
        for request in context.requests:
            if (not isinstance(request, dict)
                    or set(request) - {'target_id', 'scope'} != {'id', 'type', 'event_id', 'content', 'evidence'}
                    or not isinstance(request.get('id'), str) or not request['id']
                    or request['id'] in seen
                    or request.get('type') not in ('add', 'replace', 'forget')
                    or not isinstance(request.get('event_id'), str)
                    or request['event_id'] not in context.evidence):
                raise ValueError('invalid memory request')
            seen.add(request['id'])
            if 'target_id' in request and (type(request['target_id']) is not int or request['target_id'] <= 0):
                raise ValueError('invalid memory request target')
            if 'scope' in request:
                validate_scope(request['scope'])
            for key in ('content', 'evidence'):
                if not isinstance(request[key], str) or not request[key].strip() or len(request[key]) > 2000:
                    raise ValueError(f'invalid memory request {key}')
            if 'evidence' in request and request['evidence'] not in context.evidence[request['event_id']]:
                raise ValueError('memory request evidence changed')

    def review(self, context: PlanningContext, arguments: dict[str, object]) -> MemoryPlan:
        """Validate an already produced decision batch without touching the database."""
        self._validate_context(context)
        try:
            decisions = parse_decisions(arguments, context.requests, context.snapshots,
                                        context.evidence, tags=self.repository.tags)
        except (TypeError, KeyError) as error:
            raise ValueError('invalid memory decision structure') from error
        if any(item['action'] != 'defer' for item in decisions):
            check_budget(context.snapshots)
        for item in decisions:
            for request in context.requests:
                if request['id'] in item['operation_ids'] and 'scope' in request and any(
                    memory_scope(context.snapshots[target]) != request['scope']
                    for target in item.get('target_ids', [])
                ):
                    raise ValueError('memory targets do not match request scope')
            if item['action'] == 'write':
                if context.retrieval_fallback not in ('', 'disabled'):
                    raise ValueError('candidate recall failed; defer this request')
                scope = memory_scope(item['memory'])
                if any(memory_scope(context.snapshots[target]) != scope for target in item['target_ids']):
                    raise ValueError('memory writes cannot move or merge across scopes')
                if any('scope' in request and request['scope'] != scope for request in context.requests
                       if request['id'] in item['operation_ids']):
                    raise ValueError('memory write does not match request scope')
        return MemoryPlan(canonical_json({
            'version': 4, 'requests': context.requests, 'evidence': context.evidence,
            'snapshots': {str(key): value for key, value in context.snapshots.items()},
            'decisions': decisions,
            'evidence_times': context.evidence_times, 'retrieval_fallback': context.retrieval_fallback,
        }))

    async def plan(self, requests, *, evidence, snapshots=None, evidence_times=None, planner=None) -> MemoryPlan:
        if self.database.in_transaction:
            raise ValueError('memory planning requires an idle database connection')
        adapter = planner or self.planner
        if adapter is None:
            raise ValueError('memory planning requires an injected planner')
        context = PlanningContext(deepcopy(requests), deepcopy(evidence),
                                  {} if snapshots is None else deepcopy(snapshots),
                                  evidence_times=deepcopy(evidence_times or {}))
        self._validate_context(context)
        try:
            await self.candidates.collect(context)
        except CandidateBudgetExceeded as error:
            context.snapshots.clear()
            context.retrieval_fallback = str(error)
            return self._defer(context)
        if context.retrieval_fallback not in ('', 'disabled'):
            return self._defer(context)
        requests_before = deepcopy(context.requests)
        evidence_before = deepcopy(context.evidence)
        times_before = deepcopy(context.evidence_times)
        arguments = await adapter(context)
        # The adapter can add host-verified citations and candidate snapshots,
        # but cannot rewrite the authenticated request or its original evidence.
        if context.requests != requests_before or context.evidence_times != times_before or any(
            context.evidence.get(key) != value for key, value in evidence_before.items()
        ):
            raise ValueError('memory planner changed the request or evidence')
        return self.review(context, arguments)

    def _defer(self, context):
        return self.review(context, {'decisions': [{
            'operation_ids': [request['id'] for request in context.requests],
            'action': 'defer', 'reason': 'Candidate review unavailable: ' + context.retrieval_fallback[:400],
        }]})

    def apply(self, plan: MemoryPlan, *, operation_id: str) -> dict[str, object]:
        if not isinstance(plan, MemoryPlan):
            raise ValueError('memory apply requires a MemoryPlan')
        if not isinstance(operation_id, str) or not operation_id.strip() or len(operation_id) > 500:
            raise ValueError('invalid memory operation_id')
        payload = plan.payload()
        if not isinstance(payload, dict) or set(payload) != {
            'version', 'requests', 'evidence', 'snapshots', 'decisions',
            'evidence_times', 'retrieval_fallback'
        } or type(payload['version']) is not int or payload['version'] != 4:
            raise ValueError('invalid memory plan version or fields')
        input_hash = self.commits.input_hash(payload)
        with transaction(self.database):
            previous = self.commits.result(operation_id, input_hash)
            if previous is not None:
                return previous
            context = PlanningContext(payload['requests'], payload['evidence'], plan.snapshots,
                                      payload['evidence_times'], payload['retrieval_fallback'])
            # A serialized plan can be constructed by a caller: validate again.
            reviewed = self.review(context, {'decisions': payload['decisions']})
            current = self.repository.validate_snapshots(context.snapshots)
            now = time.time()
            results = []
            for decision in reviewed.decisions:
                action = decision['action']
                result = {'operation_ids': decision['operation_ids'], 'action': action, 'memory_ids': []}
                if action != 'defer' and 'target_ids' in decision:
                    evidence = decision['evidence']
                    last_request = next(request for request in reversed(context.requests)
                                        if request['id'] in decision['operation_ids'])
                    source = next(item for item in evidence if item['event_id'] == last_request['event_id'])
                    targets = decision['target_ids']
                    if action in {'noop', 'metadata'}:
                        for identifier in targets:
                            if action == 'metadata':
                                self.repository.update_meta(current[identifier], decision['meta'], now=now)
                            for citation in evidence:
                                self.repository.add_evidence(identifier, citation['event_id'], citation['quote'], now)
                        result['memory_ids'] = list(targets)
                    elif action == 'forget':
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
