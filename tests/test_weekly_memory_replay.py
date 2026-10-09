from tools.weekly_memory_replay import classify, month_start


def observations(n=8, day='2026-10-08'):
    return [{'id': str(i), 'date': day} for i in range(n)]


def finding(n, **changes):
    return dict(key='topic', content='一个共同认识', events=[{'observation_ids': [str(i)]} for i in range(n)],
                conflicts=[], **changes)


def test_thresholds_and_replay_do_not_double_count():
    data = observations()
    for n, status in [(2, None), (3, 'watch'), (4, 'watch'), (5, 'admission_candidate'), (8, 'long_term_candidate')]:
        raw = {'findings': [finding(n)]}
        result = classify(raw, data, '2026-10-09')['findings']
        assert (result[0]['status'] if result else None) == status
        if result:
            assert classify(raw, data, '2026-10-09', result)['findings'][0]['count'] == n
    previous = classify({'findings': [finding(3)]}, data, '2026-10-09')['findings']
    assert classify({'findings': [finding(5)]}, data, '2026-10-09', previous)['findings'][0]['count'] == 5


def test_same_event_is_counted_once_and_conflict_blocks_upgrade():
    data = observations(9)
    item = finding(8)
    item['events'] = [{'observation_ids': ['0', '1', '2']}] + item['events'][3:]
    result = classify({'findings': [item]}, data, '2026-10-09')['findings'][0]
    assert result['count'] == 6
    item['conflicts'] = ['8']
    assert classify({'findings': [item]}, data, '2026-10-09')['findings'][0]['status'] == 'blocked'


def test_month_expiry_recomputes_reviewed_candidates():
    data = observations(8, '2026-09-20')
    previous = classify({'findings': [finding(8)]}, data, '2026-09-21')['findings']
    data[0]['date'] = '2026-09-08'
    result = classify({'findings': [finding(8)]}, data, '2026-10-09', previous)['findings'][0]
    assert result['count'] == 7 and result['status'] == 'admission_candidate'
    assert not classify({'findings': [finding(8)]}, data, '2026-10-21', previous)['findings']
    assert month_start('2026-03-31') == '2026-02-28'
    assert month_start('2024-03-31') == '2024-02-29'
    assert month_start('2026-01-09') == '2025-12-09'


def test_old_event_mention_does_not_renew_and_new_candidates_need_recent_support():
    data = observations(8, '2026-09-20')
    assert not classify({'findings': [finding(8)]}, data, '2026-10-09')['findings']
    data = observations(4)
    data[0]['date'] = '2026-09-01'
    item = finding(4)
    item['events'] = [{'observation_ids': ['0', '1']}] + item['events'][2:]
    assert not classify({'findings': [item]}, data, '2026-10-09')['findings']


def test_old_candidates_can_be_corrected_or_removed_but_routing_must_be_valid():
    import pytest
    data = observations(5)
    previous = classify({'findings': [finding(5)]}, data, '2026-10-09')['findings']
    assert classify({'findings': [finding(3)]}, data, '2026-10-09', previous)['findings'][0]['count'] == 3
    assert not classify({'findings': []}, data, '2026-10-09', previous)['findings']
    item = finding(3)
    item['events'].append({'observation_ids': ['0']})
    with pytest.raises(ValueError, match='unknown_or_repeated'):
        classify({'findings': [item]}, data, '2026-10-09')
