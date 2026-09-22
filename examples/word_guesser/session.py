"""Event-driven guessing, turn cancellation, and worker lifecycle."""

from pathlib import Path
from math import isfinite
from logging import getLogger
from dataclasses import dataclass
from skribblpy import Event as ClientEvent
from examples.word_guesser.ranking import WordIndex
from examples.word_guesser.storage import WordStore
from examples.word_guesser.logging import EventLogger, player_label, SessionLogger
from asyncio import wait, Event, sleep, gather, TaskGroup, create_task, FIRST_COMPLETED
from skribblpy import Phase, Client, EventName, ActionError, Disconnected, JoinRejected

DEFAULT_LOBBY_DELAY = 10.0
DEFAULT_STARTUP_DELAY = 10.0
DEFAULT_DATABASE = str(Path(__file__).with_name('words.json'))
MAX_CLIENTS = 32
MAX_RETRY_DELAY = 60.0
POLICY_REJECTIONS = frozenset((3, 4, 5, 100, 200, 300))
GREETING = (
    "I'm an experimental Python word guesser learning from revealed answers and word frequencies."
)
EXHAUSTED_MESSAGE = "I've run out of matching words. I'll learn the answer when this turn ends."


@dataclass(frozen=True, slots=True)
class Options:
    database: str = DEFAULT_DATABASE
    statistics: str = ''
    name: str = ''
    language: str = '0'
    max_clients: int = 2
    startup_delay: float = DEFAULT_STARTUP_DELAY
    lobby_delay: float = DEFAULT_LOBBY_DELAY
    message_interval: float = 1.0
    lobby_id: str = ''


class GuesserSession:
    def __init__(
        self, client: Client, store: WordStore, index: WordIndex, logger=None, *, quiet_hints=False
    ):
        self.client = client
        self.store = store
        self.index = index
        self.logger = logger or getLogger(__name__)
        self.event_logger = EventLogger(self.logger)
        self.quiet_hints = quiet_hints
        self.depart_reason = 'Connection ended'
        self.depart = Event()
        self.joined = False
        self.turn = 0
        self.observed_turns = set()
        self.learned_turns = set()
        self.attempted = set()
        self.close_guesses = []
        self._guess_task = None
        self._messages = []
        self._revision = None
        self._exhausted = False
        self.error = None

        @client.on(EventName.ALL)
        async def on_event(event: ClientEvent) -> None:
            await self.handle(event)

        self._handler = on_event

    async def handle(self, event: ClientEvent) -> None:
        state = event.snapshot
        self.event_logger.log(event)
        if event.name == EventName.LOBBY:
            self.joined = True
            self.logger.info('Joined lobby %r.', state.lobby_id)
        if state.turn_id != self.turn:
            await self._stop_guessing()
            for message in self._messages:
                message.cancel()
            self._messages.clear()
            self.turn = state.turn_id
            self.attempted.clear()
            self.close_guesses.clear()
            self._exhausted = False
        should_depart = (
            state.is_drawer
            or (state.ready and len(state.players) <= 1)
            or (state.vote_kick and state.vote_kick.target_id == state.me)
        )
        if should_depart:
            if not self.depart.is_set():
                if state.is_drawer:
                    self.depart_reason = 'Assigned to draw'
                    self.logger.warning(
                        'Assigned to draw (%s) in lobby %r. Leaving now.',
                        Phase(state.phase).name.lower(),
                        state.lobby_id,
                    )
                elif state.ready and len(state.players) <= 1:
                    self.depart_reason = 'No other players in the lobby'
                    self.logger.warning(
                        'No other players in lobby %r. Leaving now.', state.lobby_id
                    )
                elif state.vote_kick is not None:
                    self.depart_reason = 'Vote-kick against bot'
                    vote = state.vote_kick
                    self.logger.warning(
                        'Vote-kick in lobby %r: %d of %d votes. Leaving now.',
                        state.lobby_id,
                        vote.current_votes,
                        vote.required_votes,
                    )
            await self._stop_guessing()
            self.depart.set()
            return
        if not state.guessing_enabled:
            await self._stop_guessing()
        if event.name == EventName.LOBBY:
            self._queue_message(GREETING, 'greeting', 'Sent greeting to the lobby.')

        word = None
        correct = event.name == EventName.GUESSED and event.data['id'] == state.me
        if correct:
            word = event.data.get('word')
        elif state.phase == Phase.TURN_RESULT:
            word = state.word
        if word and state.turn_id not in self.observed_turns:
            counts = await self.store.observe(word)
            self.observed_turns.add(state.turn_id)
            self.index.observations = counts['words']
            self.logger.info(
                'Recorded %r: seen %d times, %d answers recorded in total.',
                word,
                counts['words'][word],
                counts['total_observations'],
            )
            if correct:
                self.logger.info('Correct! The answer was %r.', word, extra={'color': 'success'})

                async def statistics_message(answer=word):
                    latest = await self.store.counts()
                    return (
                        f'{answer}: seen {latest["words"].get(answer, 0)} times '
                        f'in {latest["total_observations"]} observed answers.'
                    )[:100]

                self._queue_message(
                    statistics_message, 'post-guess statistics', 'Sent post-guess statistics.'
                )
        if state.phase == Phase.TURN_RESULT and word and state.turn_id not in self.learned_turns:
            new_word = word not in self.index.words
            words = await self.store.learn(word)
            self.index = WordIndex(words, self.index.observations)
            self.learned_turns.add(state.turn_id)
            if new_word:
                self.logger.info(
                    'Learned %r (%d letters, %d words); database now has %d words.',
                    word,
                    sum(char.isalpha() for char in word),
                    len(word.split()),
                    len(self.index.words),
                    extra={'color': 'success'},
                )
        if event.name == EventName.CHAT:
            player = state.players.get(event.data['id'])
            if (
                state.guessing_enabled
                and player
                and not player.guessed
                and player.id != state.drawer_id
            ):
                matches = self.index.matching_message(event.data['msg'], state.hint or '')
                ruled_out = matches - self.attempted
                self.attempted.update(matches)
                if ruled_out:
                    self.logger.info(
                        '%s guessed %r; ruled out %r.',
                        player_label(state, player.id),
                        event.data['msg'],
                        sorted(ruled_out),
                    )
        if event.name == EventName.CLOSE_GUESS and state.guessing_enabled:
            guess = str(event.data)
            if guess not in self.close_guesses:
                self.close_guesses.append(guess)
                self.logger.info(
                    '%r was close. Adjusted the remaining guesses.',
                    guess,
                    extra={'color': 'warning'},
                )
            self.attempted.update(self.index.matching_message(guess, state.hint or ''))

        if state.guessing_enabled and state.hint and not self.depart.is_set():
            revision = (
                state.turn_id,
                state.hint,
                frozenset(self.attempted),
                tuple(self.close_guesses),
            )
            if revision != self._revision:
                await self._stop_guessing()
                self._revision = revision
                self._guess_task = create_task(
                    self._run_guessing(state.turn_id), name='guesser-turn'
                )

    async def close(self) -> None:
        try:
            await self._stop_guessing()
        finally:
            for message in self._messages:
                message.cancel()
            if self._handler is not None:
                self.client.off(EventName.ALL, self._handler)
                self._handler = None
            await self.client.close()

    def _queue_message(self, text, description, success):
        try:
            pending = self.client.enqueue_chat(text)
        except Exception:
            self.logger.exception('Could not queue %s.', description)
            raise

        def sent(future):
            if future.cancelled():
                return
            error = future.exception()
            if error is not None:
                self.logger.error('Could not send %s: %s', description, error)
            else:
                self.logger.info('%s', success)

        pending.add_done_callback(sent)
        self._messages.append(pending)

    async def _run_guessing(self, turn):
        try:
            await self._guess(turn)
        except Exception as error:
            self.logger.error('Guessing failed: %s', error)
            self.error = error
            self.depart.set()

    async def _stop_guessing(self):
        if self._guess_task is not None:
            self._guess_task.cancel()
            results = await gather(self._guess_task, return_exceptions=True)
            self._guess_task = None
            for result in results:
                if isinstance(result, Exception) and not isinstance(result, ActionError):
                    raise result

    async def _guess(self, turn):
        report_hint = not self.quiet_hints
        while self.client.snapshot.guessing_enabled and self.client.snapshot.turn_id == turn:
            state = self.client.snapshot
            candidates = self.index.candidates(state.hint or '', self.attempted, self.close_guesses)
            if report_hint:
                self.logger.info(
                    'Hint %r; close guesses %r; %d candidates remain.',
                    state.hint,
                    self.close_guesses,
                    len(candidates),
                )
                report_hint = False
            if not candidates:
                if not self._exhausted:
                    self._queue_message(
                        EXHAUSTED_MESSAGE,
                        'out-of-guesses message',
                        f'No guesses left for hint {state.hint!r}.',
                    )
                    self._exhausted = True
                return
            word = candidates[0]
            try:
                await self.client.send_guess(word)
            except ActionError:
                return
            self.attempted.add(word)
            self.logger.info('Guessed %r for hint %r (turn %d).', word, state.hint, turn)


async def _worker(options: Options, worker: int):
    store = WordStore(options.database, options.statistics or None)
    delay = DEFAULT_LOBBY_DELAY
    session_number = 0
    while True:
        session_number += 1
        logger = SessionLogger(worker, session_number)
        words, counts = await store.load()
        name = (
            f'{options.name} {worker}' if options.name and options.max_clients > 1 else options.name
        )
        client = Client(
            name=name,
            language=options.language,
            track_drawing=False,
            chat_interval=options.message_interval,
            events={
                EventName.LOBBY,
                EventName.PLAYER_JOIN,
                EventName.PLAYER_LEAVE,
                EventName.STATE,
                EventName.HINT,
                EventName.CHAT,
                EventName.GUESSED,
                EventName.CLOSE_GUESS,
                EventName.VOTE_KICK,
                EventName.SPAM,
                EventName.QUEUED_CHAT,
            },
        )
        session = GuesserSession(
            client, store, WordIndex(words, counts), logger, quiet_hints=options.max_clients > 1
        )
        watchers = []
        try:
            if options.lobby_id:
                logger.info('Connecting to lobby %r.', options.lobby_id)
            else:
                logger.info('Connecting to a public lobby.')
            await client.connect(options.lobby_id)
            delay = DEFAULT_LOBBY_DELAY
            watchers = [create_task(client.wait_closed()), create_task(session.depart.wait())]
            done, _ = await wait(watchers, return_when=FIRST_COMPLETED)
            for task in done:
                task.result()
            if session.error is not None:
                raise session.error
            if client.disconnect_reason in (1, 2):
                logger.warning(
                    'Disconnected by server (reason %d); stopping worker.', client.disconnect_reason
                )
                return
        except JoinRejected as error:
            logger.error('Join rejected: %s', error)
            if error.code in POLICY_REJECTIONS:
                logger.warning('Stopping worker after join rejection (code %d).', error.code)
                return
        except (OSError, TimeoutError, Disconnected) as error:
            logger.warning('Connection failed: %s', error)
            if client.disconnect_reason in (1, 2):
                logger.warning(
                    'Disconnected by server (reason %d); stopping worker.', client.disconnect_reason
                )
                return
        except Exception:
            logger.exception('Session failed.')
            raise
        finally:
            for task in watchers:
                task.cancel()
            await gather(*watchers, return_exceptions=True)
            await session.close()
        if not session.joined:
            delay = min(MAX_RETRY_DELAY, delay + 1)
        retry = options.lobby_delay if session.depart.is_set() else delay
        target = f'lobby {options.lobby_id!r}' if options.lobby_id else 'another public lobby'
        logger.warning('%s. Joining %s in %g seconds.', session.depart_reason, target, retry)
        await sleep(retry)


async def run_pool(options: Options) -> None:
    if type(options.max_clients) is not int or not 1 <= options.max_clients <= MAX_CLIENTS:
        raise ValueError(f'max-clients must be 1..{MAX_CLIENTS}')
    if any(
        not isfinite(value) or value < 1
        for value in (options.startup_delay, options.lobby_delay, options.message_interval)
    ):
        raise ValueError('Delays and message interval must be at least one second')
    longest_name = (
        f'{options.name} {options.max_clients}'
        if options.name and options.max_clients > 1
        else options.name
    )
    if len(longest_name) > 21:
        raise ValueError('Name including worker suffix must fit in 21 characters')
    # Fail before starting workers if the database cannot be loaded.
    store = WordStore(options.database, options.statistics or None)
    words, counts = await store.load()
    getLogger('guesser').info(
        'Loaded database %r: %d words. Statistics %r: %d recorded answers.',
        str(store.database),
        len(set(words)),
        str(store.statistics),
        sum(counts.values()),
    )
    async with TaskGroup() as group:
        for worker in range(1, options.max_clients + 1):
            group.create_task(_worker(options, worker))
            if worker < options.max_clients:
                await sleep(options.startup_delay)
