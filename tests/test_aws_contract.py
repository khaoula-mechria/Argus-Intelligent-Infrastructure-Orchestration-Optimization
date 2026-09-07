"""Contract tests: do Argus's AWS calls match the real service models?

The hand-written fakes elsewhere in this suite check Argus's *logic*, but they
accept any parameter name, so a typo in ``create_stack`` would pass every one
of them and only fail against a real account. ``botocore.stub.Stubber``
validates both the request parameters and the response shape against the same
service model boto3 uses in production, which is the closest this suite can
get to the real API without a cloud account.

No network access and no credentials are involved: the client is built with
dummy values and every call is intercepted by the stubber.
"""

from __future__ import annotations

import os

import pytest
from botocore.stub import ANY, Stubber

from argus.adapters.base import UnitStatus
from argus.adapters.cloudformation import CloudFormationAdapter
from argus.aws import AwsSettings, build_client, call_with_backoff, is_throttling, poll_delays
from argus.optimizer import Optimizer

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES = os.path.join(REPO_ROOT, "infrastructure", "cloudformation")


@pytest.fixture
def session():
    """A boto3 session with dummy credentials. Nothing here reaches the network."""
    import boto3

    return boto3.session.Session(
        aws_access_key_id="test",
        aws_secret_access_key="test",
        region_name="eu-west-3",
    )


def make_client(session, service):
    return build_client(session, service, AwsSettings(region="eu-west-3"))


def one_unit(adapter, name="vpc"):
    return next(
        unit for unit in adapter.discover_units(TEMPLATES) if unit.name == name
    )


# -- CloudFormation ---------------------------------------------------------


def test_create_stack_parameters_match_the_service_model(session):
    client = make_client(session, "cloudformation")
    adapter = CloudFormationAdapter(
        {"ProjectName": "taskmanager", "Environment": "dev"}, client=client, poll_interval=0.01
    )
    unit = one_unit(adapter)

    with Stubber(client) as stubber:
        # The stack does not exist yet.
        stubber.add_client_error(
            "describe_stacks",
            service_error_code="ValidationError",
            service_message="Stack with id vpc does not exist",
        )
        stubber.add_response(
            "create_stack",
            {"StackId": "arn:aws:cloudformation:eu-west-3:1:stack/vpc/abc"},
            {
                "StackName": "vpc",
                "TemplateBody": ANY,
                "Parameters": [
                    {"ParameterKey": "Environment", "ParameterValue": "dev"},
                    {"ParameterKey": "ProjectName", "ParameterValue": "taskmanager"},
                ],
                "Capabilities": ["CAPABILITY_NAMED_IAM", "CAPABILITY_AUTO_EXPAND"],
                "OnFailure": "ROLLBACK",
            },
        )
        stubber.add_response(
            "describe_stacks",
            {"Stacks": [_stack("vpc", "CREATE_COMPLETE")]},
            {"StackName": "vpc"},
        )

        result = adapter.deploy_unit(unit)
        stubber.assert_no_pending_responses()

    assert result.status == UnitStatus.COMPLETE


def test_an_existing_stack_is_updated_not_recreated(session):
    client = make_client(session, "cloudformation")
    adapter = CloudFormationAdapter({}, client=client, poll_interval=0.01)
    unit = one_unit(adapter, "ecs-cluster")

    with Stubber(client) as stubber:
        stubber.add_response(
            "describe_stacks",
            {"Stacks": [_stack("ecs-cluster", "CREATE_COMPLETE")]},
            {"StackName": "ecs-cluster"},
        )
        stubber.add_response(
            "update_stack",
            {"StackId": "arn:aws:cloudformation:eu-west-3:1:stack/ecs-cluster/abc"},
            {
                "StackName": "ecs-cluster",
                "TemplateBody": ANY,
                "Parameters": [],
                "Capabilities": ANY,
            },
        )
        stubber.add_response(
            "describe_stacks",
            {"Stacks": [_stack("ecs-cluster", "UPDATE_COMPLETE")]},
            {"StackName": "ecs-cluster"},
        )

        result = adapter.deploy_unit(unit)
        stubber.assert_no_pending_responses()

    assert result.status == UnitStatus.COMPLETE


def test_a_stack_in_review_state_is_created_not_updated(session):
    # REVIEW_IN_PROGRESS means a change set exists but nothing was ever
    # deployed, so update_stack would fail with "does not exist".
    client = make_client(session, "cloudformation")
    adapter = CloudFormationAdapter({}, client=client, poll_interval=0.01)
    unit = one_unit(adapter, "ecs-cluster")

    with Stubber(client) as stubber:
        stubber.add_response(
            "describe_stacks",
            {"Stacks": [_stack("ecs-cluster", "REVIEW_IN_PROGRESS")]},
            {"StackName": "ecs-cluster"},
        )
        stubber.add_response(
            "create_stack",
            {"StackId": "x"},
            {
                "StackName": "ecs-cluster",
                "TemplateBody": ANY,
                "Parameters": [],
                "Capabilities": ANY,
                "OnFailure": "ROLLBACK",
            },
        )
        stubber.add_response(
            "describe_stacks",
            {"Stacks": [_stack("ecs-cluster", "CREATE_COMPLETE")]},
            {"StackName": "ecs-cluster"},
        )

        assert adapter.deploy_unit(unit).status == UnitStatus.COMPLETE
        stubber.assert_no_pending_responses()


def test_a_failure_reports_the_root_cause_resource_not_the_generic_reason(session):
    client = make_client(session, "cloudformation")
    adapter = CloudFormationAdapter({}, client=client, poll_interval=0.01)
    unit = one_unit(adapter, "ecs-cluster")

    with Stubber(client) as stubber:
        stubber.add_client_error("describe_stacks", service_error_code="ValidationError")
        stubber.add_response(
            "create_stack",
            {"StackId": "x"},
            {
                "StackName": "ecs-cluster",
                "TemplateBody": ANY,
                "Parameters": [],
                "Capabilities": ANY,
                "OnFailure": "ROLLBACK",
            },
        )
        stubber.add_response(
            "describe_stacks",
            {
                "Stacks": [
                    _stack(
                        "ecs-cluster",
                        "ROLLBACK_COMPLETE",
                        reason="The following resource(s) failed to create: [EcsCluster].",
                    )
                ]
            },
            {"StackName": "ecs-cluster"},
        )
        # Newest first, exactly as CloudFormation returns them.
        stubber.add_response(
            "describe_stack_events",
            {
                "StackEvents": [
                    _event("Cluster2", "CREATE_FAILED", "Resource creation cancelled"),
                    _event(
                        "EcsCluster",
                        "CREATE_FAILED",
                        "You have reached the limit of clusters per account",
                    ),
                ]
            },
            {"StackName": "ecs-cluster"},
        )

        result = adapter.deploy_unit(unit)
        stubber.assert_no_pending_responses()

    assert result.status == UnitStatus.FAILED
    # The actionable message, not "the following resource(s) failed to create".
    assert "limit of clusters" in result.detail
    assert "EcsCluster" in result.detail
    # The cancelled sibling is a symptom of the real failure, not the cause.
    assert "Cluster2" not in result.detail


def test_validate_template_is_called_once_per_unit(session):
    client = make_client(session, "cloudformation")
    adapter = CloudFormationAdapter({}, client=client)
    units = adapter.discover_units(TEMPLATES)[:3]

    with Stubber(client) as stubber:
        for _ in units:
            stubber.add_response("validate_template", {"Parameters": []}, {"TemplateBody": ANY})
        assert adapter.validate(units) == {}
        stubber.assert_no_pending_responses()


def test_a_malformed_template_is_reported_with_its_unit_name(session):
    client = make_client(session, "cloudformation")
    adapter = CloudFormationAdapter({}, client=client)
    units = adapter.discover_units(TEMPLATES)[:1]

    with Stubber(client) as stubber:
        stubber.add_client_error(
            "validate_template",
            service_error_code="ValidationError",
            service_message="Template format error: unsupported structure",
        )
        failures = adapter.validate(units)

    assert list(failures) == [units[0].name]
    assert "unsupported structure" in failures[units[0].name]


def test_get_unit_status_maps_a_real_status_string(session):
    client = make_client(session, "cloudformation")
    adapter = CloudFormationAdapter({}, client=client)
    unit = one_unit(adapter, "ecs-cluster")

    with Stubber(client) as stubber:
        stubber.add_response(
            "describe_stacks",
            {"Stacks": [_stack("ecs-cluster", "UPDATE_ROLLBACK_COMPLETE")]},
            {"StackName": "ecs-cluster"},
        )
        assert adapter.get_unit_status(unit) == UnitStatus.FAILED


def _stack(name, status, reason=None):
    from datetime import datetime, timezone

    stack = {
        "StackName": name,
        "CreationTime": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "StackStatus": status,
    }
    if reason:
        stack["StackStatusReason"] = reason
    return stack


def _event(logical_id, status, reason):
    from datetime import datetime, timezone

    return {
        "EventId": logical_id + "-" + status,
        "StackId": "arn:aws:cloudformation:eu-west-3:1:stack/s/abc",
        "StackName": "s",
        "Timestamp": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "LogicalResourceId": logical_id,
        "ResourceType": "AWS::ECS::Cluster",
        "ResourceStatus": status,
        "ResourceStatusReason": reason,
    }


# -- CloudWatch and ECS -----------------------------------------------------


def test_get_metric_statistics_parameters_match_the_service_model(session):
    cloudwatch = make_client(session, "cloudwatch")
    ecs = make_client(session, "ecs")
    optimizer = Optimizer(settings=AwsSettings(region="eu-west-3"))
    optimizer._clients = {"cloudwatch": cloudwatch, "ecs": ecs}

    with Stubber(ecs) as ecs_stub, Stubber(cloudwatch) as cw_stub:
        ecs_stub.add_response(
            "describe_services",
            {
                "services": [
                    {
                        "serviceArn": "arn:aws:ecs:eu-west-3:1:service/c/svc",
                        "desiredCount": 2,
                        "launchType": "FARGATE",
                        "taskDefinition": "arn:aws:ecs:eu-west-3:1:task-definition/td:7",
                    }
                ]
            },
            {"cluster": "taskmanager-dev", "services": ["svc"]},
        )
        ecs_stub.add_response(
            "describe_task_definition",
            {
                "taskDefinition": {
                    "cpu": "1024",
                    "memory": "2048",
                    "taskDefinitionArn": "arn:aws:ecs:eu-west-3:1:task-definition/td:7",
                }
            },
            {"taskDefinition": "arn:aws:ecs:eu-west-3:1:task-definition/td:7"},
        )
        for metric in ("CPUUtilization", "MemoryUtilization"):
            cw_stub.add_response(
                "get_metric_statistics",
                {"Label": metric, "Datapoints": []},
                {
                    "Namespace": "AWS/ECS",
                    "MetricName": metric,
                    "Dimensions": [
                        {"Name": "ClusterName", "Value": "taskmanager-dev"},
                        {"Name": "ServiceName", "Value": "svc"},
                    ],
                    "StartTime": ANY,
                    "EndTime": ANY,
                    "Period": 3600,
                    "Statistics": ["Average", "Maximum"],
                    "ExtendedStatistics": ["p95"],
                },
            )

        report = optimizer.analyse("svc", cluster="taskmanager-dev")
        ecs_stub.assert_no_pending_responses()
        cw_stub.assert_no_pending_responses()

    assert report.resource.attributes["cpu"] == 1024
    # No datapoints in the window: the rule must say so, not guess.
    assert report.argus.finding == "insufficient data"


def test_compute_optimizer_request_uses_the_documented_argument_name(session):
    cloudwatch = make_client(session, "cloudwatch")
    optimizer = Optimizer(settings=AwsSettings(region="eu-west-3"))
    compute_optimizer = make_client(session, "compute-optimizer")
    optimizer._clients = {"cloudwatch": cloudwatch, "compute-optimizer": compute_optimizer}

    arn = "arn:aws:ecs:eu-west-3:1:service/c/svc"
    with Stubber(compute_optimizer) as stub:
        stub.add_response(
            "get_ecs_service_recommendations",
            {"ecsServiceRecommendations": []},
            {"serviceArns": [arn]},
        )
        notes: list[str] = []
        from argus.optimizer import RESOURCE_KINDS

        assert optimizer._compute_optimizer(RESOURCE_KINDS["ecs-service"], arn, notes) is None
        stub.assert_no_pending_responses()

    assert any("14 days" in note for note in notes)


# -- the AWS helper layer ---------------------------------------------------


def test_clients_carry_the_argus_user_agent(session):
    client = make_client(session, "cloudformation")
    assert "argus/" in client.meta.config.user_agent_extra


def test_clients_use_adaptive_retries(session):
    client = make_client(session, "cloudformation")
    assert client.meta.config.retries["mode"] == "adaptive"


def test_endpoint_url_is_honoured(session):
    client = build_client(
        session, "cloudformation", AwsSettings(endpoint_url="http://localhost:4566")
    )
    assert client.meta.endpoint_url == "http://localhost:4566"


def test_throttling_is_recognised_from_the_error_code():
    class Throttled(Exception):
        response = {"Error": {"Code": "Throttling", "Message": "Rate exceeded"}}

    class NotFound(Exception):
        response = {"Error": {"Code": "ValidationError", "Message": "no such stack"}}

    assert is_throttling(Throttled())
    assert not is_throttling(NotFound())


def test_backoff_retries_throttling_then_succeeds():
    calls = {"n": 0}
    slept: list[float] = []

    class Throttled(Exception):
        response = {"Error": {"Code": "Throttling"}}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise Throttled()
        return "ok"

    result = call_with_backoff(
        flaky, base_delay=0.01, sleep=slept.append, jitter=lambda: 1.0
    )

    assert result == "ok"
    assert calls["n"] == 3
    # Exponential, not constant: a flat retry into a throttled service makes it worse.
    assert slept[1] > slept[0]


def test_backoff_does_not_retry_a_client_error():
    class Invalid(Exception):
        response = {"Error": {"Code": "ValidationError"}}

    calls = {"n": 0}

    def failing():
        calls["n"] += 1
        raise Invalid()

    with pytest.raises(Invalid):
        call_with_backoff(failing, sleep=lambda _: None)
    assert calls["n"] == 1


def test_poll_delays_are_jittered_and_respect_the_timeout():
    delays = list(poll_delays(10.0, 45.0, jitter=lambda: 0.5))
    assert sum(delays) == pytest.approx(45.0)
    # Jittered around the nominal interval, never exactly equal to it, so
    # parallel pollers do not all call describe_stacks on the same tick.
    assert all(7.5 <= delay <= 12.5 for delay in delays[:-1])
