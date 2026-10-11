"""Read-only localhost release checks. Never starts, submits or cancels a trade."""
import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from contextlib import closing

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import load_env
from core import Config, TZ, in_session
from datetime import datetime
from scripts.backtest import load_quotes, config_hash


def evidence(history, journal, backtest, config):
    """Read actual local artifacts. Missing evidence must never be a passed check."""
    report = {'data_quality': None, 'sample_outside_fit': False,
              'competition_journal': False, 'submission_observed': False,
              'cancellation_observed': False, 'fill_observed': False, 'partial_observed': False,
              'cancel_fill_race_observed': False, 'unresolved_intents': None}
    report['partial_check_required'] = config.order_size > 1
    reasons = []
    if history.is_file():
        try:
            _, quality = load_quotes(history)
            report['data_quality'] = quality
            if not quality['sufficient_for_parameter_fit']:
                reasons += quality['sufficiency_reasons']
        except (OSError, ValueError, sqlite3.Error, TypeError):
            reasons.append('无法读取历史 L1 或结构不符')
    else:
        reasons.append('尚无独立历史 L1 归档')
    try:
        replay = json.loads(backtest.read_text(encoding='utf-8'))
        historical = replay.get('historical') or {}
        oos = historical.get('out_of_sample') or {}
        report['sample_outside_fit'] = bool(report['data_quality']) and report['data_quality']['sufficient_for_parameter_fit'] and (
            bool(report['data_quality'].get('l1_sha256'))
            and historical.get('quality', {}).get('l1_sha256') == report['data_quality']['l1_sha256']
            and oos.get('status') == 'evaluated'
            and config_hash(config) in oos.get('profile_hashes', {}).values()
            and bool(oos.get('test_days')) and bool(oos.get('results')))
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    if not report['sample_outside_fit']:
        reasons.append('当前数据及参数尚无匹配的样本外评估')
    if journal.is_file():
        try:
            with closing(sqlite3.connect(journal.resolve().as_uri()+'?mode=ro', uri=True)) as db:
                db.execute('BEGIN')
                identity = db.execute("SELECT value FROM settings WHERE key='identity'").fetchone()
                identity = json.loads(identity[0]) if identity else []
                key = os.environ.get('CFAPIKEY', '')
                report['competition_journal'] = bool(key) and identity == ['competition',hashlib.sha256(key.encode()).hexdigest()]
                states = dict(db.execute('SELECT state,COUNT(*) FROM intents GROUP BY state'))
                report['unresolved_intents'] = sum(n for s,n in states.items() if s not in ('FILLED','CANCELLED','REJECTED','NOT_SENT'))
                report['journal_integrity'] = db.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
                if report['competition_journal']:
                    report['submission_observed'] = bool(sum(states.get(s,0) for s in ('OPEN','PARTIAL','FILLED','CANCELLED')))
                    report['cancellation_observed'] = bool(states.get('CANCELLED',0))
                    report['fill_observed'] = bool(states.get('FILLED',0))
                    if db.execute("SELECT 1 FROM sqlite_master WHERE name='intent_observations'").fetchone():
                        report['partial_observed'] = bool(db.execute("SELECT 1 FROM intent_observations WHERE state='PARTIAL' LIMIT 1").fetchone())
                        report['cancel_fill_race_observed'] = bool(db.execute("""SELECT 1 FROM intent_observations a
                            JOIN intent_observations b ON a.intent_id=b.intent_id
                            WHERE a.state='CANCELLING' AND b.state='FILLED' AND b.observed>=a.observed LIMIT 1""").fetchone())
        except (OSError, ValueError, TypeError, sqlite3.Error):
            reasons.append('比赛日志无法检查')
    if not report['competition_journal']:
        reasons.append('没有与当前密钥匹配的 competition 执行日志')
    if report.get('journal_integrity') is False: reasons.append('比赛日志完整性检查失败')
    for field, label in (('submission_observed','受理'), ('cancellation_observed','撤销'), ('fill_observed','成交')):
        if not report[field]: reasons.append('比赛日志尚未观察到'+label)
    if report['partial_check_required'] and not report['partial_observed']:
        reasons.append('多手委托尚未观察到部分成交')
    if report['unresolved_intents']: reasons.append('比赛日志仍有未终结委托')
    # Queries/journal states cannot prove venue fees, frozen margin or queue rules.
    reasons += ['样本外收益与实际撮合规则仍须核验', '零手续费及保证金冻结尚无平台成交对照证据']
    return report, reasons


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url',default='http://127.0.0.1:8000')
    parser.add_argument('--output',type=Path,default=Path('artifacts/preflight.json'))
    parser.add_argument('--history',type=Path,default=Path('data/l1.sqlite3'))
    parser.add_argument('--journal',type=Path,default=Path('data/competition.sqlite3'))
    parser.add_argument('--backtest-report',type=Path,default=Path('artifacts/backtest.json'))
    args=parser.parse_args()
    # Do not forward a local control token to a remote URL or redirect target.
    if args.url not in ('http://127.0.0.1:8000','http://localhost:8000'):
        parser.error('Only the default localhost service is accepted')
    load_env()
    token=os.environ.get('BOT_TOKEN','')
    if len(token)<24: parser.error('Missing valid BOT_TOKEN')
    from core import NoRedirect
    opener=urllib.request.build_opener(NoRedirect)
    def call(path,method='GET',body=None,auth=True,origin=None):
        headers={'Authorization':'Bearer '+token} if auth else {}
        if body is not None: headers['Content-Type']='application/json'
        if origin: headers['Origin']=origin
        request=urllib.request.Request(args.url+path,data=body,headers=headers,method=method)
        try:
            response=opener.open(request,timeout=15)
        except urllib.error.HTTPError as exc:
            response=exc
        with response:
            return response.code,dict(response.headers),response.read()
    checks={}
    status,headers,body=call('/')
    checks['page']=status==200 and b'IM2610' in body
    checks['security_headers']=headers.get('X-Frame-Options')=='DENY' and bool(headers.get('Content-Security-Policy'))
    checks['css']=call('/assets/style.css')[0]==200
    checks['javascript']=call('/assets/app.js')[0]==200
    checks['health']=call('/healthz',auth=False)[0]==200
    checks['unauthorized_rejected']=call('/api/state',auth=False)[0]==401
    checks['cross_origin_rejected']=call('/api/config','PUT',b'{}',origin='https://invalid.example')[0]==403
    checks['malformed_command_rejected']=call('/api/control','POST',b'{"action":null}')[0]==400
    checks['oversize_rejected']=call('/api/config','PUT',b' '*9000)[0]==413
    status,_,body=call('/api/state')
    checks['state']=status==200
    state=json.loads(body) if status==200 else {}
    checks['fixed_contract']=state.get('symbol')=='IM2610'
    checks['zero_fee']=state.get('config',{}).get('fee_per_side_bps')==0
    checks['token_not_exposed']=token.encode() not in body and b'CFAPIKEY' not in body
    checks['valid_config']=bool(state) and Config.parse(state['config']) is not None
    q=state.get('quote') or {}
    holdings=state.get('holdings') or {}
    gates=[]
    if state.get('mode')!='competition': gates.append('当前为 paper 模式')
    if state.get('latched') or not state.get('ready'): gates.append('未就绪或故障锁定')
    if state.get('query_failures'): gates.append('比赛接口正在重连，暂停报价')
    if time.time()-state.get('last_sync',0)>max(30,state.get('config',{}).get('poll_seconds',10)+15): gates.append('账户对账过期')
    if state.get('active_orders',0): gates.append('仍有本程序未终结委托，需先核查')
    if not in_session(datetime.now(TZ)): gates.append('当前交易窗口外')
    ages = [time.time()-q.get(key,0) for key in ('timestamp','received')]
    if any(not -2 <= age <= state.get('config',{}).get('max_quote_age',8) for age in ages): gates.append('行情过期或时钟偏差')
    if holdings.get('long',0)+holdings.get('short',0)>state.get('config',{}).get('max_position',1): gates.append('持仓超过策略上限，只能减仓')
    if state.get('account',{}).get('use_rate',0)>=state.get('config',{}).get('max_use_rate',.65): gates.append('保证金占比达到限制，只能减仓')
    local_evidence, remaining = evidence(args.history,args.journal,args.backtest_report,Config.parse(state.get('config',{})))
    gates += remaining
    report={'engineering_checks':checks,'engineering_pass':all(checks.values()),
            'go_live_ready':all(checks.values()) and not gates,'gates':gates,'evidence':local_evidence,'mode':state.get('mode'),
            'enabled':state.get('enabled'),'fee_bps':state.get('config',{}).get('fee_per_side_bps')}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False,indent=2))
    return 0 if report['engineering_pass'] else 1


if __name__=='__main__':
    sys.exit(main())
