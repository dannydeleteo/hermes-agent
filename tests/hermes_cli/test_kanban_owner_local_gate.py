"""Owner namespace policy extends the same strict typed evidence to every project."""
import base64
import copy
import hashlib
import json

import pytest

from hermes_cli import kanban_pr_acceptance as gate
from hermes_cli.kanban_pr_requirements import POLICY_PATH
from tests.hermes_cli.test_kanban_pm_local_gate import GitHub, POLICY, REPO as PM, HEAD, BASE


class ProjectGitHub(GitHub):
    def __init__(self, repo, *, branch='main', **kwargs):
        super().__init__(**kwargs)
        self.repo, self.branch = repo, branch

    def __call__(self, endpoint, **kwargs):
        if '/contents/' in endpoint:
            self.requests.append(endpoint)
            assert endpoint.endswith('?ref=' + BASE)
            policy = copy.deepcopy(POLICY)
            policy.update(repository=self.repo, base_branch=self.branch)
            if self.fault == 'wrong_repo': policy['repository'] = PM
            if self.fault == 'wrong_policy_creator': policy['required_checks'][0]['creator_login'] = 'other'
            raw = json.dumps(policy).encode()
            blob = hashlib.sha1(f'blob {len(raw)}\0'.encode()+raw).hexdigest()
            return {'type':'file','encoding':'base64','path':POLICY_PATH,'size':len(raw),
                    'sha': '0'*40 if self.fault == 'bad_blob' else blob,
                    'content':base64.b64encode(raw).decode()}
        value = super().__call__(endpoint.replace(self.repo, PM).replace(self.branch, 'main'), **kwargs)
        # This is transport contract data, not a patched host/runtime identity.
        return json.loads(json.dumps(value).replace(PM, self.repo)
                          .replace('"main"', json.dumps(self.branch))
                          .replace('refs/heads/main', 'refs/heads/'+self.branch))


@pytest.mark.parametrize('readable_rules', [False, True])
def test_reviewed_owned_project_policy_uses_exact_status_even_when_rules_readable(monkeypatch, readable_rules):
    repo='dannydeleteo/agent-bridge'
    api=ProjectGitHub(repo, readable_rules=readable_rules)
    monkeypatch.setattr(gate, '_api', api)
    result=gate.collect_acceptance(repo, f'https://github.com/{repo}/pull/7')
    assert result['ok'], result
    assert result['required_policy']['base_sha']==BASE
    assert result['checks'][0]['kind']=='commit_status'
    assert result['checks'][0]['creator_login']=='dannydeleteo'
    assert not any('/check-runs?' in e for e in api.requests)


@pytest.mark.parametrize('repo', [
    'agent-bridge', 'night-watch', 'greekfire-brain', 'youtube-creator',
    'greekfire-investments', 'kdp-publishing', 'greekfire-factset', 'woodshed',
    'hermes-agent', 'powerpermit', 'frontier-discovery', 'greekfire-quant',
    '13fagents', 'Greekfire-bug-mitigation', 'greekfire-finance',
    'greekfire-real-estate', 'future-owned-project',
])
def test_each_owned_project_requires_its_own_accepted_policy(monkeypatch, repo):
    repo='dannydeleteo/'+repo
    monkeypatch.setattr(gate, '_api', ProjectGitHub(repo, readable_rules=True))
    result=gate.collect_acceptance(repo, f'https://github.com/{repo}/pull/7')
    assert result['ok'], result
    assert result['required_policy']['commit_statuses']=={'local-gate':'dannydeleteo'}
    assert result['head_sha']==HEAD


@pytest.mark.parametrize('fault', [
    'missing', 'check_run_only', 'pending', 'failure', 'error', 'wrong_creator',
    'missing_creator', 'wrong_head', 'wrong_url', 'forged_check_run',
    'latest_failure', 'wrong_repo', 'wrong_policy_creator', 'bad_blob',
    'head_change', 'base_change', 'default_move',
])
@pytest.mark.parametrize('readable_rules', [False, True])
def test_all_project_extension_keeps_provenance_integrity_and_race_refusals(monkeypatch, fault, readable_rules):
    repo='dannydeleteo/agent-bridge'
    monkeypatch.setattr(gate, '_api', ProjectGitHub(repo, fault=fault, readable_rules=readable_rules))
    result=gate.collect_acceptance(repo, f'https://github.com/{repo}/pull/7')
    assert not result['ok'], (fault,result)


def test_owner_default_branch_need_not_be_named_main(monkeypatch):
    repo='dannydeleteo/13fagents'
    api=ProjectGitHub(repo, branch='release/qualified', readable_rules=True)
    monkeypatch.setattr(gate, '_api', api)
    result=gate.collect_acceptance(repo, f'https://github.com/{repo}/pull/7')
    assert result['ok'], result


@pytest.mark.parametrize('fault,ok', [('server_check', True), ('server_wrong_app', False)])
def test_owner_local_policy_cannot_downgrade_real_hosted_protection(monkeypatch, fault, ok):
    repo='dannydeleteo/agent-bridge'
    monkeypatch.setattr(gate, '_api', ProjectGitHub(repo, fault=fault, readable_rules=True))
    result=gate.collect_acceptance(repo, f'https://github.com/{repo}/pull/7')
    assert result['ok'] is ok, result
    assert {x['name'] for x in result['checks']}=={'local-gate','python'}


def test_nondefault_stacked_target_keeps_real_server_gate_without_author_policy(monkeypatch):
    repo='dannydeleteo/agent-bridge'
    api=ProjectGitHub(repo, branch='stacked/base', fault='server_check', readable_rules=True)
    def routed(endpoint, **kwargs):
        if endpoint==f'repos/{repo}':
            return {'default_branch':'main'}
        return api(endpoint, **kwargs)
    monkeypatch.setattr(gate, '_api', routed)
    result=gate.collect_acceptance(repo, f'https://github.com/{repo}/pull/7')
    assert result['ok'], result
    assert {x['name'] for x in result['checks']}=={'python'}
    assert not any('/contents/' in e for e in api.requests)


def test_missing_owner_policy_does_not_erase_genuine_server_checks(monkeypatch):
    repo='dannydeleteo/agent-bridge'
    api=ProjectGitHub(repo, fault='server_check', readable_rules=True)
    def missing(endpoint, **kwargs):
        if '/contents/' in endpoint:
            raise gate._GateAuthError('Synthetic missing accepted file', http_status=404)
        return api(endpoint, **kwargs)
    monkeypatch.setattr(gate, '_api', missing)
    result=gate.collect_acceptance(repo, f'https://github.com/{repo}/pull/7')
    assert result['ok'], result
    assert {x['name'] for x in result['checks']}=={'python'}
    assert 'required_policy' not in result


@pytest.mark.parametrize('movement', [None, 'commit', 'default_branch'])
def test_missing_optional_policy_retains_default_pin_until_after_status_collection(monkeypatch, movement):
    repo = 'dannydeleteo/agent-bridge'
    api = ProjectGitHub(repo, fault='server_check', readable_rules=True)
    collected_statuses = False

    def missing_then_move(endpoint, **kwargs):
        nonlocal collected_statuses
        if '/contents/' in endpoint:
            raise gate._GateAuthError('Synthetic missing accepted file', http_status=404)
        if '/statuses?' in endpoint:
            collected_statuses = True
        value = api(endpoint, **kwargs)
        if collected_statuses and movement == 'commit' and '/git/ref/heads/' in endpoint:
            value['object']['sha'] = 'e' * 40  # This accepted commit adds the local-gate policy.
        if collected_statuses and movement == 'default_branch' and endpoint == f'repos/{repo}':
            value['default_branch'] = 'new-default'
        return value

    monkeypatch.setattr(gate, '_api', missing_then_move)
    result = gate.collect_acceptance(repo, f'https://github.com/{repo}/pull/7')
    assert collected_statuses
    assert result['ok'] is (movement is None), result
    if movement is None:
        assert {check['name'] for check in result['checks']} == {'python'}
        assert 'required_policy' not in result
    else:
        assert result['classification'] in {'stale', 'feature_unavailable'}, result


def test_no_declaration_no_server_checks_is_never_a_pass(monkeypatch):
    repo='dannydeleteo/agent-bridge'
    api=ProjectGitHub(repo, readable_rules=True)
    def missing(endpoint, **kwargs):
        if '/contents/' in endpoint:
            raise gate._GateAuthError('Synthetic missing accepted file', http_status=404)
        return api(endpoint, **kwargs)
    monkeypatch.setattr(gate, '_api', missing)
    result=gate.collect_acceptance(repo, f'https://github.com/{repo}/pull/7')
    assert not result['ok'] and result['classification']=='feature_unavailable'


def test_real_sqlite_owner_completion_retains_exact_evidence_and_reclaim_fence(monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect
    repo='dannydeleteo/agent-bridge'; api=ProjectGitHub(repo, readable_rules=True)
    monkeypatch.setattr(gate, '_api', api)
    kb.init_db()
    with connect() as con:
        task=kb.create_task(con,title='Synthetic owned project',completion_contract=repo)
        assert kb.complete_task(con,task,result='Synthetic genuine local evidence',metadata={'published_pr':f'https://github.com/{repo}/pull/7'})
        saved=json.loads(con.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'",(task,)).fetchone()[0])
        assert saved['head_sha']==HEAD and saved['required_policy']['base_sha']==BASE
        task=kb.create_task(con,title='Synthetic replacement fence',completion_contract=repo)
        claimed=kb.claim_task(con,task)
        assert claimed is not None
        run=claimed.current_run_id
        def reclaim():
            with connect() as other:
                assert kb.block_task(other,task,reason='Synthetic replacement')
                assert kb.unblock_task(other,task)
                replacement=kb.claim_task(other,task)
                assert replacement is not None and replacement.current_run_id!=run
        monkeypatch.setattr(api,'race',reclaim)
        assert not kb.complete_task(con,task,result='Stale worker',expected_run_id=run,metadata={'published_pr':f'https://github.com/{repo}/pull/7'})
        current=kb.get_task(con,task)
        assert current is not None and current.status!='done'
        assert con.execute("SELECT count(*) FROM task_events WHERE task_id=? AND kind='pr_acceptance'",(task,)).fetchone()[0]==0
