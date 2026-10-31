import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

_test_dir = tempfile.mkdtemp(prefix='lastfollow-tests-')
os.environ['DATABASE_PATH'] = str(Path(_test_dir) / 'results.db')
import database
import app as module


class AppTests(unittest.TestCase):
    def setUp(self):
        module.app.config.update(TESTING=True, REQUIRE_SUBSCRIPTION=False)
        module.app.config.pop('SUBSCRIPTION_CHECKER', None)
        self.client = module.app.test_client()
        with database.connect() as conn:
            conn.execute('DELETE FROM results')
        with module._jobs_lock:
            module._jobs.clear()

    def test_search_reads_query_and_redirects(self):
        response = self.client.get('/?username=%40Example')
        self.assertEqual(response.status_code, 303)
        self.assertIn('username=example', response.location)
        self.assertNotIn(b'Last Follower', self.client.get('/').data)

    def test_missing_and_invalid_username(self):
        self.assertEqual(self.client.get('/results').status_code, 302)
        self.assertEqual(self.client.get('/results?username=%3Cscript%3E').status_code, 400)
        self.assertEqual(self.client.post('/', data={}).status_code, 200)

    def test_results_are_selected_by_username(self):
        database.save_result('alice', [{'username':'alice_follower'}], [])
        database.save_result('bob', [{'username':'bob_follower'}], [])
        response = self.client.get('/results?username=alice')
        self.assertIn(b'alice_follower', response.data)
        self.assertNotIn(b'bob_follower', response.data)
        self.assertIn('no-store', response.headers['Cache-Control'])

    def test_cache_is_reused_without_new_job_or_ttl_extension(self):
        expiry = database.save_result('alice', [], [])
        with patch.object(module, 'ensure_job') as start:
            self.client.get('/results?username=alice')
            start.assert_not_called()
        self.assertEqual(database.get_result_by_username('alice')['expires_at'], expiry)

    def test_exact_180_seconds_and_expired_read(self):
        with patch.object(database.time, 'time', return_value=1000):
            expiry = database.save_result('alice', [], [])
        self.assertEqual(expiry, 1180)
        with patch.object(database.time, 'time', return_value=1179.99):
            self.assertIsNotNone(database.get_result_by_username('alice'))
        with patch.object(database.time, 'time', return_value=1180):
            self.assertIsNone(database.get_result_by_username('alice'))
        with database.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM results').fetchone()[0], 0)

    def test_independent_expirations(self):
        database.save_result('alice', [], [])
        database.save_result('bob', [], [])
        with database.connect() as conn:
            conn.execute('UPDATE results SET expires_at=0 WHERE username=?', ('alice',))
        database.purge_expired()
        self.assertIsNone(database.get_result_by_username('alice'))
        self.assertIsNotNone(database.get_result_by_username('bob'))

    def test_background_cleanup_without_http_visits(self):
        database.save_result('alice', [], [])
        with database.connect() as conn:
            conn.execute('UPDATE results SET expires_at=?', (time.time() - 1,))
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            with database.connect() as conn:
                count = conn.execute('SELECT COUNT(*) FROM results').fetchone()[0]
            if count == 0:
                break
            threading.Event().wait(0.1)
        self.assertEqual(count, 0)

    def test_storage_caps_both_lists_at_50(self):
        users = [{'username':str(n)} for n in range(80)]
        database.save_result('alice', users, users)
        data = database.get_result_by_username('alice')
        self.assertEqual(len(data['followers']), 50)
        self.assertEqual(len(data['following']), 50)
        self.assertEqual(data['following'][0]['username'], '0')
        self.assertEqual(data['following'][-1]['username'], '49')

    def test_expired_url_starts_new_job(self):
        database.save_result('alice', [{'username':'old_data'}], [])
        with database.connect() as conn:
            conn.execute('UPDATE results SET expires_at=0')
        with patch.object(module.threading, 'Thread') as thread, patch.object(module, '_slots'):
            response = self.client.get('/results?username=alice')
            thread.assert_called_once()
        self.assertIn(b'http-equiv="refresh"', response.data)
        self.assertNotIn(b'old_data', response.data)

    def test_loading_job_is_deduplicated(self):
        with patch.object(module.threading, 'Thread') as thread, patch.object(module, '_slots'):
            self.client.get('/results?username=alice')
            self.client.get('/results?username=alice')
            thread.assert_called_once()

    def test_busy_server_does_not_create_thread(self):
        slots = MagicMock()
        slots.acquire.return_value = False
        with patch.object(module, '_slots', slots), patch.object(module.threading, 'Thread') as thread:
            response = self.client.get('/results?username=alice')
            self.assertEqual(response.status_code, 503)
            thread.assert_not_called()

    def test_optional_subscription_gate_blocks_before_db(self):
        module.app.config['REQUIRE_SUBSCRIPTION'] = True
        with patch.object(module, 'get_result_by_username') as read:
            self.assertEqual(self.client.get('/results?username=alice&pro=1').status_code, 403)
            read.assert_not_called()

    def test_worker_saves_both_lists_after_success(self):
        session = MagicMock()
        session.__enter__.return_value = session
        with patch.object(module, 'make_session', return_value=session), patch.object(module, 'get_user_id', return_value=('123', {})), patch.object(module, 'fetch_users', side_effect=[[{'username':'followed'}], [{'username':'follower'}]]) as fetch, patch.object(module.time, 'sleep'), patch.object(module, '_slots'):
            module.collect('alice')
        data = database.get_result_by_username('alice')
        self.assertEqual(data['followers'][0]['username'], 'follower')
        self.assertEqual(data['following'][0]['username'], 'followed')
        self.assertEqual(fetch.call_args_list[0].args[4], 50)
        self.assertEqual(fetch.call_args_list[1].args[4], 50)
        self.assertEqual(module._jobs['alice']['state'], 'ready')

    def test_partial_worker_failure_does_not_save_success(self):
        session = MagicMock()
        with patch.object(module, 'make_session', return_value=session), patch.object(module, 'get_user_id', return_value=('123', {})), patch.object(module, 'fetch_users', side_effect=[[{'username':'followed'}], module.ScrapeError('Limited')]), patch.object(module.time, 'sleep'), patch.object(module, '_slots'), self.assertLogs(module.log, level='WARNING'):
            module.collect('alice')
        self.assertIsNone(database.get_result_by_username('alice'))
        self.assertEqual(module._jobs['alice']['state'], 'error')

    def test_pagination_deduplicates_preserves_order_and_stops(self):
        def payload(nodes, more, cursor):
            return {'data':{'user':{'edge_follow':{'edges':[{'node':{'id':str(n),'username':f'user{n}'}} for n in nodes], 'page_info':{'has_next_page':more,'end_cursor':cursor}}}}}
        with patch.object(module, 'get_json', side_effect=[payload(range(30), True, 'cursor1'), payload(range(25, 70), True, 'cursor2')]) as get, patch.object(module.time, 'sleep'):
            items = module.fetch_graphql_users(MagicMock(), '123', module.FOLLOWING_HASH, 'following')
        self.assertEqual(len(items), 50)
        self.assertEqual([u['username'] for u in items], [f'user{n}' for n in range(50)])
        self.assertEqual(get.call_count, 2)
        variables = json.loads(get.call_args.kwargs['params']['variables'])
        self.assertEqual(variables['after'], 'cursor1')
        self.assertEqual(variables['first'], 20)

    def test_429_stops_without_retry(self):
        session = MagicMock()
        session.get.return_value.status_code = 429
        with self.assertRaises(module.ScrapeError):
            module.get_json(session, 'https://www.instagram.com/example')
        session.get.assert_called_once()
        self.assertIn('timeout', session.get.call_args.kwargs)

    def test_legacy_database_migration_removes_old_rows(self):
        old_path = str(Path(_test_dir) / 'legacy.db')
        with sqlite3.connect(old_path) as conn:
            conn.execute('CREATE TABLE results (id INTEGER PRIMARY KEY, username TEXT, followers TEXT, following TEXT, timestamp TEXT)')
            conn.execute("INSERT INTO results VALUES (1, 'old', '[]', '[]', '2020-01-01')")
        with patch.object(database, 'DB_NAME', old_path):
            database.init_db()
            with database.connect() as conn:
                columns = {row['name'] for row in conn.execute('PRAGMA table_info(results)')}
                self.assertIn('expires_at', columns)
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM results').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
