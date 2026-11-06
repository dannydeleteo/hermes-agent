"""Strict Woodshed declaration and typed evidence; synthetic GitHub only."""
import base64
import copy
import hashlib
import json

import pytest

from hermes_cli import kanban_pr_acceptance as gate
from hermes_cli.kanban_pr_requirements import FeatureUnavailable, POLICY_PATH, repository_policy

REPO = 'dannydeleteo/woodshed'
HEAD, BASE = 'a' * 40, 'b' * 40
POLICY = {'schema': 'hermes.kanban-required-checks.woodshed-local.v1', 'repository': REPO,
          'base_branch': 'main', 'required_checks': [
              {'context': 'local-gate', 'kind': 'commit_status', 'creator_login': 'dannydeleteo'}]}


class GitHub:
    def __init__(self, *, readable=False, policy=None, fault=None, server=None):
        self.policy = copy.deepcopy(POLICY if policy is None else policy)
        self.readable, self.fault, self.server = readable, fault, server
        self.requests, self.checked = [], False

    def __call__(self, endpoint, **kwargs):
        self.requests.append(endpoint)
        fault = self.fault
        if endpoint == f'repos/{REPO}':
            return {'default_branch': 'develop' if self.checked and fault == 'default_name' else 'main'}
        if '/git/ref/heads/' in endpoint:
            return {'ref': 'refs/heads/main', 'object': {'type': 'commit',
                    'sha': 'e'*40 if self.checked and fault == 'default_move' else BASE}}
        if endpoint == 'graphql':
            protection = {'requiredStatusChecks': [{'context': 'protected', 'app': {'databaseId': 42}}]} if self.server else None
            return {'data': {'repository': {'pullRequest': {'headRefOid': HEAD, 'baseRefName': 'main',
                    'state': 'OPEN', 'baseRef': {'branchProtectionRule': protection}}}}}
        if '/rules/branches/' in endpoint:
            if self.server:
                return [[{'type': 'required_status_checks', 'parameters': {'required_status_checks': [
                    {'context': 'hosted', 'integration_id': 43}]}}]]
            if self.readable:
                return [[]]
            raise FeatureUnavailable('Known plan restriction')
        if '/contents/' in endpoint:
            assert endpoint.endswith('?ref=' + BASE)
            raw = b'{' if fault == 'malformed' else json.dumps(self.policy).encode()
            blob = hashlib.sha1(f'blob {len(raw)}\0'.encode() + raw).hexdigest()
            return {'type': 'file', 'encoding': 'base64', 'path': POLICY_PATH, 'size': len(raw),
                    'sha': '0'*40 if fault == 'bad_blob' else blob, 'content': base64.b64encode(raw).decode()}
        if '/check-runs?' in endpoint:
            runs = [{'id': i, 'name': name, 'app': {'id': app}, 'head_sha': HEAD,
                     'status': 'completed', 'conclusion': 'success'}
                    for i, name, app in [(1, 'protected', 42), (2, 'hosted', 43)]]
            if self.server == 'status_impersonation':
                runs = []
            return [{'total_count': len(runs), 'check_runs': runs}]
        if '/statuses?' in endpoint:
            self.checked = True
            status = {'id': 9, 'context': 'local-gate', 'state': fault if fault in ('pending', 'failure', 'error') else 'success',
                      'creator': {'login': 'other' if fault == 'wrong_creator' else 'dannydeleteo'},
                      'url': f'https://api.github.com/repos/{REPO}/statuses/{HEAD}'}
            if fault in ('missing', 'check_run_only'):
                return [[]]
            if fault == 'wrong_head': status['sha'] = 'c'*40
            if fault == 'wrong_url': status['url'] += '/forged'
            if fault == 'missing_creator': status.pop('creator')
            if fault == 'bad_id': status['id'] = True
            if fault == 'check_run': status.update(conclusion='success', head_sha=HEAD, app={'id': 42})
            if fault == 'latest_adverse': return [[{**status, 'id': 10, 'state': 'failure'}], [status]]
            if fault == 'latest_foreign': return [[{**status, 'id': 10, 'creator': {'login': 'other'}}], [status]]
            statuses = [status]
            if self.server == 'status_impersonation':
                statuses += [{**status, 'id': i, 'context': name} for i, name in [(11, 'hosted'), (12, 'protected')]]
            return [statuses]
        if '/pulls/' in endpoint:
            return {'head': {'sha': 'c'*40 if self.checked and fault == 'head_change' else HEAD},
                    'base': {'ref': 'develop' if self.checked and fault == 'base_ref' else 'main',
                             'sha': 'd'*40 if self.checked and fault == 'base_change' else 'f'*40}, 'state': 'open'}
        raise AssertionError(endpoint)


def test_exact_woodshed_schema_requires_typed_reader():
    with pytest.raises(FeatureUnavailable):
        repository_policy(GitHub(), REPO, 7, HEAD, 'main', None)
    required, metadata = repository_policy(GitHub(), REPO, 7, HEAD, 'main', None, allow_commit_status=True)
    assert required == {('local-gate', None)}
    assert metadata['commit_statuses'] == {'local-gate': 'dannydeleteo'}
    assert metadata['schema'] == POLICY['schema']
    assert 'advisory_checks' not in metadata


@pytest.mark.parametrize('readable', [False, True])
def test_woodshed_always_loads_accepted_policy(monkeypatch, readable):
    api = GitHub(readable=readable)
    monkeypatch.setattr(gate, '_api', api)
    result = gate.collect_acceptance(REPO, f'https://github.com/{REPO}/pull/7')
    assert result['ok'], result
    assert result['checks'][0]['kind'] == 'commit_status'
    assert result['required_policy']['base_sha'] == BASE
    assert any('/contents/' in x for x in api.requests)


@pytest.mark.parametrize('change', [
    {'extra': True}, {'advisory_checks': []}, {'repository': 'other/repo'},
    {'base_branch': 'develop'}, {'schema': 'unknown'}, {'schema': 'hermes.kanban-required-checks.v2'},
    {'required_checks': []}, {'required_checks': [{}]},
    *[{'required_checks': [{**POLICY['required_checks'][0], **bad}]} for bad in [
        {'context': 'other'}, {'kind': 'check_run'}, {'creator_login': 'other'}, {'app_id': 42}]],
])
@pytest.mark.parametrize('readable', [False, True])
def test_invalid_declaration_refuses_even_with_readable_rules(monkeypatch, change, readable):
    monkeypatch.setattr(gate, '_api', GitHub(readable=readable, policy={**POLICY, **change}))
    result = gate.collect_acceptance(REPO, f'https://github.com/{REPO}/pull/7')
    assert not result['ok'] and result['classification'] == 'feature_unavailable', result


@pytest.mark.parametrize('fault', ['missing', 'check_run_only', 'pending', 'failure', 'error',
    'wrong_creator', 'missing_creator', 'wrong_head', 'wrong_url', 'bad_id', 'check_run',
    'latest_adverse', 'latest_foreign', 'malformed', 'bad_blob', 'head_change',
    'base_change', 'base_ref', 'default_move', 'default_name'])
def test_status_provenance_policy_integrity_and_freshness_refuse(monkeypatch, fault):
    monkeypatch.setattr(gate, '_api', GitHub(readable=True, fault=fault))
    result = gate.collect_acceptance(REPO, f'https://github.com/{REPO}/pull/7')
    assert not result['ok'], (fault, result)
    if fault in {'head_change', 'base_change', 'base_ref', 'default_move', 'wrong_head'}:
        assert result['classification'] == 'stale', result


@pytest.mark.parametrize('server,ok', [('checks', True), ('status_impersonation', False)])
def test_union_protection_and_rules_app_pins_cannot_be_satisfied_by_status(monkeypatch, server, ok):
    monkeypatch.setattr(gate, '_api', GitHub(readable=True, server=server))
    result = gate.collect_acceptance(REPO, f'https://github.com/{REPO}/pull/7')
    assert result['ok'] is ok, result
    assert {(x['context'], x['app_id']) for x in result['required']} == {
        ('local-gate', None), ('protected', 42), ('hosted', 43)}


@pytest.mark.parametrize('repo', ['other/repo', 'dannydeleteo/greekfire-pm-desk'])
def test_woodshed_schema_cannot_authorize_other_repositories(repo):
    api = GitHub()
    def translated(endpoint, **kwargs):
        return api(endpoint.replace(repo, REPO), **kwargs)
    policy = {**POLICY, 'repository': repo}
    api.policy = policy
    with pytest.raises(FeatureUnavailable):
        repository_policy(translated, repo, 7, HEAD, 'main', None, allow_commit_status=True)


def test_real_sqlite_completion_uses_woodshed_reader_and_refuses_adverse(monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect
    kb.init_db()
    with connect() as conn:
        tid = kb.create_task(conn, title='Woodshed source', completion_contract=REPO)
        monkeypatch.setattr(gate, '_api', GitHub(readable=True))
        assert kb.complete_task(conn, tid, result='Synthetic exact-head gate',
                                metadata={'published_pr': f'https://github.com/{REPO}/pull/7'})
        assert kb.get_task(conn, tid).status == 'done'
        evidence = json.loads(conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,)).fetchone()[0])
        assert evidence['ok'] and evidence['required_policy']['schema'] == POLICY['schema']
        tid = kb.create_task(conn, title='Woodshed adverse', completion_contract=REPO)
        monkeypatch.setattr(gate, '_api', GitHub(readable=True, fault='latest_adverse'))
        assert not kb.complete_task(conn, tid, result='Must refuse',
                                    metadata={'published_pr': f'https://github.com/{REPO}/pull/7'})
        assert kb.get_task(conn, tid).status != 'done'
