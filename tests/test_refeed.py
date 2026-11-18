from concurrent.futures import ThreadPoolExecutor

from app.services.refeed import refeed_object
from app.services.objects import ServiceError


def _seed_open_with_object(authed, zone_code="bronze_payments", key="raw/x.parquet"):
    batches = authed.get("/api/batches").json()["items"]
    target = next(b for b in batches if b["zone_code"] == zone_code)
    r = authed.post(
        f"/api/batches/{target['id']}/objects",
        json={"object_key": key, "content_hash": "h-x", "byte_size": 7},
    )
    assert r.status_code == 200
    return target


def _closed_seed(authed):
    batches = authed.get("/api/batches").json()["items"]
    source = next(b for b in batches if b["status"] == "已闭合")
    objs = authed.get(f"/api/batches/{source['id']}").json()["objects"]
    return source, objs


def test_refeed_copy_preserves_old_close(authed):
    source, objs = _closed_seed(authed)
    target = _seed_open_with_object(authed)
    src_obj = objs[0]

    r = authed.post(
        "/api/refeed",
        json={
            "source_batch_id": source["id"],
            "target_batch_id": target["id"],
            "object_id": src_obj["id"],
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["mode"] == "复制保留"
    assert body["run"]["outcome"] == "已复制"

    # 旧批：对象与批次闭合标记原样不动
    src_detail = authed.get(f"/api/batches/{source['id']}").json()
    assert src_detail["batch"]["status"] == "已闭合"
    old = next(o for o in src_detail["objects"] if o["id"] == src_obj["id"])
    assert old["status"] == "已闭合"
    assert old["source_object_id"] is None

    # 新批：副本为已登记可补采，带溯源列，键/指纹/字节与源一致
    tgt_detail = authed.get(f"/api/batches/{target['id']}").json()
    copy_obj = next(o for o in tgt_detail["objects"] if o["object_key"] == src_obj["object_key"])
    assert copy_obj["status"] == "已登记"
    assert copy_obj["source_object_id"] == src_obj["id"]
    assert copy_obj["source_batch_id"] == source["id"]
    assert copy_obj["content_hash"] == src_obj["content_hash"]
    assert copy_obj["byte_size"] == src_obj["byte_size"]


def test_refeed_second_time_rejected_by_lock(authed):
    source, objs = _closed_seed(authed)
    target = _seed_open_with_object(authed)
    payload = {
        "source_batch_id": source["id"],
        "target_batch_id": target["id"],
        "object_id": objs[0]["id"],
    }
    first = authed.post("/api/refeed", json=payload)
    assert first.status_code == 200

    # 同目标再灌：同键冲突或排他锁，均 409
    again = authed.post("/api/refeed", json=payload)
    assert again.status_code == 409

    # 换一个无同键的新目标：同键冲突不存在，由排他锁明确拒绝
    other_target = _seed_open_with_object(authed, zone_code="bronze_events", key="raw/y.parquet")
    cross = authed.post(
        "/api/refeed",
        json={
            "source_batch_id": source["id"],
            "target_batch_id": other_target["id"],
            "object_id": objs[0]["id"],
        },
    )
    assert cross.status_code == 409
    assert "排他锁" in cross.json()["error"]

    # 只落了一个副本
    tgt = authed.get(f"/api/batches/{target['id']}").json()
    copies = [o for o in tgt["objects"] if o["source_object_id"] == objs[0]["id"]]
    assert len(copies) == 1


def test_refeed_concurrent_exclusive_one_wins(authed):
    # 服务层真线程并发：两检修侧抢同一源对象，BEGIN IMMEDIATE + 持久锁保证一成一败
    source, objs = _closed_seed(authed)
    target = _seed_open_with_object(authed)
    src_obj = objs[1]

    import os

    db_path = os.environ["DATABASE_PATH"]

    def call(_):
        try:
            refeed_object(source["id"], target["id"], src_obj["id"], db_path=db_path)
            return 200
        except ServiceError as exc:
            return exc.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = sorted(pool.map(call, range(2)))

    assert statuses == [200, 409]
    tgt = authed.get(f"/api/batches/{target['id']}").json()
    copies = [o for o in tgt["objects"] if o["source_object_id"] == src_obj["id"]]
    assert len(copies) == 1
    runs = authed.get("/api/refeed-runs").json()["items"]
    assert len(runs) == 1


def test_refeed_mixed_batch_guards(authed):
    source, objs = _closed_seed(authed)
    target = _seed_open_with_object(authed)
    src_obj = objs[0]

    # 源批不是已闭合：找一个收集中批当“源”
    open_batches = authed.get("/api/batches", params={"status": "收集中"}).json()["items"]
    r1 = authed.post(
        "/api/refeed",
        json={
            "source_batch_id": open_batches[0]["id"],
            "target_batch_id": target["id"],
            "object_id": src_obj["id"],
        },
    )
    assert r1.status_code == 409 and "已闭合" in r1.json()["error"]

    # 目标批不是收集中：拿已闭合批当目标
    r2 = authed.post(
        "/api/refeed",
        json={
            "source_batch_id": source["id"],
            "target_batch_id": source["id"],
            "object_id": src_obj["id"],
        },
    )
    assert r2.status_code == 409 and "混批" in r2.json()["error"]

    # 对象不属于源批：把目标批里的对象冒充源批对象
    foreign = authed.get(f"/api/batches/{target['id']}").json()["objects"][0]
    r3 = authed.post(
        "/api/refeed",
        json={
            "source_batch_id": source["id"],
            "target_batch_id": target["id"],
            "object_id": foreign["id"],
        },
    )
    assert r3.status_code == 409 and "混批" in r3.json()["error"]

    # 目标批同键已存在 → 混批即停
    authed.post(
        "/api/refeed",
        json={
            "source_batch_id": source["id"],
            "target_batch_id": target["id"],
            "object_id": src_obj["id"],
        },
    )
    another_target = _seed_open_with_object(authed, zone_code="bronze_events")
    authed.post(
        f"/api/batches/{another_target['id']}/objects",
        json={
            "object_key": src_obj["object_key"],
            "content_hash": "dup",
            "byte_size": 1,
        },
    )
    r4 = authed.post(
        "/api/refeed",
        json={
            "source_batch_id": source["id"],
            "target_batch_id": another_target["id"],
            "object_id": src_obj["id"],
        },
    )
    # 该源对象已被锁，先撞排他锁
    assert r4.status_code == 409


def test_refeed_duplicate_key_in_fresh_target(authed):
    source, objs = _closed_seed(authed)
    target = _seed_open_with_object(authed)
    src_obj = objs[0]
    # 目标批先手动登记同键对象，且源对象尚未回灌过（用第二个源对象测同键冲突）
    other = objs[1]
    authed.post(
        f"/api/batches/{target['id']}/objects",
        json={
            "object_key": other["object_key"],
            "content_hash": "manual",
            "byte_size": 9,
        },
    )
    r = authed.post(
        "/api/refeed",
        json={
            "source_batch_id": source["id"],
            "target_batch_id": target["id"],
            "object_id": other["id"],
        },
    )
    assert r.status_code == 409
    assert "同键" in r.json()["error"]


def test_refeed_bad_payload(authed):
    r = authed.post(
        "/api/refeed",
        json={"source_batch_id": "x", "target_batch_id": 1, "object_id": 2},
    )
    assert r.status_code == 400


def test_refeed_audit_chain_queryable(authed):
    source, objs = _closed_seed(authed)
    target = _seed_open_with_object(authed)
    src_obj = objs[0]
    done = authed.post(
        "/api/refeed",
        json={
            "source_batch_id": source["id"],
            "target_batch_id": target["id"],
            "object_id": src_obj["id"],
        },
    ).json()

    runs = authed.get("/api/refeed-runs").json()["items"]
    assert len(runs) == 1
    run = runs[0]
    assert run["mode"] == "复制保留"
    assert run["outcome"] == "已复制"
    assert run["source_batch_id"] == source["id"]
    assert run["target_batch_id"] == target["id"]
    assert run["source_object_id"] == src_obj["id"]
    assert run["new_object_id"] == done["object"]["id"]
    assert "复制保留" in run["detail"]


def test_visibility_no_mismatch_after_refeed(authed):
    source, objs = _closed_seed(authed)
    target = _seed_open_with_object(authed)
    authed.post(
        "/api/refeed",
        json={
            "source_batch_id": source["id"],
            "target_batch_id": target["id"],
            "object_id": objs[0]["id"],
        },
    )
    report = authed.post(
        "/api/visibility-reports", json={"as_of_date": "2026-11-16"}
    ).json()["report"]
    assert report["mismatch_count"] == 0
    # 复制保留：对象总数 = 旧对象 + 目标批原有 1 + 副本 1
    assert report["object_count"] == len(objs) + 2


def test_refeed_requires_login(client):
    r = client.post(
        "/api/refeed",
        json={"source_batch_id": 1, "target_batch_id": 2, "object_id": 1},
    )
    assert r.status_code == 401
