#!/usr/bin/env python3
import hashlib
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone


API_VERSION = "2022-11-28"
FAILURE_THRESHOLD = 2
PER_PAGE = 100
STATE_VARIABLE = "RUNNER_MONITOR_STATE"
TARGET_PREFIXES = ("ORG_1", "ORG_2")


class ConfigError(Exception):
    pass


class GitHubApiError(Exception):
    pass


@dataclass(frozen=True)
class Target:
    org: str
    runner: str
    token: str


@dataclass(frozen=True)
class Check:
    target_id: str
    org: str
    runner: str
    status: str
    consecutive_failures: int


@dataclass(frozen=True)
class Alert:
    alert_type: str
    org: str
    runner: str
    status: str
    consecutive_failures: int


def parse_runner_names(value: str, name: str) -> list[str]:
    names = [part.strip() for part in value.split(",")]
    names = [part for part in names if part]
    if not names:
        raise ConfigError(f"{name} must contain at least one runner name")
    return names


def target_id(org: str, runner: str) -> str:
    return hashlib.sha256(f"{org}:{runner}".encode("utf-8")).hexdigest()[:16]


def checked_at() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class GitHubClient:
    def __init__(self, repository: str, token: str):
        if "/" not in repository:
            raise ConfigError("GITHUB_REPOSITORY must be in owner/repo form")
        self.owner, self.repo = repository.split("/", 1)
        self.token = token

    def request_json(
        self,
        url: str,
        *,
        token: str | None = None,
        method: str = "GET",
        body: dict | None = None,
        expected: tuple[int, ...] = (200,),
    ) -> dict | None:
        data = None
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token or self.token}",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": "runner-monitor",
        }
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=20) as response:
                if response.status not in expected:
                    raise GitHubApiError(f"unexpected HTTP status {response.status}")
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as err:
            raise GitHubApiError(f"HTTP_{err.code}") from err
        except urllib.error.URLError as err:
            raise GitHubApiError(err.__class__.__name__) from err

        if not raw:
            return None
        return json.loads(raw)

    def get_repo_variable(self, name: str) -> str | None:
        url = f"https://api.github.com/repos/{self.owner}/{self.repo}/actions/variables/{name}"
        try:
            payload = self.request_json(url)
        except GitHubApiError as err:
            if str(err) == "HTTP_404":
                return None
            raise
        if not isinstance(payload, dict):
            return None
        value = payload.get("value")
        return value if isinstance(value, str) else None

    def upsert_repo_variable(self, name: str, value: str) -> None:
        update_url = f"https://api.github.com/repos/{self.owner}/{self.repo}/actions/variables/{name}"
        body = {"name": name, "value": value}
        try:
            self.request_json(update_url, method="PATCH", body=body, expected=(204,))
            return
        except GitHubApiError as err:
            if str(err) != "HTTP_404":
                raise

        create_url = f"https://api.github.com/repos/{self.owner}/{self.repo}/actions/variables"
        self.request_json(create_url, method="POST", body=body, expected=(201,))

    def list_org_runners(self, org: str, token: str) -> list[dict]:
        runners = []
        page = 1

        while True:
            query = urllib.parse.urlencode({"per_page": PER_PAGE, "page": page})
            url = f"https://api.github.com/orgs/{org}/actions/runners?{query}"
            payload = self.request_json(url, token=token)
            if not isinstance(payload, dict):
                raise GitHubApiError("invalid runner response")

            page_runners = payload.get("runners", [])
            if not isinstance(page_runners, list):
                raise GitHubApiError("runner response has no runners array")

            runners.extend(page_runners)
            if len(page_runners) < PER_PAGE:
                break
            page += 1

        return runners


class SlackClient:
    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url

    def send(self, alerts: list[Alert], timestamp: str) -> None:
        payload = json.dumps({"text": render_slack_text(alerts, timestamp)}).encode("utf-8")
        req = urllib.request.Request(
            self.webhook_url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            if response.status < 200 or response.status >= 300:
                raise RuntimeError(f"slack webhook failed with status={response.status}")


def render_slack_text(alerts: list[Alert], timestamp: str) -> str:
    lines = [f"[runner-monitor] {timestamp}"]
    for alert in alerts:
        lines.append(
            "- "
            f"{alert.alert_type} "
            f"org={alert.org} "
            f"runner={alert.runner} "
            f"status={alert.status} "
            f"consecutive_failures={alert.consecutive_failures}"
        )
    return "\n".join(lines)


def load_state(client: GitHubClient) -> dict:
    raw = client.get_repo_variable(STATE_VARIABLE)
    if not raw:
        return {"version": 1, "targets": {}}
    try:
        state = json.loads(raw)
    except json.JSONDecodeError:
        return {"version": 1, "targets": {}}
    if not isinstance(state, dict) or not isinstance(state.get("targets"), dict):
        return {"version": 1, "targets": {}}
    return state


def save_state(client: GitHubClient, state: dict) -> None:
    client.upsert_repo_variable(STATE_VARIABLE, json.dumps(state, sort_keys=True))


def load_runner_names(env: dict[str, str], name: str) -> list[str]:
    raw = env.get(name, "").strip()
    if not raw:
        raise ConfigError(f"{name} secret is required")
    return parse_runner_names(raw, name)


def load_state_token(env: dict[str, str], dry_run: bool = False) -> str:
    if dry_run:
        return ""

    token = env.get("RUNNER_MONITOR_STATE_TOKEN", "").strip()
    if not token:
        raise ConfigError("RUNNER_MONITOR_STATE_TOKEN secret is required")
    return token


def load_dry_run(env: dict[str, str]) -> bool:
    value = env.get("RUNNER_MONITOR_DRY_RUN", "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def load_slack_webhook_url(env: dict[str, str], dry_run: bool) -> str:
    webhook_url = env.get("SLACK_WEBHOOK_URL", "").strip()
    if not webhook_url and not dry_run:
        raise ConfigError("SLACK_WEBHOOK_URL secret is required")
    return webhook_url


def load_targets(env: dict[str, str], client: GitHubClient) -> list[Target]:
    targets = []
    for prefix in TARGET_PREFIXES:
        token_name = f"{prefix}_RUNNER_READ_TOKEN"
        token = env.get(token_name, "").strip()
        if not token:
            raise ConfigError(f"{token_name} secret is required")

        org_name = env.get(prefix, "").strip()
        if not org_name:
            raise ConfigError(f"{prefix} secret is required")

        for runner in load_runner_names(env, f"{prefix}_RUNNERS"):
            targets.append(Target(org=org_name, runner=runner, token=token))

    return targets


def runner_status(runners: list[dict], runner_name: str) -> str:
    runners_by_name = {
        runner.get("name"): runner
        for runner in runners
        if isinstance(runner, dict) and isinstance(runner.get("name"), str)
    }
    runner = runners_by_name.get(runner_name)
    if runner is None:
        return "missing"
    status = runner.get("status")
    return status if isinstance(status, str) and status else "unknown"


def collect_checks(targets: list[Target], state: dict, client: GitHubClient) -> list[Check]:
    runners_by_org_token: dict[tuple[str, str], list[dict]] = {}
    checks = []

    for target in targets:
        key = (target.org, target.token)
        if key not in runners_by_org_token:
            runners_by_org_token[key] = client.list_org_runners(target.org, target.token)

        status = runner_status(runners_by_org_token[key], target.runner)
        tid = target_id(target.org, target.runner)
        previous = state.get("targets", {}).get(tid, {})
        previous_status = previous.get("last_status")
        previous_failures = previous.get("consecutive_failures", 0)
        if not isinstance(previous_failures, int):
            previous_failures = 0

        if status == "online":
            failures = 0
        elif previous_status != "online":
            failures = previous_failures + 1
        else:
            failures = 1

        checks.append(
            Check(
                target_id=tid,
                org=target.org,
                runner=target.runner,
                status=status,
                consecutive_failures=failures,
            )
        )

    return checks


def build_alerts(checks: list[Check], state: dict) -> tuple[list[Alert], dict]:
    alerts = []
    next_targets = {}

    for check in checks:
        previous = state.get("targets", {}).get(check.target_id, {})
        last_notified = previous.get("last_notified_status", "none")
        next_notified = last_notified if isinstance(last_notified, str) else "none"

        if check.status == "online":
            if last_notified == "down":
                next_notified = "recovered"
                alerts.append(
                    Alert(
                        alert_type="recovered",
                        org=check.org,
                        runner=check.runner,
                        status=check.status,
                        consecutive_failures=check.consecutive_failures,
                    )
                )
        elif check.consecutive_failures >= FAILURE_THRESHOLD and last_notified != "down":
            next_notified = "down"
            alerts.append(
                Alert(
                    alert_type="down",
                    org=check.org,
                    runner=check.runner,
                    status=check.status,
                    consecutive_failures=check.consecutive_failures,
                )
            )

        next_targets[check.target_id] = {
            "last_status": check.status,
            "consecutive_failures": check.consecutive_failures,
            "last_notified_status": next_notified,
        }

    return alerts, {"version": 1, "targets": next_targets}


def run(
    env: dict[str, str],
    client: GitHubClient,
    slack: SlackClient,
    timestamp: str,
    *,
    dry_run: bool | None = None,
) -> int:
    if dry_run is None:
        dry_run = load_dry_run(env)

    targets = load_targets(env, client)
    state = {"version": 1, "targets": {}} if dry_run else load_state(client)
    checks = collect_checks(targets, state, client)
    alerts, next_state = build_alerts(checks, state)
    next_state["updated_at"] = timestamp

    unavailable = sum(1 for check in checks if check.status != "online")
    failed = sum(
        1
        for check in checks
        if check.status != "online" and check.consecutive_failures >= FAILURE_THRESHOLD
    )
    print(
        "runner monitor checked: "
        f"organizations={len({target.org for target in targets})} "
        f"targets={len(checks)} "
        f"unavailable_targets={unavailable} "
        f"failed_targets={failed} "
        f"alerts={len(alerts)}"
    )

    if dry_run:
        print("runner monitor dry run: skipped slack alerts and state update")
        return 1 if failed else 0

    if alerts:
        slack.send(alerts, timestamp)
        print(f"runner monitor sent slack alerts: alerts={len(alerts)}")

    save_state(client, next_state)
    return 1 if failed else 0


def main() -> int:
    env = os.environ
    try:
        dry_run = load_dry_run(env)
        webhook_url = load_slack_webhook_url(env, dry_run)

        client = GitHubClient(env.get("GITHUB_REPOSITORY", ""), load_state_token(env, dry_run))
        return run(env, client, SlackClient(webhook_url), checked_at(), dry_run=dry_run)
    except ConfigError as err:
        print(f"configuration error: {err}", file=sys.stderr)
        return 2
    except GitHubApiError as err:
        print(f"runner monitor failed: github_api_error={err}", file=sys.stderr)
        return 1
    except Exception as err:
        print(f"runner monitor failed: error={err.__class__.__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
