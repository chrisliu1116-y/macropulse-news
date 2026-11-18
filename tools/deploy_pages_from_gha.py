# -*- coding: utf-8 -*-
"""GHA 内部署 Pages 网关：内嵌最新规则快照 + 新浪分级产物（零 binding 依赖）。

从 news 仓库本地读：
  tools/pages-gateway/_worker.js  —— worker 模板(含 __RULES_B64__/__NEWS_B64__ 占位)
  prompt_rules.md                 —— LLM 规则快照
  news_sina.json                  —— 新浪分级产物(refresh 脚本产出)
"""
import os, re, json, base64, uuid, hashlib, urllib.request, urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PROJECT = 'macropulse-api'

def main():
    acc = os.environ.get('CF_ACCOUNT_ID', ''); tok = os.environ.get('CF_TOKEN', '')
    if not (acc and tok):
        print('缺 CF 凭据，跳过 Pages 部署'); return 0
    rules = open(os.path.join(ROOT, 'prompt_rules.md'), encoding='utf-8').read()
    src = open(os.path.join(HERE, 'pages-gateway', '_worker.js'), encoding='utf-8').read()
    code = re.sub(r'const EMBEDDED_RULES_B64 = /\*__RULES_B64__\*/ "[^"]*";',
                  'const EMBEDDED_RULES_B64 = "' + base64.b64encode(rules.encode()).decode() + '";', src)
    news_path = os.environ.get('SINA_NEWS_JSON', os.path.join(ROOT, 'news_sina.json'))
    if os.path.exists(news_path):
        nb = open(news_path, 'rb').read()
        code = code.replace('/*__NEWS_B64__*/ ""', '"' + base64.b64encode(nb).decode() + '"')
        print('内嵌新闻产物 %d 字节' % len(nb))
    else:
        code = code.replace('/*__NEWS_B64__*/ ""', '""')
        print('warn: 无 news_sina.json，仅更新规则')
    body = code.encode('utf-8')
    manifest = {'_worker.js': hashlib.sha256(body).hexdigest()}
    b = '----MP' + uuid.uuid4().hex
    parts = [
        ('--%s\r\nContent-Disposition: form-data; name="manifest"\r\n\r\n%s\r\n' % (b, json.dumps(manifest))).encode(),
        ('--%s\r\nContent-Disposition: form-data; name="_worker.js"; filename="_worker.js"\r\nContent-Type: application/javascript\r\n\r\n' % b).encode(),
        body, ('\r\n--%s--\r\n' % b).encode(),
    ]
    req = urllib.request.Request(
        'https://api.cloudflare.com/client/v4/accounts/%s/pages/projects/%s/deployments' % (acc, PROJECT),
        data=b''.join(parts), method='POST',
        headers={'Authorization': 'Bearer ' + tok, 'User-Agent': 'mp',
                 'Content-Type': 'multipart/form-data; boundary=' + b})
    with urllib.request.urlopen(req, timeout=90) as r:
        d = json.loads(r.read().decode())
    assert d.get('success'), str(d.get('errors'))[:200]
    print('Pages 部署成功:', d['result'].get('url'))
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
