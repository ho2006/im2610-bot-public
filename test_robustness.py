"""Offline fault and performance guards. Never loads credentials or calls a gateway."""
import http.client
import urllib.error
import unittest
from dataclasses import asdict, replace
from unittest.mock import patch

import test_bot as fixtures
from core import Competition, QueryUnavailable, Unsafe, Uncertain, order_key
from engine import Engine, SnapshotConflict
from scripts.benchmark import run


class RobustnessTests(unittest.TestCase):
    setUp = fixtures.BotTests.setUp

    def working(self):
        self.engine.cycle()
        self.engine.command('start')
        self.engine.cycle()
        self.engine.sync()
        return self.store.intents(active=True)[0]

    def test_account_identity_rejected_before_migration(self):
        self.store.put('identity', ['competition', 'other-account'])
        config = {**asdict(self.config), 'fee_per_side_bps': 2}
        self.store.put('config', config)
        self.store.put('zero_commission_applied', False)
        with self.assertRaises(Unsafe):
            Engine(self.store, Competition('offline-other-key'))
        self.assertEqual(self.store.get('config'), config)
        self.assertFalse(self.store.get('zero_commission_applied'))

    def test_read_race_is_rechecked_once_without_resending(self):
        intent = self.working()
        old = self.broker.orders()[0]
        self.broker.state['orders'][0]['state'] = 'CANCELLED'
        latest = self.broker.orders()[0]
        reads = []
        def orders(pending=False, day=None):
            if pending:
                return []
            reads.append(1)
            return [old if len(reads) == 1 else latest]
        with patch.object(self.broker, 'orders', orders), patch.object(self.broker, 'submit') as submit:
            self.engine.sync()
            self.assertEqual(len(reads), 2)
            submit.assert_not_called()
        self.assertEqual(self.store.intents()[0]['state'], 'CANCELLED')

    def test_terminal_order_reappearing_as_pending_blocks(self):
        intent = self.working()
        self.store.update(intent['id'], 'CANCELLED')
        with self.assertRaises(SnapshotConflict):
            self.engine.sync()

    def test_transport_failures_cancel_once_and_read_recovery(self):
        self.working()
        class Wake:
            calls = 0
            def clear(_self): pass
            def set(_self): pass
            def wait(_self, delay):
                _self.calls += 1
                if _self.calls == 2: self.engine.exit.set()
        self.engine.wake = Wake()
        with patch.object(self.engine, 'cycle', side_effect=[QueryUnavailable('read failed'), None]), \
                patch.object(self.broker, 'cancel', wraps=self.broker.cancel) as cancel:
            self.engine.run()
            self.assertEqual(cancel.call_count, 1)
        self.assertEqual(self.engine.query_failures, 0)
        self.assertEqual(self.engine.latch, '')
        self.assertEqual(self.store.intents()[0]['state'], 'CANCELLED')

    def test_unknown_cancel_is_never_retried_during_reconnect(self):
        self.working()
        def end_wait(delay): self.engine.exit.set()
        with patch.object(self.engine, 'cycle', side_effect=QueryUnavailable('read failed')), \
                patch.object(self.engine.wake, 'wait', side_effect=end_wait), \
                patch.object(self.broker, 'cancel', side_effect=Uncertain('lost receipt')) as cancel:
            self.engine.run()
            self.assertEqual(cancel.call_count, 1)
        self.assertTrue(self.engine.latch)
        self.assertEqual(self.store.intents()[0]['state'], 'UNKNOWN_CANCEL')

    def test_http_truncation_and_bad_ids_fail_at_boundary(self):
        broker = Competition('offline-key')
        response = unittest.mock.MagicMock()
        response.__enter__.return_value = response
        response.read.side_effect = http.client.IncompleteRead(b'partial')
        with patch.object(broker.opener, 'open', return_value=response), patch('core.time.sleep'):
            with self.assertRaises(QueryUnavailable): broker.call('query_today_orders')
            with self.assertRaises(Uncertain): broker.call('order_submission')
        row = {'futcode':'IM2610','operType':'0','wtzt':'2','wtsl':1,'wcjsl':1,
               'wtjg':7000,'wtsj':'10:05:00','wth':None}
        for bad in (None, True, {}, [], ''):
            with patch.object(broker, 'call', return_value=[{**row,'wth':bad}]), self.assertRaises(Unsafe):
                broker.orders(True)
        with patch.object(broker.opener, 'open', side_effect=urllib.error.HTTPError('https://offline.example',401,'denied',{},None)), patch('core.time.sleep'):
            with self.assertRaises(Unsafe) as failed: broker.call('query_account_detail')
            self.assertNotIsInstance(failed.exception, QueryUnavailable)
        with patch.object(broker.opener, 'open', side_effect=urllib.error.HTTPError('https://offline.example',503,'busy',{},None)), patch('core.time.sleep'):
            with self.assertRaises(QueryUnavailable): broker.call('query_account_detail')
            with self.assertRaises(Uncertain): broker.call('cancel_order')
        with self.assertRaises(Unsafe): broker.is_target({'futcode':['IM2610']})

    def test_rate_limit_wait_cannot_send_expired_quote(self):
        broker = Competition('offline-key')
        with patch('core.time.sleep'), patch('time.time', return_value=fixtures.TS+9), \
                patch.object(broker.opener, 'open') as send, self.assertRaises(Unsafe):
            broker.submit({'op':'0','price':7000,'qty':1}, fixtures.quote())
        send.assert_not_called()
        with self.assertRaises(Unsafe): fixtures.quote(received=fixtures.TS+3).fresh(8, fixtures.TS)

    def test_trade_display_refresh_does_not_skip_risk_reads(self):
        broker = unittest.mock.Mock(mode='competition', identity='offline', max_quote_age=8)
        broker.orders.return_value = []
        broker.holdings.return_value = self.engine.holdings
        broker.account.return_value = {'equity':1_000_000, 'use_rate':0}
        broker.trades.return_value = []
        # Separate identity, preserving the existing paper journal.
        self.store.put('identity', ['competition', 'offline'])
        engine = Engine(self.store, broker)
        engine.sync(); engine.sync()
        self.assertEqual(broker.trades.call_count, 1)
        self.assertEqual(broker.account.call_count, 2)
        self.assertEqual(broker.holdings.call_count, 2)
        self.assertEqual(broker.orders.call_count, 4)
        engine.sync(force_trades=True)
        self.assertEqual(broker.trades.call_count, 2)

    def test_unchanged_journal_preserves_timestamp_and_bounded_history(self):
        intent = self.working()
        with patch('time.time', return_value=fixtures.TS+10):
            self.store.update(intent['id'], intent['state'], intent['remote_key'], intent['remote_id'], intent['remaining'])
        self.assertEqual(self.store.intents()[0]['updated'], intent['updated'])
        result = run(rows=300, repeats=2)
        self.assertEqual(result['unchanged_sql_row_writes'], 0)
        self.assertLess(result['history_peak_bytes'], 250000)

    def test_residual_alert_preserves_passive_exit(self):
        self.engine.holdings.update(short=4, available_short=4)
        class ClosingClock(fixtures.datetime):
            @classmethod
            def now(cls, tz=None): return fixtures.NOW.replace(hour=15)
        with patch('engine.datetime', ClosingClock):
            self.assertIn('4 手', self.engine.alerts()[0])
        with self.assertRaises(Unsafe):
            self.broker.submit({'op':'2','price':fixtures.quote().ask,'qty':1}, fixtures.quote())

    def test_preflight_requires_data_and_matching_competition_journal(self):
        from scripts.preflight import evidence
        directory = fixtures.Path(self.tmp.name)
        result, reasons = evidence(directory/'missing.sqlite3', fixtures.Path(self.store.path), directory/'none.json', self.config)
        self.assertIsNone(result['data_quality'])
        self.assertFalse(result['competition_journal'])
        self.assertFalse(result['sample_outside_fit'])
        self.assertTrue(reasons)
        intent = self.working()
        self.store.update(intent['id'], 'PARTIAL', remaining=1)
        self.store.update(intent['id'], 'CANCELLING')
        self.store.update(intent['id'], 'FILLED', remaining=0)
        import hashlib
        self.store.put('identity', ['competition',hashlib.sha256(b'offline-evidence-key').hexdigest()])
        with patch.dict('os.environ', {'CFAPIKEY':'different-key'}):
            result, _ = evidence(directory/'missing', fixtures.Path(self.store.path), directory/'none', self.config)
            self.assertFalse(result['competition_journal'])
        with patch.dict('os.environ', {'CFAPIKEY':'offline-evidence-key'}):
            result, _ = evidence(directory/'missing', fixtures.Path(self.store.path), directory/'none', self.config)
            self.assertTrue(result['competition_journal'])
            self.assertTrue(result['partial_observed'])
            self.assertTrue(result['cancel_fill_race_observed'])
        self.assertEqual(result['unresolved_intents'], 0)

    def test_quote_regression_stops_both_feed_call_paths(self):
        for bad in (fixtures.quote(timestamp=fixtures.TS-1), fixtures.quote(volume=999)):
            self.engine.quote = fixtures.quote()
            self.engine.feed = lambda: bad
            with self.assertRaises(Unsafe): self.engine.read_quote()
            self.assertEqual(self.engine.quote, fixtures.quote())
        self.engine.feed = fixtures.quote
        self.working()
        self.engine.feed = lambda: fixtures.quote(timestamp=fixtures.TS-1)
        self.engine.cycle()
        self.assertEqual(self.store.intents()[0]['state'], 'CANCELLING')
        self.assertIn('回退', self.engine.note)


if __name__ == '__main__':
    unittest.main()
