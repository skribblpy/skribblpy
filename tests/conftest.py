"""Local protocol fixtures. Tests never contact the game service."""

from pytest import fixture


@fixture
def lobby():
    return {
        'id': 'ROOM',
        'type': 0,
        'me': 1,
        'owner': -1,
        'round': 0,
        'settings': [0, 8, 80, 3, 3, 2, 0, 0],
        'users': [
            {'id': 1, 'name': 'Python', 'avatar': [0, 0, 0, -1]},
            {'id': 2, 'name': 'Drawer', 'avatar': [0, 0, 0, -1]},
        ],
        'state': {'id': 4, 'time': 60, 'data': {'id': 2, 'word': [3, 3], 'hints': []}},
    }
