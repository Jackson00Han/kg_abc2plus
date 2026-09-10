#!/usr/bin/env python3
"""Restart only this checkout's 8002 workbench, retaining its existing database."""
from __future__ import annotations

import fcntl
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
from urllib.parse import urlparse

from dotenv import dotenv_values
import httpx
from neo4j import GraphDatabase

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / '.local' / 'workbench'
CONFIG = ROOT / '.env.workbench.local'
URL = 'http://127.0.0.1:8002/industrial'


def listener_pids() -> list[int]:
    result = subprocess.run(['lsof', '-nP', '-iTCP:8002', '-sTCP:LISTEN', '-t'],
                            text=True, capture_output=True)
    if result.returncode not in (0, 1):
        raise RuntimeError('无法检查 8002 端口；请确认 lsof 可用。')
    return sorted({int(value) for value in result.stdout.split()})


def belongs_to_workbench(pid: int) -> bool:
    command = subprocess.run(['ps', '-p', str(pid), '-o', 'command='],
                             text=True, capture_output=True).stdout
    cwd = subprocess.run(['lsof', '-a', '-p', str(pid), '-d', 'cwd', '-Fn'],
                         text=True, capture_output=True).stdout.splitlines()
    return (('scripts/run_playground.py' in command or 'scripts.run_playground' in command)
            and re.search(r'--port(?:\s+|=)8002(?:\s|$)', command) is not None
            and 'n' + str(ROOT) in cwd)


def private_environment() -> dict[str, str]:
    if not CONFIG.is_file():
        raise RuntimeError('缺少 .env.workbench.local，请按 quick_start.md 配置现有数据库；不会新建数据库。')
    local = dotenv_values(CONFIG)
    environment = {**dotenv_values(ROOT / '.env'), **os.environ, **local}
    required = ('OPENAI_API_KEY', 'OPENAI_BASE_URL', 'MODEL_NAME', 'EMBEDDING_MODEL',
                'EMBEDDING_DIMENSIONS', 'PLAYGROUND_NEO4J_URI', 'PLAYGROUND_NEO4J_USER',
                'PLAYGROUND_NEO4J_PASSWORD', 'PLAYGROUND_NEO4J_DATABASE')
    missing = [name for name in required if not environment.get(name)]
    if missing:
        raise RuntimeError('缺少配置项：' + ', '.join(missing))
    uri = urlparse(environment['PLAYGROUND_NEO4J_URI'])
    if uri.scheme not in ('bolt', 'neo4j') or uri.hostname not in ('127.0.0.1', 'localhost', '::1'):
        raise RuntimeError('快速启动仅允许本机现有 Neo4j。')
    if environment['PLAYGROUND_NEO4J_DATABASE'] != 'neo4j':
        raise RuntimeError('当前工作台只支持既有 neo4j 数据库。')
    # This entry point always preserves the user's current data-isolation choice.
    environment['PLAYGROUND_PUMP_ONLY'] = '1'
    environment['PLAYGROUND_ALLOW_DISPOSABLE_DB'] = '1'
    return {key: str(value) for key, value in environment.items() if value is not None}


def ensure_database(environment: dict[str, str]) -> None:
    container = environment.get('WORKBENCH_NEO4J_CONTAINER')
    if container:
        result = subprocess.run(['docker', 'inspect', '--format', '{{.State.Running}}', container],
                                capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError('Docker 未启动或原数据库容器不存在；请先打开 Docker Desktop。不会创建替代数据库。')
        if result.stdout.strip() != 'true':
            result = subprocess.run(['docker', 'start', container], capture_output=True, text=True)
            if result.returncode:
                raise RuntimeError('无法启动原数据库容器，请检查 Docker Desktop。')
    with GraphDatabase.driver(environment['PLAYGROUND_NEO4J_URI'],
            auth=(environment['PLAYGROUND_NEO4J_USER'], environment['PLAYGROUND_NEO4J_PASSWORD']),
            connection_timeout=2) as driver:
        deadline = time.monotonic() + 45
        while True:
            try:
                driver.verify_connectivity()
                break
            except Exception:
                if time.monotonic() >= deadline:
                    raise RuntimeError('原数据库暂不可连接；现有服务未停止，请核对本地数据库配置。') from None
                time.sleep(1)


def restart() -> None:
    environment = private_environment()
    python = ROOT / '.venv' / 'bin' / 'python'
    if not python.is_file():
        raise RuntimeError('缺少项目 .venv，请先安装项目依赖。')
    pids = listener_pids()
    if any(not belongs_to_workbench(pid) for pid in pids):
        raise RuntimeError('8002 被其他程序或其他项目占用；为避免误关，已停止重启。')
    print('检查原数据库连接…', flush=True)
    ensure_database(environment)
    for pid in pids:
        # Recheck identity immediately before signalling, never trust a stale PID file.
        if not belongs_to_workbench(pid):
            raise RuntimeError('进程状态已变化，请重新执行。')
        print(f'正在停止本项目的 8002 服务（PID {pid}）…', flush=True)
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + 35
        while True:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError('服务仍在收尾，未强制终止后台任务；请稍后重新运行。')
            time.sleep(.2)
    if listener_pids():
        raise RuntimeError('8002 端口仍被占用，未启动重复服务。')
    log_path = STATE / '8002.log'
    with log_path.open('a') as log:
        log_path.chmod(0o600)
        process = subprocess.Popen([str(python), 'scripts/run_playground.py',
            '--enable-industrial', '--reuse-existing-corpus', '--skip-provider-warmup',
            '--host', '127.0.0.1', '--port', '8002', '--no-open'], cwd=ROOT,
            env=environment, stdin=subprocess.DEVNULL, stdout=log,
            stderr=subprocess.STDOUT, start_new_session=True)
    (STATE / '8002.pid').write_text(str(process.pid) + '\n')
    deadline = time.monotonic() + 90
    with httpx.Client(timeout=2, trust_env=False) as client:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f'服务启动失败，日志：{log_path}')
            try:
                result = client.get('http://127.0.0.1:8002/playground/bootstrap')
                if result.status_code == 200 and result.json().get('data_scope') == 'pump-only':
                    print(f'服务已启动：{URL}\nPID：{process.pid}\n日志：{log_path}', flush=True)
                    print('现有数据已保留；仅水泵测试包模式开启。关闭本终端后服务继续运行。')
                    return
            except (httpx.HTTPError, ValueError):
                pass
            time.sleep(.5)
    raise RuntimeError(f'服务仍在启动，请检查日志后再操作：{log_path}')


def main() -> int:
    os.umask(0o077)
    STATE.mkdir(parents=True, exist_ok=True)
    with (STATE / 'restart.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('已有重启操作正在进行，请稍候。', file=sys.stderr)
            return 1
        try:
            restart()
        except (RuntimeError, OSError) as error:
            print(str(error), file=sys.stderr)
            return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
