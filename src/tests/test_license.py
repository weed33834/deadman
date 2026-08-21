"""授权码模块测试（B2B-IMPLEMENTATION Step 7.1 验收）

覆盖：
  1. sign_license / verify_license：签名往返；篡改签名 / 过期 → None
  2. license_status：无码 → trial；有效码 → licensed；过期码 → expired(只读)
  3. 授权状态端点 GET /api/license/status
"""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

from deadman.billing import license as lic

SECRET = "test-license-secret-0123456789abcdef"


# =====================================================================
# 纯函数：sign / verify
# =====================================================================
def test_sign_verify_roundtrip():
    token = lic.sign_license(SECRET, {"licensee": "示例殡仪馆", "plan": "enterprise", "exp": time.time() + 3600})
    payload = lic.verify_license(SECRET, token)
    assert payload is not None
    assert payload["licensee"] == "示例殡仪馆"
    assert payload["plan"] == "enterprise"


def test_verify_rejects_tampered_signature():
    token = lic.sign_license(SECRET, {"licensee": "A", "exp": time.time() + 3600})
    body, sig = token.split(".", 1)
    tampered = body + ".A" * len(sig)
    assert lic.verify_license(SECRET, tampered) is None


def test_verify_rejects_wrong_secret():
    token = lic.sign_license(SECRET, {"licensee": "A", "exp": time.time() + 3600})
    assert lic.verify_license("other-secret", token) is None


def test_verify_rejects_expired():
    token = lic.sign_license(SECRET, {"licensee": "A", "exp": time.time() - 10})
    assert lic.verify_license(SECRET, token) is None


def test_verify_rejects_garbage():
    assert lic.verify_license(SECRET, "not-a-token") is None
    assert lic.verify_license(SECRET, "") is None


# =====================================================================
# license_status
# =====================================================================
def test_status_trial_without_token(monkeypatch):
    monkeypatch.delenv("DEADMAN_LICENSE_KEY", raising=False)
    monkeypatch.delenv("DEADMAN_LICENSE_SECRET", raising=False)
    monkeypatch.setattr(lic, "LICENSE_FILE", __import__("pathlib").Path("nonexistent_lic"))
    st = lic.license_status()
    assert st["status"] == "trial"
    assert st["readonly"] is False


def test_status_licensed(monkeypatch, tmp_path):
    token = lic.sign_license(SECRET, {"licensee": "机构X", "plan": "pro", "exp": time.time() + 86400})
    monkeypatch.setenv("DEADMAN_LICENSE_KEY", token)
    monkeypatch.setenv("DEADMAN_LICENSE_SECRET", SECRET)
    st = lic.license_status()
    assert st["status"] == "licensed"
    assert st["readonly"] is False
    assert st["licensee"] == "机构X"
    assert st["plan"] == "pro"


def test_status_expired_readonly(monkeypatch):
    token = lic.sign_license(SECRET, {"licensee": "机构X", "exp": time.time() - 60})
    monkeypatch.setenv("DEADMAN_LICENSE_KEY", token)
    monkeypatch.setenv("DEADMAN_LICENSE_SECRET", SECRET)
    st = lic.license_status()
    assert st["status"] == "expired"
    assert st["readonly"] is True


def test_status_licensed_from_file(monkeypatch, tmp_path):
    token = lic.sign_license(SECRET, {"licensee": "文件机构", "exp": time.time() + 3600})
    lic_file = tmp_path / "license.lic"
    lic_file.write_text(token, encoding="utf-8")
    monkeypatch.delenv("DEADMAN_LICENSE_KEY", raising=False)
    monkeypatch.setenv("DEADMAN_LICENSE_SECRET", SECRET)
    monkeypatch.setattr(lic, "LICENSE_FILE", lic_file)
    st = lic.license_status()
    assert st["status"] == "licensed"
    assert st["licensee"] == "文件机构"


def test_status_token_without_secret_trials(monkeypatch):
    token = lic.sign_license(SECRET, {"licensee": "A", "exp": time.time() + 3600})
    monkeypatch.setenv("DEADMAN_LICENSE_KEY", token)
    monkeypatch.delenv("DEADMAN_LICENSE_SECRET", raising=False)
    st = lic.license_status()
    assert st["status"] == "trial"


def test_trial_remaining_days(monkeypatch, tmp_path):
    import deadman.billing.license as lic_mod

    # 首次调用写入 marker，返回满 30 天
    monkeypatch.setattr(lic_mod, "_TRIAL_MARKER", tmp_path / ".trial_started")
    days = lic_mod.trial_remaining_days()
    assert days == lic_mod.TRIAL_DAYS
    assert lic_mod._TRIAL_MARKER.exists()


# =====================================================================
# 授权状态端点
# =====================================================================
def _make_app():
    from deadman.web import app as web_app

    # 复用一个最小的 app（只挂 license 状态端点逻辑）
    return web_app.app


def test_license_status_endpoint(monkeypatch):
    monkeypatch.delenv("DEADMAN_LICENSE_KEY", raising=False)
    monkeypatch.delenv("DEADMAN_LICENSE_SECRET", raising=False)
    monkeypatch.setattr(lic, "LICENSE_FILE", __import__("pathlib").Path("nonexistent_lic"))
    client = TestClient(_make_app())
    r = client.get("/api/license/status")
    assert r.status_code == 200
    assert r.json()["status"] == "trial"
