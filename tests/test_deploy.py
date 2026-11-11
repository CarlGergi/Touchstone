"""The deployment scripts, run for real against a fake aws command that records every call."""

import ast
import json
import os
import pathlib
import stat
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
SERVER = REPO / "scripts" / "server"

FAKE_AWS = r"""#!{python}
import json, os, pathlib, sys
args = sys.argv[1:]
state = pathlib.Path(os.environ["FAKE_STATE"])
with open(state / "calls.jsonl", "a") as log:
    log.write(json.dumps(args) + "\n")
if os.environ.get("FAKE_FAIL") == " ".join(args[:2]):
    print("simulated failure", file=sys.stderr)
    sys.exit(1)
def flag(name):
    return args[args.index(name) + 1] if name in args else None
def exists(key):
    return (state / key).exists()
def make(key):
    (state / key).touch()
def absent():
    sys.exit(254)
command = " ".join(args[:2])
if command == "sts get-caller-identity":
    if os.environ.get("FAKE_NO_CREDENTIALS"):
        print("Unable to locate credentials", file=sys.stderr)
        sys.exit(253)
    print("123456789012")
elif command == "s3api head-bucket":
    exists("bucket") or absent()
elif command == "s3api create-bucket":
    make("bucket")
elif command == "iam get-role":
    exists("role-" + flag("--role-name")) or absent()
    if flag("--query"):
        print("arn:aws:iam::123456789012:role/" + flag("--role-name"))
elif command == "iam create-role":
    make("role-" + flag("--role-name"))
elif command == "iam get-instance-profile":
    exists("profile") or absent()
elif command == "iam create-instance-profile":
    make("profile")
elif command == "ec2 describe-vpcs":
    print(os.environ.get("FAKE_VPC", "vpc-1"))
elif command == "ec2 describe-security-groups":
    print("sg-1" if exists("sg") else "None")
elif command == "ec2 create-security-group":
    make("sg"); print("sg-1")
elif command == "ec2 run-instances":
    tries = len(list(state.glob("launch-try-*")))
    make(f"launch-try-{{tries}}")
    if tries < int(os.environ.get("FAKE_LAUNCH_FAILURES", "0")):
        print("Invalid IAM Instance Profile", file=sys.stderr)
        sys.exit(254)
    make("instance"); print("i-1")
elif command == "ec2 describe-instances":
    if flag("--instance-ids") and "BlockDeviceMappings" in (flag("--query") or ""):
        print("vol-1")
    elif flag("--instance-ids"):
        print(os.environ.get("FAKE_INSTANCE_STATE", "running"))
    else:
        print("i-1" if exists("instance") else "None")
elif command == "ssm describe-instance-information":
    print("Online")
elif command == "ssm send-command":
    print("cmd-1")
elif command == "ssm get-command-invocation":
    status = os.environ.get("FAKE_REMOTE_STATUS", "Success")
    if flag("--query") == "Status":
        print(status)
    elif flag("--query") == "StandardOutputContent":
        print(os.environ.get("FAKE_REMOTE_OUTPUT", "remote output"))
elif command == "s3api list-objects-v2":
    print(os.environ.get("FAKE_S3_LISTING", "None"))
elif command in ("ec2 terminate-instances", "ec2 delete-volume") and "--dry-run" in args:
    print("An error occurred (DryRunOperation): would have succeeded", file=sys.stderr)
    sys.exit(254)
elif command == "s3 cp" and args[2] == "-":
    sys.stdin.buffer.read()
elif command == "s3 cp" and args[3] == "-":
    print(os.environ.get("FAKE_HEARTBEAT", '{{"products": {{"BTC-USD": 5, "ETH-USD": 5}}}}'))
elif command == "lambda get-function":
    exists("function") or absent()
    if flag("--query"):
        print("arn:aws:lambda:us-east-1:123456789012:function:touchstone-alert")
elif command == "lambda create-function":
    make("function")
elif command == "budgets describe-budget":
    exists("budget") or absent()
elif command == "budgets create-budget":
    make("budget")
elif command == "budgets describe-budget-actions-for-budget":
    print("a-1" if exists("budget-action") else "None")
elif command == "budgets create-budget-action":
    make("budget-action")
elif command == "events put-rule":
    print("arn:aws:events:us-east-1:123456789012:rule/touchstone-alert")
"""

FAKE_DOCKER = """#!/bin/sh
echo "docker $*" >>"$FAKE_STATE/docker.log"
[ "$1" = save ] && echo image-bytes
[ "$1" = run ] && echo '{"day": "fake"}'
exit "${FAKE_DOCKER_EXIT:-0}"
"""

FAKE_DATE = """#!/bin/sh
if [ "$FAKE_EPOCH" ] && [ "$2" = "+%s" ]; then
    echo "$FAKE_EPOCH"
else
    /bin/date "$@"
fi
"""

FAKE_CURL = """#!/bin/sh
echo "curl $*" >>"$FAKE_STATE/curl.log"
exit 0
"""


class Fakes:
    def __init__(self, tmp_path):
        self.state = tmp_path / "state"
        self.state.mkdir()
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        self.deploy_env = tmp_path / "deploy.env"
        for name, body in (
            ("aws", FAKE_AWS.format(python=sys.executable)),
            ("docker", FAKE_DOCKER),
            ("curl", FAKE_CURL),
            ("date", FAKE_DATE),
        ):
            path = self.bin / name
            path.write_text(body)
            path.chmod(path.stat().st_mode | stat.S_IEXEC)

    def env(self, **extra):
        return {
            **os.environ,
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "FAKE_STATE": str(self.state),
            "DEPLOY_ENV": str(self.deploy_env),
            "RETRY_DELAY_S": "0",
            "BUDGET_EMAIL": "me@example.com",
            **extra,
        }

    def calls(self):
        path = self.state / "calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def calls_to(self, *command):
        return [c for c in self.calls() if c[: len(command)] == list(command)]

    def deploy(self, **extra):
        return subprocess.run(
            ["bash", str(REPO / "scripts" / "deploy.sh")],
            env=self.env(**extra),
            capture_output=True,
            text=True,
        )


@pytest.fixture
def fakes(tmp_path):
    return Fakes(tmp_path)


def value(call, flag):
    return call[call.index(flag) + 1]


def test_first_deploy_launches_the_requested_instance(fakes):
    result = fakes.deploy()
    assert result.returncode == 0, result.stderr
    [launch] = fakes.calls_to("ec2", "run-instances")
    assert value(launch, "--instance-type") == "t4g.small"
    assert "arm64" in value(launch, "--image-id")
    [disk] = json.loads(value(launch, "--block-device-mappings"))
    assert disk["Ebs"] == {
        "VolumeSize": 50,
        "VolumeType": "gp3",
        "Encrypted": True,
        "DeleteOnTermination": False,
    }
    assert "--disable-api-termination" in launch
    assert "HttpTokens=required" in value(launch, "--metadata-options")


def test_budget_counts_spend_before_credits_with_the_agreed_alerts(fakes):
    assert fakes.deploy().returncode == 0
    [create] = fakes.calls_to("budgets", "create-budget")
    budget = json.loads(value(create, "--budget"))
    assert budget["BudgetLimit"] == {"Amount": "100", "Unit": "USD"}
    assert budget["CostTypes"]["IncludeCredit"] is False
    assert budget["TimeUnit"] == "ANNUALLY"
    alerts = json.loads(value(create, "--notifications-with-subscribers"))
    thresholds = [
        (a["Notification"]["NotificationType"], a["Notification"]["Threshold"]) for a in alerts
    ]
    assert thresholds == [("ACTUAL", 25), ("ACTUAL", 50), ("ACTUAL", 80), ("FORECASTED", 80)]
    assert {a["Subscribers"][0]["Address"] for a in alerts} == {"me@example.com"}


def test_budget_stops_the_instance_at_ninety_dollars_without_terminating_it(fakes):
    assert fakes.deploy().returncode == 0
    [action] = fakes.calls_to("budgets", "create-budget-action")
    assert "ActionThresholdValue=90" in value(action, "--action-threshold")
    definition = json.loads(value(action, "--definition"))["SsmActionDefinition"]
    assert definition == {
        "ActionSubType": "STOP_EC2_INSTANCES",
        "Region": "us-east-2",
        "InstanceIds": ["i-1"],
    }
    assert value(action, "--approval-model") == "AUTOMATIC"


def test_budget_is_not_duplicated_on_a_second_deploy(fakes):
    assert fakes.deploy().returncode == 0
    assert fakes.deploy().returncode == 0
    assert len(fakes.calls_to("budgets", "create-budget")) == 1
    assert len(fakes.calls_to("budgets", "create-budget-action")) == 1


def test_deploy_refuses_to_start_without_a_budget_email(fakes):
    result = fakes.deploy(BUDGET_EMAIL="")
    assert result.returncode != 0
    assert "BUDGET_EMAIL" in result.stderr
    assert fakes.calls() == []


def test_default_region_is_the_project_region(fakes):
    assert fakes.deploy().returncode == 0
    commands = " ".join(value(c, "--parameters") for c in fakes.calls_to("ssm", "send-command"))
    assert "AWS_REGION=us-east-2" in commands


def test_nothing_opens_an_inbound_port_or_creates_an_ssh_key(fakes):
    assert fakes.deploy().returncode == 0
    commands = {" ".join(c[:2]) for c in fakes.calls()}
    assert "ec2 authorize-security-group-ingress" not in commands
    assert "ec2 create-key-pair" not in commands
    assert "--key-name" not in fakes.calls_to("ec2", "run-instances")[0]


def test_bucket_is_private_and_versioned(fakes):
    assert fakes.deploy().returncode == 0
    [block] = fakes.calls_to("s3api", "put-public-access-block")
    settings = value(block, "--public-access-block-configuration")
    for setting in (
        "BlockPublicAcls",
        "IgnorePublicAcls",
        "BlockPublicPolicy",
        "RestrictPublicBuckets",
    ):
        assert f"{setting}=true" in settings
    assert fakes.calls_to("s3api", "put-bucket-versioning")
    assert value(block, "--bucket") == "touchstone-recordings-123456789012"


def test_server_role_cannot_delete_from_the_bucket(fakes):
    assert fakes.deploy().returncode == 0
    for call in fakes.calls_to("iam", "put-role-policy"):
        document = json.loads(value(call, "--policy-document"))
        actions = json.dumps([s["Action"] for s in document["Statement"]])
        assert "Delete" not in actions
        assert "*" not in actions


def test_alert_only_starts_after_first_data_is_reported(fakes):
    assert fakes.deploy().returncode == 0
    commands = [" ".join(c[:2]) for c in fakes.calls()]
    assert commands.index("s3 cp") < commands.index("lambda create-function")
    heartbeat_read = [c for c in fakes.calls_to("s3", "cp") if c[-1] == "-"]
    assert heartbeat_read
    assert commands.index("lambda create-function") < commands.index("events put-rule")


def test_deploy_gives_up_if_no_data_is_ever_reported(fakes):
    result = fakes.deploy(FAKE_HEARTBEAT='{"products": {"BTC-USD": 5, "ETH-USD": 0}}')
    assert result.returncode != 0
    assert "no data reported" in result.stderr
    assert not fakes.calls_to("lambda", "create-function")


def test_alert_package_holds_a_top_level_handler(fakes):
    assert fakes.deploy().returncode == 0
    [create] = fakes.calls_to("lambda", "create-function")
    assert value(create, "--handler") == "alert.handler"
    assert "arm64" in create
    environment = json.loads(value(create, "--environment"))["Variables"]
    assert environment["PRODUCTS"] == "BTC-USD,ETH-USD"
    assert environment["BUCKET"] == "touchstone-recordings-123456789012"
    assert environment["NTFY_TOPIC"].startswith("touchstone-")


def test_topic_is_random_private_and_kept_between_runs(fakes):
    assert fakes.deploy().returncode == 0
    saved = fakes.deploy_env.read_text()
    topic = saved.strip().split("=")[1]
    assert len(topic) >= len("touchstone-") + 32
    assert stat.S_IMODE(fakes.deploy_env.stat().st_mode) == 0o600
    assert fakes.deploy().returncode == 0
    assert fakes.deploy_env.read_text() == saved


def test_second_deploy_updates_without_creating_duplicates(fakes):
    assert fakes.deploy().returncode == 0
    assert fakes.deploy().returncode == 0
    assert len(fakes.calls_to("ec2", "run-instances")) == 1
    assert len(fakes.calls_to("s3api", "create-bucket")) == 1
    assert len(fakes.calls_to("ec2", "create-security-group")) == 1
    assert len(fakes.calls_to("lambda", "create-function")) == 1
    assert fakes.calls_to("lambda", "update-function-code")


def test_stopped_instance_is_started_not_replaced(fakes):
    assert fakes.deploy().returncode == 0
    assert fakes.deploy(FAKE_INSTANCE_STATE="stopped").returncode == 0
    assert len(fakes.calls_to("ec2", "run-instances")) == 1
    assert fakes.calls_to("ec2", "start-instances")


def test_launch_is_retried_while_the_instance_profile_propagates(fakes):
    assert fakes.deploy(FAKE_LAUNCH_FAILURES="2").returncode == 0
    assert len(fakes.calls_to("ec2", "run-instances")) == 3


def test_missing_credentials_stop_before_anything_is_created(fakes):
    result = fakes.deploy(FAKE_NO_CREDENTIALS="1")
    assert result.returncode != 0
    assert "credentials" in result.stderr
    assert [c[:2] for c in fakes.calls()] == [["sts", "get-caller-identity"]]
    assert not fakes.deploy_env.exists()


def test_failed_remote_command_fails_the_deploy(fakes):
    result = fakes.deploy(FAKE_REMOTE_STATUS="Failed")
    assert result.returncode != 0
    assert "status Failed" in result.stderr


def test_no_default_vpc_is_a_clear_error(fakes):
    result = fakes.deploy(FAKE_VPC="None")
    assert result.returncode != 0
    assert "no default VPC" in result.stderr


def test_server_settings_reach_the_server(fakes):
    assert fakes.deploy().returncode == 0
    commands = " ".join(value(c, "--parameters") for c in fakes.calls_to("ssm", "send-command"))
    assert "TOUCHSTONE_BUCKET=touchstone-recordings-123456789012" in commands
    assert "NTFY_TOPIC=touchstone-" in commands
    assert "umask 077" in commands


def sync(fakes, tmp_path, **extra):
    env = fakes.env(
        DATA_DIR=str(tmp_path / "data"),
        TOUCHSTONE_BUCKET="bucket",
        NTFY_TOPIC="topic",
        **extra,
    )
    return subprocess.run(
        ["bash", str(SERVER / "sync.sh")], env=env, capture_output=True, text=True
    )


def test_sync_copies_finished_files_and_never_deletes(fakes, tmp_path):
    assert sync(fakes, tmp_path, TOUCHSTONE_PRODUCTS="BTC-USD").returncode == 0
    [call] = fakes.calls_to("s3", "sync")
    assert call[3] == "s3://bucket/recordings"
    assert "--delete" not in call
    assert "--dryrun" not in call
    filters = [
        (call[i], call[i + 1]) for i, a in enumerate(call) if a in ("--exclude", "--include")
    ]
    # In aws s3 sync, later filters win, so the order is the meaning.
    assert filters == [
        ("--exclude", "*"),
        ("--include", "*/20??-??-??.parquet"),
        ("--include", "health/*.jsonl"),
        ("--exclude", ".spool/*"),
    ]


def test_yesterdays_summary_goes_up_after_the_sync(fakes, tmp_path):
    assert sync(fakes, tmp_path, TOUCHSTONE_PRODUCTS="BTC-USD,ETH-USD").returncode == 0
    commands = [c[:2] for c in fakes.calls()]
    assert commands == [["s3", "sync"], ["s3", "cp"]]
    destination = fakes.calls_to("s3", "cp")[0][3]
    assert destination.startswith("s3://bucket/ops/daily/20")
    docker = (fakes.state / "docker.log").read_text()
    assert "daily-summary" in docker
    assert "--memory=1g" in docker, "it must not be able to take the recorder down"
    assert destination.removeprefix("s3://bucket/ops/daily/").removesuffix(".json") in docker


def test_failed_summary_pushes_an_alert_and_uploads_no_summary(fakes, tmp_path):
    result = sync(fakes, tmp_path, TOUCHSTONE_PRODUCTS="BTC-USD", FAKE_DOCKER_EXIT="1")
    assert result.returncode != 0
    assert "ntfy.sh/topic" in (fakes.state / "curl.log").read_text()


def test_failed_sync_pushes_an_alert_and_fails(fakes, tmp_path):
    result = sync(fakes, tmp_path, FAKE_FAIL="s3 sync", TOUCHSTONE_PRODUCTS="BTC-USD")
    assert result.returncode != 0
    log = (fakes.state / "curl.log").read_text()
    assert "ntfy.sh/topic" in log
    assert "sync failed" in log


def test_successful_sync_sends_no_alert(fakes, tmp_path):
    assert sync(fakes, tmp_path, TOUCHSTONE_PRODUCTS="BTC-USD").returncode == 0
    assert not (fakes.state / "curl.log").exists()


def heartbeat(fakes, data_dir, **extra):
    capture = fakes.state / "body.json"
    wrapper = fakes.bin / "aws"
    wrapper.write_text(
        f'#!/bin/sh\necho "$*" >"{fakes.state}/args"\ncat >"{capture}"\nexit ${{FAKE_EXIT:-0}}\n'
    )
    env = fakes.env(
        DATA_DIR=str(data_dir),
        TOUCHSTONE_PRODUCTS="BTC-USD,ETH-USD",
        TOUCHSTONE_BUCKET="bucket",
        **extra,
    )
    result = subprocess.run(
        [sys.executable, str(SERVER / "heartbeat.py")], env=env, capture_output=True, text=True
    )
    return result, json.loads(capture.read_text()) if capture.read_text() else None


def touch(path, mtime):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    os.utime(path, (mtime, mtime))


def test_heartbeat_reports_the_newest_write_for_each_product(fakes, tmp_path):
    data = tmp_path / "data"
    touch(data / ".spool/BTC-USD/2026-11-05/part-00000001.parquet", 1000)
    touch(data / ".spool/BTC-USD/2026-11-05/part-00000002.parquet", 2000)
    touch(data / "BTC-USD/2026-11-04.parquet", 1500)
    touch(data / "ETH-USD/2026-11-04.parquet", 900)
    result, body = heartbeat(fakes, data)
    assert result.returncode == 0, result.stderr
    assert body["products"] == {"BTC-USD": 2000, "ETH-USD": 900}
    assert body["disk_free_bytes"] > 0
    assert "s3://bucket/ops/heartbeat.json" in (fakes.state / "args").read_text()


def test_heartbeat_ignores_files_that_are_not_finished_parts(fakes, tmp_path):
    data = tmp_path / "data"
    touch(data / ".spool/BTC-USD/2026-11-05/part-00000001.parquet", 1000)
    touch(data / ".spool/BTC-USD/2026-11-05/part-00000002.tmp", 9000)
    touch(data / "BTC-USD/2026-11-04.parquet.tmp", 9000)
    _, body = heartbeat(fakes, data)
    assert body["products"]["BTC-USD"] == 1000


def test_heartbeat_reports_zero_for_a_product_with_no_data(fakes, tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    result, body = heartbeat(fakes, data)
    assert result.returncode == 0
    assert body["products"] == {"BTC-USD": 0, "ETH-USD": 0}


def test_heartbeat_fails_loudly_if_the_upload_fails(fakes, tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    result, _ = heartbeat(fakes, data, FAKE_EXIT="1")
    assert result.returncode != 0


def test_heartbeat_runs_on_the_servers_python():
    # Amazon Linux 2023's system Python is 3.9.
    ast.parse((SERVER / "heartbeat.py").read_text(), feature_version=(3, 9))


@pytest.mark.parametrize(
    "script", sorted(str(p.relative_to(REPO)) for p in REPO.glob("scripts/**/*.sh"))
)
def test_shell_scripts_parse(script):
    assert subprocess.run(["bash", "-n", script], cwd=REPO).returncode == 0


def test_shell_scripts_pass_shellcheck():
    scripts = [str(p) for p in REPO.glob("scripts/**/*.sh")]
    result = subprocess.run(
        ["shellcheck", "-x", *scripts],
        cwd=REPO,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PATH": f"{pathlib.Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}",
        },
    )
    assert result.returncode == 0, result.stdout


AFTER_THE_HOLDOUT = "1796774400"  # 2026-12-09 00:00 UTC
SERVER_FILES = "spool-parts 0\nBTC-USD/2026-12-07.parquet 100\nhealth/2026-12-07.jsonl 50"
S3_FILES = "recordings/BTC-USD/2026-12-07.parquet\t100\nrecordings/health/2026-12-07.jsonl\t50"


def teardown(fakes, *args, answer="delete\n", **extra):
    settings = {
        "FAKE_EPOCH": AFTER_THE_HOLDOUT,
        "FAKE_REMOTE_OUTPUT": SERVER_FILES,
        "FAKE_S3_LISTING": S3_FILES,
        **extra,
    }
    (fakes.state / "instance").touch()
    return subprocess.run(
        ["bash", str(REPO / "scripts" / "teardown.sh"), *args],
        env=fakes.env(**settings),
        input=answer,
        capture_output=True,
        text=True,
    )


def destructive(fakes):
    wanted = {"ec2 terminate-instances", "ec2 delete-volume", "ec2 modify-instance-attribute"}
    wanted |= {"lambda delete-function", "events delete-rule"}
    return [c for c in fakes.calls() if " ".join(c[:2]) in wanted and "--dry-run" not in c]


def test_teardown_refuses_before_the_day_after_the_recording_must_end(fakes):
    result = teardown(fakes, FAKE_EPOCH=str(int(AFTER_THE_HOLDOUT) - 1))
    assert result.returncode != 0
    assert "2026-12-09" in result.stderr
    assert fakes.calls() == []


def test_teardown_refuses_today(fakes):
    result = teardown(fakes, FAKE_EPOCH="")
    assert result.returncode != 0
    assert fakes.calls() == []


def test_teardown_needs_the_typed_confirmation(fakes):
    result = teardown(fakes, answer="yes\n")
    assert result.returncode != 0
    assert "cancelled" in result.stderr
    assert destructive(fakes) == []
    assert fakes.calls_to("ssm", "send-command") == []


def test_teardown_deletes_the_instance_and_disk_after_confirming_s3(fakes):
    result = teardown(fakes)
    assert result.returncode == 0, result.stderr
    commands = [" ".join(c[:2]) for c in fakes.calls()]
    assert commands.index("s3api list-objects-v2") < commands.index("ec2 terminate-instances")
    assert commands.index("ec2 modify-instance-attribute") < commands.index(
        "ec2 terminate-instances"
    )
    assert commands.index("ec2 terminate-instances") < commands.index("ec2 delete-volume")
    assert value(fakes.calls_to("ec2", "delete-volume")[0], "--volume-id") == "vol-1"


def test_teardown_stops_the_alert_first_so_stopping_does_not_raise_it(fakes):
    assert teardown(fakes).returncode == 0
    commands = [" ".join(c[:2]) for c in fakes.calls()]
    assert commands.index("events delete-rule") < commands.index("ssm send-command")
    assert "lambda delete-function" in commands


def test_teardown_stops_merges_and_syncs_in_that_order(fakes):
    assert teardown(fakes).returncode == 0
    script = value(fakes.calls_to("ssm", "send-command")[0], "--parameters")
    stop, merge, sync_step = (
        script.index("systemctl stop"),
        script.index("--include-today"),
        script.index("/opt/touchstone/sync.sh"),
    )
    assert stop < merge < sync_step
    assert "touchstone-recorder.service" in script
    assert "set -e" in script


@pytest.mark.parametrize(
    ("server", "listing"),
    [
        (SERVER_FILES, "recordings/BTC-USD/2026-12-07.parquet\t100"),
        (
            SERVER_FILES,
            "recordings/BTC-USD/2026-12-07.parquet\t99\nrecordings/health/2026-12-07.jsonl\t50",
        ),
        (SERVER_FILES, "None"),
        ("spool-parts 3\nBTC-USD/2026-12-07.parquet 100\nhealth/2026-12-07.jsonl 50", S3_FILES),
        ("spool-parts 0", S3_FILES),
    ],
    ids=["file_missing", "wrong_size", "bucket_empty", "unmerged_parts", "server_lists_nothing"],
)
def test_teardown_deletes_nothing_unless_s3_matches_the_server(fakes, server, listing):
    result = teardown(fakes, FAKE_REMOTE_OUTPUT=server, FAKE_S3_LISTING=listing)
    assert result.returncode != 0
    assert "nothing was deleted" in result.stderr
    for command in ("terminate-instances", "delete-volume", "modify-instance-attribute"):
        assert fakes.calls_to("ec2", command) == []


def test_teardown_never_touches_the_bucket(fakes):
    assert teardown(fakes).returncode == 0
    commands = {" ".join(c[:2]) for c in fakes.calls()}
    assert not commands & {"s3 rm", "s3 rb", "s3api delete-bucket", "s3api delete-object"}


def test_early_flag_allows_abandoning_the_recording_but_still_confirms(fakes):
    result = teardown(fakes, "--early", FAKE_EPOCH="1000", answer="no\n")
    assert result.returncode != 0
    assert "cancelled" in result.stderr
    assert teardown(fakes, "--early", FAKE_EPOCH="1000").returncode == 0


def test_rehearsal_changes_nothing(fakes):
    result = teardown(fakes, "--check", FAKE_EPOCH="1000", answer="")
    assert result.returncode == 0, result.stderr
    assert destructive(fakes) == []
    assert fakes.calls_to("ssm", "send-command")
    script = value(fakes.calls_to("ssm", "send-command")[0], "--parameters")
    assert "systemctl stop" not in script
    assert fakes.calls_to("ec2", "terminate-instances")[0][-1] == "--dry-run"
    assert "2 finished file(s) on the server, 2 in S3, 0 difference(s)" in result.stdout


def test_rehearsal_reports_differences_without_failing(fakes):
    result = teardown(fakes, "--check", FAKE_EPOCH="1000", FAKE_S3_LISTING="None")
    assert result.returncode == 0
    assert "2 difference(s)" in result.stdout


def test_unknown_teardown_option_is_refused(fakes):
    result = teardown(fakes, "--force")
    assert result.returncode != 0
    assert fakes.calls() == []


def test_readme_says_when_to_run_the_teardown():
    readme = (REPO / "README.md").read_text()
    assert "Run `make teardown` after 2026-12-08" in readme


def unit(name):
    return (SERVER / "systemd" / name).read_text()


def test_recorder_is_supervised_so_even_docker_kill_restarts_it():
    service = unit("touchstone-recorder.service")
    assert "Restart=always" in service
    assert "StartLimitIntervalSec=0" in service
    assert "--rm" in service
    assert "--restart" not in service


def test_recorder_records_the_configured_products_not_a_built_in_default():
    service = unit("touchstone-recorder.service")
    assert "EnvironmentFile=/etc/touchstone/env" in service
    assert "--products ${TOUCHSTONE_PRODUCTS}" in service


def test_scheduled_jobs_run_on_the_agreed_cadence():
    assert "OnCalendar=*-*-* 00:30:00 UTC" in unit("touchstone-sync.timer")
    assert "Persistent=true" in unit("touchstone-sync.timer")
    assert "OnUnitActiveSec=60" in unit("touchstone-heartbeat.timer")
    for job in ("touchstone-sync.service", "touchstone-heartbeat.service"):
        assert "EnvironmentFile=/etc/touchstone/env" in unit(job)


def test_deployment_settings_stay_out_of_git():
    ignored = (REPO / ".gitignore").read_text()
    assert "/deploy.env" in ignored
    assert "/data/" in ignored
    assert "/data-server/" in ignored


def pull(fakes, tmp_path, **extra):
    fakes.deploy_env.write_text("NTFY_TOPIC=topic\n")
    return subprocess.run(
        ["bash", str(REPO / "scripts" / "pull-data.sh")],
        env=fakes.env(**extra),
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )


def test_pull_data_copies_recordings_and_profiles_down(fakes, tmp_path):
    result = pull(fakes, tmp_path)
    assert result.returncode == 0, result.stderr
    syncs = [(c[2], c[3]) for c in fakes.calls_to("s3", "sync")]
    assert syncs == [
        ("s3://touchstone-recordings-123456789012/recordings/", "data-server/"),
        (
            "s3://touchstone-recordings-123456789012/latency-profiles/",
            "data-server/latency-profiles/",
        ),
    ]
    assert not (fakes.state / "curl.log").exists()


def test_failed_pull_pushes_an_alert(fakes, tmp_path):
    result = pull(fakes, tmp_path, FAKE_NO_CREDENTIALS="1")
    assert result.returncode != 0
    log = (fakes.state / "curl.log").read_text()
    assert "pull-data failed" in log
    assert "ntfy.sh/topic" in log
