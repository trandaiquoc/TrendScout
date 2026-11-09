"""Bounded candidate-only token Jaccard; never merges records."""
from collections import defaultdict
from dataclasses import dataclass
import re
import unicodedata

VERSION = 'token-jaccard-v0.1'


@dataclass(frozen=True)
class Candidate:
    left: object
    right: object
    normalized_left: str
    normalized_right: str
    score: float
    threshold: float
    method: str = 'token_jaccard'
    algorithm_version: str = VERSION
    reason: str = 'shared_tokens_review_required'
    review_status: str = 'unreviewed'


def tokens(title):
    normalized = unicodedata.normalize('NFC', title).casefold().replace('_',' ')
    words = tuple(re.findall(r'[^\W_]+', normalized, re.UNICODE))
    return ' '.join(words), frozenset(words)


def candidates(rows, *, threshold=.8, max_batch=1000, max_pairs=10000, max_per_keyword=5, max_posting=50):
    if not 0 <= threshold <= 1 or min(max_batch, max_pairs, max_per_keyword, max_posting) < 1:
        raise ValueError('invalid candidate limits')
    if len(rows) > max_batch:
        return (), 'SKIPPED', 'batch_limit', 0
    ordered = sorted(rows, key=lambda r: (r.source, r.country_scope, r.access, r.keyword_id, r.observation_id))
    index = defaultdict(list)
    pairs = set()
    processed = []
    matches = []
    for row in ordered:
        name, words = tokens(row.canonical_keyword)
        if len(name) < 8 or len(words) < 2 or len(name) > 256 or len(words) > 40:
            continue
        group = (row.source, row.country_scope, row.access)
        neighbors = set()
        for word in sorted(words):
            neighbors.update(index[(group, word)])
        for prior in sorted(neighbors):
            other, other_name, other_words = processed[prior]
            if row.keyword_id == other.keyword_id:
                continue  # The same entity on another day is not a near-duplicate.
            pair = tuple(sorted((row.keyword_id, other.keyword_id)))
            if pair in pairs:
                continue
            pairs.add(pair)
            if len(pairs) > max_pairs:
                return (), 'SKIPPED', 'comparison_limit', max_pairs
            score = len(words & other_words) / len(words | other_words)
            if score >= threshold:
                yield_candidate = Candidate(other, row, other_name, name, score, threshold,
                    reason='cross_project_review_required' if other.project != row.project else 'shared_tokens_review_required')
                # Keep the complete bounded set until deterministic rank selection.
                matches.append(yield_candidate)
        idx = len(processed)
        processed.append((row, name, words))
        for word in sorted(words):
            posting = index[(group, word)]
            if len(posting) >= max_posting:
                return (), 'SKIPPED', 'posting_limit', len(pairs)
            posting.append(idx)
    counts = defaultdict(int)
    selected = []
    for candidate in sorted(matches, key=lambda c: (-c.score, c.left.keyword_id, c.right.keyword_id,
                                                     c.left.observation_id, c.right.observation_id)):
        a,b = candidate.left.keyword_id, candidate.right.keyword_id
        if counts[a] >= max_per_keyword or counts[b] >= max_per_keyword:
            continue
        selected.append(candidate)
        counts[a] += 1
        counts[b] += 1
    return tuple(selected), 'COMPLETED', 'candidate_only', len(pairs)
