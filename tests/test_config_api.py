"""Unit coverage for the admin configuration API."""

from email.message import Message
from io import BytesIO
import json
import unittest
from unittest.mock import patch

from agent_qa import settings
from agent_qa.errors import ApiError
from agent_qa.routes import get_config_setting, list_config, validate_config


class ConfigApiTests(unittest.TestCase):
    def test_list_and_single_setting_redact_secret_value_and_default(self):
        loaded = settings.load({"AGENT_QA_API_KEY": "private-token-value"})
        with patch("agent_qa.routes.settings.current", return_value=loaded):
            status, result, headers = list_config([])
            self.assertEqual(status, 200)
            self.assertEqual(headers, {})
            self.assertTrue(result["valid"])
            self.assertEqual(set(result["settings"]), set(settings.SETTINGS))
            self.assertEqual(result["settings"]["AGENT_QA_API_KEY"]["value"], "***")
            self.assertEqual(result["settings"]["AGENT_QA_API_KEY"]["default"], "***")
            self.assertEqual(
                result["settings"]["AGENT_QA_API_KEY"]["is_default"], False
            )
            status, secret, _ = get_config_setting([], {"name": "AGENT_QA_API_KEY"})
        self.assertEqual(status, 200)
        self.assertEqual(secret["name"], "AGENT_QA_API_KEY")
        self.assertEqual(secret["value"], "***")
        self.assertEqual(secret["default"], "***")
        self.assertNotIn("private-token-value", repr(result))
        self.assertNotIn("private-token-value", repr(secret))

    def test_unknown_setting_is_not_found(self):
        with self.assertRaises(ApiError) as error:
            get_config_setting([], {"name": "AGENT_QA_NOT_REGISTERED"})
        self.assertEqual(error.exception.status, 404)
        self.assertEqual(error.exception.code, "setting_not_found")

    def test_validation_is_dry_run_sorted_and_does_not_echo_values(self):
        payload = {
            "env": {
                "APP_PORT": "not-a-port-secret",
                "AGENT_QA_RATE_BURST": "0",
                "AGENT_QA_API_KEY": "private-value-must-not-appear",
                "UNREGISTERED_NAME": "also-ignored",
                "AGENT_QA_FUTURE_SETTING": "ignored-private-value",
            }
        }
        with patch("agent_qa.routes.settings.load", wraps=settings.load) as load:
            status, result, headers = validate_config([], payload=payload)
        self.assertEqual(status, 200)
        self.assertEqual(headers, {})
        self.assertFalse(result["valid"])
        self.assertEqual(
            [item["field"] for item in result["errors"]],
            ["AGENT_QA_RATE_BURST", "APP_PORT"],
        )
        self.assertEqual(
            result["unknown"], ["AGENT_QA_FUTURE_SETTING", "UNREGISTERED_NAME"]
        )
        self.assertNotIn("not-a-port-secret", repr(result))
        self.assertNotIn("private-value-must-not-appear", repr(result))
        self.assertNotIn("ignored-private-value", repr(result))
        self.assertNotIn("also-ignored", repr(result))
        load.assert_called_once_with(payload["env"])

    def test_validation_bounds_input_without_exposing_values(self):
        cases = (
            ({"env": []}, "env"),
            ({"env": {"APP_PORT": 8080}}, "APP_PORT"),
            ({"env": {"APP_PORT": "x" * 4097}}, "APP_PORT"),
            ({"env": {f"SETTING_{i}": "x" for i in range(51)}}, "env"),
            ({"env": {"BAD-NAME": "sensitive"}}, "env"),
        )
        for payload, field in cases:
            with self.subTest(payload_type=type(payload["env"]).__name__):
                with self.assertRaises(ApiError) as error:
                    validate_config([], payload=payload)
                self.assertEqual(error.exception.status, 400)
                self.assertEqual(error.exception.code, "validation_error")
                self.assertEqual(error.exception.details[0]["field"], field)
                self.assertNotIn("sensitive", repr(error.exception.details))

    def test_admin_role_is_required_and_config_post_dispatches(self):
        from agent_qa.server import Handler

        def dispatcher(path, method, body=b""):
            headers = Message()
            headers["Content-Length"] = str(len(body))
            if method == "POST":
                headers["Content-Type"] = "application/json"
            responses = []
            handler = object.__new__(Handler)
            handler.path = path
            handler.command = method
            handler.headers = headers
            handler.rfile = BytesIO(body)
            handler.request_id = "config-api-test"
            handler._json = lambda *args: responses.append(args)
            return handler, responses

        raw_body = json.dumps({"env": {"APP_PORT": "8081"}}).encode()
        requests = (
            ("/admin/config", "GET", b""),
            ("/admin/config/AGENT_QA_GIT_SHA", "GET", b""),
            ("/admin/config/validate", "POST", raw_body),
        )
        for path, method, body in requests:
            with self.subTest(path=path, role="read"):
                handler, forbidden = dispatcher(path, method, body)
                with patch(
                    "agent_qa.server.authenticate_api_key",
                    return_value={
                        "key_id": "reader",
                        "role": "read",
                        "label": "reader",
                    },
                ):
                    Handler._dispatch(handler)
                self.assertEqual(forbidden[0][0], 403)
                self.assertEqual(forbidden[0][1]["error"]["code"], "forbidden")

            with self.subTest(path=path, role="missing"):
                handler, unauthorized = dispatcher(path, method, body)
                with patch("agent_qa.server.authenticate_api_key", return_value=None):
                    Handler._dispatch(handler)
                self.assertEqual(unauthorized[0][0], 401)
                self.assertEqual(unauthorized[0][1]["error"]["code"], "unauthorized")

        for path in ("/admin/config", "/admin/config/AGENT_QA_GIT_SHA"):
            with self.subTest(path=path, role="admin"):
                handler, accepted = dispatcher(path, "GET")
                with patch(
                    "agent_qa.server.authenticate_api_key",
                    return_value={
                        "key_id": "admin",
                        "role": "admin",
                        "label": "admin",
                    },
                ):
                    Handler._dispatch(handler)
                self.assertEqual(accepted[0][0], 200)

        handler, accepted = dispatcher("/admin/config/validate", "POST", raw_body)
        with patch(
            "agent_qa.server.authenticate_api_key",
            return_value={"key_id": "admin", "role": "admin", "label": "admin"},
        ):
            Handler._dispatch(handler)
        self.assertEqual(accepted[0][0], 200)
        self.assertTrue(accepted[0][1]["valid"])
        self.assertEqual(accepted[0][1]["errors"], [])

        handler, not_found = dispatcher("/admin/config/NO_SUCH_SETTING", "GET")
        with (
            patch(
                "agent_qa.server.authenticate_api_key",
                return_value={
                    "key_id": "admin",
                    "role": "admin",
                    "label": "admin",
                },
            ),
        ):
            Handler._handle(handler)
        self.assertEqual(not_found[0][0], 404)
        self.assertEqual(not_found[0][1]["error"]["code"], "setting_not_found")

    def test_validation_rejects_deep_and_invalid_unicode_http_payloads(self):
        from agent_qa.server import Handler

        def dispatch(body):
            raw_body = json.dumps(body).encode("utf-8")
            headers = Message()
            headers["Content-Length"] = str(len(raw_body))
            headers["Content-Type"] = "application/json"
            responses = []
            handler = object.__new__(Handler)
            handler.path = "/admin/config/validate"
            handler.command = "POST"
            handler.headers = headers
            handler.rfile = BytesIO(raw_body)
            handler.request_id = "config-api-input-test"
            handler._json = lambda *args: responses.append(args)
            with patch(
                "agent_qa.server.authenticate_api_key",
                return_value={"key_id": "admin", "role": "admin", "label": "admin"},
            ):
                Handler._handle(handler)
            return responses[0]

        deeply_nested = []
        for _ in range(34):
            deeply_nested = [deeply_nested]
        status, body, _ = dispatch({"env": deeply_nested})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

        too_many_settings = {f"SETTING_{index}": "" for index in range(51)}
        status, body, _ = dispatch({"env": too_many_settings})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["details"][0]["field"], "env")

        status, body, _ = dispatch({"env": {"APP_PORT": 8080}})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["details"][0]["field"], "env.APP_PORT")

        status, body, _ = dispatch({"env": {"AGENT_QA_API_KEY": "\ud800"}})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "validation_error")
        self.assertNotIn("\\ud800", repr(body))


if __name__ == "__main__":
    unittest.main()
