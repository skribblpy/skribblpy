"""Indexed, case-sensitive hint matching and frequency/close-guess ranking."""

from re import split
from functools import lru_cache
from collections import Counter, OrderedDict, defaultdict

PATTERN_CACHE_SIZE = 64


@lru_cache(maxsize=8192)
def _parts(text: str):
    return tuple(
        ''.join(char for char in part if char.isalpha() or char == '_')
        for part in split(r'[\s-]+', text.strip())
        if any(char.isalpha() or char == '_' for char in part)
    )


@lru_cache(maxsize=8192)
def _separators(text: str):
    result, pending, have_part = [], '', False
    for char in text:
        if char.isspace() or char == '-':
            if have_part:
                pending = '-' if char == '-' or pending == '-' else ' '
        elif char.isalpha() or char == '_':
            if pending:
                result.append(pending)
            pending, have_part = '', True
    return tuple(result)


def _letters(text: str):
    return ''.join(char.lower() for char in text if char.isalnum())


def _distance(first: str, second: str):
    if len(first) > len(second):
        first, second = second, first
    previous = list(range(len(first) + 1))
    for row, right in enumerate(second, 1):
        current = [row]
        for column, left in enumerate(first, 1):
            current.append(
                min(current[-1] + 1, previous[column] + 1, previous[column - 1] + (left != right))
            )
        previous = current
    return previous[-1]


class WordIndex:
    """Shape and revealed-position indexes avoid scanning the full dictionary."""

    def __init__(self, words, observations=None):
        self.words = tuple(sorted(set(words), key=lambda word: (word.lower(), word)))
        self.observations = observations or {}
        self.parts = tuple(_parts(word) for word in self.words)
        self.separators = tuple(_separators(word) for word in self.words)
        self._word_ids = {word: index for index, word in enumerate(self.words)}
        self._comparison = tuple(_letters(word) for word in self.words)
        self._messages = defaultdict(set)
        self._patterns: OrderedDict[str, tuple[frozenset[int], dict[int, frozenset[str]]]] = (
            OrderedDict()
        )
        self.shapes = defaultdict(set)
        self.positions = defaultdict(set)
        for index, parts in enumerate(self.parts):
            self._messages[' '.join(self.words[index].split()).casefold()].add(index)
            self.shapes[tuple(map(len, parts))].add(index)
            for part_index, part in enumerate(parts):
                for position, char in enumerate(part):
                    self.positions[part_index, position, char].add(index)

    def matching(self, hint: str, attempted=()):
        matched, _ = self._pattern(hint)
        if not attempted:
            return set(matched)
        excluded = {self._word_ids[word] for word in attempted if word in self._word_ids}
        return matched - excluded

    def _pattern(self, hint: str) -> tuple[frozenset[int], dict[int, frozenset[str]]]:
        cached = self._patterns.get(hint)
        if cached is not None:
            self._patterns.move_to_end(hint)
            return cached
        parts = _parts(hint)
        if not parts:
            return frozenset(), {}
        constraints = [self.shapes.get(tuple(map(len, parts)), set())]
        for part_index, part in enumerate(parts):
            for position, char in enumerate(part):
                if char != '_':
                    constraints.append(self.positions.get((part_index, position, char), set()))
        constraints.sort(key=len)
        matched = constraints[0].copy()
        for constraint in constraints[1:]:
            matched.intersection_update(constraint)
            if not matched:
                break
        separators = _separators(hint)
        hyphens = tuple(
            position for position, separator in enumerate(separators) if separator == '-'
        )
        if hyphens:
            matched = {
                index
                for index in matched
                if all(self.separators[index][position] == '-' for position in hyphens)
            }
        hidden = tuple(
            (part_index, position)
            for part_index, part in enumerate(parts)
            for position, character in enumerate(part)
            if character == '_'
        )
        letters = {
            index: frozenset(self.parts[index][part][position] for part, position in hidden)
            for index in matched
        }
        result = frozenset(matched), letters
        self._patterns[hint] = result
        if len(self._patterns) > PATTERN_CACHE_SIZE:
            self._patterns.popitem(last=False)
        return result

    def candidates(self, hint: str, attempted=(), close_guesses=()):
        matched = self.matching(hint, attempted)
        _, unique_letters = self._pattern(hint)
        frequencies = Counter(char for index in matched for char in unique_letters[index])
        close = tuple(_letters(guess) for guess in close_guesses)

        def rank(index):
            word = self.words[index]
            distances = [_distance(self._comparison[index], guess) for guess in close]
            score = sum(len(matched) - frequencies[char] for char in unique_letters[index])
            return (
                sum(distances),
                max(distances, default=0),
                -self.observations.get(word, 0),
                -score,
                index,
            )

        return [self.words[index] for index in sorted(matched, key=rank)]

    def matching_message(self, message: str, hint: str):
        folded = ' '.join(message.split()).casefold()
        ids = self._messages.get(folded)
        if not ids:
            return set()
        matched, _ = self._pattern(hint)
        return {self.words[index] for index in ids & matched}
