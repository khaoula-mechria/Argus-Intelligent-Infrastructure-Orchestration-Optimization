"""Tests for the rightsizing analysis, driven by stubbed AWS clients."""

from __future__ import annotations

import pytest

from argus.optimizer import (
    FARGATE_LADDER,
    MetricSample,
    Optimizer,
    OptimizerError,
    ResourceMetrics,
    _percentile,
    _translate_finding,
    apply_sizing_rule,
)


def metrics(resource_type="ecs-service", attributes=None, **values) -> ResourceMetrics:
    resource = ResourceMetrics(
        resource_id="svc",
        resource_type=resource_type,
        region="eu-west-3",
        period_days=14,
        attributes=attributes or {},
    )
    for name, p95 in values.items():
        resource.metrics[name] = MetricSample(
            name=name, unit="Percent", average=p95, maximum=p95, p95=p95, datapoints=336
        )
    return resource


# -- the rule ---------------------------------------------------------------


def test_low_utilisation_steps_one_rung_down_the_fargate_ladder():
    resource = metrics(
        attributes={"cpu": 1024, "memory": 2048}, CPUUtilization=12.0, MemoryUtilization=20.0
    )
    recommendation = apply_sizing_rule(resource)

    assert recommendation.finding == "over-provisioned"
    assert recommendation.current == {"cpu": 1024, "memory": 2048}
    # One rung down from (1024, 2048), not an arbitrary smaller pair.
    index = FARGATE_LADDER.index((1024, 2048))
    expected_cpu, expected_memory = FARGATE_LADDER[index - 1]
    assert recommendation.proposed == {"cpu": expected_cpu, "memory": expected_memory}


def test_high_utilisation_steps_one_rung_up():
    resource = metrics(
        attributes={"cpu": 512, "memory": 1024}, CPUUtilization=91.0, MemoryUtilization=40.0
    )
    recommendation = apply_sizing_rule(resource)

    assert recommendation.finding == "under-provisioned"
    index = FARGATE_LADDER.index((512, 1024))
    assert recommendation.proposed == dict(zip(("cpu", "memory"), FARGATE_LADDER[index + 1]))


def test_one_saturated_metric_outweighs_a_quiet_one():
    # Memory at 95% must not be averaged away by CPU at 5%.
    resource = metrics(
        attributes={"cpu": 512, "memory": 1024}, CPUUtilization=5.0, MemoryUtilization=95.0
    )
    assert apply_sizing_rule(resource).finding == "under-provisioned"


def test_mid_range_utilisation_proposes_nothing():
    resource = metrics(
        attributes={"cpu": 512, "memory": 1024}, CPUUtilization=55.0, MemoryUtilization=60.0
    )
    recommendation = apply_sizing_rule(resource)

    assert recommendation.finding == "correctly sized"
    assert not recommendation.is_change


def test_a_size_outside_the_fargate_ladder_yields_a_finding_but_no_target():
    # ECS rejects unlisted CPU/memory pairs, so guessing a target would be wrong.
    resource = metrics(attributes={"cpu": 777, "memory": 999}, CPUUtilization=5.0)
    recommendation = apply_sizing_rule(resource)

    assert recommendation.finding == "over-provisioned"
    assert recommendation.proposed == {}


def test_the_smallest_size_does_not_step_below_the_ladder():
    smallest_cpu, smallest_memory = FARGATE_LADDER[0]
    resource = metrics(
        attributes={"cpu": smallest_cpu, "memory": smallest_memory}, CPUUtilization=1.0
    )
    assert apply_sizing_rule(resource).proposed == {}


def test_no_datapoints_gives_insufficient_data_not_a_guess():
    resource = ResourceMetrics("svc", "ecs-service", "eu-west-3", 14)
    resource.metrics["CPUUtilization"] = MetricSample("CPUUtilization", "Percent", datapoints=0)
    recommendation = apply_sizing_rule(resource)

    assert recommendation.finding == "insufficient data"
    assert not recommendation.is_change


def test_ebs_volumes_are_not_sized_by_the_cpu_rule():
    resource = metrics(resource_type="ebs-volume", attributes={"volume_type": "gp3"})
    recommendation = apply_sizing_rule(resource)

    assert recommendation.finding == "not evaluated"
    assert "IOPS" in recommendation.rationale


def test_ec2_steps_within_its_family():
    resource = metrics(
        resource_type="ec2-instance", attributes={"instance_type": "m5.large"}, CPUUtilization=8.0
    )
    assert apply_sizing_rule(resource).proposed == {"instance_type": "m5.medium"}


# -- helpers ----------------------------------------------------------------


def test_percentile_interpolates():
    assert _percentile([10.0, 20.0], 50) == pytest.approx(15.0)
    assert _percentile([5.0], 95) == 5.0


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("over_provisioned", "over-provisioned"),
        ("optimized", "correctly sized"),
        ("unavailable", "insufficient data"),
        ("something_new", "something_new"),
    ],
)
def test_compute_optimizer_findings_are_translated(raw, expected):
    assert _translate_finding(raw) == expected


# -- end to end with stubbed clients ----------------------------------------


class FakeClient:
    def __init__(self, responses):
        self._responses = responses

    def __getattr__(self, name):
        if name not in self._responses:
            raise AttributeError(name)
        value = self._responses[name]
        return value if callable(value) else (lambda **kwargs: value)


class FakeSession:
    region_name = "eu-west-3"

    def __init__(self, clients):
        self._clients = clients

    def client(self, service):
        return self._clients[service]


def datapoints(p95):
    return {
        "Datapoints": [
            {"Average": p95, "Maximum": p95 + 5, "ExtendedStatistics": {"p95": p95}}
            for _ in range(24)
        ]
    }


def build_optimizer(finding="OVER_PROVISIONED", cpu_p95=10.0, memory_p95=15.0):
    per_metric = {"CPUUtilization": datapoints(cpu_p95), "MemoryUtilization": datapoints(memory_p95)}
    cloudwatch = FakeClient(
        {"get_metric_statistics": lambda **kwargs: per_metric[kwargs["MetricName"]]}
    )
    ecs = FakeClient(
        {
            "describe_services": {
                "services": [
                    {
                        "serviceArn": "arn:aws:ecs:eu-west-3:1:service/taskmanager-dev/svc",
                        "desiredCount": 2,
                        "launchType": "FARGATE",
                        "taskDefinition": "arn:aws:ecs:eu-west-3:1:task-definition/td:7",
                    }
                ]
            },
            "describe_task_definition": {
                "taskDefinition": {
                    "cpu": "1024",
                    "memory": "2048",
                    "taskDefinitionArn": "arn:aws:ecs:eu-west-3:1:task-definition/td:7",
                }
            },
        }
    )
    compute_optimizer = FakeClient(
        {
            "get_ecs_service_recommendations": {
                "ecsServiceRecommendations": [
                    {
                        "finding": finding,
                        "recommendationOptions": [
                            {
                                "containerRecommendations": [{"cpu": 512, "memory": 1024}],
                                "savingsOpportunity": {
                                    "estimatedMonthlySavings": {"value": 18.40}
                                },
                            }
                        ],
                    }
                ]
            }
        }
    )
    session = FakeSession(
        {"cloudwatch": cloudwatch, "ecs": ecs, "compute-optimizer": compute_optimizer}
    )
    return Optimizer(region="eu-west-3", session=session)


def test_end_to_end_report_puts_both_verdicts_side_by_side():
    report = build_optimizer().analyse("svc", cluster="taskmanager-dev")

    assert report.resource.attributes["cpu"] == 1024
    assert report.argus.finding == "over-provisioned"
    assert report.aws is not None
    assert report.aws.finding == "over-provisioned"
    assert report.aws.proposed == {"cpu": 512, "memory": 1024}
    assert report.aws.estimated_monthly_saving == pytest.approx(18.40)
    assert report.agreement == "agree: over-provisioned"


def test_disagreement_is_reported_rather_than_resolved():
    # The local rule sees 10% CPU over 14 days; Compute Optimizer, with a longer
    # history and a cost model, says the service is fine. Argus reports both.
    report = build_optimizer(finding="OPTIMIZED").analyse("svc", cluster="taskmanager-dev")

    assert report.argus.finding == "over-provisioned"
    assert report.aws.finding == "correctly sized"
    assert report.agreement.startswith("disagree")


def test_a_silent_compute_optimizer_is_a_note_not_a_crash():
    optimizer = build_optimizer()
    optimizer._clients["compute-optimizer"] = FakeClient(
        {"get_ecs_service_recommendations": {"ecsServiceRecommendations": []}}
    )
    report = optimizer.analyse("svc", cluster="taskmanager-dev")

    assert report.aws is None
    assert any("14 days" in note for note in report.notes)
    assert report.argus.finding == "over-provisioned"  # the local rule still works


def test_cloudwatch_dimensions_use_the_short_cluster_name():
    seen = {}

    optimizer = build_optimizer()

    def capture(**kwargs):
        seen.update(kwargs)
        return datapoints(10.0)

    optimizer._clients["cloudwatch"] = FakeClient({"get_metric_statistics": capture})
    optimizer.analyse("svc", cluster="arn:aws:ecs:eu-west-3:1:cluster/taskmanager-dev")

    assert {"Name": "ClusterName", "Value": "taskmanager-dev"} in seen["Dimensions"]


def test_unsupported_resource_type_is_rejected():
    with pytest.raises(OptimizerError, match="unsupported resource type"):
        Optimizer(session=FakeSession({})).analyse("x", resource_type="lambda-function")


def test_report_serialises_for_the_explainer():
    payload = build_optimizer().analyse("svc", cluster="taskmanager-dev").to_dict()

    assert payload["resource"]["type"] == "ecs-service"
    assert payload["argus"]["finding"] == "over-provisioned"
    assert payload["metrics"]["CPUUtilization"]["datapoints"] == 24
