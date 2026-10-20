from app.collector import Worker, needs_login
from app.storage import Store


def test_login_detection():
    assert needs_login("https://mobile.yangkeduo.com/login.html", "")
    assert needs_login("https://mobile.yangkeduo.com/goods.html", "请完成安全验证")
    assert not needs_login("https://mobile.yangkeduo.com/goods.html", "收纳盒 注意事项：远离火源")


def test_login_resume_keeps_same_job(tmp_path, source_url):
    store = Store(tmp_path)
    created = store.create("login-test", source_url)
    job = store.claim()

    class Page:
        def wait_for_timeout(self, timeout):
            assert store.get(job["id"])["status"] == "needs_login"
            store.resume(job["id"])

    Worker(store).wait_for_user(Page(), job)
    assert store.get(created["id"])["status"] == "running"
    assert len(store.list()) == 1
    assert store.claim() is None
