import asyncio
import csv
import io
import json
import logging
import os
from pathlib import Path
import re
import threading
import time
from urllib.parse import unquote

import requests
from aiograpi import Client
from flask import Flask, Response, abort, redirect, render_template, request, url_for
from database import init_db, save_result, get_result_by_username, purge_expired

app = Flask(__name__)
app.config['REQUIRE_SUBSCRIPTION'] = os.getenv('REQUIRE_SUBSCRIPTION', '0') == '1'
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)
init_db()

_jobs = {}
_jobs_lock = threading.RLock()
_slots = threading.BoundedSemaphore(2)


class ScrapeError(Exception):
    pass


def normalize_username(value):
    username = (value or '').strip().lstrip('@').lower()
    if not re.fullmatch(r'[a-z0-9._]{1,30}', username):
        raise ValueError('Invalid Instagram username.')
    return username


async def fetch_instagram_data(username):
    cl = Client()
    
    session_path = Path(__file__).with_name('COOKIES_mobile.json')
    if not session_path.is_file():
        session_path = Path(__file__).with_name('session.json')
        
    try:
        if session_path.is_file():
            with open(session_path, 'r', encoding='utf-8') as f:
                raw_data = json.load(f)
            
            if isinstance(raw_data, list):
                cookies = {item['name']: item['value'] for item in raw_data if 'name' in item and 'value' in item}
                sessionid = cookies.get('sessionid')
                if sessionid:
                    await cl.login_by_sessionid(unquote(sessionid))
                else:
                    raise ScrapeError('Instagram sessionid is missing in COOKIES_mobile.json.')
            elif isinstance(raw_data, dict):
                cl.set_settings(raw_data)
        else:
            sessionid = os.getenv('INSTAGRAM_SESSION_ID')
            if sessionid:
                await cl.login_by_sessionid(unquote(sessionid))
            else:
                raise ScrapeError('Cookie file (COOKIES_mobile.json) or session configuration is missing.')

        user_id = await cl.user_id_from_username(username)
        user_info = await cl.user_info(user_id)
        
        if getattr(user_info, 'is_private', False):
            raise ScrapeError('This tool supports public profiles only.')

        followers_raw = await cl.user_followers(user_id, amount=50)
        following_raw = await cl.user_following(user_id, amount=50)

        followers = []
        items = followers_raw.values() if isinstance(followers_raw, dict) else followers_raw
        for item in items:
            pk = getattr(item, 'pk', None) or (item.get('pk') if isinstance(item, dict) else None)
            uname = getattr(item, 'username', None) or (item.get('username') if isinstance(item, dict) else None)
            if pk and uname:
                followers.append({'id': str(pk), 'username': uname})

        following = []
        f_items = following_raw.values() if isinstance(following_raw, dict) else following_raw
        for item in f_items:
            pk = getattr(item, 'pk', None) or (item.get('pk') if isinstance(item, dict) else None)
            uname = getattr(item, 'username', None) or (item.get('username') if isinstance(item, dict) else None)
            if pk and uname:
                following.append({'id': str(pk), 'username': uname})

        return followers[:50], following[:50]

    except Exception as exc:
        log.error("aiograpi error: %s", exc)
        raise ScrapeError(f'Collection failed: {str(exc)}') from exc


def collect(username):
    state, message = 'error', 'Collection failed. Please try again later.'
    try:
        followers, following = asyncio.run(fetch_instagram_data(username))
        save_result(username, followers, following)
        state, message = 'ready', None
    except ScrapeError as exc:
        message = str(exc)
    except Exception as exc:
        log.exception('Unexpected collection failure')
        message = 'Collection failed. Please check server configuration.'
    finally:
        with _jobs_lock:
            _jobs[username] = {'state': state, 'message': message, 'updated': time.time()}
        _slots.release()


def ensure_job(username):
    with _jobs_lock:
        job = _jobs.get(username)
        if job and job['state'] == 'loading':
            return job
        if job and job['state'] == 'error' and time.time() - job['updated'] < 30:
            return job
        if not _slots.acquire(blocking=False):
            return {'state': 'busy', 'message': 'Server busy. Try again in a moment.'}
        job = {'state': 'loading', 'message': None, 'updated': time.time()}
        _jobs[username] = job
        try:
            threading.Thread(target=collect, args=(username,), daemon=True).start()
        except Exception:
            _jobs.pop(username, None)
            _slots.release()
            raise
        return job


def cleanup_loop():
    while True:
        time.sleep(1)
        try:
            purge_expired()
            with _jobs_lock:
                stale = [u for u, j in _jobs.items()
                         if j['state'] != 'loading' and time.time() - j['updated'] >= 180]
                for username in stale:
                    _jobs.pop(username, None)
        except Exception:
            log.exception('Cache cleanup failed')


threading.Thread(target=cleanup_loop, daemon=True, name='cache-cleanup').start()


@app.route('/', methods=['GET', 'POST'])
def index():
    raw = request.form.get('username') if request.method == 'POST' else request.args.get('username')
    if raw is not None:
        try:
            username = normalize_username(raw)
        except ValueError as exc:
            return render_template('index.html', error=str(exc)), 400
        return redirect(url_for('results', username=username), code=303)
    return render_template('index.html')


@app.route('/results')
@app.route('/results/')
def results():
    if not request.args.get('username'):
        return redirect(url_for('index'))
    try:
        username = normalize_username(request.args['username'])
    except ValueError as exc:
        return render_template('index.html', error=str(exc)), 400
        
    data = get_result_by_username(username)
    if data:
        return render_template('results.html', remaining_ms=max(0, int((data['expires_at'] - time.time()) * 1000)), **data)
        
    job = ensure_job(username)
    if job['state'] == 'ready':
        data = get_result_by_username(username)
        if data:
            return render_template('results.html', remaining_ms=max(0, int((data['expires_at'] - time.time()) * 1000)), **data)
            
    status = 503 if job['state'] == 'busy' else 200
    return render_template('waiting.html', username=username, **job), status


# مسار الـ Proxy لجلب وتمرير صور الحسابات بأمان وتجنب حظر إنستغرام
@app.route('/proxy/avatar/<path:username>')
def proxy_avatar(username):
    # توجيه المتصفح مباشرة لرابط الصورة السريع والآمن
    return redirect(f"https://unavatar.io/instagram/{username}")
    
    # صورة افتراضية احتياطية في حال تعذر الجلب
    return redirect(f"https://api.dicebear.com/7.x/avataaars/svg?seed={username}")


@app.route('/export/csv/<username>')
def export_csv(username):
    try:
        username = normalize_username(username)
    except ValueError:
        return abort(400)
        
    data = get_result_by_username(username)
    if not data:
        return abort(404)
        
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Type', 'User ID', 'Username'])
    
    for item in data.get('followers', []):
        writer.writerow(['Follower', item.get('id', ''), item.get('username', '')])
        
    for item in data.get('following', []):
        writer.writerow(['Following', item.get('id', ''), item.get('username', '')])
        
    response = Response(output.getvalue(), mimetype='text/csv')
    response.headers['Content-Disposition'] = f'attachment; filename={username}_data.csv'
    return response


if __name__ == '__main__':
    app.run(host='127.0.0.1', port=int(os.getenv('PORT', '5000')), debug=False, threaded=True)