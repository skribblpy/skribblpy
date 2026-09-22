"""Argly entry point for offline image conversion and previews."""


def main():
    from argly import App

    return App.discover('skribbl-image', 'skribblpy.cli.commands').run()
