"""Fetch chunk metadata and bodies, then commit them idempotently.

History is ingested before live events. This module does not advance the clock.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from controller.api.client import SimulatorClient
from controller.persistence.repositories import ChunkCommit, Store

log = logging.getLogger("controller.ingest")


@dataclass
class KindStats:
    kind: str
    chunks_fetched: int = 0
    chunks_committed: int = 0
    inserted: int = 0
    duplicates: int = 0
    issues: int = 0
    commits: list[ChunkCommit] = field(default_factory=list)


def ingest_available(client: SimulatorClient, store: Store) -> list[KindStats]:
    """Ingest every history chunk, then every live chunk, that is not yet committed."""
    return [ingest_kind(client, store, "history"), ingest_kind(client, store, "events")]


def ingest_kind(client: SimulatorClient, store: Store, kind: str) -> KindStats:
    checkpoint = store.checkpoint(kind)
    after = checkpoint.last_chunk if checkpoint is not None else -1
    metas = sorted(client.chunks(kind, after=after), key=lambda item: item.chunk)
    stats = KindStats(kind=kind, chunks_fetched=len(metas))
    for meta in metas:
        if meta.kind != kind:
            raise ValueError(f"chunk {meta.chunk} has kind {meta.kind}, expected {kind}")
        commit = store.commit_chunk(meta, "" if store.is_committed(meta.chunk) else client.chunk_text(meta.chunk))
        stats.commits.append(commit)
        if commit.already_committed:
            continue
        stats.chunks_committed += 1
        stats.inserted += commit.inserted
        stats.duplicates += commit.duplicates
        stats.issues += commit.issues
        log.info(
            "chunk committed kind=%s chunk=%s inserted=%s duplicates=%s issues=%s",
            kind,
            meta.chunk,
            commit.inserted,
            commit.duplicates,
            commit.issues,
        )
    log.info(
        "ingest kind=%s fetched=%s committed=%s inserted=%s duplicates=%s issues=%s",
        kind,
        stats.chunks_fetched,
        stats.chunks_committed,
        stats.inserted,
        stats.duplicates,
        stats.issues,
    )
    return stats
