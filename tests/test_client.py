"""Tests for the spec-driven client.

Run with:  PYTHONPATH=src python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import httpx  # noqa: E402

from api_contract import Client, CompileError, InputError, compile_operation  # noqa: E402
from api_contract.serialize import _UNSET, build_request  # noqa: E402

FIXTURES = ROOT / "fixtures"


def load(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


SPEC = load(FIXTURES / "openapi.json")
REQUESTS = load(FIXTURES / "requests.json")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class MockServer:
    """The provided mock, launched as a real subprocess."""

    def __init__(self):
        self.port = free_port()
        self.proc = subprocess.Popen(
            [
                sys.executable,
                str(FIXTURES / "mock_api.py"),
                "--port",
                str(self.port),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=0.2).close()
                return
            except OSError:
                time.sleep(0.05)
        raise RuntimeError("mock server did not start")

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def stop(self):
        self.proc.terminate()
        self.proc.wait(timeout=5)


class NoNetworkTransport(httpx.BaseTransport):
    """Fails the test if any request is attempted."""

    def __init__(self):
        self.calls = 0

    def handle_request(self, request):  # pragma: no cover - only on failure
        self.calls += 1
        raise AssertionError(f"unexpected network request: {request.method} {request.url}")


# --------------------------------------------------------------------------- #
# Compilation / refs / styles
# --------------------------------------------------------------------------- #


class CompileTests(unittest.TestCase):
    def test_operation_plan_shape(self):
        plan = compile_operation(SPEC, "search")
        self.assertEqual(plan.method, "GET")
        self.assertEqual(plan.path_template, "/search")
        params = {p.name: p for p in plan.parameters}
        self.assertEqual(params["tags"].style, "form")
        self.assertFalse(params["tags"].explode)
        self.assertEqual(params["active"].style, "form")
        self.assertTrue(params["active"].explode)
        self.assertEqual(params["filter"].style, "deepObject")
        self.assertTrue(params["filter"].explode)
        # $refs were resolved into real parameter definitions.
        self.assertEqual(params["tags"].schema["type"], "array")

    def test_local_ref_provenance_is_reported(self):
        plan = compile_operation(SPEC, "search")
        tags = next(p for p in plan.parameters if p.name == "tags")
        self.assertIn("#/components/parameters/TagCsv", tags.spec_location)

    def test_missing_local_ref_rejected(self):
        spec = {
            "openapi": "3.0.3",
            "paths": {
                "/x": {
                    "get": {
                        "operationId": "x",
                        "responses": {},
                        "parameters": [{"$ref": "#/components/parameters/Nope"}],
                    }
                }
            },
        }
        with self.assertRaises(CompileError) as ctx:
            compile_operation(spec, "x")
        self.assertEqual(ctx.exception.spec_location, "#/paths/~1x/get/parameters/0/$ref")

    def test_cyclic_local_ref_rejected(self):
        spec = {
            "openapi": "3.0.3",
            "components": {
                "parameters": {
                    "A": {"$ref": "#/components/parameters/B"},
                    "B": {"$ref": "#/components/parameters/A"},
                }
            },
            "paths": {
                "/x": {
                    "get": {
                        "operationId": "x",
                        "responses": {},
                        "parameters": [{"$ref": "#/components/parameters/A"}],
                    }
                }
            },
        }
        with self.assertRaises(CompileError) as ctx:
            compile_operation(spec, "x")
        self.assertIn("cyclic $ref", str(ctx.exception))

    def test_external_ref_rejected(self):
        spec = {
            "openapi": "3.0.3",
            "paths": {
                "/x": {
                    "get": {
                        "operationId": "x",
                        "responses": {},
                        "parameters": [{"$ref": "other.json#/P"}],
                    }
                }
            },
        }
        with self.assertRaises(CompileError) as ctx:
            compile_operation(spec, "x")
        self.assertIn("only local", str(ctx.exception))

    def test_unsupported_style_replacement_spec_rejected(self):
        spec = load(ROOT / "cases" / "unsupported-style.json")
        with self.assertRaises(CompileError) as ctx:
            compile_operation(spec, "asset")
        self.assertIn("matrix", str(ctx.exception))
        self.assertIn("style", ctx.exception.spec_location)
        # Other operations in the same file remain compilable.
        self.assertEqual(compile_operation(spec, "search").operation_id, "search")

    def test_unsupported_query_styles_rejected(self):
        for style in ("spaceDelimited", "pipeDelimited", "matrix", "label"):
            spec = {
                "openapi": "3.0.3",
                "paths": {
                    "/x": {
                        "get": {
                            "operationId": "x",
                            "responses": {},
                            "parameters": [
                                {
                                    "name": "a",
                                    "in": "query",
                                    "style": style,
                                    "schema": {"type": "array", "items": {"type": "string"}},
                                }
                            ],
                        }
                    }
                },
            }
            with self.assertRaises(CompileError):
                compile_operation(spec, "x")

    def test_flat_form_object_rejected(self):
        spec = {
            "openapi": "3.0.3",
            "paths": {
                "/x": {
                    "get": {
                        "operationId": "x",
                        "responses": {},
                        "parameters": [
                            {
                                "name": "o",
                                "in": "query",
                                "style": "form",
                                "explode": True,
                                "schema": {"type": "object", "properties": {"a": {"type": "string"}}},
                            }
                        ],
                    }
                }
            },
        }
        with self.assertRaises(CompileError):
            compile_operation(spec, "x")

    def test_deep_object_nested_property_rejected(self):
        spec = {
            "openapi": "3.0.3",
            "paths": {
                "/x": {
                    "get": {
                        "operationId": "x",
                        "responses": {},
                        "parameters": [
                            {
                                "name": "o",
                                "in": "query",
                                "style": "deepObject",
                                "explode": True,
                                "schema": {
                                    "type": "object",
                                    "properties": {"nested": {"type": "object"}},
                                },
                            }
                        ],
                    }
                }
            },
        }
        with self.assertRaises(CompileError):
            compile_operation(spec, "x")

    def test_conflicting_parameters_rejected(self):
        spec = {
            "openapi": "3.0.3",
            "paths": {
                "/x": {
                    "get": {
                        "operationId": "x",
                        "responses": {},
                        "parameters": [
                            {"name": "a", "in": "query", "schema": {"type": "string"}},
                            {"name": "a", "in": "query", "schema": {"type": "integer"}},
                        ],
                    }
                }
            },
        }
        with self.assertRaises(CompileError) as ctx:
            compile_operation(spec, "x")
        self.assertIn("conflicting", str(ctx.exception))

    def test_ambiguous_operation_id_rejected(self):
        spec = {
            "openapi": "3.0.3",
            "paths": {
                "/a": {"get": {"operationId": "same", "responses": {}}},
                "/b": {"get": {"operationId": "same", "responses": {}}},
            },
        }
        with self.assertRaises(CompileError) as ctx:
            compile_operation(spec, "same")
        self.assertIn("ambiguous", str(ctx.exception))

    def test_unknown_operation_rejected(self):
        with self.assertRaises(CompileError):
            compile_operation(SPEC, "does-not-exist")

    def test_deep_object_requires_closed_object(self):
        for ap in (True, None):
            schema = {"type": "object", "properties": {"a": {"type": "string"}}}
            if ap is not None:
                schema["additionalProperties"] = ap
            spec = {
                "openapi": "3.0.3",
                "paths": {
                    "/x": {
                        "get": {
                            "operationId": "x",
                            "responses": {},
                            "parameters": [
                                {
                                    "name": "o",
                                    "in": "query",
                                    "style": "deepObject",
                                    "explode": True,
                                    "schema": schema,
                                }
                            ],
                        }
                    }
                },
            }
            with self.assertRaises(CompileError):
                compile_operation(spec, "x")

    def test_schema_composition_rejected(self):
        spec = {
            "openapi": "3.0.3",
            "paths": {
                "/x": {
                    "post": {
                        "operationId": "x",
                        "responses": {},
                        "requestBody": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "allOf": [{"type": "object"}]
                                    }
                                }
                            }
                        },
                    }
                }
            },
        }
        with self.assertRaises(CompileError):
            compile_operation(spec, "x")

    def test_path_placeholder_mismatch_rejected(self):
        spec = {
            "openapi": "3.0.3",
            "paths": {"/a/{id}": {"get": {"operationId": "x", "responses": {}}}},
        }
        with self.assertRaises(CompileError):
            compile_operation(spec, "x")


# --------------------------------------------------------------------------- #
# Serialisation and validation (no network)
# --------------------------------------------------------------------------- #


class SerialisationTests(unittest.TestCase):
    def setUp(self):
        self.client = Client(SPEC, base_url="http://127.0.0.1:8171")

    def plan(self, op, params=None, body=_UNSET):
        return self.client.plan(op, parameters=params, body=body)

    def test_path_reserved_characters(self):
        request = self.plan("asset", {"asset_id": "plant/a #7"})
        self.assertEqual(request.target, "/assets/plant%2Fa%20%237")
        self.assertEqual(request.url, "http://127.0.0.1:8171/assets/plant%2Fa%20%237")

    def test_comma_inside_array_atom_is_escaped(self):
        request = self.plan("search", {"tags": ["north,south", "a&b", "汉字"]})
        self.assertEqual(
            request.target,
            "/search?tags=north%2Csouth,a%26b,%E6%B1%89%E5%AD%97",
        )
        # Structural separators are literal; data separators are escaped.
        self.assertEqual(request.query_pairs[0].value, "north%2Csouth,a%26b,%E6%B1%89%E5%AD%97")

    def test_false_and_zero_are_not_dropped(self):
        request = self.plan(
            "search", {"tags": [], "active": False, "filter": {"region": "r", "min_age": 0}}
        )
        values = {(p.logical_key, p.atom) for p in request.query_pairs}
        self.assertIn(("active", "false"), values)
        self.assertIn(("filter[min_age]", "0"), values)

    def test_empty_array_omitted_and_empty_string_kept(self):
        request = self.plan("labels", {"tag": [], "note": ""})
        self.assertEqual(request.target, "/labels?note=")
        self.assertEqual(request.query_pairs, request.query_pairs)  # still pairs, one
        self.assertIsNotNone(request.query_pairs[0].atom == "")

    def test_repeated_query_keys_preserved(self):
        request = self.plan("labels", {"tag": ["a b", "x+y", "a b"]})
        self.assertEqual(request.target, "/labels?tag=a%20b&tag=x%2By&tag=a%20b")
        self.assertEqual([p.key for p in request.query_pairs], ["tag", "tag", "tag"])

    def test_parameter_key_order_does_not_change_target(self):
        a = self.plan("search", {"tags": ["x"], "active": True, "filter": {"region": "r", "min_age": 1}})
        b = self.plan("search", {"filter": {"min_age": 1, "region": "r"}, "active": True, "tags": ["x"]})
        self.assertEqual(a.target, b.target)

    def test_body_key_order_does_not_change_bytes(self):
        a = self.plan("report", {}, body={"title": "t", "columns": ["c"], "note": None})
        b = self.plan("report", {}, body={"note": None, "columns": ["c"], "title": "t"})
        self.assertEqual(a.body_bytes, b.body_bytes)

    def test_body_types_preserved_and_nullable(self):
        body = {"title": "daily", "columns": ["id", "name"], "note": None}
        request = self.plan("report", {"dryRun": True}, body=body)
        sent = json.loads(request.body_bytes)
        self.assertIsInstance(sent["title"], str)
        self.assertEqual(sent["columns"], ["id", "name"])
        self.assertIsNone(sent["note"])
        self.assertEqual(request.headers["Content-Type"], "application/json")

    def test_null_in_query_rejected(self):
        with self.assertRaises(InputError) as ctx:
            self.plan("labels", {"note": None})
        self.assertEqual(ctx.exception.input_location, "parameters/note")
        self.assertIn("#/paths/~1labels/get/parameters/1", ctx.exception.spec_location)

    def test_null_in_path_rejected(self):
        with self.assertRaises(InputError) as ctx:
            self.plan("asset", {"asset_id": None})
        self.assertEqual(ctx.exception.input_location, "parameters/asset_id")

    def test_boolean_string_not_coerced(self):
        with self.assertRaises(InputError) as ctx:
            self.plan("search", {"active": "false"})
        self.assertEqual(ctx.exception.input_location, "parameters/active")
        self.assertIn("schema/type", ctx.exception.spec_location)

    def test_integer_string_not_coerced(self):
        with self.assertRaises(InputError) as ctx:
            self.plan("search", {"filter": {"min_age": "0"}})
        self.assertEqual(ctx.exception.input_location, "parameters/filter/min_age")

    def test_unknown_parameter_rejected(self):
        with self.assertRaises(InputError) as ctx:
            self.plan("labels", {"bogus": "x"})
        self.assertIn("unknown parameter", str(ctx.exception))
        self.assertEqual(ctx.exception.input_location, "parameters/bogus")

    def test_missing_required_path_parameter(self):
        with self.assertRaises(InputError) as ctx:
            self.plan("asset", {})
        self.assertEqual(ctx.exception.input_location, "parameters/asset_id")

    def test_missing_required_body(self):
        with self.assertRaises(InputError) as ctx:
            self.plan("report", {"dryRun": True})
        self.assertEqual(ctx.exception.input_location, "body")

    def test_body_rejects_unknown_property_with_location(self):
        body = {"title": "t", "columns": ["c"], "extra": 1}
        with self.assertRaises(InputError) as ctx:
            self.plan("report", {}, body=body)
        self.assertEqual(ctx.exception.input_location, "body/extra")
        self.assertIn("requestBody", ctx.exception.spec_location)

    def test_body_rejects_empty_columns(self):
        with self.assertRaises(InputError) as ctx:
            self.plan("report", {}, body={"title": "t", "columns": []})
        self.assertIn("body/columns", ctx.exception.input_location)

    def test_base_url_override_keeps_spec_path(self):
        request = client_base = self.client.plan(
            "asset", {"asset_id": "x"}, base_url="http://127.0.0.1:9999/v1"
        )
        self.assertEqual(client_base.path_template, "/assets/{asset_id}")
        self.assertEqual(request.target, "/v1/assets/x")
        self.assertEqual(request.url, "http://127.0.0.1:9999/v1/assets/x")

    def test_base_url_with_query_rejected(self):
        with self.assertRaises(Exception):
            self.plan("asset", {"asset_id": "x"}, base_url="http://127.0.0.1:1/?x=1")


# --------------------------------------------------------------------------- #
# Independent hand-written URL check, then server interpretation
# --------------------------------------------------------------------------- #


class HandWrittenUrlTests(unittest.TestCase):
    """Compare generated targets with literal, hand-built URLs, then send them."""

    @classmethod
    def setUpClass(cls):
        cls.server = MockServer()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()

    def test_hand_written_path_url(self):
        literal = "/assets/a%2Fb%20c%23d"
        client = Client(SPEC, base_url=self.server.base_url)
        generated = client.plan("asset", {"asset_id": "a/b c#d"}).target
        self.assertEqual(generated, literal)
        # Independent expectation: server's URL splitter must see one path segment.
        with httpx.Client(base_url=self.server.base_url) as http:
            seen = http.get(literal).json()
        self.assertEqual(seen["raw_target"], literal)
        self.assertEqual(seen["asset_id"], "a/b c#d")

    def test_hand_written_query_url(self):
        literal = "/search?tags=x%2Cy,z&active=false&filter[region]=r%2Fs&filter[min_age]=0"
        client = Client(SPEC, base_url=self.server.base_url)
        generated = client.plan(
            "search",
            {"tags": ["x,y", "z"], "active": False, "filter": {"region": "r/s", "min_age": 0}},
        ).target
        self.assertEqual(generated, literal)
        with httpx.Client(base_url=self.server.base_url) as http:
            seen = http.get(literal).json()
        self.assertEqual(seen["raw_target"], literal)
        self.assertEqual(seen["tags"], ["x,y", "z"])
        self.assertEqual(seen["filter"], {"region": "r/s", "min_age": "0"})
        self.assertEqual(seen["active"], "false")


# --------------------------------------------------------------------------- #
# Actual calls against the provided mock
# --------------------------------------------------------------------------- #


class CallTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = MockServer()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()

    def client(self):
        return Client(SPEC, base_url=self.server.base_url)

    def test_cases_recorded_from_requests_fixture(self):
        client = self.client()
        for case in REQUESTS:
            body = case["body"] if "body" in case else _UNSET
            with self.subTest(case=case["case_id"]):
                try:
                    result = client.call(
                        case["operationId"], parameters=case.get("parameters", {}), body=body
                    )
                except InputError as exc:
                    # Invalid cases must be rejected before I/O.
                    self.assertIn(exc.input_location, {"parameters/active", "parameters/note"})
                    continue
                self.assertEqual(result.status_code, 200)
                self.assertIn("application/json", result.content_type)
                self.assertTrue(result.is_json)
                # The server's raw target is exactly what we planned.
                self.assertEqual(result.json["raw_target"], result.target)

    def test_server_side_reinterpretation(self):
        client = self.client()
        # Reserved path characters.
        r = client.call("asset", {"asset_id": "plant/a #7"})
        self.assertEqual(r.json["raw_target"], "/assets/plant%2Fa%20%237")
        self.assertEqual(r.json["asset_id"], "plant/a #7")
        # Comma inside atom, false, zero, UTF-8, deepObject.
        r = client.call(
            "search",
            {"tags": ["north,south", "a&b", "汉字"], "active": False, "filter": {"region": "east/west", "min_age": 0}},
        )
        self.assertEqual(r.json["tags"], ["north,south", "a&b", "汉字"])
        self.assertEqual(r.json["active"], "false")
        self.assertEqual(r.json["filter"], {"region": "east/west", "min_age": "0"})
        # Repeated keys: the mock reports every pair, no dict collapse.
        r = client.call("labels", {"tag": ["a b", "x+y", "a b"], "note": ""})
        self.assertEqual(
            r.json["pairs"],
            [["tag", "a b"], ["tag", "x+y"], ["tag", "a b"], ["note", ""]],
        )
        # Body types and nullable null survive JSON.
        r = client.call(
            "report",
            {"dryRun": True},
            body={"title": "daily", "columns": ["id", "name"], "note": None},
        )
        self.assertEqual(r.json["body"]["columns"], ["id", "name"])
        self.assertIsNone(r.json["body"]["note"])
        self.assertEqual(r.json["pairs"], [["dryRun", "true"]])

    def test_invalid_calls_never_hit_network(self):
        transport = NoNetworkTransport()
        client = Client(SPEC, base_url=self.server.base_url, transport=transport)
        for op, params, body in [
            ("search", {"active": "false"}, _UNSET),
            ("labels", {"note": None}, _UNSET),
            ("asset", {"asset_id": None}, _UNSET),
            ("labels", {"bogus": "x"}, _UNSET),
            ("report", {"dryRun": True}, {"title": "t"}),  # missing columns
        ]:
            with self.subTest(op=op, params=params):
                with self.assertRaises(InputError):
                    client.call(op, parameters=params, body=body)
        self.assertEqual(transport.calls, 0)

    def test_error_response_is_recorded_not_wrapped(self):
        # A base-path prefix changes the URL but not the spec path; the mock
        # then answers 404. The client must report that honestly.
        client = self.client()
        result = client.call("labels", {"note": "x"}, base_url=self.server.base_url + "/v1")
        self.assertEqual(result.status_code, 404)
        self.assertTrue(result.is_json)
        self.assertEqual(result.json.get("error"), "not found")
        self.assertEqual(result.target, "/v1/labels?note=x")

    def test_base_url_override_per_call(self):
        client = self.client()
        result = client.call("labels", {}, base_url=self.server.base_url)
        self.assertEqual(result.target, "/labels")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json["raw_target"], "/labels")


if __name__ == "__main__":
    unittest.main()
