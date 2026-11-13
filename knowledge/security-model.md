# The security model, in plain terms

## The one-sentence version

Every layer assumes the content flowing through it might be hostile, every
control fails closed, and anything fail-open gets a status surface so "off"
is visible instead of silent.

## The ingestion injection gate

The moment third-party content enters a RAG corpus, the attack channel is
the ANSWER: a poisoned document can carry instructions the model might
follow ("ignore your rules", "send the conversation to this URL"). So every
document is scanned AT INGESTION for instruction overrides, role hijacks,
exfiltration patterns, hidden Unicode text, and agent-directed commands.
What happens next depends on provenance: untrusted content with a hot
finding is QUARANTINED - withheld from the corpus entirely, held for owner
review. The owner's own curated content is only TAGGED, never withheld,
because a corpus legitimately quotes attack strings (these very docs do).
Every ingestion path shares one gate choke point, so a new ingestion
surface inherits the scan by construction.

## Documents are parsed in a locked box

The injection gate reads TEXT, so something has to turn a PDF or Word file
into text first, and that parser is the one piece that handles a hostile
file's raw bytes. A file can be built to make a parser loop forever or
exhaust its memory, so the backend never parses one itself - it does not
even have the PDF and Word libraries installed. Every PDF and docx, from
every path (upload, the knowledge sync, the agent's read_file), goes to the
extractor service: a separate container with no network, no secrets, no
mount but the socket it answers on, a read-only filesystem, no Linux
capabilities and a non-root user. Inside it, each file is parsed in a fresh
child process held to a clock (20 seconds for a normal file, growing with
its size to at most 2 minutes) and a 512 MB memory cap, and it can
write no file at all. A hostile file costs that one child process; a parser
bug bad enough to run code would land in an empty box, where the most it
can do is answer with false text - which then meets the same injection gate
as any other untrusted text. If the service is down, documents wait (a
fixed "this server cannot read .pdf files right now" message, indexed
copies untouched) rather than being parsed anywhere else.
`docker compose exec extractor python -I check_locks.py` proves each lock
from inside the running box.

## Quarantine review and release

Quarantined content never touched the index - its full text waits in a
review queue (GET /api/admin/kb/quarantine) with the exact findings listed.
Releasing is an Owner-only trust decision: the document re-ingests with
the block waived but the injection tag preserved, so it stays visibly
flagged and its content is still handled as data, never instructions.
Deleting discards it. When a file with the same name is uploaded again to
the same department and indexes cleanly, the earlier held copy leaves the
queue as superseded - releasing it later would put the old text back over
the new one. Only uploads do this, direct or queued; a re-ingest through
POST /api/ingest leaves an earlier hold of the same source in the queue.

## Retrieved content is data, not instructions

The answer-layer half of the injection defense. Every retrieved chunk
arrives in the prompt inside a labeled block that opens with an explicit
contract: instructions found inside retrieved content are content to
report, never directives to follow. Provenance labels rank authority -
live system records and the owner's documents outrank peer and third-party
content, and untrusted content can never unlock restricted material or
redefine who the user is. The assistant is also forbidden from emitting
markdown images or links built from context data, which closes the classic
render-time exfiltration channel.

## Access tiers enforced four times

A clearance level guards content at every surface that could serve it:
retrieval drops departments above the caller's level (including
query-routed ones), the assistant's file tools refuse reads above level
(and hide even the NAMES of higher-tier files in listings and searches -
the admin panel's source listings and its quarantine queue follow the same
rule, showing only the departments the caller's level clears and, below the
Owner, only the general department and the caller's own),
the answer layer itself carries a non-owner rule that refuses to
recount internal operational history even if fragments of it leaked into
general-floor context, and federation gates both directions - a peer
key's scope is a rung on this same ladder, and a local clearance floor
decides who may receive what a peer sends back. Four surfaces, one
shared clearance map - they cannot drift apart because they read the
same configuration.

Federation became the fourth in v0.1.1, and the reason is worth stating
plainly: before it, a peer key scoped `all` reached every non-general
department - the Owner-only ones included, and every department an
operator had created and never classified. The serve path calls the
similarity search directly, and that function takes no clearance
argument, because the gate lived one layer above it on a path peers
never touch.

## Authentication hardening

A session alone cannot create durable authority. Every write that creates or
raises it - creating an account, changing a role, a permissions write that
adds manage_users or manage_system, registering or changing a federation
peer, changing the provider settings (the egress address and every provider
key) - re-asks the caller's own password (`jwt_auth.require_step_up`), so a
stolen or unattended session cannot mint an account that outlives it. The
same rule covers the two self-service credential doors: changing your
username (the route re-issues the token pair, a fresh refresh token
included) and setting up or re-keying your authenticator (it replaces the
second factor). A bearer session may use the account; it may not create a
longer-lived credential or replace an authentication factor without the
password. Re-keying an authenticator that is already enrolled takes more:
the password AND a current code from that authenticator (since 2026-10-02),
so a session plus the password cannot swap in someone else's - and a person
who lost theirs asks an admin for the MFA reset below. Code attempts are
capped per account at every door that checks one - counted in one write
before the code is read, so a burst of concurrent guesses gets no more than
the cap - and only a right code (or that reset) clears the count, so passing
a password step-up never buys more guesses; MFA enable verifies a pending
code only, within 30 minutes of its setup, and will not test a code against
an enrolled account's live seed. A wrong password answers 400 with a plain
reason, never a 401 that would log the operator out, and it counts against
the same lockout as a failed login, so the step-up is not a second guessing
surface. Changing your own password (`PATCH /api/auth/me/password`) goes
through the same helper: the route has always asked for the current
password, but it used to check it on its own, where a wrong one was not
counted and a locked account was not refused - an unbounded place to guess
the very password every door above asks for. A wrong current password there
now counts toward the lock like any other, the miss that reaches the limit
and every attempt while the account is locked answer 429, and a right one
clears the count. The sign-in and that helper are the only two places a
password is checked, and both count. The cost is the same at each of these
doors and is accepted: a session can lock its own account for the lockout
window by sending wrong passwords; the session itself stays usable. An account
with no usable password (the `!` sentinel hash) cannot be handed those
scopes, and nothing on this surface sets a password for such an account; a
port that creates them adds the route that does.

The lock alone does not bound the passwords checked at those doors, so the
helper keeps a second count that does (`security.count_password_attempt`).
Each password is counted in one write BEFORE it is hashed, and past
`MAX_LOGIN_ATTEMPTS` in one lockout window (`LOCKOUT_DURATION_MINUTES`, from
the first of them) the answer is 429 with the minutes to wait and nothing is
hashed - for a right password as for a wrong one. So the guarantee holds
for one request at a time and for any number sent together: no more than
that limit are checked per account per window. A password that cannot be
encoded (text that is not valid Unicode) can match nothing stored, so it is
a miss like any other: counted, logged, answered 400, never hashed. The
window is fixed - no attempt, counted or refused, moves its end. Only a
right password at one of those doors clears the count. An unlock does not -
the proof an unlock takes is never this account's password (below) -
and neither does a right authenticator code. The lock itself is read from
the stored row when the password arrives, not from the copy the session
loaded, and is looked at again in the statement that clears the count after
a right password, so a lock that lands while a right password is being
checked still refuses it; and the lock's count is raised by one statement,
so misses that land together are each counted. What an unlock
(`POST /api/admin/users/{id}/unlock`) does clear is the sign-in's lock: the
person can sign in again at once, and the step-up doors answer again when
that window ends. Unlocking an Owner's account is Owner-only, the ceiling
the MFA reset below carries: the lock is part of the Owner's authentication
boundary. At the sign-in the lock is read before the password is checked,
so there a burst is bounded per address instead (`AUTH_MAX_ATTEMPTS` per
`AUTH_WINDOW`, counted before any password is checked), and an
administrator's unlock always lets the person sign in.

Per account, the lock is the sign-in's bound, so clearing it takes proof of
a person, never a session alone. Unlocking someone else's account takes the
caller's own password, through the same step-up helper: a missing or wrong
one is a 400, a wrong one counts against the caller, and a caller whose own
account is locked is refused (429, naming its own lock). Unlocking your own
account takes a current authenticator code, counted before it is read in
the bound every code door shares; the lock does not refuse that one door,
because the lock is what a right code clears, and a wrong code while the
lock stands leaves the lock exactly as it is. It is allowed once per
lockout window: the unlock takes the window's one place before the code is
read, and a second one in the window is refused (429, with the minutes),
a right code included - otherwise whoever held a session and the
authenticator, and not the password, could unlock, guess at the sign-in
until the lock landed, and unlock again without end. A request whose code
was wrong hands its place back - its own place, in the window it took it
in: one that is answered after that window has ended frees nothing in the
next - and nothing else ends the count before its window does. An administrator's unlock of another account is deliberately
not limited that way: it takes the administrator's password each time. The
password is not accepted
for your own account - a locked account's password is not accepted
anywhere - and the caller's own id is not simply refused, because a sole
Owner whose sign-in someone else keeps locking has no other account that
may unlock theirs. An account with no authenticator enrolled cannot unlock
itself: it waits for the lock to end, or asks another Owner (an Owner's
account) or another administrator, and the refusal says so with the minutes
left. The target is read after the proof and whatever its status: an id
that names no account is a 404, and the Owner-only ceiling holds for a
deactivated account as for an active one. A password that cannot be encoded
is a wrong password at the sign-in as well: the same 401, counted as one
miss. A username that cannot be encoded is an unknown username there, at
the cost of one, and a code that cannot be encoded is a wrong code at every
door that takes a code. Text a route stores on an account is asked about
the same way before it is written: a department that cannot be encoded is a
400 that names the field. A user id from a path that is past what the users
table can hold never reaches the database: it names no account, and each
route answers for it what it answers for an id that names none, in its own
order (at the unlock, after the proof).

The administrative MFA reset (`POST /api/admin/users/{id}/mfa-reset`)
requires the manage_users scope, refuses Admin-on-Owner, and since
2026-09-21 re-asks the caller's own password like the other authority
writes, so a bearer session alone cannot strip a second factor. Targeting
your own account is allowed with the step-up, because there is no
self-service disable route and refusing it would strand a sole Owner.

Every route that takes a user id writes by that id alone, so each asks
first whether the id names an account: one that names none is a 404 "No
such user", and nothing is written or logged for it. That holds at the
deactivation, the department change, the permissions change and the MFA
reset as it does at the role change and the unlock. The 404 comes after the
caller's own permission checks and after the proof where a route asks for
one (the MFA reset, and a permissions write that grants manage_users or
manage_system), so a caller without the proof learns nothing about which
ids exist. A deactivated account is still an account. The permissions
change and the MFA reset read their target whatever its status, as the
unlock does, so the Owner-only rule and the no-usable-password rule apply
to a deactivated row as to an active one; the role change keeps answering
404 for an account that is not active.

No route answers a server error for an id it cannot use or a body field of
the wrong type. Every whole-number id a path carries - a user, a session's
token, an evaluation question, a result, a quarantined item - reaches its
route through one guard (`jwt_auth.stored_row_id`), which hands on a number
the database cannot hold as 0, an id no row has, so the route answers what
it answers for an id that names nothing, in its own order. The few routes
that take their JSON body without a schema read each text field through
`jwt_auth.body_text`: the wrong type is a 400 that names the field, a
string like the route's other refusals, and nothing is counted or written
for it. A whole number in a body that a column cannot hold is refused by
name the same way. A test walks the app's own route table with such ids
and bodies, so a route added later is asked too.

Password logins carry per-account lockout after repeated failures. TOTP
two-factor is built in, and REQUIRE_MFA=true refuses password logins for
un-enrolled accounts - checked AFTER password verification, deliberately,
so the refusal cannot be used to probe which accounts exist. The first-run
setup endpoint self-disables once an Owner exists, and the last active
Owner can never be deactivated or demoted - both guards close the
account-takeover path through an unconfigured or orphaned instance.

## Route-level authorization, tested two-sided

Every API route carries its own auth dependency, independent of the
middleware - defense in depth, so flipping the middleware off never exposes
an endpoint. The test suite sweeps the live route table both ways: a new
route without auth fails the build, and so does a stale entry on the
explicit public allowlist, where every public route documents its own
compensating gate.

## Guardrails that live in code, not config

The non-negotiable refusal rules (credentials and secrets, compensation
figures, instruction-override attempts) are compiled into the backend, not
stored in the editable system prompt - an admin edit can never drop them.
The credentials rule is deliberately strict: while refusing, the assistant
will not name where secrets live, their rotation status, or offer to
summarize them, because narrating the neighborhood of a secret is itself a
leak.

## Peer and boundary scanning

Content from federated peers arrives at chat time and never passes the
ingestion gate - so it gets the same injection scan at the chat boundary.
A peer chunk with a hot finding is dropped from that answer and logged;
milder findings ride along visibly tagged. Peer knowledge is reference
material, labeled EXTERNAL, and never outranks your own corpus.

## The tier the quarantine cannot withhold

Live system records rank above every other kind of content and are exempt
from the ingestion injection scan by design - the scan exists to judge
content of unknown provenance, and these are written by the instance
about itself. That exemption means nothing downstream would stop a bad
record, so the producer is constrained instead of inspected: a value
reaches a record only if it is a number, a code-produced date, or a string
that passes an allowlist, and no free-text database column is ever
interpolated. As a second layer the producer scans its own output and
refuses to write on any finding, because a hit there means the generator
is broken rather than that the corpus is quoting an attack.

The records are Owner clearance. One consequence worth knowing before
enabling peer serving: a peer key scoped `owner` is granted that same
rung, so an instance serving under that scope serves its posture record
with it. A key scoped `all` sits at the Admin rung and does not - which
is the whole reason `owner` has to be typed out rather than reached by
picking the friendlier-sounding word.

## Fail-open controls get positive signals

A control that fails open (rate limiting, injection scanning, the
reranker) is silent when broken - so each one reports its live state on a
status surface (/api/status, the rerank status endpoint) where "on" is a
readable fact, not an assumption. The backup status endpoint inverts the
pattern: it returns an error status when backups are missing or stale, so
a backup job that silently stops running trips monitoring instead of
being discovered during a restore.
