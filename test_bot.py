"""Offline regression check: python -m unittest -v test_bot. No credentials/network."""
import json
import tempfile
import unittest
import urllib.error
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from app import create_app
from core import (Competition, Config, Quote, TZ, Unsafe, Uncertain, in_session,
                  make_quotes, parse_sina, validate_order)
from engine import Engine, Paper, Store

NOW = datetime(2026, 9, 30, 10, 5, tzinfo=TZ)
TS = NOW.timestamp()


class Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW if tz else NOW.replace(tzinfo=None)


def quote(**changes):
    return replace(Quote(7000, 7000.6, 30, 10, 7000.2, 1000, 7700, 6300, TS, TS), **changes)


def raw_quote():
    values = ["0"] * 50
    for i, val in {3:"7291.0",4:"36071",9:"8054.6",10:"6590.2",16:"7290.6",17:"3",26:"7291.2",27:"26",36:"2026-09-30",37:"15:00:00",49:"中证1000指数期货2610"}.items():
        values[i] = val
    return 'var hq_str_nf_IM2610="' + ','.join(values) + '";\n'


class BotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "bot.sqlite3")
        self.config = Config(fee_per_side_bps=0, edge_ticks=0,max_position=2,entry_filter=False)
        self.store.put("config", asdict(self.config))
        self.broker = Paper(self.store, self.config)
        self.engine = Engine(self.store, self.broker, feed=quote)
        self.clock = patch("engine.datetime", Clock)
        self.timer = patch("time.time", return_value=TS)
        self.clock.start()
        self.timer.start()
        self.addCleanup(self.clock.stop)
        self.addCleanup(self.timer.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_sina_actual_50_field_layout(self):
        q = parse_sina(raw_quote(), received=TS)
        self.assertEqual((q.bid, q.ask, q.bid_size, q.ask_size), (7290.6,7291.2,3,26))
        self.assertLess(q.microprice, q.mid)
        self.assertEqual(datetime.fromtimestamp(q.timestamp,TZ).hour,15)
        for bad in (raw_quote().replace('nf_IM2610','nf_IM2611'), raw_quote().replace(',26,',',0,'), raw_quote().replace('7290.6','NaN'),raw_quote().replace('7290.6','7292.0')):
            with self.assertRaises(Unsafe):parse_sina(bad)

    def test_freshness_and_exchange_session(self):
        quote().fresh(8, TS+7)
        with self.assertRaises(Unsafe):quote().fresh(8,TS+9)
        with self.assertRaises(Unsafe):quote().fresh(8,TS-3)
        self.assertTrue(in_session(NOW))
        for now in (NOW.replace(hour=12),NOW.replace(hour=15),NOW.replace(month=10,day=19),NOW.replace(day=26)):
            self.assertFalse(in_session(now))

    def test_microprice_and_imbalance_are_consistent(self):
        q=quote()
        self.assertAlmostEqual(q.imbalance,.5)
        self.assertAlmostEqual(q.microprice,q.mid+q.imbalance*(q.ask-q.bid)/2)
        for imbalance in ((1,100),(100,1),(1,1)):
            q=quote(bid_size=imbalance[0],ask_size=imbalance[1])
            plans=make_quotes(q,0,0,0,self.config)
            for p in plans:validate_order(p,q)
            if imbalance==(1,100):self.assertEqual([p['op'] for p in plans],['1'])
            if imbalance==(100,1):self.assertEqual([p['op'] for p in plans],['0'])

    def test_close_first_and_reduce_only(self):
        plans=make_quotes(quote(),1,1,0,self.config,reduce_only=True)
        self.assertEqual([p['op'] for p in plans],['3'])
        self.assertEqual(plans[0]['price'],quote().ask)
        plans=make_quotes(quote(),-1,0,1,self.config,reduce_only=True)
        self.assertEqual([p['op'] for p in plans],['2'])
        self.assertEqual(make_quotes(quote(),0,0,0,self.config,reduce_only=True),[])

    def test_parameter_and_order_validation(self):
        for values in ({'order_size':True},{'order_size':1.5},{'max_quote_age':float('nan')},{'max_position':0},{'poll_seconds':0},{'symbol':'IF2610'},{'flatten_time':'15:30'}):
            with self.assertRaises(Unsafe):Config.parse(values)
        for p in ({'op':'0','price':7000.1,'qty':1},{'op':'0','price':7000.2,'qty':1},{'op':'3','price':7000,'qty':1},{'op':'4','price':7000,'qty':1}):
            with self.assertRaises(Unsafe):validate_order(p,quote())

    def test_full_cycle_no_duplicate_then_pause_cancel(self):
        self.engine.cycle()
        self.engine.command('start')
        self.engine.cycle()
        self.assertEqual(len(self.broker.orders()),1)
        self.engine.cycle()
        self.assertEqual(len(self.broker.orders()),2)
        self.engine.cycle()
        self.assertEqual(len(self.broker.orders()),2)
        self.engine.command('pause')
        self.engine.cycle()
        self.engine.cycle()
        self.engine.cycle()
        self.assertEqual(self.broker.orders(True),[])
        self.assertFalse(self.engine.enabled)
        self.assertFalse(self.store.intents(active=True))

    def test_submit_timeout_is_journalled_and_never_retried(self):
        self.engine.cycle()
        self.engine.command('start')
        original=self.broker.submit
        def timeout(plan,q):
            original(plan,q)
            raise Uncertain('lost response')
        with patch.object(self.broker,'submit',side_effect=timeout) as submit:
            with self.assertRaises(Uncertain):self.engine.cycle()
            self.engine.halt('lost response')
            self.assertEqual(self.store.intents()[0]['state'],'UNKNOWN')
            self.engine.cycle()
            self.assertEqual(submit.call_count,1)
            self.assertTrue(self.engine.latch)
            self.assertEqual(len(self.broker.orders()),1)

    def test_prepared_intent_after_crash_does_not_resend(self):
        self.store.add_intent({'op':'0','price':7000,'qty':1},[])
        with patch.object(self.broker,'submit') as submit:
            with self.assertRaises(Unsafe):self.engine.cycle()
            self.assertFalse(submit.called)
            self.assertEqual(self.store.intents()[0]['state'],'UNKNOWN')

    def test_cancel_timeout_does_not_repeat(self):
        self.engine.cycle();self.engine.command('start');self.engine.cycle();self.engine.sync()
        with patch.object(self.broker,'cancel',side_effect=Uncertain('lost')) as cancel:
            with self.assertRaises(Uncertain):self.engine.cancel_all_step()
            self.engine.cancel_all_step()
            self.assertEqual(cancel.call_count,1)
            self.assertEqual(self.store.intents()[0]['state'],'UNKNOWN_CANCEL')

    def test_restart_reconciles_known_order(self):
        self.engine.cycle();self.engine.command('start');self.engine.cycle()
        restarted=Engine(self.store,Paper(self.store,self.config),feed=quote)
        restarted.cycle()
        self.assertFalse(restarted.enabled)
        self.assertEqual(len(restarted.broker.orders()),1)
        self.assertEqual(restarted.broker.orders()[0]['state'],'CANCELLED')

    def test_unknown_foreign_orders_are_not_claimed(self):
        self.broker.submit({'op':'0','price':7000,'qty':1},quote())
        with self.assertRaisesRegex(Unsafe,'非本程序'):self.engine.cycle()
        self.assertEqual(len(self.broker.orders(True)),1)

    def test_partial_fill_retains_reservation(self):
        self.broker.on_quote(quote())
        self.broker.submit({'op':'0','price':6999.8,'qty':2},quote())
        later=quote(bid=6999.4,ask=6999.8,last=6999.6,volume=1001,timestamp=TS+1,received=TS+1)
        self.broker.on_quote(later)
        order=self.broker.orders(True)[0]
        self.assertEqual((order['state'],order['remaining']),('PARTIAL',1))
        self.assertEqual(self.broker.holdings()['long'],1)
        self.broker.on_quote(later)
        self.assertEqual(self.broker.holdings()['long'],1)

    def test_inventory_and_pending_order_budget(self):
        self.engine.account={'equity':1_000_000,'use_rate':0}
        self.engine.holdings={'long':1,'short':0,'available_long':1,'available_short':0}
        self.engine.pending=[{'op':'0','remaining':1}]
        plans,_=self.engine.risk_and_plan(quote(),NOW)
        self.assertTrue(any(p['op']=='3' for p in plans))
        self.engine.pending=[{'op':'0','remaining':2}]
        plans,_=self.engine.risk_and_plan(quote(),NOW)
        self.assertFalse(any(p['op'] in ('0','1') for p in plans))

    def test_loss_latch_and_fee_edge(self):
        self.engine.account={'equity':1_000_000,'use_rate':0}
        self.store.put('daily',{'loss_hit':True})
        plans,reduce=self.engine.risk_and_plan(quote(),NOW)
        self.assertTrue(reduce);self.assertEqual(plans,[])
        cheap=make_quotes(quote(),0,0,0,self.config)
        expensive=make_quotes(quote(),0,0,0,Config(fee_per_side_bps=10))
        self.assertLess(expensive[0]['price'],cheap[0]['price'])
        self.assertGreater(expensive[1]['price'],cheap[1]['price'])

    def test_zero_commission_default_and_legacy_config_migration(self):
        self.assertEqual(Config().fee_per_side_bps,0)
        self.assertEqual(Config().max_position,1)
        self.assertEqual(Config().flatten_time,'14:50')
        self.store.put('config',asdict(Config(fee_per_side_bps=2.3)))
        self.store.put('zero_commission_applied',False)
        migrated=Engine(self.store,self.broker,feed=quote)
        self.assertEqual(migrated.config.fee_per_side_bps,0)
        self.assertEqual(self.store.get('config')['fee_per_side_bps'],0)
        self.assertEqual(self.broker.config.fee_per_side_bps,0)

    def test_zero_commission_fill_does_not_deduct_cash(self):
        self.broker.config=Config()
        self.broker.on_quote(quote())
        self.broker.submit({'op':'0','price':6999.8,'qty':1},quote())
        self.broker.on_quote(quote(bid=6999.4,ask=6999.8,last=6999.6,volume=1001,timestamp=TS+1,received=TS+1))
        self.assertEqual(self.broker.holdings()['long'],1)
        self.assertEqual(self.broker.state['cash'],1_000_000)

    def test_contract_name_mapping_is_exact(self):
        broker=Competition('fake-not-a-real-key');broker.names.add('中证1000股指2610')
        self.assertTrue(broker.is_target({'futname':'中证1000股指2610'}))
        for row in ({'futname':'中证1000股指2611'},{'futcode':'IF2610','futname':'中证1000股指2610'}):
            self.assertFalse(broker.is_target(row))
        with self.assertRaises(Unsafe):broker.is_target({'ccsl':'9'})

    def test_database_cannot_switch_mode_or_account(self):
        with self.assertRaises(Unsafe):Engine(self.store,Competition('other-account'),feed=quote)

    def test_gateway_fixed_contract_and_no_network_retry(self):
        broker=Competition('offline-dummy-credential')
        class Response:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read(self,*args):return b'{"status":0,"msg":"ok"}'
        with patch.object(broker.opener,'open',return_value=Response()) as opening, patch('core.time.sleep'):
            broker.submit({'op':'0','price':7000,'qty':1},quote())
            payload=json.loads(opening.call_args.args[0].data)
            self.assertEqual(payload,{'action':'order_submission','market':'CFFEX','futcode':'IM2610','operType':'0','wtjg':'7000.0','wtsl':'1'})
            broker.cancel('sample-order-id')
            self.assertEqual(json.loads(opening.call_args.args[0].data),{'action':'cancel_order','futcode':'IM2610','wth':'sample-order-id'})
        with patch.object(broker.opener,'open',side_effect=TimeoutError) as opening,patch('core.time.sleep'):
            with self.assertRaises(Uncertain):broker.submit({'op':'0','price':7000,'qty':1},quote())
            self.assertEqual(opening.call_count,1)

    def test_stale_feed_removes_working_quotes(self):
        self.engine.cycle();self.engine.command('start');self.engine.cycle();self.engine.sync()
        self.engine.feed=lambda:quote(timestamp=TS-100)
        self.engine.cycle()
        self.assertFalse(self.broker.orders(True))
        self.assertEqual(len(self.broker.orders()),1)

    def test_pending_pause_prevents_submit_after_slow_read(self):
        self.engine.cycle();self.engine.enabled=True
        calls=[]
        def feed():
            calls.append(1)
            if len(calls)==2:self.engine.command('pause')
            return quote()
        self.engine.feed=feed
        self.engine.cycle()
        self.assertEqual(self.broker.orders(),[])

    def test_cancel_confirmation_before_replacement(self):
        self.engine.cycle();self.engine.command('start');self.engine.cycle();self.engine.sync()
        self.engine.config.order_lifetime=0
        self.engine.cycle()
        self.assertEqual(len(self.broker.orders()),1)
        self.assertEqual(self.store.intents()[0]['state'],'CANCELLING')
        self.engine.sync()
        self.assertEqual(self.store.intents()[0]['state'],'CANCELLED')

    def test_restart_uses_history_for_previous_day_intents(self):
        self.engine.cycle();self.engine.command('start');self.engine.cycle();self.engine.sync()
        intent=self.store.intents()[0]
        previous='2026-09-29'
        from core import order_key
        self.broker.state['orders'][0]['time']=previous+'T10:05:00+08:00'
        self.broker.state['orders'][0]['state']='CANCELLED'
        row={**self.broker.state['orders'][0],'day':previous}
        with self.store.connect() as db:
            db.execute('UPDATE intents SET day=?,remote_key=? WHERE id=?',(previous,order_key(row),intent['id']))
        self.engine.sync()
        self.assertEqual(self.store.intents()[0]['state'],'CANCELLED')

    def test_fills_are_persisted_by_trade_day(self):
        self.broker.state['trades']=[{'time':NOW.isoformat(),'operation':'买开','price':7000,'qty':1}]
        self.engine.sync()
        self.assertEqual(self.store.get('trades:2026-09-30'),self.broker.trades())

    def test_duplicate_or_regressing_quote_does_not_evict_history(self):
        self.store.tick(quote(),7000,1_000_000,0)
        self.store.tick(quote(received=TS+10),7000,1_000_000,0)
        self.store.tick(quote(timestamp=TS-1),7000,1_000_000,0)
        self.assertEqual(len(self.store.history()['ticks']),1)

    def test_high_margin_existing_short_is_reduce_only(self):
        self.engine.account={'equity':970000,'use_rate':.903}
        self.engine.holdings={'long':0,'short':4,'available_long':0,'available_short':4}
        plans,reduce=self.engine.risk_and_plan(quote(),NOW)
        self.assertTrue(reduce)
        self.assertEqual([(p['op'],p['qty'],p['price']) for p in plans],[('2',1,quote().bid)])

    def test_passive_daily_loss_is_not_a_guaranteed_loss_cap(self):
        self.broker.state.update(long=1,long_cost=7000,mid=7000)
        self.engine.sync()
        self.broker.on_quote(quote(bid=6979.8,ask=6980.2,last=6980,volume=1100,timestamp=TS+1))
        self.engine.sync()
        self.assertTrue(self.store.get('daily')['loss_hit'])
        self.broker.on_quote(quote(bid=6899.8,ask=6900.2,last=6900,volume=1200,timestamp=TS+2))
        self.engine.sync()
        self.assertLess(self.store.get('daily')['pnl'],-self.config.max_daily_loss)

    def test_backtest_data_quality_and_no_same_sample_fill(self):
        from scripts.backtest import load_quotes,replay
        path=Path(self.tmp.name)/'l1.jsonl'
        records=[quote(),quote(),quote(timestamp=TS-100,received=TS+1)]
        raw=[asdict(q) for q in records]
        raw += [{**asdict(quote()),'bid_size':True}, {**asdict(quote()),'received':1e100},
                {**asdict(quote()),'symbol':'IF2610'}]
        path.write_text('\n'.join(json.dumps(q) for q in raw),encoding='utf-8')
        records,meta=load_quotes(path)
        self.assertEqual(meta['exact_duplicates'],1)
        self.assertEqual(meta['invalid'],3)
        self.assertEqual(meta['fresh_in_session'],1)
        self.assertFalse(meta['sufficient_for_parameter_fit'])
        result=replay([quote()],self.config)
        self.assertEqual(result['orders'],1)
        self.assertEqual(result['fill_lots'],0)
        self.assertEqual(result['pnl_marked'],0)

    def test_backtest_delayed_order_and_fill_cancel_race(self):
        from scripts.backtest import ReplayPaper
        broker=ReplayPaper(self.store,self.config,'through',3,.1)
        broker.clock=TS
        broker.on_quote(quote())
        broker.submit({'op':'0','price':6999.8,'qty':1},quote())
        broker.clock=TS+1
        q=quote(bid=6999.4,ask=6999.8,last=6999.6,volume=1100,timestamp=TS+1,received=TS+1)
        broker.on_quote(q)
        self.assertEqual(broker.state['long'],0)
        broker.clock=TS+2
        broker.cancel(broker.state['orders'][0]['remote_id'])
        broker.clock=TS+4
        broker.on_quote(replace(q,volume=1200,timestamp=TS+4,received=TS+4))
        self.assertEqual(broker.state['long'],1)
        self.assertEqual(broker.orders()[0]['state'],'FILLED')

    def test_backtest_optimistic_touch_is_only_a_sensitivity_model(self):
        from scripts.backtest import ReplayPaper
        broker=ReplayPaper(self.store,self.config,'touch',0,.1)
        broker.clock=TS
        broker.on_quote(quote())
        broker.submit({'op':'0','price':7000,'qty':1},quote())
        broker.clock=TS+1
        touched=quote(last=7000,volume=1100,timestamp=TS+1,received=TS+1)
        self.assertFalse(self.broker.matches(broker.state['orders'][0],touched))
        broker.on_quote(touched)
        self.assertEqual(broker.state['long'],1)

    def test_health_detects_stalled_worker_and_database_integrity(self):
        client=create_app(self.engine,'test-token-for-offline-check-only').test_client()
        self.engine.last_cycle=TS-300
        self.assertEqual(client.get('/healthz').status_code,503)
        with self.store.connect() as db:
            self.assertEqual(db.execute('PRAGMA quick_check').fetchone()[0],'ok')

    def test_paper_marks_closing_quote_without_matching_outside_session(self):
        closing=NOW.replace(hour=15,minute=0)
        class ClosingClock(datetime):
            @classmethod
            def now(cls,tz=None):return closing if tz else closing.replace(tzinfo=None)
        self.broker.state.update(long=1,long_cost=7000,mid=7000)
        self.engine.feed=lambda:quote(bid=6899.8,ask=6900.2,last=6900,
            timestamp=closing.timestamp(),received=closing.timestamp())
        with patch('engine.datetime',ClosingClock),patch('time.time',return_value=closing.timestamp()):
            self.engine.cycle()
            self.assertEqual(self.broker.account()['equity'],980000)
            self.assertEqual(self.broker.state['long'],1)
            self.assertEqual(self.broker.orders(),[])

    def test_http_auth_csrf_config_and_control(self):
        token='test-token-for-offline-check-only'
        app=create_app(self.engine,token);client=app.test_client();headers={'Authorization':'Bearer '+token}
        with client.get('/') as response:
            self.assertEqual(response.status_code,200)
        self.assertEqual(client.get('/api/state').status_code,401)
        self.assertEqual(client.get('/api/state',headers=headers).status_code,200)
        response=client.post('/api/control',headers={**headers,'Origin':'https://evil.example'},json={'action':'start'})
        self.assertEqual(response.status_code,403)
        self.assertEqual(client.post('/api/control',headers=headers,json={'action':'start','symbol':'IF2610'}).status_code,400)
        self.assertEqual(client.put('/api/config',headers=headers,json={'max_position':float('nan')}).status_code,400)
        self.assertEqual(client.post('/api/control',headers=headers,json={'action':'pause'}).status_code,202)
        payload=client.get('/api/state',headers=headers).get_data(as_text=True)
        self.assertNotIn('CFAPIKEY',payload);self.assertNotIn(token,payload)


if __name__=='__main__':unittest.main()
