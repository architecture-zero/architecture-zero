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

The filter rewrites a record's arguments before any handler formats them,
by one rule per logger:

  - `uvicorn.access`: EVERY str argument is cut at its first "?". The target
    a client sends need not start with "/" - an absolute-form target
    (`GET http://host/p?q=...`), one that starts with "?", one with no slash,
    `OPTIONS *?q=...` - and each of those kept its query under a rule that
    looked for a leading "/" (the security read of 2026-11-16 drove all of
    them through a real uvicorn). Cutting every argument is safe because
    none of the others can hold a "?": the client is host:port, the method
    and the version are HTTP tokens, the status is a number - and uvicorn
    percent-encodes the path (protocols/utils.py, get_path_with_query_string,
    `urllib.parse.quote(scope["path"])`, whose safe set is "/"), so the first
    literal "?" in the target is always the query's separator.
  - `uvicorn.error`: a str argument that starts with "/" is cut at its first
    "?", and so is the target of a WebSocket handshake line - all three of
    those open with '%s - "WebSocket %s"' (protocols/websockets/
    websockets_impl.py and wsproto_impl.py), the target second, whatever its
    first character. Any other argument on this logger - an error's own
    words - keeps its "?".

The client, method, path, version and status stay as they were, so the line
still says who asked for what and how it went. The filter never drops a
record and never raises: a record whose arguments are not a tuple is left
alone (uvicorn passes a tuple on every line this is for), and anything the
rewrite cannot read leaves the record as it came.

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

# The loggers whose lines carry a request's target: the access line, and the
# WebSocket handshake lines.
LOGGERS = ("uvicorn.access", "uvicorn.error")

# The logger whose every argument is cut (see the module's note).
_EVERY_ARGUMENT = "uvicorn.access"

# How every WebSocket handshake line opens; its second argument is the target.
_WEBSOCKET_LINE = '%s - "WebSocket %s"'

# The mark install() looks for, as an attribute rather than a class check, so
# a reload of this module (a new class object) still finds the filter it
# installed before.
_MARK = "_drops_query_strings"


def _before_query(value):
    """A str with its first "?" and everything after it cut off; any other
    value as it came."""
    if isinstance(value, str) and "?" in value:
        return value.split("?", 1)[0]
    return value


def _without_query(value):
    """A path-shaped str (it starts with "/") cut at its first "?"; any other
    value as it came."""
    if isinstance(value, str) and value.startswith("/"):
        return _before_query(value)
    return value


class DropQueryStrings(logging.Filter):
    """Cuts the query string off a record's target. `every_argument` is the
    access logger's rule: every str argument is cut at its first "?".
    Without it, a path-shaped argument and a WebSocket handshake line's
    target are. It never drops a record: the line is kept, without what
    followed the "?"."""

    _drops_query_strings = True

    def __init__(self, every_argument: bool = False):
        super().__init__()
        self.every_argument = every_argument

    def filter(self, record):
        try:
            args = record.args
            if isinstance(args, tuple) and args:
                if self.every_argument:
                    args = tuple(_before_query(a) for a in args)
                else:
                    args = tuple(_without_query(a) for a in args)
                    msg = record.msg
                    if (isinstance(msg, str) and msg.startswith(_WEBSOCKET_LINE)
                            and len(args) > 1):
                        args = args[:1] + (_before_query(args[1]),) + args[2:]
                record.args = args
        except Exception:
            # A record this cannot read is kept as it came: a log line lost
            # to the filter would be worse than one it could not shorten.
            pass
        return True


def install():
    """Put one DropQueryStrings on each logger in LOGGERS - the access
    logger's cutting every argument - and leave a logger that already
    carries one as it is."""
    for name in LOGGERS:
        logger = logging.getLogger(name)
        if not any(getattr(f, _MARK, False) for f in logger.filters):
            logger.addFilter(DropQueryStrings(every_argument=(name == _EVERY_ARGUMENT)))
