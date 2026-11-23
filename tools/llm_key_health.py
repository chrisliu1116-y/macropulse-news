# -*- coding: utf-8 -*-
"""Gemini 多 key 逐个体检（零依赖，只打印状态码，绝不打印 key 明文）。
用途：回答「三个 key 是不是都 429」——逐个发一个 maxOutputTokens=1 的最小请求，
把每个 key 的 HTTP 状态、错误摘要、以及 key 指纹（sha256 前 8 位）都打出来，
指纹用于判断三个 key 是否真的互不相同（不同 GCP 项目）。"""
import hashlib
import json
import os
import urllib.error
import urllib.request

MODEL = os.environ.get('HEALTH_MODEL') or 'gemini-3-flash-preview'
BASE = 'https://generativelanguage.googleapis.com/v1beta/models/' + MODEL + ':generateContent?key='

keys = []
for env in ('GEMINI_KEY', 'GEMINI_API_KEY', 'GEMINI_API_KEYS'):
    v = os.environ.get(env, '') or ''
    for k in v.split(','):
        k = k.strip()
        if k and k not in keys:
            keys.append(k)

print('=== Gemini key 体检（模型 %s）===' % MODEL)
print('拆分后 key 数量 = %d' % len(keys))
uniq = {hashlib.sha256(k.encode()).hexdigest()[:8] for k in keys}
print('去重指纹数量 = %d（%d = 三个确实是不同的 key）' % (len(uniq), len(uniq)))

for i, k in enumerate(keys):
    fp = hashlib.sha256(k.encode()).hexdigest()[:8]
    body = {'contents': [{'role': 'user', 'parts': [{'text': 'hi'}]}],
            'generationConfig': {'maxOutputTokens': 1}}
    req = urllib.request.Request(BASE + k, data=json.dumps(body).encode(),
                                 headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            print('key#%d fp=%s -> HTTP %d 可用' % (i + 1, fp, r.status))
    except urllib.error.HTTPError as e:
        detail = ''
        try:
            detail = e.read().decode('utf-8', 'ignore')[:240].replace('\n', ' ')
        except Exception:
            pass
        print('key#%d fp=%s -> HTTP %d  %s' % (i + 1, fp, e.code, detail))
    except Exception as e:
        print('key#%d fp=%s -> ERR %s' % (i + 1, fp, type(e).__name__))
