# -*- coding: utf-8 -*-
"""部署 Pages Functions 网关 + 同步规则快照到 Pages 环境变量。

设计（用户 2026-11-18 定调）：
  · CF Pages 免费托管，*.pages.dev 国内可达（实测 0.6~1.2s），绕开 workers.dev DNS 污染。
  · /rules 规则从 Pages 环境变量 PROMPT_RULES 读（GHA push docs/ 后本脚本自动更新）。
  · /__proxy 快讯增量抓取，CORS=* 放行前端静态 H5 跨域直连。
  · 东财绝不经此网关（红线：东财由 GHA/CF 跑批直连，本机/浏览器代理不碰）。

用法：
  python3 tools/cf-worker/deploy_pages_gateway.py
"""
import os, re, json, urllib.request, urllib.error, uuid, hashlib, sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))  # 前端项目根
GHA = 'E:/WorkBuddy工作区/insum-cloud'
PROJECT = 'macropulse-api'

def _creds():
    a, t = os.environ.get('CF_ACCOUNT_ID', ''), os.environ.get('CF_TOKEN', '')
    if not (a and t):
        env = 'E:/WorkBuddy工作区/_tools/cf.env'
        if os.path.exists(env):
            for ln in open(env, encoding='utf-8'):
                if '=' in ln and not ln.startswith('#'):
                    k, v = ln.strip().split('=', 1); os.environ.setdefault(k, v.strip('"\''))
        a, t = os.environ.get('CF_ACCOUNT_ID', ''), os.environ.get('CF_TOKEN', '')
    if not (a and t): raise RuntimeError('缺 CF_TOKEN/CF_ACCOUNT_ID')
    return a, t

def api(acc, tok, method, path, body=None, raw=False, headers=None):
    data = None if body is None else (body if raw else json.dumps(body).encode())
    h = {'Authorization': 'Bearer ' + tok, 'User-Agent': 'mp'}
    if not raw: h['Content-Type'] = 'application/json'
    if headers: h.update(headers)
    req = urllib.request.Request('https://api.cloudflare.com/client/v4' + path,
        data=data, method=method, headers=h)
    with urllib.request.urlopen(req, timeout=90) as r:
        return r.status, json.loads(r.read().decode() or '{}')

def main():
    acc, tok = _creds()
    # 1) KV 已绑定(Pages deployment_configs)，规则与新闻都从 KV 实时读，不再内嵌
    rules = open(os.path.join(GHA, 'prompt_rules.md'), encoding='utf-8').read()
    NS = os.environ.get('CF_KV_NS', '2b7a91bb9ec945ebafbc41393c6fd8f8')
    # 同步规则快照到 KV（/rules 实时读）
    api(acc, tok, 'PUT', '/accounts/%s/storage/kv/namespaces/%s/values/mp_prompt_rules' % (acc, NS),
        body=rules.encode('utf-8'), raw=True)
    print('[1/2] 规则快照 %d 字已写 KV' % len(rules))
    # 2) 直接上传 _worker.js（KV 绑定生效，Functions 从 env.ACCT_KV 读）
    worker_js = os.path.join(ROOT, '_tools', 'pages-gateway', '_worker.js')
    code = open(worker_js, encoding='utf-8').read().encode('utf-8')
    manifest = {'_worker.js': hashlib.sha256(code).hexdigest()}
    b = '----MP' + uuid.uuid4().hex
    parts = [
        ('--%s\r\nContent-Disposition: form-data; name="manifest"\r\n\r\n%s\r\n' % (b, json.dumps(manifest))).encode(),
        ('--%s\r\nContent-Disposition: form-data; name="_worker.js"; filename="_worker.js"\r\nContent-Type: application/javascript\r\n\r\n' % b).encode(),
        code, ('\r\n--%s--\r\n' % b).encode(),
    ]
    st, d = api(acc, tok, 'POST', '/accounts/%s/pages/projects/%s/deployments' % (acc, PROJECT),
                body=b''.join(parts), raw=True,
                headers={'Content-Type': 'multipart/form-data; boundary=' + b})
    assert d.get('success'), '部署失败: %s' % str(d.get('errors'))[:200]
    print('[2/2] 已部署: %s' % d['result'].get('url'))
    print('网关地址: https://%s.pages.dev' % PROJECT)
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
