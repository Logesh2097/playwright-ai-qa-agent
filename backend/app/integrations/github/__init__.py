"""GitHub: the official REST API, webhooks, test selection and pull request reporting.

    client.py     the few REST calls the platform makes, and how it authenticates
    webhooks.py   signature verification and reading events
    selection.py  which suites a set of changed files concerns (rules, no model)
    reporting.py  commit status and pull request comment from a test report

A repository is only ever addressed if a project has linked it (`GithubRepository`).
"""
