"""Okapi BM25 over a small in-memory corpus, tokenized without a segmenter.

ASCII runs become casefolded words; other letters become character bigrams
(single characters for one-letter runs), which suits Chinese without jieba.
"""
import math
import re
import unicodedata
from collections import Counter
from collections.abc import Sequence

BM25_K1 = 1.2
BM25_B = 0.75
_RUNS = re.compile(r"[0-9a-z_]+|[^\W\d_a-z]+")


def bm25_tokens(text: str) -> list[str]:
    tokens = []
    for run in _RUNS.findall(unicodedata.normalize("NFKC", text).casefold()):
        if run.isascii():
            tokens.append(run)
        elif len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[i:i + 2] for i in range(len(run) - 1))
    return tokens


def bm25_scores(query: str, documents: Sequence[str]) -> list[float]:
    if not documents:
        return []
    corpus = [Counter(bm25_tokens(document)) for document in documents]
    lengths = [sum(counts.values()) for counts in corpus]
    average = sum(lengths) / len(lengths) or 1.0
    terms = set(bm25_tokens(query))
    frequency = {term: sum(1 for counts in corpus if term in counts) for term in terms}
    scores = []
    for counts, length in zip(corpus, lengths):
        score = 0.0
        for term in terms:
            tf = counts.get(term, 0)
            if not tf:
                continue
            df = frequency[term]
            idf = math.log(1 + (len(corpus) - df + 0.5) / (df + 0.5))
            score += idf * tf * (BM25_K1 + 1) / (tf + BM25_K1 * (1 - BM25_B + BM25_B * length / average))
        scores.append(score)
    return scores
