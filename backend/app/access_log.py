"""uvicorn's access log keeps a request's path and drops its query string
(2026-11-16).

uvicorn writes one line per request through the `uvicorn.access` logger:
the client, the method, the path WITH its query string, the HTTP version and
the status (protocols/http/h11_impl.py, the protocol the image's command
names). A query string can carry what no log should keep - a question a peer
was asked by URL, an authorization code handed back to a sign-in callback -
and the log keeps it for as long as the log is kept, on every host that
collects it. Sensitive data rides in a body or a header, never in a URL
(OWASP ASVS 8.3.1); this is the log's half of that rule, for the requests
that still put something in one.

The filter rewrites a record's arguments before any handler formats them: a
str argument that starts with "/" and holds a "?" keeps only what comes
before the "?". The client, method, path, version and status stay as they
were, so the line still says who asked for what and how it went. A record
whose arguments are not a tuple is left alone - uvicorn passes a tuple on
every line this is for.

The WebSocket handshake lines name the same path and query string through
the `uvicorn.error` logger (protocols/websockets/websockets_impl.py and
wsproto_impl.py), so the filter sits on that logger too. A line on it whose
arguments hold no path is untouched.

WHY INSTALLING AT IMPORT WORKS (read in uvicorn 0.32.1, the version every
requirements.txt pins). Config.__init__ runs configure_logging() - the
dictConfig that gives `uvicorn.access` its handler - and the app module is
imported later, by Config.load() inside Server._serve. With --workers each
child process runs configure_logging() again and then imports the app
(uvicorn/_subprocess.py). So when app.main imports this module the loggers
are already configured, and nothing reconfigures them after. A dictConfig
that did run later would still keep the filter: it replaces a logger's
handlers and leaves its filters (logging.config's common_logger_config).

install() is idempotent: a second call - a second import of app.main in one
process, a reload of this module - finds the mark and adds nothing.
"""
import logging

# The loggers whose lines carry a request's path: the access line, and the
# WebSocket handshake lines.
LOGGERS = ("uvicorn.access", "uvicorn.error")

# The mark install() looks for, as an attribute rather than a class check, so
# a reload of this module (a new class object) still finds the filter it
# installed before.
_MARK = "_drops_query_strings"


def _without_query(value):
    """A path-shaped str with the "?" and everything after it cut off; any
    other value as it came."""
    if isinstance(value, str) and value.startswith("/") and "?" in value:
        return value.split("?", 1)[0]
    return value


class DropQueryStrings(logging.Filter):
    """Cuts the query string off every path-shaped argument of a record. It
    never drops a record: the line is kept, without what followed the "?"."""

    _drops_query_strings = True

    def filter(self, record):
        args = record.args
        if isinstance(args, tuple) and args:
            record.args = tuple(_without_query(a) for a in args)
        return True


def install():
    """Put one DropQueryStrings on each logger in LOGGERS; a logger that
    already carries one is left as it is."""
    for name in LOGGERS:
        logger = logging.getLogger(name)
        if not any(getattr(f, _MARK, False) for f in logger.filters):
            logger.addFilter(DropQueryStrings())
