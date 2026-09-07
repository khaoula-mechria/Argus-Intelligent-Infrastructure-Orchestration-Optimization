"""Tests for the explanation layer, with a stubbed Anthropic client."""

from __future__ import annotations

import anthropic
import pytest

from argus.explainer import DEFAULT_MODEL, Explainer, ExplainerError, render_apply_plan

REPORT = {
    "resource": {
        "id": "taskmanager-dev-service",
        "type": "ecs-service",
        "region": "eu-west-3",
        "period_days": 14,
        "attributes": {
            "cluster": "taskmanager-dev",
            "cpu": 1024,
            "memory": 2048,
            "task_definition": "arn:aws:ecs:eu-west-3:1:task-definition/td:7",
        },
    },
    "metrics": {"CPUUtilization": {"p95": 11.0, "datapoints": 336, "unit": "Percent"}},
    "argus": {
        "source": "argus",
        "finding": "over-provisioned",
        "rationale": "CPUUtilization p95=11.0%",
        "current": {"cpu": 1024, "memory": 2048},
        "proposed": {"cpu": 512, "memory": 4096},
    },
    "aws": None,
    "agreement": "no AWS recommendation available",
    "notes": [],
}


class Block:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class Usage:
    input_tokens = 1200
    output_tokens = 300


class Message:
    def __init__(self, text="The service is idle.", stop_reason="end_turn", model=DEFAULT_MODEL):
        self.content = [Block(text)] if text else []
        self.stop_reason = stop_reason
        self.stop_details = None
        self.model = model
        self.usage = Usage()


class FakeMessages:
    def __init__(self, result, recorder):
        self._result = result
        self._recorder = recorder

    def create(self, **kwargs):
        self._recorder.append(kwargs)
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class FakeClient:
    """Mimics the two call paths the explainer uses: beta and stable."""

    def __init__(self, beta_result, stable_result=None):
        self.calls: list[dict] = []
        self.messages = FakeMessages(stable_result or Message(), self.calls)
        self.beta = type("Beta", (), {"messages": FakeMessages(beta_result, self.calls)})()


def test_explanation_is_returned_with_its_usage():
    explainer = Explainer(client=FakeClient(Message("Only 11% of the CPU is used.")))
    explanation = explainer.explain(REPORT)

    assert "11%" in explanation.text
    assert explanation.model == DEFAULT_MODEL
    assert explanation.input_tokens == 1200
    assert explanation.fallback_used is True


def test_the_report_is_sent_verbatim_as_json():
    client = FakeClient(Message())
    Explainer(client=client).explain(REPORT)

    prompt = client.calls[0]["messages"][0]["content"]
    assert "taskmanager-dev-service" in prompt
    assert "over-provisioned" in prompt
    # The system prompt is what keeps the model from inventing numbers.
    assert "Ground every claim in a number" in client.calls[0]["system"]


def test_an_extra_question_reaches_the_model():
    client = FakeClient(Message())
    Explainer(client=client).explain(REPORT, question="Is 512 CPU enough for a burst?")

    assert "Is 512 CPU enough for a burst?" in client.calls[0]["messages"][0]["content"]


def test_a_refusal_is_surfaced_rather_than_returned_as_text():
    explainer = Explainer(client=FakeClient(Message("", stop_reason="refusal")))
    with pytest.raises(ExplainerError, match="declined"):
        explainer.explain(REPORT)


def test_an_empty_response_is_an_error():
    explainer = Explainer(client=FakeClient(Message("")))
    with pytest.raises(ExplainerError, match="no text"):
        explainer.explain(REPORT)


def test_an_account_without_the_fallback_beta_falls_back_to_the_stable_endpoint():
    # The fallback parameter is opt-in; an account that lacks it must still get
    # its explanation rather than lose the command to a 400.
    rejected = anthropic.BadRequestError(
        message="unsupported beta: server-side-fallback-2026-07-01",
        response=_response(400),
        body=None,
    )
    client = FakeClient(rejected, stable_result=Message("Explained anyway."))
    explanation = Explainer(client=client).explain(REPORT)

    assert explanation.text == "Explained anyway."
    assert explanation.fallback_used is False
    assert len(client.calls) == 2


def test_an_unrelated_bad_request_is_not_retried():
    rejected = anthropic.BadRequestError(
        message="max_tokens: must be greater than 0", response=_response(400), body=None
    )
    client = FakeClient(rejected)
    with pytest.raises(ExplainerError, match="max_tokens"):
        Explainer(client=client).explain(REPORT)
    assert len(client.calls) == 1


def test_authentication_errors_say_what_to_set():
    failure = anthropic.AuthenticationError(
        message="invalid x-api-key", response=_response(401), body=None
    )
    with pytest.raises(ExplainerError, match="ANTHROPIC_API_KEY"):
        Explainer(client=FakeClient(failure)).explain(REPORT)


def _response(status_code):
    import httpx2

    return httpx2.Response(status_code, request=httpx2.Request("POST", "https://api.anthropic.com"))


# -- the apply plan ---------------------------------------------------------


def test_apply_plan_is_commands_for_a_human_not_an_action():
    commands = render_apply_plan(REPORT)

    assert commands
    joined = "\n".join(commands)
    assert "register-task-definition" in joined
    assert "update-service" in joined
    # This repository generates taskdef.json at build time, so the durable fix
    # is the template, not a hand-registered revision.
    assert "taskdef.template.json" in joined


def test_no_proposed_change_means_no_commands():
    report = dict(REPORT, argus=dict(REPORT["argus"], proposed={}))
    assert render_apply_plan(report) == []


def test_apply_plan_for_rds_warns_about_the_reboot():
    report = {
        "resource": {"id": "db-1", "type": "rds-instance", "region": "eu-west-3", "attributes": {}},
        "argus": {"proposed": {"instance_type": "db.t3.small"}},
    }
    joined = "\n".join(render_apply_plan(report))
    assert "modify-db-instance" in joined
    assert "reboot" in joined
