#!/usr/bin/env python3
import base64
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
import time
from datetime import datetime, timezone

CONFIG = Path('/etc/lastsietch/portal-game-auth.json')
RPC = '/opt/rabbitmq/sbin/rabbitmqctl'


def run(args, data=None, timeout=20):
    result = subprocess.run(args, input=data, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise ValueError('transport_unavailable')
    return result.stdout


def settings():
    info = CONFIG.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise ValueError('configuration_unavailable')
    value = json.loads(CONFIG.read_text())
    if value.get('enabled') is not True:
        raise ValueError('verification_paused')
    for key in ('namespace', 'pod'):
        if re.fullmatch('[a-z0-9][a-z0-9-]{1,100}', value.get(key, '')) is None:
            raise ValueError('configuration_unavailable')
    if type(value.get('sender_account')) is not int or value['sender_account'] <= 0:
        raise ValueError('configuration_unavailable')
    accounts = value.get('allowed_accounts')
    if accounts is not None and (not isinstance(accounts, list) or not 1 <= len(accounts) <= 16
            or any(type(account) is not int or not 0 < account < 2**63 for account in accounts)
            or len(set(accounts)) != len(accounts)):
        raise ValueError('configuration_unavailable')
    deadline = value.get('expires_at')
    if deadline is not None and (type(deadline) not in (int, float) or not math.isfinite(deadline)
            or time.time() >= deadline):
        raise ValueError('verification_paused')
    return value


def database(sql):
    raw = run(['/root/dq.sh', '-qtAX'], 'BEGIN READ ONLY;\n' + sql + '\nROLLBACK;\n')
    return json.loads(raw)


def lookup(character=None, account_id=None):
    if account_id is not None:
        if type(account_id) is not int or not 0 < account_id < 2**63:
            raise ValueError('character_unavailable')
        predicate = 'a.id=' + str(account_id)
    else:
        if not isinstance(character, str) or not 1 <= len(character.strip()) <= 80:
            raise ValueError('character_unavailable')
        encoded = character.strip().encode().hex()
        predicate = "lower(ps.character_name)=lower(convert_from(decode('" + encoded + "','hex'),'UTF8'))"
    rows = database("SELECT coalesce(json_agg(t),'[]'::json) FROM (SELECT a.id AS account_id, "
        "ps.character_name,a.\"user\" AS principal,a.funcom_id FROM dune.player_state ps "
        "JOIN dune.accounts a ON a.id=ps.account_id WHERE ps.online_status='Online' AND " + predicate + " LIMIT 2) t;")
    if len(rows) != 1:
        raise ValueError('character_unavailable')
    row = rows[0]
    if not isinstance(row.get('principal'), str) or not row['principal'] or not isinstance(row.get('funcom_id'), str):
        raise ValueError('character_unavailable')
    return row


def broker(config, *args):
    return run(['kubectl', 'exec', '-n', config['namespace'], config['pod'], '--', RPC, *args])


def encoded(value):
    return 'base64:decode(<<"' + base64.b64encode(value.encode()).decode() + '">>)'


def native_queue_guard(principal):
    if not isinstance(principal, str) or not re.fullmatch('[A-Za-z0-9_-]{1,128}', principal):
        raise ValueError('private_delivery_unavailable')
    foreign = secrets.token_hex(8)
    while foreign == principal:
        foreign = secrets.token_hex(8)
    return f'''
{{ok,"3.13.7"}} = application:get_key(rabbit,vsn),
{{ok,[rabbit_auth_backend_cache]}} = application:get_env(rabbit,auth_backends),
{{ok,rabbit_auth_backend_http}} = application:get_env(rabbitmq_auth_backend_cache,cached_backend),
false = rabbit_trace:enabled(<<"/">>),
Principal = {encoded(principal)},
QName = rabbit_misc:r(<<"/">>,queue,{encoded(principal + '_queue')}),
{{ok,Q}} = rabbit_amqqueue:lookup(QName),
rabbit_classic_queue = amqqueue:get_type(Q),
[{{<<"x-expires">>,signedint,1800000}}] = amqqueue:get_arguments(Q),
undefined = rabbit_policy:get(<<"dead-letter-exchange">>,Q),
QPid = amqqueue:get_pid(Q), true = is_pid(QPid),
[{{Ch,Tag,false,_,true,_,[],Principal}}] = rabbit_amqqueue:consumers(Q),
true = is_pid(Ch), true = is_binary(Tag), true = (byte_size(Tag) > 0),
Info = rabbit_channel:info(Ch,[connection,user,vhost,state]),
true = (proplists:get_value(user,Info) =:= Principal),
true = (proplists:get_value(vhost,Info) =:= <<"/">>),
running = proplists:get_value(state,Info),
Conn = proplists:get_value(connection,Info), true = is_pid(Conn),
Reader = rabbit_reader:info(Conn,[user,vhost,state]),
true = (proplists:get_value(user,Reader) =:= Principal),
true = (proplists:get_value(vhost,Reader) =:= <<"/">>),
running = proplists:get_value(state,Reader),
GS = sys:get_state(Ch,2000), true = (element(1,GS) =:= gs2_state),
rabbit_channel = element(5,GS), S = element(4,GS),
true = (element(1,S) =:= ch), true = (tuple_size(S) =:= 21),
C = element(2,S), true = (element(1,C) =:= conf), true = (tuple_size(C) =:= 22),
User = rabbit_channel:get_user(S), true = (element(10,C) =:= User),
true = (element(2,User) =:= Principal), true = (element(11,C) =:= <<"/">>),
Context = element(19,C), true = is_map(Context),
ok = rabbit_access_control:check_resource_access(User,QName,read,Context),
Other = rabbit_misc:r(<<"/">>,queue,{encoded(foreign + '_queue')}),
denied = try rabbit_access_control:check_resource_access(User,Other,read,Context), allowed
         catch exit:{{amqp_error,access_refused,_,_}} -> denied end,
Proof = #{{queue => {encoded(principal + '_queue')},
          queue_pid => list_to_binary(pid_to_list(QPid)),
          channel_pid => list_to_binary(pid_to_list(Ch)),
          connection_pid => list_to_binary(pid_to_list(Conn)), consumer_tag => Tag}},
'''


def private_queue(config, principal):
    expr = ('try ' + native_queue_guard(principal)
            + 'io:format("~s~n",[rabbit_json:encode(Proof)]), verified catch _:_ -> refused end.')
    lines = broker(config, 'eval', expr).strip().splitlines()
    if len(lines) != 2 or lines[1] != 'verified':
        raise ValueError('private_delivery_unavailable')
    try:
        proof = json.loads(lines[0])
    except (ValueError, TypeError):
        raise ValueError('private_delivery_unavailable') from None
    if (not isinstance(proof, dict)
            or set(proof) != {'queue', 'queue_pid', 'channel_pid', 'connection_pid', 'consumer_tag'}
            or proof['queue'] != principal + '_queue'
            or any(not isinstance(proof[key], str) or not re.fullmatch(r'<0\.[0-9]+\.[0-9]+>', proof[key])
                   for key in ('queue_pid', 'channel_pid', 'connection_pid'))
            or not isinstance(proof['consumer_tag'], str) or not 1 <= len(proof['consumer_tag']) <= 255):
        raise ValueError('private_delivery_unavailable')
    return proof['queue'], proof


def expression(player, sender, queue, owner, code):
    inner = {
        'm_Id': secrets.token_hex(16).upper(), 'm_ChannelType': 'Whispers', 'm_bUseSpoofedUserName': True,
        'm_SpoofedUserNameFrom': {'m_TableId': '', 'm_Key': '', 'm_UnlocalizedName': 'Cielago'},
        'm_FuncomIdFrom': sender['funcom_id'], 'm_UserNameTo': player['funcom_id'].split('#')[0],
        'm_Message': {'m_UnlocalizedMessage': 'Last Sietch portal verification: ' + code +
            '. Enter this only at portal.lastsietch.com. Expires in 5 minutes. If you did not request it, ignore it.',
            'm_LocalizedMessage': {'m_TableId': '', 'm_Key': '', 'm_FormatArgs': []}},
        'm_Timestamp': datetime.now(timezone.utc).strftime('%Y.%m.%d-%H.%M.%S'),
        'm_OriginLocation': {'X': 0, 'Y': 0, 'Z': 0}, 'm_HasSeenMessage': False,
    }
    body = json.dumps({'content': json.dumps(inner, separators=(',', ':')), 'Type': 'TextChat'}, separators=(',', ':'))
    if queue != player['principal'] + '_queue' or not isinstance(owner, dict):
        raise ValueError('private_delivery_unavailable')
    expected = ','.join(encoded(key) + ' => ' + encoded(owner[key]) for key in
                        ('queue', 'queue_pid', 'channel_pid', 'connection_pid', 'consumer_tag'))
    return f'''try
{native_queue_guard(player['principal'])}
Expected = #{{{expected}}},
true = (maps:from_list([{{atom_to_binary(K,utf8),V}} || {{K,V}} <- maps:to_list(Proof)]) =:= Expected),
Body={encoded(body)},
XName=rabbit_misc:r(<<"/">>,exchange,<<"chat.whispers">>),
P={{'P_basic',<<"Content">>,undefined,[],1,undefined,undefined,undefined,<<"300000">>,
    {encoded(secrets.token_hex(16))},undefined,<<"text_chat">>,{encoded(sender['principal'])},undefined,undefined}},
Content=rabbit_basic:build_content(P,Body),
{{ok,Msg}}=rabbit_basic:message(XName,{encoded(player['funcom_id'])},Content),
{{ok,stateless,[]}}=rabbit_queue_type:deliver([Q],Msg,#{{}},stateless),
accepted
catch _:_ -> refused end.
'''


def publish(config, expr):
    path = '/tmp/lastsietch-auth-' + secrets.token_hex(16)
    loader = ('try {ok,B}=file:read_file("' + path + '"),ok=file:delete("' + path + '"),'
              '{ok,T,_}=erl_scan:string(binary_to_list(B)),{ok,E}=erl_parse:parse_exprs(T),'
              '{value,V,_}=erl_eval:exprs(E,[]),V catch _:_ -> refused end.')
    script = ('set -eu\numask 077\ntrap \'rm -f -- "' + path + '"\' EXIT HUP INT TERM\n'
              'set -C\ncat > "' + path + '"\n' + RPC + " eval '" + loader + "'\n")
    # Only the random file path and constant loader reach argv. The expression
    # arrives on stdin, is mode 0600 and is removed before evaluating its body.
    result = run(['kubectl', 'exec', '-i', '-n', config['namespace'], config['pod'], '--',
                  'sh', '-c', script], expr, timeout=25)
    if result.strip() != 'accepted':
        raise ValueError('private_delivery_unavailable')


def execute(job):
    config = settings()
    if job.get('op') == 'lookup':
        player = lookup(character=job.get('character'))
        if config.get('allowed_accounts') is not None and player['account_id'] not in config['allowed_accounts']:
            raise ValueError('character_unavailable')
        private_queue(config, player['principal'])
        return {'ok': True, 'account_id': player['account_id'], 'character_name': player['character_name']}
    if job.get('op') not in ('send', 'check'):
        raise ValueError('bad_request')
    if config.get('allowed_accounts') is not None and job.get('account_id') not in config['allowed_accounts']:
        raise ValueError('character_unavailable')
    player = lookup(account_id=job.get('account_id'))
    queue, owner = private_queue(config, player['principal'])
    sender = database('SELECT json_build_object(\'principal\',"user",\'funcom_id\',funcom_id) '
                      'FROM dune.accounts WHERE id=' + str(config['sender_account']) + ';')
    if not sender or not sender.get('principal') or not str(sender.get('funcom_id', '')).startswith('Cielago#'):
        raise ValueError('sender_unavailable')
    if lookup(account_id=player['account_id']) != player:
        raise ValueError('character_unavailable')
    if job['op'] == 'check':
        return {'ok': True, 'private_queue_verified': True, 'sent': False}
    code = job.get('code')
    if not isinstance(code, str) or re.fullmatch('[0-9]{6}', code) is None:
        raise ValueError('bad_request')
    publish(config, expression(player, sender, queue, owner, code))
    return {'ok': True, 'sent': True}


def main():
    try:
        raw = sys.stdin.read(4097)
        if len(raw) > 4096:
            raise ValueError('bad_request')
        job = json.loads(raw)
        if not isinstance(job, dict):
            raise ValueError('bad_request')
        result = execute(job)
    except Exception:
        result = {'ok': False, 'error': 'game_verification_unavailable'}
    print(json.dumps(result, separators=(',', ':')))


if __name__ == '__main__':
    main()
