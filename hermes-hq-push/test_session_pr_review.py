"""Pull request review facts on sessions/pr-status: size, checks, title, base, and with detail files and
check runs (CONTRACT.md of the review card). Real temporary git repositories; GitHub is a fake of _api."""
import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('pr_review_status_test', Path(__file__).with_name('session_pr_status.py'))
status = importlib.util.module_from_spec(spec)
spec.loader.exec_module(status)
local = status._local()

TOKEN = 'ghp_' + 'x' * 36
OWNER = status.Access(token=TOKEN)
PUBLIC = status.Access(public_repos={'example/app'})
HEAD = 'a' * 40
ENV = {**os.environ, 'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@example.com', 'GIT_COMMITTER_NAME': 't',
       'GIT_COMMITTER_EMAIL': 't@example.com', 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull}
NEW_KEYS = {'size', 'checks', 'title', 'base', 'files'}
ITEM_KEYS = {'state', 'repo', 'branch', 'number', 'url', 'source'} | NEW_KEYS
ANSWER_KEYS = {'profile', 'id', 'state', 'repo', 'branch', 'number', 'url', 'manual', 'items', 'size', 'checks',
               'title', 'base'}


def _caches():
    return (status._cache, status._reviews, local._repo_cache, local._answer_cache, local._facts, local._by_main,
            local._static, local._reviews)


@pytest.fixture(autouse=True)
def clear():
    for kept in _caches():
        kept.clear()
    yield
    for kept in _caches():
        kept.clear()


# ---- a fake GitHub -----------------------------------------------------------------------------------

def pr(number=24, state='open', branch='fix/reconnect-crash', repo='example/app', **more):
    merged = state == 'merged'
    data = {'number': number, 'state': 'open' if state in ('open', 'draft') else 'closed', 'draft': state == 'draft',
            'merged_at': '2026-01-01T00:00:00Z' if merged else None,
            'base': {'ref': 'main', 'repo': {'full_name': repo}},
            'head': {'ref': branch, 'sha': HEAD, 'repo': {'full_name': repo}},
            'title': 'Fix reconnect crash', 'changed_files': 4, 'additions': 128, 'deletions': 12}
    data.update(more)
    return data


def run(name, status_='completed', conclusion='success'):
    return {'name': name, 'status': status_, 'conclusion': conclusion}


def runs(*listed, total=None):
    return {'total_count': len(listed) if total is None else total, 'check_runs': list(listed)}


class GitHub:
    """Answers by endpoint; ``fail`` makes one kind of request raise; ``asked`` lists every endpoint."""

    def __init__(self, monkeypatch, prs, checks='one passing run', files=None):
        self.prs, self.files = prs, files or []
        self.checks = runs(run('unit')) if checks == 'one passing run' else checks
        self.asked, self.tags, self.fail = [], [], set()
        monkeypatch.setattr(status, '_api', self)

    def __call__(self, endpoint, access, etag=None):
        self.asked.append(endpoint)
        self.tags.append(etag)
        kind = 'checks' if '/check-runs' in endpoint else 'files' if endpoint.endswith('/files?per_page=50') else 'pr'
        if kind in self.fail:
            raise OSError('offline')
        if kind == 'checks':
            assert endpoint.endswith(f'/commits/{HEAD}/check-runs?per_page=100')
            return self.checks, '"c1"'
        if kind == 'files':
            return self.files, '"f1"'
        return self.prs[int(endpoint.rsplit('/', 1)[1])], '"p1"'

    def count(self, kind):
        return sum(1 for e in self.asked if ('check-runs' in e) == (kind == 'checks') and
                   e.endswith('/files?per_page=50') == (kind == 'files'))


def row(created=(), work=None, access=OWNER, repos=(), sid='s-release', **more):
    return {'profile': 'deploys', 'id': sid, 'created': None if created is None else list(created), 'work': work if work is not None else NO_WORK,
            'access': access, 'repos': repos, **more}


NO_WORK = {'branches': {}, 'commits': []}


def one(*args, **kwargs):
    return status.resolve_batch([row(*args, **kwargs)])[0]


def sent_keys_are_named(answer):
    assert set(answer) <= ANSWER_KEYS, set(answer) - ANSWER_KEYS
    for item in answer.get('items', ()):
        assert set(item) <= ITEM_KEYS, set(item) - ITEM_KEYS
        for file in item.get('files', ()):
            assert set(file) <= {'path', 'additions', 'deletions', 'binary', 'patch', 'patch_cut'}
        if 'checks' in item:
            assert set(item['checks']) <= {'state', 'passed', 'failed', 'running', 'runs'}
    json.dumps(answer)          # everything sent is JSON


# ---- GitHub pull requests ---------------------------------------------------------------------------

def test_a_waiting_pull_request_carries_its_facts_and_the_lead_the_same(monkeypatch):
    gh = GitHub(monkeypatch, {24: pr()})
    answer = one([('example/app', 24)])
    item = answer['items'][0]
    assert item == {'state': 'open', 'repo': 'example/app', 'number': 24, 'url': 'https://github.com/example/app/pull/24',
                    'source': 'github', 'size': {'files': 4, 'additions': 128, 'deletions': 12},
                    'checks': {'state': 'passed', 'passed': 1, 'failed': 0, 'running': 0},
                    'title': 'Fix reconnect crash', 'base': 'main'}
    assert {k: answer[k] for k in ('size', 'checks', 'title', 'base')} == {k: item[k] for k in ('size', 'checks', 'title', 'base')}
    assert 'files' not in answer and 'files' not in item and 'runs' not in answer['checks']
    assert gh.count('pr') == 1 and gh.count('checks') == 1 and gh.count('files') == 0
    sent_keys_are_named(answer)


def test_draft_is_waiting_too(monkeypatch):
    GitHub(monkeypatch, {24: pr(state='draft')})
    answer = one([('example/app', 24)])
    assert answer['state'] == 'draft' and answer['size']['files'] == 4 and answer['checks']['state'] == 'passed'


@pytest.mark.parametrize('state', ['merged', 'closed'])
def test_merged_or_closed_has_no_size_or_checks_and_no_check_request(monkeypatch, state):
    gh = GitHub(monkeypatch, {24: pr(state=state)})
    answer = one([('example/app', 24)], detail=True)
    item = answer['items'][0]
    assert item['state'] == state and item['title'] == 'Fix reconnect crash' and item['base'] == 'main'
    assert not {'size', 'checks', 'files'} & set(item) and not {'size', 'checks'} & set(answer)
    assert answer['title'] == 'Fix reconnect crash'
    assert gh.count('checks') == 0 and gh.count('files') == 0 and gh.count('pr') == 1


@pytest.mark.parametrize('listed,expected', [
    ([run('a'), run('b', conclusion='neutral'), run('c', conclusion='skipped')], ('passed', 3, 0, 0)),
    ([run('a'), run('b', conclusion='failure')], ('failed', 1, 1, 0)),
    ([run('a', conclusion='cancelled')], ('failed', 0, 1, 0)),
    ([run('a', conclusion='timed_out')], ('failed', 0, 1, 0)),
    ([run('a', conclusion='action_required')], ('failed', 0, 1, 0)),
    ([run('a', conclusion=None)], ('failed', 0, 1, 0)),                   # completed, no success: failed
    ([run('a'), run('b', 'in_progress', None)], ('running', 1, 0, 1)),
    ([run('a', 'queued', None), run('b', 'waiting', None)], ('running', 0, 0, 2)),
    ([run('a', 'in_progress', None), run('b', conclusion='failure')], ('failed', 0, 1, 1)),   # failed wins
])
def test_each_checks_state(monkeypatch, listed, expected):
    GitHub(monkeypatch, {24: pr()}, runs(*listed))
    checks = one([('example/app', 24)])['checks']
    assert (checks['state'], checks['passed'], checks['failed'], checks['running']) == expected
    assert checks['passed'] + checks['failed'] + checks['running'] == len(listed)


@pytest.mark.parametrize('answer_', [
    runs(),                                                  # no check run: nothing to say
    runs(run('a'), total=101),                               # more than one page: not all known
    runs(run('a'), {'name': 'b', 'status': 7}),              # malformed run
    runs({'name': 'b'}),                                     # no status
    {'check_runs': 'x', 'total_count': 1}, [], None, {'total_count': '1', 'check_runs': []},
])
def test_no_checks_unless_every_run_is_known(monkeypatch, answer_):
    GitHub(monkeypatch, {24: pr()}, answer_)
    answer = one([('example/app', 24)])
    assert 'checks' not in answer and 'checks' not in answer['items'][0]
    assert answer['size']['files'] == 4                       # the rest of the facts still stand


def test_runs_with_detail_failed_first_capped_and_named(monkeypatch):
    listed = ([run(f'pass-{i}') for i in range(20)] + [run(f'run-{i}', 'in_progress', None) for i in range(10)]
              + [run('x' * 300, conclusion='failure'), run(None, conclusion='failure'), run('  ', conclusion='failure')])
    GitHub(monkeypatch, {24: pr()}, runs(*listed))
    answer = one([('example/app', 24)], detail=True)
    checks = answer['items'][0]['checks']
    assert (checks['state'], checks['passed'], checks['failed'], checks['running']) == ('failed', 20, 3, 10)
    assert len(checks['runs']) == 30
    assert checks['runs'][0] == {'name': 'x' * 120, 'state': 'failed'}
    assert [r['state'] for r in checks['runs']] == ['failed'] + ['running'] * 10 + ['passed'] * 19
    assert 'runs' not in answer['checks']                    # never on the top-level answer


@pytest.mark.parametrize('broken,gone', [
    ({'changed_files': '4'}, 'size'), ({'additions': -1}, 'size'), ({'deletions': True}, 'size'),
    ({'changed_files': 2 ** 40}, 'size'), ({'title': 7}, 'title'), ({'title': '   '}, 'title'),
])
def test_a_malformed_field_drops_only_its_key(monkeypatch, broken, gone):
    GitHub(monkeypatch, {24: pr(**broken)})
    answer = one([('example/app', 24)])
    assert gone not in answer and gone not in answer['items'][0]
    assert answer['state'] == 'open' and answer['number'] == 24 and 'base' in answer and 'checks' in answer


def test_a_malformed_base_or_head_drops_base_or_checks(monkeypatch):
    gh = GitHub(monkeypatch, {24: pr(base={'ref': 'bad ref\n', 'repo': {'full_name': 'example/app'}},
                                     head={'ref': 'fix/reconnect-crash', 'sha': 'not-a-sha', 'repo': {'full_name': 'example/app'}})})
    answer = one([('example/app', 24)])
    assert 'base' not in answer and 'checks' not in answer and answer['size']['files'] == 4
    assert gh.count('checks') == 0                           # no head commit: never asked


def test_title_is_trimmed_and_capped(monkeypatch):
    GitHub(monkeypatch, {24: pr(title='  ' + 'T' * 250 + '  ')})
    assert one([('example/app', 24)])['title'] == 'T' * 200


def test_check_runs_follow_the_lookups_rules(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(status.time, 'monotonic', lambda: now[0])
    gh = GitHub(monkeypatch, {24: pr()})
    first = one([('example/app', 24)])
    assert first['checks']['state'] == 'passed'
    one([('example/app', 24)])
    assert gh.count('checks') == 1                           # cached
    now[0] += status.TTL + 1
    gh.fail.add('checks')
    again = one([('example/app', 24)])
    assert again['checks'] == first['checks']                # failed: the last answer stands
    assert gh.count('checks') == 2 and gh.tags[-1] == '"c1"'  # asked with its ETag
    one([('example/app', 24)], fresh=True)
    assert gh.count('checks') == 2                           # not asked again within TTL_FAILED, fresh or not
    now[0] += status.TTL_FAILED + 1
    gh.fail.clear()

    def unchanged(endpoint, access, etag=None):
        gh.asked.append(endpoint)
        gh.tags.append(etag)
        if 'check-runs' in endpoint:
            raise status._Unchanged()
        return pr(), None
    monkeypatch.setattr(status, '_api', unchanged)
    assert one([('example/app', 24)])['checks'] == first['checks']
    assert gh.tags[-1] == '"c1"'


def test_a_failed_check_request_with_no_earlier_answer_says_nothing(monkeypatch):
    gh = GitHub(monkeypatch, {24: pr()})
    gh.fail.add('checks')
    answer = one([('example/app', 24)])
    assert 'checks' not in answer and answer['size']['files'] == 4


def test_check_runs_need_the_owners_token_or_a_public_repository(monkeypatch):
    gh = GitHub(monkeypatch, {24: pr()})
    assert one([('example/app', 24)], access=PUBLIC)['checks']['state'] == 'passed'
    assert gh.count('checks') == 1
    status._cache.clear(); status._reviews.clear()
    gh.asked.clear()
    answer = one([('example/app', 24)], access=status.Access())
    assert answer['state'] == 'unknown' and gh.asked == []
    # A cached lookup answer handed to a caller without access never makes a check request.
    assert status._checks('example/app', HEAD, status.Access()) is None
    assert status._pr_files('example/app', 24, HEAD, status.Access()) is None and gh.asked == []


def test_the_endpoints_pass_the_api_check(monkeypatch):
    seen = []

    class Response:
        status = 200
        headers = {'ETag': '"v"'}

        def __init__(self, body):
            self.body = body

        def read(self, limit):
            return self.body

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    def open_(request, timeout):
        seen.append(request.full_url)
        if 'check-runs' in request.full_url:
            return Response(json.dumps(runs(run('unit'))).encode())
        if request.full_url.endswith('/files?per_page=50'):
            return Response(json.dumps([{'filename': 'a.txt', 'additions': 1, 'deletions': 0, 'patch': '@@ -0,0 +1 @@\n+a'}]).encode())
        return Response(json.dumps(pr()).encode())
    monkeypatch.setattr(status._opener, 'open', open_)
    answer = one([('example/app', 24)], detail=True)
    assert seen == ['https://api.github.com/repos/example/app/pulls/24',
                    f'https://api.github.com/repos/example/app/commits/{HEAD}/check-runs?per_page=100',
                    'https://api.github.com/repos/example/app/pulls/24/files?per_page=50']
    assert answer['items'][0]['files'] == [{'path': 'a.txt', 'additions': 1, 'deletions': 0, 'patch': '@@ -0,0 +1 @@\n+a'}]


def test_github_files_with_detail(monkeypatch):
    files = [{'filename': 'App/Net/Reconnect.swift', 'additions': 96, 'deletions': 8, 'patch': '@@ -10,3 +10,4 @@\n context\n-old\n+new\n+more'},
             {'filename': 'big.json', 'additions': 9000, 'deletions': 0},                       # GitHub left it out
             {'filename': 'logo.png', 'additions': 0, 'deletions': 0},                          # can't tell: nothing
             {'filename': 'odd.txt', 'additions': 1, 'deletions': 0, 'patch': 'diff --git x'},    # malformed patch
             {'filename': 'long.txt', 'additions': 1, 'deletions': 0, 'patch': '@@ ' + 'x' * 20_000}]
    files += [{'filename': f'f{i}.txt', 'additions': 1, 'deletions': 0, 'patch': '@@ -0,0 +1 @@\n+x'} for i in range(60)]
    gh = GitHub(monkeypatch, {24: pr()}, files=files)
    answer = one([('example/app', 24)], detail=True)
    listed = answer['items'][0]['files']
    assert len(listed) == 50 and 'files' not in answer
    assert listed[0] == {'path': 'App/Net/Reconnect.swift', 'additions': 96, 'deletions': 8,
                         'patch': '@@ -10,3 +10,4 @@\n context\n-old\n+new\n+more'}
    assert listed[1] == {'path': 'big.json', 'additions': 9000, 'deletions': 0, 'patch_cut': True}
    assert listed[2] == {'path': 'logo.png', 'additions': 0, 'deletions': 0}
    assert listed[3] == {'path': 'odd.txt', 'additions': 1, 'deletions': 0}
    assert listed[4] == {'path': 'long.txt', 'additions': 1, 'deletions': 0, 'patch_cut': True}
    assert gh.count('files') == 1
    one([('example/app', 24)], detail=True)
    assert gh.count('files') == 1 and gh.count('checks') == 1 and gh.count('pr') == 1   # cached
    sent_keys_are_named(answer)


@pytest.mark.parametrize('files', [{'x': 1}, [{'filename': 3, 'additions': 1, 'deletions': 0}],
                                   [{'filename': 'a', 'additions': -1, 'deletions': 0}], ['a']])
def test_malformed_github_files_drop_the_files_key(monkeypatch, files):
    GitHub(monkeypatch, {24: pr()}, files=files)
    item = one([('example/app', 24)], detail=True)['items'][0]
    assert 'files' not in item and item['size']['files'] == 4


def test_detail_is_ignored_above_four_sessions(monkeypatch):
    gh = GitHub(monkeypatch, {24: pr()}, runs(run('unit')), files=[{'filename': 'a', 'additions': 1, 'deletions': 0}])
    rows = [row([('example/app', 24)], sid=f's-{i}', detail=True) for i in range(5)]
    for answer in status.resolve_batch(rows):
        assert 'files' not in answer['items'][0] and 'runs' not in answer['items'][0]['checks']
    assert gh.count('files') == 0
    four = status.resolve_batch(rows[:4])
    assert all('files' in a['items'][0] and 'runs' in a['items'][0]['checks'] for a in four)


def test_a_mark_keeps_title_and_base_but_not_size_or_checks_on_the_answer(monkeypatch):
    GitHub(monkeypatch, {24: pr()})
    answer = one([('example/app', 24)], closed=True)
    assert answer['state'] == 'closed' and answer['manual'] and not {'size', 'checks'} & set(answer)
    assert answer['title'] == 'Fix reconnect crash' and answer['base'] == 'main'
    assert answer['items'][0]['size']['files'] == 4 and answer['items'][0]['checks']['state'] == 'passed'


def test_merged_answers_are_still_remembered_as_numbers_only(tmp_path, monkeypatch):
    access = status.Access(public_repos={'example/app'}, data_dir=str(tmp_path))
    GitHub(monkeypatch, {7: pr(7, state='merged')})
    assert one([('example/app', 7)], access=access)['title'] == 'Fix reconnect crash'
    assert json.loads((tmp_path / status.MERGED_NAME).read_text()) == {'example/app': [7]}


# ---- answers without new facts are exactly what they were --------------------------------------------

def _orig():
    """The installed add-on's module before review facts, when this copy sits beside it (scratch review)."""
    base = Path(__file__).resolve().parent.parent / 'orig'
    if not (base / 'session_pr_status.py').is_file():
        pytest.skip('no orig copy beside this one')
    loaded = {}
    for name, file in (('status', 'session_pr_status.py'), ('local', 'session_pr_local.py')):
        spec_ = importlib.util.spec_from_file_location(f'orig_review_{name}', base / file)
        loaded[name] = importlib.util.module_from_spec(spec_)
        spec_.loader.exec_module(loaded[name])
    loaded['status']._local = lambda: loaded['local']
    return loaded['status'], loaded['local']


def _strip(answer):
    out = {k: v for k, v in answer.items() if k not in NEW_KEYS}
    if 'items' in out:
        out['items'] = [{k: v for k, v in item.items() if k not in NEW_KEYS} for item in out['items']]
    return out


def test_old_shaped_answers_are_byte_for_byte_and_every_old_key_keeps_its_value(monkeypatch, tmp_path):
    orig, orig_local = _orig()
    plain = {1: {k: v for k, v in pr(1).items() if k not in ('title', 'changed_files', 'additions', 'deletions')},
             2: pr(2, state='merged'), 3: pr(3, state='closed'), 24: pr()}
    plain[1]['base'] = {'repo': {'full_name': 'example/app'}}
    plain[1]['head'] = {'ref': 'b', 'repo': {'full_name': 'example/app'}}
    orig._cache.clear()
    monkeypatch.setattr(orig, '_api', lambda endpoint, access, etag=None: (plain[int(endpoint.rsplit('/', 1)[1])], None))
    monkeypatch.setattr(status, '_api', lambda endpoint, access, etag=None: (
        (runs(run('unit')), None) if 'check-runs' in endpoint else (plain[int(endpoint.rsplit('/', 1)[1])], None)))
    rows = [row([('example/app', 1)], sid='old-shape'), row([], sid='none'), row(None, sid='unknown'),
            row([('example/app', 2), ('example/app', 3)], sid='finished-no-title-in-orig'),
            row([('example/app', 24)], sid='rich'), row([('example/app', 1)], sid='marked', closed=True)]
    new, old = status.resolve_batch(rows), orig.resolve_batch(rows)
    assert json.dumps(new[:3]) == json.dumps(old[:3])              # no new fact: byte for byte
    assert json.dumps(new[5]) == json.dumps(old[5])
    for a, b in zip(new, old):
        assert _strip(a) == b and json.dumps(_strip(a)) == json.dumps(b)   # every old key, its old value and order
    assert new[4]['size']['files'] == 4 and 'size' not in old[4]


# ---- local branches (git) ----------------------------------------------------------------------------

def git(repo, *args, data=None):
    done = subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True, env=ENV,
                          input=data)
    return done.stdout.decode('utf-8', 'replace').strip()


def commit(repo, name, text='x\n', message=None):
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text if isinstance(text, bytes) else text.encode())
    git(repo, 'add', '-A')
    git(repo, 'commit', '-q', '-m', message or f'add {name}')
    return git(repo, 'rev-parse', 'HEAD')


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / 'app'
    path.mkdir()
    git(path, 'init', '-q', '-b', 'main')
    commit(path, 'README', 'hello\n')
    commit(path, 'App/Net/Reconnect.swift', ''.join(f'line {i}\n' for i in range(20)))
    return path


def listed(repo):
    return local.load_repos([{'path': str(repo)}])


def branch(name):
    return {'branches': {name: []}, 'commits': []}


def count_git(monkeypatch):
    asked = []
    real = local._git

    def counted(path, *args, **kwargs):
        asked.append(next((a for a in args if a in ('diff', 'merge-base', 'for-each-ref', 'log', 'cherry', 'reflog')), args[0]))
        return real(path, *args, **kwargs)
    monkeypatch.setattr(local, '_git', counted)
    return asked


def test_a_local_branch_has_gits_size_and_main_as_its_base(repo, monkeypatch):
    monkeypatch.setattr(status, '_api', lambda *_: pytest.fail('asked GitHub'))
    git(repo, 'switch', '-q', '-c', 'fix/reconnect-crash')
    commit(repo, 'App/Net/Reconnect.swift', ''.join(f'line {i}\n' for i in range(20)).replace('line 3\n', 'changed\nmore\n'))
    commit(repo, 'App/New.swift', 'a\nb\nc\n')
    git(repo, 'switch', '-q', 'main')
    commit(repo, 'later-on-main', 'm\n')                   # main moved on: the merge-base, not main, is compared
    answer = one(work=branch('fix/reconnect-crash'), repos=listed(repo))
    assert answer == {'profile': 'deploys', 'id': 's-release', 'state': 'draft', 'branch': 'fix/reconnect-crash',
                      'size': {'files': 2, 'additions': 5, 'deletions': 1}, 'base': 'main',
                      'items': [{'state': 'draft', 'branch': 'fix/reconnect-crash', 'source': 'git',
                                 'size': {'files': 2, 'additions': 5, 'deletions': 1}, 'base': 'main'}]}


def test_local_files_with_detail_binary_and_renamed(repo, monkeypatch):
    monkeypatch.setattr(status, '_api', lambda *_: pytest.fail('asked GitHub'))
    commit(repo, 'docs/guide.md', ''.join(f'guide line {i}\n' for i in range(30)))
    git(repo, 'switch', '-q', '-c', 'fix/reconnect-crash')
    commit(repo, 'App/Net/Reconnect.swift', ''.join(f'line {i}\n' for i in range(20)).replace('line 3\n', 'new\n'))
    commit(repo, 'logo.png', b'\x89PNG\x00\x01\x02' * 50)
    git(repo, 'mv', 'README', 'README.md')
    git(repo, 'mv', 'docs/guide.md', 'docs/handbook.md')
    (repo / 'docs/handbook.md').write_text(''.join(f'guide line {i}\n' for i in range(30)).replace('guide line 9\n', 'nine\n'))
    git(repo, 'add', '-A')
    git(repo, 'commit', '-q', '-m', 'renames')
    git(repo, 'switch', '-q', 'main')
    answer = one(work=branch('fix/reconnect-crash'), repos=listed(repo), detail=True)
    item = answer['items'][0]
    files = {f['path']: f for f in item['files']}
    assert [f['path'] for f in item['files']] == ['App/Net/Reconnect.swift', 'README.md', 'docs/handbook.md', 'logo.png']
    assert files['App/Net/Reconnect.swift'] == {'path': 'App/Net/Reconnect.swift', 'additions': 1, 'deletions': 1,
                                                'patch': '@@ -1,7 +1,7 @@\n line 0\n line 1\n line 2\n-line 3\n+new\n line 4\n line 5\n line 6'}
    assert files['README.md'] == {'path': 'README.md', 'additions': 0, 'deletions': 0}            # a pure rename: no patch
    assert files['docs/handbook.md']['patch'].startswith('@@ ') and files['docs/handbook.md']['additions'] == 1
    assert files['logo.png'] == {'path': 'logo.png', 'additions': 0, 'deletions': 0, 'binary': True}
    assert item['size'] == {'files': 4, 'additions': 2, 'deletions': 2}
    assert answer['size'] == item['size'] and 'files' not in answer
    sent_keys_are_named(answer)


def _plumb(repo, name, files):
    """A branch *name* off main adding *files* (bytes path -> bytes content), names git itself accepts but a
    file system may not."""
    index = repo.parent / 'plumb-index'
    env = {**ENV, 'GIT_INDEX_FILE': str(index)}
    subprocess.run(['git', '-C', str(repo), 'read-tree', 'main'], check=True, env=env)
    lines = b''
    for path, content in files.items():
        blob = subprocess.run(['git', '-C', str(repo), 'hash-object', '-w', '--stdin'], check=True, env=env,
                              input=content, capture_output=True).stdout.strip()
        lines += b'100644 blob ' + blob + b'\t' + path + b'\0'
    subprocess.run(['git', '-C', str(repo), 'update-index', '-z', '--index-info'], check=True, env=env, input=lines)
    tree = subprocess.run(['git', '-C', str(repo), 'write-tree'], check=True, env=env, capture_output=True).stdout.strip()
    made = subprocess.run(['git', '-C', str(repo), 'commit-tree', tree, '-p', 'main', '-m', 'hostile'], check=True,
                          env=env, capture_output=True).stdout.strip()
    subprocess.run(['git', '-C', str(repo), 'update-ref', f'refs/heads/{name}', made], check=True, env=env)
    index.unlink()


def test_hostile_file_names_do_not_break_parsing(repo, monkeypatch):
    monkeypatch.setattr(status, '_api', lambda *_: pytest.fail('asked GitHub'))
    _plumb(repo, 'fix/hostile', {b'new\nline "quoted".txt': b'one\ntwo\n', b'caf\xe9-latin1.txt': b'\xff\xfe bytes\n',
                                 b'-dash \\ tab\t.txt': b'x\n', b':(glob)*': b'star\n', b'zz-plain.txt': b'plain\n'})
    answer = one(work=branch('fix/hostile'), repos=listed(repo), detail=True)
    files = answer['items'][0]['files']
    assert answer['items'][0]['size'] == {'files': 5, 'additions': 6, 'deletions': 0}
    by_path = {f['path']: f for f in files}
    assert set(by_path) == {'new\nline "quoted".txt', 'caf�-latin1.txt', '-dash \\ tab\t.txt', ':(glob)*', 'zz-plain.txt'}
    assert by_path['new\nline "quoted".txt']['patch'] == '@@ -0,0 +1,2 @@\n+one\n+two'
    assert by_path['caf�-latin1.txt']['patch'] == '@@ -0,0 +1 @@\n+�� bytes'
    assert by_path[':(glob)*']['patch'] == '@@ -0,0 +1 @@\n+star'          # a pathspec's magic is only a name
    assert by_path['zz-plain.txt']['patch'] == '@@ -0,0 +1 @@\n+plain'
    assert by_path['-dash \\ tab\t.txt']['patch'] == '@@ -0,0 +1 @@\n+x'
    json.dumps(answer)


def test_the_caps_fifty_files_and_patch_sizes(repo, monkeypatch):
    monkeypatch.setattr(status, '_api', lambda *_: pytest.fail('asked GitHub'))
    git(repo, 'switch', '-q', '-c', 'feat/many')
    for i in range(60):
        (repo / f'many/f{i:02d}.txt').parent.mkdir(exist_ok=True)
        (repo / f'many/f{i:02d}.txt').write_text(f'{i}\n')
    git(repo, 'add', '-A'); git(repo, 'commit', '-q', '-m', 'many')
    git(repo, 'switch', '-q', '-c', 'feat/big', 'main')
    (repo / 'wide.txt').write_text(''.join('w' * 99 + '\n' for _ in range(300)))         # 30,000 characters, 300 lines
    (repo / 'tall.txt').write_text(''.join(f'{i}\n' for i in range(12_000)))           # cut without asking git
    for i in range(14):                                                                 # 14 x ~18,000: over 200,000
        (repo / f'part{i:02d}.txt').write_text(''.join(f'{i:02d}' + 'p' * 86 + '\n' for _ in range(200)))
    git(repo, 'add', '-A'); git(repo, 'commit', '-q', '-m', 'big')
    git(repo, 'switch', '-q', 'main')
    many = one(work=branch('feat/many'), repos=listed(repo), detail=True)['items'][0]
    assert many['size']['files'] == 60 and len(many['files']) == 50
    assert [f['path'] for f in many['files']] == [f'many/f{i:02d}.txt' for i in range(50)]
    big = one(work=branch('feat/big'), repos=listed(repo), detail=True)['items'][0]
    files = {f['path']: f for f in big['files']}
    assert files['wide.txt'] == {'path': 'wide.txt', 'additions': 300, 'deletions': 0, 'patch_cut': True}
    assert files['tall.txt'] == {'path': 'tall.txt', 'additions': 12_000, 'deletions': 0, 'patch_cut': True}
    patches = [f['patch'] for f in big['files'] if 'patch' in f]
    assert all(len(p) <= 20_000 for p in patches) and sum(map(len, patches)) <= 200_000
    assert len(patches) == 11 and sum(1 for f in big['files'] if f.get('patch_cut')) == 5
    assert [f['path'] for f in big['files'] if 'patch' in f] == [f'part{i:02d}.txt' for i in range(11)]


def test_the_patch_budget_is_per_answer_across_its_items(repo, monkeypatch):
    monkeypatch.setattr(status, '_api', lambda *_: pytest.fail('asked GitHub'))
    for name in ('feat/one', 'feat/two'):
        git(repo, 'switch', '-q', '-c', name, 'main')
        for i in range(7):                                                              # 7 x ~18,000 each
            (repo / f'{name[5:]}{i}.txt').write_text(''.join(f'{i}' + 'q' * 87 + '\n' for _ in range(200)))
        git(repo, 'add', '-A'); git(repo, 'commit', '-q', '-m', name)
    git(repo, 'switch', '-q', 'main')
    work = {'branches': {'feat/one': [], 'feat/two': []}, 'commits': []}
    answer = one(work=work, repos=listed(repo), detail=True)
    first, second = answer['items']                         # newest first: feat/two, then feat/one
    assert first['branch'] == 'feat/two' and all('patch' in f for f in first['files'])
    assert sum(len(f.get('patch', '')) for item in answer['items'] for f in item['files']) <= 200_000
    assert any(f.get('patch_cut') for f in second['files'])
    alone = one(work=branch('feat/one'), repos=listed(repo), detail=True)['items'][0]
    assert all('patch' in f for f in alone['files'])          # its own answer has room for all of them


def test_a_second_resolve_with_nothing_moved_asks_git_and_github_nothing(repo, monkeypatch):
    git(repo, 'switch', '-q', '-c', 'fix/reconnect-crash')
    commit(repo, 'App/Fix.swift', 'fix\n')
    git(repo, 'switch', '-q', '-c', 'feat/pr-only', 'main')
    git(repo, 'switch', '-q', 'main')
    gh = GitHub(monkeypatch, {24: pr(branch='elsewhere'), 25: pr(25, branch='fix/reconnect-crash')},
                files=[{'filename': 'a', 'additions': 1, 'deletions': 0}])
    asked = count_git(monkeypatch)
    rows = [row([('example/app', 24), ('example/app', 25)], work=branch('fix/reconnect-crash'), repos=listed(repo), detail=True)]
    first = status.resolve_batch(rows)
    assert asked.count('diff') == 2 and asked.count('merge-base') == 1      # numstat and the patches, once
    assert len(gh.asked) == 4                                                 # two lookups, a check request each...
    asked.clear(); gh.asked.clear()
    assert status.resolve_batch(rows) == first
    assert asked == [] and gh.asked == []                                     # nothing moved: nothing asked
    local._repo_cache.clear()                                                 # the refs are read again
    assert status.resolve_batch(rows) == first
    assert asked == ['for-each-ref'] and gh.asked == []
    asked.clear()
    git(repo, 'switch', '-q', 'fix/reconnect-crash')
    commit(repo, 'App/Fix.swift', 'fix\nagain\n')                             # the tip moved: asked again
    git(repo, 'switch', '-q', 'main')
    local._repo_cache.clear()
    moved = status.resolve_batch(rows)[0]
    assert asked.count('merge-base') == 1 and asked.count('diff') == 2
    joined = next(i for i in moved['items'] if i['number'] == 25)
    assert joined['files'][0]['patch'] == '@@ -0,0 +1,2 @@\n+fix\n+again'


def test_a_joined_item_has_githubs_size_and_checks_and_local_files(repo, monkeypatch):
    git(repo, 'switch', '-q', '-c', 'fix/reconnect-crash')
    commit(repo, 'App/Fix.swift', 'fix\n')
    git(repo, 'switch', '-q', 'main')
    gh = GitHub(monkeypatch, {24: pr()}, files=[{'filename': 'never', 'additions': 1, 'deletions': 0}])
    answer = one([('example/app', 24)], work=branch('fix/reconnect-crash'), repos=listed(repo), detail=True)
    assert len(answer['items']) == 1
    item = answer['items'][0]
    assert item['source'] == 'github' and item['state'] == 'open' and item['number'] == 24
    assert item['size'] == {'files': 4, 'additions': 128, 'deletions': 12}                # GitHub's while it waits there
    assert item['checks']['state'] == 'passed' and item['title'] == 'Fix reconnect crash' and item['base'] == 'main'
    assert item['files'] == [{'path': 'App/Fix.swift', 'additions': 1, 'deletions': 0, 'patch': '@@ -0,0 +1 @@\n+fix'}]
    assert gh.count('files') == 0                                                         # a joined item uses local git
    assert {k: answer[k] for k in ('size', 'checks', 'title', 'base')} == {
        'size': item['size'], 'checks': {k: v for k, v in item['checks'].items() if k != 'runs'},
        'title': item['title'], 'base': item['base']}


def test_a_joined_item_whose_pull_request_closed_but_branch_waits_has_the_branchs_size(repo, monkeypatch):
    git(repo, 'switch', '-q', '-c', 'fix/reconnect-crash')
    commit(repo, 'App/Fix.swift', 'fix\n')
    git(repo, 'switch', '-q', 'main')
    gh = GitHub(monkeypatch, {24: pr(state='closed')})
    answer = one([('example/app', 24)], work=branch('fix/reconnect-crash'), repos=listed(repo))
    item = answer['items'][0]
    assert item['state'] == 'draft' and item['source'] == 'github'
    assert item['size'] == {'files': 1, 'additions': 1, 'deletions': 0} and 'checks' not in item
    assert gh.count('checks') == 0                                                        # closed on GitHub: never asked
    assert answer['size'] == item['size'] and answer['title'] == 'Fix reconnect crash'


def test_a_finished_local_branch_carries_no_new_key_and_asks_git_nothing_new(repo, monkeypatch):
    monkeypatch.setattr(status, '_api', lambda *_: pytest.fail('asked GitHub'))
    asked = count_git(monkeypatch)
    git(repo, 'switch', '-q', '-c', 'feat/done')
    commit(repo, 'done', 'd\n')
    git(repo, 'switch', '-q', 'main')
    git(repo, 'merge', '-q', '--no-ff', '-m', "Merge branch 'feat/done'", 'feat/done')
    answer = one(work=branch('feat/done'), repos=listed(repo), detail=True)
    assert answer == {'profile': 'deploys', 'id': 's-release', 'state': 'merged', 'branch': 'feat/done',
                      'items': [{'state': 'merged', 'branch': 'feat/done', 'source': 'git'}]}
    assert 'diff' not in asked and 'merge-base' not in asked


def test_tips_and_merged_files_keep_their_shape(repo, tmp_path, monkeypatch):
    monkeypatch.setattr(status, '_api', lambda *_: pytest.fail('asked GitHub'))
    git(repo, 'switch', '-q', '-c', 'fix/reconnect-crash')
    commit(repo, 'App/Fix.swift', 'fix\n')
    git(repo, 'switch', '-q', 'main')
    data = tmp_path / 'data'
    data.mkdir()
    status._tips.clear()
    one(work=branch('fix/reconnect-crash'), repos=listed(repo), data_dir=str(data), detail=True)
    saved = json.loads((data / status.TIPS_NAME).read_text())
    assert saved == {f'{listed(repo)[0]["path"]}\nfix/reconnect-crash': git(repo, 'rev-parse', 'fix/reconnect-crash')}
    status._tips.clear()


def test_the_route_takes_detail_and_honours_it_for_four_or_fewer(tmp_path, monkeypatch):
    import test_session_pr_status as base
    from fastapi import FastAPI, Request
    from types import SimpleNamespace
    from gateway.session_contract import SessionActor
    from gateway.config import Platform
    from gateway import session_authorities, session_history_mount
    from hermes_cli import web_server_sessions, profiles
    from hermes_state import SessionDB
    from fastapi.testclient import TestClient
    api = base._api_module(tmp_path, monkeypatch, 'pr_review_route_test')
    batches = []
    monkeypatch.setattr(api.session_pr_status, 'resolve_batch', lambda items: batches.append(items) or [
        {'profile': i['profile'], 'id': i['id'], 'state': 'none'} for i in items])
    home = tmp_path / 'default'
    home.mkdir()
    db = SessionDB(db_path=home / 'state.db')
    db.create_session('s-1', source='desktop')
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
    client = TestClient(app)
    one_ = {'sessions': [{'profile': 'default', 'id': 's-1'}]}
    assert client.post('/sessions/pr-status', json=one_).status_code == 200
    assert batches[-1][0]['detail'] is False
    client.post('/sessions/pr-status', json={**one_, 'detail': True})
    assert batches[-1][0]['detail'] is True
    client.post('/sessions/pr-status', json={'sessions': one_['sessions'] * 5, 'detail': True})
    assert batches[-1][0]['detail'] is False
    assert client.post('/sessions/pr-status', json={**one_, 'detail': 'yes'}).status_code == 422
    assert client.post('/sessions/pr-status', json={**one_, 'detail': 1}).status_code == 422
