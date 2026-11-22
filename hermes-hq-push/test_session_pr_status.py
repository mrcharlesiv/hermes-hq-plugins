import hashlib
import importlib.util
import io
import json
import os
import subprocess
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('pr_status_test', Path(__file__).with_name('session_pr_status.py'))
status = importlib.util.module_from_spec(spec)
spec.loader.exec_module(status)

TOKEN = 'ghp_' + 'x' * 36
OWNER = status.Access(token=TOKEN)            # the signed-in owner's own token
PUBLIC = status.Access(public_repos={'owner/repo'})
NOBODY = status.Access()


@pytest.fixture(autouse=True)
def clear_cache():
    status._cache.clear()
    yield
    status._cache.clear()


@pytest.fixture(autouse=True)
def no_host_tools(monkeypatch):
    # The host's gh sign-in is never a credential for this endpoint.
    monkeypatch.setattr(subprocess, 'run', lambda *a, **kw: pytest.fail('ran a host tool'))
    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **kw: pytest.fail('ran a host tool'))


def pr(number=1, state='open', draft=False, merged_at=None, branch='feature', repo='owner/repo'):
    return {'number': number, 'state': state, 'draft': draft, 'merged_at': merged_at,
            'base': {'repo': {'full_name': repo}}, 'head': {'ref': branch, 'repo': {'full_name': repo}}}


def item(created, access=OWNER, sid='session-1'):
    return {'profile': 'default', 'id': sid, 'created': created, 'access': access}


def answer(data, etag=None):
    return lambda endpoint, access, tag=None: (data, etag)


class _Response(io.BytesIO):
    status = 200
    headers = {'ETag': '"v1"'}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


@pytest.mark.parametrize('data,expected', [
    (pr(), 'open'), (pr(draft=True), 'draft'), (pr(state='closed'), 'closed'),
    (pr(state='closed', merged_at='2026-01-01T00:00:00Z'), 'merged')])
def test_states_of_an_opened_pull_request(monkeypatch, data, expected):
    monkeypatch.setattr(status, '_api', answer(data))
    assert status._resolve_created([('owner/repo', 1)], OWNER) == {
        'state': expected, 'repo': 'owner/repo', 'number': 1, 'url': 'https://github.com/owner/repo/pull/1'}


def test_none_opened_is_none_and_unreadable_history_is_unknown(monkeypatch):
    monkeypatch.setattr(status, '_api', lambda *_: pytest.fail('asked GitHub'))
    assert status.resolve_batch([item([], sid='quiet'), item(None, sid='unreadable')]) == [
        {'profile': 'default', 'id': 'quiet', 'state': 'none'},
        {'profile': 'default', 'id': 'unreadable', 'state': 'unknown'}]


@pytest.mark.parametrize('created', [[('../x/y', 1)], [('owner/repo', 0)], [('owner/repo', True)], [('a/..', 1)]])
def test_malformed_identity_never_reaches_github(monkeypatch, created):
    monkeypatch.setattr(status, '_api', lambda *_: pytest.fail('asked GitHub'))
    assert status._resolve_created(created, OWNER) == {'state': 'unknown'}


def test_undeclared_repository_never_leaves_without_owner_token(monkeypatch):
    monkeypatch.setattr(status, '_api', lambda *_: pytest.fail('asked GitHub'))
    for access in (NOBODY, status.Access(public_repos={'someone/else'})):
        assert status._resolve_created([('owner/repo', 4)], access) == {
            'state': 'unknown', 'repo': 'owner/repo', 'number': 4, 'url': 'https://github.com/owner/repo/pull/4'}


def test_public_repo_is_asked_anonymously_by_number(monkeypatch):
    seen = []
    monkeypatch.setattr(status, '_api', lambda endpoint, access, tag=None: seen.append((endpoint, access.token)) or (pr(), None))
    assert status._resolve_created([('Owner/Repo', 1)], PUBLIC)['state'] == 'open'
    assert seen == [('/repos/Owner/Repo/pulls/1', None)]


def test_a_waiting_pull_request_outranks_a_newer_merged_one(monkeypatch):
    states = {1: pr(1, draft=True), 2: pr(2, state='closed', merged_at='2026-01-01T00:00:00Z'),
              3: pr(3, state='closed', merged_at='2026-01-02T00:00:00Z')}
    monkeypatch.setattr(status, '_api', lambda endpoint, access, tag=None: (states[int(endpoint.rsplit('/', 1)[1])], None))
    assert status._resolve_created([('owner/repo', 1), ('owner/repo', 2), ('owner/repo', 3)], OWNER)['number'] == 1
    assert status._resolve_created([('owner/repo', 2), ('owner/repo', 3)], OWNER) == {
        'state': 'merged', 'repo': 'owner/repo', 'number': 3, 'url': 'https://github.com/owner/repo/pull/3'}


def test_answer_must_be_the_pull_request_asked_about(monkeypatch):
    monkeypatch.setattr(status, '_api', answer(pr(number=2)))
    assert status._resolve_created([('owner/repo', 1)], OWNER)['state'] == 'unknown'
    status._cache.clear()
    monkeypatch.setattr(status, '_api', answer(pr(repo='owner/other')))
    assert status._resolve_created([('owner/repo', 1)], OWNER)['state'] == 'unknown'


def test_cache_is_per_access_and_merged_is_final(monkeypatch):
    now = [1.0]
    calls = []
    data = [pr()]
    monkeypatch.setattr(status.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(status, '_api', lambda endpoint, access, tag=None: calls.append(access.key) or (data[0], None))
    created = [('owner/repo', 1)]
    status._resolve_created(created, OWNER); status._resolve_created(created, OWNER)
    assert len(calls) == 1
    status._resolve_created(created, PUBLIC)   # an anonymous caller never reads a token's cached answer
    assert len(calls) == 2 and calls[1] == 'anonymous'
    now[0] += status.TTL + 1
    data[0] = pr(state='closed', merged_at='2026-01-01T00:00:00Z')
    assert status._resolve_created(created, OWNER)['state'] == 'merged'
    assert len(calls) == 3
    now[0] += 10 * 365 * 86400
    assert status._resolve_created(created, OWNER)['state'] == 'merged'
    assert len(calls) == 3                     # merged is never asked about again
    assert TOKEN not in repr(OWNER) and TOKEN not in OWNER.key
    with pytest.raises(ValueError):
        status.resolve_batch([item([])] * 33)


def test_merged_public_pull_requests_are_remembered_across_restarts(tmp_path, monkeypatch):
    access = status.Access(public_repos={'owner/repo'}, data_dir=str(tmp_path))
    monkeypatch.setattr(status, '_api', answer(pr(7, state='closed', merged_at='2026-01-01T00:00:00Z')))
    assert status._resolve_created([('owner/repo', 7)], access)['state'] == 'merged'
    assert json.loads((tmp_path / status.MERGED_NAME).read_text()) == {'owner/repo': [7]}
    status._cache.clear(); status._merged_loaded.clear()
    monkeypatch.setattr(status, '_api', lambda *_: pytest.fail('asked GitHub again'))
    assert status._resolve_created([('owner/repo', 7)], access) == {
        'state': 'merged', 'repo': 'owner/repo', 'number': 7, 'url': 'https://github.com/owner/repo/pull/7'}
    owner = status.Access(token=TOKEN, data_dir=str(tmp_path / 'private'))   # a token's answers are never written
    monkeypatch.setattr(status, '_api', answer(pr(8, state='closed', merged_at='2026-01-01T00:00:00Z')))
    status._resolve_created([('secret/repo', 8)], owner)
    assert not (tmp_path / 'private').exists()
    status._merged_loaded.clear()


def test_a_failed_answer_keeps_the_last_one_and_is_unknown_without_one(monkeypatch):
    now = [1.0]
    monkeypatch.setattr(status.time, 'monotonic', lambda: now[0])

    def fail(*_):
        raise OSError('offline')
    monkeypatch.setattr(status, '_api', fail)
    assert status._resolve_created([('owner/repo', 4)], OWNER) == {
        'state': 'unknown', 'repo': 'owner/repo', 'number': 4, 'url': 'https://github.com/owner/repo/pull/4'}
    now[0] += status.TTL_FAILED + 1
    monkeypatch.setattr(status, '_api', answer(pr(4, draft=True), '"v1"'))
    assert status._resolve_created([('owner/repo', 4)], OWNER)['state'] == 'draft'
    now[0] += status.TTL + 1
    monkeypatch.setattr(status, '_api', fail)
    assert status._resolve_created([('owner/repo', 4)], OWNER)['state'] == 'draft'   # rate limited: last answer stands
    now[0] += status.TTL_FAILED + 1
    tags = []

    def unchanged(endpoint, access, tag=None):
        tags.append(tag)
        raise status._Unchanged()
    monkeypatch.setattr(status, '_api', unchanged)
    assert status._resolve_created([('owner/repo', 4)], OWNER)['state'] == 'draft'
    assert tags == ['"v1"']


def test_requests_carry_no_host_identity(monkeypatch):
    seen = []

    def open_(request, timeout):
        seen.append((request.full_url, dict(request.header_items()), timeout))
        return _Response(json.dumps(pr()).encode())
    monkeypatch.setattr(status._opener, 'open', open_)
    monkeypatch.setenv('GH_TOKEN', 'host-token-must-not-be-used')
    monkeypatch.setenv('GITHUB_TOKEN', 'host-token-must-not-be-used')
    assert status._resolve_created([('owner/repo', 1)], PUBLIC)['state'] == 'open'
    url, headers, timeout = seen[0]
    assert url == 'https://api.github.com/repos/owner/repo/pulls/1'
    assert 'Authorization' not in headers and timeout == status._TIMEOUT
    assert 'host-token-must-not-be-used' not in json.dumps(headers)
    status._cache.clear()
    assert status._resolve_created([('owner/repo', 1)], OWNER)['state'] == 'open'
    assert seen[1][1]['Authorization'] == f'Bearer {TOKEN}'


def test_redirects_are_not_followed():
    handler = status._NoRedirect()
    assert handler.redirect_request(None, None, 301, 'Moved', {}, 'https://elsewhere.example/') is None
    with pytest.raises(ValueError):
        status._api('https://elsewhere.example/repos/a/b', NOBODY)


def test_owner_token_must_be_private_and_owned(tmp_path):
    subject = 'uid:501'
    path = status.owner_token_path(tmp_path, subject)
    assert path.name == hashlib.sha256(subject.encode()).hexdigest() + '.token'
    assert status.access_for(tmp_path, subject).token is None
    path.parent.mkdir()
    path.write_text(TOKEN + '\n')
    os.chmod(path, 0o644)
    assert status.access_for(tmp_path, subject).token is None      # readable by others
    os.chmod(path, 0o600)
    assert status.access_for(tmp_path, subject).token == TOKEN
    assert status.access_for(tmp_path, 'uid:502').token is None    # another owner's sessions
    link = status.owner_token_path(tmp_path, 'uid:503')
    link.symlink_to(path)
    assert status.access_for(tmp_path, 'uid:503').token is None    # never followed


def test_config_public_repos(tmp_path):
    assert status.load_config(tmp_path) == {'public_repos': frozenset(), 'repos': (), 'by_hand': ()}
    (tmp_path / status.CONFIG_NAME).write_text(json.dumps({'public_repos': ['Owner/Repo'], 'repos': [
        {'path': '/work/app/', 'published_marker': 'hq-public-published', 'public_repo': 'owner/repo'}, {'path': 'relative'}]}))
    by_hand = ({'path': '/work/app', 'main': 'main', 'published_marker': 'hq-public-published', 'receipts': None,
                'public_repo': 'owner/repo'},)
    assert status.load_config(tmp_path) == {'public_repos': frozenset({'owner/repo'}), 'repos': by_hand, 'by_hand': by_hand}
    (tmp_path / status.CONFIG_NAME).write_text(json.dumps({'public_repos': ['../x']}))
    assert status.load_config(tmp_path)['public_repos'] == frozenset()


def test_the_store_rules_come_from_the_running_hermes():
    """0.9.80: Hermes v0.23.3 moved the store's ownership rules (owner 2026-11-21: the update turned the status off).
    Whichever Hermes runs, the route gets all five, and the row rules answer for a plain local row."""
    prefix, lineage, legacy_row, binding_prefix, names = status.store_rules()
    assert isinstance(prefix, str) and isinstance(binding_prefix, str) and callable(lineage)
    assert legacy_row({'source': 'cli', 'user_id': None}) is True
    assert legacy_row({'source': 'telegram', 'user_id': '123'}) is False
    assert isinstance(names('{}', 'nobody'), bool)


def test_capability_only_on_supported_runtime(monkeypatch):
    monkeypatch.setattr(status, '_supported', None)
    real = status.importlib.import_module

    def missing(name, *args, **kwargs):
        if name == 'gateway.session_history_mount':
            raise ImportError(name)
        return real(name, *args, **kwargs)
    monkeypatch.setattr(status.importlib, 'import_module', missing)
    assert status.supported() is False
    monkeypatch.setattr(status, '_supported', None)
    monkeypatch.setattr(status.importlib, 'import_module', real)
    assert status.supported() is True       # the runtime these tests import has every API


def _api_module(tmp_path, monkeypatch, name):
    import test_tasks
    monkeypatch.setenv('HERMES_HQ_PUSH_DATA_DIR', str(tmp_path / 'data'))
    monkeypatch.setenv('HERMES_HQ_PUSH_RELAY_URL', 'off')
    monkeypatch.setattr(test_tasks.push, '_watcher_started', True)
    api_spec = importlib.util.spec_from_file_location(name, Path(__file__).parent / 'dashboard/plugin_api.py')
    api = importlib.util.module_from_spec(api_spec)
    api_spec.loader.exec_module(api)
    monkeypatch.setattr(api, '_loop_watched', True)
    return api


def test_status_and_route_follow_runtime_support(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    api = _api_module(tmp_path, monkeypatch, 'pr_status_capability_test')
    monkeypatch.setattr(api.session_pr_status, '_supported', False)
    assert api.status()['sessionPRStatus'] is False
    app = FastAPI()
    app.include_router(api.router)
    response = TestClient(app).post('/sessions/pr-status', json={'sessions': [{'profile': 'default', 'id': 's'}]})
    assert response.status_code == 501
    monkeypatch.setattr(api.session_pr_status, '_supported', True)
    assert api.status()['sessionPRStatus'] is True


def test_http_route_and_validation(tmp_path, monkeypatch):
    from fastapi import FastAPI, Depends, HTTPException, Header, Request
    from types import SimpleNamespace
    from gateway.session_contract import SessionActor
    from gateway.config import Platform
    from hermes_cli import web_server_sessions, profiles
    from fastapi.testclient import TestClient
    api = _api_module(tmp_path, monkeypatch, 'pr_status_api_test')
    batches = []

    def fake_resolve(items):
        batches.append(items)
        return [{'profile': i['profile'], 'id': i['id'], 'state': 'none'} for i in items]
    monkeypatch.setattr(api.session_pr_status, 'resolve_batch', fake_resolve)
    calls = []
    home = tmp_path / 'default'

    class Owner:
        db = SimpleNamespace(db_path=str(home / 'state.db'))
        sessions = {'s-1': SimpleNamespace(source=SimpleNamespace(platform=Platform.LOCAL, user_id='alice'))}

        def authorize(self, actor, ref, capability):
            calls.append((actor, ref, capability))
    owner = Owner()
    from gateway import session_authorities, session_history_mount
    from hermes_state import SessionDB
    home.mkdir()
    db = SessionDB(db_path=home / 'state.db')
    db.create_session('s-1', source='desktop')
    call = {'id': 'c1', 'type': 'function', 'function': {'name': 'write_file',
            'arguments': json.dumps({'path': '/work/app.py', 'content': 'x'})}}
    db.append_message('s-1', 'assistant', '', tool_calls=[call])
    db.close()
    monkeypatch.setattr(session_authorities, 'require_serving', lambda _: None)
    monkeypatch.setattr(session_history_mount, 'is_history_mount', lambda _: False)
    monkeypatch.setattr(profiles, 'get_profile_dir', lambda _: home)
    monkeypatch.setattr(web_server_sessions, '_session_mutation_context', lambda req, profile:
                        (owner, SessionActor(req.state.who, str(home), frozenset({'session:read', 'session:operator'}), 'http')))

    def auth(request: Request, authorization: str = Header(default='')):
        if authorization not in ('Bearer alice', 'Bearer bob'):
            raise HTTPException(401)
        request.state.who = authorization.split()[1]
    app = FastAPI()
    app.include_router(api.router, prefix='/api/plugins/hermes-hq-push', dependencies=[Depends(auth)])
    client = TestClient(app)
    url = '/api/plugins/hermes-hq-push/sessions/pr-status'
    payload = {'sessions': [{'profile': 'default', 'id': 's-1'}]}
    assert client.post(url, json=payload).status_code == 401
    headers = {'Authorization': 'Bearer alice'}
    response = client.post(url, json=payload, headers=headers)
    assert response.status_code == 200
    assert response.json() == {'sessions': [{'profile': 'default', 'id': 's-1', 'state': 'none'}]}
    assert batches[-1][0]['created'] == [] and batches[-1][0]['access'].token is None
    assert batches[-1][0]['fresh'] is False
    client.post(url, json={**payload, 'fresh': True}, headers=headers)
    assert batches[-1][0]['fresh'] is True           # a chat being opened (0.9.75)
    client.post(url, json={'sessions': payload['sessions'] * 5, 'fresh': True}, headers=headers)
    assert batches[-1][0]['fresh'] is False          # a whole list is never asked for fresh
    assert client.post(url, json={**payload, 'fresh': 'yes'}, headers=headers).status_code == 422
    owner.sessions['s-1'].source = SimpleNamespace(platform=Platform.TELEGRAM, user_id='alice')
    assert client.post(url, json=payload, headers=headers).json() == {'sessions': []}
    owner.sessions['s-1'].source = SimpleNamespace(platform=Platform.LOCAL, user_id='alice')
    assert client.post(url, json=payload, headers={'Authorization': 'Bearer bob'}).json() == {'sessions': []}
    assert calls == []  # no restore, mount, touch or operator authorization
    assert client.post(url, json={'sessions': [payload['sessions'][0], {'profile': 'default', 'id': 'other'}]}, headers=headers).json() == {
        'sessions': [{'profile': 'default', 'id': 's-1', 'state': 'none'}]}
    monkeypatch.setattr(web_server_sessions, '_session_mutation_context', lambda *_: (None, None))
    assert client.post(url, json=payload, headers=headers).json() == {'sessions': []}
    assert client.post(url, json={'sessions': payload['sessions'] * 33}, headers=headers).status_code == 422
    assert client.post(url, json={'sessions': [{'profile': '../bad', 'id': 's'}]}, headers=headers).status_code == 422
    assert client.post(url, json={'sessions': [{'profile': 'default', 'id': 1}]}, headers=headers).status_code == 422


def test_live_session_whose_stored_row_names_someone_else_is_not_read(tmp_path, monkeypatch):
    from fastapi import FastAPI, Request
    from types import SimpleNamespace
    from gateway.session_contract import SessionActor
    from gateway.config import Platform
    from gateway import session_authorities, session_history_mount
    from hermes_cli import web_server_sessions, profiles
    from hermes_state import SessionDB
    from fastapi.testclient import TestClient
    api = _api_module(tmp_path, monkeypatch, 'pr_status_live_row_test')
    monkeypatch.setattr(api.session_pr_status, 'created_in', lambda *_: pytest.fail('read history'))
    home = tmp_path / 'default'
    home.mkdir()
    db = SessionDB(db_path=home / 'state.db')
    db.create_session('s-1', source='desktop', user_id='uid:foreign')
    db.close()
    owner = SimpleNamespace(db=SimpleNamespace(db_path=str(home / 'state.db')),
                            sessions={'s-1': SimpleNamespace(source=SimpleNamespace(platform=Platform.LOCAL, user_id='alice'))})
    monkeypatch.setattr(session_authorities, 'require_serving', lambda _: None)
    monkeypatch.setattr(session_history_mount, 'is_history_mount', lambda _: False)
    monkeypatch.setattr(profiles, 'get_profile_dir', lambda _: home)
    monkeypatch.setattr(web_server_sessions, '_session_mutation_context', lambda req, profile:
                        (owner, SessionActor('alice', str(home), frozenset({'session:read'}), 'http')))
    app = FastAPI()
    app.include_router(api.router)
    assert TestClient(app).post('/sessions/pr-status', json={'sessions': [{'profile': 'default', 'id': 's-1'}]}).json() == {
        'sessions': []}


def test_installed_authority_rejects_other_user_without_operator(monkeypatch):
    from types import SimpleNamespace
    from gateway.config import Platform
    from gateway.session_authority import SessionAuthority
    from gateway.session_contract import SessionActor, SessionRef
    from gateway import session_authorities, session_retirement
    monkeypatch.setattr(session_authorities, 'require_serving', lambda _: None)
    monkeypatch.setattr(session_retirement, 'touch_restored', lambda *_: None)
    authority = SimpleNamespace(profile_id='p', sessions={
        's': SimpleNamespace(source=SimpleNamespace(platform=Platform.LOCAL, user_id='alice'))})
    ref = SessionRef('p', 's')
    SessionAuthority.authorize(authority, SessionActor('alice', 'p', frozenset({'session:read'}), 'http'), ref, 'session:read')
    with pytest.raises(Exception):
        SessionAuthority.authorize(authority, SessionActor('bob', 'p', frozenset({'session:read'}), 'http'), ref, 'session:read')
