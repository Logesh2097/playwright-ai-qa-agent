"""The deterministic test engine.

It executes scripts (`app.testing.automation`) with Playwright and reports what
happened, step by step. No model is involved anywhere in this package.

    protocol.py    what the worker sends (a plan) and gets back (events)
    browser.py     launching browsers and creating isolated contexts
    locators.py    turning a locator description into one element
    actions.py     the steps that do something
    assertions.py  the steps that check something
    evidence.py    screenshots, video, traces
    runner.py      running the cases of a plan: login, steps, cleanup, retries
    child.py       one run in its own process
    server.py      the engine service: starts runs, serves their evidence
    client.py      the worker's side of that service

The engine runs in its own container. Only `protocol.py` and `client.py` are
used by the rest of the backend.
"""
