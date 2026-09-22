"""Word and statistics files with shared lock files and atomic writes."""

from pathlib import Path
from json import dump, load
from functools import partial
from os import fsync, replace
from typing import cast, TypedDict
from collections.abc import Callable
from tempfile import NamedTemporaryFile
from contextlib import asynccontextmanager
from asyncio import sleep, shield, timeout, to_thread, create_task, CancelledError


class Statistics(TypedDict):
    version: int
    total_observations: int
    words: dict[str, int]


STATISTICS_VERSION = 1
LOCK_POLL_INTERVAL = 0.05
LOCK_TIMEOUT = 30
MAX_COUNTER = (1 << 64) - 1


def _read(path):
    with path.open(encoding='utf-8') as source:
        return load(source)


def _write(path, data):
    temporary = None
    try:
        with NamedTemporaryFile(
            'w',
            dir=path.parent,
            prefix=f'.{path.name}-',
            suffix='.tmp',
            encoding='utf-8',
            delete=False,
        ) as output:
            temporary = Path(output.name)
            dump(data, output, ensure_ascii=False, indent=2)
            output.write('\n')
            output.flush()
            fsync(output.fileno())
        replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


async def _transaction[**P, T](
    function: Callable[P, T], *arguments: P.args, **keywords: P.kwargs
) -> T:
    # Keep the lock until an already-started filesystem operation finishes.
    task = create_task(to_thread(partial(function, *arguments, **keywords)))
    cancelled = False
    while not task.done():
        try:
            await shield(task)
        except CancelledError:
            cancelled = True
    result = task.result()
    if cancelled:
        raise CancelledError
    return result


@asynccontextmanager
async def _locked(path):
    lock_path = Path(str(path) + '.lock')
    path.parent.mkdir(parents=True, exist_ok=True)
    async with timeout(LOCK_TIMEOUT):
        while True:
            try:
                lock = lock_path.open('x')  # noqa: ASYNC230 - atomic, tiny lock acquisition
                lock.close()
                break
            except FileExistsError:
                await sleep(LOCK_POLL_INTERVAL)
    try:
        yield
    finally:
        lock_path.unlink()  # noqa: ASYNC240 - release synchronously even during cancellation


def _statistics(path: Path) -> Statistics:
    if not path.exists():
        return {'version': STATISTICS_VERSION, 'total_observations': 0, 'words': {}}
    data = _read(path)
    if (
        not isinstance(data, dict)
        or type(data.get('version')) is not int
        or data['version'] != STATISTICS_VERSION
        or type(data.get('total_observations')) is not int
        or not isinstance(data.get('words'), dict)
    ):
        raise ValueError('Invalid statistics file version or word counts')
    if any(
        not word.strip() or type(count) is not int or not 0 <= count <= MAX_COUNTER
        for word, count in data['words'].items()
    ):
        raise ValueError('Invalid word observation count')
    total = sum(data['words'].values())
    if total != data.get('total_observations') or total > MAX_COUNTER:
        raise ValueError('Statistics total does not match word counts')
    return cast(Statistics, data)


def _records(path):
    records = _read(path)
    if not isinstance(records, list) or any(
        not isinstance(record, dict)
        or not isinstance(record.get('word'), str)
        or not record['word'].strip()
        for record in records
    ):
        raise ValueError('Word database must be an array of records with nonempty words')
    return records


class WordStore:
    def __init__(self, database, statistics=None):
        self.database = Path(database).resolve()
        self.statistics = (
            Path(statistics).resolve()
            if statistics
            else self.database.with_name(
                self.database.stem + '.stats' + (self.database.suffix or '.json')
            )
        )
        if self.database == self.statistics:
            raise ValueError('Database and statistics paths must differ')

    async def load(self):
        async with _locked(self.database):
            records = await _transaction(_records, self.database)
        counts = await self.counts()
        return [record['word'] for record in records], counts['words']

    async def counts(self):
        async with _locked(self.statistics):
            return await _transaction(_statistics, self.statistics)

    async def observe(self, word: str):
        word = word.strip()
        if not word:
            raise ValueError('Observed answer is empty')
        async with _locked(self.statistics):
            return await _transaction(self._observe, word)

    async def learn(self, word: str):
        word = word.strip()
        if not word:
            raise ValueError('Revealed answer is empty')
        async with _locked(self.database):
            return await _transaction(self._learn, word)

    def _observe(self, word):
        data = _statistics(self.statistics)
        if data['total_observations'] == MAX_COUNTER:
            raise OverflowError('Observation counter exhausted')
        data['words'][word] = data['words'].get(word, 0) + 1
        data['total_observations'] += 1
        _write(self.statistics, data)
        return data

    def _learn(self, word):
        records = _records(self.database)
        if not any(record['word'] == word for record in records):
            count = len(word.split())
            records.append(
                {
                    'word': word,
                    'letter_count': sum(char.isalpha() for char in word),
                    'multi_word': count > 1,
                    'contains_hyphen': '-' in word,
                    'word_count': count,
                }
            )
            records.sort(key=lambda record: (record['word'].lower(), record['word']))
            _write(self.database, records)
        return [record['word'] for record in records]
