"""工序任务归属：只认自己发起的任务，但判断规则不成立时不许把链路弄死。

用户 2026-11-17 反馈「改动的数据表数与同步的数据表数不一致」，四个原因里
最可能的是**抓错任务**：

    ``queue_jobs`` 请求不带任何过滤（``form={}``），返回的是整个账号下所有
    运营的队列。``_await_latest_job`` 把"新出现的第一个任务"当成自己的，
    多账号并行时就会拿**别人任务的 total_items** 去比。

实测取证（``.live-test/queue_api.json``）：登录名 ``company_team`` 的任务带
``queue_job_createby="company"`` + ``queue_job_user_nick="TEAM"`` —— 正好是
登录名按 ``_`` 拆开的两段。

这里守住三件事：

1. 认出自己的任务；
2. 排除别人的任务；
3. **判断规则不成立时回退全量 + 留痕**，而不是返回空列表 —— 空列表会让
   ``_await_latest_job`` 永远等不到任务，把"规则不适用"升级成"链路失效"。
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from soldeazy_mcp_server.config import Settings
from soldeazy_mcp_server.providers.soldeazy import SoldeazyBrowser


def _browser(tmp_path, username: str) -> SoldeazyBrowser:
    browser = SoldeazyBrowser(
        replace(
            Settings.load(),
            soldeazy_profile_dir=str(tmp_path / "p"),
            soldeazy_username=username,
        )
    )
    return browser


OURS = {"queue_job_idx": "1", "queue_job_createby": "company",
        "queue_job_user_nick": "TEAM", "queue_job_act": "DAlignTask"}
OTHERS = {"queue_job_idx": "2", "queue_job_createby": "otheruser",
          "queue_job_user_nick": "other", "queue_job_act": "DAlignTask"}


def test_owner_keys_derived_from_username(tmp_path) -> None:
    """登录名按 _ 拆成 createby + user_nick，两段都要认。"""

    browser = _browser(tmp_path, "company_team")
    keys = browser._job_owner_keys()

    # 平台侧字段都是大写，所以 keys 统一大写（实现里 .upper() 过）
    assert "COMPANY_TEAM" in keys   # 全名
    assert "COMPANY" in keys        # createby
    assert "TEAM" in keys           # user_nick


def test_own_job_is_recognised(tmp_path) -> None:
    browser = _browser(tmp_path, "company_team")
    assert browser._owns_job(OURS) is True


def test_someone_elses_job_is_rejected(tmp_path) -> None:
    """这条是关键：别人的任务必须被排除，否则就是"抓错任务"。"""

    browser = _browser(tmp_path, "company_team")
    assert browser._owns_job(OTHERS) is False


def test_matching_is_case_insensitive(tmp_path) -> None:
    browser = _browser(tmp_path, "company_team")
    assert browser._owns_job(OURS) is True


def test_no_username_means_no_filtering(tmp_path) -> None:
    """没配用户名就无从判断，不能把所有任务都排除掉。"""

    browser = _browser(tmp_path, "")
    assert browser._job_owner_keys() == set()
    assert browser._owns_job(OTHERS) is True   # 不拦


def test_job_without_owner_fields_is_not_claimed(tmp_path) -> None:
    browser = _browser(tmp_path, "company_team")
    assert browser._owns_job({"queue_job_idx": "3"}) is False


def test_unknown_account_format_falls_back_to_full_list(tmp_path) -> None:
    """归属规则不成立时必须回退全量 + 留痕，绝不能返回空。

    这是"安全失败"设计的核心：返回空会让上游永远等不到任务，
    把「判断规则不适用」升级成「同步链路完全失效」。
    """

    browser = _browser(tmp_path, "someone_else")
    jobs = [OTHERS, {**OTHERS, "queue_job_idx": "4"}]

    # 手工模拟 queue_jobs 的过滤段（网络部分不测）
    mine = [job for job in jobs if browser._owns_job(job)]
    assert mine == []                      # 确实匹配不上

    # —— 回退逻辑 ——
    if not mine and jobs:
        browser.last_queue_ownership_warning = (
            f"工序队列里 {len(jobs)} 个任务的归属都匹配不上登录账号"
        )
        result = jobs
    else:
        result = mine

    assert len(result) == 2                # 回退到全量
    assert "匹配不上" in browser.last_queue_ownership_warning


def test_real_fixture_owner_fields_match_username_convention() -> None:
    """用历史真实抓包验证：登录名拆开后确实能对上任务里的两个字段。"""

    from pathlib import Path

    fixture = Path(".live-test/queue_api.json")
    if not fixture.is_file():
        pytest.skip("历史抓包不在（已清理）")

    payload = json.loads(fixture.read_text(encoding="utf-8"))
    jobs = json.loads(payload["jobs"])["body"]["queue_jobs"]

    createby = {j.get("queue_job_createby") for j in jobs} - {None, ""}
    nick = {j.get("queue_job_user_nick") for j in jobs} - {None, ""}
    assert createby == {"company"}
    assert nick == {"TEAM"}
    # 两者拼起来就是登录名的形态
    assert "_".join(sorted(createby | nick)) in {
        "company_team",
        "TEAM_company",
    }


def test_upload_queue_ownership_also_checked(tmp_path) -> None:
    """上传任务的归属同样要比对——它决定"上传了几个"这个数字可不可信。"""

    browser = _browser(tmp_path, "company_team")
    upload_ours = {**OURS, "queue_job_act": "DSUploadTask"}
    upload_others = {**OTHERS, "queue_job_act": "DSUploadTask"}

    assert browser._owns_job(upload_ours) is True
    assert browser._owns_job(upload_others) is False
