"""Real installed MCP/Hook subprocess acceptance against synthetic data only."""
from pathlib import Path
from datetime import datetime, timezone
import argparse
import json
import os
import subprocess
import sys
import tempfile

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--report', type=Path, required=True)
parser.add_argument('--temp-root', type=Path, default=Path(__file__).resolve().parents[1] / '.tmp')
parser.add_argument('--serve', action='store_true', help='Keep the synthetic GUI open for browser acceptance; stop with Ctrl-C.')
options = parser.parse_args()
report = options.report
options.temp_root.mkdir(parents=True, exist_ok=True)
with tempfile.TemporaryDirectory(prefix='mg-offline-recovery-', dir=options.temp_root) as directory:
    root = Path(directory)
    # Never inherit the operator's production identity or canonical Data Home.
    for key in list(os.environ):
        if key.startswith('MEMORYGUARD_'):
            del os.environ[key]
    # A synthetic lifecycle must never trigger profile repair in the real app.
    os.environ.pop('CODEX_HOME', None)
    os.environ.update({
        'MEMORYGUARD_HOME': str(root), 'MEMORYGUARD_WORKSPACE': str(root),
        'MEMORYGUARD_AGENT_ID': 'offline-owner', 'MEMORYGUARD_PROVIDER': 'codex',
        'MEMORYGUARD_STRICT_BINDING': '1', 'MEMORYGUARD_ADMIN': '1',
        'MEMORYGUARD_SESSION_ID': 'offline-recovery', 'MEMORYGUARD_SESSION_SOURCE': 'transport',
        'MEMORYGUARD_SESSION_TRUSTED': '1', 'MEMORYGUARD_PROJECT_CWD': str(root),
        'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8',
    })
    import memoryguard
    from memoryguard.memory import MemoryAtomStore, MemoryAtom
    from memoryguard.evidence import EvidenceStore
    from memoryguard.governance_v2 import GovernanceV2, V2MutationContext
    from memoryguard.runtime_v2.group_native import GroupControlService
    from memoryguard.storage.schema import initialize_all
    from memoryguard.storage.layout import WorkspaceV2Layout
    from memoryguard.system.manifest import ManifestManager, ManifestState
    from memoryguard.mcp_client import MCPClient
    from memoryguard.runtime_lease import compute_code_fingerprint
    assert 'site-packages' in memoryguard.__file__
    initialize_all(WorkspaceV2Layout(root))
    memory, evidence = MemoryAtomStore(root), EvidenceStore(root)
    governance = GovernanceV2(root, memory_store=memory, evidence_store=evidence)
    manifest = ManifestManager(root)
    manifest.transition(ManifestState.V2_BUILDING, migration_id='offline-recovery')
    manifest.transition(ManifestState.V2_READY, source_digest='fixture', target_digest='fixture',
                        manifest_digest='fixture', digests={'validator_passed': True, 'checkpoints': {'mcp': True}})
    manifest.transition(ManifestState.V2_ACTIVE)
    GroupControlService(root, write=True).bind_agents(['offline-owner', 'offline-peer'], share_group_id='offline-team')
    context = V2MutationContext(workspace_id=str(root), share_group_id='offline-team',
                               agent_instance_id='offline-owner', project_ref=str(root), provider='codex',
                               actor='historical-test-fixture', admin=True)
    revisions = {}
    for name in ('alpha', 'bravo'):
        atom, receipt = governance.put_atom(MemoryAtom(
            memory_id=name, body=name + ' ' + name[0].upper() * 594, kind='procedure',
            injection_policy='always', workspace_id=str(root), share_group_id='offline-team',
            agent_instance_id='offline-owner', project_ref=str(root), provider='codex',
            metadata={'owner_agent_id': 'offline-owner', 'audience': {
                'source': 'native_v2', 'target_type': 'agent', 'target_id': 'offline-owner'}},
        ), context=context, evidence=[{'source_ref': 'fixture:' + name}], idempotency_key='seed:' + name)
        memory.project_evidence(evidence)
        memory.set_visibility('active', atom_ids=[atom.atom_id])
        revisions[name] = atom.revision

    def hook(event, payload, session='offline-recovery'):
        completed = subprocess.run([
            sys.executable, '-X', 'utf8', '-m', 'memoryguard.host_hooks', 'run',
            '--provider', 'codex', '--event', event, '--workspace', str(root),
            '--agent-id', 'offline-owner', '--share-group-id', 'offline-team',
            '--managed-by', 'memoryguard',
        ], input=json.dumps({'session_id': session, 'cwd': str(root), **payload}),
            capture_output=True, text=True, encoding='utf-8', timeout=30)
        assert completed.returncode == 0, (completed.returncode, completed.stderr)
        return json.loads(completed.stdout)

    def decode(result):
        return json.loads(next(item['text'] for item in result['content'] if item['type'] == 'text'))

    evidence_result = {'at': datetime.now(timezone.utc).isoformat(), 'synthetic_only': True,
                       'installed_package': memoryguard.__file__, 'fingerprint': compute_code_fingerprint()}
    with MCPClient([sys.executable], ['-X', 'utf8', '-m', 'memoryguard.mcp_server'], timeout=30) as client:
        evidence_result['initialize'] = client.initialize()['serverInfo']
        listed = {item['name']: item for item in client.list_tools()}
        assert 'preview' in listed['memoryguard_memory_update']['inputSchema']['properties']
        blocked = decode(client.call_tool('memoryguard_context_bootstrap', {'task': 'offline recovery verification'}))
        assert not blocked['ok'] and blocked.get('code', blocked.get('error')) == 'mandatory_budget_exceeded', blocked
        evidence_result['bootstrap_before'] = 'mandatory_budget_exceeded'
        hook('user_prompt', {'prompt': 'offline recovery verification'})
        ordinary = {'tool_name': 'Read', 'tool_input': {'file_path': str(root / 'fixture.txt')}}
        denied = hook('pre_tool', ordinary)
        assert denied.get('hookSpecificOutput', {}).get('permissionDecision') == 'deny', denied
        evidence_result['ordinary_tool_before'] = 'deny'
        request = {
            'memory_id': 'alpha', 'body': 'Always verify alpha release approval.', 'expected_revision': revisions['alpha'],
            'related_updates': [{'memory_id': 'bravo', 'body': 'Always verify bravo release approval.',
                                 'expected_revision': revisions['bravo']}],
            'reason': 'synthetic historical overflow recovery', 'idempotency_key': 'offline-repair', 'preview': True,
        }
        update_tool = {'tool_name': 'mcp__memoryguard__memoryguard_memory_update', 'tool_input': request}
        assert hook('pre_tool', update_tool) == {}
        preview = decode(client.call_tool('memoryguard_memory_update', request))
        assert preview['ok'] and preview['data']['committed'] is False, preview
        evidence_result['preview_committed'] = False
        request['preview'] = False
        applied_result = client.call_tool('memoryguard_memory_update', request)
        applied = decode(applied_result)
        assert applied['ok'] and applied['data']['committed'] is True, applied
        evidence_result['update_committed'] = True
        # Deliver the real MCP responses through the normal Hook post-tool path.
        hook('post_tool', {**update_tool, 'tool_result': applied_result})
        still_denied = hook('pre_tool', ordinary)
        assert still_denied.get('hookSpecificOutput', {}).get('permissionDecision') == 'deny', still_denied
        evidence_result['update_alone_clears_gate'] = False
        bootstrap_tool = {'tool_name': 'mcp__memoryguard__memoryguard_context_bootstrap',
                          'tool_input': {'task': 'offline recovery verification'}}
        assert hook('pre_tool', bootstrap_tool) == {}
        recovered_result = client.call_tool('memoryguard_context_bootstrap', bootstrap_tool['tool_input'])
        recovered = decode(recovered_result)
        assert recovered['ok'], recovered
        hook('post_tool', {**bootstrap_tool, 'tool_result': recovered_result})
        final_gate = hook('pre_tool', ordinary)
        assert final_gate == {}, {'gate': final_gate, 'bootstrap': recovered}
        evidence_result['bootstrap_after'] = 'ok'
        evidence_result['ordinary_tool_after'] = 'allow'
        status = decode(client.call_tool('memoryguard_memory_status', {}))
        assert status['ok'], status
        evidence_result['memory_status_after'] = 'ok'
        evidence_result['budget_after'] = recovered.get('data', {}).get('budget')
    # The desktop's actual SafeBridge mutation must change the canonical
    # injected body and allow two independently blocked sessions to recover.
    from memoryguard.access_context import AccessContext
    from memoryguard.gui import SafeBridgeApi
    from memoryguard.rules.v2_store import RuleV2Store
    from memoryguard.rule_definition import build_definition
    from memoryguard.rule_binding import build_binding
    from memoryguard.rule_reconciliation import settle_native_canonical_snapshot
    rules = RuleV2Store(root)
    definition = rules.upsert_definition(build_definition('Overflow ' + 'Z' * 1050, kind='procedure', rule_strength='must'))
    did = definition.definition_id
    rules.upsert_binding(build_binding(did, share_group_id='offline-team', target_type='group', target_id='offline-team', owner_agent_id='offline-owner'))
    rules.upsert_source_link(source_kind='fixture', share_group_id='offline-team', memory_id='canonical-source',
                            source_ref='fixture:canonical', original_definition_id=did, canonical_definition_id=did, status='active')
    rules.record_evidence_ref({'evidence_id':'canonical-evidence', 'definition_id':did, 'source_rule_id':'canonical-source',
                              'share_group_id':'offline-team', 'evidence_ref':'fixture:canonical', 'content_digest':definition.semantic_hash})
    settle_native_canonical_snapshot(root, 'offline-team', store=rules)
    sessions = ['gui-recovery-a', 'gui-recovery-b']
    for session in sessions:
        hook('user_prompt', {'prompt':'synthetic canonical overflow'}, session)
        assert hook('pre_tool', ordinary, session).get('hookSpecificOutput', {}).get('permissionDecision') == 'deny'
    gui = SafeBridgeApi(str(root), direct_mutations=True, _trusted_access_context=AccessContext(
        trusted_agent_id='offline-owner', is_admin=True, strict_binding=True, allow_anon=False,
        session_id='synthetic-local-gui', session_source='transport', session_trusted=True))
    saved = gui.request_mutation('update_rule_body', [did, 'Use the reviewed release checklist.', 1, 'offline-team'])
    assert saved.get('ok') is not False and not saved.get('error'), saved
    assert rules.get_definition(did).canonical_text == 'Use the reviewed release checklist.'
    evidence_result['gui_canonical_save'] = {'definition_revision':rules.get_definition(did).revision, 'bridge_result':saved}
    evidence_result['gui_sessions'] = []
    for session in sessions:
        assert hook('pre_tool', ordinary, session).get('hookSpecificOutput', {}).get('permissionDecision') == 'deny'
        os.environ['MEMORYGUARD_SESSION_ID'] = session
        with MCPClient([sys.executable], ['-X','utf8','-m','memoryguard.mcp_server'], timeout=30) as client:
            client.initialize()
            assert hook('pre_tool', bootstrap_tool, session) == {}
            response = client.call_tool('memoryguard_context_bootstrap', bootstrap_tool['tool_input'])
            decoded = decode(response)
            assert decoded['ok'], decoded
            hook('post_tool', {**bootstrap_tool, 'tool_result':response}, session)
            assert hook('pre_tool', ordinary, session) == {}
            evidence_result['gui_sessions'].append({'session':session, 'bootstrap':'ok', 'ordinary_tool':'allow'})
    report.write_text(json.dumps(evidence_result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(evidence_result, ensure_ascii=False, indent=2))
    if options.serve:
        from memoryguard.desktop_executor import SERVER_ADMIN_AGENT_ID
        from memoryguard.gui import open_localhost_window
        GroupControlService(root, write=True).set_scope(
            SERVER_ADMIN_AGENT_ID,
            {'mode': 'share_group', 'share_group_id': 'offline-team'},
            admin=True,
        )
        desktop = SafeBridgeApi(str(root), direct_mutations=True, _trusted_access_context=AccessContext(
            trusted_agent_id=SERVER_ADMIN_AGENT_ID, is_admin=True, strict_binding=True, allow_anon=False,
            session_id='synthetic-localhost-admin', session_source='transport', session_trusted=True))
        listed = desktop.call_readonly('list_rules_habits', ['offline-team'])
        assert listed.get('ok') is not False and not listed.get('error'), listed
        open_localhost_window(str(root), auto_open=False)
