"""Module 2 -- rightsizing analysis.

Argus reads the CloudWatch metrics of one resource, applies an explicit and
auditable sizing rule to them, and puts its own verdict next to what AWS
Compute Optimizer says about the same resource. The two are reported side by
side on purpose: Compute Optimizer needs 14 days of history and stays silent
below that, while the local rule always produces something. Neither is
presented as the truth -- the disagreement is the interesting part.

Nothing here changes any infrastructure. Applying a recommendation is a
separate, explicitly confirmed step.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

DEFAULT_PERIOD_DAYS = 14
DEFAULT_REGION_HINT = "set --region or AWS_REGION"

#: Below this p95 utilisation a resource is considered over-provisioned.
UNDERUSED_THRESHOLD = 40.0
#: Above this p95 utilisation it is considered at risk of throttling.
SATURATED_THRESHOLD = 80.0


class OptimizerError(RuntimeError):
    """Raised when the resource cannot be read or is not supported."""


@dataclass
class MetricSample:
    """One CloudWatch metric summarised over the analysis window."""

    name: str
    unit: str
    average: float | None = None
    maximum: float | None = None
    p95: float | None = None
    datapoints: int = 0

    @property
    def is_usable(self) -> bool:
        return self.datapoints > 0 and self.p95 is not None


@dataclass
class ResourceMetrics:
    resource_id: str
    resource_type: str
    region: str
    period_days: int
    metrics: dict[str, MetricSample] = field(default_factory=dict)
    attributes: dict[str, Any] = field(default_factory=dict)

    def value(self, name: str) -> float | None:
        sample = self.metrics.get(name)
        return sample.p95 if sample and sample.is_usable else None


@dataclass
class Recommendation:
    """A sizing verdict, from Argus or from Compute Optimizer."""

    source: str
    finding: str
    rationale: str
    current: dict[str, Any] = field(default_factory=dict)
    proposed: dict[str, Any] = field(default_factory=dict)
    estimated_monthly_saving: float | None = None

    @property
    def is_change(self) -> bool:
        return bool(self.proposed) and self.proposed != self.current


@dataclass
class OptimizationReport:
    resource: ResourceMetrics
    argus: Recommendation
    aws: Recommendation | None
    notes: list[str] = field(default_factory=list)

    @property
    def agreement(self) -> str:
        """Whether the local rule and Compute Optimizer point the same way."""
        if self.aws is None:
            return "no AWS recommendation available"
        if self.argus.finding == self.aws.finding:
            return "agree: " + self.argus.finding
        return "disagree: Argus says " + self.argus.finding + ", AWS says " + self.aws.finding

    def summary(self) -> str:
        lines = [
            self.resource.resource_type + " " + self.resource.resource_id
            + " in " + self.resource.region,
            "window: last " + str(self.resource.period_days) + " days",
            "",
            "metrics (p95 over the window):",
        ]
        for sample in self.resource.metrics.values():
            if sample.is_usable:
                lines.append(
                    "  %-24s p95=%8.2f %-8s avg=%8.2f  max=%8.2f  (%d datapoints)"
                    % (sample.name, sample.p95, sample.unit, sample.average or 0.0,
                       sample.maximum or 0.0, sample.datapoints)
                )
            else:
                lines.append("  %-24s no datapoints in the window" % sample.name)

        lines += ["", "Argus rule: " + self.argus.finding, "  " + self.argus.rationale]
        if self.argus.is_change:
            lines.append("  current : " + _render(self.argus.current))
            lines.append("  proposed: " + _render(self.argus.proposed))

        if self.aws is not None:
            lines += ["", "AWS Compute Optimizer: " + self.aws.finding, "  " + self.aws.rationale]
            if self.aws.is_change:
                lines.append("  proposed: " + _render(self.aws.proposed))
            if self.aws.estimated_monthly_saving:
                lines.append("  estimated monthly saving: $%.2f" % self.aws.estimated_monthly_saving)

        lines += ["", "verdict: " + self.agreement]
        for note in self.notes:
            lines.append("note: " + note)
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        """Plain structure handed to the explainer and to the dashboard."""
        return {
            "resource": {
                "id": self.resource.resource_id,
                "type": self.resource.resource_type,
                "region": self.resource.region,
                "period_days": self.resource.period_days,
                "attributes": self.resource.attributes,
            },
            "metrics": {
                name: {
                    "unit": sample.unit,
                    "average": sample.average,
                    "maximum": sample.maximum,
                    "p95": sample.p95,
                    "datapoints": sample.datapoints,
                }
                for name, sample in self.resource.metrics.items()
            },
            "argus": _recommendation_dict(self.argus),
            "aws": _recommendation_dict(self.aws) if self.aws else None,
            "agreement": self.agreement,
            "notes": self.notes,
        }


def _recommendation_dict(recommendation: Recommendation) -> dict[str, Any]:
    return {
        "source": recommendation.source,
        "finding": recommendation.finding,
        "rationale": recommendation.rationale,
        "current": recommendation.current,
        "proposed": recommendation.proposed,
        "estimated_monthly_saving": recommendation.estimated_monthly_saving,
    }


def _render(values: dict[str, Any]) -> str:
    return ", ".join(str(key) + "=" + str(value) for key, value in sorted(values.items()))


# ---------------------------------------------------------------------------
# Resource kinds
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResourceKind:
    """How to read one class of resource, and whether Argus can size it.

    ``sizable`` marks the kinds where the CPU/memory rule below actually
    applies. EBS is deliberately not sizable: its rightsizing question is
    about IOPS and throughput, not utilisation percentages, and pretending
    otherwise would produce confident nonsense.
    """

    name: str
    namespace: str
    dimension: str
    metrics: tuple[tuple[str, str], ...]
    compute_optimizer_call: str
    arn_field: str
    sizable: bool = True


RESOURCE_KINDS: dict[str, ResourceKind] = {
    "ecs-service": ResourceKind(
        name="ecs-service",
        namespace="AWS/ECS",
        dimension="ServiceName",
        metrics=(("CPUUtilization", "Percent"), ("MemoryUtilization", "Percent")),
        compute_optimizer_call="get_ecs_service_recommendations",
        arn_field="serviceArns",
    ),
    "ec2-instance": ResourceKind(
        name="ec2-instance",
        namespace="AWS/EC2",
        dimension="InstanceId",
        metrics=(("CPUUtilization", "Percent"),),
        compute_optimizer_call="get_ec2_instance_recommendations",
        arn_field="instanceArns",
    ),
    "rds-instance": ResourceKind(
        name="rds-instance",
        namespace="AWS/RDS",
        dimension="DBInstanceIdentifier",
        metrics=(("CPUUtilization", "Percent"), ("FreeableMemory", "Bytes")),
        compute_optimizer_call="get_rds_database_recommendations",
        arn_field="resourceArns",
    ),
    "ebs-volume": ResourceKind(
        name="ebs-volume",
        namespace="AWS/EBS",
        dimension="VolumeId",
        metrics=(("VolumeReadOps", "Count"), ("VolumeWriteOps", "Count")),
        compute_optimizer_call="get_ebs_volume_recommendations",
        arn_field="volumeArns",
        sizable=False,
    ),
}

#: Valid Fargate CPU/memory pairs, ordered from smallest to largest. A task
#: definition that does not use a listed pair is rejected by ECS, so the rule
#: below can only ever move along this ladder.
FARGATE_LADDER: list[tuple[int, int]] = [
    (256, 512), (256, 1024), (256, 2048),
    (512, 1024), (512, 2048), (512, 3072), (512, 4096),
    (1024, 2048), (1024, 3072), (1024, 4096), (1024, 6144), (1024, 8192),
    (2048, 4096), (2048, 8192), (2048, 12288), (2048, 16384),
    (4096, 8192), (4096, 16384), (4096, 30720),
]

#: EC2 size suffixes within a family, smallest first.
EC2_SIZE_LADDER = [
    "nano", "micro", "small", "medium", "large",
    "xlarge", "2xlarge", "4xlarge", "8xlarge", "12xlarge", "16xlarge", "24xlarge",
]


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------


def apply_sizing_rule(resource: ResourceMetrics) -> Recommendation:
    """Argus's own verdict: one threshold rule, stated in the output.

    The rule is deliberately simple so a reader can check it by hand:
    if the 95th percentile of every utilisation metric sits below
    UNDERUSED_THRESHOLD, the resource is over-provisioned and moves one step
    down the ladder; if any metric exceeds SATURATED_THRESHOLD it moves one
    step up; otherwise it stays. It is not a substitute for Compute
    Optimizer's model, it is a second opinion that also works on a resource
    too young for Compute Optimizer to have an opinion at all.
    """
    kind = RESOURCE_KINDS[resource.resource_type]

    if not kind.sizable:
        return Recommendation(
            source="argus",
            finding="not evaluated",
            rationale=(
                "Argus has no sizing rule for " + kind.name + ": rightsizing a volume "
                "depends on IOPS and throughput headroom rather than on utilisation "
                "percentages. The AWS recommendation below is the one to read."
            ),
        )

    utilisation = {
        name: resource.value(name)
        for name, unit in kind.metrics
        if unit == "Percent"
    }
    observed = {name: value for name, value in utilisation.items() if value is not None}

    if not observed:
        return Recommendation(
            source="argus",
            finding="insufficient data",
            rationale=(
                "CloudWatch returned no datapoint for "
                + ", ".join(sorted(utilisation)) + " over the last "
                + str(resource.period_days) + " days. A service that has never run, or "
                "a metric that is not published, cannot be sized."
            ),
        )

    highest = max(observed.values())
    detail = ", ".join(name + " p95=" + ("%.1f%%" % value) for name, value in sorted(observed.items()))

    if highest > SATURATED_THRESHOLD:
        current, proposed = _step(resource, direction=+1)
        return Recommendation(
            source="argus",
            finding="under-provisioned",
            rationale=(
                detail + ", above the " + str(SATURATED_THRESHOLD) + "% saturation threshold. "
                "One step up the ladder."
            ),
            current=current,
            proposed=proposed,
        )

    if highest < UNDERUSED_THRESHOLD:
        current, proposed = _step(resource, direction=-1)
        return Recommendation(
            source="argus",
            finding="over-provisioned",
            rationale=(
                detail + ", every metric below the " + str(UNDERUSED_THRESHOLD)
                + "% threshold. One step down the ladder."
            ),
            current=current,
            proposed=proposed,
        )

    return Recommendation(
        source="argus",
        finding="correctly sized",
        rationale=(
            detail + ", between the " + str(UNDERUSED_THRESHOLD) + "% and "
            + str(SATURATED_THRESHOLD) + "% thresholds. No change."
        ),
    )


def _step(resource: ResourceMetrics, direction: int) -> tuple[dict[str, Any], dict[str, Any]]:
    """Move one rung along the ladder appropriate to the resource kind."""
    if resource.resource_type == "ecs-service":
        cpu = resource.attributes.get("cpu")
        memory = resource.attributes.get("memory")
        current = {"cpu": cpu, "memory": memory}
        if cpu is None or memory is None:
            return current, {}
        try:
            index = FARGATE_LADDER.index((int(cpu), int(memory)))
        except ValueError:
            # A task definition outside the documented Fargate pairs: report the
            # direction without inventing a specific target.
            return current, {}
        target = min(max(index + direction, 0), len(FARGATE_LADDER) - 1)
        if target == index:
            return current, {}
        new_cpu, new_memory = FARGATE_LADDER[target]
        return current, {"cpu": new_cpu, "memory": new_memory}

    if resource.resource_type in ("ec2-instance", "rds-instance"):
        instance_type = resource.attributes.get("instance_type")
        current = {"instance_type": instance_type}
        if not instance_type or "." not in instance_type:
            return current, {}
        family, size = instance_type.rsplit(".", 1)
        if size not in EC2_SIZE_LADDER:
            return current, {}
        index = EC2_SIZE_LADDER.index(size)
        target = min(max(index + direction, 0), len(EC2_SIZE_LADDER) - 1)
        if target == index:
            return current, {}
        return current, {"instance_type": family + "." + EC2_SIZE_LADDER[target]}

    return {}, {}


# ---------------------------------------------------------------------------
# AWS access
# ---------------------------------------------------------------------------


class Optimizer:
    """Reads CloudWatch and Compute Optimizer for one resource."""

    def __init__(self, region: str | None = None, session: Any = None, period_days: int = DEFAULT_PERIOD_DAYS):
        self.region = region
        self.period_days = period_days
        self._session = session
        self._clients: dict[str, Any] = {}

    def client(self, service: str):
        if service not in self._clients:
            if self._session is None:
                import boto3

                self._session = boto3.session.Session(region_name=self.region)
            self._clients[service] = self._session.client(service)
        return self._clients[service]

    @property
    def resolved_region(self) -> str:
        return self.region or getattr(self._session, "region_name", None) or DEFAULT_REGION_HINT

    # -- entry point -------------------------------------------------------

    def analyse(
        self,
        identifier: str,
        resource_type: str = "ecs-service",
        cluster: str | None = None,
    ) -> OptimizationReport:
        if resource_type not in RESOURCE_KINDS:
            raise OptimizerError(
                "unsupported resource type '" + resource_type + "'; expected one of "
                + ", ".join(sorted(RESOURCE_KINDS))
            )
        kind = RESOURCE_KINDS[resource_type]
        notes: list[str] = []

        dimensions, attributes, arn = self._describe(kind, identifier, cluster, notes)

        resource = ResourceMetrics(
            resource_id=identifier,
            resource_type=resource_type,
            region=self.resolved_region,
            period_days=self.period_days,
            attributes=attributes,
        )
        for metric_name, unit in kind.metrics:
            resource.metrics[metric_name] = self._read_metric(kind, metric_name, unit, dimensions)

        argus = apply_sizing_rule(resource)
        aws = self._compute_optimizer(kind, arn, notes) if arn else None
        if arn is None:
            notes.append(
                "no ARN could be resolved for this resource, so Compute Optimizer was not queried"
            )

        return OptimizationReport(resource=resource, argus=argus, aws=aws, notes=notes)

    # -- describe ----------------------------------------------------------

    def _describe(
        self, kind: ResourceKind, identifier: str, cluster: str | None, notes: list[str]
    ) -> tuple[list[dict[str, str]], dict[str, Any], str | None]:
        """Resolve the CloudWatch dimensions, the current size, and the ARN."""
        if kind.name == "ecs-service":
            return self._describe_ecs_service(identifier, cluster, notes)

        if kind.name == "ec2-instance":
            dimensions = [{"Name": "InstanceId", "Value": identifier}]
            try:
                reservations = self.client("ec2").describe_instances(InstanceIds=[identifier])
                instance = reservations["Reservations"][0]["Instances"][0]
                attributes = {"instance_type": instance.get("InstanceType")}
                arn = "arn:aws:ec2:" + self.resolved_region + ":" + instance.get("OwnerId", "") \
                    + ":instance/" + identifier
            except Exception as exc:
                notes.append("could not describe the instance: " + str(exc))
                attributes, arn = {}, None
            return dimensions, attributes, arn

        if kind.name == "rds-instance":
            dimensions = [{"Name": "DBInstanceIdentifier", "Value": identifier}]
            try:
                instances = self.client("rds").describe_db_instances(DBInstanceIdentifier=identifier)
                instance = instances["DBInstances"][0]
                attributes = {"instance_type": instance.get("DBInstanceClass")}
                arn = instance.get("DBInstanceArn")
            except Exception as exc:
                notes.append("could not describe the database: " + str(exc))
                attributes, arn = {}, None
            return dimensions, attributes, arn

        # ebs-volume
        dimensions = [{"Name": "VolumeId", "Value": identifier}]
        try:
            volumes = self.client("ec2").describe_volumes(VolumeIds=[identifier])
            volume = volumes["Volumes"][0]
            attributes = {"volume_type": volume.get("VolumeType"), "size_gib": volume.get("Size")}
            arn = "arn:aws:ec2:" + self.resolved_region + "::volume/" + identifier
        except Exception as exc:
            notes.append("could not describe the volume: " + str(exc))
            attributes, arn = {}, None
        return dimensions, attributes, arn

    def _describe_ecs_service(
        self, identifier: str, cluster: str | None, notes: list[str]
    ) -> tuple[list[dict[str, str]], dict[str, Any], str | None]:
        ecs = self.client("ecs")

        if cluster is None:
            cluster = self._find_cluster(ecs, identifier, notes)
        if cluster is None:
            raise OptimizerError(
                "service '" + identifier + "' was not found in any ECS cluster in "
                + self.resolved_region + "; pass --cluster explicitly"
            )

        dimensions = [
            {"Name": "ClusterName", "Value": _short_name(cluster)},
            {"Name": "ServiceName", "Value": identifier},
        ]

        attributes: dict[str, Any] = {"cluster": _short_name(cluster)}
        arn = None
        try:
            described = ecs.describe_services(cluster=cluster, services=[identifier])["services"][0]
            arn = described.get("serviceArn")
            attributes["desired_count"] = described.get("desiredCount")
            attributes["launch_type"] = described.get("launchType")
            task_definition = described.get("taskDefinition")
            if task_definition:
                definition = ecs.describe_task_definition(taskDefinition=task_definition)["taskDefinition"]
                # Fargate stores the size on the task definition as strings.
                attributes["cpu"] = _as_int(definition.get("cpu"))
                attributes["memory"] = _as_int(definition.get("memory"))
                attributes["task_definition"] = definition.get("taskDefinitionArn")
        except Exception as exc:
            notes.append("could not describe the ECS service: " + str(exc))

        return dimensions, attributes, arn

    def _find_cluster(self, ecs, service_name: str, notes: list[str]) -> str | None:
        """Locate the cluster holding ``service_name``, so --cluster is optional."""
        try:
            paginator = ecs.get_paginator("list_clusters")
            for page in paginator.paginate():
                for cluster_arn in page.get("clusterArns", []):
                    services = ecs.list_services(cluster=cluster_arn).get("serviceArns", [])
                    if any(arn.endswith("/" + service_name) for arn in services):
                        return cluster_arn
        except Exception as exc:
            notes.append("could not list ECS clusters: " + str(exc))
        return None

    # -- CloudWatch --------------------------------------------------------

    def _read_metric(
        self, kind: ResourceKind, metric_name: str, unit: str, dimensions: list[dict[str, str]]
    ) -> MetricSample:
        sample = MetricSample(name=metric_name, unit=unit)
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=self.period_days)

        try:
            response = self.client("cloudwatch").get_metric_statistics(
                Namespace=kind.namespace,
                MetricName=metric_name,
                Dimensions=dimensions,
                StartTime=start,
                EndTime=end,
                # One hour buckets: 14 days fit well under the 1440-datapoint
                # cap while still showing a daily peak.
                Period=3600,
                Statistics=["Average", "Maximum"],
                ExtendedStatistics=["p95"],
            )
        except Exception:
            return sample

        datapoints = response.get("Datapoints", [])
        sample.datapoints = len(datapoints)
        if not datapoints:
            return sample

        averages = [point["Average"] for point in datapoints if "Average" in point]
        maxima = [point["Maximum"] for point in datapoints if "Maximum" in point]
        percentiles = [
            point["ExtendedStatistics"]["p95"]
            for point in datapoints
            if point.get("ExtendedStatistics", {}).get("p95") is not None
        ]

        sample.average = sum(averages) / len(averages) if averages else None
        sample.maximum = max(maxima) if maxima else None
        # p95 of the hourly p95s: the level the resource sustains at its busiest,
        # without letting one spike drive the decision the way Maximum would.
        sample.p95 = _percentile(percentiles, 95) if percentiles else sample.average
        return sample

    # -- Compute Optimizer -------------------------------------------------

    def _compute_optimizer(self, kind: ResourceKind, arn: str, notes: list[str]) -> Recommendation | None:
        try:
            client = self.client("compute-optimizer")
            call = getattr(client, kind.compute_optimizer_call)
            response = call(**{kind.arn_field: [arn]})
        except Exception as exc:
            notes.append("Compute Optimizer could not be queried: " + str(exc))
            return None

        entries = _first_list(response)
        if not entries:
            notes.append(
                "Compute Optimizer returned no recommendation. It needs about 14 days of "
                "metrics before it says anything, and it stays silent on resources it has "
                "not yet analysed."
            )
            return None

        entry = entries[0]
        finding = str(entry.get("finding", "UNKNOWN")).lower()
        options = entry.get("recommendationOptions") or []
        proposed: dict[str, Any] = {}
        saving: float | None = None

        if options:
            best = options[0]
            for key in ("instanceType", "dbInstanceClass", "volumeType"):
                if key in best:
                    proposed["instance_type" if key != "volumeType" else "volume_type"] = best[key]
            container = (best.get("containerRecommendations") or [{}])[0]
            if container:
                proposed["cpu"] = container.get("cpu")
                proposed["memory"] = container.get("memory")
            estimate = best.get("savingsOpportunity") or {}
            saving = (estimate.get("estimatedMonthlySavings") or {}).get("value")

        return Recommendation(
            source="aws-compute-optimizer",
            finding=_translate_finding(finding),
            rationale="Compute Optimizer reports finding '" + finding + "' for this resource.",
            proposed={key: value for key, value in proposed.items() if value is not None},
            estimated_monthly_saving=saving,
        )


def _translate_finding(finding: str) -> str:
    """Map the per-service Compute Optimizer vocabulary onto Argus's wording."""
    return {
        "over_provisioned": "over-provisioned",
        "overprovisioned": "over-provisioned",
        "under_provisioned": "under-provisioned",
        "underprovisioned": "under-provisioned",
        "optimized": "correctly sized",
        "not_optimized": "over-provisioned",
        "unavailable": "insufficient data",
    }.get(finding, finding)


def _first_list(response: dict[str, Any]) -> list[Any]:
    """Pull the recommendation list out, whatever the per-service key is called."""
    for key, value in response.items():
        if key != "ResponseMetadata" and isinstance(value, list):
            return value
    return []


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("no values")
    if len(ordered) == 1:
        return ordered[0]
    rank = (percentile / 100.0) * (len(ordered) - 1)
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    weight = rank - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _short_name(cluster: str) -> str:
    """CloudWatch dimensions want the cluster name, describe_services takes an ARN."""
    return cluster.rsplit("/", 1)[-1]


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
