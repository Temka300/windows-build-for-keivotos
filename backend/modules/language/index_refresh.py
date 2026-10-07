"""Best-effort derived indexing after an acknowledged original-file mutation."""
from __future__ import annotations

import logging
from pathlib import Path

from files_base import sources
from modules.language import documents

logger = logging.getLogger("keivotos.language")
WARNING = "The file change is saved; the Files listing needs refresh."


def attempt(source: sources.Source, db_path: Path, name: str,
            previous_names: tuple[str, ...] | list[str] = ()) -> dict:
    """Retry only index facts; old names may have been recreated externally."""
    try:
        for current in dict.fromkeys((name, *previous_names)):
            documents.refresh_index(source, db_path, current)
    except Exception as error:
        # The caller has already committed/preserved the original. No index
        # failure may turn that successful mutation into a retryable file write.
        logger.warning("Files refresh failed after Language mutation: source=%r note=%r error=%s: %s",
                       source.source_id, name, type(error).__name__, error)
        return {"status": "failed", "warning": WARNING}
    return {"status": "complete", "warning": ""}
