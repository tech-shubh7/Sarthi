"""Durable SQLite task queue, workers, scheduling and catch-up runs.

Architecture 2.2 and 2.18. Every pipeline step runs as a task so it can retry independently.
"""
