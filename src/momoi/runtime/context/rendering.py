import logging
import math
from xml.etree import ElementTree
from xml.sax.saxutils import escape, quoteattr

from ...conversation_roles import speaker_label

from ...observability.events import log_event
from ...storage import (
    REFLECTION_MEMORY_CAUTION,
    Store,
    estimate_tokens,
    format_reflection_memory,
    truncate_tokens,
)
from ...storage.episode_ranking import rank_recall_items
from ...storage.memory_values import format_memory
from ..agent.budget import SECTION_BUDGET_ALLOCATOR
from .retrieval import _merge_matches

logger = logging.getLogger(__name__)


def _memory_lines(items: object) -> str:
    if not isinstance(items, list):
        return ""
    return "\n".join(
        format_memory(item)
        for item in items
        if isinstance(item, dict)
        and item.get("kind") not in (None, "")
        and item.get("key") not in (None, "")
        and item.get("content") not in (None, "")
    )


def _reflection_memory_lines(items: object) -> str:
    if not isinstance(items, list):
        return ""
    return "\n".join(
        format_reflection_memory(item)
        for item in items
        if isinstance(item, dict)
        and item.get("kind") not in (None, "")
        and item.get("key") not in (None, "")
        and item.get("content") not in (None, "")
    )


def _episode_search_text(episode: dict[str, object]) -> str:
    return " ".join(
        str(episode.get(name) or "")
        for name in (
            "title",
            "narrative_summary",
            "working_summary",
            "topics",
            "entities",
            "open_loops",
            "matches",
        )
    )


def _goal_directory_lines(items: object) -> str:
    """Render the part of a Goal that survives its execution unchanged."""

    if not isinstance(items, list):
        return ""
    return "\n".join(
        f"<goal id={quoteattr(str(item['id']))} "
        f"title={quoteattr(truncate_tokens(str(item.get('title') or ''), 80))} />"
        for item in items
        if isinstance(item, dict) and item.get("id")
    )


def _episode_summary(episode: dict[str, object]) -> tuple[str, str]:
    narrative = str(episode.get("narrative_summary") or "")
    if narrative:
        return narrative, "narrative"
    claims = episode.get("working_summary_claims")
    if isinstance(claims, list) and claims:
        return str(episode.get("working_summary") or ""), "extractive"
    return "", "empty"


def _episode_header(
    episode: dict[str, object], selected: dict[str, object] | None = None
) -> str:
    parts = [f"id={quoteattr(str(episode['id']))}"]
    status = str(episode.get("status") or "")
    if status:
        parts.append(f"status={quoteattr(status)}")
    confidence = (selected or {}).get("relevance_confidence")
    if (
        isinstance(confidence, (int, float))
        and not isinstance(confidence, bool)
        and math.isfinite(confidence)
        and 0 <= confidence <= 1
    ):
        parts.append(f'confidence="{confidence:.3f}"')
    return f"<episode {' '.join(parts)}>"


def _episode_match_lines(
    selected: dict[str, object],
    token_budget: int,
    exclude_message_ids: set[int],
) -> list[str]:
    matches = [
        match
        for match in selected.get("matches") or []
        if isinstance(match, dict)
        and match.get("id") not in exclude_message_ids
        and str(match.get("content") or "").strip()
    ][:3]
    if not matches or token_budget <= 0:
        return []
    per_match = max(1, token_budget // len(matches))
    lines = ["<matched_evidence>"]
    for match in matches:
        role = str(match.get("role") or "")
        delivery = str(match.get("delivery_state") or "")
        source = speaker_label(role)
        attributes = {
            "source": source,
            "turn_id": match.get("turn_id"),
        }
        content = str(match["content"])
        if role == "assistant":
            attributes["delivery"] = delivery or "unknown"
        header = " ".join(
            f"{key}={quoteattr(str(value))}"
            for key, value in attributes.items()
            if value is not None
        )
        lines.append(
            f"<message {header}>"
            f"{escape(truncate_tokens(content, per_match))}</message>"
        )
    lines.append("</matched_evidence>")
    return lines


def _fit_episode_xml(text: str, token_budget: int) -> str:
    """Fit text nodes, keeping XML boundaries and attributes intact."""
    root = ElementTree.fromstring(text)
    ElementTree.indent(root, space="  ")

    def serialize() -> str:
        return ElementTree.tostring(root, encoding="unicode", short_empty_elements=False)

    rendered = serialize()
    if estimate_tokens(rendered) <= token_budget:
        return rendered
    leaves = [(node, node.text or "") for node in root.iter() if not len(node)]
    sizes = [estimate_tokens(value) for _, value in leaves]
    total = sum(sizes)

    def fit(available: int) -> str:
        for (node, value), size in zip(leaves, sizes, strict=True):
            node.text = truncate_tokens(value, available * size // max(1, total))
        return serialize()

    rendered = fit(0)
    if estimate_tokens(rendered) > token_budget:
        return ""
    low, high = 0, total
    while low < high:
        middle = (low + high + 1) // 2
        candidate = fit(middle)
        if estimate_tokens(candidate) <= token_budget:
            low = middle
            rendered = candidate
        else:
            high = middle - 1
    return rendered


def _episode_context(
    store: Store,
    episodes: object,
    summary_token_budget: int,
    raw_token_budget: int = 0,
    exclude_message_ids: set[int] | None = None,
) -> str:
    if not isinstance(episodes, list):
        return ""
    existing = [
        item
        for item in episodes
        if not item.get("is_new") and store.episode(str(item["episode_id"]))
    ]
    if not existing or summary_token_budget <= 0:
        return ""
    per_summary = max(1, summary_token_budget // len(existing))
    per_raw = max(
        1,
        min(
            per_summary,
            (raw_token_budget or summary_token_budget) // len(existing),
        )
        // 2,
    )
    excluded = exclude_message_ids or set()
    sections: list[str] = []
    quality_counts: dict[str, int] = {}
    for selected in existing:
        episode = store.episode(str(selected["episode_id"]))
        if episode is None:
            continue
        lines = [
            _episode_header(episode, selected),
            f"<title>{escape(str(episode['title']))}</title>",
        ]
        summary, quality = _episode_summary(episode)
        quality_counts[quality] = quality_counts.get(quality, 0) + 1
        lines.extend(_episode_match_lines(selected, per_raw, excluded))
        lines.append(
            "<summary>"
            f"{escape(truncate_tokens(summary, max(1, per_summary - per_raw)))}</summary>"
        )
        if episode["open_loops"]:
            lines.append(
                "<open_loops>" + "".join(
                    f"<item>{escape(str(item))}</item>" for item in episode["open_loops"]
                ) + "</open_loops>"
            )
        lines.append("</episode>")
        section = _fit_episode_xml("\n".join(lines), per_summary)
        if section:
            sections.append(section)
    rendered = "\n\n".join(sections)
    log_event(
        logger,
        logging.INFO,
        "episode_directory_assembled",
        stage="context_recall",
        episodes=len(sections),
        tokens=estimate_tokens(rendered) if rendered else 0,
        raw_messages=0,
        summary_quality=quality_counts,
    )
    return rendered


def assemble_main_context(
    store: Store,
    retrieval: dict[str, object],
    summary_token_budget: int,
) -> dict[str, str]:
    return {
        "episodes": _episode_context(
            store,
            retrieval.get("episodes"),
            summary_token_budget,
        ),
        "long_term_memories": str(retrieval.get("long_term_memories") or ""),
        "recent_memories": str(retrieval.get("recent_memories") or ""),
        "recall_memories": _memory_lines(retrieval.get("recall_memories")),
        "query_recall": str(retrieval.get("query_recall") or ""),
        "reflection_memories": (
            REFLECTION_MEMORY_CAUTION
            + "\n"
            + _reflection_memory_lines(retrieval.get("reflection_memories"))
            if retrieval.get("reflection_memories")
            else ""
        ),
        "goal_directory": _goal_directory_lines(retrieval.get("goals")),
    }


def recall_episode_context(
    store: Store,
    query: str,
    max_results: int,
    summary_token_budget: int,
) -> str:
    query = query.strip()
    if not query:
        return ""
    episodes = SECTION_BUDGET_ALLOCATOR.select(
        [("query", store.search_episodes(query, max_results))],
        lambda row: row["id"],
        lambda row: truncate_tokens(
            _episode_search_text(row),
            max(1, summary_token_budget // max(1, max_results)),
        ),
        lambda row: {
            "episode_id": row["id"],
            "relation": "recalled",
            "is_new": False,
            "matches": row.get("matches", []),
            "matched_keywords": row.get("matched_keywords", []),
            "keyword_match_count": row.get("keyword_match_count", 0),
            "search_score": row.get("search_score", 0),
            "relevance_confidence": row.get("relevance_confidence"),
        },
        _merge_matches,
        max_results,
        summary_token_budget,
    )
    episodes = rank_recall_items(episodes)
    return _episode_context(
        store,
        episodes,
        summary_token_budget,
        summary_token_budget,
    )
