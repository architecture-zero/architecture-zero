"""No route answers a server error for an id it cannot use or a body field of
the wrong type.

This file walks the app's own route table, so a route added later is asked
too.

IDS. A route's path takes any whole number and a table keeps 64 bits: a
number past that reached the database and raised. Such a number names no
row, so every route that takes a whole-number id in its path takes it
through one guard (jwt_auth.stored_row_id, and stored_user_id for a user
id), which hands it on as 0 - an id no row has - and the route answers
exactly what it answers for an id that names nothing, in the same order
relative to its permission and proof checks. A text id is asked about as
well: a very long one, one holding a NUL, one made of bytes that are not
valid text (a path is decoded with replacement, so no text id can arrive
that cannot be encoded).

BODIES. A route that takes its JSON body as a plain object has no schema, so
nothing checked a field's type before the handler read it: a number where
text was expected raised. Each field such a route reads is refused by name
with a string 400 when it holds the wrong type - never a server error, and
never the list a schema's 422 carries. A route with a schema refuses a field
of the wrong type itself (422) before its handler runs; that is walked too.
A whole number in a body can be past what a column keeps: each such field
is named here with what the route answers for it.

FIX (each fails on the routes as they were):
  - a whole-number id past the range, at every route that is not a user
    route (those were already guarded), is answered like an id that names
    nothing
  - no route declares a whole-number id from its path as a bare int
  - a field of the wrong type in a body with no schema is a string 400
    that names the field: the role change, the department change, the
    permissions change, the config write (which stored a list, an object
    or null as its printed form)
  - null where text is expected in a body with no schema is the field left
    out: a null department raised
  - a whole number past the range in a body: an evaluation question's
    level, a result's score, a feedback's turn number, the ids a run names
CONTROL (true before and after, by design):
  - a text id that is very long, holds a NUL or is not valid text is
    answered like one that names nothing
  - a field of the wrong type is refused by the schema before the handler
    runs, at every route that has one
  - a whole-number id past the range at the user routes, which were
    already guarded
  - every body with no schema, and every loose or whole-number field of a
    schema, is named in this file

NOT WALKED, by name, with the reason (see _NOT_WALKED below).

Every case drives the app's own routes through the suite's test client, on
throwaway accounts of its own.
"""
import ast
import inspect
import itertools
import json
import re
import textwrap
import typing

import pytest
from fastapi.dependencies.utils import get_flat_dependant
from fastapi.routing import APIRoute
from pydantic import BaseModel, ValidationError

from app import jwt_auth, state_store
from app.jwt_auth import create_access_token, hash_password
from app.main import app
from app.users import create_user, deactivate_user, get_user_by_id_any_status, unlock_user

PW = "OldPassw0rd!x"
PAST_THE_RANGE = (2 ** 63, 2 ** 70, -(2 ** 63) - 1)
NAMES_NOTHING = 987_654_321_012
TEXT_NAMES_NOTHING = "names-nothing-u7"
ODD_TEXT_IDS = {"very long": "x" * 5000, "a NUL": "a%00b", "not valid text": "%ED%A0%80"}

_names = itertools.count(1)


def _account(role):
    name = f"r500_{next(_names)}"
    uid = create_user(name, hash_password(PW), role=role)
    token = create_access_token(uid, name, role)
    return {"id": uid, "name": name, "role": role,
            "headers": {"Authorization": f"Bearer {token}"}}


@pytest.fixture
def accounts():
    """Makes throwaway accounts by role and retires each when the test ends."""
    made = []

    def make(role="member"):
        made.append(_account(role))
        return made[-1]

    yield make
    for account in made:
        unlock_user(account["id"])
        deactivate_user(account["id"])


def _send(client, method, path, headers, body=None):
    """One request. Returns (status, body), or ("error", name) if the route
    raised - which a deployed server answers as a 500."""
    assert "{" not in path, f"a path parameter was left unfilled: {path}"
    try:
        if body is None:
            r = client.request(method, path, headers=headers)
        else:
            r = client.request(method, path, content=json.dumps(body),
                               headers={**headers, "Content-Type": "application/json"})
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, r.text[:200]
    except Exception as exc:
        return "error", type(exc).__name__


def _is_server_error(answer):
    return answer[0] == "error" or answer[0] >= 500


# -- The route table -----------------------------------------------------------

def _api_routes():
    return sorted(((method, route.path, route) for route in app.routes
                   if isinstance(route, APIRoute) for method in sorted(route.methods)),
                  key=lambda r: (r[1], r[0]))


def _path_ids(route):
    """{name in the path: type} for every parameter the route's path
    carries, through its dependencies too. By ALIAS: a guard's own parameter
    has a name of its own and carries the path's name as its alias."""
    return {p.alias: p.type_ for p in get_flat_dependant(route.dependant).path_params}


def _fill(path, values):
    for name, value in values.items():
        path = re.sub(r"\{" + re.escape(name) + r"(:\w+)?\}", str(value), path)
    return path


def _schema(route):
    """("none", None), ("schema", Model), ("object", None) for a body taken
    as a plain object, or ("other", names) for anything else (a form, a
    file)."""
    params = get_flat_dependant(route.dependant).body_params
    if not params:
        return "none", None
    if len(params) == 1:
        declared = params[0].type_
        for kind in (declared,) + typing.get_args(declared):
            if isinstance(kind, type) and issubclass(kind, BaseModel):
                return "schema", kind
        if declared is dict:
            return "object", None
    return "other", [p.name for p in params]


def _filled(model):
    """The smallest body the schema takes: a value for each required field,
    and the caller's own password where the schema has that field, so a route
    that asks for the proof gets past it."""
    body = {}
    for name, field in model.model_fields.items():
        if name == "current_password":
            body[name] = PW
        elif field.is_required():
            kind = typing.get_origin(field.annotation) or field.annotation
            body[name] = {int: 1, float: 1.0, bool: False, list: [], dict: {}}.get(kind, "probe")
    return body


# What a body with no schema carries so that every proof passes and every
# field a route on this surface reads has a value of the right type.
_PASSES = {"current_password": PW, "role": "admin", "department": "general",
           "permissions": ["chat"]}


def _passing_body(route):
    kind, model = _schema(route)
    if kind == "schema":
        return _filled(model)
    if kind == "object":
        return dict(_PASSES)
    return None


_ROUTES = _api_routes()
_WHOLE_NUMBER_ID_ROUTES = [(m, p) for m, p, r in _ROUTES if int in _path_ids(r).values()]
_TEXT_ID_ROUTES = [(m, p) for m, p, r in _ROUTES
                   if _path_ids(r) and int not in _path_ids(r).values()]
_SCHEMA_ROUTES = [(m, p) for m, p, r in _ROUTES if _schema(r)[0] == "schema"]
_OBJECT_ROUTES = [(m, p) for m, p, r in _ROUTES if _schema(r)[0] == "object"]


def _route(method, path):
    return next(r for m, p, r in _ROUTES if (m, p) == (method, path))


def _label(pairs):
    return [f"{m} {p}" for m, p in pairs]


# -- FIX: a whole-number id past the range --------------------------------------

@pytest.mark.parametrize("method,path", _WHOLE_NUMBER_ID_ROUTES,
                         ids=_label(_WHOLE_NUMBER_ID_ROUTES))
def test_a_whole_number_id_past_the_range_is_answered_like_one_that_names_nothing(
        client, accounts, method, path):
    """As the top tier with a body that passes the route's proof, as the top
    tier with the proof left out, and as an account with no authority: an
    id past the range gets, each time, exactly the answer an id that names
    nothing gets. So the route's order is unchanged, and a caller without
    the proof learns nothing from sending one."""
    route = _route(method, path)
    top, low = accounts("owner"), accounts("member")
    body = _passing_body(route)
    without_proof = None if body is None else {k: v for k, v in body.items()
                                               if k != "current_password"}
    ids = _path_ids(route)

    def ask(number, headers, sent):
        values = {name: (number if kind is int else TEXT_NAMES_NOTHING)
                  for name, kind in ids.items()}
        return _send(client, method, _fill(path, values), headers, sent)

    for headers, sent in ((top["headers"], body), (top["headers"], without_proof),
                          (low["headers"], body)):
        expected = ask(NAMES_NOTHING, headers, sent)
        assert not _is_server_error(expected), (method, path, expected)
        # Never the path's own refusal: the number reached the route.
        assert expected[0] != 422, (method, path, expected)
        for number in PAST_THE_RANGE + (0, -1):
            answer = ask(number, headers, sent)
            assert answer == expected, (method, path, number, answer, expected)


def test_no_route_takes_a_whole_number_id_from_its_path_as_a_bare_int():
    """Read from the app's route table: every whole-number parameter a path
    carries reaches its route through a guard - stored_user_id, or a
    stored_row_id made for that name - never declared on the route itself.
    A new route that takes one as a bare int fails here."""
    seen, bare = 0, []
    for method, path, route in _ROUTES:
        for param in route.dependant.path_params:
            if param.type_ is int:
                bare.append((method, path, param.name))
        for sub in route.dependant.dependencies:
            for param in sub.path_params:
                if param.type_ is not int:
                    continue
                seen += 1
                # By module and name, not identity: other files of this
                # suite reload app.jwt_auth, and the routes keep the function
                # they were built with.
                guarded = (getattr(sub.call, "__module__", "") == jwt_auth.__name__
                           and (getattr(sub.call, "__name__", "") == "stored_user_id"
                                or getattr(sub.call, "guards_path_id", None) == param.alias))
                if not guarded:
                    bare.append((method, path, param.name))
    assert seen >= 12 and bare == [], (seen, bare)
    assert [jwt_auth.row_id(n) for n in (0, -7, 2 ** 63, 2 ** 70)] == [0, 0, 0, 0]
    assert jwt_auth.row_id(7) == 7 and jwt_auth.row_id(2 ** 63 - 1) == 2 ** 63 - 1


# -- CONTROL: a text id --------------------------------------------------------

@pytest.mark.parametrize("method,path", _TEXT_ID_ROUTES, ids=_label(_TEXT_ID_ROUTES))
def test_control_a_text_id_no_route_can_use_is_answered_like_one_that_names_nothing(
        client, accounts, method, path):
    """A very long id, one holding a NUL and one made of bytes that are not
    valid text: none raises, and each gets the status an id that names
    nothing gets."""
    route = _route(method, path)
    top = accounts("owner")
    body = _passing_body(route)
    expected = _send(client, method, _fill(path, {n: TEXT_NAMES_NOTHING for n in _path_ids(route)}),
                     top["headers"], body)
    assert not _is_server_error(expected), (method, path, expected)
    for what, text in ODD_TEXT_IDS.items():
        answer = _send(client, method, _fill(path, {n: text for n in _path_ids(route)}),
                       top["headers"], body)
        assert answer[0] == expected[0], (method, path, what, answer, expected)


# -- CONTROL: a schema refuses a field of the wrong type ------------------------

_WRONG = (12345, 1.5, True, None, [], {}, ["a"], [1], [None], [[]], {"a": 1}, "text")


def _schema_takes(model, body):
    try:
        model.model_validate(body)
        return True
    except ValidationError:
        return False


@pytest.mark.parametrize("method,path", _SCHEMA_ROUTES, ids=_label(_SCHEMA_ROUTES))
def test_control_a_schema_refuses_a_field_of_the_wrong_type_before_the_handler_runs(
        client, accounts, method, path):
    """Each field of the route's schema in turn, holding each of a dozen
    values, in a body that is otherwise the smallest the schema takes. A
    value the schema refuses for that field is answered 422 and the handler
    never runs. (A value the schema itself takes for the field - true for a
    whole number, say - is that field's type as far as the route is
    concerned and is not sent: the handler would run for real.)"""
    route = _route(method, path)
    _, model = _schema(route)
    top = accounts("owner")
    url = _fill(path, {n: (NAMES_NOTHING if k is int else TEXT_NAMES_NOTHING)
                       for n, k in _path_ids(route).items()})
    sent = 0
    for field in model.model_fields:
        for wrong in _WRONG:
            body = {**_filled(model), field: wrong}
            if _schema_takes(model, body):
                continue
            sent += 1
            answer = _send(client, method, url, top["headers"], body)
            assert answer[0] == 422, (method, path, field, wrong, answer)
    assert sent >= len(model.model_fields), (method, path, sent)


# -- FIX: a body with no schema ------------------------------------------------

_NOT_TEXT = (12345, 1.5, True, [], {}, ["a"], {"a": 1})
_NOT_A_SCOPE_LIST = (12345, 1.5, True, "chat", "", {}, {"a": 1}, [1], [None], [[]], [{}],
                     ["chat", 7])
_NOT_A_ROLE = (12345, 1.5, True, None, [], {}, ["admin"], {"a": 1}, "nobody")
_NOT_A_CONFIG_VALUE = (None, [1], {"a": 1})


def _config_keys():
    """The keys the config write accepts, read from the set its handler
    names `allowed`."""
    from app.routers import admin
    tree = ast.parse(textwrap.dedent(inspect.getsource(admin.admin_set_config)))
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "allowed"
                and isinstance(node.value, ast.Set)):
            return sorted(e.value for e in node.value.elts)
    raise AssertionError("the config write no longer names its keys in a set called allowed")


# For each route that takes its body as a plain object: the fields it reads,
# and for each the values that are the wrong type for it. `base` is what the
# rest of the body carries so the request reaches that field.
_NO_SCHEMA = {
    ("PATCH", "/api/users/{user_id}/role"): {
        "current_password": (_NOT_TEXT, {"role": "admin"}),
        "role": (_NOT_A_ROLE, {"current_password": PW}),
    },
    ("PATCH", "/api/users/{user_id}/department"): {
        "department": (_NOT_TEXT, {}),
    },
    ("PATCH", "/api/users/{user_id}/permissions"): {
        "permissions": (_NOT_A_SCOPE_LIST, {"current_password": PW}),
        "current_password": (_NOT_TEXT, {"permissions": ["chat", "manage_users"]}),
    },
    ("PATCH", "/api/admin/config"): {key: (_NOT_A_CONFIG_VALUE, {}) for key in _config_keys()},
}


def _fields_read(route):
    """Every key the handler reads from `body`, from its source: body.get("k"),
    body["k"] and "k" in body."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(route.endpoint)))
    read = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get" and getattr(node.func.value, "id", "") == "body"
                and node.args and isinstance(node.args[0], ast.Constant)):
            read.add(node.args[0].value)
        elif (isinstance(node, ast.Call) and getattr(node.func, "id", "") == "body_text"
                and len(node.args) > 1 and isinstance(node.args[1], ast.Constant)):
            read.add(node.args[1].value)
        elif (isinstance(node, ast.Subscript) and getattr(node.value, "id", "") == "body"
                and isinstance(node.slice, ast.Constant)):
            read.add(node.slice.value)
        elif (isinstance(node, ast.Compare) and isinstance(node.left, ast.Constant)
                and any(isinstance(op, ast.In) for op in node.ops)
                and any(getattr(c, "id", "") == "body" for c in node.comparators)):
            read.add(node.left.value)
    return read


def test_control_every_body_with_no_schema_is_named_here_with_every_field_it_reads():
    """Found from the route table and the handlers' source: a new route that
    takes its body as a plain object, or a new field one of these reads,
    fails here until it is named above with the values that are wrong for
    it."""
    assert sorted(_OBJECT_ROUTES) == sorted(_NO_SCHEMA), (_OBJECT_ROUTES, sorted(_NO_SCHEMA))
    for (method, path), fields in _NO_SCHEMA.items():
        unnamed = _fields_read(_route(method, path)) - set(fields)
        assert unnamed == set(), (method, path, unnamed)


_NO_SCHEMA_CASES = [(m, p, field) for (m, p), fields in _NO_SCHEMA.items() for field in fields]


@pytest.mark.parametrize("method,path,field", _NO_SCHEMA_CASES,
                         ids=[f"{m} {p} {f}" for m, p, f in _NO_SCHEMA_CASES])
def test_a_field_of_the_wrong_type_in_a_body_with_no_schema_is_a_400_that_names_it(
        client, accounts, method, path, field):
    """On an account that exists, as the top tier: each wrong value for the
    field is a 400 whose detail is a string that names the field. Nothing is
    written, and a current_password of the wrong type is not counted as a
    password attempt."""
    top, someone = accounts("owner"), accounts("member")
    url = _fill(path, {"user_id": someone["id"]})
    before = get_user_by_id_any_status(someone["id"])
    wrong_values, base = _NO_SCHEMA[(method, path)][field]
    for wrong in wrong_values:
        answer = _send(client, method, url, top["headers"], {**base, field: wrong})
        assert answer[0] == 400, (field, wrong, answer)
        detail = answer[1].get("detail")
        assert isinstance(detail, str) and field in detail, (field, wrong, answer)
    assert get_user_by_id_any_status(someone["id"]) == before
    assert state_store.get(f"pw_fail:{top['id']}") is None
    assert get_user_by_id_any_status(top["id"])["failed_attempts"] == 0


def test_null_where_text_is_expected_is_the_field_left_out(client, accounts):
    """A null current_password is a missing one, as it was (the step-up's
    own 400). A null department raised; it is the field left out too, so the
    default one."""
    top, someone = accounts("owner"), accounts("member")
    answer = _send(client, "PATCH", f"/api/users/{someone['id']}/role", top["headers"],
                   {"role": "admin", "current_password": None})
    assert answer == (400, {"detail": "Your current password is required to change a role"}), answer
    answer = _send(client, "PATCH", f"/api/users/{someone['id']}/department", top["headers"],
                   {"department": None})
    assert answer == (200, {"status": "updated"}), answer
    assert get_user_by_id_any_status(someone["id"])["department"] == "general"
    assert jwt_auth.body_text({"a": "b"}, "a") == "b"
    assert jwt_auth.body_text({}, "a", "c") == "c" and jwt_auth.body_text({"a": None}, "a") == ""


def test_the_config_write_stores_a_number_or_true_false_as_its_text_as_before(client, accounts):
    """What the config write still takes for a key that holds text: a string,
    and a number or true/false stored as its text. Only a list, an object and
    null are refused (above); they were stored as their printed form."""
    from app.config import get_config, set_config
    top = accounts("owner")
    kept = get_config("instance_name", "")
    try:
        answer = _send(client, "PATCH", "/api/admin/config", top["headers"], {"instance_name": 42})
        assert answer[0] == 200 and get_config("instance_name", "") == "42", answer
        answer = _send(client, "PATCH", "/api/admin/config", top["headers"],
                       {"instance_name": None})
        assert answer == (400, {"detail": "instance_name must be text, not a list, an "
                                          "object or null"}), answer
        assert get_config("instance_name", "") == "42"
    finally:
        set_config("instance_name", kept)


# -- FIX: a whole number in a body, past the range ------------------------------

BIG = 2 ** 70


def _whole_number_and_loose_fields():
    """Every field of every body schema that holds a whole number, a list or
    an object: (method, path, field)."""
    found = set()
    for method, path, route in _ROUTES:
        kind, model = _schema(route)
        if kind != "schema":
            continue
        for name, field in model.model_fields.items():
            declared = str(field.annotation)
            if any(word in declared for word in ("int", "list", "dict", "List", "Dict", "Any")):
                found.add((method, path, name))
    return found


# Each of them, with what this file does about it.
_NUMBERS_AND_LOOSE = {
    ("POST", "/api/feedback", "turn_index"): "driven below: 400, out of range",
    ("POST", "/api/feedback", "value"): "driven below: 400, must be 1 or -1",
    ("POST", "/api/admin/evals/questions", "as_level"): "driven below: 400, out of range",
    ("POST", "/api/admin/evals/questions", "holdout"): "driven below: stored as 1, as any non-zero is",
    ("PATCH", "/api/admin/evals/questions/{question_id}", "as_level"): "driven below: 400, out of range",
    ("PATCH", "/api/admin/evals/questions/{question_id}", "holdout"): "driven below: stored as 1",
    ("PATCH", "/api/admin/evals/results/{result_id}", "score"): "driven below: 400, out of range",
    ("POST", "/api/admin/evals/run", "question_ids"): "driven below: an id past the range names no question",
    ("POST", "/api/admin/evals/run", "n_results"):
        "NOT WALKED: the number is only used once a run has started, and this file starts none",
    ("POST", "/api/ingest", "metadata"):
        "NOT WALKED: the values go to the vector store, which this suite replaces with a stand-in",
    ("POST", "/api/chat", "history"):
        "NOT WALKED: a list of a schema of its own, so a wrong entry is the schema's 422; "
        "a right one would start a real turn",
}

# Routes whose body is neither a schema nor a plain object.
_NOT_WALKED = {
    ("POST", "/api/ingest/upload"): "a file upload: a form, not a JSON body",
}


def test_control_every_whole_number_and_loose_field_of_a_schema_is_named_here():
    assert _whole_number_and_loose_fields() == set(_NUMBERS_AND_LOOSE), \
        _whole_number_and_loose_fields() ^ set(_NUMBERS_AND_LOOSE)
    other = sorted((m, p) for m, p, r in _ROUTES if _schema(r)[0] == "other")
    assert other == sorted(_NOT_WALKED), other


@pytest.fixture
def question():
    """An evaluation question of this file's own, removed afterwards."""
    from app.db import get_session
    from app.models import EvalQuestion
    with get_session() as db:
        row = EvalQuestion(question="r500 probe?", category="general", notes="",
                           created_at="2000-01-01T00:00:00")
        db.add(row)
        db.flush()
        qid = row.id
    yield qid
    with get_session() as db:
        db.query(EvalQuestion).filter(EvalQuestion.id == qid).delete()


def test_a_level_past_the_range_is_refused_in_words_where_a_question_is_written(
        client, accounts, question):
    """Creating a question and changing one raised at the write. Both answer
    400 in words and write nothing; a holdout of any size is stored as 1, as
    any non-zero value always was."""
    from app.db import get_session
    from app.models import EvalQuestion
    top = accounts("owner")
    out_of_range = (400, {"detail": "as_level is out of range"})
    with get_session() as db:
        before = db.query(EvalQuestion).count()
    answer = _send(client, "POST", "/api/admin/evals/questions", top["headers"],
                   {"question": "r500 never made?", "as_level": BIG})
    assert answer == out_of_range, answer
    with get_session() as db:
        assert db.query(EvalQuestion).count() == before
    path = f"/api/admin/evals/questions/{question}"
    assert _send(client, "PATCH", path, top["headers"], {"as_level": BIG}) == out_of_range
    assert _send(client, "PATCH", path, top["headers"], {"as_level": -BIG}) == out_of_range
    answer = _send(client, "PATCH", path, top["headers"], {"holdout": BIG, "as_level": 2})
    assert answer[0] == 200 and (answer[1]["holdout"], answer[1]["as_level"]) == (1, 2), answer
    # An id that names no question is still the first answer.
    answer = _send(client, "PATCH", f"/api/admin/evals/questions/{NAMES_NOTHING}",
                   top["headers"], {"as_level": BIG})
    assert answer == (404, {"detail": "Question not found"}), answer


def test_a_score_past_the_range_is_refused_in_words(client, accounts):
    from app.db import get_session
    from app.models import EvalResult
    top = accounts("owner")
    with get_session() as db:
        row = EvalResult(run_id="r500-run", question_text="r500?", category="general",
                         response="r500", run_at="2000-01-01T00:00:00")
        db.add(row)
        db.flush()
        rid = row.id
    try:
        path = f"/api/admin/evals/results/{rid}"
        answer = _send(client, "PATCH", path, top["headers"], {"score": BIG})
        assert answer == (400, {"detail": "score is out of range"}), answer
        answer = _send(client, "PATCH", path, top["headers"], {"score": 1})
        assert answer == (200, {"id": rid, "score": 1}), answer
    finally:
        with get_session() as db:
            db.query(EvalResult).filter(EvalResult.id == rid).delete()


def test_a_turn_number_past_the_range_is_refused_in_words(client, accounts):
    """On a conversation of the caller's own, so the request reaches the
    write. A rating that is not 1 or -1 is refused as it was."""
    from app.history import clear_session, save_message
    low = accounts("member")
    session = f"r500-{low['id']}"
    save_message(session, "user", "r500", user_id=low["id"])
    try:
        answer = _send(client, "POST", "/api/feedback", low["headers"],
                       {"session_id": session, "turn_index": BIG, "value": 1})
        assert answer == (400, {"detail": "turn_index is out of range"}), answer
        answer = _send(client, "POST", "/api/feedback", low["headers"],
                       {"session_id": session, "turn_index": 0, "value": BIG})
        assert answer == (400, {"detail": "value must be 1 or -1"}), answer
    finally:
        clear_session(session, user_id=low["id"])


def test_a_run_naming_only_ids_past_the_range_has_no_questions_to_run(client, accounts):
    """An id past the range names no question: the run answers what it
    answers for ids that name none, and starts nothing. It raised in the
    query."""
    top = accounts("owner")
    nothing = _send(client, "POST", "/api/admin/evals/run", top["headers"],
                    {"question_ids": [NAMES_NOTHING], "retrieval_only": True})
    assert nothing == (400, {"detail": "No questions to run"}), nothing
    answer = _send(client, "POST", "/api/admin/evals/run", top["headers"],
                   {"question_ids": [BIG, -BIG], "retrieval_only": True})
    assert answer == nothing, answer
