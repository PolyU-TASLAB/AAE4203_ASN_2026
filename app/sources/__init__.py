"""Where submissions come from.

Two interchangeable back-ends, selected by ``[sources] kind`` in config.toml:

``local_mirror``
    A folder kept in sync by Google Drive for Desktop.  No credentials, no
    network code, works even if the internet drops (the mirror simply stops
    updating).

``gdrive_api``
    The Drive REST API v3 with a service account.  Use this when the server is
    a campus VM that cannot run the Drive desktop client.

Both produce the same :class:`SubmissionRef` objects, so nothing downstream
knows or cares which one is active.
"""

from .base import Source, SubmissionRef, SourceError, make_source, parse_filename

__all__ = ["Source", "SubmissionRef", "SourceError", "make_source", "parse_filename"]
