"""
dynamodb_session_manager.py
===========================
Stand-out: persistent conversation memory for the OrchestratorAgent in a
DynamoDB `agent-sessions` table.

Strands ships file- and S3-backed session managers; this module adds a
DynamoDB-backed one through the SDK's official extension point
(`SessionRepository` + `RepositorySessionManager`). Every message the
orchestrator exchanges is written to DynamoDB as it happens, so a chat can be
resumed - even from a new process or another machine - and the orchestrator
still knows what "it" or "that order" refers to.

Single-table design  (table: <PROJECT_NAME>-agent-sessions)

    pk = "SESSION#<session_id>"
    sk = "SESSION"                                  -> session metadata
    sk = "AGENT#<agent_id>"                         -> agent state
    sk = "AGENT#<agent_id>#MSG#<000000000042>"      -> one message (zero-padded -> sorted)
    data = JSON document written by Strands  (stored as a string: no Decimal conversion)
    ttl  = epoch seconds; DynamoDB expires the item after SESSION_TTL_DAYS

How it complements AgentCore Memory (Task 4):
    DynamoDB session storage  - exact message history of one chat, loaded back
                                into the agent verbatim (short-term, local control)
    AgentCore Memory          - managed cloud memory that SUMMARIZES sessions
                                (long-term, cross-session context)

Usage:
    python standout/dynamodb_session_manager.py create-table
    python standout/dynamodb_session_manager.py show <session_id>
    python standout/dynamodb_session_manager.py delete-table
"""

import json
import os
import sys
import time
from typing import Any, Optional

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

from strands.session.repository_session_manager import RepositorySessionManager
from strands.session.session_repository import SessionRepository
from strands.types.exceptions import SessionException
from strands.types.session import Session, SessionAgent, SessionMessage

SESSIONS_TABLE   = f"{config.PROJECT_NAME}-agent-sessions"
SESSION_TTL_DAYS = 7          # same retention as the AgentCore Memory events


def ensure_sessions_table(table_name: str = SESSIONS_TABLE) -> str:
    """Create the on-demand sessions table (pk/sk + TTL) if it does not exist yet."""
    client = boto3.client('dynamodb', region_name=config.AWS_REGION)
    try:
        client.describe_table(TableName=table_name)
        return table_name
    except client.exceptions.ResourceNotFoundException:
        pass
    client.create_table(
        TableName=table_name,
        AttributeDefinitions=[{'AttributeName': 'pk', 'AttributeType': 'S'},
                              {'AttributeName': 'sk', 'AttributeType': 'S'}],
        KeySchema=[{'AttributeName': 'pk', 'KeyType': 'HASH'},
                   {'AttributeName': 'sk', 'KeyType': 'RANGE'}],
        BillingMode='PAY_PER_REQUEST',
        Tags=[{'Key': 'Project', 'Value': config.PROJECT_NAME}],
    )
    client.get_waiter('table_exists').wait(TableName=table_name)
    client.update_time_to_live(TableName=table_name,
                               TimeToLiveSpecification={'Enabled': True, 'AttributeName': 'ttl'})
    return table_name


class DynamoDbSessionManager(RepositorySessionManager, SessionRepository):
    """Strands session manager that persists sessions, agents and messages in DynamoDB."""

    def __init__(self, session_id: str, table_name: str = SESSIONS_TABLE,
                 region_name: Optional[str] = None, **kwargs: Any):
        self.table = boto3.resource('dynamodb', region_name=region_name or config.AWS_REGION) \
                          .Table(table_name)
        super().__init__(session_id=session_id, session_repository=self)

    # ── key helpers ──────────────────────────────────────────────────────
    @staticmethod
    def _pk(session_id: str) -> str:
        return f"SESSION#{session_id}"

    @staticmethod
    def _agent_sk(agent_id: str) -> str:
        return f"AGENT#{agent_id}"

    @staticmethod
    def _message_sk(agent_id: str, message_id: int) -> str:
        return f"AGENT#{agent_id}#MSG#{int(message_id):012d}"

    def _put(self, session_id: str, sk: str, data: dict, condition: Optional[str] = None) -> None:
        item = {'pk': self._pk(session_id), 'sk': sk,
                'data': json.dumps(data, ensure_ascii=False),
                'ttl': int(time.time()) + SESSION_TTL_DAYS * 86400}
        kwargs = {'ConditionExpression': condition} if condition else {}
        try:
            self.table.put_item(Item=item, **kwargs)
        except ClientError as e:
            if e.response['Error']['Code'] == 'ConditionalCheckFailedException':
                raise SessionException(f"{sk} already exists in session {session_id}") from e
            raise SessionException(f"DynamoDB error writing {sk}: {e}") from e

    def _get(self, session_id: str, sk: str) -> Optional[dict]:
        try:
            item = self.table.get_item(Key={'pk': self._pk(session_id), 'sk': sk}).get('Item')
        except ClientError as e:
            raise SessionException(f"DynamoDB error reading {sk}: {e}") from e
        return json.loads(item['data']) if item else None

    # ── SessionRepository: sessions ──────────────────────────────────────
    def create_session(self, session: Session, **kwargs: Any) -> Session:
        self._put(session.session_id, 'SESSION', session.to_dict(),
                  condition='attribute_not_exists(pk)')
        return session

    def read_session(self, session_id: str, **kwargs: Any) -> Optional[Session]:
        data = self._get(session_id, 'SESSION')
        return Session.from_dict(data) if data else None

    # ── SessionRepository: agents ────────────────────────────────────────
    def create_agent(self, session_id: str, session_agent: SessionAgent, **kwargs: Any) -> None:
        self._put(session_id, self._agent_sk(session_agent.agent_id), session_agent.to_dict())

    def read_agent(self, session_id: str, agent_id: str, **kwargs: Any) -> Optional[SessionAgent]:
        data = self._get(session_id, self._agent_sk(agent_id))
        return SessionAgent.from_dict(data) if data else None

    def update_agent(self, session_id: str, session_agent: SessionAgent, **kwargs: Any) -> None:
        previous = self.read_agent(session_id, session_agent.agent_id)
        if previous is None:
            raise SessionException(f"Agent {session_agent.agent_id} in session {session_id} does not exist")
        session_agent.created_at = previous.created_at
        self._put(session_id, self._agent_sk(session_agent.agent_id), session_agent.to_dict())

    # ── SessionRepository: messages ──────────────────────────────────────
    def create_message(self, session_id: str, agent_id: str, session_message: SessionMessage,
                       **kwargs: Any) -> None:
        self._put(session_id, self._message_sk(agent_id, session_message.message_id),
                  session_message.to_dict())

    def read_message(self, session_id: str, agent_id: str, message_id: int,
                     **kwargs: Any) -> Optional[SessionMessage]:
        data = self._get(session_id, self._message_sk(agent_id, message_id))
        return SessionMessage.from_dict(data) if data else None

    def update_message(self, session_id: str, agent_id: str, session_message: SessionMessage,
                       **kwargs: Any) -> None:
        previous = self.read_message(session_id, agent_id, session_message.message_id)
        if previous is None:
            raise SessionException(f"Message {session_message.message_id} does not exist")
        session_message.created_at = previous.created_at
        self._put(session_id, self._message_sk(agent_id, session_message.message_id),
                  session_message.to_dict())

    def list_messages(self, session_id: str, agent_id: str, limit: Optional[int] = None,
                      offset: int = 0, **kwargs: Any) -> list:
        """Messages of one agent in message_id order (sort key is zero-padded)."""
        condition = (Key('pk').eq(self._pk(session_id)) &
                     Key('sk').begins_with(f"{self._agent_sk(agent_id)}#MSG#"))
        items, start_key = [], None
        try:
            while True:
                kwargs_q = {'KeyConditionExpression': condition}
                if start_key:
                    kwargs_q['ExclusiveStartKey'] = start_key
                page = self.table.query(**kwargs_q)
                items.extend(page.get('Items', []))
                start_key = page.get('LastEvaluatedKey')
                if not start_key:
                    break
        except ClientError as e:
            raise SessionException(f"DynamoDB error listing messages: {e}") from e
        items = items[offset:offset + limit] if limit is not None else items[offset:]
        return [SessionMessage.from_dict(json.loads(i['data'])) for i in items]


def _show(session_id: str) -> None:
    table = boto3.resource('dynamodb', region_name=config.AWS_REGION).Table(SESSIONS_TABLE)
    items = table.query(KeyConditionExpression=Key('pk').eq(f"SESSION#{session_id}"))['Items']
    print(f"{len(items)} item(s) for session {session_id} in {SESSIONS_TABLE}")
    for item in items:
        data = json.loads(item['data'])
        text = ''
        if '#MSG#' in item['sk']:
            msg = data.get('message', {})
            parts = [c.get('text') or (f"[toolUse {c['toolUse']['name']}]" if 'toolUse' in c else
                                       '[toolResult]' if 'toolResult' in c else '')
                     for c in msg.get('content', [])]
            text = f"{msg.get('role', '?'):>9}: " + ' '.join(p for p in parts if p)[:110]
        print(f"  {item['sk']:<45} {text}")


if __name__ == '__main__':
    command = sys.argv[1] if len(sys.argv) > 1 else ''
    if command == 'create-table':
        print(f"Table ready: {ensure_sessions_table()} (on-demand, TTL {SESSION_TTL_DAYS} days)")
    elif command == 'show' and len(sys.argv) > 2:
        _show(sys.argv[2])
    elif command == 'delete-table':
        boto3.client('dynamodb', region_name=config.AWS_REGION).delete_table(TableName=SESSIONS_TABLE)
        print(f"Deleted {SESSIONS_TABLE}")
    else:
        print(__doc__)
