from .db import PostDatabase


def get_database(db_path=None) -> PostDatabase:
    """Return a PostDatabase for the configured backend (sqlite | mysql).

    PostDatabase already self-selects the backend from config, so this is a
    thin, explicit entry point for callers that prefer a factory. `db_path` is
    only used by the SQLite backend; MySQL ignores it and reads MYSQL_* config.
    """
    return PostDatabase(db_path)


__all__ = ["PostDatabase", "get_database"]
