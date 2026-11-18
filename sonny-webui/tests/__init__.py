"""Test suite for the Sonny (Open WebUI + Open Terminal) dual-PC kit.

Every test is pure Python 3.11+ standard library (``unittest``). HTTP endpoints
for Open WebUI v0.13.4, Open Terminal v0.17.20, and Ollama are mocked with real
in-process sockets, so no network access, Docker, Ollama, or Windows host is
required.

Run from the repository root::

    py -3 -m unittest discover -s sonny-webui/tests

Or run this directory directly, which also works on Windows::

    py -3 sonny-webui/tests
"""
