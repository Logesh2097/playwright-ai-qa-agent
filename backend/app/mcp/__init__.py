"""Model Context Protocol client.

This package only speaks the protocol: connect, discover, call, close. It knows
nothing about browsers, permissions, projects or the database. What a tool may
be used for is decided in `app.browser`.
"""
