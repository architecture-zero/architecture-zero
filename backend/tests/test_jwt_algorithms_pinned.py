"""The python-jose acceptances in the security workflow rest on this test.

CVE-2026-85394 (python-jose through 3.5.0, no fixed release): an HS256 token
can pass verification against a DER-encoded PUBLIC key when the decode call
leaves its algorithms open. Not exercised here, because every jwt.decode in
app/ pins exactly one algorithm: HS256 with the server's own SECRET_KEY, or
RS256 with a sign-in provider's fetched key. A forged HS256 token never meets
a public key. PYSEC-2026-1325 (ecdsa) needs an ES* algorithm, which nothing
here uses.

If this test fails, re-triage both entries in the workflow's ignore-vulns
before touching the test.
"""
import ast
import pathlib

APP = pathlib.Path(__file__).resolve().parents[1] / "app"


def _violations(app_dir: pathlib.Path) -> tuple[list[str], int]:
    """(problems, number of jwt.decode calls seen) for every module under app_dir."""
    problems, seen = [], 0
    for path in sorted(app_dir.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        where = path.relative_to(app_dir)
        for node in ast.walk(tree):
            # Every ALGORITHM constant the HS256 calls name must be HS256.
            if isinstance(node, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == "ALGORITHM" for t in node.targets):
                if not (isinstance(node.value, ast.Constant) and node.value.value == "HS256"):
                    problems.append(f"{where}:{node.lineno}: ALGORITHM is not the literal 'HS256'")
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "decode" and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "jwt"):
                continue
            seen += 1
            algs = next((k.value for k in node.keywords if k.arg == "algorithms"), None)
            if not (isinstance(algs, ast.List) and len(algs.elts) == 1):
                problems.append(f"{where}:{node.lineno}: jwt.decode does not pin exactly one algorithm")
                continue
            alg = algs.elts[0]
            if isinstance(alg, ast.Constant) and alg.value == "RS256":
                continue
            if (isinstance(alg, ast.Name) and alg.id == "ALGORITHM") or (
                    isinstance(alg, ast.Constant) and alg.value == "HS256"):
                key = node.args[1] if len(node.args) > 1 else next(
                    (k.value for k in node.keywords if k.arg == "key"), None)
                if not (isinstance(key, ast.Name) and key.id == "SECRET_KEY"):
                    problems.append(f"{where}:{node.lineno}: an HS256 decode verifies with something other than SECRET_KEY")
                continue
            problems.append(f"{where}:{node.lineno}: jwt.decode pins an algorithm outside HS256/RS256")
    return problems, seen


def test_every_jwt_decode_pins_one_algorithm_family():
    problems, seen = _violations(APP)
    assert seen > 0, "no jwt.decode found under app/ - the guard is reading the wrong tree"
    assert problems == [], "\n".join(problems)


def test_the_guard_catches_an_open_or_mixed_decode(tmp_path):
    # The control: a guard that cannot fail is not a guard.
    (tmp_path / "bad.py").write_text(
        "from jose import jwt\n"
        "ALGORITHM = 'RS256'\n"
        "a = jwt.decode(t, SECRET_KEY)\n"
        "b = jwt.decode(t, SECRET_KEY, algorithms=['HS256', 'RS256'])\n"
        "c = jwt.decode(t, public_key, algorithms=[ALGORITHM])\n"
        "d = jwt.decode(t, key, algorithms=['ES256'])\n",
        encoding="utf-8",
    )
    problems, seen = _violations(tmp_path)
    assert seen == 4
    assert len(problems) == 5, problems
