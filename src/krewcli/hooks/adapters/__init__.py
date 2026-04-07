"""Embedded adapter source files for agents that need an in-process plugin.

These files are written to disk by per-agent writers in
`krewcli/hooks/writers/`. They run inside the agent's own runtime
(JS for opencode, TS for amp/droid, py for gemini if needed) and
forward events to the krewcli bridge over HTTP.
"""
