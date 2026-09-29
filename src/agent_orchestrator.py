"""
agent_orchestrator.py
=====================
Enterprise Multi-Agent Customer Support System
Built with Strands Agents SDK + Amazon Bedrock AgentCore

Architecture implemented:

  Customer Request
        │
  OrchestratorAgent  (Claude Haiku 4.5 - fast routing, manages WorkflowState)
        │
   ┌────┼────────────────────┬────────────────────────┐
   │    │                    │                        │
InventoryAgent   PolicyAgent   RefundAgent  CommunicationAgent
(DynamoDB)    (Multi-Agent RAG)  (DynamoDB)   (composes response)
                    │
         ┌──────────┼──────────┐
    ReturnsPolicyRetriever  ShippingPolicyRetriever  WarrantyPolicyRetriever
        (KB: returns)           (KB: shipping)           (KB: warranty)
         └──────────── all run in PARALLEL ────────────┘

Shared state flows through DynamoDB WorkflowStateTable.
OrchestratorAgent creates state at start, each routing tool reads and
updates it after the worker responds.

Commands:
  python src/agent_orchestrator.py test            # 3 scenarios, local run, traced to X-Ray
  python src/agent_orchestrator.py chat            # interactive terminal chat
  python src/agent_orchestrator.py deploy          # Tasks 3-6 deployment pipeline (uses the AgentCore CLI)
  python src/agent_orchestrator.py invoke "<msg>"  # call the deployed AgentCore Runtime
  python src/agent_orchestrator.py serve           # HTTP server (what AgentCore Runtime runs)

Deployment uses the AgentCore CLI (`agentcore`, npm package @aws/agentcore,
https://github.com/aws/agentcore-cli) through the pre-written helper
src/agentcore_cli.py - see the README for the prerequisites (Node.js 20+, uv).
"""

import boto3
import json
import time
import os
import sys
import uuid
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional

# Ensure the parent directory is on sys.path so config.py and
# bedrock_kb_retrieval.py are importable regardless of where this
# script is invoked from (e.g. python src/agent_orchestrator.py)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Strands Agents SDK - see: https://github.com/strands-agents/sdk-python
from strands import Agent
from strands.models import BedrockModel
from strands.tools.executors import SequentialToolExecutor
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

import config
from bedrock_kb_retrieval import retrieve_from_knowledge_base, format_kb_results

# Configure logging for debugging
logging.basicConfig(
    level=logging.WARNING,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────
# OUTPUT UTILITIES
# ─────────────────────────────────────────────────────
# Terminal trace UI, ANSI colour constants, and agent metadata
# are defined in agent_utils.py - keeping this file focused on
# agent architecture.
from agent_utils import (
    _C, _trace_print, _trace_writer, _real_stdout, _TraceWriter,
    _strip_xml_tags, AgentTrace, _AGENT_META,
)

# ─────────────────────────────────────────────────────
# OBSERVABILITY
# ─────────────────────────────────────────────────────
# `tool` is the Strands @tool decorator wrapped so that every tool call is
# recorded as an X-Ray subsegment (the orchestrator's route_to_* tools become
# the worker-agent nodes on the X-Ray Service Map) and logged at INFO level.
# Use it exactly like `strands.tool`:  @tool  above each tool function.
from agent_observability import (
    tool, tracer, setup_logging, flush_logs, print_trace_hint,
    apply_observability_config, wait_for_runtime_ready,
)


# ─────────────────────────────────────────────────────
# AWS CLIENTS
# ─────────────────────────────────────────────────────
bedrock_agent_client = boto3.client('bedrock-agent', region_name=config.AWS_REGION)
bedrock_runtime      = boto3.client('bedrock-runtime', region_name=config.AWS_REGION)
agentcore_client     = boto3.client('bedrock-agentcore', region_name=config.AWS_REGION)
agentcore_control    = boto3.client('bedrock-agentcore-control', region_name=config.AWS_REGION)
dynamodb             = boto3.resource('dynamodb', region_name=config.AWS_REGION)
logs_client          = boto3.client('logs', region_name=config.AWS_REGION)


# ═══════════════════════════════════════════════════════
#  WORKFLOW STATE - SHARED DynamoDB STATE OBJECT
#
#  WorkflowState stores the accumulated context for one customer session:
#    - What the InventoryAgent found (order status, eligibility, customer tier)
#    - What the PolicyAgent found (relevant policy text)
#    - What the RefundAgent decided (approval/denial, reference number)
#    - The CommunicationAgent's final draft
#
#  The `version` field enables optimistic locking: every write is a
#  conditional DynamoDB update that fails if someone else updated first.
#  If the condition fails, the update is retried after a fresh read.
# ═══════════════════════════════════════════════════════

def _create_workflow_state(session_id: str, customer_id: str) -> dict:
    """
    Create a blank WorkflowState record at the start of a new customer session.

    Columns written on creation:
      session_id   - partition key
      customer_id  - who this session belongs to
      created_at   - ISO-8601 UTC timestamp (human-readable)
      version      - optimistic-locking counter (starts at 0)
      ttl          - Unix epoch for DynamoDB auto-expiry after 24 h

    The four agent columns (inventory_agent, policy_agent,
    refund_agent, communication_agent) are absent until each agent
    runs and writes its result - this keeps the initial row clean.
    """
    state = {
        'session_id':  session_id,
        'customer_id': customer_id,
        'created_at':  time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'version':     0,
        'ttl':         int(time.time()) + (24 * 3600),
    }
    table = dynamodb.Table(config.WORKFLOW_STATE_TABLE)
    table.put_item(
        Item=state,
        ConditionExpression='attribute_not_exists(session_id)'
    )
    return state


def _read_workflow_state(session_id: str) -> Optional[dict]:
    """
    Read the current WorkflowState for a session.
    """
    table = dynamodb.Table(config.WORKFLOW_STATE_TABLE)
    response = table.get_item(Key={'session_id': session_id})
    return response.get('Item')


# Trace singleton - created after _read_workflow_state so AgentTrace.summary()
# can read DynamoDB WorkflowState. The read_state_fn avoids a circular import.
trace = AgentTrace(read_state_fn=_read_workflow_state)


def _update_workflow_state(session_id: str, updates: dict,
                           expected_version: int, max_retries: int = 3) -> dict:
    """
    Update WorkflowState with optimistic locking.
    """
    from boto3.dynamodb.conditions import Attr

    table = dynamodb.Table(config.WORKFLOW_STATE_TABLE)

    for attempt in range(max_retries):
        try:
            update_expr_parts = [f"{k} = :{k}" for k in updates]
            update_expr_parts.append("version = :new_version")
            update_expr = "SET " + ", ".join(update_expr_parts)

            expr_values = {f":{k}": v for k, v in updates.items()}
            expr_values[':new_version']      = expected_version + 1
            expr_values[':expected_version'] = expected_version

            table.update_item(
                Key={'session_id': session_id},
                UpdateExpression=update_expr,
                ConditionExpression='version = :expected_version',
                ExpressionAttributeValues=expr_values
            )
            return _read_workflow_state(session_id)

        except dynamodb.meta.client.exceptions.ConditionalCheckFailedException:
            if attempt == max_retries - 1:
                raise RuntimeError(
                    f"WorkflowState update failed after {max_retries} retries "
                    f"(session: {session_id}). Too many concurrent writes."
                )
            logger.warning(
                f"WorkflowState version conflict on attempt {attempt+1}, retrying..."
            )
            current = _read_workflow_state(session_id)
            if current:
                expected_version = int(current['version'])
            time.sleep(0.1 * (attempt + 1))

    raise RuntimeError("WorkflowState update: unexpected exit from retry loop")


# ═══════════════════════════════════════════════════════
#  TASK 2 - MULTI-AGENT ORCHESTRATION
# ═══════════════════════════════════════════════════════

# Return window per customer tier (days after delivery). Single source of
# truth for the RefundAgent prompt and the initiate_refund() backstop.
RETURN_WINDOW_DAYS = {'Standard': 30, 'Premium': 60}

# WorkflowState columns written by the four worker agents.
AGENT_COLUMNS = ('inventory_agent', 'policy_agent', 'refund_agent', 'communication_agent')


def _json_safe(value):
    """Convert DynamoDB Decimals (recursively) to int/float so tool results serialize cleanly."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    return value


def _days_since(iso_date: str) -> Optional[int]:
    """Whole days between an ISO date (YYYY-MM-DD) and today, or None if unparseable."""
    try:
        return (date.today() - date.fromisoformat(iso_date)).days
    except (TypeError, ValueError):
        return None


def _evaluate_return_eligibility(order: dict, tier: str) -> dict:
    """
    Deterministic return-eligibility check shared by the RefundAgent's
    initiate_refund tool. The return window is counted from delivery
    (return policy: "within 30 days of delivery").
    """
    window = RETURN_WINDOW_DAYS.get(tier, RETURN_WINDOW_DAYS['Standard'])
    status = order.get('status', 'unknown')
    if status != 'delivered':
        return {'eligible': False, 'window_days': window, 'days_since_delivery': None,
                'reason': f"order status is '{status}' - only delivered orders can be returned"}
    days = _days_since(order.get('estimated_delivery') or order.get('order_date'))
    if days is None:
        return {'eligible': False, 'window_days': window, 'days_since_delivery': None,
                'reason': 'delivery date is unknown'}
    eligible = days <= window
    return {
        'eligible': eligible,
        'window_days': window,
        'days_since_delivery': days,
        'reason': (f"delivered {days} days ago, within the {window}-day {tier} return window"
                   if eligible else
                   f"delivered {days} days ago, outside the {window}-day {tier} return window"),
    }


def _run_worker(agent: Agent, prompt: str) -> str:
    """
    Invoke a worker agent statelessly and return its text.

    Workers are built once and reused across requests, so their conversation
    history is cleared first: everything a worker needs arrives in the prompt
    or through WorkflowState, and one customer's data never leaks into
    another customer's request.
    """
    agent.messages.clear()
    return _strip_xml_tags(str(agent(prompt))).strip()


# ───────────────────────────────────────────────────────
#  2.A - INVENTORY AGENT
# ───────────────────────────────────────────────────────

def build_inventory_agent() -> Agent:
    """
    Build the Inventory Agent.

    Gathers order and customer facts from DynamoDB. Does NOT make decisions -
    only retrieves data for the OrchestratorAgent to share with downstream agents.
    """

    model = BedrockModel(
        model_id=config.WORKER_MODEL_ID,
        region_name=config.AWS_REGION,
        temperature=0.1,
    )

    system_prompt = """You are the NovaMart InventoryAgent, a data-gathering specialist.
Your only job is to retrieve accurate order and customer facts from NovaMart's
database and report them. You never make decisions.

How to work:
1. Always call get_customer_tier for the customer in the request.
2. If the request mentions an order ID (e.g. ORD-27176), call check_order_status
   with the customer ID and that order ID.
3. If the request concerns orders but gives no order ID (e.g. "my headphones",
   "my recent orders"), call list_customer_orders and identify the matching order(s).
4. Report only what the tools return. Never guess or invent values.

Output format (plain text, no markdown headers):
CUSTOMER: <name> (<customer_id>) | Tier: <tier>
ORDER: <order_id> | <product_name> | status: <status> | qty: <quantity> | price: $<price>
DATES: ordered <order_date> | delivered <estimated_delivery> | <days_since_delivery> days since delivery
TRACKING: <tracking_number>
NOTES: <anything missing or not found>

Rules:
- Do NOT decide or state whether an order is eligible for return or refund.
  That decision belongs to the RefundAgent.
- Do NOT promise refunds, replacements or any outcome to the customer.
- If an order or customer is not found, say so clearly in NOTES."""

    orders_table    = dynamodb.Table(config.ORDERS_TABLE)
    customers_table = dynamodb.Table(config.CUSTOMERS_TABLE)

    # NOTE: the Orders table has a COMPOSITE key (customer_id = partition key,
    # order_id = sort key), so a get_item needs BOTH values. That is why this
    # tool takes customer_id as well as order_id.
    @tool
    def check_order_status(customer_id: str, order_id: str) -> dict:
        """
        Look up one order in DynamoDB and report its status, product, dates
        and amount. Reports facts only - it does NOT decide return eligibility.

        Args:
            customer_id: The customer's unique identifier (e.g. CUST-001)
            order_id: The order identifier (e.g. ORD-27176)

        Returns:
            {'found': True, 'order': {order_id, status, product_name, order_date,
            estimated_delivery, price, quantity, ...}, 'days_since_order': int,
            'days_since_delivery': int | None} or {'found': False, 'message': ...}
        """
        try:
            item = orders_table.get_item(
                Key={'customer_id': customer_id, 'order_id': order_id}
            ).get('Item')
        except ClientError as exc:
            return {'found': False, 'error': str(exc)}
        if not item:
            return {'found': False,
                    'message': f"Order {order_id} was not found for customer {customer_id}."}
        order = _json_safe(item)
        delivered = order.get('status') == 'delivered'
        return {
            'found': True,
            'order': order,
            'days_since_order': _days_since(order.get('order_date')),
            'days_since_delivery': _days_since(order.get('estimated_delivery')) if delivered else None,
        }

    @tool
    def get_customer_tier(customer_id: str) -> dict:
        """
        Retrieve a customer's tier (Standard or Premium) from DynamoDB.
        Standard customers have a 30-day return window; Premium customers have 60 days.

        Args:
            customer_id: The customer's unique identifier (e.g. CUST-001)

        Returns:
            {'found': True, 'customer': {customer_id, name, email, tier,
            account_created, total_orders, preferred_contact}} or
            {'found': False, 'message': ...}
        """
        try:
            item = customers_table.get_item(Key={'customer_id': customer_id}).get('Item')
        except ClientError as exc:
            return {'found': False, 'error': str(exc)}
        if not item:
            return {'found': False, 'message': f"Customer {customer_id} was not found."}
        return {'found': True, 'customer': _json_safe(item)}

    @tool
    def list_customer_orders(customer_id: str) -> dict:
        """
        Retrieve all orders for a customer from DynamoDB.

        Args:
            customer_id: The customer's unique identifier (e.g. CUST-001)

        Returns:
            {'customer_id': ..., 'order_count': int, 'orders': [{order_id,
            product_name, status, order_date, price, quantity}, ...]}
            sorted newest first
        """
        try:
            items = orders_table.query(
                KeyConditionExpression=Key('customer_id').eq(customer_id)
            ).get('Items', [])
        except ClientError as exc:
            return {'customer_id': customer_id, 'order_count': 0, 'error': str(exc)}
        fields = ('order_id', 'product_name', 'status', 'order_date', 'price', 'quantity')
        orders = [{f: o.get(f) for f in fields} for o in _json_safe(items)]
        orders.sort(key=lambda o: o.get('order_date') or '', reverse=True)
        return {'customer_id': customer_id, 'order_count': len(orders), 'orders': orders}

    return Agent(
        name='InventoryAgent',
        model=model,
        system_prompt=system_prompt,
        tools=[check_order_status, get_customer_tier, list_customer_orders],
    )


# ───────────────────────────────────────────────────────
#  2.B - REFUND AGENT
# ───────────────────────────────────────────────────────

def build_refund_agent() -> Agent:
    """
    Build the Refund Agent.

    Makes return/refund eligibility decisions based on order facts from
    WorkflowState and applies the correct policy window per customer tier.
    """

    model = BedrockModel(
        model_id=config.WORKER_MODEL_ID,
        region_name=config.AWS_REGION,
        temperature=0.1,
    )

    standard_days = RETURN_WINDOW_DAYS['Standard']
    premium_days  = RETURN_WINDOW_DAYS['Premium']
    system_prompt = f"""You are the NovaMart RefundAgent. You decide whether an order
is eligible for return/refund and, when it is and the customer asked for it,
you initiate the return.

Decision process - follow it in order, every time:
1. ALWAYS call get_inventory_context(session_id) first. It contains the order
   and customer facts gathered by the InventoryAgent. Never decide without it.
2. From those facts identify: customer tier, order ID, order status, and the
   number of days since delivery.
3. Apply the return window for the customer's tier, counted from delivery:
     - Standard tier: {standard_days} days
     - Premium tier:  {premium_days} days
   Only orders with status "delivered" can be returned. Orders that are
   "processing" or "shipped" have not arrived yet; "cancelled" orders were
   never charged; "return_initiated" orders already have a return open.
4. If the order is eligible AND the customer asked to return or refund it,
   call initiate_refund(customer_id, order_id, reason). Use the customer's
   stated reason, or "Customer requested return" if none was given.
5. If the order is not eligible, do NOT call initiate_refund.
6. If the customer only asked about order status (no return intent),
   do not call initiate_refund - report NO_ACTION.

Output format (plain text):
DECISION: APPROVED | DENIED | NO_ACTION | NEEDS_INFO
ORDER: <order_id> - <product_name>
TIER / WINDOW: <tier> / <window> days | days since delivery: <n>
REASON: <one or two sentences explaining the decision>
RETURN REFERENCE: <reference from initiate_refund, or N/A>
NEXT STEPS: <what the customer should do next, from initiate_refund when available>

Never invent order facts, dates or reference numbers. If the inventory context
is missing or the order was not found, answer DECISION: NEEDS_INFO."""

    orders_table    = dynamodb.Table(config.ORDERS_TABLE)
    customers_table = dynamodb.Table(config.CUSTOMERS_TABLE)

    @tool
    def get_inventory_context(session_id: str) -> dict:
        """
        Read the WorkflowState to access facts gathered by the InventoryAgent.
        Call this FIRST, before making any eligibility decision.

        Args:
            session_id: The current session identifier

        Returns:
            {'session_id', 'customer_id', 'inventory_agent': '<InventoryAgent findings>'},
            or an empty dict if the session or the inventory findings do not exist yet
        """
        state = _read_workflow_state(session_id)
        if not state or not state.get('inventory_agent'):
            return {}
        return {
            'session_id':      session_id,
            'customer_id':     state.get('customer_id'),
            'inventory_agent': state['inventory_agent'],
        }

    @tool
    def initiate_refund(customer_id: str, order_id: str, reason: str) -> dict:
        """
        Initiate a return by updating the order record in DynamoDB.
        The order status changes from "delivered" to "return_initiated" and a
        return reference is recorded. The tier return window is re-checked
        here, so an ineligible order is never updated.

        Args:
            customer_id: The customer's unique identifier
            order_id: The order to return
            reason: Customer-provided reason for the return

        Returns:
            {'success': True, 'return_reference', 'refund_amount', 'instructions', ...}
            or {'success': False, 'reason': ...} when the return cannot be initiated
        """
        order = orders_table.get_item(
            Key={'customer_id': customer_id, 'order_id': order_id}).get('Item')
        if not order:
            return {'success': False, 'reason': f"Order {order_id} not found for {customer_id}."}
        order = _json_safe(order)

        # Idempotent: a second request for the same order returns the existing reference.
        if order.get('status') == 'return_initiated':
            return {'success': True, 'already_initiated': True, 'order_id': order_id,
                    'return_reference': order.get('return_reference'),
                    'refund_amount': order.get('refund_amount'),
                    'instructions': 'A return is already open for this order.'}

        customer = customers_table.get_item(Key={'customer_id': customer_id}).get('Item') or {}
        tier = customer.get('tier', 'Standard')
        eligibility = _evaluate_return_eligibility(order, tier)
        if not eligibility['eligible']:
            return {'success': False, 'order_id': order_id, 'tier': tier, **eligibility}

        reference = f"RMA-{uuid.uuid4().hex[:8].upper()}"
        refund_amount = f"{float(order.get('price', 0)) * int(order.get('quantity', 1)):.2f}"
        try:
            orders_table.update_item(
                Key={'customer_id': customer_id, 'order_id': order_id},
                UpdateExpression=('SET #status = :new_status, return_reference = :ref, '
                                  'return_reason = :reason, refund_amount = :amount, '
                                  'return_initiated_at = :ts'),
                ConditionExpression='#status = :delivered',
                ExpressionAttributeNames={'#status': 'status'},     # "status" is a reserved word
                ExpressionAttributeValues={
                    ':new_status': 'return_initiated',
                    ':delivered':  'delivered',
                    ':ref':        reference,
                    ':reason':     reason or 'Customer requested return',
                    ':amount':     refund_amount,
                    ':ts':         datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
                },
            )
        except dynamodb.meta.client.exceptions.ConditionalCheckFailedException:
            return {'success': False, 'order_id': order_id,
                    'reason': 'The order status changed before the return could be recorded.'}

        return {
            'success': True,
            'order_id': order_id,
            'product_name': order.get('product_name'),
            'return_reference': reference,
            'refund_amount': refund_amount,
            'refund_method': 'original payment method',
            'eligibility': eligibility['reason'],
            'instructions': ('Print the prepaid return label from Order History, pack the item '
                             'securely with all original accessories, and drop it off at any '
                             'authorized carrier location. The refund is issued to the original '
                             'payment method within 5-7 business days after the return arrives.'),
        }

    return Agent(
        name='RefundAgent',
        model=model,
        system_prompt=system_prompt,
        tools=[get_inventory_context, initiate_refund],
    )


# ───────────────────────────────────────────────────────
#  2.C - POLICY AGENT - MULTI-AGENT RAG
# ───────────────────────────────────────────────────────

def build_policy_agent() -> Agent:
    """
    Build the Policy Agent - a multi-agent RAG system.

    Internally creates three specialized retriever sub-agents that run in
    PARALLEL, each querying its own Knowledge Base. The coordinator synthesizes
    the combined results into a complete, grounded policy answer.
    """

    def _retriever_prompt(domain: str, tool_name: str) -> str:
        return f"""You are the NovaMart {domain}PolicyRetrieverAgent.
You have one tool, {tool_name}, which searches the {domain} Policy knowledge base.

1. Call {tool_name} exactly once, passing the user's question as the query.
2. Return ONLY the retrieved passages that are relevant to the question,
   copied verbatim, each followed by its Source line.
3. Do not summarize, interpret, or add knowledge that is not in the passages.
4. If nothing relevant was retrieved, reply exactly: No relevant {domain.lower()} policy passages found."""

    # Retrievers are deterministic (temperature 0.0) and run without a
    # streaming callback: three agents print concurrently, so their
    # output is returned as values and printed in order by trace.kb_result().

    @tool
    def retrieve_returns_policy(query: str) -> str:
        """
        Retrieve relevant passages from the Returns Policy knowledge base.

        Args:
            query: The policy question to search for

        Returns:
            Formatted passages with relevance score and S3 source, or a
            "No relevant policy documents found." message
        """
        return format_kb_results(retrieve_from_knowledge_base(config.RETURNS_KB_ID, query))

    returns_retriever = Agent(
        name='ReturnsPolicyRetrieverAgent',
        model=BedrockModel(model_id=config.WORKER_MODEL_ID,
                           region_name=config.AWS_REGION, temperature=0.0),
        system_prompt=_retriever_prompt('Returns', 'retrieve_returns_policy'),
        tools=[retrieve_returns_policy],
        callback_handler=None,
    )

    @tool
    def retrieve_shipping_policy(query: str) -> str:
        """
        Retrieve relevant passages from the Shipping Policy knowledge base.

        Args:
            query: The policy question to search for

        Returns:
            Formatted passages with relevance score and S3 source, or a
            "No relevant policy documents found." message
        """
        return format_kb_results(retrieve_from_knowledge_base(config.SHIPPING_KB_ID, query))

    shipping_retriever = Agent(
        name='ShippingPolicyRetrieverAgent',
        model=BedrockModel(model_id=config.WORKER_MODEL_ID,
                           region_name=config.AWS_REGION, temperature=0.0),
        system_prompt=_retriever_prompt('Shipping', 'retrieve_shipping_policy'),
        tools=[retrieve_shipping_policy],
        callback_handler=None,
    )

    @tool
    def retrieve_warranty_policy(query: str) -> str:
        """
        Retrieve relevant passages from the Warranty Policy knowledge base.

        Args:
            query: The policy question to search for

        Returns:
            Formatted passages with relevance score and S3 source, or a
            "No relevant policy documents found." message
        """
        return format_kb_results(retrieve_from_knowledge_base(config.WARRANTY_KB_ID, query))

    warranty_retriever = Agent(
        name='WarrantyPolicyRetrieverAgent',
        model=BedrockModel(model_id=config.WORKER_MODEL_ID,
                           region_name=config.AWS_REGION, temperature=0.0),
        system_prompt=_retriever_prompt('Warranty', 'retrieve_warranty_policy'),
        tools=[retrieve_warranty_policy],
        callback_handler=None,
    )

    @tool
    def search_all_policies(query: str) -> str:
        """
        Query all three policy knowledge bases IN PARALLEL and return combined results.

        Runs ReturnsPolicyRetrieverAgent, ShippingPolicyRetrieverAgent, and
        WarrantyPolicyRetrieverAgent simultaneously, then combines their findings.

        Args:
            query: The customer's policy question

        Returns:
            Combined policy passages from all three knowledge bases
        """
        retrievers = {
            'Returns':  returns_retriever,
            'Shipping': shipping_retriever,
            'Warranty': warranty_retriever,
        }

        # ── Trace: show parallel KB dispatch to learners ──────────────────
        trace.kb_start({
            'Returns':  config.RETURNS_KB_ID,
            'Shipping': config.SHIPPING_KB_ID,
            'Warranty': config.WARRANTY_KB_ID,
        })

        # Define a helper to run one retriever sub-agent
        def _run_retriever(domain: str, agent, query: str) -> tuple:
            """
            Run one retriever sub-agent and return (domain, result_text).

            stdout is suppressed globally for all threads by the
            _TraceWriter._suppress_parallel flag set in kb_start().
            This covers both the direct worker thread and any internal
            streaming child threads that Strands SDK spawns internally -
            which do NOT inherit thread-local variables and therefore cannot
            be suppressed with a thread-local capture approach.
            Results are returned as values and printed cleanly and
            sequentially by trace.kb_result() after all futures join.
            """
            try:
                return domain, _run_worker(agent, query)
            except Exception as exc:        # one failed KB must not sink the other two
                logger.warning("%s retriever failed: %s", domain, exc)
                return domain, f"[{domain} retriever error: {exc}]"

        # All three retrievers run at the same time: total latency is the
        # slowest retriever, not the sum of all three.
        results = {}
        try:
            with ThreadPoolExecutor(max_workers=3) as executor:
                futures = {
                    executor.submit(_run_retriever, domain, agent, query): domain
                    for domain, agent in retrievers.items()
                }
                for future in as_completed(futures):
                    domain, text = future.result()
                    results[domain] = text
        finally:
            # ── Trace: all KBs responded - print each result sequentially ─────
            trace.kb_done(len(retrievers))
        for domain in ['Returns', 'Shipping', 'Warranty']:
            trace.kb_result(domain, results.get(domain, '[No results]'))

        # Combine results from all three domains (fixed order for a stable prompt)
        return "\n\n".join(
            f"=== {domain.upper()} POLICY (Knowledge Base) ===\n{results.get(domain, '[No results]')}"
            for domain in ['Returns', 'Shipping', 'Warranty']
        )

    model = BedrockModel(
        model_id=config.WORKER_MODEL_ID,
        region_name=config.AWS_REGION,
        temperature=0.2,
    )

    system_prompt = """You are the NovaMart PolicyAgent, the coordinator of a multi-agent
RAG system over three policy knowledge bases (Returns, Shipping, Warranty).

1. ALWAYS call search_all_policies first, passing the policy question.
   It queries all three knowledge bases in parallel and returns their passages.
2. Synthesize a clear, complete answer grounded ONLY in the retrieved passages.
   Name the policy each fact comes from (Returns / Shipping / Warranty policy).
3. Combine duplicate passages: the customer tier document appears in all three
   knowledge bases, so state each fact once.
4. If passages disagree (for example a general rule and a tier-specific rule),
   state the specific rule and note the general one it overrides.
5. If the passages do not answer the question, say so plainly. Never use
   outside knowledge and never invent numbers, fees or time frames.
6. You only know policy text. You have no access to a specific customer's
   tier or orders - do not guess them.

Answer in concise plain text (short bullet points are fine)."""

    return Agent(
        name='PolicyAgent',
        model=model,
        system_prompt=system_prompt,
        tools=[search_all_policies],
    )


# ───────────────────────────────────────────────────────
#  2.D - COMMUNICATION AGENT
# ───────────────────────────────────────────────────────

def build_communication_agent() -> Agent:
    """
    Build the Communication Agent.

    Drafts the final customer-facing message by reading the full WorkflowState
    and composing a coherent, empathetic response.
    """

    model = BedrockModel(
        model_id=config.WORKER_MODEL_ID,
        region_name=config.AWS_REGION,
        temperature=0.3,
    )

    system_prompt = """You are the NovaMart CommunicationAgent. You write the final,
customer-facing reply on behalf of NovaMart Customer Support.

1. ALWAYS call get_full_workflow_context(session_id) first. It holds the
   findings of the other agents for this request:
     - inventory_agent: order and customer facts
     - refund_agent:    the return/refund decision and reference number
     - policy_agent:    policy information from the knowledge bases
   Columns that are empty or missing were not needed for this request.
2. Answer the customer's original request using ALL relevant findings:
     - Return/refund: state the decision clearly, the reason (tier and return
       window), the return reference number and the next steps.
     - If a return was denied: explain why with empathy and offer a helpful
       alternative (e.g. warranty claim for defects, contacting support).
     - Policy questions: give the policy facts, naming the policy they come from.
     - Account questions: give the facts from inventory_agent.
3. Calculation questions (no findings needed): compute the answer yourself,
   step by step, carrying full precision. Round money to cents only once, at
   the very end, rounding half up (e.g. $134.955 -> $134.96). Show the steps
   briefly, then the final amount.
4. Never invent order details, dates, amounts, policies or reference numbers.
   If information is missing, say what is missing and how the customer can get help.
5. Tone: warm, professional, empathetic and concise. Address the customer by
   first name when it is known. Keep it under ~180 words.
6. Never mention internal details: agent names, session IDs, WorkflowState,
   tools, DynamoDB or knowledge bases.
7. Never write email addresses or phone numbers (the safety guardrail masks
   them). To offer more help, invite the customer to simply reply to this message.
8. Plain text only - no markdown (no **bold**, no # headings); numbered or
   dashed lists are fine. End with: "Best regards,
   NovaMart Customer Support"."""

    @tool
    def get_full_workflow_context(session_id: str) -> dict:
        """
        Read the complete WorkflowState to access all findings from previous agents.

        Args:
            session_id: The current session identifier

        Returns:
            Full WorkflowState dict (customer_id, version, inventory_agent,
            policy_agent, refund_agent, previous_response), or
            {'found': False} if the session does not exist
        """
        state = _read_workflow_state(session_id)
        if not state:
            return {'found': False, 'session_id': session_id}
        context = _json_safe(state)
        context.pop('ttl', None)
        context.pop('communication_agent', None)     # this agent's own (previous) output
        context['found'] = True
        return context

    return Agent(
        name='CommunicationAgent',
        model=model,
        system_prompt=system_prompt,
        tools=[get_full_workflow_context],
    )


# ───────────────────────────────────────────────────────
#  2.E - ORCHESTRATOR AGENT
# ───────────────────────────────────────────────────────

def build_orchestrator_agent(
    inventory_agent:      Agent,
    refund_agent:         Agent,
    policy_agent:         Agent,
    communication_agent:  Agent,
    session_manager=None,
) -> Agent:
    """
    Build the Orchestrator Agent that routes requests and manages WorkflowState.

    session_manager (optional): a Strands session manager (e.g. the DynamoDB one
    in standout/dynamodb_session_manager.py). When given, the orchestrator's
    conversation is persisted and restored, so follow-up messages in the same
    chat can refer to earlier turns - even after a restart.
    """

    model = BedrockModel(
        model_id=config.ORCHESTRATOR_MODEL_ID,
        region_name=config.AWS_REGION,
        temperature=0.0,
    )

    system_prompt = """You are the NovaMart OrchestratorAgent. You are a router: you
decide which specialist agents handle a customer request, in which order.
You NEVER answer the customer yourself.

Every message starts with "[Session ID: <id>] [Customer ID: <id>]" followed by
the customer's message. Pass these exact values as session_id and customer_id
to every tool. Call ONE tool at a time and wait for its result.

ROUTING RULES
Rule 1 - Every request, always first: call initialize_session - including
         follow-up messages in a conversation that was already initialized
         (it starts a fresh turn for the new message).
Rule 2 - Order status, order history, return or refund requests:
         call route_to_inventory_agent, THEN route_to_refund_agent.
Rule 3 - Policy meaning questions (return windows, shipping options or rates,
         warranty terms, what a tier includes in general):
         call route_to_policy_agent.
Rule 4 - Account questions about THIS customer ("what is my tier?",
         "am I premium?", "what orders do I have?"):
         call route_to_inventory_agent. NEVER route_to_policy_agent for these -
         the policy agent only knows policy text, not customer data.
Rule 5 - Math / calculation questions (prices, discounts, totals):
         do NOT call the inventory, policy or refund agents.
         Go straight to route_to_communication_agent, which does the math.
         Do not calculate anything yourself.
Rule 6 - Every request, always last: call route_to_communication_agent with
         original_request = the customer's message exactly as written
         (without the bracketed IDs).

A request that matches several rules (e.g. a return plus a policy question)
uses each matching agent once, in the order inventory -> refund -> policy,
then Rule 6. Greetings or anything else: Rule 1, then Rule 6.

Personal policy questions - about THIS customer's own order or tier ("how long
is the warranty on my headphones?", "how long do I have to return it?") -
combine Rule 4 and Rule 3: route_to_inventory_agent first (tier, order facts),
then route_to_policy_agent, so the answer applies to this customer.
General policy questions ("what is the return policy for premium customers?")
stay Rule 3 only.

Follow-up messages ("how long is the warranty on it?") may refer to earlier
turns of this conversation. Pass the workers a self-contained request that
names the order ID or product from earlier (e.g. "How long is the warranty on
the Wireless Headphones Pro from order ORD-27176?").

Examples
- "I want to return my order ORD-27176":
  initialize_session -> route_to_inventory_agent -> route_to_refund_agent -> route_to_communication_agent
- "Where is my order ORD-27177?":
  initialize_session -> route_to_inventory_agent -> route_to_refund_agent -> route_to_communication_agent
- "What is the return policy for premium customers?":
  initialize_session -> route_to_policy_agent -> route_to_communication_agent
- "Am I a premium member?":
  initialize_session -> route_to_inventory_agent -> route_to_communication_agent
- "How long is the warranty on my headphones from ORD-27176?":
  initialize_session -> route_to_inventory_agent -> route_to_policy_agent -> route_to_communication_agent
- "How much are 5 items at $29.99 with 10% off?":
  initialize_session -> route_to_communication_agent

CRITICAL: you are never permitted to write the customer-facing response.
route_to_communication_agent must be your very last tool call for every
request - no exceptions, even if you think you already know the answer, and
even if an earlier tool failed. After it returns, reply with exactly the text
it returned: no preamble, no summary, no additions."""

    # Each routing tool follows the same pattern:
    #   1. read the current WorkflowState  (_read_workflow_state)
    #   2. invoke the worker agent
    #   3. write its result back with optimistic locking
    #      (_update_workflow_state(session_id, {'<column>': text}, expected_version))
    # The terminal trace UI shows each step: trace.step_start(column) before
    # the worker runs and trace.step_done(column, old_version) after.

    def _read_or_create_state(session_id: str, customer_id: str) -> dict:
        """Read WorkflowState; create it if the model skipped initialize_session."""
        state = _read_workflow_state(session_id)
        if state is None:
            try:
                _create_workflow_state(session_id, customer_id)
            except ClientError as exc:
                if exc.response['Error']['Code'] != 'ConditionalCheckFailedException':
                    raise
            state = _read_workflow_state(session_id)
        return state

    def _invoke_and_record(column: str, label: str, worker: Agent,
                           session_id: str, customer_id: str, prompt: str) -> str:
        """Read state -> run worker -> write its result to `column` with optimistic locking."""
        state = _read_or_create_state(session_id, customer_id)
        old_version = int(state['version'])

        trace.step_start(column)
        trace.agent_section(label)
        result = _run_worker(worker, prompt)

        _update_workflow_state(session_id, {column: result}, expected_version=old_version)
        trace.step_done(column, old_version)
        return result

    @tool
    def route_to_inventory_agent(session_id: str, customer_id: str, request: str) -> str:
        """
        Route an order-related request to the Inventory Agent to gather order facts.
        Call this FIRST for any request involving order status, history, or returns,
        and for account questions such as "what is my tier?".

        Args:
            session_id:  The current session identifier (from the customer request)
            customer_id: The customer's unique identifier
            request:     The customer's original request

        Returns:
            Inventory facts retrieved by the InventoryAgent
        """
        prompt = f"[Session ID: {session_id}] [Customer ID: {customer_id}] {request}"
        return _invoke_and_record('inventory_agent', 'InventoryAgent', inventory_agent,
                                  session_id, customer_id, prompt)

    @tool
    def route_to_policy_agent(session_id: str, request: str) -> str:
        """
        Route a policy question to the Policy Agent (multi-agent RAG).
        Call this for questions about return policies, shipping, or warranties.
        Never use it for questions about a specific customer's own account or orders.

        Args:
            session_id: The current session identifier
            request:    The customer's policy question

        Returns:
            Policy information retrieved and synthesized by PolicyAgent
        """
        state = _read_workflow_state(session_id) or {}
        customer_id = state.get('customer_id', 'UNKNOWN')
        return _invoke_and_record('policy_agent', 'PolicyAgent', policy_agent,
                                  session_id, customer_id, request)

    @tool
    def route_to_refund_agent(session_id: str, customer_id: str, request: str) -> str:
        """
        Route a return/refund request to the Refund Agent.
        Call this AFTER route_to_inventory_agent has gathered order facts.

        Args:
            session_id:  The current session identifier
            customer_id: The customer's unique identifier
            request:     The return/refund request

        Returns:
            Refund decision from the RefundAgent
        """
        prompt = f"[Session ID: {session_id}] [Customer ID: {customer_id}] {request}"
        return _invoke_and_record('refund_agent', 'RefundAgent', refund_agent,
                                  session_id, customer_id, prompt)

    @tool
    def route_to_communication_agent(session_id: str, customer_id: str,
                                     original_request: str) -> str:
        """
        Route to the Communication Agent to compose the final customer response.
        Call this LAST - after all relevant worker agents have run. It must be
        the final tool call of every request.

        Args:
            session_id:       The current session identifier
            customer_id:      The customer's unique identifier
            original_request: The customer's original message

        Returns:
            Final customer-facing response drafted by CommunicationAgent
        """
        prompt = (f"[Session ID: {session_id}] [Customer ID: {customer_id}]\n"
                  f"Customer's original request: {original_request}")
        return _invoke_and_record('communication_agent', 'CommunicationAgent',
                                  communication_agent, session_id, customer_id, prompt)

    @tool
    def initialize_session(session_id: str, customer_id: str) -> str:
        """
        Create a blank WorkflowState record at the start of each new session.
        Call this at the VERY BEGINNING of processing every customer request.
        For a follow-up message in an existing session, it starts a new turn:
        the previous turn's agent findings are cleared and the last reply is
        kept as previous_response for conversational context.

        Args:
            session_id:  A unique identifier for this session
            customer_id: The customer's identifier

        Returns:
            Confirmation that the session was initialized
        """
        existing = _read_workflow_state(session_id)
        if existing is None:
            try:
                _create_workflow_state(session_id, customer_id)
                return f"Session {session_id} initialized for {customer_id} (version 0)."
            except ClientError as exc:
                if exc.response['Error']['Code'] != 'ConditionalCheckFailedException':
                    raise
                existing = _read_workflow_state(session_id)

        # Follow-up turn in the same session: reset the per-turn agent columns.
        turn = int(existing.get('turn', 1)) + 1
        updates = {column: '' for column in AGENT_COLUMNS}
        updates['turn'] = turn
        updates['previous_response'] = existing.get('communication_agent', '')
        state = _update_workflow_state(session_id, updates,
                                       expected_version=int(existing['version']))
        return (f"Session {session_id} continued for {customer_id} "
                f"(turn {turn}, version {int(state['version'])}).")

    return Agent(
        name='OrchestratorAgent',
        agent_id='orchestrator',
        model=model,
        system_prompt=system_prompt,
        tools=[initialize_session, route_to_inventory_agent, route_to_policy_agent,
               route_to_refund_agent, route_to_communication_agent],
        # Routing is a strict sequence (inventory must finish before refund),
        # so tool calls are never executed concurrently.
        tool_executor=SequentialToolExecutor(),
        session_manager=session_manager,
    )


# ═══════════════════════════════════════════════════════
#  AGENT GRAPH HELPERS
# ═══════════════════════════════════════════════════════

def _apply_guardrail(agents: list) -> None:
    """
    Attach the Bedrock Guardrail (Task 3) to every agent's BedrockModel.
    Guardrails are enforced per model invocation, so once GUARDRAIL_ID /
    GUARDRAIL_VERSION are known (in .env locally, as runtime environment
    variables when deployed) every agent in the graph runs behind the
    guardrail - no change to the agents themselves is needed.
    """
    guardrail_id      = config.GUARDRAIL_ID
    guardrail_version = config.GUARDRAIL_VERSION
    if not guardrail_id or not guardrail_version:
        return
    for agent in agents:
        model = getattr(agent, 'model', None)
        if model is not None and hasattr(model, 'update_config'):
            model.update_config(guardrail_id=guardrail_id,
                                guardrail_version=guardrail_version)


def build_agent_graph(verbose: bool = False) -> Agent:
    """Build all five agents, apply the guardrail, return the orchestrator."""
    def _ok(label):
        if verbose:
            print(f"  {_C.GRY}          {_C.OK}[OK]{_C.RESET}{_C.GRY}  {label}{_C.RESET}", flush=True)

    inventory_agent     = build_inventory_agent();     _ok('InventoryAgent')
    refund_agent        = build_refund_agent();        _ok('RefundAgent')
    policy_agent        = build_policy_agent();        _ok('PolicyAgent')
    communication_agent = build_communication_agent(); _ok('CommunicationAgent')
    orchestrator = build_orchestrator_agent(
        inventory_agent, refund_agent, policy_agent, communication_agent
    )
    _ok('Orchestrator')
    _apply_guardrail([inventory_agent, refund_agent, policy_agent,
                      communication_agent, orchestrator])
    if verbose and config.GUARDRAIL_ID:
        print(f"  {_C.GRY}          Guardrail {config.GUARDRAIL_ID} "
              f"(v{config.GUARDRAIL_VERSION}) attached to all agents{_C.RESET}")
    return orchestrator


# ═══════════════════════════════════════════════════════
#  DEPLOYMENT TOOLING - AgentCore CLI
#
#  The runtime is deployed with the AgentCore CLI (`agentcore`, npm package
#  @aws/agentcore - https://github.com/aws/agentcore-cli) through the
#  pre-written helper src/agentcore_cli.py:
#
#    agentcore_cli.stage_runtime_code()   copies this file, its helper modules
#                                         and config.py to build/runtime/ with a
#                                         pyproject.toml of the runtime deps
#    agentcore_cli.configure_runtime()    writes the runtime settings (network
#                                         mode, protocol, execution role, env
#                                         vars) to agentcore/agentcore.json
#    agentcore_cli.deploy()               runs `agentcore deploy -y`: the CLI
#                                         downloads arm64 / Python 3.12 wheels
#                                         with uv, zips them with the code
#                                         (direct code deployment) and creates
#                                         or updates the runtime via CDK
#    agentcore_cli.deployed_runtime_arn() reads the ARN the CLI recorded
#
#  Inside the runtime this same file is the entry point: it is started with
#  no command-line argument and serves HTTP (see run_serve). The marker file
#  written next to it by stage_runtime_code() tells __main__ to do so.
# ═══════════════════════════════════════════════════════

_RUNTIME_MARKER = '.agentcore-runtime'         # written by agentcore_cli.stage_runtime_code()
_SRC_DIR        = os.path.dirname(os.path.abspath(__file__))


# ═══════════════════════════════════════════════════════
#  TASK 3 - AGENTCORE DEPLOYMENT + GUARDRAILS
# ═══════════════════════════════════════════════════════

def _guardrail_policies() -> dict:
    """
    Policy configuration of the NovaMart guardrail, shared by create_guardrail()
    and by updates of an existing guardrail (update_guardrail() takes the same
    keyword arguments), so the policies are defined in exactly one place.
    """
    # Content filters: harmful categories at HIGH, insults / misconduct at MEDIUM
    # (applied to both the customer's input and the model's output).
    content_strength = {
        'SEXUAL':     'HIGH',
        'VIOLENCE':   'HIGH',
        'HATE':       'HIGH',
        'INSULTS':    'MEDIUM',
        'MISCONDUCT': 'MEDIUM',
    }
    filters_config = [
        {'type': ftype, 'inputStrength': strength, 'outputStrength': strength}
        for ftype, strength in content_strength.items()
    ]
    # Extra layer beyond the required filters: prompt-injection / jailbreak
    # detection on customer input (the adversarial suite in standout/ showed
    # injection attempts pass the other policies). Output strength must be NONE.
    filters_config.append({'type': 'PROMPT_ATTACK', 'inputStrength': 'HIGH',
                           'outputStrength': 'NONE'})

    # PII: payment card numbers and SSNs are never accepted or echoed;
    # emails and phone numbers are masked instead of blocking the whole message.
    pii_entities_config = [
        {'type': 'CREDIT_DEBIT_CARD_NUMBER',  'action': 'BLOCK'},
        {'type': 'US_SOCIAL_SECURITY_NUMBER', 'action': 'BLOCK'},
        {'type': 'EMAIL',                     'action': 'ANONYMIZE'},
        {'type': 'PHONE',                     'action': 'ANONYMIZE'},
    ]

    # Denied topics - one per config.GUARDRAIL_BLOCKED_TOPICS entry. Pricing
    # negotiation is defined narrowly (haggling / changing an advertised price)
    # so that arithmetic with a price and discount the customer already stated
    # ("5 items at $29.99 with 10% off") is still answered.
    topic_definitions = {
        'competitor products': {
            'definition': ('Discussing, comparing or recommending products, prices, deals or '
                           'services of other retailers or competing stores instead of NovaMart.'),
            'examples': [
                'Is Amazon cheaper than NovaMart for these headphones?',
                'Should I buy this laptop at Best Buy instead?',
                'Which competitor has a better return policy than you?',
            ],
        },
        'pricing negotiations': {
            'definition': ('Haggling or asking NovaMart to lower, match or change an advertised '
                           'price or to grant an unoffered discount. Calculating a total from a '
                           'stated price and discount is not negotiation.'),
            'examples': [
                'Can you give me a better price on this laptop?',
                'I will only buy it if you drop the price by 20 percent.',
                'Will you price match if I find it cheaper somewhere else?',
                'Give me an extra discount or I am cancelling my order.',
            ],
        },
        'legal threats': {
            'definition': ('Threats of lawsuits, legal action, attorneys, court or regulatory '
                           'complaints against NovaMart or its employees.'),
            'examples': [
                'I am going to sue NovaMart if you do not refund me today.',
                'My lawyer will be contacting your company.',
                'I will take you to court over this order.',
            ],
        },
    }
    topics_config = [
        {
            'name':       topic.title(),
            'definition': topic_definitions[topic]['definition'],
            'examples':   topic_definitions[topic]['examples'],
            'type':       'DENY',
        }
        for topic in config.GUARDRAIL_BLOCKED_TOPICS
    ]

    return dict(
        contentPolicyConfig={'filtersConfig': filters_config},
        sensitiveInformationPolicyConfig={'piiEntitiesConfig': pii_entities_config},
        topicPolicyConfig={
            'topicsConfig': topics_config,
            'tierConfig':   {'tierName': 'STANDARD'},
        },
        crossRegionConfig={'guardrailProfileIdentifier': 'us.guardrail.v1:0'},
        wordPolicyConfig={'managedWordListsConfig': [{'type': 'PROFANITY'}]},
        blockedInputMessaging=(
            "I'm sorry, but I can't help with that request. I'm here to help with your "
            "NovaMart orders, returns, shipping and warranty questions - is there "
            "something along those lines I can do for you?"
        ),
        blockedOutputsMessaging=(
            "I'm sorry, but I can't share that response. Please contact NovaMart Customer "
            "Support at support@novamart.example.com and we'll be happy to help."
        ),
    )


def create_guardrail() -> tuple[str, str]:
    """
    Create a Bedrock Guardrail for enterprise safety enforcement.

    Blocks harmful content, PII exposure, off-topic subjects, and profanity.
    Returns (guardrail_id, guardrail_version).
    """
    bedrock_client = boto3.client('bedrock', region_name=config.AWS_REGION)

    # Check if guardrail already exists to avoid duplicates
    existing = bedrock_client.list_guardrails()
    for g in existing.get('guardrails', []):
        if g['name'] == config.GUARDRAIL_NAME:
            guardrail_id = g['id']
            versions = bedrock_client.list_guardrails(guardrailIdentifier=guardrail_id)
            guardrail_version = 'DRAFT'
            for v in versions.get('guardrails', []):
                if v.get('version', 'DRAFT') != 'DRAFT':
                    guardrail_version = v['version']
            print(f"Guardrail already exists: {guardrail_id} (version: {guardrail_version})")
            return guardrail_id, guardrail_version

    response = bedrock_client.create_guardrail(
        name=config.GUARDRAIL_NAME,
        description=('NovaMart customer support guardrail: harmful content, PII, '
                     'off-topic subjects (competitors, price haggling, legal threats), '
                     'profanity and prompt attacks.'),
        clientRequestToken=str(uuid.uuid4()),
        **_guardrail_policies(),
    )
    guardrail_id = response['guardrailId']
    print(f"  Guardrail created: {guardrail_id} (DRAFT)")

    # Promote DRAFT to an immutable, numbered version - agents must reference a
    # published version so later DRAFT edits cannot silently change production.
    version_response = bedrock_client.create_guardrail_version(
        guardrailIdentifier=guardrail_id,
        description='Initial published version for the NovaMart AgentCore runtime',
        clientRequestToken=str(uuid.uuid4()),
    )
    guardrail_version = version_response['version']

    # Wait until the published version is READY before agents use it.
    deadline = time.time() + 120
    while time.time() < deadline:
        status = bedrock_client.get_guardrail(guardrailIdentifier=guardrail_id,
                                              guardrailVersion=guardrail_version)['status']
        if status == 'READY':
            break
        if status == 'FAILED':
            raise RuntimeError(f"Guardrail {guardrail_id} v{guardrail_version} failed to publish")
        time.sleep(3)
    print(f"  Guardrail version published: {guardrail_version}")
    return guardrail_id, guardrail_version


def deploy_to_agentcore_runtime(
    orchestrator_agent: Agent,
    guardrail_id: str,
    guardrail_version: str
) -> str:
    """
    Deploy the multi-agent system to Amazon Bedrock AgentCore Runtime with
    the AgentCore CLI (src/agentcore_cli.py wraps it).

    AgentCore does not serialize Python objects, so `orchestrator_agent` is
    not uploaded directly. Instead the pre-written staging step copies this
    file, which doubles as the HTTP entry point (see run_serve), together
    with its helper modules and config.py to build/runtime/. `agentcore
    deploy` then packages that directory with arm64 dependencies and creates
    or updates the runtime ("direct code deployment"). Re-running is safe:
    an unchanged runtime is left alone, a changed one is updated in place.

    The guardrail is attached by environment variables: inside the runtime
    build_agent_graph() reads GUARDRAIL_ID / GUARDRAIL_VERSION and applies
    them to every agent's model (see _apply_guardrail), exactly as `test`
    and `chat` do locally.

    Returns:
        The AgentCore Runtime ARN
    """
    import agentcore_cli

    runtime_name = config.AGENTCORE_RUNTIME_NAME
    print(f"  AWS Account: {config.ACCOUNT_ID}  |  Region: {config.AWS_REGION}")
    print(f"  Runtime: {runtime_name}  |  CLI project: agentcore/agentcore.json "
          f"(stack {agentcore_cli.stack_name()})")
    previous_arn = agentcore_cli.deployed_runtime_arn()
    if previous_arn:
        print(f"  Runtime already deployed - updating it: {previous_arn}")

    # Stage the code the CLI packages (src modules + config.py + pyproject.toml).
    agentcore_cli.stage_runtime_code()

    # 1. Runtime environment variables - inside the runtime, config.py and
    #    _apply_guardrail() read these instead of the local .env file.
    runtime_env = {
        'AWS_REGION':        config.AWS_REGION,
        'PROJECT_NAME':      config.PROJECT_NAME,
        'RETURNS_KB_ID':     config.RETURNS_KB_ID,
        'SHIPPING_KB_ID':    config.SHIPPING_KB_ID,
        'WARRANTY_KB_ID':    config.WARRANTY_KB_ID,
        'AGENT_LOG_GROUP':   config.AGENT_LOG_GROUP,
        'GUARDRAIL_ID':      guardrail_id,
        'GUARDRAIL_VERSION': guardrail_version,
    }
    missing = [k for k, v in runtime_env.items() if not v]
    if missing:
        print(f"  [Warning] empty runtime variables (skipped until set): {', '.join(missing)}")

    # 2. Runtime settings -> agentcore/agentcore.json
    agentcore_cli.configure_runtime(
        env_vars=runtime_env,
        network_mode='PUBLIC',
        protocol='HTTP',
        execution_role_arn=config.AGENTCORE_ROLE_ARN,
    )

    # 3. Package build/runtime/ with arm64 wheels and create/update the runtime (CDK)
    agentcore_cli.deploy()

    # 4. ARN recorded by the CLI in agentcore/.cli/deployed-state.json
    runtime_arn = agentcore_cli.deployed_runtime_arn()

    if not runtime_arn:
        raise NotImplementedError("deploy_to_agentcore_runtime: AgentCore CLI deployment not implemented")

    # Wait for the runtime to become READY and return its ARN.
    print(f"  Runtime deployed: {runtime_arn}")
    print("  Waiting for runtime status READY", end='', flush=True)
    wait_for_runtime_ready(agentcore_control, runtime_arn.split('/')[-1])
    print(' ready.')
    return runtime_arn


# ═══════════════════════════════════════════════════════
#  TASK 4 - MEMORY
# ═══════════════════════════════════════════════════════

def configure_memory(runtime_arn: str) -> str:
    """
    Create an AgentCore Memory resource for session-scoped conversational
    context. Uses the SESSION_SUMMARY (summaryMemoryStrategy) strategy with
    7-day event retention.

    Returns:
        The memory resource ARN
    """
    memory_name = config.MEMORY_NAME
    existing = agentcore_control.list_memories()
    for m in existing.get('memories', []):
        if m['id'].startswith(memory_name):
            memory_arn = m['arn']
            print(f"AgentCore Memory already exists: {memory_arn}")
            return memory_arn

    # SESSION_SUMMARY strategy: AgentCore condenses each session's events into
    # a running summary, stored per customer (actorId) and session (sessionId).
    response = agentcore_control.create_memory(
        name=memory_name,
        description=('NovaMart customer support conversation memory: session summaries '
                     'so customers do not have to repeat themselves between turns.'),
        eventExpiryDuration=7,                       # raw events kept for 7 days
        memoryStrategies=[{
            'summaryMemoryStrategy': {
                'name':        'SessionSummary',
                'description': 'Summarizes each customer support session',
                'namespaces':  ['/summaries/{actorId}/{sessionId}'],
            }
        }],
        clientToken=str(uuid.uuid4()),               # idempotent retries
    )

    if response is None:
        raise NotImplementedError("configure_memory: create_memory() not implemented")

    # Wait until the memory resource is ACTIVE and return its ARN.
    memory = response['memory']
    print(f"  Memory created: {memory['arn']}  (status: {memory['status']})")
    print("  Waiting for memory status ACTIVE", end='', flush=True)
    deadline = time.time() + 300
    while memory['status'] != 'ACTIVE' and time.time() < deadline:
        time.sleep(10)
        print('.', end='', flush=True)
        memory = agentcore_control.get_memory(memoryId=memory['id'])['memory']
        if memory['status'] == 'FAILED':
            raise RuntimeError(f"Memory creation failed: {memory.get('failureReason')}")
    print(' ready.' if memory['status'] == 'ACTIVE' else f" status {memory['status']}")
    return memory['arn']


# ═══════════════════════════════════════════════════════
#  TASK 6 - OBSERVABILITY
# ═══════════════════════════════════════════════════════

def configure_observability(runtime_arn: str) -> None:
    """
    Configure observability for the deployed agent:
    - Agent logs → CloudWatch Logs at INFO level (config.AGENT_LOG_GROUP)
    - Execution traces → AWS X-Ray at 100% sampling

    The loggingConfiguration built here is applied by
    apply_observability_config() (agent_observability.py):
      cloudWatchConfig -> log group created; runtime env AGENT_LOG_GROUP /
                          AGENT_LOG_LEVEL so the deployed agent ships its logs there
      xRayConfig       -> CloudWatch Transaction Search enabled with the given
                          sampling percentage; runtime env AGENT_TRACING_ENABLED /
                          AGENT_TRACE_SAMPLING_RATE
    """
    logging_configuration = {
        # CloudWatch Logs: agent reasoning, tool calls and timings at INFO level
        'cloudWatchConfig': {
            'logGroupName': config.AGENT_LOG_GROUP,
            'logLevel':     'INFO',
            'enabled':      True,
        },
        # X-Ray: trace 100% of requests in development (reduce to ~0.05 in production)
        'xRayConfig': {
            'enabled':      True,
            'samplingRate': 1.0,
        },
    }

    try:
        summary = apply_observability_config(runtime_arn, logging_configuration)
        print(f"  CloudWatch Logs : {summary['log_group']} "
              f"(level {logging_configuration['cloudWatchConfig']['logLevel']})")
        print(f"  X-Ray tracing   : enabled, sampling rate "
              f"{logging_configuration['xRayConfig']['samplingRate']:.0%}")
    except Exception as e:
        print(f"  [Note] Observability configuration failed: {e}")


# ═══════════════════════════════════════════════════════
#  AGENTCORE GATEWAY DEPLOYMENT
#
#  Production equivalent of in-process @tool functions.
#  Registers Lambda-backed tools on a managed MCP endpoint so tools
#  can be independently deployed, versioned, and discovered at runtime.
#
#  Deployment pattern:
#    Local dev  → LambdaGateway + gateway.register_target(...)
#    Production → deploy_agentcore_gateway() using real AWS API
#
#  Requires Lambda tool functions to be deployed separately.
#  Set ORDERS_FUNCTION, POLICY_FUNCTION, CUSTOMERS_FUNCTION in .env
#  to the deployed Lambda function names.
# ═══════════════════════════════════════════════════════

# Lambda function names for gateway tool backends (set in .env after deploying)
_ORDERS_FUNCTION = os.environ.get('ORDERS_FUNCTION', '')
_POLICY_FUNCTION = os.environ.get('POLICY_FUNCTION', '')
_CUSTOMERS_FUNCTION = os.environ.get('CUSTOMERS_FUNCTION', '')


def _gw_get_function_arn(function_name: str) -> str:
    """Resolve a Lambda function name to its full ARN."""
    lambda_client = boto3.client('lambda', region_name=config.AWS_REGION)
    resp = lambda_client.get_function(FunctionName=function_name)
    return resp['Configuration']['FunctionArn']


def _gw_stack_uuid() -> str:
    """Return the short UUID from the project CloudFormation stack ID.
    Gives the gateway a stable name so re-runs never hit ConflictException."""
    cf = boto3.client('cloudformation', region_name=config.AWS_REGION)
    stacks = cf.describe_stacks(StackName=config.PROJECT_NAME)
    stack_id = stacks['Stacks'][0]['StackId']
    full_uuid = stack_id.split('/')[-1]
    return full_uuid.split('-')[0]


def _gw_wait_for_ready(agentcore_ctrl, gateway_id: str, timeout: int = 120) -> str:
    """Poll until the gateway reaches READY status. Returns the gateway URL."""
    deadline = time.time() + timeout
    first    = True
    while time.time() < deadline:
        gw     = agentcore_ctrl.get_gateway(gatewayIdentifier=gateway_id)
        status = gw['status']
        if status == 'READY':
            if not first:
                print(' ready.')
            return gw.get('gatewayUrl', '')
        if 'FAILED' in status:
            print(f' failed: {status}')
            raise RuntimeError(f"Gateway {gateway_id} entered status {status}")
        if first:
            print('    Gateway provisioning (async — normal AWS behaviour)',
                  end='', flush=True)
            first = False
        print('.', end='', flush=True)
        time.sleep(5)
    raise TimeoutError(f"Gateway {gateway_id} not READY after {timeout}s")


def _gw_get_or_create(agentcore_ctrl, name: str, role_arn: str,
                       instructions: str) -> tuple[str, str]:
    """Create an AgentCore Gateway, or reuse it if it already exists."""
    try:
        gw = agentcore_ctrl.create_gateway(
            name=name,
            roleArn=role_arn,
            protocolType='MCP',
            authorizerType='NONE',
            protocolConfiguration={'mcp': {'instructions': instructions,
                                            'searchType': 'SEMANTIC'}},
        )
        gw_id  = gw['gatewayId']
        print(f'    Gateway ID  : {gw_id}')
        print(f'    Status      : {gw["status"]}')
        gw_url = _gw_wait_for_ready(agentcore_ctrl, gw_id)
        print(f'    Gateway URL : {gw_url}')
        return gw_id, gw_url
    except agentcore_ctrl.exceptions.ConflictException:
        print(f"    Gateway '{name}' already exists — reusing it.")
        gateways = agentcore_ctrl.list_gateways().get('items', [])
        existing = next((g for g in gateways if g['name'] == name), None)
        if not existing:
            raise RuntimeError(f"Gateway '{name}' not found after ConflictException")
        gw_id  = existing['gatewayId']
        print(f'    Gateway ID  : {gw_id}')
        gw_url = _gw_wait_for_ready(agentcore_ctrl, gw_id)
        print(f'    Gateway URL : {gw_url}')
        return gw_id, gw_url


def _gw_create_target(agentcore_ctrl, gateway_id: str, t: dict,
                       lambda_arn: str) -> None:
    """Register one Lambda target on the gateway. Skips if it already exists."""
    payload = dict(
        gatewayIdentifier=gateway_id,
        name=t['name'],
        description=t['description'],
        targetConfiguration={
            'mcp': {
                'lambda': {
                    'lambdaArn': lambda_arn,
                    'toolSchema': {
                        'inlinePayload': [{
                            'name':        t['tool_name'],
                            'description': t['tool_description'],
                            'inputSchema': {
                                'type': 'object',
                                'properties': {
                                    t['param_name']: {
                                        'type':        'string',
                                        'description': t['param_desc'],
                                    }
                                },
                                'required': [t['param_name']],
                            },
                        }]
                    },
                }
            }
        },
        credentialProviderConfigurations=[
            {'credentialProviderType': 'GATEWAY_IAM_ROLE'}
        ],
    )
    try:
        resp = agentcore_ctrl.create_gateway_target(**payload)
        print(f"    [{resp['status']:12s}] {t['name']} → target {resp['targetId']}")
    except agentcore_ctrl.exceptions.ConflictException:
        print(f"    [already exists] {t['name']} — skipped")


def deploy_agentcore_gateway() -> dict:
    """
    Create an AgentCore Gateway and register the NovaMart tool Lambda targets.

    Optional extension to the in-process @tool functions. Resolve configured
    Lambda functions first; if none exist, skip gateway creation. Otherwise
    create/reuse the gateway and submit its targets. Connecting agents to this
    MCP endpoint requires separate integration; this starter uses in-process tools.

    Requires Lambda tool functions to be deployed via a separate stack.
    Set ORDERS_FUNCTION, POLICY_FUNCTION, CUSTOMERS_FUNCTION in .env.

    Returns:
        A SKIPPED result with a reason, or gateway details and target count.
    """
    targets = [
        {
            'name':             'orders-api',
            'description':      'Look up order details, status, and return eligibility for a customer',
            'function':         _ORDERS_FUNCTION,
            'tool_name':        'check_order_status',
            'tool_description': 'Check order status and return eligibility for a specific order',
            'param_name':       'order_id',
            'param_desc':       'Order ID (e.g. ORD-27176)',
        },
        {
            'name':             'policy-api',
            'description':      'Retrieve return, shipping, and warranty policy text from knowledge bases',
            'function':         _POLICY_FUNCTION,
            'tool_name':        'search_policies',
            'tool_description': 'Search all policy knowledge bases for a customer query',
            'param_name':       'query',
            'param_desc':       'Customer question about returns, shipping, or warranty',
        },
        {
            'name':             'customers-api',
            'description':      'Look up customer tier (Standard or Premium) and account details',
            'function':         _CUSTOMERS_FUNCTION,
            'tool_name':        'get_customer_tier',
            'tool_description': 'Get customer tier and account information by customer ID',
            'param_name':       'customer_id',
            'param_desc':       'Customer ID (e.g. CUST-001)',
        },
    ]

    # Resolve optional Lambda targets before creating any gateway resources.
    available = []
    for target in targets:
        if not target['function']:
            continue
        try:
            available.append((target, _gw_get_function_arn(target['function'])))
        except ClientError as exc:
            if exc.response['Error']['Code'] != 'ResourceNotFoundException':
                raise
            print(f"    [Skipped] {target['name']}: Lambda function not found")

    if not available:
        return {'status': 'SKIPPED', 'reason': 'No configured Lambda tool functions are available.'}

    agentcore_ctrl = boto3.client('bedrock-agentcore-control',
                                   region_name=config.AWS_REGION)

    try:
        gw_uuid = _gw_stack_uuid()
    except Exception:
        gw_uuid = config.PROJECT_NAME

    gw_name = f"novamart-support-{gw_uuid}"
    print(f"  Calling create_gateway (name: {gw_name})...")
    gateway_id, gateway_url = _gw_get_or_create(
        agentcore_ctrl, gw_name, config.AGENTCORE_ROLE_ARN,
        "NovaMart customer support gateway. Provides order lookup, "
        "policy search, and customer tier tools.",
    )

    print(f"\n  Registering {len(available)} Gateway targets...")
    for target, lambda_arn in available:
        _gw_create_target(agentcore_ctrl, gateway_id, target, lambda_arn)

    return {'gateway_id': gateway_id, 'gateway_url': gateway_url,
            'status': 'TARGETS_SUBMITTED', 'target_count': len(available)}



# ═══════════════════════════════════════════════════════
#  RUNTIME INVOCATION
# ═══════════════════════════════════════════════════════

def invoke_agent(session_id: str, customer_id: str, user_message: str) -> dict:
    """
    Invoke the deployed agent via AgentCore Runtime (see run_serve).

    AgentCore requires runtimeSessionId to be at least 33 characters, so the
    short project session id is embedded in a longer, unique runtime session id.
    """
    if not config.AGENTCORE_RUNTIME_ARN:
        raise RuntimeError("AGENTCORE_RUNTIME_ARN is not set - run the deploy command first")

    runtime_session_id = f"{session_id}-{uuid.uuid4().hex}"     # >= 33 chars
    payload = json.dumps({
        'prompt':      user_message,
        'session_id':  session_id,
        'customer_id': customer_id,
    })
    response = agentcore_client.invoke_agent_runtime(
        agentRuntimeArn=config.AGENTCORE_RUNTIME_ARN,
        runtimeSessionId=runtime_session_id,
        contentType='application/json',
        accept='application/json',
        payload=payload,
    )
    body = response['response'].read()
    try:
        return json.loads(body)
    except (TypeError, ValueError):
        return {'result': body.decode('utf-8', errors='replace') if isinstance(body, bytes) else str(body)}


# ═══════════════════════════════════════════════════════
#  DEPLOYMENT ENTRY POINT
# ═══════════════════════════════════════════════════════

def deploy_all():
    """Full deployment pipeline. Run after completing all tasks."""
    print("\n" + "="*60)
    print("  Deploying Enterprise Multi-Agent System")
    print("="*60 + "\n")

    # Fail fast if the AgentCore CLI (used by Steps 3 and 5) is missing.
    import agentcore_cli
    print(f"AgentCore CLI: {agentcore_cli.cli_version()} ({agentcore_cli.cli_path()})\n")

    print("Step 1/6: Building agent graph...")
    inventory_agent     = build_inventory_agent()
    refund_agent        = build_refund_agent()
    policy_agent        = build_policy_agent()
    communication_agent = build_communication_agent()
    orchestrator = build_orchestrator_agent(
        inventory_agent, refund_agent, policy_agent, communication_agent
    )
    print("  All 5 agents initialized\n")

    print("Step 2/6: Creating Bedrock Guardrail...")
    guardrail_id, guardrail_version = create_guardrail()
    print()

    print("Step 3/6: Deploying to AgentCore Runtime...")
    runtime_arn = deploy_to_agentcore_runtime(orchestrator, guardrail_id, guardrail_version)
    print()

    print("Step 4/6: Configuring Memory...")
    memory_arn = configure_memory(runtime_arn)
    print()

    print("Step 5/6: Configuring Observability...")
    configure_observability(runtime_arn)
    print()

    print("Step 6/6: Deploying AgentCore Gateway...")
    try:
        gw = deploy_agentcore_gateway()
        if gw['status'] == 'SKIPPED':
            print(f"  [Skipped] Gateway: {gw['reason']}")
        else:
            print(f"  Gateway URL : {gw['gateway_url']}")
            print("  Lambda targets submitted; connect an MCP client separately to use them.")
    except Exception as e:
        print(f"  [Note] Optional Gateway deployment failed: {e}")
        print(f"  (Deploy Lambda tool functions and set ORDERS_FUNCTION etc. in .env to enable)")
    print()

    print("="*60)
    print("  Deployment Complete!")
    print("="*60)
    print(f"\n  Add these to your .env file:")
    print(f"  AGENTCORE_RUNTIME_ARN={runtime_arn}")
    print(f"  GUARDRAIL_ID={guardrail_id}")
    print(f"  GUARDRAIL_VERSION={guardrail_version}\n")
    print(f"  Then try the deployed runtime:")
    print(f"  python src/agent_orchestrator.py invoke \"What is the return policy for premium customers?\"")
    print(f"  or with the CLI:  agentcore invoke \"What is the return policy for premium customers?\"")
    print(f"  (agentcore status / agentcore logs show the deployed runtime and its logs)\n")
    return runtime_arn, guardrail_id


# ═══════════════════════════════════════════════════════
#  LOCAL TEST SCENARIOS
# ═══════════════════════════════════════════════════════

# Order IDs match infrastructure/seed_data.py.
TEST_CASES = [
    ("CUST-001", "I want to return my wireless headphones from order ORD-27176"),
    ("CUST-002", "What is the return policy for premium customers?"),
    ("CUST-003", "How much would 5 items at $29.99 be with a 10% discount?"),
]

# Test customers shown by the chat command. Data matches seed_data.py.
TEST_CUSTOMERS = [
    ("CUST-001", "Alice Johnson", "Premium",  "ORD-27176", "Wireless Headphones Pro"),
    ("CUST-002", "Bob Smith",     "Standard", "ORD-28001", "Mechanical Keyboard K2"),
    ("CUST-003", "Carol Davis",   "Premium",  "ORD-29001", "Laptop UltraBook 14"),
    ("CUST-004", "David Lee",     "Standard", "ORD-30001", "Phone Case Slim"),
]


def run_test_scenarios() -> None:
    """Run the three scenarios locally; every request is traced to X-Ray."""
    print("Running local agent test...")
    setup_logging(to_cloudwatch=True)
    orchestrator = build_agent_graph()

    for customer_id, query in TEST_CASES:
        session_id = str(uuid.uuid4())[:8]
        print(f"\n{'─'*60}")
        print(f"Session: {session_id} | Customer: {customer_id}")
        print(f"Query: {query}")
        prompt = f"[Session ID: {session_id}] [Customer ID: {customer_id}] {query}"
        with tracer.trace_request(session_id, customer_id, query):
            response = orchestrator(prompt)
        print(f"Response: {response}")
        print_trace_hint()
    flush_logs()


def run_chat() -> None:
    """Interactive terminal chat - educational mode."""
    W = _C.W

    # ── Welcome banner ────────────────────────────────────────────────
    print()
    print(f"  {_C.GRY}{'=' * W}{_C.RESET}")
    print(f"  {_C.ORCH}{_C.BOLD}{'NovaMart -- Multi-Agent Customer Support':^{W}}{_C.RESET}")
    print(f"  {_C.GRY}{'Strands Agents SDK  +  Amazon Bedrock AgentCore':^{W}}{_C.RESET}")
    print(f"  {_C.GRY}{'=' * W}{_C.RESET}")

    # ── Test customers ────────────────────────────────────────────────
    print()
    print(f"  {_C.GRY}{'─' * W}{_C.RESET}")
    print(f"  {_C.BOLD}Test Customers{_C.RESET}")
    print(f"  {_C.GRY}{'─' * W}{_C.RESET}")
    print(f"  {_C.GRY}{'ID':<10}  {'Name':<18}  {'Tier':<10}  {'Order':<12}  Product{_C.RESET}")
    print(f"  {_C.GRY}{'─'*8}  {'─'*16}  {'─'*8}  {'─'*10}  {'─'*20}{_C.RESET}")
    for cid, name, tier, order, product in TEST_CUSTOMERS:
        tier_col = _C.INV if tier == 'Premium' else _C.GRY
        print(f"  {_C.BOLD}{cid}{_C.RESET}  {name:<18}  "
              f"{tier_col}{tier:<10}{_C.RESET}  {order}  {product}")
    print(f"  {_C.GRY}{'─' * W}{_C.RESET}")
    print()

    customer_id = (
        input(f"  Enter Customer ID (default: CUST-001): ").strip()
        or "CUST-001"
    )
    session_id  = str(uuid.uuid4())[:8]
    print()
    print(f"  {_C.GRY}Session  : {_C.RESET}{_C.BOLD}{session_id}{_C.RESET}")
    print(f"  {_C.GRY}Customer : {_C.RESET}{_C.BOLD}{customer_id}{_C.RESET}")
    print(f"  {_C.GRY}Type a question and press Enter.  Type 'quit' to exit.{_C.RESET}")
    print()

    # ── Build agents and show initialization order.
    print(f"  {_C.GRY}[SYSTEM]  Initializing agent graph...{_C.RESET}")
    setup_logging(to_cloudwatch=True)
    orchestrator = build_agent_graph(verbose=True)
    print(f"  {_C.GRY}[SYSTEM]  All 5 agents ready.{_C.RESET}")
    print()

    # ── Conversation loop ─────────────────────────────────────────────
    while True:
        try:
            user_input = input(
                f"  {_C.BOLD}You >{_C.RESET} "
            ).strip()
        except (EOFError, KeyboardInterrupt):
            print(f"\n  {_C.GRY}Session ended.{_C.RESET}")
            break

        if not user_input:
            continue
        if user_input.lower() in ('quit', 'exit', 'q'):
            print(f"  {_C.GRY}Session ended.{_C.RESET}")
            break

        prompt  = (f"[Session ID: {session_id}] "
                   f"[Customer ID: {customer_id}] {user_input}")
        t0_turn = time.time()

        # ── Install proxy, run orchestrator (traced), restore stdout ───
        trace.new_turn()
        sys.stdout = _trace_writer
        try:
            with tracer.trace_request(session_id, customer_id, user_input):
                response = orchestrator(prompt)
        finally:
            sys.stdout = _real_stdout   # always restore, even on exception

        elapsed = time.time() - t0_turn

        # ── Resolve the final customer-facing text ────────────────────
        final_state = _read_workflow_state(session_id) or {}
        comm_result = final_state.get('communication_agent', '')
        text = _strip_xml_tags(comm_result or str(response))

        # ── DynamoDB workflow state summary ───────────────────────────
        trace.summary(session_id, elapsed)

        # ── Final customer-facing response ────────────────────────────
        print()
        print(f"  {_C.GRY}{'=' * W}{_C.RESET}")
        print(f"  {_C.COM}{_C.BOLD}AGENT RESPONSE{_C.RESET}")
        print(f"  {_C.GRY}{'=' * W}{_C.RESET}")
        for line in text.splitlines():
            print(f"  {line}")
        print(f"  {_C.GRY}{'=' * W}{_C.RESET}")
        if tracer.last_trace_id:
            print(f"  {_C.GRY}X-Ray trace : {tracer.last_trace_id}"
                  f"{'' if tracer.last_published else '  (not published)'}{_C.RESET}")
        print()
    flush_logs()


def run_invoke(message: str, customer_id: str = "CUST-001") -> None:
    """Send one message to the deployed AgentCore Runtime and print the reply."""
    session_id = str(uuid.uuid4())[:8]
    print(f"Invoking {config.AGENTCORE_RUNTIME_ARN}")
    print(f"Session: {session_id} | Customer: {customer_id}")
    print(f"Query: {message}\n")
    result = invoke_agent(session_id, customer_id, message)
    print(f"Response: {result.get('result', result)}")
    if result.get('trace_id'):
        print(f"X-Ray trace: {result['trace_id']}")


def run_serve() -> None:
    """
    HTTP entry point executed inside Amazon Bedrock AgentCore Runtime.

    BedrockAgentCoreApp (bedrock-agentcore SDK) exposes the contract the
    runtime expects - POST /invocations and GET /ping on port 8080 - and hands
    each request payload to the function decorated with @app.entrypoint.

    Request payload (see invoke_agent):
        {"prompt": "<customer message>", "customer_id": "CUST-001", "session_id": "abc12345"}
    Response:
        {"result": "<final customer-facing text>", "session_id": ..., "trace_id": ...}

    The five-agent graph is built once (first request) and reused. Guardrail,
    tracing and logging are applied exactly as in the local test/chat modes,
    from the runtime's environment variables.
    """
    from bedrock_agentcore import BedrockAgentCoreApp

    os.environ.setdefault('AGENT_RUNTIME_MODE', 'agentcore-runtime')
    if os.environ.get('AGENT_LOG_GROUP') and 'AGENT_LOG_TO_CLOUDWATCH' not in os.environ:
        os.environ['AGENT_LOG_TO_CLOUDWATCH'] = 'true'

    app   = BedrockAgentCoreApp()
    lock  = threading.Lock()
    graph = {}

    def _orchestrator():
        with lock:
            if 'agent' not in graph:
                setup_logging()
                graph['agent'] = build_agent_graph()
        return graph['agent']

    @app.entrypoint
    def invoke(payload, context=None):
        payload     = payload or {}
        prompt      = payload.get('prompt') or payload.get('message') or ''
        customer_id = payload.get('customer_id') or 'CUST-001'
        session_id  = payload.get('session_id') or (
            getattr(context, 'session_id', None) or uuid.uuid4().hex)[:8]
        if not prompt:
            return {'error': "payload must include 'prompt'"}

        enriched = f"[Session ID: {session_id}] [Customer ID: {customer_id}] {prompt}"
        with tracer.trace_request(session_id, customer_id, prompt):
            response = _orchestrator()(enriched)

        state = _read_workflow_state(session_id) or {}
        text  = _strip_xml_tags(state.get('communication_agent', '') or str(response))
        flush_logs()
        return {'result': text, 'session_id': session_id, 'customer_id': customer_id,
                'trace_id': tracer.last_trace_id}

    app.run()


if __name__ == '__main__':
    command = sys.argv[1] if len(sys.argv) > 1 else ''

    # Inside the AgentCore Runtime package (marker file next to this script)
    # the entry point is started without arguments -> serve HTTP.
    if not command and os.path.exists(os.path.join(_SRC_DIR, _RUNTIME_MARKER)):
        command = 'serve'

    if command == 'deploy':
        deploy_all()

    elif command == 'serve':
        run_serve()

    elif command == 'test':
        run_test_scenarios()

    elif command == 'chat':
        run_chat()

    elif command == 'invoke':
        if len(sys.argv) < 3:
            print('Usage: python src/agent_orchestrator.py invoke "<message>" [CUSTOMER_ID]')
            sys.exit(1)
        run_invoke(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "CUST-001")

    else:
        print("Usage:")
        print("  python src/agent_orchestrator.py deploy           # Deploy to AgentCore (Tasks 3-6)")
        print("  python src/agent_orchestrator.py test             # Run the 3 test scenarios locally")
        print("  python src/agent_orchestrator.py chat             # Interactive terminal chat")
        print("  python src/agent_orchestrator.py invoke \"<msg>\"   # Call the deployed runtime")
        print("  python src/agent_orchestrator.py serve            # HTTP server (used inside AgentCore Runtime)")
