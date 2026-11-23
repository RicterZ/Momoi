import json

from evals.episode_relations import DATASET, _validate, score


def test_dataset_cases_are_well_formed():
    cases = json.loads(DATASET.read_text(encoding="utf-8"))["cases"]
    assert len({case["id"] for case in cases}) == len(cases)
    for case in cases:
        assert case["source"]["conversation"]
        for item in case["candidates"]:
            assert item["label"] in {"follows_up", "revises", "context", "none"}
            assert item["label"] in item.get("accept", [item["label"]])
            assert item["turns"]


def test_validate_requires_verbatim_evidence():
    case = json.loads(DATASET.read_text(encoding="utf-8"))["cases"][0]
    relation = {"target_episode_id": "ep_game_start", "relation": "follows_up", "explanation": "x",
                "source_evidence": "打到第三章了", "target_evidence": "今晚开玩"}
    assert _validate(case, [relation]) is None
    assert "target_evidence" in _validate(case, [{**relation, "target_evidence": "改写过的内容"}])
    assert _validate(case, [{**relation, "target_episode_id": "ep_missing"}])


def test_score_separates_accepted_and_off_topic_edges():
    def pair(label, predicted, category, accept=None):
        return {"label": label, "predicted": predicted, "category": category, "accept": accept or [label]}
    summary = score([
        {"pairs": [pair("follows_up", "follows_up", "core"), pair("none", "follows_up", "bridge")]},
        {"pairs": [pair("revises", "follows_up", "core", ["revises", "follows_up"]),
                   pair("none", "none", "incidental")]},
        {"error": "RuntimeError"},
    ])
    assert summary["errors"] == 1
    assert summary["follows_up_precision"] == round(2 / 3, 4)
    assert summary["follows_up_recall"] == 1.0
    assert summary["negative_follows_up_rate"] == 0.5
    assert summary["mistakes"] == {"bridge:none->follows_up": 1}
