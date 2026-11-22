"""Read-only GitHub PR status for the pull requests a session opened. No transcript is executed.

Which pull requests are a session's is decided locally (session_pr_created): only one the session's
own tool call opened. This module asks GitHub what became of them.

* The computer's own GitHub sign-in (``gh``, ``GH_TOKEN``, a keychain, ``~/.netrc``) is never used.
  A caller of the route is not necessarily the person ``gh`` is signed in as.
* GitHub is asked about a pull request only when either
    - the owner listed its repository as public in ``<plugin-data>/session-pr.json``
      (``"public_repos": ["owner/repo"]``); then GitHub is asked anonymously, by number, or
    - the signed-in owner put a GitHub token of their own at
      ``<plugin-data>/session-pr-github/<sha256 of their subject>.token`` (mode 0600, owned by this
      user); then GitHub is asked with that token only, for that owner's sessions only.
  Otherwise nothing leaves the computer and the pull request's state is ``unknown``.
* A session that opened none is ``none``. A session whose history could not be read is ``unknown``.
"""
import hashlib
import importlib
import importlib.util
import json
import os
import re
import stat
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote

MAX_BATCH = 32
TTL = 180          # an open or draft pull request is asked about again after this long
TTL_CLOSED = 3600  # a closed one can be reopened
TTL_FAILED = 30
MAX_CACHE = 4096
_TIMEOUT = 8
_MAX_RESPONSE = 4_000_000
API_ROOT = 'https://api.github.com'
CONFIG_NAME = 'session-pr.json'
MERGED_NAME = 'session-pr-merged.json'   # merged is final: remembered across restarts (public repos only)
TOKEN_DIR = 'session-pr-github'
_cache = OrderedDict()
_lock = threading.Lock()
_gh_slots = threading.BoundedSemaphore(4)
_PROFILE = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9_.-]{0,63}\Z")
_ID = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9_.:-]{0,255}\Z")
_REPO = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?/[A-Za-z0-9_.-]{1,100}\Z")
_TOKEN = re.compile(r"[A-Za-z0-9_]{20,255}\Z")

# Everything the route calls on the runtime. The app is told the endpoint exists only when all of it does.
_RUNTIME = (
    ('hermes_cli.web_server_sessions', '_session_mutation_context'),
    ('hermes_cli.web_server_cron', '_cron_profile_home'),
    ('gateway.owner_identity', 'host_owner_subject'),
    ('gateway.session_contract', 'SessionActor'),
    ('hermes_cli.profiles', 'get_profile_dir'),
    ('gateway.config', 'Platform'),
    ('gateway.owner_identity', 'same_owner'),
    ('gateway.session_authorities', 'require_serving'),
    ('gateway.session_history_mount', 'is_history_mount'),
)
_supported = None


def store_rules():
    """The session store's ownership rules the route judges a stored chat by, from whichever Hermes runs:
    ``(POLICY_PREFIX, validate_local_lineage(conn, receipt), legacy_history_row(row), BINDING_PREFIX,
    binding_names(saved, subject))``. Hermes v0.23.3 moved them out of the old ``hermes_state_*`` modules into
    ``persistence/`` and ``session_rules/`` and gave the row rules the host's owner rule as an argument; before
    this, that update turned the whole pull request status off (owner 2026-11-21). Raises when neither is there."""
    try:
        from hermes_state_local import POLICY_PREFIX
        from hermes_state_local_lineage import validate_local_lineage
        from hermes_state_local_migration import legacy_history_row
        from hermes_state_mutation_binding import BINDING_PREFIX, binding_names
        return POLICY_PREFIX, validate_local_lineage, legacy_history_row, BINDING_PREFIX, binding_names
    except ImportError:
        pass
    from persistence.sqlite.conversation._store.local import POLICY_PREFIX
    from persistence.sqlite.conversation._store.local_lineage import validate_local_lineage
    from persistence.sqlite.authority._binding_history import BINDING_PREFIX
    from session_rules import legacy_local
    from gateway.session_persistence import owner_rule

    def legacy_history_row(row):
        return legacy_local.legacy_history_row(row, owner_rule())

    def binding_names(saved, subject):
        return legacy_local.binding_names(saved, subject, owner_rule())

    return POLICY_PREFIX, validate_local_lineage, legacy_history_row, BINDING_PREFIX, binding_names


def supported():
    """Whether this Hermes has every session-ownership API the route relies on (checked once)."""
    global _supported
    if _supported is None:
        try:
            ok = all(hasattr(importlib.import_module(module), name) for module, name in _RUNTIME)
            ok = ok and hasattr(importlib.import_module('gateway.config').Platform, 'LOCAL')
            ok = ok and all(callable(rule) or isinstance(rule, str) for rule in store_rules())
        except Exception:
            ok = False
        _supported = bool(ok)
    return _supported


def valid_identity(profile, session_id):
    return (isinstance(profile, str) and bool(_PROFILE.fullmatch(profile)) and profile not in ('.', '..')
            and isinstance(session_id, str) and bool(_ID.fullmatch(session_id)) and '..' not in session_id)


def _valid_repo(repo):
    return (isinstance(repo, str) and bool(_REPO.fullmatch(repo))
            and repo.split('/')[1] not in ('.', '..') and '..' not in repo)


def _read_private(path, limit):
    """A regular file owned by this user, not a symlink, readable by nobody else; else None."""
    if not hasattr(os, 'O_NOFOLLOW') or not hasattr(os, 'getuid'):
        return None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, 'O_NONBLOCK', 0))
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077
                or info.st_size > limit):
            return None
        with os.fdopen(fd, 'rb', closefd=False) as handle:
            return handle.read(limit + 1).decode('utf-8')
    except (OSError, UnicodeDecodeError):
        return None
    finally:
        os.close(fd)


def load_config(data_dir):
    """The owner's choices in ``<plugin-data>/session-pr.json``. Anything malformed counts as unset."""
    if not data_dir:
        return {'public_repos': frozenset(), 'repos': (), 'by_hand': ()}
    try:
        linked = marks().repos(data_dir)       # the repositories linked from the app, per project
    except Exception:
        linked = ()
    empty = {'public_repos': frozenset(r['public_repo'].lower() for r in linked if r['public_repo']),
             'repos': linked, 'by_hand': ()}
    path = Path(data_dir) / CONFIG_NAME
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 65536:
            return empty
        raw = json.loads(path.read_text())
    except (OSError, ValueError, UnicodeDecodeError):
        return empty
    if not isinstance(raw, dict):
        return empty
    repos = raw.get('public_repos', [])
    if not isinstance(repos, list) or not all(_valid_repo(r) for r in repos):
        return empty
    by_hand = _local().load_repos(raw.get('repos'))
    paths = {r['path'] for r in linked}
    return {'public_repos': frozenset(r.lower() for r in repos) | empty['public_repos'],
            'repos': linked + tuple(r for r in by_hand if r['path'] not in paths), 'by_hand': by_hand}


def owner_token_path(data_dir, subject):
    """Where the owner *subject* may keep a GitHub token of their own for this endpoint."""
    digest = hashlib.sha256(str(subject).encode('utf-8')).hexdigest()
    return Path(data_dir) / TOKEN_DIR / f'{digest}.token'


class Access:
    """What GitHub may be asked for one owner's sessions. Never holds the host's credentials."""
    __slots__ = ('public_repos', 'token', 'key', 'data_dir')

    def __init__(self, public_repos=frozenset(), token=None, data_dir=None):
        self.public_repos = frozenset(public_repos)
        self.token = token
        self.data_dir = data_dir
        # Cached answers are shared only between callers with the same access: anonymous, or one token.
        self.key = ('token:' + hashlib.sha256(token.encode()).hexdigest()[:32]) if token else 'anonymous'

    def __repr__(self):
        return f'Access(public_repos={sorted(self.public_repos)!r}, token={"set" if self.token else None})'


def access_for(data_dir, subject, config=None):
    config = config if config is not None else load_config(data_dir)
    token = None
    if data_dir and isinstance(subject, str) and subject:
        text = _read_private(owner_token_path(data_dir, subject), 512)
        if text is not None and _TOKEN.fullmatch(text.strip()):
            token = text.strip()
    return Access(config['public_repos'], token, data_dir)


def _sibling(name, file):
    # The readers are maintained separately; load them lazily so a missing reader fails closed.
    module = sys.modules.get(name)
    if module is None:
        spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(file))
        if spec is None or spec.loader is None:
            raise ImportError('session history reader unavailable')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        sys.modules[name] = module
    return module


def _local():
    return _sibling('hermes_hq_session_pr_local', 'session_pr_local.py')


def marks():
    """The owner's own marks and links (session_pr_marks)."""
    return _sibling('hermes_hq_session_pr_marks', 'session_pr_marks.py')


TIPS_NAME = 'session-pr-tips.json'     # the tip of each branch seen waiting: tells merged from given up later
_tips = {}                              # data dir -> {"<repo path>\n<branch>": commit}


def _tips_for(data_dir):
    if not data_dir:
        return None
    with _lock:
        if data_dir not in _tips:
            try:
                saved = json.loads((Path(data_dir) / TIPS_NAME).read_text())
                _tips[data_dir] = {k: v for k, v in saved.items() if isinstance(k, str) and isinstance(v, str)} \
                    if isinstance(saved, dict) else {}
            except (OSError, ValueError, UnicodeDecodeError):
                _tips[data_dir] = {}
        return _tips[data_dir]


def _save_tips(data_dir, tips, before):
    if tips is None or dict(tips) == before:
        return
    with _lock:
        for key in list(tips)[:-3000]:
            del tips[key]
        try:
            path = Path(data_dir) / TIPS_NAME
            temp = path.with_name(f'{path.name}.{os.getpid()}.tmp')
            temp.write_text(json.dumps(tips))
            os.chmod(temp, 0o600)
            os.replace(temp, path)
        except OSError:
            pass


def created_in(conn, session_id):
    """The pull requests one session opened, read on the caller's connection and snapshot."""
    return _sibling('hermes_hq_session_pr_created', 'session_pr_created.py').created_from_conn(conn, session_id)


def work_in(conn, session_id):
    """The branches and commits one session made, read on the caller's connection and snapshot."""
    return _local().work_from_conn(conn, session_id)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # A redirect could carry an owner's token to another host; a moved repository is simply unknown.
    def redirect_request(self, *args, **kwargs):
        return None


_opener = urllib.request.build_opener(_NoRedirect())


class _Unchanged(Exception):
    pass


def _api(endpoint, access, etag=None):
    """GET api.github.com*endpoint*, anonymously or with the owner's own token; nothing else.

    Returns ``(json, etag)``; raises ``_Unchanged`` when GitHub answers 304 to *etag*.
    """
    if not isinstance(endpoint, str) or not endpoint.startswith('/repos/') or '\\' in endpoint or '@' in endpoint:
        raise ValueError('invalid endpoint')
    headers = {'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28',
               'User-Agent': 'hermes-hq-push'}
    if access.token:
        headers['Authorization'] = f'Bearer {access.token}'
    if etag:
        headers['If-None-Match'] = etag
    request = urllib.request.Request(API_ROOT + endpoint, headers=headers, method='GET')
    with _gh_slots:
        try:
            with _opener.open(request, timeout=_TIMEOUT) as response:
                if response.status != 200:
                    raise ValueError('unexpected status')
                body = response.read(_MAX_RESPONSE + 1)
                tag = response.headers.get('ETag') if getattr(response, 'headers', None) is not None else None
        except urllib.error.HTTPError as error:
            if error.code == 304 and etag:
                raise _Unchanged() from None
            raise
    if len(body) > _MAX_RESPONSE:
        raise ValueError('response too large')
    return json.loads(body), tag if isinstance(tag, str) and len(tag) < 256 else None


def _pr_result(pr, repo, branch=None, number=None, head_repo=None):
    if not isinstance(pr, dict):
        raise ValueError('invalid PR')
    num = pr.get('number')
    base = pr.get('base')
    head = pr.get('head')
    if (type(num) is not int or num <= 0 or (number is not None and num != number)
            or not isinstance(base, dict) or not isinstance(base.get('repo'), dict)
            or str(base['repo'].get('full_name', '')).lower() != repo.lower()
            or not isinstance(head, dict) or not isinstance(head.get('ref'), str)
            or (branch is not None and head['ref'] != branch)
            or (head_repo is not None and (not isinstance(head.get('repo'), dict)
                or str(head['repo'].get('full_name', '')).lower() != head_repo.lower()))):
        raise ValueError('mismatched PR')
    state = pr.get('state')
    if state not in ('open', 'closed') or type(pr.get('draft')) is not bool:
        raise ValueError('invalid state')
    if state == 'open':
        state = 'draft' if pr['draft'] else 'open'
    elif pr.get('merged_at'):
        state = 'merged'
    else:
        state = 'closed'
    return {'state': state, 'repo': repo, 'branch': head['ref'], 'number': num,
            'url': f'https://github.com/{repo}/pull/{num}'}


_merged_loaded = set()


def _load_merged(data_dir):
    """Merged pull requests of public repositories remembered from earlier runs (asked about once)."""
    if not data_dir or data_dir in _merged_loaded:
        return
    _merged_loaded.add(data_dir)
    try:
        path = Path(data_dir) / MERGED_NAME
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 2_000_000:
            return
        saved = json.loads(path.read_text())
        for repo, numbers in saved.items() if isinstance(saved, dict) else ():
            if not _valid_repo(repo) or not isinstance(numbers, list):
                continue
            for number in numbers[-2000:]:
                if type(number) is int and 0 < number <= 2147483647:
                    _cache[('anonymous', repo.lower(), number)] = (
                        float('inf'), {'state': 'merged', 'repo': repo, 'number': number,
                                       'url': f'https://github.com/{repo}/pull/{number}'}, None)
    except (OSError, ValueError, UnicodeDecodeError):
        return


def _save_merged(data_dir):
    if not data_dir:
        return
    saved = {}
    for (who, _, number), (_, result, _) in list(_cache.items()):
        if who == 'anonymous' and result.get('state') == 'merged':
            saved.setdefault(result['repo'], []).append(number)
    try:
        path = Path(data_dir) / MERGED_NAME
        temp = path.with_name(path.name + f'.{os.getpid()}.tmp')
        temp.write_text(json.dumps({repo: sorted(numbers) for repo, numbers in sorted(saved.items())}))
        os.replace(temp, path)
    except OSError:
        pass


FRESH_AFTER = 15   # a chat being opened asks again about its waiting pull requests once they are this old


def _lookup(repo, number, access, fresh=False):
    """What became of one pull request: draft, open, merged, closed, or unknown (never a guess).

    The answer carries ``_head`` (the pull request's branch) when GitHub said it, for joining it to the
    chat's own branch; it is never sent. *fresh*: a waiting answer older than ``FRESH_AFTER`` is asked
    again (with its ETag, so an unchanged one costs GitHub nothing new)."""
    link = {'repo': repo, 'number': number, 'url': f'https://github.com/{repo}/pull/{number}'}
    unknown = {'state': 'unknown', **link}
    if not _valid_repo(repo) or type(number) is not int or not 0 < number <= 2147483647:
        return {'state': 'unknown'}
    # Nothing unproven leaves the computer: with no owner token, only a repository the owner
    # declared public is asked about.
    if not access.token and repo.lower() not in access.public_repos:
        return unknown
    key = (access.key, repo.lower(), number)
    with _lock:
        if not access.token:
            _load_merged(access.data_dir)
        entry = _cache.get(key)
        now = time.monotonic()
        stale = (fresh and entry is not None and entry[1].get('state') in ('draft', 'open')
                 and now - (entry[0] - TTL) >= FRESH_AFTER)
        if entry and entry[0] > now and not stale:
            _cache.move_to_end(key)
            return dict(entry[1])
    previous = entry[1] if entry and entry[1].get('state') != 'unknown' else None
    etag = entry[2] if previous else None
    owner, name = repo.split('/')
    try:
        pr, tag = _api(f'/repos/{quote(owner, safe="")}/{quote(name, safe="")}/pulls/{number}', access, etag)
        result = {('_head' if key_ == 'branch' else key_): value
                  for key_, value in _pr_result(pr, repo, None, number).items()}
        etag = tag
    except _Unchanged:
        result = dict(previous)
    except Exception:
        # GitHub didn't answer (offline, rate limit). What it last said is better than nothing;
        # with no earlier answer the state is unknown.
        result = dict(previous) if previous else unknown
        with _lock:
            _cache[key] = (time.monotonic() + TTL_FAILED, result, etag)
        return dict(result)
    life = {'merged': float('inf'), 'closed': TTL_CLOSED}.get(result['state'], TTL)
    with _lock:
        _cache[key] = (time.monotonic() + life, dict(result), etag)
        _cache.move_to_end(key)
        while len(_cache) > MAX_CACHE:
            _cache.popitem(last=False)
        if result['state'] == 'merged' and not access.token and not (previous and previous['state'] == 'merged'):
            _save_merged(access.data_dir)
    return result


def _public(answer):
    """An answer as it is sent: nothing internal (``_head``, ``direct``, ``source``)."""
    return {key: value for key, value in answer.items() if not key.startswith('_') and key not in ('direct', 'source')}


_WAITING = ('draft', 'open')


def _finished_pick(answers):
    """Of finished answers (oldest first): the newest merged one, else the newest closed one."""
    for state in ('merged', 'closed'):
        for answer in reversed(answers):
            if answer['state'] == state:
                return answer
    return None


def _resolve_created(created, access=None, fresh=False):
    """One answer from the pull requests a session opened itself, oldest first; None when it opened none.

    The newest one still waiting (draft or open) if there is one; else an unreadable one, because it
    may be the one still waiting; else the newest merged one, else the newest closed one.
    """
    access = access if access is not None else Access()
    answers = [_lookup(repo, number, access, fresh) for repo, number in (created or ())]
    if not answers:
        return None
    pick = next((a for a in reversed(answers) if a['state'] in _WAITING), None) \
        or next((a for a in reversed(answers) if a['state'] == 'unknown'), None) or _finished_pick(answers)
    return _public(pick or answers[-1])


def _joined(github, git):
    """One pull request and the chat's own branch it was opened from, as one answer.

    Waiting on GitHub wins; then work still waiting on the branch; then landed (merged on GitHub, or the
    branch reached main without the pull request); then closed. GitHub's number and link are kept."""
    if github['state'] in _WAITING:
        state = github['state']
    elif git['state'] == 'draft':
        state = 'draft'
    elif 'merged' in (github['state'], git['state']):
        state = 'merged'
    elif 'closed' in (github['state'], git['state']):
        state = 'closed'
    else:
        state = github['state']
    joined = {**git, **{k: v for k, v in github.items() if k in ('repo', 'number', 'url')}, 'state': state,
              'branch': git.get('branch') or github.get('_head'), 'source': 'github'}
    return joined


def _detail(created, work, access=None, repos=(), tips=None, fresh=False):
    """``(answer, items)``: where a session's code changes stand as one answer, and every pull request
    and branch of its own that it rests on (newest first, at most ``MAX_ITEMS``).

    The one answer: work still waiting comes first: a pull request waiting on GitHub (``draft`` or
    ``open``), then a branch not merged yet (``draft``). Then a pull request GitHub couldn't be asked
    about, when no branch of the chat's own stands for it: it may be the one still waiting. Then what is
    finished: merged before closed (a chat whose work landed is not undone by a later attempt it gave
    up). Then, when older work was left out or half the history couldn't be read, ``unknown``. No pull
    request and no branch of its own: ``none``.
    """
    if created is None and work is None:
        return {'state': 'unknown'}, []
    access = access if access is not None else Access()
    github = [dict(_lookup(repo, number, access, fresh), source='github') for repo, number in (created or ())]
    try:
        git, unplaced = _local().local_items(work, repos, tips) if work and repos else ([], [])
    except Exception:
        git, unplaced = [], [None]
    git = [dict(answer, source='git') for answer in git]
    # A pull request opened from one of the chat's own branches is that branch's: one item, not two.
    for index, pr in enumerate(github):
        head = pr.get('_head')
        match = next((i for i, answer in enumerate(git) if head and answer.get('branch') == head), None)
        if match is not None:
            github[index] = _joined(pr, git.pop(match))
    known = git + github          # the same state from GitHub is told before a branch's (newest last)
    items = [{**_public(a), 'source': a['source']} for a in list(reversed(github)) + list(reversed(git))]
    items += [{'state': 'unknown', 'branch': name, 'source': 'git'} for name in reversed(unplaced) if name]
    items = items[:MAX_ITEMS]

    waiting = next((a for a in reversed(github) if a['state'] in _WAITING), None) \
        or next((a for a in reversed(git) if a['state'] in _WAITING), None)
    if waiting is not None:
        return _public(waiting), items
    unreadable = next((a for a in reversed(github) if a['state'] == 'unknown'), None)
    if unreadable is not None and not git:
        return _public(unreadable), items
    finished = _finished_pick([a for a in known if a['state'] in ('merged', 'closed')])
    truncated = bool(getattr(created, 'truncated', False)) or bool((work or {}).get('truncated'))
    if finished is not None and not truncated:
        return _public(finished), items
    if truncated:
        return {'state': 'unknown'}, items       # older work was left out: it may still be waiting
    if unreadable is not None:
        return _public(unreadable), items
    if created is None or work is None or unplaced:
        # Half the history could not be read, or the chat made a branch no listed repository
        # knows: not proof of nothing.
        return {'state': 'unknown'}, items
    return {'state': 'none'}, items


MAX_ITEMS = 12


def _computed(created, work, access=None, repos=(), tips=None):
    return _detail(created, work, access, repos, tips)[0]


def _resolve(created, work, access=None, repos=(), tips=None, closed=False):
    return _resolve_detail(created, work, access, repos, tips, closed)[0]


def _resolve_detail(created, work, access=None, repos=(), tips=None, closed=False, fresh=False):
    answer, items = _detail(created, work, access, repos, tips, fresh)
    # The owner's own mark: a label over whatever isn't merged, a pull request still open on GitHub
    # included (each item keeps what GitHub or git says). Work that reached main says so itself.
    if closed and answer.get('state') in ('none', 'draft', 'open', 'unknown', 'closed'):
        answer = {**answer, 'state': 'closed', 'manual': True}
    return answer, items


def resolve_batch(items):
    """*items*: ``{'profile', 'id', 'created', 'work', 'access', 'repos', 'data_dir', 'closed', 'fresh'}`` already
    authorized and read in one snapshot."""
    if len(items) > MAX_BATCH:
        raise ValueError('too many sessions')
    data_dir = next((item.get('data_dir') for item in items if item.get('data_dir')), None)
    tips = _tips_for(data_dir)
    before = dict(tips) if tips is not None else None

    def one(item):
        try:
            result, listed = _resolve_detail(item.get('created'), item.get('work', {}), item.get('access'),
                                             item.get('repos') or (), tips, bool(item.get('closed')),
                                             bool(item.get('fresh')))
        except Exception:
            result, listed = {'state': 'unknown'}, []
        answer = {'profile': item['profile'], 'id': item['id'], **result}
        if listed:
            answer['items'] = listed   # every pull request and branch the answer rests on (0.9.75)
        return answer

    with ThreadPoolExecutor(max_workers=4) as pool:
        out = list(pool.map(one, items))
    _save_tips(data_dir, tips, before)
    return out
