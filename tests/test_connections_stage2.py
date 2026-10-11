"""Regressions for route lifecycle, preserved credentials and launch readiness."""
import asyncio
import json
import os
import time
from pathlib import Path

import pytest

from test_connections import request


def configure(t, **defaults):
    for tool, name in defaults.items():
        t.CFG['agents'][tool]['proxy'] = name
    t.save_config(t.CFG)


def mutate(t, data):
    return t.connections_mutate(json.dumps(data).encode())


def details(t, name='eu-1'):
    return t.connection_details(name)


def test_legacy_delete_assigned_route_is_rejected_without_writing(t):
    configure(t, claude='eu-1', codex='eu-1')
    before = Path(t.CONFIG_FILE).read_bytes()
    result = t.settings_save(json.dumps({'proxies':[t.CFG['proxies'][1]]}).encode())
    assert result != 'ok'
    assert Path(t.CONFIG_FILE).read_bytes() == before
    assert t.agent_command('claude','',False) is not None


def test_legacy_rename_updates_all_agent_assignments(t):
    configure(t, claude='eu-1', codex='eu-1', pi='eu-1', omp='us-1')
    shown = t.settings_state()['config']['proxies']
    shown[0].update(name='renamed', orig='eu-1')
    assert t.settings_save(json.dumps({'proxies':shown}).encode()) == 'ok'
    assert [t.CFG['agents'][a]['proxy'] for a in ('claude','codex','pi','omp')] == ['renamed','renamed','renamed','us-1']
    assert '203.0.113.10:3128' in t.agent_command('codex','',False)


def test_proxy_details_never_return_current_password(t):
    configure(t, claude='eu-1', pi='eu-1')
    d = details(t)
    assert d['ok'] and d['proxy']['host'] == '203.0.113.10'
    assert d['proxy']['username'] == 'user'
    assert d['proxy']['password_set'] is True
    assert d['used_by'] == ['claude','pi']
    assert not any(secret in json.dumps(d) for secret in ('s3cret','http://user:','pa55'))


def test_edit_preserves_password_and_renames_assignments_atomically(t):
    configure(t, claude='eu-1', codex='eu-1')
    d = details(t)
    result = mutate(t, {'action':'edit','name':'eu-1','revision':d['revision'],
                       'proxy':{'name':'renamed','scheme':'https','host':'new.example','port':8443,
                                'username':'changed','password_action':'keep'}})
    assert result['ok']
    assert t.CFG['proxies'][0] == {'name':'renamed','url':'https://changed:s3cret@new.example:8443'}
    assert t.CFG['agents']['claude']['proxy'] == t.CFG['agents']['codex']['proxy'] == 'renamed'
    assert 's3cret' not in json.dumps(result)


@pytest.mark.parametrize('action,password,url', [('replace','new:@ /','http://user:new%3A%40%20%2F@203.0.113.10:3128'),
                                                ('remove','','http://user:@203.0.113.10:3128')])
def test_edit_password_requires_explicit_action(t, action, password, url):
    d = details(t)
    result = mutate(t, {'action':'edit','name':'eu-1','revision':d['revision'],
                       'proxy':{'name':'eu-1','scheme':'http','host':'203.0.113.10','port':3128,
                                'username':'user','password_action':action,'password':password}})
    assert result['ok'] and t.CFG['proxies'][0]['url'] == url


def test_duplicate_copies_credentials_without_exposing_them(t):
    d = details(t)
    assert mutate(t, {'action':'duplicate','name':'eu-1','revision':d['revision'],'new_name':'copy'})['ok']
    assert t.CFG['proxies'][-1] == {'name':'copy','url':'http://user:s3cret@203.0.113.10:3128'}
    assert t.CFG['agents']['claude'].get('proxy','') == ''


@pytest.mark.parametrize('replacements', [{}, {'claude':'missing','codex':'direct'}, {'claude':'direct'},
                                         {'claude':'eu-1','codex':'direct'}, {'claude':'direct','codex':'direct','pi':'us-1'}])
def test_delete_requires_valid_replacement_for_exactly_affected_agents(t, replacements):
    configure(t, claude='eu-1', codex='eu-1')
    before = Path(t.CONFIG_FILE).read_bytes()
    d = details(t)
    result = mutate(t, {'action':'delete','name':'eu-1','revision':d['revision'],'replacements':replacements})
    assert not result['ok']
    assert Path(t.CONFIG_FILE).read_bytes() == before


def test_delete_reassigns_agents_and_keeps_launchable_commands(t):
    configure(t, claude='eu-1', codex='eu-1', pi='eu-1')
    d = details(t)
    assert mutate(t, {'action':'delete','name':'eu-1','revision':d['revision'],
                      'replacements':{'claude':'direct','codex':'us-1','pi':'us-1'}})['ok']
    assert [p['name'] for p in t.CFG['proxies']] == ['us-1']
    assert t.agent_command('claude','',False).startswith('env -u HTTPS_PROXY')
    assert '198.51.100.7:8080' in t.agent_command('codex','',False)
    assert t.CFG['agents']['pi']['proxy'] == 'us-1'


def test_stale_editor_cannot_overwrite_changed_proxy(t):
    d = details(t)
    t.CFG['proxies'][0]['url'] = 'http://user:rotated@203.0.113.10:3128'
    t.save_config(t.CFG)
    before = Path(t.CONFIG_FILE).read_bytes()
    assert not mutate(t, {'action':'delete','name':'eu-1','revision':d['revision']})['ok']
    assert Path(t.CONFIG_FILE).read_bytes() == before


def test_failed_config_write_keeps_memory_and_file_unchanged(t, monkeypatch):
    d = details(t)
    before = Path(t.CONFIG_FILE).read_bytes()
    def fail(cfg):
        raise OSError('write failed')
    monkeypatch.setattr(t,'save_config',fail)
    assert not mutate(t, {'action':'duplicate','name':'eu-1','revision':d['revision'],'new_name':'copy'})['ok']
    assert Path(t.CONFIG_FILE).read_bytes() == before
    assert len(t.CFG['proxies']) == 2


def test_readiness_finds_cli_in_actual_login_shell_without_running_agents(t, tmp_path, monkeypatch):
    # Real shell resolves a temporary executable; CLI is never invoked.
    marker = tmp_path/'agent-was-run'
    cli = tmp_path/'claude'
    cli.write_text('#!/bin/sh\ntouch '+str(marker)+'\n')
    cli.chmod(0o700)
    shell = tmp_path/'login-shell'
    shell.write_text('#!/bin/sh\nexport PATH='+str(tmp_path)+':/usr/bin:/bin\nexec /bin/sh -c "$2"\n')
    shell.chmod(0o700)
    monkeypatch.setenv('SHELL',str(shell))
    found = asyncio.run(t.probe_launch_clis())
    assert found['claude'] is True and found['codex'] is False
    assert not marker.exists()


def test_readiness_separates_cli_iterm_and_directory(t, tmp_path, monkeypatch):
    t.CONN.clear()
    t.STATE['cwd'] = str(tmp_path)
    t.CACHE['launch:cli'] = (time.monotonic(), {'claude':True,'codex':False,'error':''})
    state = asyncio.run(t.connections_state())
    assert state['readiness']['iterm'] is False
    assert state['readiness']['directory'] is True
    assert state['readiness']['cli']['claude'] is True
    assert state['readiness']['cli']['codex'] is False
    monkeypatch.setitem(t.CONN,'c',object())
    t.STATE['cwd'] = str(tmp_path/'missing')
    state = asyncio.run(t.connections_state())
    assert state['readiness']['iterm'] is True and state['readiness']['directory'] is False


@pytest.mark.parametrize('path', ['/connections/mutate','/connections/readiness','/connections/launch'])
def test_new_connection_actions_require_post_and_guard_header(t, path):
    assert asyncio.run(request(t,'POST',path,header=False))[0] == 403
    assert asyncio.run(request(t,'GET',path))[0] == 403


def test_launch_refuses_missing_cli_before_iterm_rpc(t, tmp_path, monkeypatch):
    monkeypatch.setitem(t.CONN,'c',object())
    t.CACHE['launch:cli'] = (time.monotonic(), {'claude':False,'codex':False,'error':''})
    result = asyncio.run(t.connections_launch(json.dumps({'tool':'claude','proxy':'direct','dir':str(tmp_path)}).encode()))
    assert not result['ok'] and 'CLI' in result['error']


def test_launch_uses_explicit_route_directory_and_server_launch_settings(t, tmp_path, monkeypatch):
    monkeypatch.setitem(t.CONN,'c',object())
    t.CACHE['launch:cli'] = (time.monotonic(), {'claude':True,'codex':True,'error':''})
    t.CFG['open_in'] = 'tab'
    captured = {}
    async def external_iterm(q):
        captured.update(q)
        return 'ok'
    monkeypatch.setattr(t,'open_agent',external_iterm)
    result = asyncio.run(t.connections_launch(json.dumps({'tool':'codex','proxy':'us-1','dir':str(tmp_path),'where':'window'}).encode()))
    assert result['ok']
    assert captured == {'tool':'codex','proxy':'us-1','dir':str(tmp_path),'where':'tab'}


def test_details_and_mutation_http_roundtrip(t):
    code, payload = asyncio.run(request(t,'GET','/connections/details?name=eu-1'))
    assert code == 200
    d = json.loads(payload)
    code, payload = asyncio.run(request(t,'POST','/connections/mutate',{'action':'duplicate','name':'eu-1','revision':d['revision'],'new_name':'copy'}))
    assert code == 200 and json.loads(payload)['ok']
    assert 's3cret' not in payload.decode()


def test_cancelled_readiness_probe_stops_its_shell(t, tmp_path, monkeypatch):
    import signal
    pidfile = tmp_path/'probe.pid'
    shell = tmp_path/'slow-shell'
    shell.write_text('#!/bin/sh\necho $$ > '+str(pidfile)+'\nsleep 20\n')
    shell.chmod(0o700)
    monkeypatch.setenv('SHELL',str(shell))
    async def scenario():
        task = asyncio.create_task(t.probe_launch_clis())
        for _ in range(100):
            if pidfile.exists():
                break
            await asyncio.sleep(0.01)
        pid = int(pidfile.read_text())
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        try:
            os.kill(pid,0)
        except ProcessLookupError:
            return
        os.kill(pid,signal.SIGKILL)
        pytest.fail('Readiness check left its shell alive after cancellation')
    asyncio.run(scenario())


@pytest.mark.parametrize('origin', [[], {}, 42])
def test_legacy_invalid_proxy_origin_is_rejected_without_writing(t, origin):
    before = Path(t.CONFIG_FILE).read_bytes()
    proxies = t.settings_state()['config']['proxies']
    proxies[0]['orig'] = origin
    assert t.settings_save(json.dumps({'proxies':proxies}).encode()) != 'ok'
    assert Path(t.CONFIG_FILE).read_bytes() == before


def test_invalid_legacy_assignment_returns_validation_error(t):
    before = Path(t.CONFIG_FILE).read_bytes()
    payload = {'agents':{'claude':{'proxy':[]}}, 'proxies':t.settings_state()['config']['proxies']}
    assert t.settings_save(json.dumps(payload).encode()) != 'ok'
    assert Path(t.CONFIG_FILE).read_bytes() == before


@pytest.mark.parametrize('failure', ['directory','iterm','route'])
def test_launch_rejects_invalid_readiness_without_dispatch(t, tmp_path, monkeypatch, failure):
    monkeypatch.setitem(t.CONN,'c',object())
    t.CACHE['launch:cli'] = (time.monotonic(), {'claude':True,'codex':True,'error':''})
    payload = {'tool':'claude','proxy':'direct','dir':str(tmp_path)}
    if failure == 'directory':
        payload['dir'] = str(tmp_path/'missing')
    elif failure == 'iterm':
        t.CONN.clear()
    else:
        payload['proxy'] = 'deleted'
    dispatched = []
    async def external_iterm(q):
        dispatched.append(q)
        return 'ok'
    monkeypatch.setattr(t,'open_agent',external_iterm)
    result = asyncio.run(t.connections_launch(json.dumps(payload).encode()))
    assert not result['ok'] and result['error']
    assert dispatched == []


def test_launch_rechecks_expired_cli_cache(t, tmp_path, monkeypatch):
    monkeypatch.setitem(t.CONN,'c',object())
    t.CACHE['launch:cli'] = (0, {'claude':True,'codex':True,'error':''})
    async def cli_removed():
        return {'claude':False,'codex':True,'error':''}
    monkeypatch.setattr(t,'probe_launch_clis',cli_removed)
    result = asyncio.run(t.connections_launch(json.dumps({'tool':'claude','proxy':'direct','dir':str(tmp_path)}).encode()))
    assert not result['ok'] and 'CLI' in result['error']
    assert t.launch_readiness()['cli']['claude'] is False
