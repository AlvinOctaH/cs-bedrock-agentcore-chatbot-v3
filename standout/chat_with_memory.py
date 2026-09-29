"""
chat_with_memory.py
===================
Stand-out: multi-turn chat whose OrchestratorAgent history is persisted in the
DynamoDB `agent-sessions` table (standout/dynamodb_session_manager.py).

Start a chat, quit, and resume it later with the same session id - the
orchestrator remembers the earlier turns, so follow-ups like "how long is the
warranty on it?" still resolve to the right order.

Usage:
    python standout/chat_with_memory.py                          # new session, CUST-001
    python standout/chat_with_memory.py --customer CUST-002
    python standout/chat_with_memory.py --session <id>           # resume a session
    python standout/chat_with_memory.py --session <id> --ask "How long is the warranty on it?"
"""

import argparse
import os
import sys
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'src'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import agent_orchestrator as ao
from agent_observability import tracer, setup_logging, flush_logs
from agent_utils import _strip_xml_tags
from dynamodb_session_manager import DynamoDbSessionManager, ensure_sessions_table, SESSIONS_TABLE


def build_graph_with_memory(session_id: str):
    """Same five-agent graph as build_agent_graph(), with a persistent orchestrator session."""
    inventory     = ao.build_inventory_agent()
    refund        = ao.build_refund_agent()
    policy        = ao.build_policy_agent()
    communication = ao.build_communication_agent()
    orchestrator  = ao.build_orchestrator_agent(
        inventory, refund, policy, communication,
        session_manager=DynamoDbSessionManager(session_id=session_id),
    )
    ao._apply_guardrail([inventory, refund, policy, communication, orchestrator])
    return orchestrator


def ask(orchestrator, session_id: str, customer_id: str, message: str) -> str:
    prompt = f"[Session ID: {session_id}] [Customer ID: {customer_id}] {message}"
    with tracer.trace_request(session_id, customer_id, message):
        response = orchestrator(prompt)
    state = ao._read_workflow_state(session_id) or {}
    return _strip_xml_tags(state.get('communication_agent') or str(response))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--session', help='session id to resume (default: new session)')
    parser.add_argument('--customer', default='CUST-001')
    parser.add_argument('--ask', help='send one message and exit (non-interactive)')
    args = parser.parse_args()

    ensure_sessions_table()
    session_id = args.session or uuid.uuid4().hex[:8]
    setup_logging(to_cloudwatch=True)
    orchestrator = build_graph_with_memory(session_id)

    restored = len(orchestrator.messages)
    print(f"\nSession {session_id} | customer {args.customer} | table {SESSIONS_TABLE}")
    print(f"Restored {restored} message(s) from DynamoDB"
          + (" - continuing the earlier conversation." if restored else " - new conversation."))

    def turn(text: str) -> None:
        print(f"\nYou > {text}")
        reply = ask(orchestrator, session_id, args.customer, text)
        print(f"\nNovaMart >\n{reply}")
        print(f"\n  [memory] orchestrator history: {len(orchestrator.messages)} messages persisted")

    if args.ask:
        turn(args.ask)
    else:
        print("Type a message (or 'quit').")
        while True:
            try:
                text = input("\nYou > ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if text.lower() in ('quit', 'exit', 'q'):
                break
            if text:
                turn(text)
    flush_logs()
    print(f"\nResume later with:  python standout/chat_with_memory.py --session {session_id} "
          f"--customer {args.customer}")


if __name__ == '__main__':
    main()
