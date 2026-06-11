"""
全 API 機能のスモークテスト（1 エンドポイントあたり 1 リクエスト）。

x-client-transaction-id 生成の修正（issue #312 / sign.o-*.js 対応）が
実際の X API 全エンドポイントで通ることを確認するためのスクリプト。

- accounts.db のアカウントをそのまま使う（ログイン済み Cookie 前提）
- 各リクエストの間に SLEEP 秒の待機を入れる（レート制限対策）
- 各 high-level メソッドを 1 回ずつ叩く。`*_raw` 版は同じエンドポイントを
  叩くだけなので省略。
- 各リクエスト前に active なアカウント数を確認し、0 になったら以降を SKIP する
  （アカウントが無い状態の「0 件 = 偽 OK」を防ぐため）。

ログ出力:
  実行ごとに LOG_ROOT/<日時>/ ディレクトリを作り、以下を全部書き出す。
    - run.log                : コンソールと同じ実行ログ
    - NN_<endpoint>.json     : そのエンドポイントで取得した「全データ」(JSON)
    - summary.json           : 各エンドポイントの結果サマリ
  → 取ってきた内容が全部ここで確認できる。

注意:
  user_by_id（UserByRestId）は X 側で廃止済みで 403 を返す。twscrape は 403 を
  「セッション失効/BAN」とみなして該当アカウントを inactive 化し、リトライで全
  アカウントを消費してしまう。そのため user_by_id は **最後** に実行する。
  終了時には「開始時 active だったのに error_msg 無しで inactive 化された分だけ」
  を復元する（RESTORE_ACCOUNTS）。手動で inactive にしたアカウントや BAN 検出
  （error_msg 付き）はそのまま尊重し、勝手に active 化しない。

使い方:
    python3 smoke_test.py
必要に応じて下の CONFIG を書き換えてください。
"""

import asyncio
import json
import re
import time
import traceback
from datetime import datetime
from pathlib import Path

from twscrape import API, gather
from twscrape.logger import set_log_level

# ---------------------------------------------------------------- CONFIG
DB_FILE = "accounts.db"          # アカウント DB
SLEEP = 5.0                      # 各リクエスト間の待機秒数
LIMIT = 20                       # ページング系メソッドの取得上限（≒1ページ=1リクエスト）
RESTORE_ACCOUNTS = True          # 終了時に「開始時 active かつ error_msg 無しで inactive 化された
                                 # 分だけ」復元する（最後の user_by_id で落ちた分を戻す）。
                                 # 手動で inactive にしたアカウントや BAN 検出（error_msg 付き）は触らない。
LOG_ROOT = "smoke_test_logs"     # ログ出力先のルートディレクトリ

SEARCH_Q = "elon musk"           # 検索クエリ
USER_LOGIN = "xdevelopers"       # user_by_login 用
USER_ID = 2244994945             # user 系（@XDevelopers）
TWEET_ID = 20                    # tweet 系（Jack の最初のツイート, 永続）
LIST_ID = 1494877848087187461    # 公開リスト（list_timeline 用）
TREND = "news"                   # trends 用
LOG_LEVEL = "ERROR"              # twscrape 内部ログ（DEBUG にすると詳細が見える）
# ------------------------------------------------------------------------


class Tee:
    """コンソールと run.log の両方へ書く簡易ロガー。"""

    def __init__(self, path: Path):
        self._fh = open(path, "w", encoding="utf-8")

    def print(self, msg: str = "", end: str = "\n"):
        print(msg, end=end, flush=True)
        self._fh.write(msg + end)
        self._fh.flush()

    def close(self):
        self._fh.close()


async def snapshot_active(api: API) -> dict[str, bool]:
    """開始時の各アカウントの active 状態を記録する。"""
    return {a.username: a.active for a in await api.pool.get_all()}


async def restore_active(api: API, snapshot: dict[str, bool]) -> int:
    """開始時 active だったのに error_msg 無しで inactive 化された分だけ復元。
    手動 inactive（開始時から非 active）や BAN 検出（error_msg 付き）は触らない。"""
    restored = 0
    for a in await api.pool.get_all():
        if snapshot.get(a.username) and not a.active and not a.error_msg:
            await api.pool.set_active(a.username, True)
            restored += 1
    return restored


async def active_count(api: API) -> int:
    return sum(1 for a in await api.pool.get_all() if a.active)


def _slug(label: str) -> str:
    s = re.sub(r"[^0-9A-Za-z_]+", "_", label).strip("_")
    return s or "endpoint"


def _serialize(res):
    """モデル(.dict() を持つ)・リスト・None を JSON 可能な形に変換。"""
    if res is None:
        return None
    if isinstance(res, list):
        return [x.dict() if hasattr(x, "dict") else x for x in res]
    return res.dict() if hasattr(res, "dict") else res


def _summary(res) -> str:
    if res is None:
        return "None（見つからない／取得不可）"
    if isinstance(res, list):
        head = ""
        if res:
            first = res[0]
            ident = getattr(first, "id", None) or getattr(first, "username", None)
            head = f" 例: {type(first).__name__}#{ident}"
        return f"{len(res)} 件{head}"
    ident = getattr(res, "id", None) or getattr(res, "username", None)
    return f"{type(res).__name__}#{ident}"


async def run_one(api: API, log: Tee, run_dir: Path, idx: int, total: int, label: str, factory):
    """1 ケース実行 → (status, count) を返し、全データを NN_<endpoint>.json に保存。"""
    before = await active_count(api)
    log.print(f"[{idx:>2}/{total}] {label:<24} ", end="")

    if before == 0:
        log.print("SKIP  -> active アカウントが 0（以降は実リクエストできません）")
        return "SKIP", 0

    t0 = time.time()
    try:
        res = await factory()
        dt = time.time() - t0
        after = await active_count(api)
        drop = f"  ⚠ active {before}->{after}" if after < before else ""
        status = "WARN" if res is None else "OK"
        log.print(f"{status}  ({dt:.1f}s)  -> {_summary(res)}{drop}")
    except Exception as e:
        dt = time.time() - t0
        log.print(f"FAIL  ({dt:.1f}s)  -> {type(e).__name__}: {e}")
        log.print(traceback.format_exc())
        _dump(run_dir, idx, label, {"status": "FAIL", "error": f"{type(e).__name__}: {e}"})
        return "FAIL", 0

    data = _serialize(res)
    count = len(data) if isinstance(data, list) else (0 if data is None else 1)
    _dump(run_dir, idx, label, {
        "endpoint": label,
        "status": status,
        "elapsed_sec": round(dt, 2),
        "count": count,
        "data": data,
    })
    return status, count


def _dump(run_dir: Path, idx: int, label: str, payload: dict):
    path = run_dir / f"{idx:02d}_{_slug(label)}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str)


async def main():
    set_log_level(LOG_LEVEL)
    api = API(DB_FILE)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = Path(LOG_ROOT) / stamp
    run_dir.mkdir(parents=True, exist_ok=True)
    log = Tee(run_dir / "run.log")

    snapshot = await snapshot_active(api)

    accs = await api.pool.get_all()
    log.print(f"ログ出力先: {run_dir}/")
    log.print(f"アカウント: 全 {len(accs)} 件 / active {await active_count(api)} 件")
    log.print(f"設定: sleep={SLEEP}s limit={LIMIT} user_id={USER_ID} tweet_id={TWEET_ID} list_id={LIST_ID}")
    log.print("注: 最初の 1 リクエストで x-client-transaction-id 用アセット取得が走るため初回だけ遅いです。")
    log.print("注: user_by_id は X 側廃止で 403 を返しアカウントを消費するため最後に実行します。\n")

    # (ラベル, 実行関数)。各エンドポイント 1 回ずつ。user_by_id は破壊的なので最後。
    cases = [
        ("user_by_login",            lambda: api.user_by_login(USER_LOGIN)),
        ("search",                   lambda: gather(api.search(SEARCH_Q, limit=LIMIT))),
        ("search_user",              lambda: gather(api.search_user(SEARCH_Q, limit=LIMIT))),
        ("search_trend",             lambda: gather(api.search_trend(SEARCH_Q, limit=LIMIT))),
        ("tweet_details",            lambda: api.tweet_details(TWEET_ID)),
        ("tweet_replies",            lambda: gather(api.tweet_replies(TWEET_ID, limit=LIMIT))),
        ("retweeters",               lambda: gather(api.retweeters(TWEET_ID, limit=LIMIT))),
        ("followers",                lambda: gather(api.followers(USER_ID, limit=LIMIT))),
        ("verified_followers",       lambda: gather(api.verified_followers(USER_ID, limit=LIMIT))),
        ("following",                lambda: gather(api.following(USER_ID, limit=LIMIT))),
        ("subscriptions",            lambda: gather(api.subscriptions(USER_ID, limit=LIMIT))),
        ("user_tweets",              lambda: gather(api.user_tweets(USER_ID, limit=LIMIT))),
        ("user_tweets_and_replies",  lambda: gather(api.user_tweets_and_replies(USER_ID, limit=LIMIT))),
        ("user_media",               lambda: gather(api.user_media(USER_ID, limit=LIMIT))),
        ("list_timeline",            lambda: gather(api.list_timeline(LIST_ID, limit=LIMIT))),
        ("trends",                   lambda: gather(api.trends(TREND))),
        ("bookmarks",                lambda: gather(api.bookmarks(limit=LIMIT))),
        ("user_by_id (廃止/破壊的)",   lambda: api.user_by_id(USER_ID)),
    ]

    total = len(cases)
    results: list[tuple[str, str, int]] = []
    for i, (label, factory) in enumerate(cases, start=1):
        status, count = await run_one(api, log, run_dir, i, total, label, factory)
        results.append((label, status, count))
        if i < total:
            await asyncio.sleep(SLEEP)

    if RESTORE_ACCOUNTS:
        n = await restore_active(api, snapshot)
        log.print(f"\n（テスト後: 開始時 active だったアカウント {n} 件を復元しました）")

    # サマリ（コンソール + summary.json）
    counts: dict[str, int] = {}
    log.print("\n================ サマリ ================")
    for label, status, count in results:
        counts[status] = counts.get(status, 0) + 1
        log.print(f"  {status:<4}  {label:<26} {count} 件")
    log.print("---------------------------------------")
    order = ["OK", "WARN", "FAIL", "SKIP"]
    log.print("  " + "  ".join(f"{k}={counts.get(k, 0)}" for k in order) + f"  / 全{total}")
    log.print("  ※ WARN=None 返却（廃止 or 該当データ無し）, SKIP=active アカウント枯渇で未実行")
    log.print(f"\n取得した全データは {run_dir}/ 配下の NN_*.json に保存しました。")

    with open(run_dir / "summary.json", "w", encoding="utf-8") as f:
        json.dump(
            {
                "timestamp": stamp,
                "totals": counts,
                "results": [
                    {"endpoint": label, "status": status, "count": count}
                    for label, status, count in results
                ],
            },
            f, ensure_ascii=False, indent=2,
        )

    log.close()


if __name__ == "__main__":
    asyncio.run(main())
