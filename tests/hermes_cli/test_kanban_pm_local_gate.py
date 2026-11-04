"""PM-only accepted local-status policy; no live GitHub or runtime mutation."""
import base64
import copy
import hashlib
import json

import pytest

from hermes_cli import kanban_pr_acceptance as gate
from hermes_cli.kanban_pr_requirements import FeatureUnavailable, POLICY_PATH

REPO = 'dannydeleteo/greekfire-pm-desk'
HEAD, BASE = 'a' * 40, 'b' * 40
POLICY = {'schema': 'hermes.kanban-required-checks.v2', 'repository': REPO, 'base_branch': 'main',
          'required_checks': [{'context': 'local-gate', 'kind': 'commit_status', 'creator_login': 'dannydeleteo'}],
          'advisory_checks': [{'context': 'python', 'app_id': 15368}, {'context': 'node', 'app_id': 15368}]}


class GitHub:
    def __init__(self, fault=None, readable_rules=False):
        self.fault = fault
        self.readable_rules = readable_rules
        self.requests = []
        self.checked = False
        self.race = None

    def __call__(self, endpoint, **kwargs):
        self.requests.append(endpoint)
        fault = self.fault
        if endpoint == f'repos/{REPO}':
            return {'default_branch': 'main'}
        if '/git/ref/heads/' in endpoint:
            return {'ref': 'refs/heads/main', 'object': {'type': 'commit', 'sha': 'e'*40 if self.checked and fault == 'default_move' else BASE}}
        if endpoint == 'graphql':
            return {'data': {'repository': {'pullRequest': {'headRefOid': HEAD, 'baseRefName': 'main',
                    'state': 'OPEN', 'baseRef': {'branchProtectionRule': None}}}}}
        if '/rules/branches/' in endpoint:
            if fault in ('server_check', 'server_wrong_app'):
                return [[{'type': 'required_status_checks', 'parameters': {'required_status_checks': [{'context': 'python', 'integration_id': 15368}]}}]]
            if self.readable_rules:
                return [[]]
            raise FeatureUnavailable('Known plan restriction')
        if '/contents/' in endpoint:
            assert endpoint.endswith('?ref=' + BASE)
            policy = copy.deepcopy(POLICY)
            if fault == 'wrong_repo': policy['repository'] = 'other/repo'
            if fault == 'wrong_kind': policy['required_checks'][0]['kind'] = 'check_run'
            if fault == 'wrong_policy_creator': policy['required_checks'][0]['creator_login'] = 'other'
            if fault == 'ambiguous_app': policy['required_checks'][0]['app_id'] = 15368
            if fault == 'empty_policy': policy['required_checks'] = []
            if fault == 'v1_unpinned':
                policy = {'schema': 'hermes.kanban-required-checks.v1', 'base_branch': 'main', 'required_checks': [{'context': 'local-gate', 'app_id': None}]}
            raw = json.dumps(policy).encode()
            blob = hashlib.sha1(f'blob {len(raw)}\0'.encode() + raw).hexdigest()
            return {'type': 'file', 'encoding': 'base64', 'path': POLICY_PATH, 'size': len(raw),
                    'sha': '0'*40 if fault == 'bad_blob' else blob, 'content': base64.b64encode(raw).decode()}
        if '/check-runs?' in endpoint:
            assert fault in ('server_check', 'server_wrong_app'), 'advisory Actions must not be a dependency'
            run = {'id': 42, 'name': 'python', 'app': {'id': 9 if fault == 'server_wrong_app' else 15368},
                   'head_sha': HEAD, 'status': 'completed', 'conclusion': 'success'}
            return [{'total_count': 1, 'check_runs': [run]}]
        if '/statuses?' in endpoint:
            self.checked = True
            if self.race: self.race()
            if fault in ('missing', 'check_run_only'):
                return [[]]
            status = {'id': 9, 'context': 'local-gate', 'state': fault if fault in ('pending','failure','error') else 'success',
                      'creator': {'login': 'other' if fault == 'wrong_creator' else 'dannydeleteo'},
                      'url': f'https://api.github.com/repos/{REPO}/statuses/{HEAD}'}
            if fault == 'wrong_head': status['sha'] = 'c'*40
            if fault == 'wrong_url': status['url'] = f'https://api.github.com/repos/{REPO}/statuses/'+'c'*40
            if fault == 'forged_check_run': status.update(conclusion='success', app={'id':15368}, head_sha=HEAD)
            if fault == 'missing_creator': status.pop('creator')
            if fault == 'latest_failure': return [[status, {**status, 'id':10, 'state':'failure'}]]
            return [[status]]
        if '/pulls/' in endpoint:
            return {'head': {'sha': 'c'*40 if self.checked and fault == 'head_change' else HEAD},
                    'base': {'ref': 'main', 'sha': 'd'*40 if self.checked and fault == 'base_change' else 'f'*40}, 'state':'open'}
        raise AssertionError(endpoint)


@pytest.mark.parametrize('readable_rules', [False, True])
def test_truthful_pm_local_status_succeeds_without_hosted_jobs(monkeypatch, readable_rules):
    api = GitHub(readable_rules=readable_rules)
    monkeypatch.setattr(gate, '_api', api)
    result = gate.collect_acceptance(REPO, f'https://github.com/{REPO}/pull/7')
    assert result['ok'], result
    assert result['checks'][0]['name'] == 'local-gate'
    assert result['checks'][0]['kind'] == 'commit_status'
    assert result['checks'][0]['creator_login'] == 'dannydeleteo'
    assert result['required_policy']['base_sha'] == BASE
    assert result['required_policy']['advisory_checks'] == POLICY['advisory_checks']
    assert not any('/check-runs?' in x for x in api.requests)


@pytest.mark.parametrize('fault', ['missing','check_run_only','pending','failure','error','wrong_creator','missing_creator',
    'wrong_head','wrong_url','forged_check_run','latest_failure','wrong_repo','wrong_kind','wrong_policy_creator',
    'ambiguous_app','empty_policy','v1_unpinned','bad_blob','head_change','base_change','default_move'])
def test_local_status_refuses_bad_provenance_results_policy_and_races(monkeypatch, fault):
    monkeypatch.setattr(gate, '_api', GitHub(fault))
    result = gate.collect_acceptance(REPO, f'https://github.com/{REPO}/pull/7')
    assert not result['ok'], (fault,result)
    if fault in ('head_change','base_change','default_move','wrong_head'):
        assert result['classification'] == 'stale', result


@pytest.mark.parametrize('fault,ok', [('server_check',True),('server_wrong_app',False)])
def test_real_server_requirements_are_never_downgraded(monkeypatch, fault, ok):
    monkeypatch.setattr(gate, '_api', GitHub(fault))
    result = gate.collect_acceptance(REPO, f'https://github.com/{REPO}/pull/7')
    assert result['ok'] is ok, result
    assert {x['name'] for x in result['checks']} == {'local-gate','python'}


def test_v2_does_not_enable_unpinned_policy_for_another_repository(monkeypatch):
    api = GitHub()
    monkeypatch.setattr(gate, '_api', lambda endpoint, **kw: api(endpoint.replace('acme/repo',REPO),**kw))
    result = gate.collect_acceptance('acme/repo','https://github.com/acme/repo/pull/7')
    assert not result['ok'] and result['classification'] == 'feature_unavailable'


def test_sqlite_completion_persists_truthful_status_and_retains_reclaimed_run_fence(monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect
    kb.init_db()
    api = GitHub(); monkeypatch.setattr(gate, '_api', api)
    with connect() as conn:
        tid = kb.create_task(conn,title='PM source',completion_contract=REPO)
        assert kb.complete_task(conn,tid,result='Local merge-result gate passed',metadata={'published_pr':f'https://github.com/{REPO}/pull/7'})
        assert kb.get_task(conn,tid).status == 'done'
        receipt=json.loads(conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'",(tid,)).fetchone()[0])
        assert receipt['ok'] and receipt['head_sha'] == HEAD and receipt['checks'][0]['kind'] == 'commit_status'
        tid = kb.create_task(conn,title='PM reclaimed source',completion_contract=REPO)
        run_id=kb.claim_task(conn,tid).current_run_id
        def reclaim():
            with connect() as rival:
                assert kb.block_task(rival,tid,reason='Synthetic reassignment')
                assert kb.unblock_task(rival,tid)
                assert kb.claim_task(rival,tid).current_run_id != run_id
        api.race=reclaim
        assert not kb.complete_task(conn,tid,result='Old worker completion',expected_run_id=run_id,metadata={'published_pr':f'https://github.com/{REPO}/pull/7'})
        assert kb.get_task(conn,tid).status != 'done'
        assert conn.execute("SELECT count(*) FROM task_events WHERE task_id=? AND kind='pr_acceptance'",(tid,)).fetchone()[0] == 0


def test_old_reader_new_parser_pair_refuses_v2_until_status_provenance_is_understood():
    from hermes_cli.kanban_pr_requirements import repository_policy
    # The installed old collector calls without the new capability keyword.
    # Promotion of the parser first must refuse rather than accidentally treat
    # the typed status as its old unpinned-context/check-run alternative.
    with pytest.raises(FeatureUnavailable):
        repository_policy(GitHub(),REPO,7,HEAD,'main',None)
    required, metadata = repository_policy(GitHub(),REPO,7,HEAD,'main',None,allow_commit_status=True)
    assert required == {('local-gate',None)} and metadata['commit_statuses'] == {'local-gate':'dannydeleteo'}
