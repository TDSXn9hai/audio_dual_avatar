"""Logging helpers for the streaming application."""

from __future__ import annotations


def log(message: str) -> None:
    print(f"[stream] {message}", flush=True)
