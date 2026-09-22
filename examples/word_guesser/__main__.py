"""Run with ``python -m examples.word_guesser run --database words.json``."""

from argly import App

raise SystemExit(App.discover('word-guesser', 'examples.word_guesser.commands').run())
