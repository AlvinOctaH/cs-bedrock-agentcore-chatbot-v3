"""
create_dashboard.py
===================
Stand-out: CloudWatch dashboard for the NovaMart multi-agent system.

Creates (or updates) the dashboard "NovaMart-MultiAgent-Observability" with:

  1. Customer requests over time          - Logs Insights on the agent log group
  2. Agent invocations over time, per agent
  3. Average / p95 latency per agent type - parsed from "tool done <tool> (<secs>s)"
  4. Parallel RAG latency (search_all_policies) vs. slowest single KB
  5. Guardrail trigger frequency          - AWS/Bedrock/Guardrails metrics
  6. AgentCore Runtime invocations, latency and errors - Bedrock-AgentCore metrics

The agent-level widgets read the INFO log lines that agent_observability.py
writes for every tool call, so they work for local runs (test / chat / demo)
and for the deployed runtime alike. Metric widgets use SEARCH expressions so
they pick up whatever dimensions the services publish.

Usage:
    python standout/create_dashboard.py            # create / update, print the URL
    python standout/create_dashboard.py --delete   # remove the dashboard
"""

import json
import os
import sys

import boto3

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

DASHBOARD_NAME = 'NovaMart-MultiAgent-Observability'
PERIOD = 300        # 5-minute buckets


def _logs_widget(title: str, query: str, view: str, x: int, y: int, w: int = 12, h: int = 6) -> dict:
    return {
        'type': 'log', 'x': x, 'y': y, 'width': w, 'height': h,
        'properties': {
            'title': title,
            'region': config.AWS_REGION,
            'query': f"SOURCE '{config.AGENT_LOG_GROUP}' | {query}",
            'view': view,
        },
    }


def _metric_widget(title: str, expressions: list, x: int, y: int, w: int = 12, h: int = 6,
                   stat: str = 'Sum', view: str = 'timeSeries') -> dict:
    return {
        'type': 'metric', 'x': x, 'y': y, 'width': w, 'height': h,
        'properties': {
            'title': title,
            'region': config.AWS_REGION,
            'view': view,
            'stat': stat,
            'period': PERIOD,
            'metrics': [[{'expression': expr, 'label': label, 'id': f"e{i}"}]
                        for i, (label, expr) in enumerate(expressions, 1)],
        },
    }


def build_dashboard() -> dict:
    tool_done = r"parse @message /tool done\s+(?<tool>\S+) \((?<secs>[\d.]+)s\)/"
    agent_of = ("| filter tool like /^route_to_/ "
                "| fields replace(replace(tool, 'route_to_', ''), '_agent', '') as agent")

    # Dimension sets are spelled out so each series is counted exactly once.
    guardrail        = "{AWS/Bedrock/Guardrails,GuardrailArn,GuardrailVersion}"
    guardrail_policy = "{AWS/Bedrock/Guardrails,GuardrailPolicyType,Operation}"
    runtime_total    = "{AWS/Bedrock-AgentCore,AggregateOperation}"
    runtime_resource = "{AWS/Bedrock-AgentCore,Resource,Operation}"
    runtime_named    = "{AWS/Bedrock-AgentCore,Resource,Operation,Name}"

    widgets = [
        {
            'type': 'text', 'x': 0, 'y': 0, 'width': 24, 'height': 2,
            'properties': {'markdown': (
                "# NovaMart Multi-Agent Support - Observability\n"
                f"Agent logs: `{config.AGENT_LOG_GROUP}` · Guardrail `{config.GUARDRAIL_ID}` "
                f"v{config.GUARDRAIL_VERSION} · Traces: CloudWatch → Application Signals → Trace Map"
            )},
        },
        # Row 1 - volume
        _logs_widget(
            'Customer requests (1 CommunicationAgent call per request)',
            "filter @message like /tool done\\s+route_to_communication_agent/ "
            "| stats count(*) as requests by bin(5m)",
            'timeSeries', 0, 2),
        _logs_widget(
            'Agent invocations by agent',
            f"{tool_done} {agent_of} | stats count(*) as invocations by agent | sort invocations desc",
            'bar', 12, 2),
        # Row 2 - latency
        _logs_widget(
            'Latency per agent type (seconds)',
            f"{tool_done} {agent_of} "
            "| stats count(*) as calls, avg(secs) as avg_s, pct(secs, 95) as p95_s, max(secs) as max_s "
            "by agent | sort avg_s desc",
            'table', 0, 8),
        _logs_widget(
            'Average latency per agent over time (seconds)',
            f"{tool_done} | filter tool like /^route_to_/ "
            "| stats avg(secs) as avg_s by bin(5m), tool",
            'timeSeries', 12, 8),
        # Row 3 - parallel RAG + guardrail
        _logs_widget(
            'Parallel RAG: search_all_policies (3 KBs in parallel) vs single retrievers',
            f"{tool_done} | filter tool like /search_all_policies|retrieve_.*_policy/ "
            "| stats count(*) as calls, avg(secs) as avg_s, max(secs) as max_s by tool",
            'table', 0, 14),
        _metric_widget(
            'Guardrail evaluations vs interventions',
            [('Evaluations', f"SUM(SEARCH('{guardrail} MetricName=\"Invocations\"', 'Sum', {PERIOD}))"),
             ('Intervened (blocked / masked)',
              f"SUM(SEARCH('{guardrail} MetricName=\"InvocationsIntervened\"', 'Sum', {PERIOD}))")],
            12, 14),
        # Row 4 - guardrail detail
        {
            'type': 'metric', 'x': 0, 'y': 20, 'width': 12, 'height': 6,
            'properties': {
                'title': 'Guardrail triggers by policy type (content / PII / topic / word)',
                'region': config.AWS_REGION, 'view': 'timeSeries', 'stacked': True,
                'stat': 'Sum', 'period': PERIOD,
                'metrics': [[{'expression': f"SEARCH('{guardrail_policy} MetricName=\"InvocationsIntervened\"', "
                                            f"'Sum', {PERIOD})", 'id': 'e1'}]],
            },
        },
        _metric_widget(
            'Guardrail intervention rate (%)',
            [('Intervention rate',
              f"100 * SUM(SEARCH('{guardrail} MetricName=\"InvocationsIntervened\"', 'Sum', {PERIOD})) / "
              f"SUM(SEARCH('{guardrail} MetricName=\"Invocations\"', 'Sum', {PERIOD}))")],
            12, 20),
        # Row 5 - deployed runtime
        _metric_widget(
            'AgentCore Runtime invocations and errors',
            [('Invocations', f"SUM(SEARCH('{runtime_total} MetricName=\"Invocations\"', 'Sum', {PERIOD}))"),
             ('Errors', f"SUM(SEARCH('{runtime_named} MetricName=\"Errors\"', 'Sum', {PERIOD}))")],
            0, 26),
        _metric_widget(
            'AgentCore Runtime latency (ms, average)',
            [('Latency', f"AVG(SEARCH('{runtime_resource} MetricName=\"Latency\" "
                         f"Operation=\"InvokeAgentRuntime\"', 'Average', {PERIOD}))")],
            12, 26, stat='Average'),
    ]
    return {'start': '-PT3H', 'periodOverride': 'inherit', 'widgets': widgets}


def main() -> int:
    cw = boto3.client('cloudwatch', region_name=config.AWS_REGION)
    if '--delete' in sys.argv:
        cw.delete_dashboards(DashboardNames=[DASHBOARD_NAME])
        print(f"Deleted dashboard {DASHBOARD_NAME}")
        return 0

    body = build_dashboard()
    response = cw.put_dashboard(DashboardName=DASHBOARD_NAME, DashboardBody=json.dumps(body))
    problems = response.get('DashboardValidationMessages', [])
    for p in problems:
        print(f"  [validation] {p.get('DataPath')}: {p.get('Message')}")
    url = (f"https://{config.AWS_REGION}.console.aws.amazon.com/cloudwatch/home?"
           f"region={config.AWS_REGION}#dashboards/dashboard/{DASHBOARD_NAME}")
    print(f"Dashboard '{DASHBOARD_NAME}' saved ({len(body['widgets'])} widgets).")
    print(f"Open: {url}")
    print("Generate data first: python src/agent_orchestrator.py test   (and the guardrail suite)")
    return 1 if problems else 0


if __name__ == '__main__':
    sys.exit(main())
