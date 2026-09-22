"""Argly commands for the word guesser example."""

from asyncio import run
from typing import Annotated
from argly import Option, command
from logging import INFO, getLogger, basicConfig
from examples.word_guesser.session import Options, run_pool, DEFAULT_DATABASE


@command('run', summary='Run a concurrent word guesser using a JSON word database.')
def guesser(
    *,
    database: Annotated[str, Option('-d')] = DEFAULT_DATABASE,
    statistics: Annotated[str, Option()] = '',
    name: Annotated[str, Option()] = '',
    language: Annotated[str, Option()] = '0',
    max_clients: Annotated[int, Option()] = 2,
    startup_delay: Annotated[float, Option()] = 10.0,
    lobby_delay: Annotated[float, Option()] = 10.0,
    message_interval: Annotated[float, Option()] = 1.0,
    lobby_id: Annotated[str, Option()] = '',
) -> int:
    basicConfig(level=INFO, format='%(asctime)s %(name)s %(message)s')
    options = Options(
        database=database,
        statistics=statistics,
        name=name,
        language=language,
        max_clients=max_clients,
        startup_delay=startup_delay,
        lobby_delay=lobby_delay,
        message_interval=message_interval,
        lobby_id=lobby_id,
    )
    try:
        run(run_pool(options))
    except KeyboardInterrupt:
        return 130
    except (ValueError, OSError) as error:
        getLogger('guesser').error('%s', error)
        return 2
    return 0
