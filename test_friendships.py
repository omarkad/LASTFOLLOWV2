import unittest
from unittest.mock import MagicMock, patch
import test_app
import app as m

class FriendshipsTests(unittest.TestCase):
    def fetch(self, pages):
        with patch.object(m, 'FETCH_BACKEND', 'friendships'), patch.object(m, 'get_json', side_effect=pages) as get, patch.object(m.time, 'sleep'):
            result = m.fetch_users(MagicMock(), '123', '', 'followers')
            return result, get.call_args_list

    def page(self, start, end, cursor=None):
        return {'users': [{'pk':n, 'username':f'u{n}'} for n in range(start,end)], 'next_max_id':cursor}

    def test_dedup_and_cap(self):
        rows, calls = self.fetch([self.page(0,25,'a'), self.page(20,45,'b'), self.page(40,65)])
        self.assertEqual(len(rows),50)
        self.assertEqual(len({r['username'] for r in rows}),50)
        self.assertEqual(calls[1].kwargs['params']['max_id'],'a')
        self.assertEqual(calls[2].kwargs['params']['count'],5)

    def test_small_list(self):
        self.assertEqual(len(self.fetch([self.page(0,3)])[0]),3)

    def test_repeated_cursor(self):
        with self.assertRaises(m.ScrapeError):
            self.fetch([self.page(0,10,'a'),self.page(5,10,'a')])

    def test_invalid_payload(self):
        with self.assertRaises(m.ScrapeError): self.fetch([{'status':'ok'}])

    def test_false_empty(self):
        with self.assertRaises(m.ScrapeError):
            m.validate_list_count([], {'edge_followed_by':{'count':123}},'edge_followed_by')

    def test_private_rejected(self):
        with patch.object(m,'get_json',return_value={'data':{'user':{'id':'1','is_private':True}}}), self.assertRaises(m.ScrapeError):
            m.get_user_id('private',MagicMock())

    def test_fail_in_http_200(self):
        s=MagicMock(); s.get.return_value.status_code=200
        s.get.return_value.json.return_value={'status':'fail','message':'login_required'}
        with self.assertRaises(m.ScrapeError): m.get_json(s,'https://www.instagram.com/')
        s.get.assert_called_once()

    def test_deadline(self):
        with patch.object(m,'get_json') as get, self.assertRaises(m.ScrapeError):
            m.fetch_users(MagicMock(),'123','',deadline=0)
        get.assert_not_called()

if __name__=='__main__': unittest.main()
