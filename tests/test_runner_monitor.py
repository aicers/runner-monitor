import contextlib
import importlib.util
import io
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "runner_monitor.py"


def load_module():
    spec = importlib.util.spec_from_file_location("runner_monitor", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


MONITOR = load_module()


class FakeGitHubClient:
    def __init__(self, variables=None, runners=None, state=None):
        self.variables = dict(variables or {})
        self.runners = dict(runners or {})
        self.saved_state = state
        self.upsert_calls = []
        if state is not None:
            self.variables[MONITOR.STATE_VARIABLE] = state

    def get_repo_variable(self, name):
        return self.variables.get(name)

    def upsert_repo_variable(self, name, value):
        self.upsert_calls.append((name, value))
        self.variables[name] = value
        if name == MONITOR.STATE_VARIABLE:
            self.saved_state = value

    def list_org_runners(self, org, _token):
        return self.runners.get(org, [])


class FakeSlackClient:
    def __init__(self):
        self.messages = []

    def send(self, alerts, timestamp):
        self.messages.append(MONITOR.render_slack_text(alerts, timestamp))


def default_env():
    return {
        "GITHUB_REPOSITORY": "owner/repo",
        "RUNNER_MONITOR_STATE_TOKEN": "state-token",
        "ORG_1": "org-alpha",
        "ORG_1_RUNNERS": "runner1,runner2",
        "ORG_1_RUNNER_READ_TOKEN": "org-alpha-token",
        "ORG_2": "org-beta",
        "ORG_2_RUNNERS": "runner3",
        "ORG_2_RUNNER_READ_TOKEN": "org-beta-token",
        "SLACK_WEBHOOK_URL": "https://example.test/webhook",
    }


class RunnerMonitorTests(unittest.TestCase):
    def run_monitor(self, env, client, slack):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = MONITOR.run(env, client, slack, "2026-07-03T00:00:00Z")
        return result, stdout.getvalue()

    def test_passes_when_all_targets_are_online(self):
        client = FakeGitHubClient(
            variables={},
            runners={
                "org-alpha": [
                    {"name": "runner1", "status": "online"},
                    {"name": "runner2", "status": "online"},
                ],
                "org-beta": [{"name": "runner3", "status": "online"}],
            },
        )
        slack = FakeSlackClient()

        result, output = self.run_monitor(default_env(), client, slack)

        self.assertEqual(result, 0)
        self.assertIn("targets=3", output)
        self.assertIn("alerts=0", output)
        self.assertEqual(slack.messages, [])
        self.assertNotIn("runner1", output)

    def test_first_unavailable_check_only_updates_state(self):
        client = FakeGitHubClient(
            variables={},
            runners={
                "org-alpha": [
                    {"name": "runner1", "status": "offline"},
                    {"name": "runner2", "status": "online"},
                ],
                "org-beta": [{"name": "runner3", "status": "online"}],
            },
        )
        slack = FakeSlackClient()

        result, output = self.run_monitor(default_env(), client, slack)

        self.assertEqual(result, 0)
        self.assertIn("unavailable_targets=1", output)
        self.assertIn("failed_targets=0", output)
        self.assertIn("alerts=0", output)
        self.assertEqual(slack.messages, [])
        self.assertNotIn("runner1", output)

    def test_second_consecutive_unavailable_check_alerts_down_once(self):
        tid = MONITOR.target_id("org-alpha", "runner1")
        state = (
            '{"version":1,"targets":{'
            f'"{tid}":{{"last_status":"offline","consecutive_failures":1,"last_notified_status":"none"}}'
            "}}"
        )
        client = FakeGitHubClient(
            variables={},
            state=state,
            runners={
                "org-alpha": [
                    {"name": "runner1", "status": "offline"},
                    {"name": "runner2", "status": "online"},
                ],
                "org-beta": [{"name": "runner3", "status": "online"}],
            },
        )
        slack = FakeSlackClient()

        result, output = self.run_monitor(default_env(), client, slack)

        self.assertEqual(result, 1)
        self.assertIn("alerts=1", output)
        self.assertIn("failed_targets=1", output)
        self.assertEqual(len(slack.messages), 1)
        self.assertIn("down", slack.messages[0])
        self.assertIn("runner1", slack.messages[0])
        self.assertNotIn("runner1", output)

    def test_duplicate_down_alert_is_not_repeated(self):
        tid = MONITOR.target_id("org-alpha", "runner1")
        state = (
            '{"version":1,"targets":{'
            f'"{tid}":{{"last_status":"offline","consecutive_failures":2,"last_notified_status":"down"}}'
            "}}"
        )
        client = FakeGitHubClient(
            variables={},
            state=state,
            runners={
                "org-alpha": [
                    {"name": "runner1", "status": "offline"},
                    {"name": "runner2", "status": "online"},
                ],
                "org-beta": [{"name": "runner3", "status": "online"}],
            },
        )
        slack = FakeSlackClient()

        result, output = self.run_monitor(default_env(), client, slack)

        self.assertEqual(result, 1)
        self.assertIn("alerts=0", output)
        self.assertEqual(slack.messages, [])

    def test_recovery_alerts_after_down_notification(self):
        tid = MONITOR.target_id("org-alpha", "runner1")
        state = (
            '{"version":1,"targets":{'
            f'"{tid}":{{"last_status":"offline","consecutive_failures":2,"last_notified_status":"down"}}'
            "}}"
        )
        client = FakeGitHubClient(
            variables={},
            state=state,
            runners={
                "org-alpha": [
                    {"name": "runner1", "status": "online"},
                    {"name": "runner2", "status": "online"},
                ],
                "org-beta": [{"name": "runner3", "status": "online"}],
            },
        )
        slack = FakeSlackClient()

        result, output = self.run_monitor(default_env(), client, slack)

        self.assertEqual(result, 0)
        self.assertIn("alerts=1", output)
        self.assertEqual(len(slack.messages), 1)
        self.assertIn("recovered", slack.messages[0])
        self.assertNotIn("runner1", output)

    def test_dry_run_skips_slack_alerts_and_state_update(self):
        tid = MONITOR.target_id("org-alpha", "runner1")
        state = (
            '{"version":1,"targets":{'
            f'"{tid}":{{"last_status":"offline","consecutive_failures":1,"last_notified_status":"none"}}'
            "}}"
        )
        env = default_env()
        env["RUNNER_MONITOR_DRY_RUN"] = "true"
        client = FakeGitHubClient(
            variables={},
            state=state,
            runners={
                "org-alpha": [
                    {"name": "runner1", "status": "offline"},
                    {"name": "runner2", "status": "online"},
                ],
                "org-beta": [{"name": "runner3", "status": "online"}],
            },
        )
        slack = FakeSlackClient()

        result, output = self.run_monitor(env, client, slack)

        self.assertEqual(result, 1)
        self.assertIn("alerts=1", output)
        self.assertIn("dry run", output)
        self.assertEqual(slack.messages, [])
        self.assertEqual(client.upsert_calls, [])
        self.assertEqual(client.saved_state, state)

    def test_missing_runner_counts_as_unavailable(self):
        client = FakeGitHubClient(
            variables={},
            runners={
                "org-alpha": [{"name": "runner2", "status": "online"}],
                "org-beta": [{"name": "runner3", "status": "online"}],
            },
        )
        slack = FakeSlackClient()

        result, output = self.run_monitor(default_env(), client, slack)

        self.assertEqual(result, 0)
        self.assertIn("unavailable_targets=1", output)
        self.assertIn("failed_targets=0", output)

    def test_second_consecutive_missing_runner_alerts_down(self):
        tid = MONITOR.target_id("org-alpha", "runner1")
        state = (
            '{"version":1,"targets":{'
            f'"{tid}":{{"last_status":"missing","consecutive_failures":1,"last_notified_status":"none"}}'
            "}}"
        )
        client = FakeGitHubClient(
            variables={},
            state=state,
            runners={
                "org-alpha": [{"name": "runner2", "status": "online"}],
                "org-beta": [{"name": "runner3", "status": "online"}],
            },
        )
        slack = FakeSlackClient()

        result, output = self.run_monitor(default_env(), client, slack)

        self.assertEqual(result, 1)
        self.assertIn("failed_targets=1", output)
        self.assertIn("alerts=1", output)
        self.assertEqual(len(slack.messages), 1)

    def test_runner_names_can_be_loaded_from_environment_for_local_tests(self):
        env = default_env()
        env["ORG_1_RUNNERS"] = "runner1"
        env["ORG_2_RUNNERS"] = "runner2"
        client = FakeGitHubClient(
            variables={},
            runners={
                "org-alpha": [{"name": "runner1", "status": "online"}],
                "org-beta": [{"name": "runner2", "status": "online"}],
            },
        )
        slack = FakeSlackClient()

        result, _output = self.run_monitor(env, client, slack)

        self.assertEqual(result, 0)

    def test_missing_runner_secret_is_configuration_error(self):
        env = default_env()
        del env["ORG_2_RUNNERS"]
        client = FakeGitHubClient()

        with self.assertRaisesRegex(
            MONITOR.ConfigError,
            "ORG_2_RUNNERS secret is required",
        ):
            MONITOR.load_targets(env, client)

    def test_missing_org_secret_is_configuration_error(self):
        env = default_env()
        del env["ORG_2"]
        client = FakeGitHubClient(variables={})

        with self.assertRaisesRegex(
            MONITOR.ConfigError,
            "ORG_2 secret is required",
        ):
            MONITOR.load_targets(env, client)

    def test_missing_state_token_is_configuration_error(self):
        env = default_env()
        del env["RUNNER_MONITOR_STATE_TOKEN"]

        with self.assertRaisesRegex(
            MONITOR.ConfigError,
            "RUNNER_MONITOR_STATE_TOKEN secret is required",
        ):
            MONITOR.load_state_token(env)

    def test_slack_webhook_is_optional_for_dry_run_only(self):
        env = default_env()
        del env["SLACK_WEBHOOK_URL"]

        self.assertEqual(MONITOR.load_slack_webhook_url(env, True), "")
        with self.assertRaisesRegex(
            MONITOR.ConfigError,
            "SLACK_WEBHOOK_URL secret is required",
        ):
            MONITOR.load_slack_webhook_url(env, False)


if __name__ == "__main__":
    unittest.main()
