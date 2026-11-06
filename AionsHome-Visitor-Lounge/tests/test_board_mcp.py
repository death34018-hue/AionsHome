from datetime import datetime, timedelta, timezone
import logging

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from visitor_lounge.board import BoardRepository
from visitor_lounge.container import Container
from visitor_lounge.mcp_app import create_mcp_server
from visitor_lounge.repository import VisitorRepository
from visitor_lounge.security import KeyService
from visitor_lounge.settings import Settings
from visitor_lounge.turn_coordinator import VisitorTurnCoordinator
from visitor_lounge.visitor_service import VisitorService


@pytest.fixture
def mcp_board(tmp_path, database):
    (tmp_path / 'config').mkdir()
    (tmp_path / 'config' / 'persona.md').write_text('Test host', encoding='utf-8')
    database.initialize()
    settings = Settings(
        root=tmp_path, database_path=database.path,
        visitor_host='127.0.0.1', visitor_port=8001,
        admin_host='127.0.0.1', admin_port=8002,
        max_generations=1, max_generations_hard_limit=2, max_waiting=3,
        queue_timeout_seconds=120, generation_timeout_seconds=120,
        key_pepper=b'test-pepper', master_key=Fernet.generate_key(),
        session_secret=b'test-secret', codex_workdir=tmp_path / 'workdir',
        chat_enabled=True, board_enabled=True,
    )
    now = datetime.now(timezone.utc)
    container = Container(settings=settings, database=database, codex_adapter=object(), clock=lambda: now)
    service = VisitorService(container)
    container.visitor_service = service
    _, app, _ = create_mcp_server(container, VisitorTurnCoordinator(service))
    visitors = VisitorRepository(database)
    visitor_id = visitors.create_unclaimed_visitor()
    visitors.claim_name(visitor_id, 'Test household', 'test')
    key = KeyService(visitors, settings).create(visitor_id).value
    with TestClient(app, base_url='http://localhost:8001') as client:
        def rpc(method, params):
            response = client.post('/mcp', headers={
                'Authorization': 'Bearer ' + key,
                'Accept': 'application/json, text/event-stream',
                'MCP-Protocol-Version': '2025-11-25',
            }, json={'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params})
            assert response.status_code == 200
            payload = response.json()
            assert 'error' not in payload
            return payload['result']
        yield rpc, visitors, visitor_id, database, now, key


def test_tools_publish_accurate_annotations_over_mcp(mcp_board):
    rpc, *_ = mcp_board
    tools = {tool['name']: tool for tool in rpc('tools/list', {})['tools']}
    # (read-only, destructive, idempotent): private board posts are additive;
    # ending a topic and binding a permanent identity have irreversible effects.
    expected = {
        'list_message_threads': (True, False, True),
        'read_message_thread': (True, False, True),
        'start_message_thread': (False, False, False),
        'reply_message_thread': (False, False, False),
        'close_message_thread': (False, True, True),
        'get_lounge_info': (True, False, True),
        'claim_identity': (False, True, False),
        'begin_visit': (False, False, False),
        'talk_to_host': (False, True, False),
        'get_visit_state': (False, False, False),
        'end_visit': (False, True, False),
    }
    assert tools.keys() == expected.keys()
    for name, (readonly, destructive, idempotent) in expected.items():
        annotations = tools[name].get('annotations')
        assert annotations is not None, name
        assert annotations['readOnlyHint'] is readonly, name
        assert annotations['destructiveHint'] is destructive, name
        assert annotations['idempotentHint'] is idempotent, name
        assert annotations['openWorldHint'] is False, name


def test_board_reads_do_not_resume_visitors_or_release_locks(mcp_board):
    rpc, visitors, visitor_id, database, now, _ = mcp_board
    thread = BoardRepository(database).create_thread(
        visitor_id, title='Test', author_name='Test AI', content='note', request_id='seed')
    visitors.end_visit(visitor_id, now)
    for name, args in [('list_message_threads', {}),
                       ('read_message_thread', {'thread_id': thread['id']})]:
        assert not rpc('tools/call', {'name': name, 'arguments': args}).get('isError')
        assert visitors.visitor(visitor_id).status == 'suspended'
    with database.transaction(immediate=True) as conn:
        conn.execute("UPDATE visitors SET status='safety_lock', safety_locked_until=? WHERE id=?",
                     ((now - timedelta(minutes=1)).isoformat(), visitor_id))
    info = rpc('tools/call', {'name': 'get_lounge_info', 'arguments': {}})
    assert not info.get('isError')
    assert info['structuredContent']['lounge_state'] == 'open'
    assert visitors.visitor(visitor_id).status == 'safety_lock'
    assert not rpc('tools/call', {'name': 'list_message_threads', 'arguments': {}}).get('isError')
    assert visitors.visitor(visitor_id).status == 'safety_lock'
    assert not rpc('tools/call', {'name': 'reply_message_thread', 'arguments': {
        'thread_id': thread['id'], 'author_name': 'Test AI', 'content': 'After expiry',
        'request_id': 'after-expiry'}}).get('isError')
    assert visitors.visitor(visitor_id).status == 'active'
    with database.transaction(immediate=True) as conn:
        conn.execute("UPDATE visitors SET status='safety_lock', safety_locked_until=? WHERE id=?",
                     ((now + timedelta(minutes=1)).isoformat(), visitor_id))
    assert rpc('tools/call', {'name': 'list_message_threads', 'arguments': {}})['isError']


def test_direct_board_writes_and_safe_diagnostics(mcp_board, caplog):
    rpc, visitors, visitor_id, database, now, key = mcp_board
    visitors.end_visit(visitor_id, now)
    caplog.set_level(logging.INFO, logger='visitor_lounge.mcp_tools')
    private_text = 'private-content-do-not-log'
    created = rpc('tools/call', {'name': 'start_message_thread', 'arguments': {
        'title': 'private-title-do-not-log', 'author_name': 'private-name-do-not-log',
        'content': private_text, 'request_id': 'test-start'}})
    assert not created.get('isError')
    thread_id = created['structuredContent']['id']
    reply = rpc('tools/call', {'name': 'reply_message_thread', 'arguments': {
        'thread_id': thread_id, 'author_name': 'Test AI', 'content': private_text,
        'request_id': 'test-reply'}})
    assert not reply.get('isError')
    rpc('tools/call', {'name': 'close_message_thread', 'arguments': {
        'thread_id': thread_id, 'author_name': 'Test AI'}})
    failed = rpc('tools/call', {'name': 'reply_message_thread', 'arguments': {
        'thread_id': thread_id, 'author_name': 'Test AI', 'content': private_text,
        'request_id': 'closed-reply'}})
    assert failed['isError']
    # An administrator pause still blocks board access.
    with database.transaction(immediate=True) as conn:
        conn.execute("UPDATE visitors SET status='paused' WHERE id=?", (visitor_id,))
    assert rpc('tools/call', {'name': 'list_message_threads', 'arguments': {}})['isError']
    # Exercise the async wrapper without invoking a model or publishing a report.
    rejected = rpc('tools/call', {'name': 'talk_to_host', 'arguments': {'message': private_text * 30}})
    assert rejected['structuredContent']['status'] == 'message_too_long'
    records = '\n'.join(r.getMessage() for r in caplog.records if r.name == 'visitor_lounge.mcp_tools')
    assert 'tool=start_message_thread outcome=success' in records
    assert 'tool=reply_message_thread outcome=error error_type=BoardThreadClosed' in records
    assert 'tool=talk_to_host outcome=rejected status=message_too_long' in records
    for secret in (key, private_text, 'private-title-do-not-log', 'private-name-do-not-log', thread_id):
        assert secret not in records
    with database.connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM board_posts').fetchone()[0] == 2
        assert conn.execute('SELECT COUNT(*) FROM messages').fetchone()[0] == 0
