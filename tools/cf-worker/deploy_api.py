# -*- coding: utf-8 -*-
"""用 Cloudflare API 直接部署 Worker —— 不需要 Node / wrangler / 交互式登录。

用法（Windows Git Bash）：
  export CF_TOKEN='你的API Token'
  export CF_ACCOUNT_ID='你的Account ID'
  python tools/cf-worker/deploy_api.py

脚本会：验证 token → 确保 workers.dev 子域存在 → 上传 module worker
→ 启用 workers.dev 路由 → 请求 /health 验证。
"""
import io
import json
import os
import sys
import time
import uuid
import urllib.error
import urllib.request

NAME = 'macropulse-edge'
BASE = 'https://api.cloudflare.com/client/v4'
HERE = os.path.dirname(os.path.abspath(__file__))


def api(method, path, token, body=None, ctype='application/json'):
    data = None
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode('utf-8')
    headers = {'Authorization': 'Bearer ' + token}
    if data is not None:
        headers['Content-Type'] = ctype
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            raw = r.read().decode('utf-8')
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode('utf-8')
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {'raw': raw[:400]}
    except Exception as e:
        return -1, {'error': str(e)}


def show(label, status, obj):
    ok = obj.get('success') if isinstance(obj, dict) else False
    errs = (obj or {}).get('errors') or []
    msg = '; '.join(str(x.get('message', x)) for x in errs[:2]) if errs else ''
    print('  %-24s HTTP %s  success=%s %s' % (label, status, ok, msg))
    return ok


def main():
    token = os.environ.get('CF_TOKEN', '').strip()
    acc = os.environ.get('CF_ACCOUNT_ID', '').strip()
    if not token or not acc:
        print('缺少 CF_TOKEN 或 CF_ACCOUNT_ID')
        sys.exit(2)

    # GitHub PAT：优先环境变量，否则从同账号镜像仓库的 origin 读取（绝不写进代码/日志）
    gh_pat = os.environ.get('GH_PAT', '').strip()
    if not gh_pat:
        import re as _re
        import subprocess as _sp
        try:
            project = os.path.dirname(os.path.dirname(HERE))
            mirror_repo = os.path.join(os.path.dirname(project), 'macropulse-mirror')
            url = _sp.check_output(
                ['git', '-C', mirror_repo, 'remote', 'get-url', 'origin'],
                text=True, stderr=_sp.DEVNULL).strip()
            m = _re.match(r'https://([^@]+)@', url)
            if m:
                gh_pat = m.group(1)
        except Exception:
            pass
    if not gh_pat:
        print('缺少 GitHub PAT（GH_PAT，或镜像仓库 origin 内嵌凭据）')
        sys.exit(2)
    cron_key = os.environ.get('CRON_KEY', '').strip() or uuid.uuid4().hex
    print('GitHub PAT: 已取得（长度 %d，不打印明文）' % len(gh_pat))

    print('[1/5] 验证 token')
    st, r = api('GET', '/user/tokens/verify', token)
    ok = show('verify', st, r)
    if not ok:
        # cfat_ 新格式 token 对 /user/tokens/verify 会返回 1000 Invalid API Token，
        # 但 /accounts 完全可用（实测）。不能据此判定 token 无效，降级用账号接口验。
        st2, r2 = api('GET', '/accounts?per_page=50', token)
        ids = [x.get('id') for x in (r2.get('result') or [])]
        ok = bool(r2.get('success')) and acc in ids
        print('  verify via accounts   success=%s (token 可读取账号)' % ok)
    if not ok:
        print('  token 无效或权限不足（需要 Workers Scripts 编辑权限）')
        sys.exit(1)

    print('[2/5] 确认 workers.dev 子域')
    st, r = api('GET', '/accounts/%s/workers/subdomain' % acc, token)
    sub = (r.get('result') or {}).get('subdomain')
    if not sub:
        sub = 'mp-' + acc[:8].lower()
        st2, r2 = api('PUT', '/accounts/%s/workers/subdomain' % acc, token,
                      {'subdomain': sub})
        show('set subdomain', st2, r2)
    print('  子域:', sub)

    print('[2.5/5] 确保账号备份 KV 命名空间')
    kv_path = '/accounts/%s/storage/kv/namespaces' % acc
    st, r = api('GET', kv_path + '?per_page=50', token)
    ns_id = None
    for x in (r.get('result') or []):
        if x.get('title') == 'macropulse-account':
            ns_id = x.get('id')
            break
    if not ns_id:
        st, r = api('POST', kv_path, token, {'title': 'macropulse-account'})
        ns_id = (r.get('result') or {}).get('id')
        show('create kv namespace', st, r)
    if not ns_id:
        print('  KV 命名空间不可用，账号备选通道无法启用')
        sys.exit(1)
    print('  KV namespace:', ns_id)

    print('[3/5] 上传 worker.js（module 格式用 multipart）')
    code = io.open(os.path.join(HERE, 'worker.js'), 'rb').read()
    boundary = '----MP' + uuid.uuid4().hex
    meta = json.dumps({'main_module': 'worker.js',
                       'compatibility_date': '2026-11-10',
                       'bindings': [
                           {'type': 'secret_text', 'name': 'GH_TOKEN', 'text': gh_pat},
                           # 账号/备份代理复用同一 PAT；前端永不接触该值
                           {'type': 'secret_text', 'name': 'GH_PAT', 'text': gh_pat},
                           {'type': 'secret_text', 'name': 'CRON_KEY', 'text': cron_key},
                           # v446：CF 边缘 2 分钟驱动的 LLM 分级凭据（从本地 llm.env 读）
                           {'type': 'secret_text', 'name': 'ZHIPU_KEY', 'text': os.environ.get('ZHIPU_KEY', '')},
                           {'type': 'secret_text', 'name': 'GEMINI_KEY', 'text': os.environ.get('GEMINI_KEY', '')},
                           # v450：外部 webhook 已停用（免费额度2分钟连发2条会耗尽）。
                           # 告警只在 App 顶部红灯显示，不推微信。默认空，且 Worker 端不再发送。
                           {'type': 'secret_text', 'name': 'ALERT_WEBHOOK_URL', 'text': ''},
                           # 账号与设置备份存 Workers KV（GitHub PAT 无 Gist 写权限）
                           {'type': 'kv_namespace', 'name': 'ACCT_KV',
                            'namespace_id': ns_id}
                       ]}).encode('utf-8')
    body = (('--%s\r\n' % boundary).encode()
            + b'Content-Disposition: form-data; name="metadata"\r\n'
            + b'Content-Type: application/json\r\n\r\n' + meta
            + ('\r\n--%s\r\n' % boundary).encode()
            + b'Content-Disposition: form-data; name="worker.js"; filename="worker.js"\r\n'
            + b'Content-Type: application/javascript+module\r\n\r\n' + code
            + ('\r\n--%s--\r\n' % boundary).encode())
    st, r = api('PUT', '/accounts/%s/workers/scripts/%s' % (acc, NAME), token,
                body, 'multipart/form-data; boundary=' + boundary)
    if not show('upload', st, r):
        print('  上传失败，完整响应:', json.dumps(r, ensure_ascii=False)[:600])
        sys.exit(1)

    print('[4/5] 启用 workers.dev 路由')
    st, r = api('POST', '/accounts/%s/workers/scripts/%s/subdomain' % (acc, NAME),
                token, {'enabled': True})
    show('enable subdomain', st, r)

    print('[4.5/5] 设置 Cron Triggers（v412：UTC 07 点每15分钟=北京15:00/15:15/15:30/15:45，一条无兜底）')
    # 免费版 Cron Triggers 上限是「每账号 5 条」，同账号还有别的 Worker 在用。
    # v276：把原 17:23 兜底提前到 16:00；15:50 之后仍有每10分钟主窗没必要继续，
    # 直接改为两条：主窗 15:00~15:50 + 16:00 明确兜底。
    # 就绪由 workflow 探针 + 脚本 wait_ready 决定；no_wait 已改 false。
    # v278（2026-11-12 用户决定）：合并为**一条**表达式。
    # 原来两条（`*/10 7` 主窗 + `0 8` 16:00 兜底）语义重复且只覆盖到 16:00，
    # 16:00 之后万一失败就要等到第二天；合并成 */10 7,8 后连续覆盖
    # 北京 15:00–16:50 每 10 分钟，既省一个槽位（3/5 → 2/5）又更稳。
    # 多出来的几次会被 worker 里的 todayPublished() 闸门跳过，成本可忽略。
    # v340（用户口径）：15:00 起每10分钟检查，跑出当天数据后由 Worker 自动跳过；
    # 16:00 只兜底一次，不再延续到 16:50，减少无谓 dispatch。
    # v412（2026-11-17 用户决定）：合并为**一条**表达式，15–16 点每 15 分钟。
    # 用户口径：① 验证过跑得成第 1 轮就成了，15 点那次与 16 点数据一样，
    # 无需单独 16:00 兜底（一天至少跑两次纯浪费 GitHub 分钟）；
    # ② 冷却逻辑本身就是 15 分钟，cron 15 分钟一轮语义一致且不再需要兜底条；
    # ③ 要确保的只是「15–16 点之间跑出一次数据」——todayPublished() 有数据即停，
    # 多余时点零 dispatch。覆盖 15:00/15:15/15:30/15:45（UTC 7 点段）。
    # 只占 1 个槽位（3/5 → 2/5）。
    crons = [
        {'cron': '*/15 7 * * 1-5'},   # 北京 15:00/15:15/15:30/15:45 每15分钟（breadth跑批）
        {'cron': '30 23 * * *'},      # v447：北京 07:30 早盘班（收割隔夜美股/美联储/欧美夜盘）
        {'cron': '45 12 * * *'},      # v447：北京 20:45 晚盘班（覆盖20:30美国CPI/非农，迎美股开盘）
        {'cron': '*/1 * * * *'},      # v448：CF边缘快讯增量驱动（1分钟粒度，无新增短路，非东财源）
    ]
    sched_path = '/accounts/%s/workers/scripts/%s/schedules' % (acc, NAME)
    want = sorted(c['cron'] for c in crons)

    def _read_sched():
        st2, r2 = api('GET', sched_path, token)
        got = (r2 or {}).get('result') or {}
        items = got.get('schedules') or got.get('result') or []
        return sorted(str(x.get('cron') or '') for x in items if isinstance(x, dict))

    st, r = api('PUT', sched_path, token, crons)
    if not show('set schedules', st, r):
        st, r = api('PUT', sched_path, token, {'schedules': crons})
        show('set schedules (wrapped)', st, r)

    # v295【部署后强制回读校验】PUT 的响应体不代表云端真实状态。
    # 11-13 就出现过「部署成功」但 cron 实际不在：当天 12 个时点全部空转、
    # 跑批漏了一整天（CF Analytics 显示 macropulse-edge 当天 0 次调用）。
    # 这里回读并逐条比对，不一致重试一次，仍不一致直接失败退出，绝不报「部署完成」。
    got = _read_sched()
    print('  期望 schedules:', want)
    print('  云端 schedules:', got)
    if got != want:
        print('  !! 云端与期望不一致，重试 PUT ...')
        api('PUT', sched_path, token, crons)
        time.sleep(3)
        got = _read_sched()
        print('  重试后 schedules:', got)
    if got != want:
        print('  !! Cron 注册失败：云端 %s != 期望 %s' % (got, want))
        print('  !! 这会导致整天不跑批，请勿认为部署完成。')
        return 1
    print('  Cron 回读校验通过（%d 条）' % len(want))

    url = 'https://%s.%s.workers.dev' % (NAME, sub)
    print('[5/5] 健康检查 + Cron 链路自检')
    try:
        with urllib.request.urlopen(url + '/health', timeout=20) as resp:
            print('  /health ->', resp.status, resp.read().decode()[:120])
    except Exception as e:
        print('  /health 暂不可达（workers.dev 国内常被污染，不影响 Cron 内部触发）:', str(e)[:100])
    # v361【部署不得触发真实跑批】/__cron 会真发 workflow_dispatch。
    # 风险管控要求「一次只跑一轮」，部署阶段设 MP_SKIP_CRON_SELFCHECK=1
    # 跳过它；要跑批请用 check_cron.py --backfill 显式触发，次数完全可控。
    if os.environ.get('MP_SKIP_CRON_SELFCHECK') == '1':
        print('  /__cron 自检跳过（避免部署意外触发跑批）')
    else:
        try:
            req = urllib.request.Request(url + '/__cron', data=b'', method='POST',
                                         headers={'X-MP-Key': cron_key,
                                                  'User-Agent': 'MacroPulse-Edge/2.0'})
            with urllib.request.urlopen(req, timeout=25) as resp:
                print('  /__cron ->', resp.status, resp.read().decode()[:160])
        except Exception as e:
            print('  /__cron 自检失败（不影响 Cron 内部触发）:', str(e)[:120])

    print('\n部署完成：', url)
    print('Cron 已在云端注册，不依赖电脑、不依赖 GitHub schedule。')


if __name__ == '__main__':
    sys.exit(main() or 0)
