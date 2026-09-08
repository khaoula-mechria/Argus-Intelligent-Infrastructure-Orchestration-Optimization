"""Module 3 -- turning an optimization report into plain language.

The explainer takes the structured output of Module 2 and asks Claude to
explain it: what the numbers mean, whether the two verdicts agree, what the
risk of acting is, and what to check first. It never changes anything, and it
is never the thing that decides -- the numbers in the report are.

Applying a recommendation stays a separate, explicitly confirmed step
(``argus explain --apply``), which prints the exact commands for a human to
run rather than running them.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

#: Anthropic's most capable model; the reasoning here is short but the cost of
#: a confidently wrong infrastructure explanation is high.
DEFAULT_MODEL = "claude-opus-5"
DEFAULT_MAX_TOKENS = 4000

SYSTEM_PROMPT = """You explain AWS rightsizing analyses to an engineer who owns the \
infrastructure but is not a capacity-planning specialist.

You will be given a JSON report containing:
- the resource and its current size,
- CloudWatch metrics summarised over an analysis window,
- a verdict from a simple local threshold rule ("argus"),
- a verdict from AWS Compute Optimizer ("aws"), which may be absent,
- notes recording anything that could not be read.

Write a short explanation covering, in this order:
1. What the metrics actually say about how the resource is used.
2. Whether the two verdicts agree, and if they disagree, why that is plausible
   (the local rule uses a fixed threshold over a short window; Compute
   Optimizer uses a longer history and a cost model).
3. The concrete risk of applying the proposed change, and the risk of leaving
   it alone.
4. What to verify before acting.

Rules:
- Ground every claim in a number from the report. If the report does not
  support a claim, do not make it.
- If the data is thin (few datapoints, no Compute Optimizer recommendation),
  say so plainly and say the recommendation is weak. Do not fill the gap with
  generalities about AWS best practice.
- Never tell the reader to apply the change automatically. A human decides.
- Be concise: at most six short paragraphs, no headings, no bullet lists."""


class ExplainerError(RuntimeError):
    """Raised when the explanation cannot be produced."""


@dataclass
class Explanation:
    text: str
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    fallback_used: bool = False


class Explainer:
    """Thin wrapper over the Anthropic Messages API.

    The client is built lazily so that importing Argus, or running the other
    three modules, never requires an API key.
    """

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        *,
        api_key: str | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        client: Any = None,
    ) -> None:
        self.model = model
        self.max_tokens = max_tokens
        self._api_key = api_key
        self._client = client

    @property
    def client(self):
        if self._client is None:
            try:
                import anthropic
            except ImportError as exc:
                raise ExplainerError(
                    "the anthropic package is not installed; run pip install -r requirements.txt"
                ) from exc

            key = self._api_key or os.environ.get("ANTHROPIC_API_KEY")
            if not key and not os.environ.get("ANTHROPIC_AUTH_TOKEN"):
                # An `ant auth login` profile also works, so an unset key is not
                # proof that there are no credentials -- let the SDK resolve it
                # and report the SDK's own error if there really are none.
                pass
            self._client = anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()
        return self._client

    def explain(self, report: dict[str, Any], question: str | None = None) -> Explanation:
        """Explain one optimization report, optionally answering a question about it."""
        payload = json.dumps(report, indent=2, default=str)
        prompt = "Here is the report:\n\n" + payload
        if question:
            prompt += "\n\nThe engineer also asks: " + question

        response, fallback_used = self._create(prompt)

        if getattr(response, "stop_reason", None) == "refusal":
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None) if details else None
            raise ExplainerError(
                "the model declined to answer"
                + (" (category: " + str(category) + ")" if category else "")
            )

        text = "".join(block.text for block in response.content if block.type == "text").strip()
        if not text:
            raise ExplainerError("the model returned no text")

        usage = getattr(response, "usage", None)
        return Explanation(
            text=text,
            model=getattr(response, "model", self.model),
            input_tokens=getattr(usage, "input_tokens", None) if usage else None,
            output_tokens=getattr(usage, "output_tokens", None) if usage else None,
            fallback_used=fallback_used,
        )

    def _create(self, prompt: str):
        """Send the request, with a server-side fallback on a policy decline.

        The fallback is on by default. If the account does not have the beta
        enabled, the request is retried once on the stable endpoint rather than
        failing the whole command over an opt-in feature.
        """
        import anthropic

        request = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": prompt}],
        }

        try:
            return (
                self.client.beta.messages.create(
                    betas=["server-side-fallback-2026-07-01"],
                    fallbacks="default",
                    **request,
                ),
                True,
            )
        except anthropic.BadRequestError as exc:
            message = str(exc).lower()
            if "beta" not in message and "fallback" not in message:
                raise ExplainerError(_readable(exc)) from exc
        except anthropic.APIError as exc:
            raise ExplainerError(_readable(exc)) from exc

        try:
            return self.client.messages.create(**request), False
        except anthropic.APIError as exc:
            raise ExplainerError(_readable(exc)) from exc


def _readable(exc: Exception) -> str:
    """Turn an SDK exception into something a CLI user can act on."""
    import anthropic

    if isinstance(exc, anthropic.AuthenticationError):
        return (
            "the Anthropic API rejected the credentials; set ANTHROPIC_API_KEY or run "
            "`ant auth login`"
        )
    if isinstance(exc, anthropic.RateLimitError):
        retry = exc.response.headers.get("retry-after", "60") if exc.response is not None else "60"
        return "rate limited by the Anthropic API; retry after " + str(retry) + "s"
    if isinstance(exc, anthropic.NotFoundError):
        return "unknown model '" + str(getattr(exc, "message", exc)) + "'"
    if isinstance(exc, anthropic.APIConnectionError):
        return "could not reach the Anthropic API; check network access"
    if isinstance(exc, anthropic.APIStatusError):
        return "Anthropic API error " + str(exc.status_code) + ": " + str(exc.message)
    return str(exc)


def render_apply_plan(report: dict[str, Any]) -> list[str]:
    """The commands a human would run to apply Argus's recommendation.

    Printed, never executed. Argus deliberately has no code path that resizes a
    resource: the confirmation step is a person reading these lines and running
    them, which is also where a bad recommendation gets caught.
    """
    proposed = (report.get("argus") or {}).get("proposed") or {}
    if not proposed:
        return []

    resource = report.get("resource") or {}
    resource_type = resource.get("type")
    identifier = resource.get("id")
    region = resource.get("region")
    attributes = resource.get("attributes") or {}

    if resource_type == "ecs-service":
        cluster = attributes.get("cluster", "<cluster>")
        return [
            "# 1. Register a new task definition revision with the proposed size.",
            "#    The current one is " + str(attributes.get("task_definition", "<task-definition>")) + ".",
            "aws ecs describe-task-definition --task-definition "
            + str(attributes.get("task_definition", "<task-definition>"))
            + " --region " + str(region) + " --query taskDefinition > taskdef.json",
            "# 2. Edit taskdef.json: set cpu to " + str(proposed.get("cpu"))
            + " and memory to " + str(proposed.get("memory")) + ".",
            "aws ecs register-task-definition --cli-input-json file://taskdef.json --region " + str(region),
            "# 3. Point the service at the new revision.",
            "aws ecs update-service --cluster " + str(cluster) + " --service " + str(identifier)
            + " --task-definition <new-revision-arn> --region " + str(region),
            "#",
            "# In this repository the task definition is generated at build time from",
            "# task-manager/taskdef.template.json, so the durable change is to that",
            "# template and to ecs-task-definition.yaml, not to a one-off revision.",
        ]

    if resource_type in ("ec2-instance", "rds-instance"):
        target = proposed.get("instance_type")
        if resource_type == "ec2-instance":
            return [
                "# Resizing an EC2 instance requires a stop/start, which changes the public IP.",
                "aws ec2 stop-instances --instance-ids " + str(identifier) + " --region " + str(region),
                "aws ec2 modify-instance-attribute --instance-id " + str(identifier)
                + " --instance-type " + str(target) + " --region " + str(region),
                "aws ec2 start-instances --instance-ids " + str(identifier) + " --region " + str(region),
            ]
        return [
            "# --apply-immediately causes a reboot; without it the change waits for the",
            "# next maintenance window.",
            "aws rds modify-db-instance --db-instance-identifier " + str(identifier)
            + " --db-instance-class " + str(target) + " --region " + str(region),
        ]

    return []
